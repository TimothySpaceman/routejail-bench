"""Fixed network plan shared by all setups.

    sandbox i  --  10.100.i.0/24 (gw .1 on the host, sandbox .2)  --+
                                                                     +-- HOST (forwards) --  10.200.0.0/24 -- target namespace (.2)
    sandbox j  --  10.100.j.0/24 ...                               --+

The target server lives in its own network namespace, so sandbox->target traffic is really
*forwarded* by the host kernel (not delivered locally), exactly as with an external target.
"""
from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .config import Config, parse_cpus
from .sh import log, run

TARGET_NS = "bench-tgt"
TARGET_HOST_IF, TARGET_NS_IF = "bt-h", "bt-t"
TARGET_GW, TARGET_IP = "10.200.0.1", "10.200.0.2"
TARGET_NAME = "target.bench"

PORT_ALLOWED = 9000     # the benchmark target port
PORT_FORBIDDEN = 9999   # a live server on the target that policy setups must block
PORT_HOST = 9998        # a live server on the host: sandbox->host must be blocked


# Interfaces whose forwarded traffic the host firewall must not drop. Docker (and ufw) set the iptables
# FORWARD policy to DROP, which silently kills traffic routed between our veths. We insert ACCEPT rules
# (in DOCKER-USER if it exists, else at the top of FORWARD) and remove them again on teardown.
# Setup 1's own policy is enforced earlier (nftables, priority -1), so these rules do not weaken it.
FW_MATCHES = [("-i", "bsb+"), ("-o", "bsb+"), ("-i", TARGET_HOST_IF), ("-o", TARGET_HOST_IF)]
FW_TAG = ["-m", "comment", "--comment", "bench"]
# Docker >= 28 installs, per container, `-d <container ip> ! -i <its bridge> -j DROP` in the *raw* PREROUTING chain.
# That chain runs before conntrack and before DOCKER-USER, so it also drops the target's replies (they arrive on
# the target veth, addressed to the sandbox). One ACCEPT for traffic coming from the target veth fixes that.
RAW_RULE = ["-t", "raw", "-i", TARGET_HOST_IF, "-d", "10.100.0.0/16"]


def sandbox_net(idx: int) -> tuple[str, str, str]:
    """(sandbox ip, gateway ip on host, cidr)"""
    return f"10.100.{idx}.2", f"10.100.{idx}.1", f"10.100.{idx}.0/24"


