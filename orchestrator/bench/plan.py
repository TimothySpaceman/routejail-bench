"""Dry run: what `run` would do, how long it should take and how much disk it needs. No root, no host access.

The timing constants are rough (the orchestrator has no way to know how slow your Docker is); calibrate them from
a real group's log. Everything else (counts, structure) is exact.
"""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .setups import SETUPS

# seconds to create and later remove ONE sandbox, per runtime (rough guesses)
SANDBOX_COST = {"netns": 0.15, "docker": 1.5, "runsc": 3.0}
EXTRA_PER_GROUP = {"2": 3.0, "3": 6.0, "4": 8.0}      # starting the mediator (and, for 3, the relays) and its readiness wait
RUN_GAP_S = 4.0                              # pauses and conntrack flush around every run
PROBE_BASE_S = 5.0
# bytes on disk (measured on real result files)
SANDBOX_JSON = {"ttfb": 9000, "latency": 12000, "tput": 1500}
RUN_FILES = 2500                             # run.json + cores.json + stderr.log + directory entries
PROCS_JSON = 6000                            # procs.json per instance group, rough


def runtime_key(cfg: Config) -> str:
    if cfg.runtime == "docker":
        return "runsc" if cfg.docker_runtime == "runsc" else "docker"
    return cfg.runtime


def build_plan(cfg: Config, launch_cost: float | None = None) -> dict:
    rk = runtime_key(cfg)
    if launch_cost is None:
        launch_cost = cfg.launch_cost_s if cfg.launch_cost_s is not None else {"netns": 0.05, "docker": 0.4, "runsc": 1.0}[rk]
    out_dir = cfg.results_dir / cfg.experiment
    groups, total_s, total_runs, total_files, total_bytes = [], 0.0, 0, 0, 0
    for setup_id in cfg.setups:
        known = setup_id in SETUPS
        for n in cfg.ns:
            secs = n * SANDBOX_COST[rk] + PROBE_BASE_S + 0.05 * n + EXTRA_PER_GROUP.get(setup_id, 0.0)
            runs = files = size = 0
            done_cells = 0
            scenarios = [s for s in cfg.scenarios if s != "peer" or n >= 2]      # the peer scenario needs at least two sandboxes
            if "peer" in scenarios:
                secs += n * 0.3 + 5.0                                           # starting a server in every sandbox and checking the peer path
            for scenario in scenarios:
                for b in cfg.benchmarks:
                    per_run = cfg.lead_base_s + n * launch_cost + b.warmup + b.duration + RUN_GAP_S
                    secs += cfg.runs * per_run
                    runs += cfg.runs
                    files += cfg.runs * (n + 3)
                    size += cfg.runs * (n * SANDBOX_JSON.get(b.cmd, 3000) + RUN_FILES + (PROCS_JSON if setup_id in EXTRA_PER_GROUP else 0))
                    if known and (out_dir / SETUPS[setup_id].slug / scenario / b.name / f"n{n}" / "done.json").exists():
                        done_cells += 1
            groups.append({"setup": setup_id, "slug": SETUPS[setup_id].slug if known else f"setup {setup_id} (not built)", "n": n, "runs": runs,
                           "est_min": secs / 60, "done_cells": done_cells, "cells": len(scenarios) * len(cfg.benchmarks), "implemented": known})
            if known:
                total_s += secs
                total_runs += runs
                total_files += files
                total_bytes += size
    return {"experiment": cfg.experiment, "runtime": rk, "groups": groups, "total_hours": total_s / 3600, "total_runs": total_runs,
            "total_files": total_files, "total_mb": total_bytes / 1e6, "launch_cost_s": launch_cost}


def format_plan(cfg: Config, plan: dict) -> str:
    L = [f"experiment: {plan['experiment']}   runtime: {plan['runtime']}   results: {cfg.results_dir / cfg.experiment}",
         f"setups {cfg.setups} x N {cfg.ns} x scenarios {cfg.scenarios} x {len(cfg.benchmarks)} benchmarks x {cfg.runs} runs   (client launch cost {plan['launch_cost_s']} s)",
         "", f"{'setup':26}{'N':>4}{'runs':>6}{'est. min':>10}   state"]
    for g in plan["groups"]:
        state = "NOT IMPLEMENTED" if not g["implemented"] else ("done" if g["done_cells"] == g["cells"] else
                f"{g['done_cells']}/{g['cells']} cells done" if g["done_cells"] else "to do")
        est = f"{g['est_min']:>10.1f}" if g["implemented"] else f"{'-':>10}"
        L.append(f"{g['slug']:26}{g['n']:>4}{g['runs']:>6}{est}   {state}")
    todo_h = sum(g["est_min"] for g in plan["groups"] if g["implemented"] and g["done_cells"] < g["cells"]) / 60
    L += ["", f"total runs: {plan['total_runs']}    result files: ~{plan['total_files']}    disk: ~{plan['total_mb']:.0f} MB",
          f"estimated time: {plan['total_hours']:.1f} h for the whole grid, {todo_h:.1f} h still to do (rough: sandbox creation times are guesses, the rest is arithmetic)"]
    return "\n".join(L)
