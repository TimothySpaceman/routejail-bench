from __future__ import annotations

import argparse
import copy
import fcntl
import logging
import os
import shutil
import signal
import sys
from pathlib import Path

from . import config as cfgmod
from .plan import build_plan, format_plan
from .preflight import Finding, envoy_version, is_dynamic_elf, router_findings, setup_findings, tuning_findings, uds_selftest
from .runner import Runner
from .runtime import make_runtime
from .sh import log, run

SMOKE_BENCHES = ("ttfb", "latency", "tput_up_1")


def setup_logging(out: Path | None, verbose: bool):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    h = logging.StreamHandler()
    h.setFormatter(fmt)
    log.addHandler(h)
    if out:
        out.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(out / "orchestrator.log")
        fh.setFormatter(fmt)
        log.addHandler(fh)


def cmd_check(cfg) -> int:
    problems = 0

    def rep(level, what, detail=""):
        nonlocal problems
        if level == "FAIL":
            problems += 1
        print(f"{level:5} {what}{(': ' + detail) if detail else ''}")

    rep("OK" if os.geteuid() == 0 else "FAIL", "running as root", "needed for netns/nft/docker")
    tools = ["ip", "nft", "taskset"] + (["docker"] if cfg.runtime == "docker" else [])
    for t in tools:
        rep("OK" if shutil.which(t) else "FAIL", f"tool {t}")
    rep("OK" if shutil.which("conntrack") else "WARN", "tool conntrack", "used to flush state between runs")
    nb = cfg.netbench
    rep("OK" if nb.exists() and os.access(nb, os.X_OK) else "FAIL", f"netbench binary {nb}")
    if nb.exists():
        ok = run([str(nb), "help"], check=False).returncode == 0
        rep("OK" if ok else "FAIL", "netbench starts", "" if ok else "wrong architecture, or the binary is damaged")
        if cfg.runtime == "docker":
            rep("FAIL" if is_dynamic_elf(nb) else "OK", "netbench is statically linked",
                "rebuild with CGO_ENABLED=0 (a dynamic binary cannot start in a busybox container)" if is_dynamic_elf(nb) else "")
    ncpu = os.cpu_count() or 1
    used = sorted({c for spec in cfg.cores.values() for c in cfgmod.parse_cpus(spec)})
    rep("OK" if used and max(used) < ncpu else "FAIL", "core layout", f"{cfg.cores} on {ncpu} cpus")
    smt = Path("/sys/devices/system/cpu/smt/active")
    if smt.exists() and smt.read_text().strip() == "1":
        rep("WARN", "SMT is on", "pin roles to physical cores or disable SMT")
    gov = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    if gov.exists() and gov.read_text().strip() != "performance":
        rep("WARN", "cpu governor", f"{gov.read_text().strip()} (use performance)")
    if cfg.runtime == "docker":
        rep("OK" if run(["docker", "info"], check=False).returncode == 0 else "FAIL", "docker daemon")
        ver = run(["docker", "version", "--format", "{{.Server.Version}}"], check=False).stdout.strip()
        rep("OK", "docker server version", ver or "unknown")
        if cfg.docker_runtime:
            rep("OK" if shutil.which(cfg.docker_runtime) else "WARN", f"runtime {cfg.docker_runtime}")
        # end-to-end self-test: image present, runtime registered, cpuset accepted, binary mounted and executable inside
        cmd = ["docker", "run", "--rm"] + (["--runtime", cfg.docker_runtime] if cfg.docker_runtime else []) + \
              ["--cpuset-cpus", cfg.cores["sandbox"], "-v", f"{nb}:/netbench:ro", cfg.docker_image, "/netbench", "help"]
        cp = run(cmd, check=False, timeout=120)
        rep("OK" if cp.returncode == 0 else "FAIL", "sandbox self-test (start a container, run netbench inside)",
            "" if cp.returncode == 0 else (cp.stderr.strip().splitlines() or ["failed"])[-1][:140])
    if any(x in cfg.setups for x in ("2", "3")):
        ver = envoy_version(cfg)
        rep("OK" if ver else "FAIL", f"envoy ({cfg.envoy})", ver or "not found: install it (see README.md) or set `envoy = \"/path/to/envoy\"` in the config")
    if "4" in cfg.setups:
        found = router_findings(cfg)
        if not found:
            rep("OK", "router command and sidecar executable (setup 4)")
        for f in found:
            rep(f.level, f.what, f.fix)
    if any(x in cfg.setups for x in ("3", "4")) and cfg.runtime == "docker" and nb.exists():
        ok, detail = uds_selftest(cfg)
        rep("OK" if ok else "FAIL", "sandbox can reach a host Unix socket (needed by setups 3 and 4)",
            "" if ok else detail + (" | gVisor needs runsc started with --host-uds=open (see README.md)" if cfg.docker_runtime else ""))
    for f in tuning_findings(cfg):
        rep(f.level, f.what + " (blocks `run`, only warned by `smoke`)" if f.level == "FAIL" else f.what, f.fix)
    return 1 if problems else 0


def acquire_lock():
    f = open("/tmp/bench-orchestrator.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Another benchmark orchestrator is already running on this host (lock /tmp/bench-orchestrator.lock).")
    return f


def main():
    ap = argparse.ArgumentParser(prog="bench")
    ap.add_argument("command", choices=["check", "plan", "smoke", "run", "cleanup"])
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--force", action="store_true", help="run despite failed tuning checks, or continue an experiment whose setup changed")
    args = ap.parse_args()
    cfg = cfgmod.load(args.config)

    if args.command == "check":
        sys.exit(cmd_check(cfg))
    if args.command == "plan":
        print(format_plan(cfg, build_plan(cfg)))
        return
    lock = acquire_lock()                        # one orchestrator per host: the network names are global
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))   # so teardown runs on `kill`
    if args.command == "cleanup":
        setup_logging(None, args.verbose)
        from .topology import Topology
        make_runtime(cfg).cleanup_all()
        Topology(cfg).down()
        run(["nft", "delete", "table", "inet", "bench"], check=False)
        from .setups import kill_leftovers
        kill_leftovers(cfg)
        return
    if args.command == "smoke":
        cfg = copy.copy(cfg)
        cfg.experiment += "-smoke"
        cfg.ns, cfg.runs, cfg.max_retries = [2], 1, 1
        picked = [b for b in cfg.benchmarks if b.name in SMOKE_BENCHES] or cfg.benchmarks[:3]
        cfg.benchmarks = [copy.copy(b) for b in picked]
        for b in cfg.benchmarks:
            b.warmup, b.duration = 1.0, 3.0
    setup_logging(cfg.results_dir / cfg.experiment, args.verbose)
    tuning, prereq = tuning_findings(cfg), setup_findings(cfg)
    for f in tuning + prereq:
        log.warning("%s: %s  (%s)", f.level, f.what, f.fix)
    if any(f.level == "FAIL" for f in prereq) and not args.force:
        sys.exit("Not starting: a configured setup is missing something (see above). Fix it, remove that setup from `setups`, or pass --force.")
    if args.command == "run" and any(f.level == "FAIL" for f in tuning) and not args.force:
        sys.exit("Refusing to start the full run: the host is not in the benchmark state (see above). Fix it, or pass --force.")
    try:
        Runner(cfg).run_all(force=args.force)
    except KeyboardInterrupt:
        log.warning("interrupted; sandboxes and firewall rules were removed (finished cells are kept; rerun to resume)")
        sys.exit(130)


main()
