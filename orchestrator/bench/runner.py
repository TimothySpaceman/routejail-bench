from __future__ import annotations

import json
import os
import random
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import subprocess

from . import manifest
from .config import Bench, Config, parse_cpus
from .runtime import Runtime, Sandbox, make_runtime
from .sampler import CoreSampler
from .setups import Setup, make_setup
from .sh import log, run
from .topology import Topology

SCENARIO = "outbound"


class ProbeFailure(RuntimeError):
    pass


@dataclass
class ClientResult:
    sb: Sandbox
    rc: int
    stdout: str
    stderr: str
    data: dict | None


def _jsonline(text: str) -> dict | None:
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


class Runner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rt: Runtime = make_runtime(cfg)
        self.topo = Topology(cfg)
        self.out = cfg.results_dir / cfg.experiment
        self.lead_extra = 0.0

    # ---------- plan ----------

    def run_all(self):
        cpus = parse_cpus(self.cfg.cores["os"])
        os.sched_setaffinity(0, set(cpus))          # the orchestrator stays on the OS cores
        self.out.mkdir(parents=True, exist_ok=True)
        manifest.write(self.out / "manifest.json", manifest.collect(self.cfg, self.rt))
        plan = [(s, n) for s in self.cfg.setups for n in self.cfg.ns]
        random.Random(self.cfg.seed).shuffle(plan)
        log.info("plan: %s", plan)
        for setup_id, n in plan:
            if self._group_done(setup_id, n):
                log.info("skip setup %s n=%d (already done)", setup_id, n)
                continue
            try:
                self.run_group(setup_id, n)
            except Exception:
                log.error("group setup=%s n=%d FAILED\n%s", setup_id, n, traceback.format_exc())
                gdir = self._group_dir(setup_id, n)
                gdir.mkdir(parents=True, exist_ok=True)
                (gdir / "failed.json").write_text(json.dumps({"error": traceback.format_exc()}, indent=2))

    def _group_dir(self, setup_id: str, n: int) -> Path:
        from .setups import SETUPS
        return self.out / SETUPS[setup_id].slug / SCENARIO / f"n{n}"

    def _cell_dir(self, setup_id: str, n: int, bench: Bench) -> Path:
        from .setups import SETUPS
        return self.out / SETUPS[setup_id].slug / SCENARIO / bench.name / f"n{n}"

    def _group_done(self, setup_id: str, n: int) -> bool:
        return all((self._cell_dir(setup_id, n, b) / "done.json").exists() for b in self.cfg.benchmarks)

    # ---------- one group: N sandboxes of one setup, all benchmarks, all runs ----------

    def run_group(self, setup_id: str, n: int):
        log.info("=== setup %s, n=%d ===", setup_id, n)
        self.rt.cleanup_all()
        setup = make_setup(setup_id, self.cfg, self.rt)
        self.topo.up()
        try:
            self.topo.start_servers()
            sandboxes = setup.up(n)
            self.topo.allow_replies()
            self._probe_group(setup, sandboxes, self._group_dir(setup_id, n))
            for bench in self.cfg.benchmarks:
                cell = self._cell_dir(setup_id, n, bench)
                if (cell / "done.json").exists():
                    continue
                for r in range(1, self.cfg.runs + 1):
                    self._run_one(setup, sandboxes, bench, n, r, cell)
                (cell / "done.json").write_text(json.dumps({"finished_unix": time.time()}) + "\n")
        finally:
            for step in (setup.down, self.topo.down):
                try:
                    step()
                except Exception:
                    log.warning("teardown step failed: %s", traceback.format_exc())

    # ---------- probes ----------

    def _probe_group(self, setup: Setup, sandboxes: list[Sandbox], gdir: Path):
        gdir.mkdir(parents=True, exist_ok=True)

        def one(item):
            sb, p = item
            argv = self.rt.exec_argv(sb, [self.rt.binary, "probe", "--target", p.target, "--expect", p.expect,
                                          "--timeout", "2s"], self.cfg.cores["sandbox"])
            cp = run(argv, check=False, timeout=30)
            return {"sandbox": sb.name, "probe": p.label, "target": p.target, "expect": p.expect,
                    "rc": cp.returncode, "result": _jsonline(cp.stdout)}

        items = [(sb, p) for sb in sandboxes for p in setup.probes(sb)]
        with ThreadPoolExecutor(max_workers=min(len(items), 32)) as ex:
            results = list(ex.map(one, items))
        (gdir / "probes.json").write_text(json.dumps(results, indent=2) + "\n")
        bad = [r for r in results if r["rc"] != 0]
        if bad:
            try:
                log.error("network and firewall state at the time of the probe failure:\n%s", self.topo.diagnose())
            except Exception:
                pass
            raise ProbeFailure(f"{len(bad)} probe(s) failed, e.g. {bad[0]}")
        log.info("probes ok (%d)", len(results))

    # ---------- one run (with retries for invalid results) ----------

    def _run_one(self, setup: Setup, sandboxes: list[Sandbox], bench: Bench, n: int, r: int, cell: Path):
        final = cell / f"run{r}"
        if final.exists():
            return
        attempts = self.cfg.max_retries + 1
        for attempt in range(1, attempts + 1):
            tmp = cell / f"run{r}.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            results, cores, meta = self._execute(setup, sandboxes, bench, n, r, tmp)
            ok = all(c.rc == 0 and c.data and c.data.get("valid") for c in results)
            self._write_run(tmp, results, cores, meta | {"attempt": attempt, "accepted": ok})
            log.info("%s n=%d run%d attempt %d: %s", bench.name, n, r, attempt, "ok" if ok else "INVALID")
            run(["conntrack", "-F"], check=False)
            if ok or attempt == attempts:
                tmp.rename(final)
                break
            tmp.rename(cell / f"run{r}.rejected{attempt}")
            time.sleep(2)
        time.sleep(1)

    def _execute(self, setup, sandboxes, bench: Bench, n: int, r: int, tmp: Path):
        lead = self.cfg.lead_base_s + n * self.rt.launch_cost_s + self.lead_extra
        start_at = time.time() + lead
        timeout = lead + bench.warmup + bench.duration + 30
        sampler = CoreSampler()

        def one(sb: Sandbox) -> ClientResult:
            labels = {"setup": setup.id, "scenario": SCENARIO, "bench": bench.name, "n": n, "run": r, "sandbox": sb.name}
            argv = [self.rt.binary, bench.cmd, "--target", setup.target_for(sb),
                    "--warmup", f"{bench.warmup:g}s", "--duration", f"{bench.duration:g}s",
                    "--start-at", f"{start_at:.3f}", "--phase-key", sb.name, *bench.args]
            for k, v in labels.items():
                argv += ["--label", f"{k}={v}"]
            try:
                cp = run(self.rt.exec_argv(sb, argv, self.cfg.cores["sandbox"]), check=False, timeout=timeout)
                return ClientResult(sb, cp.returncode, cp.stdout, cp.stderr, _jsonline(cp.stdout))
            except subprocess.TimeoutExpired:
                return ClientResult(sb, -1, "", "orchestrator timeout", None)

        sampler.start()
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(one, sandboxes))
        t1 = time.time()
        cores = sampler.stop()
        if any("start-at" in c.stderr for c in results):      # clients could not start in time: grow the lead
            self.lead_extra += 3.0
            log.warning("start lead too short, increasing by 3s (now +%.0fs)", self.lead_extra)
        meta = {"start_at": start_at, "lead_s": lead, "launched_unix": t0, "finished_unix": t1,
                "bench": bench.name, "cmd": bench.cmd, "args": bench.args, "n": n, "run": r}
        return results, cores, meta

    def _write_run(self, d: Path, results: list[ClientResult], cores: dict, meta: dict):
        log_lines = []
        for c in results:
            if c.data is not None:
                (d / f"{c.sb.name}.json").write_text(json.dumps(c.data) + "\n")
            log_lines.append(f"[{c.sb.name}] rc={c.rc} {c.stderr.strip()}")
            if c.data is None:
                (d / f"{c.sb.name}.stdout").write_text(c.stdout)
        (d / "stderr.log").write_text("\n".join(log_lines) + "\n")
        (d / "cores.json").write_text(json.dumps(cores) + "\n")
        meta = meta | {"rc": {c.sb.name: c.rc for c in results},
                       "valid": {c.sb.name: bool(c.data and c.data.get("valid")) for c in results}}
        (d / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
