"""CPU and memory of the processes that make up a setup (Envoy, the in-sandbox relays, later the router).

Two ways to see a process:
  - host processes (Envoy): read from /proc by the orchestrator, once per second;
  - processes inside sandboxes (the relays): they append JSON lines about themselves to a file in a directory the
    host mounted into the sandbox; the orchestrator reads those files after the run. Nothing has to enter the sandbox.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024


@dataclass
class ProcSpec:
    name: str                                            # group name in the results, e.g. "envoy" or "relay"
    pids: list[int] = field(default_factory=list)        # host processes, sampled from /proc
    files: list[Path] = field(default_factory=list)      # self-reported stats files (one per instance)


def read_pid(pid: int):
    """(cpu seconds so far, resident kB) of a host process including all its threads, or None if it is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    rest = data[data.rindex(")") + 2:].split()      # fields after the command name (which may contain spaces)
    utime, stime, rss_pages = int(rest[11]), int(rest[12]), int(rest[21])
    return (utime + stime) / CLK_TCK, rss_pages * PAGE_KB


def read_stats_file(path: Path, t_from: float, t_to: float) -> list[dict]:
    """Samples from a self-reported stats file: any sequence of JSON objects, one per line or spread over several lines.
    A half-written last object (the process is still running) is skipped."""
    try:
        text = Path(path).read_text()
    except OSError:
        return []
    dec, pos, out = json.JSONDecoder(), 0, []
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        try:
            d, pos = dec.raw_decode(text, pos)
        except ValueError:                                   # junk or a partial write: resynchronise at the next line
            nl = text.find("\n", pos)
            pos = len(text) if nl < 0 else nl + 1
            continue
        if isinstance(d, dict) and t_from <= d.get("t", 0) <= t_to:
            sample = {"t": d["t"], "cpu_s": d.get("cpu_ns", 0) / 1e9, "rss_kb": d.get("rss_kb", 0),
                      "hwm_kb": d.get("hwm_kb", 0), "conns": d.get("conns", 0)}
            if d.get("pss_kb"):                              # proportional set size: shared pages split between processes
                sample["pss_kb"] = d["pss_kb"]
            out.append(sample)
    return out


class ProcSampler:
    def __init__(self, specs: list[ProcSpec], interval: float = 1.0):
        self.specs, self.interval = specs, interval
        self.samples: dict[tuple[str, int], list[dict]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0

    def _sample_once(self):
        t = round(time.time(), 3)
        for spec in self.specs:
            for pid in spec.pids:
                r = read_pid(pid)
                if r:
                    self.samples.setdefault((spec.name, pid), []).append({"t": t, "cpu_s": round(r[0], 3), "rss_kb": r[1]})

    def start(self):
        self._t0 = time.time()
        self.samples = {}
        self._stop.clear()
        self._sample_once()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(self.interval):
            self._sample_once()

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join()
        self._sample_once()
        t_to = time.time() + 2
        groups: dict[str, list[dict]] = {}
        for spec in self.specs:
            inst = groups.setdefault(spec.name, [])
            for pid in spec.pids:
                inst.append({"id": f"pid{pid}", "samples": self.samples.get((spec.name, pid), [])})
            for f in spec.files:
                inst.append({"id": Path(f).parent.name, "samples": read_stats_file(f, self._t0 - 2, t_to)})
        return {"interval_s": self.interval, "groups": {k: {"instances": v} for k, v in groups.items()}}
