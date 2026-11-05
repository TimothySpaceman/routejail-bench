"""Setup adapters. A setup builds the sandboxes plus whatever mediates their traffic, and tells the
runner what each client should dial and what to probe.

Build order: 0 (control) and 1 (kernel forwarding) now; proxy and router setups later, same interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .config import Config
from .runtime import Runtime, Sandbox
from .sh import run
from .topology import PORT_ALLOWED, PORT_FORBIDDEN, PORT_HOST, TARGET_IP, TARGET_NAME


@dataclass
class Probe:
    label: str
    target: str
    expect: str    # ok | blocked


class Setup(ABC):
    id: str
    slug: str
    enforces: bool      # does this setup block anything? (control does not)

    def __init__(self, cfg: Config, runtime: Runtime):
        self.cfg, self.rt = cfg, runtime
        self.sandboxes: list[Sandbox] = []

    def _create_sandboxes(self, n: int):
        # Every setup resolves the target by name, so name handling is exercised everywhere.
        hosts = {TARGET_NAME: TARGET_IP}
        self.sandboxes = [self.rt.create(i, net="routed", hosts=hosts, cpus=self.cfg.cores["sandbox"])
                          for i in range(1, n + 1)]

    @abstractmethod
    def up(self, n: int) -> list[Sandbox]: ...

    def target_for(self, sb: Sandbox) -> str:
        return f"{TARGET_NAME}:{PORT_ALLOWED}"

    def probes(self, sb: Sandbox) -> list[Probe]:
        ps = [Probe("allowed", self.target_for(sb), "ok")]
        if self.enforces:
            ps += [Probe("forbidden-port", f"{TARGET_NAME}:{PORT_FORBIDDEN}", "blocked"),
                   Probe("host-input", f"{sb.gw}:{PORT_HOST}", "blocked")]
        return ps

    def processes(self) -> list:
        return []      # processes whose CPU/RAM is recorded (none for setups 0 and 1)

    def down(self):
        for sb in self.sandboxes:
            self.rt.destroy(sb)
        self.sandboxes = []


class Control(Setup):
    """Setup 0: routed through the host, no policy. The zero line."""
    id, slug, enforces = "0", "setup0-control", False

    def up(self, n):
        self._create_sandboxes(n)
        return self.sandboxes


class Kernel(Setup):
    """Setup 1: host nftables. One allow rule per sandbox (its address -> target:port), then drop
    everything else coming from a sandbox interface, including traffic addressed to the host itself.

    Established flows are accepted first, as in any production ruleset, so the rule list is walked
    once per new connection rather than once per packet."""
    id, slug, enforces = "1", "setup1-kernel", True

    def ruleset(self) -> str:
        fwd = ["type filter hook forward priority -1; policy accept;", "ct state established,related accept"]
        for sb in self.sandboxes:
            fwd.append(f'iifname "{sb.host_if}" ip saddr {sb.ip} ip daddr {TARGET_IP} tcp dport {PORT_ALLOWED} accept')
        fwd.append('iifname "bsb*" drop')
        inp = ["type filter hook input priority -1; policy accept;", 'iifname "bsb*" drop']
        body = "\n".join(["table inet bench {", " chain forward {", *["  " + l for l in fwd], " }",
                          " chain input {", *["  " + l for l in inp], " }", "}"])
        return body + "\n"

    def up(self, n):
        self._create_sandboxes(n)
        run(["nft", "delete", "table", "inet", "bench"], check=False)
        run(["nft", "-f", "-"], input=self.ruleset())
        return self.sandboxes

    def down(self):
        run(["nft", "delete", "table", "inet", "bench"], check=False)
        super().down()


SETUPS = {c.id: c for c in (Control, Kernel)}


def make_setup(setup_id: str, cfg: Config, runtime: Runtime) -> Setup:
    if setup_id not in SETUPS:
        raise ValueError(f"setup {setup_id!r} is not implemented yet (available: {sorted(SETUPS)})")
    return SETUPS[setup_id](cfg, runtime)
