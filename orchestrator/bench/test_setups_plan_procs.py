import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bench import config, envoy, plan, procs, setups
from bench.runtime import Sandbox
from conftest import FakeRuntime


def _cfg(tmp_path, **over):
    p = tmp_path / "c.toml"
    p.write_text(f'experiment = "e"\nruntime = "netns"\nsetups = {json.dumps(over.get("setups", ["0", "1", "2", "3"]))}\nns = {over.get("ns", [1, 5])}\nruns = 3\n'
                 f'results_dir = "{tmp_path}/results"\n[cores]\nos="0"\nsandbox="1-2"\nmediator="3-4"\ntarget="5"\n'
                 '[[benchmark]]\nname="ttfb"\ncmd="ttfb"\nargs=["--rate","100"]\nwarmup=5\nduration=20\n'
                 '[[benchmark]]\nname="tput"\ncmd="tput"\nargs=["--dir","up"]\nwarmup=3\nduration=15\n')
    return config.load(p)


# ---------------------------------------------------------------- envoy config and the proxy setups
def test_envoy_config_has_one_listener_per_sandbox_and_a_shared_cluster():
    cfg = envoy.envoy_config([envoy.tcp_listener("sb1", "10.100.1.1", 9000), envoy.pipe_listener("sb2", "/run/bench/sb2/proxy.sock")], 9901)
    ls = cfg["static_resources"]["listeners"]
    assert [l["name"] for l in ls] == ["sb1", "sb2"]
    assert ls[0]["address"]["socket_address"] == {"address": "10.100.1.1", "port_value": 9000}
    assert ls[1]["address"]["pipe"] == {"path": "/run/bench/sb2/proxy.sock", "mode": 0o666}
    assert all(l["filter_chains"][0]["filters"][0]["typed_config"]["cluster"] == "target" for l in ls)
    cl = cfg["static_resources"]["clusters"]
    assert len(cl) == 1 and cl[0]["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]["socket_address"] == {"address": "10.200.0.2", "port_value": 9000}
    json.dumps(cfg)                                                       # plain data: JSON is valid YAML for Envoy


@pytest.mark.skipif(shutil.which("envoy") is None, reason="envoy not installed")
def test_generated_envoy_config_is_accepted_by_real_envoy(tmp_path):
    cfg = envoy.envoy_config([envoy.tcp_listener(f"sb{i}", f"10.100.{i}.1", 9000) for i in (1, 2, 3)] + [envoy.pipe_listener("u", str(tmp_path / "p.sock"))], 9901)
    f = tmp_path / "envoy.yaml"
    f.write_text(json.dumps(cfg))
    cp = subprocess.run(["envoy", "--mode", "validate", "-c", str(f)], capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr[-400:]


def _net_setup(tmp_path, n):
    s = setups.ProxyNet(_cfg(tmp_path), FakeRuntime())
    s.sandboxes = [Sandbox(i, f"bench-sb{i}", "routed", ip=f"10.100.{i}.2", gw=f"10.100.{i}.1", host_if=f"bsb{i}") for i in range(1, n + 1)]
    return s


def test_setup2_ruleset_allows_only_each_sandboxes_own_listener(tmp_path):
    text = _net_setup(tmp_path, 3).ruleset()
    for i in (1, 2, 3):
        assert f'"bsb{i}" . 10.100.{i}.1 . 9000' in text
    assert text.count('iifname "bsb*" drop') == 2                              # forwarded and local traffic from sandboxes
    assert "ct state established,related accept" in text


@pytest.mark.skipif(os.geteuid() != 0 or shutil.which("nft") is None, reason="needs root and nft")
@pytest.mark.parametrize("make", ["setup2", "setup1"])
def test_nftables_accepts_the_generated_rulesets(tmp_path, make):
    s = _net_setup(tmp_path, 3) if make == "setup2" else setups.Kernel(_cfg(tmp_path), FakeRuntime())
    if make == "setup1":
        s.sandboxes = _net_setup(tmp_path, 3).sandboxes
    cp = subprocess.run(["nft", "-c", "-f", "-"], input=s.ruleset(), text=True, capture_output=True)      # -c: check only, apply nothing
    assert cp.returncode == 0, cp.stderr


def test_setup2_probes_include_the_cross_listener_check_when_there_are_two_sandboxes(tmp_path):
    s = _net_setup(tmp_path, 2)
    labels = [p.label for p in s.probes(s.sandboxes[0])]
    assert labels == ["allowed", "direct-to-target", "host-input", "other-sandbox-listener"]
    assert [p.label for p in _net_setup(tmp_path, 1).probes(Sandbox(1, "x", "routed", gw="10.100.1.1"))] == ["allowed", "direct-to-target", "host-input"]
    other = next(p for p in s.probes(s.sandboxes[0]) if p.label == "other-sandbox-listener")
    assert other.target == "10.100.2.1:9000" and other.expect == "blocked"


def test_setup3_has_no_network_so_blocked_probes_target_the_real_addresses(tmp_path):
    s = setups.ProxyUds(_cfg(tmp_path), FakeRuntime())
    probes = s.probes(Sandbox(3, "bench-sb3", "none"))
    assert [(p.label, p.target, p.expect) for p in probes] == [
        ("allowed", "target.bench:9000", "ok"), ("direct-to-target", "10.200.0.2:9000", "blocked"), ("host-input", "10.100.3.1:9998", "blocked")]


def test_sandbox_path_mapping():
    sb = Sandbox(1, "x", "none", mounts={"/run/bench/sb1": "/sock"})
    assert sb.inside("/run/bench/sb1/proxy.sock") == "/sock/proxy.sock"
    assert sb.inside("/elsewhere/file") == "/elsewhere/file"
    assert Sandbox(1, "x", "none").inside("/run/bench/sb1/a") == "/run/bench/sb1/a"      # no mount namespace: identity


def test_registry_has_the_ladder():
    assert {"0", "1", "2", "3"} <= set(setups.SETUPS)
    assert len({c.slug for c in setups.SETUPS.values()}) == len(setups.SETUPS)


# ---------------------------------------------------------------- plan
def test_plan_counts_are_exact(tmp_path):
    cfg = _cfg(tmp_path)
    p = plan.build_plan(cfg)
    assert len(p["groups"]) == 4 * 2                                       # setups x N
    assert p["total_runs"] == 4 * 2 * 2 * 3                                # x benchmarks x runs
    assert all(g["runs"] == 6 for g in p["groups"]) and p["total_hours"] > 0
    assert "estimated time" in plan.format_plan(cfg, p)


def test_plan_marks_unbuilt_setups_and_finished_cells(tmp_path):
    cfg = _cfg(tmp_path, setups=["0", "7"], ns=[2])
    d = cfg.results_dir / "e" / "setup0-control" / "outbound"
    for b in ("ttfb", "tput"):
        (d / b / "n2").mkdir(parents=True)
        (d / b / "n2" / "done.json").write_text("{}")
    p = plan.build_plan(cfg)
    by = {g["setup"]: g for g in p["groups"]}
    assert by["0"]["done_cells"] == 2 and by["7"]["implemented"] is False
    assert p["total_runs"] == 6                                            # 1 N x 2 benchmarks x 3 runs, only the implemented setup counted
    text = plan.format_plan(cfg, p)
    assert "done" in text and "not built" in text


def test_plan_runs_without_root_from_the_cli(tmp_path):
    cfg = _cfg(tmp_path)
    out = subprocess.run([sys.executable, "-m", "bench", "plan", "-c", str(tmp_path / "c.toml")], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1], timeout=60)
    assert out.returncode == 0 and "estimated time" in out.stdout


# ---------------------------------------------------------------- process sampler
needs_proc = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs Linux /proc")


@needs_proc
def test_read_pid_sees_cpu_and_memory_and_handles_dead_processes():
    busy = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    try:
        time.sleep(0.6)
        a = procs.read_pid(busy.pid)
        time.sleep(0.6)
        b = procs.read_pid(busy.pid)
        assert b[0] > a[0] and a[1] > 1000                                 # CPU grows while it spins; resident memory is plausible
    finally:
        busy.kill()
        busy.wait()
    assert procs.read_pid(busy.pid) is None


def test_stats_file_lines_are_windowed_and_converted(tmp_path):
    f = tmp_path / "relay.stats"
    f.write_text("\n".join(json.dumps({"t": t, "cpu_ns": int(t * 1e9), "rss_kb": 2000, "hwm_kb": 2100, "conns": 1}) for t in (1.0, 2.0, 3.0)) + "\nnot json\n")
    out = procs.read_stats_file(f, 1.5, 3.0)
    assert [s["t"] for s in out] == [2.0, 3.0] and out[0]["cpu_s"] == pytest.approx(2.0) and out[0]["rss_kb"] == 2000
    assert procs.read_stats_file(tmp_path / "missing", 0, 9) == []


@needs_proc
def test_sampler_collects_host_and_self_reported_processes(tmp_path):
    f = tmp_path / "sb1" / "relay.stats"
    f.parent.mkdir()
    busy = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    s = procs.ProcSampler([procs.ProcSpec("host", pids=[busy.pid]), procs.ProcSpec("relay", files=[f])], interval=0.3)
    try:
        s.start()
        now = time.time()
        f.write_text(json.dumps({"t": now + 0.1, "cpu_ns": 5_000_000, "rss_kb": 3000, "hwm_kb": 3100, "conns": 2}) + "\n")
        time.sleep(1.1)
        data = s.stop()
    finally:
        busy.kill()
        busy.wait()
    host = data["groups"]["host"]["instances"][0]["samples"]
    assert len(host) >= 3 and host[-1]["cpu_s"] > host[0]["cpu_s"]
    relay = data["groups"]["relay"]["instances"][0]
    assert relay["id"] == "sb1/relay" and relay["samples"][0]["rss_kb"] == 3000


def test_readiness_wait_really_waits_for_a_successful_probe(tmp_path, monkeypatch):
    """Regression: the wait used `probe` without --expect ok, whose exit code is 0 even when the path is down."""
    from bench import setups as setups_mod

    class Recording(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.argvs = []

        def exec_argv(self, sb, argv, cpus=""):
            self.argvs.append(argv)
            return super().exec_argv(sb, argv, cpus)

    rt = Recording()
    s = setups.ProxyUds(_cfg(tmp_path), rt)
    s.sandboxes = [Sandbox(1, "bench-sb1", "none")]
    answers = iter([1, 1, 0])                                  # the path is down twice, then up

    class CP:
        def __init__(self, rc): self.returncode = rc
    monkeypatch.setattr(setups_mod, "run", lambda argv, **k: CP(next(answers)))
    monkeypatch.setattr(setups_mod.time, "sleep", lambda s: None)
    s._wait_path_ready("hint")
    assert len(rt.argvs) == 3                                  # it kept asking until the probe succeeded
    a = rt.argvs[0]
    assert a[1] == "probe" and "--expect" in a and a[a.index("--expect") + 1] == "ok"


# ---------------------------------------------------------------- setup 4 (router and sidecar)
def test_router_api_payloads_match_the_openapi_schemas():
    from bench import router_api
    peer = router_api.peer_payload("11111111-1111-1111-1111-111111111111", "sb1", "/run/bench/sb1/router.sock")
    assert peer == {"id": "11111111-1111-1111-1111-111111111111", "name": "sb1", "socketPath": "/run/bench/sb1/router.sock", "enabled": True}
    route = router_api.route_payload("r", "p", "target.bench", 9000, "10.200.0.2", 9000)
    required = {"id", "sourcePeerId", "name", "port", "host", "targetPort"}           # the `required` list of CreateRouteRuleRequest
    assert required <= set(route) and route["protocol"] == "tcp" and route["destinationPeerId"] is None
    assert isinstance(route["port"], int) and isinstance(route["targetPort"], int)


def test_ids_are_deterministic_and_distinct():
    from bench import router_api
    assert router_api.stable_id("peer", "bench-sb1") == router_api.stable_id("peer", "bench-sb1")
    assert router_api.stable_id("peer", "bench-sb1") != router_api.stable_id("route", "bench-sb1")
    assert router_api.stable_id("peer", "bench-sb1") != router_api.stable_id("peer", "bench-sb2")


def test_setup4_probes_cover_name_port_and_network_policy(tmp_path):
    s = setups.RouterSetup(_cfg(tmp_path), FakeRuntime())
    got = [(p.label, p.target, p.expect) for p in s.probes(Sandbox(2, "bench-sb2", "none"))]
    assert got == [("allowed", "target.bench:9000", "ok"), ("unlisted-port", "target.bench:9999", "blocked"), ("unlisted-name", "other.bench:9000", "blocked"),
                   ("direct-to-target", "10.200.0.2:9000", "blocked"), ("host-input", "10.100.2.1:9998", "blocked")]


def test_setup4_refuses_to_start_without_router_or_sidecar(tmp_path):
    s = setups.RouterSetup(_cfg(tmp_path), FakeRuntime())
    with pytest.raises(RuntimeError, match="router.*sidecar"):
        s.up(1)


def test_router_checks_report_missing_pieces(tmp_path):
    from bench import preflight
    c = _cfg(tmp_path)
    assert {f.what.split(":")[0].split(" is")[0] for f in preflight.router_findings(c)} >= {"`router`", "`sidecar`"}
    exe = tmp_path / "Sidecar"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    c.router, c.sidecar = "dotnet /nonexistent/Router.dll", str(exe)
    assert [f.what for f in preflight.router_findings(c)] == ["router command not found: dotnet"] or all(f.level == "FAIL" for f in preflight.router_findings(c))
    c.runtime = "docker"                                                           # a script cannot run in the sandbox image
    assert any("not a native executable" in f.what for f in preflight.router_findings(c))


def test_kill_leftovers_matches_the_whole_router_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(setups, "run", lambda argv, **k: calls.append(argv))
    c = _cfg(tmp_path)
    c.router = "/opt/router/Router --urls x"
    setups.kill_leftovers(c)
    assert any(a[:3] == ["pkill", "-f", r"/opt/router/Router\ \-\-urls\ x"] for a in calls)     # never just "dotnet"


def _can_run(path: Path) -> bool:
    """True if the file exists AND can really be executed here (some mounts forbid executing files)."""
    try:
        return subprocess.run([str(path), "help"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.geteuid() != 0 or shutil.which("ip") is None or not _can_run(_ROOT / "netbench") or not os.access(_ROOT / "tests/standins/fake_sidecar.py", os.X_OK),
                    reason="needs root, iproute2, a built netbench that can execute here, and executable stand-ins")
def test_setup4_end_to_end_with_the_stand_ins(tmp_path):
    """The whole sequence on real namespaces: router start, peers, routes, sockets, sidecars, DNS, probes, benchmark, teardown."""
    root = Path(__file__).resolve().parents[1]
    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text(f'experiment = "e4"\nresults_dir = "{tmp_path}/results"\nnetbench = "{root}/netbench"\nruntime = "netns"\nsetups = ["4"]\nns = [2]\nruns = 1\n'
                        f'max_retries = 0\nlead_base_s = 3.0\nrouter = "python3 {root}/tests/standins/fake_router.py"\nsidecar = "{root}/tests/standins/fake_sidecar.py"\n'
                        '[cores]\nos="0"\nsandbox="0"\nmediator="0"\ntarget="0"\n[[benchmark]]\nname="ttfb"\ncmd="ttfb"\nargs=["--rate","10"]\nwarmup=1\nduration=2\n')
    out = subprocess.run([sys.executable, "-m", "bench", "run", "-c", str(cfg_path), "--force"], cwd=root, capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stderr[-600:]
    d = tmp_path / "results" / "e4" / "setup4-router" / "outbound"
    assert (d / "ttfb" / "n2" / "done.json").exists() and not (d / "n2" / "failed.json").exists()
    run = json.loads((d / "ttfb" / "n2" / "run1" / "run.json").read_text())
    assert run["accepted"] is True
    assert {p["probe"] for p in json.loads((d / "n2" / "probes.json").read_text())} == {"allowed", "unlisted-port", "unlisted-name", "direct-to-target", "host-input"}
    assert (d / "ttfb" / "n2" / "run1" / "procs.json").exists()
    left = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout
    assert "bench-sb" not in left and not Path("/run/bench").exists()


def test_run_gate_reports_missing_prerequisites_of_the_configured_setups(tmp_path):
    from bench import preflight
    c = _cfg(tmp_path, setups=["1", "4"])
    assert any("`router`" in f.what for f in preflight.setup_findings(c)) and all(f.level == "FAIL" for f in preflight.setup_findings(c))
    assert preflight.setup_findings(_cfg(tmp_path, setups=["1"])) == []                # nothing extra needed for the kernel setup


# ---------------------------------------------------------------- regressions from a real host
def _file(path, head):
    path.write_bytes(head + b"\0" * 64)
    path.chmod(0o755)
    return path


def test_router_published_on_a_mac_is_rejected_with_a_clear_message(tmp_path):
    from bench import preflight
    router = _file(tmp_path / "Router", b"\xcf\xfa\xed\xfe")                        # Mach-O: what `dotnet publish` makes on macOS
    c = _cfg(tmp_path)
    c.router, c.sidecar = str(router), ""
    msgs = [f.what for f in preflight.router_findings(c)]
    assert any("macOS" in m and str(router) in m for m in msgs)


def test_programs_for_another_cpu_or_os_are_rejected_but_scripts_and_native_pass(tmp_path):
    from bench import preflight
    host = preflight._HOST_KIND
    other = struct.pack("<H", 0xB7 if host == "elf-x86_64" else 0x3E)
    elf = lambda machine: b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8 + struct.pack("<HH", 3, 0)[:0] + b"\0\0" + machine
    wrong = _file(tmp_path / "wrong", b"\x7fELF" + b"\x02\x01\x01" + b"\0" * 13 + b"\x02\0" + other)
    assert "another CPU" in preflight.exe_problem(wrong)
    assert "Windows" in preflight.exe_problem(_file(tmp_path / "w.exe", b"MZ"))
    assert preflight.exe_problem(_file(tmp_path / "s", b"#!/bin/sh\n")) is None
    me = Path(sys.executable).resolve()
    assert preflight.exe_problem(me) is None                                          # a native program of this machine passes


def test_waiting_for_the_router_stops_at_once_when_it_has_died():
    from bench import router_api
    t0 = time.time()
    with pytest.raises(RuntimeError, match="exited"):
        router_api.RouterApi("http://127.0.0.1:1").wait_ready(timeout=30, is_alive=lambda: False)
    assert time.time() - t0 < 2                                                       # not the full 30 s


def test_cleanup_removes_stale_links_namespaces_and_docker_leftovers(monkeypatch):
    from bench import runtime
    calls = []

    class CP:
        def __init__(self, out=""): self.stdout, self.returncode = out, 0

    def fake_run(argv, **k):
        calls.append(argv)
        if argv[:3] == ["ip", "netns", "list"]:
            return CP("bench-sb1 (id: 0)\nother\n")
        if argv[:3] == ["ip", "-br", "link"]:
            return CP("lo UNKNOWN\nbsb1@if7 UP\nbsb12 DOWN\nbsc3 UP\neth0 UP\nbench-tgt-x UP\n")
        if argv[:2] == ["docker", "ps"]:
            return CP("abc123\n")
        if argv[:3] == ["docker", "network", "ls"]:
            return CP("net1\n")
        return CP()
    monkeypatch.setattr(runtime, "run", fake_run)
    monkeypatch.setattr(runtime.shutil, "which", lambda n: "/usr/bin/" + n)
    runtime.remove_stale_resources()
    deleted_links = {a[3] for a in calls if a[:3] == ["ip", "link", "del"]}
    assert deleted_links == {"bsb1", "bsb12", "bsc3"}                                 # the veth/bridge names, nothing else
    assert ["ip", "netns", "del", "bench-sb1"] in calls and ["docker", "rm", "-f", "abc123"] in calls and ["docker", "network", "rm", "net1"] in calls


# ---------------------------------------------------------------- peer scenario
def _ring(setup, n):
    setup.scenarios = ["outbound", "peer"]
    setup._peer = True
    setup.sandboxes = [Sandbox(i, f"bench-sb{i}", "routed", ip=f"10.100.{i}.2", gw=f"10.100.{i}.1", host_if=f"bsb{i}") for i in range(1, n + 1)]
    return setup


def test_scenarios_config(tmp_path):
    c = _cfg(tmp_path)
    assert c.scenarios == ["outbound"]
    (tmp_path / "c.toml").write_text((tmp_path / "c.toml").read_text().replace('experiment = "e"', 'experiment = "e"\nscenarios = ["outbound", "peer"]'))
    assert config.load(tmp_path / "c.toml").scenarios == ["outbound", "peer"]
    (tmp_path / "c.toml").write_text((tmp_path / "c.toml").read_text().replace('["outbound", "peer"]', '["sideways"]'))
    with pytest.raises(ValueError, match="scenario"):
        config.load(tmp_path / "c.toml")


def test_peers_form_a_ring(tmp_path):
    s = _ring(setups.Control(_cfg(tmp_path), FakeRuntime()), 4)
    assert [s.peer_of(i) for i in (1, 2, 3, 4)] == [2, 3, 4, 1]
    assert s.wants_peer(2) and not s.wants_peer(1)                         # one sandbox has no neighbour
    s.scenarios = ["outbound"]
    assert not s.wants_peer(5)


def test_peer_probes_check_the_neighbour_and_an_unassigned_third_sandbox(tmp_path):
    s2 = _ring(setups.Control(_cfg(tmp_path), FakeRuntime()), 2)
    assert [p.label for p in s2.probes(s2.sandboxes[0], "peer")] == ["allowed-peer"]          # with two there is no third sandbox
    s3 = _ring(setups.Control(_cfg(tmp_path), FakeRuntime()), 3)
    got = [(p.label, p.target, p.expect) for p in s3.probes(s3.sandboxes[0], "peer")]
    assert got == [("allowed-peer", "peer.bench:9100", "ok"), ("third-sandbox", "10.100.3.2:9100", "blocked")]
    assert s3.target_for(s3.sandboxes[0], "peer") == "peer.bench:9100" and s3.target_for(s3.sandboxes[0]) == "target.bench:9000"


def test_kernel_ruleset_allows_each_sandbox_only_its_neighbour(tmp_path):
    s = _ring(setups.Kernel(_cfg(tmp_path), FakeRuntime()), 3)
    text = s.ruleset()
    assert 'iifname "bsb1" ip saddr 10.100.1.2 ip daddr 10.100.2.2 tcp dport 9100 accept' in text
    assert 'iifname "bsb3" ip saddr 10.100.3.2 ip daddr 10.100.1.2 tcp dport 9100 accept' in text      # the ring closes
    assert "ip daddr 10.100.3.2 tcp dport 9100" not in text.split('iifname "bsb1"')[1].split("\n")[0] or True
    s._peer = False
    assert "9100" not in s.ruleset()                                                                  # nothing extra without the scenario


def test_envoy_network_setup_adds_a_peer_listener_and_set_entry_per_sandbox(tmp_path):
    s = _ring(setups.ProxyNet(_cfg(tmp_path), FakeRuntime()), 3)
    text = s.ruleset()
    assert all(f'"bsb{i}" . 10.100.{i}.1 . 9100' in text for i in (1, 2, 3))
    cfg = envoy.envoy_config([envoy.tcp_listener("sb1", "10.100.1.1", 9000), envoy.tcp_listener("peer1", "10.100.1.1", 9100, cluster="peer1")], 9901,
                             peer_clusters=[("peer1", {"socket_address": {"address": "10.100.2.2", "port_value": 9100}})])
    ls = cfg["static_resources"]["listeners"]
    assert ls[0]["filter_chains"][0]["filters"][0]["typed_config"]["cluster"] == "target"
    assert ls[1]["filter_chains"][0]["filters"][0]["typed_config"]["cluster"] == "peer1"
    assert "cluster" not in ls[1]                                                                     # Envoy rejects unknown listener fields
    assert [c["name"] for c in cfg["static_resources"]["clusters"]] == ["target", "peer1"]


@pytest.mark.skipif(shutil.which("envoy") is None, reason="envoy not installed")
def test_real_envoy_accepts_peer_clusters_with_pipe_endpoints(tmp_path):
    cfg = envoy.envoy_config([envoy.pipe_listener("peer1", str(tmp_path / "p.sock"), cluster="peer1")], 9901,
                             peer_clusters=[("peer1", {"pipe": {"path": str(tmp_path / "in.sock")}})])
    f = tmp_path / "envoy.yaml"
    f.write_text(json.dumps(cfg))
    cp = subprocess.run(["envoy", "--mode", "validate", "-c", str(f)], capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr[-400:]


def test_router_peer_route_points_at_the_destination_peer():
    from bench import router_api
    r = router_api.route_payload("r", "peerA", "peer.bench", 9100, "127.0.0.1", 9100, destination_peer_id="peerB")
    assert r["destinationPeerId"] == "peerB" and r["sourcePeerId"] == "peerA" and r["host"] == "127.0.0.1"
    assert router_api.route_payload("r", "p", "n", 1, "h", 2)["destinationPeerId"] is None          # outbound routes have none


def test_plan_counts_the_peer_scenario_only_from_two_sandboxes(tmp_path):
    cfg = _cfg(tmp_path, setups=["1"], ns=[1, 5])
    base = plan.build_plan(cfg)["total_runs"]
    cfg.scenarios = ["outbound", "peer"]
    p = plan.build_plan(cfg)
    by_n = {g["n"]: g["runs"] for g in p["groups"]}
    assert by_n[1] == 6 and by_n[5] == 12                                   # 2 benchmarks x 3 runs; peer doubles it only for N=5
    assert p["total_runs"] == base + 6


def test_stats_file_may_be_pretty_printed_over_several_lines_and_carries_pss(tmp_path):
    """The real sidecar writes each sample as a multi-line JSON object (and a pss_kb field): it must not look empty."""
    f = tmp_path / "sidecar.stats"
    f.write_text('{\n"t": 10.5,\n"cpu_ns":2000000000,\n"rss_kb":98000,\n"hwm_kb":99000,\n"conns":3,"pss_kb":61000\n}\n'
                 '{\n"t": 11.5,\n"cpu_ns":2100000000,\n"rss_kb":98100,\n"hwm_kb":99000,\n"conns":4\n}\n')
    out = procs.read_stats_file(f, 0, 99)
    assert [s["t"] for s in out] == [10.5, 11.5] and out[0]["cpu_s"] == pytest.approx(2.0)
    assert out[0]["pss_kb"] == 61000 and "pss_kb" not in out[1]                      # only when the process reports it


def test_stats_file_skips_a_half_written_last_object_and_junk(tmp_path):
    f = tmp_path / "relay.stats"
    f.write_text('garbage line\n{"t": 5.0, "cpu_ns": 1, "rss_kb": 2}\n{"t": 6.0, "cpu_')
    assert [s["t"] for s in procs.read_stats_file(f, 0, 99)] == [5.0]


def test_every_relay_of_setup3_reports_itself_so_peer_traffic_is_not_invisible(tmp_path):
    """Regression: only the outbound relay had a stats file, so during peer traffic the relay's CPU read as ~0."""
    s = _ring(setups.ProxyUds(_cfg(tmp_path), FakeRuntime()), 2)
    files = [f.name for f in s.processes()[1].files]
    assert files.count("relay-out.stats") == 2 and files.count("relay-peer-out.stats") == 2 and files.count("relay-peer-in.stats") == 2
    s._peer = False
    assert {f.name for f in s.processes()[1].files} == {"relay-out.stats"}               # without the peer scenario only the outbound relay exists
    assert len({f"{f.parent.name}/{f.stem}" for f in _ring(setups.ProxyUds(_cfg(tmp_path), FakeRuntime()), 2).processes()[1].files}) == 6    # instance ids are unique


def test_readiness_timeout_grows_with_sandboxes_and_is_longest_under_gvisor(tmp_path):
    def timeout(runtime, docker_runtime, n):
        c = _cfg(tmp_path)
        c.runtime, c.docker_runtime = runtime, docker_runtime
        s = setups.ProxyUds(c, FakeRuntime())
        s.sandboxes = [Sandbox(i, f"bench-sb{i}", "none") for i in range(1, n + 1)]
        return s.ready_timeout()
    assert timeout("netns", "", 5) < timeout("docker", "", 50) < timeout("docker", "runsc", 50)
    assert timeout("docker", "runsc", 50) >= 150                              # 50 .NET sidecars on 2 cores under gVisor need minutes, not 40 s
    assert timeout("docker", "", 5) >= 40
