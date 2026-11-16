import json

import pytest

from conftest import OK_RESULT, ok_behavior


def cell(cfg, n, bench="ttfb", slug="setup9-fake"):
    return cfg.results_dir / cfg.experiment / slug / "outbound" / bench / f"n{n}"


def test_clean_run_writes_everything(make_cfg, harness):
    cfg = make_cfg(ns=(2,), runs=2)
    r, rt, topo = harness(cfg)
    r.run_all()
    for run in ("run1", "run2"):
        d = cell(cfg, 2) / run
        assert {p.name for p in d.iterdir()} >= {"bench-sb1.json", "bench-sb2.json", "cores.json", "run.json", "stderr.log", "procs.json"}
        meta = json.loads((d / "run.json").read_text())
        assert meta["accepted"] is True and meta["attempt"] == 1 and meta["late_start"] is False
    assert (cell(cfg, 2) / "done.json").exists()
    assert (cfg.results_dir / "t" / "setup9-fake" / "outbound" / "n2" / "probes.json").exists()
    assert topo.events == ["up", "down"]                                   # network built once and torn down
    assert not list(cell(cfg, 2).glob("*.rejected*")) and not list(cell(cfg, 2).glob("*.tmp"))


def test_invalid_attempt_is_kept_and_retried(make_cfg, harness):
    cfg = make_cfg(retries=2)
    calls = {"n": 0}

    def behavior(sb, argv):
        if argv[1] == "probe":
            return 0, {"outcome": "ok"}, ""
        calls["n"] += 1
        bad = calls["n"] <= 2                                              # both sandboxes of the first attempt are invalid
        return 0, dict(OK_RESULT, valid=not bad), ""
    r, rt, _ = harness(cfg, behavior)
    r.run_all()
    c = cell(cfg, 2)
    assert (c / "run1.rejected1").is_dir()
    meta = json.loads((c / "run1" / "run.json").read_text())
    assert meta["attempt"] == 2 and meta["accepted"] is True


def test_all_attempts_invalid_marks_run_not_accepted(make_cfg, harness):
    cfg = make_cfg(retries=1)
    r, rt, _ = harness(cfg, lambda sb, argv: (0, {"outcome": "ok"}, "") if argv[1] == "probe" else (0, dict(OK_RESULT, valid=False), ""))
    r.run_all()
    c = cell(cfg, 2)
    assert (c / "run1.rejected1").is_dir()
    meta = json.loads((c / "run1" / "run.json").read_text())
    assert meta["attempt"] == 2 and meta["accepted"] is False
    assert (c / "done.json").exists()                                      # the cell is finished, just not clean


def test_late_start_extends_lead_without_using_an_attempt(make_cfg, harness):
    cfg = make_cfg(retries=0)                                               # no retries allowed at all
    state = {"late": True}

    def behavior(sb, argv):
        if argv[1] == "probe":
            return 0, {"outcome": "ok"}, ""
        if state["late"]:
            if sb.idx == 2:
                state["late"] = False                                      # the next attempt will be fine
            return 2, None, "netbench: --start-at is 4.2s in the past"
        return 0, dict(OK_RESULT), ""
    r, rt, _ = harness(cfg, behavior)
    r.run_all()
    meta = json.loads((cell(cfg, 2) / "run1" / "run.json").read_text())
    assert meta["accepted"] is True and meta["attempt"] == 1               # no attempt was consumed
    assert r.lead_extra >= 3.0                                             # but the lead grew
    assert not list(cell(cfg, 2).glob("*.rejected*"))


def test_resume_skips_finished_groups(make_cfg, harness):
    cfg = make_cfg()
    r, rt, _ = harness(cfg)
    r.run_all()
    before = len(rt.calls)
    r2, rt2, _ = harness(cfg)
    r2.run_all()
    assert rt2.calls == [] and before > 0


def test_changed_benchmark_definition_is_refused_unless_forced(make_cfg, harness, tmp_path):
    cfg = make_cfg(rate="20")
    harness(cfg)[0].run_all()
    changed = make_cfg(rate="40")                                          # same experiment name, different rate
    with pytest.raises(SystemExit) as e:
        harness(changed)[0].run_all()
    assert "config.benchmark" in str(e.value)
    harness(changed)[0].run_all(force=True)                                # allowed knowingly
    assert list((cfg.results_dir / "t").glob("manifest.session-*.json"))


def test_extending_the_grid_is_allowed(make_cfg, harness):
    harness(make_cfg(ns=(2,)))[0].run_all()
    bigger = make_cfg(ns=(2, 3))
    r, rt, _ = harness(bigger)
    r.run_all()                                                            # no refusal; only n=3 is new
    assert (cell(bigger, 3) / "done.json").exists()
    assert {name for name, cmd in rt.calls} <= {"bench-sb1", "bench-sb2", "bench-sb3"}


