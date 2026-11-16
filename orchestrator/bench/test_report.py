import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _manifest(path, started, tuned):
    host = {"smt_active": "0" if tuned else "1", "governors": ["performance" if tuned else "powersave"], "freq": {"cpu1": {"min_khz": 3800000, "max_khz": 3800000 if tuned else 5100000}},
            "cpuidle": {"cpu1": "POLL=on(0us) C1=on(1us) C2=" + ("off" if tuned else "on") + "(18us)"}}
    path.write_text(json.dumps({"started_unix": started, "host": host, "config": {}, "tools": {}}))


def _run(exp, setup, bench, n, launched):
    d = exp / setup / "outbound" / bench / f"n{n}" / "run1"
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"launched_unix": launched, "accepted": True, "rc": {}}))


def test_report_flags_the_untuned_session_and_lists_its_cells(tmp_path):
    exp = tmp_path / "exp"
    exp.mkdir()
    _manifest(exp / "manifest.json", 1000, tuned=True)
    _manifest(exp / "manifest.session-2.json", 2000, tuned=False)
    _run(exp, "setup1-kernel", "ttfb", 5, 1500)          # measured in the tuned session
    _run(exp, "setup2-envoy-net", "latency", 20, 2500)   # measured in the untuned one
    out = subprocess.run([sys.executable, str(ROOT / "report.py"), str(exp)], capture_output=True, text=True, timeout=60).stdout
    assert "tuned  (1 runs)" in out.replace("  ", " ") or "tuned" in out
    assert "UNTUNED: SMT on, governor not performance, clock not pinned, deep idle states on" in out
    assert "setup2-envoy-net outbound latency n=20: 1 run(s)" in out
    assert "setup1-kernel outbound ttfb n=5" not in out                      # only the untuned session's cells are listed
