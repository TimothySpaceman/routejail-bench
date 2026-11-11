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
    launch_cost_s: float | None   # seconds to start one client in a sandbox; None = runtime default
    scenarios: list[str]          # outbound (sandbox -> target) and/or peer (sandbox -> sandbox, in a ring)
    envoy: str                    # envoy binary (path or name on PATH), used by setups 2 and 3
    envoy_admin_port: int
    router: str                   # command that starts the router on the host (setup 4), e.g. "/opt/router/Router"
    router_url: str               # where the router's REST API listens
    sidecar: str                  # sidecar executable on the host; it is mounted into every sandbox (setup 4)
    seed: int
    benchmarks: list[Bench]
    raw: dict


RUNTIME_ALIASES = {            # what people naturally write -> (runtime, docker_runtime)
    "runsc": ("docker", "runsc"), "gvisor": ("docker", "runsc"), "runc": ("docker", ""),
}


def normalize_runtime(raw: dict) -> None:
    """runtime is netns or docker; gVisor is docker with docker_runtime = "runsc".
    Aliases are accepted and written back, so the manifest always records the explicit form."""
    rt = raw.get("runtime", "netns")
    if rt in RUNTIME_ALIASES:
        raw["runtime"], raw["docker_runtime"] = RUNTIME_ALIASES[rt]
    elif rt not in ("netns", "docker"):
        raise ValueError(f'runtime = "{rt}" is not valid. Use runtime = "docker" (with docker_runtime = "runsc" for gVisor, '
                         f'or "" for runc), or runtime = "netns". Shortcuts also accepted: runsc, gvisor, runc.')
    raw.setdefault("runtime", "netns")


def load(path: str | Path) -> Config:
    p = Path(path)
    raw = tomllib.loads(p.read_text())
    normalize_runtime(raw)
    base = p.parent
    benches = [
        Bench(b["name"], b["cmd"], [str(x) for x in b.get("args", [])], float(b["warmup"]), float(b["duration"]))
        for b in raw.get("benchmark", [])
    ]
    bad = [x for x in raw.get("scenarios", ["outbound"]) if x not in ("outbound", "peer")]
    if bad:
        raise ValueError(f'unknown scenario {bad}: use "outbound" and/or "peer"')
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
        launch_cost_s=(float(raw["launch_cost_s"]) if "launch_cost_s" in raw else None),
        scenarios=[str(x) for x in raw.get("scenarios", ["outbound"])],
        envoy=raw.get("envoy", "envoy"),
        envoy_admin_port=int(raw.get("envoy_admin_port", 9901)),
        router=raw.get("router", ""),
        router_url=raw.get("router_url", "http://127.0.0.1:5010"),
        sidecar=raw.get("sidecar", ""),
        seed=int(raw.get("seed", 1)),
        benchmarks=benches,
        raw=raw,
    )
