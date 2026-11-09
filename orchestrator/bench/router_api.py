"""Client for the router's REST control API (setup 4).

Contract (from the router's OpenAPI document):
  POST /peers   {"id", "name", "socketPath", "enabled"}   the router provisions the socket at socketPath and listens on it
  POST /routes  {"id", "sourcePeerId", "name", "port", "host", "targetPort", "destinationPeerId", "protocol"}
                a sandbox may reach `name` (served on `port` inside the sandbox) and the router connects to host:targetPort
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid

NAMESPACE = uuid.UUID("5c1f2f7e-9d3b-4a38-8f0e-3b6f7a3c9a10")


def stable_id(*parts: str) -> str:
    """Deterministic ids, so a rerun after a crash addresses the same peers and routes."""
    return str(uuid.uuid5(NAMESPACE, "/".join(parts)))


def peer_payload(peer_id: str, name: str, socket_path: str) -> dict:
    return {"id": peer_id, "name": name, "socketPath": socket_path, "enabled": True}


def route_payload(route_id: str, peer_id: str, name: str, port: int, host: str, target_port: int) -> dict:
    return {"id": route_id, "sourcePeerId": peer_id, "name": name, "port": port, "host": host,
            "targetPort": target_port, "destinationPeerId": None, "protocol": "tcp"}


class RouterApi:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base, self.timeout = base_url.rstrip("/"), timeout

    def _call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = r.read().decode()
                return json.loads(text) if text.strip() else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"router API {method} {path} -> {e.code}: {e.read().decode()[:300]}") from None

    def wait_ready(self, timeout: float = 30.0, is_alive=None):
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            if is_alive is not None and not is_alive():
                raise RuntimeError("the router process exited before its API came up")
            try:
                self._call("GET", "/peers")
                return
            except (OSError, RuntimeError) as e:
                last = str(e)
                time.sleep(0.3)
        raise RuntimeError(f"router API at {self.base} did not answer within {timeout:.0f}s ({last})")

    def create_peer(self, peer_id, name, socket_path):
        return self._call("POST", "/peers", peer_payload(peer_id, name, socket_path))

    def create_route(self, route_id, peer_id, name, port, host, target_port):
        return self._call("POST", "/routes", route_payload(route_id, peer_id, name, port, host, target_port))

    def peers(self):
        return self._call("GET", "/peers")

    def routes(self):
        return self._call("GET", "/routes")
