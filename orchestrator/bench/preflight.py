"""Checks that must pass before a long run: host tuning applied, conntrack big enough, binary usable.

`check` prints all of them; `run` refuses to start when a FAIL is present (override with --force);
`smoke` only warns, because it tests plumbing, not performance.
"""
from __future__ import annotations

import os
import platform
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config, parse_cpus
from .sh import run

SYS = Path("/sys/devices/system/cpu")
CT_MAX = Path("/proc/sys/net/netfilter/nf_conntrack_max")


@dataclass
class Finding:
    level: str      # FAIL | WARN
    what: str
    fix: str = ""


def _read(p) -> str:
    try:
        return Path(p).read_text().strip()
    except OSError:
        return ""


def is_dynamic_elf(path: Path) -> bool:
    """True if the binary needs an interpreter (dynamically linked) and would not start in a busybox container."""
    try:
        with open(path, "rb") as f:
            h = f.read(64)
            if h[:4] != b"\x7fELF" or h[4] != 2:        # 64-bit ELF only
                return False
            phoff, = struct.unpack_from("<Q", h, 0x20)
            phentsize, phnum = struct.unpack_from("<HH", h, 0x36)
            f.seek(phoff)
            for _ in range(phnum):
                ph = f.read(phentsize)
                if struct.unpack_from("<I", ph, 0)[0] == 3:      # PT_INTERP
                    return True
    except OSError:
        pass
    return False


def conntrack_demand(cfg: Config) -> int:
    """New connections per run in the busiest TTFB benchmark at the largest N (each one becomes a tracked flow)."""
    worst = 0
    for b in cfg.benchmarks:
        if b.cmd != "ttfb":
            continue
        rate = 100.0
        if "--rate" in b.args:
            try:
                rate = float(b.args[b.args.index("--rate") + 1])
            except (ValueError, IndexError):
                pass
        worst = max(worst, max(cfg.ns) * rate * (b.warmup + b.duration))
    return int(worst * 1.2)


def tuning_findings(cfg: Config) -> list[Finding]:
    out: list[Finding] = []
    fix = "run: sudo ./host-tune.sh --verify"
    cpus = sorted({c for spec in cfg.cores.values() for c in parse_cpus(spec)})
    if _read(SYS / "smt/active") == "1":
        out.append(Finding("FAIL", "SMT is on", fix))
    bad_gov = [c for c in cpus if _read(SYS / f"cpu{c}/cpufreq/scaling_governor") not in ("", "performance")]
    if bad_gov:
        out.append(Finding("FAIL", f"cpu governor is not 'performance' on cpus {bad_gov}", fix))
    loose = [c for c in cpus if (lo := _read(SYS / f"cpu{c}/cpufreq/scaling_min_freq"))
             and lo != _read(SYS / f"cpu{c}/cpufreq/scaling_max_freq")]
    if loose:
        out.append(Finding("FAIL", f"clock is not pinned (min != max frequency) on cpus {loose}", fix))
    deep = []
    for c in cpus:
        for st in sorted((SYS / f"cpu{c}/cpuidle").glob("state*")):
            if st.name[5:].isdigit() and int(st.name[5:]) >= 2 and _read(st / "disable") == "0":
                deep.append(f"cpu{c}:{_read(st / 'name')}")
    if deep:
        out.append(Finding("FAIL", f"deep idle states enabled ({', '.join(deep[:6])}{'...' if len(deep) > 6 else ''})", fix))
    cap = _read(CT_MAX)
    if cap:
        demand = conntrack_demand(cfg)
        if demand > 0.7 * int(cap):
            out.append(Finding("FAIL", f"connection tracking table too small: up to ~{demand} new connections per run vs nf_conntrack_max={cap}",
                               "raise it: sudo ./host-tune.sh (sets nf_conntrack_max), or lower the ttfb rate / N"))
        elif demand > 0.4 * int(cap):
            out.append(Finding("WARN", f"connection tracking table is tight: ~{demand} new connections per run vs nf_conntrack_max={cap}", "sudo ./host-tune.sh"))
    return out


def envoy_version(cfg: Config) -> str | None:
    """Version string of the configured Envoy, or None if it cannot be started."""
    cp = run([cfg.envoy, "--version"], check=False, timeout=30)
    if cp.returncode != 0:
        return None
    text = (cp.stdout or cp.stderr).strip().splitlines()
    return text[-1].strip() if text else "unknown"


