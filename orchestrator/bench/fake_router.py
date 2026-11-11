#!/usr/bin/env python3
"""STAND-IN for the real router, for testing the orchestrator without the real binaries. NOT a measurement tool.

It has the same REST surface that the orchestrator uses (POST/GET /peers, /routes) and the same externally visible
behaviour: adding a peer provisions a Unix socket at its socketPath, and traffic is proxied over it to host:targetPort.
The protocol between this router and fake_sidecar.py is invented (text lines), not the real frame format.
Environment: ASPNETCORE_URLS (default http://127.0.0.1:5010)."""
import json
import os
import socket
import sys
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

peers, routes, controls, reservations = {}, {}, {}, {}
lock = threading.Lock()


def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    try:
        b.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def read_line(c):
    buf = b""
    while not buf.endswith(b"\n"):
        ch = c.recv(1)
        if not ch:
            break
        buf += ch
    return buf.decode().strip()


def push(peer_id):
    with lock:
        snap = json.dumps({"routes": [r for r in routes.values() if r["sourcePeerId"] == peer_id]}) + "\n"
        socks = list(controls.get(peer_id, []))
    for c in socks:
        try:
            c.sendall(snap.encode())
        except OSError:
            pass


def handle_uds(peer_id, c):
    line = read_line(c)
    if line == "CONNECT":
        with lock:
            controls.setdefault(peer_id, []).append(c)
        push(peer_id)
        try:
            while c.recv(4096):
                pass
        except OSError:
            pass
        return
    if line.startswith("ACCEPT "):                 # the destination sidecar answers a peer-to-peer request
        with lock:
            h = reservations.get(line.split()[1])
        if h:
            h["conn"] = c
            h["ev"].set()
        return
    if line.startswith("OPEN "):
        with lock:
            r = routes.get(line.split()[1])
        if not r or r["sourcePeerId"] != peer_id:
            c.close()
            return
        if r.get("destinationPeerId"):              # peer-to-peer: ask the DESTINATION sidecar to connect to host:targetPort in its sandbox
            res, h = str(uuid.uuid4()), {"conn": None, "ev": threading.Event()}
            with lock:
                reservations[res] = h
                dest = list(controls.get(r["destinationPeerId"], []))
            if not dest:
                c.close()
                return
            dest[0].sendall((json.dumps({"accept": {"reservation": res, "host": r["host"], "port": int(r["targetPort"])}}) + "\n").encode())
            if not h["ev"].wait(5):
                c.close()
                return
            threading.Thread(target=pipe, args=(h["conn"], c), daemon=True).start()
            pipe(c, h["conn"])
            return
        try:
            up = socket.create_connection((r["host"], int(r["targetPort"])), timeout=5)
        except OSError:
            c.close()
            return
        up.settimeout(None)
        threading.Thread(target=pipe, args=(up, c), daemon=True).start()
        pipe(c, up)
        return
    c.close()


def serve_peer(peer_id, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(64)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle_uds, args=(peer_id, c), daemon=True).start()


class Api(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/peers":
            return self._send(200, list(peers.values()))
        if u.path == "/routes":
            return self._send(200, list(routes.values()))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        b = self._body()
        if self.path == "/peers":
            for k in ("id", "name", "socketPath"):
                if not b.get(k):
                    return self._send(400, {"error": f"{k} is required"})
            with lock:
                peers[b["id"]] = b
            threading.Thread(target=serve_peer, args=(b["id"], b["socketPath"]), daemon=True).start()
            return self._send(200, b)
        if self.path == "/routes":
            for k in ("id", "sourcePeerId", "name", "port", "host", "targetPort"):
                if b.get(k) in (None, ""):
                    return self._send(400, {"error": f"{k} is required"})
            if b["sourcePeerId"] not in peers:
                return self._send(400, {"error": "unknown peer"})
            with lock:
                routes[b["id"]] = b
            push(b["sourcePeerId"])
            return self._send(200, b)
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    url = urllib.parse.urlparse(os.environ.get("ASPNETCORE_URLS", "http://127.0.0.1:5010").split(";")[0])
    ThreadingHTTPServer((url.hostname, url.port), Api).serve_forever()
