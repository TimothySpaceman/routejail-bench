"""Setup adapters. A setup builds the sandboxes plus whatever mediates their traffic, and tells the
runner what each client should dial and what to probe.

The ladder: 0 control, 1 kernel rules, 2 Envoy over a network hop, 3 Envoy over a Unix socket, 4 the custom
router (not built yet). Every setup resolves `target.bench` and the client always dials `target.bench:9000`;
what that name leads to is the setup's business.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .envoy import EnvoyProxy, pipe_listener, tcp_listener
from .procs import ProcSpec
from .router_api import RouterApi, stable_id
from .runtime import Runtime, Sandbox
from .sh import log, run
from .topology import PORT_ALLOWED, PORT_FORBIDDEN, PORT_HOST, TARGET_IP, TARGET_NAME, sandbox_net

RUN_DIR = Path("/run/bench")      # per-sandbox socket directories (setup 3)


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
        self.workdir: Path = Path("/tmp/bench-work")     # set by the runner: where config and logs of the mediator go

    def _create_sandboxes(self, n: int, target_ip=None):
        # Every setup resolves the target by name, so name handling is exercised everywhere.
        # target_ip(i) tells which address `target.bench` has inside sandbox i (default: the real target).
        self.sandboxes = [self.rt.create(i, net="routed", hosts={TARGET_NAME: target_ip(i) if target_ip else TARGET_IP},
                                         cpus=self.cfg.cores["sandbox"]) for i in range(1, n + 1)]

    @abstractmethod
    def up(self, n: int) -> list[Sandbox]: ...

    def _wait_path_ready(self, hint: str, timeout: float = 40.0):
        """Return once every sandbox reaches the target through its whole mediation path (the first real probe)."""
        def ready(sb):
            deadline = time.time() + timeout
            while time.time() < deadline:
                # --expect ok matters: without it `probe` exits 0 even when the path is down and the wait would never wait
                argv = self.rt.exec_argv(sb, [self.rt.binary, "probe", "--target", self.target_for(sb), "--expect", "ok", "--timeout", "1s"], self.cfg.cores["sandbox"])
                if run(argv, check=False, timeout=30).returncode == 0:
                    return True
                time.sleep(0.3)
            return False
        with ThreadPoolExecutor(max_workers=min(len(self.sandboxes), 32)) as ex:
            ok = list(ex.map(ready, self.sandboxes))
        bad = [sb.name for sb, o in zip(self.sandboxes, ok) if not o]
        if bad:
            raise RuntimeError(f"the path to the target did not come up in {len(bad)} sandbox(es), e.g. {bad[0]}. {hint}")

    def target_for(self, sb: Sandbox) -> str:
        return f"{TARGET_NAME}:{PORT_ALLOWED}"

    def probes(self, sb: Sandbox) -> list[Probe]:
        ps = [Probe("allowed", self.target_for(sb), "ok")]
        if self.enforces:
            ps += [Probe("forbidden-port", f"{TARGET_NAME}:{PORT_FORBIDDEN}", "blocked"),
                   Probe("host-input", f"{sb.gw}:{PORT_HOST}", "blocked")]
        return ps

    def processes(self) -> list[ProcSpec]:
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


class ProxyNet(Setup):
    """Setup 2: the sandbox reaches the target only through Envoy, over a network hop.

    `target.bench` resolves, inside sandbox i, to the host-side address of its own network (10.100.i.1), where
    Envoy has one TCP-proxy listener (port 9000) for that sandbox; Envoy forwards to the real target. Everything
    else from the sandbox is dropped by nftables: nothing is forwarded, and the only local port it may reach is
    its own listener. The allow-list is one set lookup, so its cost does not grow with N."""
    id, slug, enforces = "2", "setup2-envoy-net", True

    def __init__(self, cfg, runtime):
        super().__init__(cfg, runtime)
        self.envoy = EnvoyProxy(cfg, self.workdir, "net")

    def ruleset(self) -> str:
        elements = ", ".join(f'"{sb.host_if}" . {sb.gw} . {PORT_ALLOWED}' for sb in self.sandboxes)
        return f"""table inet bench {{
  set listeners {{
    type ifname . ipv4_addr . inet_service
    elements = {{ {elements} }}
  }}
  chain forward {{
    type filter hook forward priority -1; policy accept;
    iifname "bsb*" drop
  }}
  chain input {{
    type filter hook input priority -1; policy accept;
    ct state established,related accept
    iifname . ip daddr . tcp dport @listeners accept
    iifname "bsb*" drop
  }}
}}
"""

    def up(self, n):
        self._create_sandboxes(n, target_ip=lambda i: sandbox_net(i)[1])
        run(["nft", "delete", "table", "inet", "bench"], check=False)
        run(["nft", "-f", "-"], input=self.ruleset())
        self.envoy = EnvoyProxy(self.cfg, self.workdir, "net")
        self.envoy.start([tcp_listener(f"sb{sb.idx}", sb.gw, PORT_ALLOWED) for sb in self.sandboxes])
        return self.sandboxes

    def probes(self, sb):
        ps = [Probe("allowed", self.target_for(sb), "ok"),
              Probe("direct-to-target", f"{TARGET_IP}:{PORT_ALLOWED}", "blocked"),
              Probe("host-input", f"{sb.gw}:{PORT_HOST}", "blocked")]
        others = [o for o in self.sandboxes if o.idx != sb.idx]
        if others:
            ps.append(Probe("other-sandbox-listener", f"{others[0].gw}:{PORT_ALLOWED}", "blocked"))
        return ps

    def processes(self):
        return [ProcSpec("envoy", pids=[self.envoy.pid])]

    def down(self):
        self.envoy.stop()
        run(["nft", "delete", "table", "inet", "bench"], check=False)
        super().down()


class ProxyUds(Setup):
    """Setup 3: the sandbox has no network at all. A small relay inside it (netbench relay) listens on
    127.0.0.1:9000 and forwards every connection to a Unix socket that the host mounted into this sandbox only;
    on the host side of that socket is Envoy, which forwards to the target. The socket a connection arrives on
    is the sandbox's identity."""
    id, slug, enforces = "3", "setup3-envoy-uds", True

    def __init__(self, cfg, runtime):
        super().__init__(cfg, runtime)
        self.envoy = EnvoyProxy(cfg, self.workdir, "uds")

    def _dir(self, idx: int) -> Path:
        return RUN_DIR / f"sb{idx}"

    def up(self, n):
        shutil.rmtree(RUN_DIR, ignore_errors=True)
        for i in range(1, n + 1):
            self._dir(i).mkdir(parents=True, mode=0o755)
        self.sandboxes = [self.rt.create(i, net="none", hosts={TARGET_NAME: "127.0.0.1"}, cpus=self.cfg.cores["sandbox"],
                                         mounts={str(self._dir(i)): "/sock"}) for i in range(1, n + 1)]
        self.envoy = EnvoyProxy(self.cfg, self.workdir, "uds")
        self.envoy.start([pipe_listener(f"sb{sb.idx}", str(self._dir(sb.idx) / "proxy.sock")) for sb in self.sandboxes])
        for sb in self.sandboxes:
            sock = sb.inside(str(self._dir(sb.idx) / "proxy.sock"))
            stats = sb.inside(str(self._dir(sb.idx) / "relay.stats"))
            self.rt.start_in(sb, [self.rt.binary, "relay", "--listen", f"127.0.0.1:{PORT_ALLOWED}", "--unix", sock, "--stats", stats],
                             self.cfg.cores["sandbox"])
        self._wait_relays()
        return self.sandboxes

    def _wait_relays(self):
        self._wait_path_ready("Under gVisor the runtime must allow host Unix sockets (runsc --host-uds=open, README.md).")

    def probes(self, sb):
        gw = sandbox_net(sb.idx)[1]
        return [Probe("allowed", self.target_for(sb), "ok"),
                Probe("direct-to-target", f"{TARGET_IP}:{PORT_ALLOWED}", "blocked"),
                Probe("host-input", f"{gw}:{PORT_HOST}", "blocked")]

    def processes(self):
        return [ProcSpec("envoy", pids=[self.envoy.pid]),
                ProcSpec("relay", files=[self._dir(sb.idx) / "relay.stats" for sb in self.sandboxes])]

    def down(self):
        self.envoy.stop()
        super().down()                      # destroying the sandboxes also ends the relays
        shutil.rmtree(RUN_DIR, ignore_errors=True)


