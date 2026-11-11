#!/usr/bin/env python3
"""STAND-IN for the real sidecar (testing only). Same externally visible behaviour: connects to the router's socket,
learns its routes, runs a DNS stub on 127.0.0.53:53, listens on a loopback address per routed name, and carries every
connection over a fresh connection to the router's socket. Wire protocol with fake_router.py is invented.
Environment: Sidecar__SocketPath, Sidecar__StatsPath (optional: one JSON line per second about this process)."""
import json
import os
import socket
import struct
import threading
import time

SOCK = os.environ.get("Sidecar__SocketPath", "/tmp/router.sock")
STATS = os.environ.get("Sidecar__StatsPath")
table, listeners, names = {}, {}, {}
active = [0]
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


def alloc(name):
    with lock:
        if name not in names:
            n = len(names)
            names[name] = f"127.1.{n // 250}.{n % 250 + 1}"
        return names[name]


def answer(q):
    if len(q) < 12:
        return b""
    pos, labels = 12, []
    while q[pos]:
        ln = q[pos]
        labels.append(q[pos + 1:pos + 1 + ln].decode())
        pos += ln + 1
    end = pos + 5
    qtype = struct.unpack(">H", q[pos + 1:pos + 3])[0]
    known = ".".join(labels).lower() in table
    rcode = 0 if known else 3
    head = q[:2] + struct.pack(">H", 0x8400 | (struct.unpack(">H", q[2:4])[0] & 0x0100) | rcode) + struct.pack(">HHHH", 1, 1 if (known and qtype == 1) else 0, 0, 0)
    resp = head + q[12:end]
    if known and qtype == 1:
        resp += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 1, 4) + socket.inet_aton(table[".".join(labels).lower()])
    return resp


def dns_stub():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.53", 53))
    while True:
        data, addr = s.recvfrom(512)
        r = answer(data)
        if r:
            s.sendto(r, addr)


def proxy(app, route_id):
    active[0] += 1
    try:
        up = socket.socket(socket.AF_UNIX)
        up.connect(SOCK)
        up.sendall(f"OPEN {route_id}\n".encode())
        threading.Thread(target=pipe, args=(up, app), daemon=True).start()
        pipe(app, up)
    except OSError:
        app.close()
    finally:
        active[0] -= 1


def listen(route):
    ip = alloc(route["name"].lower())
    srv = socket.socket(socket.AF_INET)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((ip, int(route["port"])))
    srv.listen(64)
    listeners[route["id"]] = srv
    table[route["name"].lower()] = ip

    def accept_loop():
        while True:
            c, _ = srv.accept()
            threading.Thread(target=proxy, args=(c, route["id"]), daemon=True).start()
    threading.Thread(target=accept_loop, daemon=True).start()


def accept_ptp(m):
    """The router asks this sidecar to connect to a server in this sandbox and hand the connection back."""
    try:
        local = socket.create_connection((m["host"], int(m["port"])), timeout=5)
        local.settimeout(None)
        up = socket.socket(socket.AF_UNIX)
        up.connect(SOCK)
        up.sendall(f"ACCEPT {m['reservation']}\n".encode())
        threading.Thread(target=pipe, args=(up, local), daemon=True).start()
        pipe(local, up)
    except OSError:
        pass


def stats_loop():
    while True:
        rss = hwm = 0
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
            elif line.startswith("VmHWM:"):
                hwm = int(line.split()[1])
        with open(STATS, "a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "cpu_ns": time.process_time_ns(), "rss_kb": rss, "hwm_kb": hwm, "conns": active[0]}) + "\n")
        time.sleep(1)


def session():
    c = socket.socket(socket.AF_UNIX)
    c.connect(SOCK)
    c.sendall(b"CONNECT\n")
    buf = b""
    while True:
        d = c.recv(65536)
        if not d:
            return
        buf += d
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            msg = json.loads(line)
            for r in msg.get("routes", []):
                if r["id"] not in listeners:
                    listen(r)
            if "accept" in msg:
                threading.Thread(target=accept_ptp, args=(msg["accept"],), daemon=True).start()


if __name__ == "__main__":
    threading.Thread(target=dns_stub, daemon=True).start()
    if STATS:
        threading.Thread(target=stats_loop, daemon=True).start()
    delay = 1
    while True:                                   # like the real sidecar: retry with a growing delay
        try:
            session()
            delay = 1
        except OSError:
            pass
        time.sleep(delay)
        delay = min(delay * 2, 30)
