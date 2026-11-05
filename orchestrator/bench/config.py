from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


def parse_cpus(spec: str) -> list[int]:
    """'1-2,4' -> [1, 2, 4]"""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            out += range(int(a), int(b) + 1)
        else:
            out.append(int(part))
    return out


@dataclass
class Bench:
    name: str
    cmd: str            # netbench subcommand: ttfb | latency | tput
    args: list[str]     # extra netbench flags (rate, size, dir, streams, ...)
    warmup: float
    duration: float


@dataclass
class Config:
    experiment: str
    results_dir: Path
    netbench: Path           # netbench binary on the host
    runtime: str             # netns | docker
    docker_runtime: str      # e.g. "runsc" for gVisor, "" for the default runc
    docker_image: str
    cores: dict[str, str]    # os | sandbox | mediator | target
    setups: list[str]
    ns: list[int]
    runs: int
    max_retries: int
    lead_base_s: float
    seed: int
    benchmarks: list[Bench]
    raw: dict


def load(path: str | Path) -> Config:
    p = Path(path)
    raw = tomllib.loads(p.read_text())
    base = p.parent
    benches = [
        Bench(b["name"], b["cmd"], [str(x) for x in b.get("args", [])], float(b["warmup"]), float(b["duration"]))
        for b in raw.get("benchmark", [])
    ]
    return Config(
        experiment=raw.get("experiment", "experiment"),
        results_dir=(base / raw.get("results_dir", "results")).resolve(),
        netbench=(base / raw.get("netbench", "./netbench")).resolve(),
        runtime=raw.get("runtime", "netns"),
        docker_runtime=raw.get("docker_runtime", ""),
        docker_image=raw.get("docker_image", "busybox:latest"),
        cores=raw["cores"],
        setups=[str(s) for s in raw.get("setups", ["0", "1"])],
        ns=[int(n) for n in raw.get("ns", [1, 5, 10, 20, 50])],
        runs=int(raw.get("runs", 3)),
        max_retries=int(raw.get("max_retries", 2)),
        lead_base_s=float(raw.get("lead_base_s", 3.0)),
        seed=int(raw.get("seed", 1)),
        benchmarks=benches,
        raw=raw,
    )
