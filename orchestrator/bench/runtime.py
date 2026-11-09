"""Sandbox runtimes: how a sandbox is created, entered and destroyed.

NetnsRuntime  - plain Linux network namespaces. For development and for testing the host plumbing
                (it behaves like runc as far as the network is concerned). Not a benchmark runtime.
DockerRuntime - Docker, with --runtime runsc for gVisor. The real one.

Both expose the same sandbox shape, so setups do not care which one is in use.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .sh import log, run
from .topology import sandbox_net


@dataclass
class Sandbox:
    idx: int
    name: str
    net: str            # "routed" | "none"
    ip: str = ""
    gw: str = ""
    host_if: str = ""   # host-side interface (veth or bridge) carrying this sandbox's traffic
    mounts: dict[str, str] = field(default_factory=dict)   # host path -> path inside the sandbox

    def inside(self, host_path: str) -> str:
        """Where a host path appears inside this sandbox (identity for runtimes without a mount namespace)."""
        for host, inner in self.mounts.items():
            if host_path == host or host_path.startswith(host.rstrip("/") + "/"):
                return inner.rstrip("/") + host_path[len(host.rstrip("/")):]
        return host_path


def remove_stale_resources():
    """Leftovers of an earlier run, whichever runtime created them: namespaces, host-side links (veths or Docker
    bridges named bsb<i>/bsc<i>), relay processes, and Docker containers and networks named bench-sb<i>."""
    run(["pkill", "-f", "relay --listen 127.0.0.1:9000 --unix"], check=False)
    for line in run(["ip", "netns", "list"], check=False).stdout.splitlines():
        ns = line.split()[0] if line.split() else ""
        if ns.startswith("bench-sb"):
            run(["ip", "netns", "del", ns], check=False)
            shutil.rmtree(Path("/etc/netns") / ns, ignore_errors=True)
    if shutil.which("docker"):
        ids = run(["docker", "ps", "-aq", "--filter", "name=bench-sb"], check=False, timeout=60).stdout.split()
        if ids:
            run(["docker", "rm", "-f", *ids], check=False, timeout=120)
        for n in run(["docker", "network", "ls", "-q", "--filter", "name=bench-sb"], check=False, timeout=60).stdout.split():
            run(["docker", "network", "rm", n], check=False)
    for line in run(["ip", "-br", "link"], check=False).stdout.splitlines():
        name = line.split()[0].split("@")[0] if line.split() else ""
        if re.fullmatch(r"(bsb|bsc)\d+", name):
            run(["ip", "link", "del", name], check=False)


class Runtime(ABC):
    name: str
    binary: str              # where netbench lives as seen from inside a sandbox
    launch_cost_s: float     # rough cost of starting one client in a sandbox (sizes the start lead time)

    @abstractmethod
    def create(self, idx: int, *, net: str = "routed", hosts: dict[str, str] | None = None, cpus: str = "",
               mounts: dict[str, str] | None = None, nameserver: str | None = None) -> Sandbox: ...

    @abstractmethod
    def start_in(self, sb: Sandbox, argv: list[str], cpus: str = "") -> None:
        """Start a long-running process inside the sandbox and return immediately (it dies with the sandbox)."""

    @abstractmethod
    def exec_argv(self, sb: Sandbox, argv: list[str], cpus: str = "") -> list[str]: ...

    @abstractmethod
    def destroy(self, sb: Sandbox): ...

    @abstractmethod
    def cleanup_all(self): ...

    def version_info(self) -> dict:
        return {}


class NetnsRuntime(Runtime):
    name = "netns"
    launch_cost_s = 0.05

    def __init__(self, cfg: Config):
        self.binary = str(cfg.netbench)
        self._bg: dict[str, list[subprocess.Popen]] = {}

    def create(self, idx, *, net="routed", hosts=None, cpus="", mounts=None, nameserver=None):
        ns = f"bench-sb{idx}"
        sb = Sandbox(idx, ns, net)           # no mount namespace here: a mounted host path is visible as it is
        run(["ip", "netns", "add", ns])
        run(["ip", "-n", ns, "link", "set", "lo", "up"])
        entries = ["127.0.0.1 localhost"]
        if net == "routed":
            sb.ip, sb.gw, cidr = sandbox_net(idx)
            sb.host_if, ns_if = f"bsb{idx}", f"bsc{idx}"
            run(["ip", "link", "add", sb.host_if, "type", "veth", "peer", "name", ns_if])
            run(["ip", "link", "set", ns_if, "netns", ns])
            run(["ip", "addr", "add", f"{sb.gw}/24", "dev", sb.host_if])
            run(["ip", "link", "set", sb.host_if, "up"])
            run(["ip", "-n", ns, "addr", "add", f"{sb.ip}/24", "dev", ns_if])
            run(["ip", "-n", ns, "link", "set", ns_if, "up"])
            run(["ip", "-n", ns, "route", "add", "default", "via", sb.gw])
        entries += [f"{ip} {name}" for name, ip in (hosts or {}).items()]
        d = Path("/etc/netns") / ns       # `ip netns exec` bind-mounts this over /etc/hosts
        d.mkdir(parents=True, exist_ok=True)
        (d / "hosts").write_text("\n".join(entries) + "\n")
        if nameserver:                          # `ip netns exec` bind-mounts this over /etc/resolv.conf
            (d / "resolv.conf").write_text(f"nameserver {nameserver}\n")
        return sb

    def exec_argv(self, sb, argv, cpus=""):
        pin = ["taskset", "-c", cpus] if cpus else []
        return ["ip", "netns", "exec", sb.name, *pin, *argv]

    def start_in(self, sb, argv, cpus=""):
        p = subprocess.Popen(self.exec_argv(sb, argv, cpus), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        self._bg.setdefault(sb.name, []).append(p)

    def destroy(self, sb):
        for p in self._bg.pop(sb.name, []):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
        run(["ip", "netns", "del", sb.name], check=False)
        shutil.rmtree(Path("/etc/netns") / sb.name, ignore_errors=True)

    def cleanup_all(self):
        remove_stale_resources()

class DockerRuntime(Runtime):
    """One Docker network (bridge) per sandbox, so every sandbox has its own subnet and host-side
    interface (bsb<idx>) and traffic between it and the target is routed by the host kernel.
    Masquerading is off: the target sees the sandbox's own address."""
    name = "docker"
    binary = "/netbench"
    launch_cost_s = 0.4

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.launch_cost_s = 1.0 if cfg.docker_runtime == "runsc" else 0.4   # gVisor exec is much slower

    def create(self, idx, *, net="routed", hosts=None, cpus="", mounts=None, nameserver=None):
        name = f"bench-sb{idx}"
        sb = Sandbox(idx, name, net, mounts=dict(mounts or {}))
        if nameserver:                          # mount a one-line resolv.conf over the container's own
            resolv = Path("/run/bench/resolv") / f"sb{idx}.conf"
            resolv.parent.mkdir(parents=True, exist_ok=True)
            resolv.write_text(f"nameserver {nameserver}\n")
            sb.mounts[str(resolv)] = "/etc/resolv.conf"
        cmd = ["docker", "run", "-d", "--name", name, "--stop-timeout", "0",
               "-v", f"{self.cfg.netbench}:/netbench:ro"]
        for host_path, inner in sb.mounts.items():
            cmd += ["-v", f"{host_path}:{inner}"]
        if self.cfg.docker_runtime:
            cmd += ["--runtime", self.cfg.docker_runtime]
        if cpus:
            cmd += ["--cpuset-cpus", cpus]
        for h, ip in (hosts or {}).items():
            cmd += ["--add-host", f"{h}:{ip}"]
        if net == "routed":
            sb.ip, sb.gw, cidr = sandbox_net(idx)
            sb.host_if = f"bsb{idx}"
            run(["docker", "network", "create", "--driver", "bridge", "--subnet", cidr, "--gateway", sb.gw,
                 "-o", f"com.docker.network.bridge.name={sb.host_if}",
                 "-o", "com.docker.network.bridge.enable_ip_masquerade=false", name])
            cmd += ["--network", name, "--ip", sb.ip]
        else:
            cmd += ["--network", "none"]
        run(cmd + [self.cfg.docker_image, "tail", "-f", "/dev/null"], timeout=120)
        return sb

    def exec_argv(self, sb, argv, cpus=""):
        return ["docker", "exec", sb.name, *argv]   # pinning is done by the container's cpuset

    def start_in(self, sb, argv, cpus=""):
        run(["docker", "exec", "-d", sb.name, *argv], timeout=60)

    def destroy(self, sb):
        run(["docker", "rm", "-f", sb.name], check=False, timeout=60)
        if sb.net == "routed":
            run(["docker", "network", "rm", sb.name], check=False)

    def cleanup_all(self):
        remove_stale_resources()

    def version_info(self):
        info = {"docker": run(["docker", "version", "--format", "{{.Server.Version}}"], check=False).stdout.strip()}
        if self.cfg.docker_runtime:
            info[self.cfg.docker_runtime] = run([self.cfg.docker_runtime, "--version"], check=False).stdout.strip()
        return info


def make_runtime(cfg: Config) -> Runtime:
    rt: Runtime | None = None
    if cfg.runtime == "netns":
        rt = NetnsRuntime(cfg)
    elif cfg.runtime == "docker":
        rt = DockerRuntime(cfg)
    if rt is not None:
        if cfg.launch_cost_s is not None:
            rt.launch_cost_s = cfg.launch_cost_s
        return rt
    raise ValueError(f'unknown runtime {cfg.runtime!r}: use "netns" or "docker" (gVisor = docker + docker_runtime = "runsc")')