class Topology:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.procs: list[subprocess.Popen] = []
        self._old_fwd = "0"
        self._fw_chain: str | None = None

    def up(self):
        self.down()
        run(["ip", "netns", "add", TARGET_NS])
        run(["ip", "link", "add", TARGET_HOST_IF, "type", "veth", "peer", "name", TARGET_NS_IF])
        run(["ip", "link", "set", TARGET_NS_IF, "netns", TARGET_NS])
        run(["ip", "addr", "add", f"{TARGET_GW}/24", "dev", TARGET_HOST_IF])
        run(["ip", "link", "set", TARGET_HOST_IF, "up"])
        run(["ip", "-n", TARGET_NS, "addr", "add", f"{TARGET_IP}/24", "dev", TARGET_NS_IF])
        run(["ip", "-n", TARGET_NS, "link", "set", TARGET_NS_IF, "up"])
        run(["ip", "-n", TARGET_NS, "link", "set", "lo", "up"])
        run(["ip", "-n", TARGET_NS, "route", "add", "default", "via", TARGET_GW])  # replies to sandbox subnets
        fwd = Path("/proc/sys/net/ipv4/ip_forward")
        self._old_fwd = fwd.read_text().strip()
        fwd.write_text("1")
        self._allow_forwarding()

    def _find_fw_chain(self) -> str | None:
        if not shutil.which("iptables"):
            return None
        if run(["iptables", "-w", "5", "-n", "-L", "DOCKER-USER"], check=False).returncode == 0:
            return "DOCKER-USER"
        if "-P FORWARD DROP" in run(["iptables", "-w", "5", "-S", "FORWARD"], check=False).stdout:
            return "FORWARD"
        return None

    def _allow_forwarding(self):
        if not shutil.which("iptables"):
            return
        self._fw_chain = self._find_fw_chain()
        if self._fw_chain:
            log.info("host firewall drops forwarded traffic by default: allowing bench interfaces via %s", self._fw_chain)
            for m in FW_MATCHES:
                run(["iptables", "-w", "5", "-I", self._fw_chain, "1", *m, *FW_TAG, "-j", "ACCEPT"])

    def allow_replies(self):
        """Call after the sandboxes exist: Docker adds its raw-table drop rules when it creates a container,
        possibly at the top of the chain, so our ACCEPT must be (re)inserted afterwards, at position 1."""
        if not shutil.which("iptables"):
            return
        while run(["iptables", "-w", "5", "-D", "PREROUTING", *RAW_RULE, *FW_TAG, "-j", "ACCEPT"], check=False).returncode == 0:
            pass
        cp = run(["iptables", "-w", "5", "-I", "PREROUTING", "1", *RAW_RULE, *FW_TAG, "-j", "ACCEPT"], check=False)
        if cp.returncode == 0:
            log.info("allowed replies from the target to sandbox addresses in the raw table (Docker 28+ direct-routing filter)")

    def _revoke_forwarding(self):
        while run(["iptables", "-w", "5", "-D", "PREROUTING", *RAW_RULE, *FW_TAG, "-j", "ACCEPT"], check=False).returncode == 0:
            pass
        chains = {self._fw_chain} if self._fw_chain else {"DOCKER-USER", "FORWARD"}   # also cleans after a crash
        for chain in chains:
            for m in FW_MATCHES:
                while run(["iptables", "-w", "5", "-D", chain, *m, *FW_TAG, "-j", "ACCEPT"], check=False).returncode == 0:
                    pass
        self._fw_chain = None

    def diagnose(self) -> str:
        """Firewall and network state, for the log when probes fail."""
        cmds = [["iptables", "-w", "5", "-t", "raw", "-S", "PREROUTING"], ["iptables", "-w", "5", "-S", "FORWARD"],
                ["iptables", "-w", "5", "-S", "DOCKER-USER"], ["nft", "list", "tables"], ["ip", "-br", "addr"],
                ["ip", "route"], ["docker", "version", "--format", "server {{.Server.Version}}"]]
        out = []
        for c in cmds:
            if not shutil.which(c[0]):
                continue
            cp = run(c, check=False)
            text = (cp.stdout or cp.stderr).strip()
            if c[0] == "ip" and c[1] == "-br":
                text = "\n".join(l for l in text.splitlines() if "bsb" in l or "bt-" in l or "br-" in l)
            if c[0] == "ip" and c[1] == "route":
                text = "\n".join(l for l in text.splitlines() if "10.100" in l or "10.200" in l)
            out.append(f"$ {' '.join(c)}\n{text}")
        return "\n".join(out)

    def start_servers(self):
        nb = str(self.cfg.netbench)
        tc = self.cfg.cores["target"]
        self._spawn(["ip", "netns", "exec", TARGET_NS, "taskset", "-c", tc, nb, "server",
                     "--listen", f":{PORT_ALLOWED},:{PORT_FORBIDDEN}", "--procs", str(len(parse_cpus(tc)))], 2)
        self._spawn(["taskset", "-c", self.cfg.cores["os"], nb, "server",
                     "--listen", f":{PORT_HOST}", "--procs", "1"], 1)

    def _spawn(self, argv, expect_lines):
        log.debug("spawn %s", " ".join(argv))
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
        self.procs.append(p)
        seen, buf, deadline = 0, b"", time.time() + 8
        while seen < expect_lines:
            if p.poll() is not None:
                raise RuntimeError(f"server exited early: {buf.decode(errors='replace')}")
            if time.time() > deadline:
                raise RuntimeError("server did not report listening in time")
            r, _, _ = select.select([p.stderr], [], [], 0.5)
            if r:
                buf += os.read(p.stderr.fileno(), 4096)
                seen = buf.count(b"listening")

    def stop_servers(self):
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for p in self.procs:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
        self.procs.clear()

    def down(self):
        self.stop_servers()
        if shutil.which("iptables"):
            self._revoke_forwarding()
        run(["ip", "netns", "del", TARGET_NS], check=False)
        run(["ip", "link", "del", TARGET_HOST_IF], check=False)
        try:
            Path("/proc/sys/net/ipv4/ip_forward").write_text(self._old_fwd)
        except OSError:
            pass
