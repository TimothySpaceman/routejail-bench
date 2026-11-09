"""Envoy as the mediator of setups 2 and 3: configuration generation and process management.

The proxy is as plain as it gets: one TCP-proxy listener per sandbox, one shared cluster pointing at the
target. No HTTP parsing, no xDS, no fault injection.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

from .config import Config, parse_cpus
from .sh import log, run
from .topology import PORT_ALLOWED, TARGET_IP

TCP_PROXY = "type.googleapis.com/envoy.extensions.filters.network.tcp_proxy.v3.TcpProxy"


def tcp_listener(name: str, address: str, port: int) -> dict:
    return {"name": name, "address": {"socket_address": {"address": address, "port_value": port}}}


def pipe_listener(name: str, path: str) -> dict:
    return {"name": name, "address": {"pipe": {"path": path, "mode": 0o666}}}


def envoy_config(listeners: list[dict], admin_port: int, target_ip: str = TARGET_IP, target_port: int = PORT_ALLOWED) -> dict:
    """The whole configuration as plain data (JSON is valid YAML, so Envoy reads it as it is)."""
    return {
        "admin": {"address": {"socket_address": {"address": "127.0.0.1", "port_value": admin_port}}},
        # silence the "no configured limit to downstream connections" warning without limiting anything
        "layered_runtime": {"layers": [{"name": "static", "static_layer": {"overload": {"global_downstream_max_connections": 1000000}}}]},
        "static_resources": {
            "listeners": [dict(l, filter_chains=[{"filters": [{"name": "envoy.filters.network.tcp_proxy",
                                                               "typed_config": {"@type": TCP_PROXY, "stat_prefix": l["name"], "cluster": "target"}}]}])
                          for l in listeners],
            "clusters": [{"name": "target", "type": "STATIC", "connect_timeout": "5s",
                          "load_assignment": {"cluster_name": "target", "endpoints": [{"lb_endpoints": [
                              {"endpoint": {"address": {"socket_address": {"address": target_ip, "port_value": target_port}}}}]}]}}],
        },
    }


class EnvoyProxy:
    def __init__(self, cfg: Config, workdir: Path, tag: str):
        self.cfg, self.workdir, self.tag = cfg, Path(workdir), tag
        self.proc: subprocess.Popen | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc else 0

    def start(self, listeners: list[dict]):
        self.workdir.mkdir(parents=True, exist_ok=True)
        conf = self.workdir / f"envoy-{self.tag}.yaml"
        conf.write_text(json.dumps(envoy_config(listeners, self.cfg.envoy_admin_port), indent=1) + "\n")
        v = run([self.cfg.envoy, "--mode", "validate", "-c", str(conf)], check=False, timeout=90)
        if v.returncode != 0:
            raise RuntimeError(f"Envoy rejected its configuration ({conf}):\n{(v.stderr or v.stdout).strip()[-800:]}")
        cpus = parse_cpus(self.cfg.cores["mediator"])
        argv = ["taskset", "-c", self.cfg.cores["mediator"], self.cfg.envoy, "-c", str(conf), "--concurrency", str(len(cpus)),
                "--disable-hot-restart", "--log-level", "warning"]
        logf = open(self.workdir / f"envoy-{self.tag}.log", "w")
        self.proc = subprocess.Popen(argv, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        self._wait_ready(logf.name)

    def _wait_ready(self, logpath: str, timeout: float = 30.0):
        url = f"http://127.0.0.1:{self.cfg.envoy_admin_port}/ready"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"Envoy exited early (rc {self.proc.returncode}):\n{Path(logpath).read_text()[-800:]}")
            try:
                with urllib.request.urlopen(url, timeout=1) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.25)
        raise RuntimeError(f"Envoy did not become ready within {timeout:.0f}s:\n{Path(logpath).read_text()[-800:]}")

    def stop(self):
        if not self.proc:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc = None
