"""Shared fixtures: a config builder, a fake runtime (no sandboxes, no root) and a fake setup, so the orchestrator's
own logic (retries, resume, late starts, result files) can be tested anywhere."""
from __future__ import annotations

import json
import os
import sys
import types
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import config, runner as runner_mod, setups as setups_mod          # noqa: E402
from bench.procs import ProcSpec                                              # noqa: E402
from bench.runtime import Runtime, Sandbox                                    # noqa: E402

STUB = Path(__file__).with_name("fake_client.py")
OK_RESULT = {"schema": 2, "mode": "ttfb", "valid": True, "counts": {"ok": 10}, "params": {"warmup_s": 0.1, "duration_s": 0.2}}


def ok_behavior(sb, argv):
    if argv[1] == "probe":
        return 0, {"outcome": "ok"}, ""
    return 0, dict(OK_RESULT), "summary line"


class FakeRuntime(Runtime):
    name, binary, launch_cost_s = "fake", "/fake/netbench", 0.0

    def __init__(self, behavior=ok_behavior):
        self.behavior, self.calls, self.created = behavior, [], []

    def create(self, idx, *, net="routed", hosts=None, cpus="", mounts=None, nameserver=None):
        sb = Sandbox(idx, f"bench-sb{idx}", net, ip=f"10.100.{idx}.2", gw=f"10.100.{idx}.1", host_if=f"bsb{idx}")
        self.created.append(sb)
        return sb

    def exec_argv(self, sb, argv, cpus=""):
        rc, out, err = self.behavior(sb, argv)
        self.calls.append((sb.name, argv[1]))
        return [sys.executable, str(STUB), json.dumps([rc, out, err])]

    def start_in(self, sb, argv, cpus=""):
        pass

    def destroy(self, sb):
        pass

    def cleanup_all(self):
        pass


class FakeTopology:
    def __init__(self):
        self.events = []

    def up(self): self.events.append("up")
    def start_servers(self): pass
    def allow_replies(self): pass
    def down(self): self.events.append("down")
    def diagnose(self): return "fake diagnostics"


class FakeSetup(setups_mod.Setup):
    id, slug, enforces = "9", "setup9-fake", False
    current_n = [0]

    def up(self, n):
        FakeSetup.current_n[0] = n
        self._create_sandboxes(n)
        return self.sandboxes

    def processes(self):
        return [ProcSpec("fake", pids=[os.getpid()])]


@pytest.fixture
def make_cfg(tmp_path):
    def _make(ns=(2,), runs=1, retries=1, setups=("9",), rate="20", experiment="t", scenarios=("outbound",)):
        (tmp_path / "netbench").write_text("#!/bin/sh\nexit 0\n")
        os.chmod(tmp_path / "netbench", 0o755)
        (tmp_path / "cfg.toml").write_text(f'''experiment = "{experiment}"
results_dir = "{tmp_path}/results"
netbench = "{tmp_path}/netbench"
runtime = "netns"
setups = {json.dumps(list(setups))}
ns = {list(ns)}
runs = {runs}
max_retries = {retries}
lead_base_s = 0.0
launch_cost_s = 0.0
seed = 1
scenarios = {json.dumps(list(scenarios))}

[cores]
os = "0"
sandbox = "0"
mediator = "0"
target = "0"

[[benchmark]]
name = "ttfb"
cmd = "ttfb"
args = ["--rate", "{rate}"]
warmup = 0.1
duration = 0.2
''')
        return config.load(tmp_path / "cfg.toml")
    return _make


@pytest.fixture
def harness(monkeypatch):
    """Patches what would touch the host or slow the tests down, and registers the fake setup."""
    monkeypatch.setitem(setups_mod.SETUPS, "9", FakeSetup)
    monkeypatch.setattr(runner_mod, "time", types.SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(runner_mod.os, "sched_setaffinity", lambda *a, **k: None, raising=False)   # missing on macOS
    monkeypatch.setattr(runner_mod, "kill_leftovers", lambda *a, **k: None)
    FakeSetup.current_n[0] = 0

    def build(cfg, behavior=ok_behavior):
        rt, topo = FakeRuntime(behavior), FakeTopology()
        return runner_mod.Runner(cfg, runtime=rt, topo=topo), rt, topo
    return build
