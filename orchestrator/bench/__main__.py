from __future__ import annotations

import argparse
import copy
import logging
import os
import shutil
import sys
from pathlib import Path

from . import config as cfgmod
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
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser(prog="bench")
    ap.add_argument("command", choices=["check", "smoke", "run", "cleanup"])
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    cfg = cfgmod.load(args.config)

    if args.command == "check":
        sys.exit(cmd_check(cfg))
    if args.command == "cleanup":
        setup_logging(None, args.verbose)
        from .topology import Topology
        make_runtime(cfg).cleanup_all()
        Topology(cfg).down()
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
    Runner(cfg).run_all()


main()