def uds_selftest(cfg: Config) -> tuple[bool, str]:
    """Can a sandbox (docker runtime, no network) connect to a Unix socket mounted from the host?
    Needed by setup 3. Under gVisor this requires runsc to run with --host-uds=open."""
    d = Path("/tmp/bench-check")
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    srv = subprocess.Popen([str(cfg.netbench), "server", "--listen", f"unix:{d}/s.sock"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(30):
            if (d / "s.sock").exists():
                break
            time.sleep(0.1)
        cmd = ["docker", "run", "--rm", "--network", "none"] + (["--runtime", cfg.docker_runtime] if cfg.docker_runtime else []) + \
              ["--cpuset-cpus", cfg.cores["sandbox"], "-v", f"{cfg.netbench}:/netbench:ro", "-v", f"{d}:/sock", cfg.docker_image,
               "/netbench", "probe", "--target", "unix:/sock/s.sock", "--expect", "ok", "--timeout", "3s"]
        cp = run(cmd, check=False, timeout=120)
        if cp.returncode == 0:
            return True, ""
        hint = (cp.stdout.strip() or cp.stderr.strip()).splitlines()
        return False, (hint[-1][:150] if hint else "failed")
    finally:
        srv.terminate()
        shutil.rmtree(d, ignore_errors=True)


def router_findings(cfg: Config) -> list[Finding]:
    """What setup 4 needs: a startable router command and a native sidecar executable."""
    import shlex
    out: list[Finding] = []
    if not cfg.router:
        out.append(Finding("FAIL", "`router` is not set in the config", "README.md"))
    else:
        first = shlex.split(cfg.router)[0]
        found = shutil.which(first) or (first if Path(first).exists() else None)
        if not found:
            out.append(Finding("FAIL", f"router command not found: {first}", "publish the router (see README.md) and set `router`"))
        elif exe_problem(found):
            out.append(Finding("FAIL", f"router {exe_problem(found)}: {found}",
                               "publish it on/for this machine: dotnet publish Router -c Release -r linux-x64 (see README.md)"))
    if not cfg.sidecar:
        out.append(Finding("FAIL", "`sidecar` is not set in the config", "README.md"))
    else:
        sc = Path(cfg.sidecar)
        if not sc.exists():
            out.append(Finding("FAIL", f"sidecar not found: {sc}", "publish it self-contained (see README.md)"))
        elif not os.access(sc, os.X_OK):
            out.append(Finding("FAIL", f"sidecar is not executable: {sc}", "chmod +x"))
        elif exe_problem(sc):
            out.append(Finding("FAIL", f"sidecar {exe_problem(sc)}: {sc}", "publish it self-contained for this machine: -r linux-x64 --self-contained (see README.md)"))
        elif cfg.runtime == "docker" and exe_kind(sc) == "script":
            out.append(Finding("FAIL", f"sidecar is not a native executable: {sc}", "a script cannot run in the sandbox image; publish self-contained"))
    return out


def setup_findings(cfg: Config) -> list[Finding]:
    """Prerequisites of the configured setups, so a long run does not fail at its last group."""
    out: list[Finding] = []
    if any(x in cfg.setups for x in ("2", "3")) and not envoy_version(cfg):
        out.append(Finding("FAIL", f"envoy not found ({cfg.envoy})", "install it (see README.md)"))
    if "4" in cfg.setups:
        out += router_findings(cfg)
    return out


_MACHO = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce", b"\xbe\xba\xfe\xca")
_HOST_KIND = {"x86_64": "elf-x86_64", "amd64": "elf-x86_64", "aarch64": "elf-aarch64", "arm64": "elf-aarch64"}.get(platform.machine().lower())


def exe_kind(path) -> str:
    """What a file is: elf-x86_64, elf-aarch64, elf-other, macho, windows, script or unknown."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return "unreadable"
    if head[:4] == b"\x7fELF" and len(head) >= 20:
        return {0x3E: "elf-x86_64", 0xB7: "elf-aarch64"}.get(struct.unpack_from("<H", head, 18)[0], "elf-other")
    if head[:4] in _MACHO:
        return "macho"
    if head[:2] == b"MZ":
        return "windows"
    if head[:2] == b"#!":
        return "script"
    return "unknown"


def exe_problem(path) -> str | None:
    """Why this file cannot run on this machine, or None if it can (scripts count as runnable)."""
    kind = exe_kind(path)
    if kind in ("script", _HOST_KIND):
        return None
    if kind == "macho":
        return "is a macOS program (published on a Mac?)"
    if kind == "windows":
        return "is a Windows program"
    if kind.startswith("elf"):
        return f"was built for another CPU ({kind[4:]}; this machine is {platform.machine()})"
    return "is not a Linux executable"