class RouterSetup(Setup):
    """Setup 4: the custom router and sidecar.

    The router runs on the host (pinned to the mediator cores) and is configured over its REST API: one peer per sandbox
    (the router provisions that sandbox's socket) and one route per sandbox (target.bench:9000 -> the real target).
    The sandbox has no network. Inside it the sidecar connects to its socket, runs a DNS stub on 127.0.0.53 (the sandbox's
    resolv.conf points there) and a listener on the loopback address the stub hands out for `target.bench`; every
    connection to that listener is carried over a fresh connection to the router's socket.

    Order matters: the sidecar retries a missing socket with a growing delay (1 s ... 30 s), so the peer is created and its
    socket exists before the sidecar starts."""
    id, slug, enforces = "4", "setup4-router", True
    DNS = "127.0.0.53"

    def __init__(self, cfg, runtime):
        super().__init__(cfg, runtime)
        self.router_proc: subprocess.Popen | None = None
        self.api = RouterApi(cfg.router_url)

    def _dir(self, idx: int) -> Path:
        return RUN_DIR / f"sb{idx}"

    def _env(self) -> dict:
        env = dict(os.environ)
        env.update({"ASPNETCORE_URLS": self.cfg.router_url, "Logging__LogLevel__Default": "Warning",   # no per-connection logging while measuring
                    "DOTNET_TieredPGO": "0",
                   "DOTNET_ThreadPool_UnfairSemaphoreSpinLimit": "0",
                   "DOTNET_SYSTEM_NET_SOCKETS_INLINE_COMPLETIONS": "1"})
        return env

    def _start_router(self):
        self.workdir.mkdir(parents=True, exist_ok=True)
        logf = open(self.workdir / "router.log", "w")
        argv = ["taskset", "-c", self.cfg.cores["mediator"], *shlex.split(self.cfg.router)]
        self.router_proc = subprocess.Popen(argv, env=self._env(), stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            self.api.wait_ready(is_alive=lambda: self.router_proc.poll() is None)
        except RuntimeError as e:
            tail = (self.workdir / "router.log").read_text()[-600:]
            hint = ("\nThe router was published framework-dependent: publish it self-contained "
                    "(dotnet publish Router -c Release -r linux-x64 --self-contained) or install the .NET runtime.") if ".NET location" in tail or "install .NET" in tail else ""
            raise RuntimeError(f"{e}\nrouter log tail:\n{tail}{hint}") from None
        if self.router_proc.poll() is not None:
            raise RuntimeError(f"the router exited early (rc {self.router_proc.returncode}); see {self.workdir / 'router.log'}")

    def up(self, n):
        if not self.cfg.router or not self.cfg.sidecar:
            raise RuntimeError("setup 4 needs `router` and `sidecar` in the config (see README.md)")
        sidecar = Path(self.cfg.sidecar).resolve()
        shutil.rmtree(RUN_DIR, ignore_errors=True)
        for i in range(1, n + 1):
            self._dir(i).mkdir(parents=True, mode=0o755)
        self._start_router()
        self.sandboxes = [self.rt.create(i, net="none", cpus=self.cfg.cores["sandbox"], nameserver=self.DNS,
                                         mounts={str(self._dir(i)): "/sock", str(sidecar.parent): "/sidecar"}) for i in range(1, n + 1)]
        for sb in self.sandboxes:                              # peers first: this makes the router create each socket
            sock = self._dir(sb.idx) / "router.sock"
            peer = stable_id("peer", sb.name)
            self.api.create_peer(peer, f"sb{sb.idx}", str(sock))
            for _ in range(50):
                if sock.exists():
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(f"the router did not create {sock} after the peer was added")
            self.api.create_route(stable_id("route", sb.name), peer, TARGET_NAME, PORT_ALLOWED, TARGET_IP, PORT_ALLOWED)
        for sb in self.sandboxes:                              # then the sidecars
            env = [f"Sidecar__SocketPath={sb.inside(str(self._dir(sb.idx) / 'router.sock'))}",
                   f"Sidecar__StatsPath={sb.inside(str(self._dir(sb.idx) / 'sidecar.stats'))}",
                   "Logging__LogLevel__Default=Warning", 
                   "DOTNET_TieredPGO=0",
                   "DOTNET_BUNDLE_EXTRACT_BASE_DIR=/tmp",
                   "DOTNET_ThreadPool_UnfairSemaphoreSpinLimit=0",
                   "DOTNET_SYSTEM_NET_SOCKETS_INLINE_COMPLETIONS=1"]
            self.rt.start_in(sb, ["env", *env, sb.inside(str(sidecar))], self.cfg.cores["sandbox"])
        self._wait_path_ready("Check <group>/mediator/router.log, and that the sidecar binary is self-contained and runs in the sandbox image (see README.md).")
        return self.sandboxes

    def probes(self, sb):
        gw = sandbox_net(sb.idx)[1]
        return [Probe("allowed", self.target_for(sb), "ok"),
                Probe("unlisted-port", f"{TARGET_NAME}:{PORT_FORBIDDEN}", "blocked"),      # the sidecar listens only on routed ports
                Probe("unlisted-name", "other.bench:9000", "blocked"),                      # the DNS stub answers NXDOMAIN
                Probe("direct-to-target", f"{TARGET_IP}:{PORT_ALLOWED}", "blocked"),        # no network in the sandbox
                Probe("host-input", f"{gw}:{PORT_HOST}", "blocked")]

    def processes(self):
        specs = [ProcSpec("router", pids=[self.router_proc.pid] if self.router_proc else [])]
        specs.append(ProcSpec("sidecar", files=[self._dir(sb.idx) / "sidecar.stats" for sb in self.sandboxes]))
        return specs

    def down(self):
        if self.router_proc:
            try:
                os.killpg(self.router_proc.pid, signal.SIGTERM)
                self.router_proc.wait(timeout=8)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.router_proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.router_proc = None
        super().down()
        shutil.rmtree(RUN_DIR, ignore_errors=True)


def kill_leftovers(cfg=None):
    """Envoy processes and socket directories left behind by a crashed run."""
    run(["pkill", "-f", "envoy-(net|uds)\\.yaml"], check=False)
    if cfg is not None and cfg.router:                           # the router is a user-defined command: match it whole
        run(["pkill", "-f", re.escape(cfg.router)], check=False)
    shutil.rmtree(RUN_DIR, ignore_errors=True)


SETUPS = {c.id: c for c in (Control, Kernel, ProxyNet, ProxyUds, RouterSetup)}


def make_setup(setup_id: str, cfg: Config, runtime: Runtime) -> Setup:
    if setup_id not in SETUPS:
        raise ValueError(f"setup {setup_id!r} is not implemented yet (available: {sorted(SETUPS)})")
    return SETUPS[setup_id](cfg, runtime)