def test_probe_failure_aborts_only_that_group(make_cfg, harness):
    cfg = make_cfg(ns=(2, 3))
    from conftest import FakeSetup

    def behavior(sb, argv):
        if argv[1] == "probe":
            return (1, {"outcome": "timeout"}, "") if FakeSetup.current_n[0] == 2 else (0, {"outcome": "ok"}, "")
        return 0, dict(OK_RESULT), ""
    r, rt, topo = harness(cfg, behavior)
    r.run_all()
    failed = cfg.results_dir / "t" / "setup9-fake" / "outbound" / "n2" / "failed.json"
    assert failed.exists() and "ProbeFailure" in json.loads(failed.read_text())["error"]
    assert not (cell(cfg, 2) / "done.json").exists()
    assert (cell(cfg, 3) / "done.json").exists()                           # the other group still ran
    assert topo.events.count("down") == 2                                  # both groups were torn down


def test_client_without_json_leaves_stdout_and_marks_invalid(make_cfg, harness):
    cfg = make_cfg(retries=0)
    r, rt, _ = harness(cfg, lambda sb, argv: (0, {"outcome": "ok"}, "") if argv[1] == "probe" else (1, None, "crashed"))
    r.run_all()
    meta = json.loads((cell(cfg, 2) / "run1" / "run.json").read_text())
    assert meta["accepted"] is False and meta["rc"] == {"bench-sb1": 1, "bench-sb2": 1}
    assert "crashed" in (cell(cfg, 2) / "run1" / "stderr.log").read_text()


def test_peer_scenario_runs_next_to_outbound_and_needs_two_sandboxes(make_cfg, harness):
    cfg = make_cfg(ns=(1, 3), scenarios=("outbound", "peer"))
    r, rt, topo = harness(cfg)
    r.run_all()
    for n, scenarios in ((1, {"outbound"}), (3, {"outbound", "peer"})):
        for sc in ("outbound", "peer"):
            done = cfg.results_dir / "t" / "setup9-fake" / sc / "ttfb" / f"n{n}" / "done.json"
            assert done.exists() == (sc in scenarios), (n, sc)
    base = cfg.results_dir / "t" / "setup9-fake"
    assert (base / "peer" / "n3" / "probes.json").exists() and not (base / "peer" / "n1").exists()
    meta = json.loads((base / "peer" / "ttfb" / "n3" / "run1" / "run.json").read_text())
    assert meta["accepted"] is True
    assert topo.events.count("up") == 2                                    # one network per group, shared by both scenarios


def test_peer_scenario_can_be_added_later_to_a_finished_outbound_experiment(make_cfg, harness):
    harness(make_cfg(ns=(3,)))[0].run_all()
    bigger = make_cfg(ns=(3,), scenarios=("outbound", "peer"))               # same experiment, one more scenario: allowed
    r, rt, _ = harness(bigger)
    r.run_all()
    assert (cfg_dir := bigger.results_dir / "t" / "setup9-fake" / "peer" / "ttfb" / "n3" / "done.json").exists(), cfg_dir


def test_peer_path_that_cannot_come_up_does_not_cost_the_outbound_results(make_cfg, harness):
    """Regression (gVisor, setup 3): a failing peer path aborted the whole group, so the outbound scenario was lost too."""
    from conftest import FakeSetup
    cfg = make_cfg(ns=(3,), scenarios=("outbound", "peer"))
    FakeSetup.peer_fails = True
    r, rt, topo = harness(cfg)
    FakeSetup.peer_fails = True                                              # the harness resets it; set it again for this run
    r.run_all()
    base = cfg.results_dir / "t" / "setup9-fake"
    assert (base / "outbound" / "ttfb" / "n3" / "done.json").exists()          # outbound was measured
    assert not (base / "peer" / "ttfb" / "n3" / "done.json").exists()
    flag = json.loads((base / "peer" / "n3" / "failed.json").read_text())
    assert flag["unavailable"] is True and "peer" in flag["error"]
    # resuming: the unavailable scenario is remembered, the finished group is not repeated
    r2, rt2, topo2 = harness(cfg)
    r2.run_all()
    assert topo2.events == [] and rt2.calls == []
    (base / "peer" / "n3" / "failed.json").unlink()                           # deleting the flag asks for another try
    FakeSetup.peer_fails = False
    r3, rt3, _ = harness(cfg)
    r3.run_all()
    assert (base / "peer" / "ttfb" / "n3" / "done.json").exists()
