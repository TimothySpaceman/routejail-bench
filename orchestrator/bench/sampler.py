"""Per-core CPU utilization, sampled from /proc/stat once per interval.

Recorded as a time series (unix timestamps) so that any window can be cut out later.
It is the only way to tell afterwards whether a core group (clients, mediator, target) saturated.
"""
from __future__ import annotations

import threading
import time


def _read() -> dict[int, tuple[int, int]]:
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu") and line[3].isdigit():
                p = line.split()
                vals = [int(x) for x in p[1:]]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
                total = sum(vals[:8])                                # excludes guest (already in user/nice)
                out[int(p[0][3:])] = (total - idle, total)
    return out


class CoreSampler:
    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self.samples = []
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        prev = _read()
        while not self._stop.wait(self.interval):
            cur = _read()
            busy = {}
            for cpu, (b, t) in cur.items():
                pb, pt = prev.get(cpu, (b, t))
                busy[cpu] = round((b - pb) / (t - pt), 4) if t > pt else 0.0
            self.samples.append({"t": round(time.time(), 3), "busy": busy})
            prev = cur

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join()
        return {"interval_s": self.interval, "samples": self.samples}
