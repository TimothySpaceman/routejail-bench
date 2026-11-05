"""Sandbox runtimes: how a sandbox is created, entered and destroyed.

NetnsRuntime  - plain Linux network namespaces. For development and for testing the host plumbing
                (it behaves like runc as far as the network is concerned). Not a benchmark runtime.
DockerRuntime - Docker, with --runtime runsc for gVisor. The real one.

Both expose the same sandbox shape, so setups do not care which one is in use.
"""
from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
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


class Runtime(ABC):
    name: str
    binary: str              # where netbench lives as seen from inside a sandbox
    launch_cost_s: float     # rough cost of starting one client in a sandbox (sizes the start lead time)

    @abstractmethod
    def create(self, idx: int, *, net: str = "routed", hosts: dict[str, str] | None = None, cpus: str = "") -> Sandbox: ...

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

    def create(self, idx, *, net="routed", hosts=None, cpus=""):
        ns = f"bench-sb{idx}"
        sb = Sandbox(idx, ns, net)
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
        return sb

    def exec_argv(self, sb, argv, cpus=""):
        pin = ["taskset", "-c", cpus] if cpus else []
        return ["ip", "netns", "exec", sb.name, *pin, *argv]

    def destroy(self, sb):
        run(["ip", "netns", "del", sb.name], check=False)
        shutil.rmtree(Path("/etc/netns") / sb.name, ignore_errors=True)

    def cleanup_all(self):
        out = run(["ip", "netns", "list"], check=False).stdout
        for line in out.splitlines():
            ns = line.split()[0] if line.split() else ""
            if ns.startswith("bench-sb"):
                run(["ip", "netns", "del", ns], check=False)
                shutil.rmtree(Path("/etc/netns") / ns, ignore_errors=True)


class DockerRuntime(Runtime):
    """One Docker network (bridge) per sandbox, so every sandbox has its own subnet and host-side
    interface (bsb<idx>) and traffic between it and the target is routed by the host kernel.
    Masquerading is off: the target sees the sandbox's own address."""
    name = "docker"
    binary = "/netbench"
    launch_cost_s = 0.4

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def create(self, idx, *, net="routed", hosts=None, cpus=""):
        name = f"bench-sb{idx}"
        sb = Sandbox(idx, name, net)
        cmd = ["docker", "run", "-d", "--name", name, "--stop-timeout", "0",
               "-v", f"{self.cfg.netbench}:/netbench:ro"]
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

    def destroy(self, sb):
        run(["docker", "rm", "-f", sb.name], check=False, timeout=60)
        if sb.net == "routed":
            run(["docker", "network", "rm", sb.name], check=False)

    def cleanup_all(self):
        ids = run(["docker", "ps", "-aq", "--filter", "name=bench-sb"], check=False).stdout.split()
        if ids:
            run(["docker", "rm", "-f", *ids], check=False, timeout=120)
        nets = run(["docker", "network", "ls", "-q", "--filter", "name=bench-sb"], check=False).stdout.split()
        for n in nets:
            run(["docker", "network", "rm", n], check=False)

    def version_info(self):
        info = {"docker": run(["docker", "version", "--format", "{{.Server.Version}}"], check=False).stdout.strip()}
        if self.cfg.docker_runtime:
            info[self.cfg.docker_runtime] = run([self.cfg.docker_runtime, "--version"], check=False).stdout.strip()
        return info


def make_runtime(cfg: Config) -> Runtime:
    if cfg.runtime == "netns":
        return NetnsRuntime(cfg)
    if cfg.runtime == "docker":
        return DockerRuntime(cfg)
    raise ValueError(f"unknown runtime {cfg.runtime!r}")
