from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path

from .config import Config
from .runtime import Runtime
from .sh import run


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return ""


def _freq() -> dict:
    """Frequency limits and driver per online cpu, so runs can be compared only under equal tuning."""
    out = {}
    for i in range(os.cpu_count() or 1):
        base = f"/sys/devices/system/cpu/cpu{i}/cpufreq"
        if Path(base).exists():
            out[f"cpu{i}"] = {"min_khz": _read(f"{base}/scaling_min_freq"), "max_khz": _read(f"{base}/scaling_max_freq"),
                              "driver": _read(f"{base}/scaling_driver")}
    return out


def _cpuidle() -> dict:
    """Idle states per cpu as 'name=on|off(exit latency us)'. Deep states change wake-up latency a lot."""
    out = {}
    for i in range(os.cpu_count() or 1):
        base = Path(f"/sys/devices/system/cpu/cpu{i}/cpuidle")
        if not base.exists():
            continue
        parts = []
        for st in sorted(base.glob("state*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0):
            parts.append(f"{_read(f'{st}/name')}={'off' if _read(f'{st}/disable') == '1' else 'on'}({_read(f'{st}/latency')}us)")
        out[f"cpu{i}"] = " ".join(parts)
    return out


def _envoy(cfg: Config) -> dict:
    """Envoy version, recorded when a setup that uses it is part of the experiment."""
    if not any(x in cfg.setups for x in ("2", "3")):
        return {}
    cp = run([cfg.envoy, "--version"], check=False, timeout=30)
    text = (cp.stdout or cp.stderr).strip().splitlines()
    return {"envoy": text[-1].strip() if cp.returncode == 0 and text else None}


def collect(cfg: Config, rt: Runtime) -> dict:
    nb = cfg.netbench
    govs = sorted({_read(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_governor")
                   for i in range(os.cpu_count() or 1)} - {""})
    return {
        "experiment": cfg.experiment,
        "started_unix": round(time.time(), 3),
        "host": {"kernel": platform.release(), "cpu": _cpu_model(), "cpus": os.cpu_count(),
                 "smt_active": _read("/sys/devices/system/cpu/smt/active"), "governors": govs,
                 "boost": _read("/sys/devices/system/cpu/cpufreq/boost"), "freq": _freq(), "cpuidle": _cpuidle(),
                 "nf_conntrack_max": _read("/proc/sys/net/netfilter/nf_conntrack_max"), "nf_conntrack_buckets": _read("/sys/module/nf_conntrack/parameters/hashsize"), "python": sys.version.split()[0]},
        "tools": {"nft": run(["nft", "--version"], check=False).stdout.strip(),
                  "ip": run(["ip", "-V"], check=False).stdout.strip(),
                  "conntrack": bool(shutil.which("conntrack")), **rt.version_info(), **_envoy(cfg)},
        "netbench": {"path": str(nb), "sha256": hashlib.sha256(nb.read_bytes()).hexdigest() if nb.exists() else None},
        "config": cfg.raw,
    }


def write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


COMPARE_KEYS = ["tools.envoy", "config.runtime", "config.docker_runtime", "config.docker_image", "config.cores", "config.benchmark",
                "netbench.sha256", "host.smt_active", "host.governors", "host.boost", "host.freq", "host.cpuidle"]


def _dig(d, path):
    for k in path.split("."):
        d = d.get(k) if isinstance(d, dict) else None
    return d


def compare(old: dict, new: dict) -> list[str]:
    """What differs between the session that started an experiment and this one (things that change results)."""
    out = []
    for k in COMPARE_KEYS:
        o, n = _dig(old, k), _dig(new, k)
        if o != n:
            out.append(f"{k}: {str(o)[:70]} -> {str(n)[:70]}")
    return out
