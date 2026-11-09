#!/usr/bin/env python3
"""Short, shareable summary of one experiment folder: what failed and why, without the raw JSON files.

    python3 report.py results/<experiment>  >  report.txt

No root needed, standard library only, independent of the rest of the code (works with older versions too).
It prints: host and tuning facts, failed groups (with the mediator's last log lines), failed probes, runs that were
invalid or redone (grouped, with the distinct reasons and error counts), and the warnings and tail of orchestrator.log.
"""
import collections
import json
import re
import sys
from pathlib import Path


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def tail(path, n):
    try:
        lines = [l.rstrip() for l in Path(path).read_text(errors="replace").splitlines() if l.strip()]
    except OSError:
        return []
    return lines[-n:]


def norm(text):
    return re.sub(r"\d+(\.\d+)?", "#", text)


def capped(lines, limit):
    return lines[:limit] + ([f"  (+{len(lines) - limit} more)"] if len(lines) > limit else [])


def header(exp, out):
    m = load(exp / "manifest.json") or {}
    h, c, t = m.get("host", {}), m.get("config", {}), m.get("tools", {})
    freq = sorted({f"{v.get('min_khz')}-{v.get('max_khz')}" for v in (h.get("freq") or {}).values()})
    idle = sorted(set((h.get("cpuidle") or {}).values()))
    out += [f"experiment {exp.name}   runtime {c.get('runtime')}/{c.get('docker_runtime') or 'default'}   scenarios {c.get('scenarios', ['outbound'])}",
            f"setups {c.get('setups')}  N {c.get('ns')}  runs {c.get('runs')}  retries {c.get('max_retries')}  cores {c.get('cores')}",
            f"host: {h.get('cpu')} | {h.get('cpus')} cpus | kernel {h.get('kernel')} | smt {h.get('smt_active')} | governors {h.get('governors')} | boost {h.get('boost')}",
            f"      clock {freq} kHz | idle states {idle} | conntrack max {h.get('nf_conntrack_max')}",
            f"tools: docker {t.get('docker')} | runsc {t.get('runsc')} | envoy {str(t.get('envoy'))[:60]} | netbench {str((m.get('netbench') or {}).get('sha256'))[:12]}"]
    if not m:
        out.append("(no manifest.json: was this the experiment folder?)")


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    exp = Path(argv[1])
    if not exp.is_dir():
        sys.exit(f"not a folder: {exp}")
    out = []
    header(exp, out)

    setups = sorted(p for p in exp.iterdir() if p.is_dir() and re.match(r"setup\d+", p.name))
    cells = collections.defaultdict(lambda: {"final": 0, "accepted": 0, "rejected": 0, "reasons": collections.Counter(), "examples": {},
                                              "errors": collections.Counter(), "rc": collections.Counter(), "clientlog": collections.Counter()})
    failed, probes_bad, total_cells, done_cells = [], [], 0, 0

    for sdir in setups:
        for scdir in sorted(p for p in sdir.iterdir() if p.is_dir()):
            scenario = scdir.name
            for gdir in sorted(scdir.glob("n[0-9]*")):
                n = int(gdir.name[1:])
                f = load(gdir / "failed.json")
                if f:
                    lines = [l for l in (f.get("error") or "").splitlines() if l.strip()]
                    logs = [(p.name, tail(p, 5)) for p in sorted((gdir / "mediator").glob("*.log"))] if (gdir / "mediator").exists() else []
                    failed.append((sdir.name, scenario, n, lines[-1][:400] if lines else "?", logs))
                pr = load(gdir / "probes.json") or []
                agg = collections.defaultdict(lambda: [0, set(), ""])
                for p in pr:
                    if p.get("rc") != 0:
                        a = agg[p.get("probe")]
                        a[0] += 1
                        a[1].add((p.get("result") or {}).get("outcome"))
                        a[2] = a[2] or str((p.get("result") or {}).get("detail", ""))[:160]
                for name, (cnt, outcomes, detail) in agg.items():
                    probes_bad.append(f"  {sdir.name} {scenario} n={n}: probe '{name}' failed in {cnt} sandbox(es), outcome {sorted(map(str, outcomes))} {detail}")
            for bdir in sorted(p for p in scdir.iterdir() if p.is_dir() and not re.fullmatch(r"n\d+", p.name)):
                for ndir in sorted(bdir.glob("n[0-9]*")):
                    total_cells += 1
                    done_cells += (ndir / "done.json").exists()
                    key = (sdir.name, scenario, bdir.name, int(ndir.name[1:]))
                    cell = cells[key]
                    for rdir in sorted(p for p in ndir.iterdir() if p.is_dir() and p.name.startswith("run") and not p.name.endswith(".tmp")):
                        rejected = ".rejected" in rdir.name
                        meta = load(rdir / "run.json") or {}
                        cell["rejected" if rejected else "final"] += 1
                        if not rejected and meta.get("accepted"):
                            cell["accepted"] += 1
                        if rejected or not meta.get("accepted"):
                            for sb in rdir.glob("*.json"):
                                if sb.name in ("run.json", "cores.json", "procs.json"):
                                    continue
                                d = load(sb) or {}
                                for w in d.get("warnings") or []:
                                    cell["reasons"][norm(w)] += 1
                                    cell["examples"].setdefault(norm(w), w)
                                for k, v in (d.get("counts") or {}).items():
                                    if k.startswith("err_") or k in ("dropped", "lost", "bad_payload", "bad_total"):
                                        cell["errors"][k] += v
                            for sbname, rc in (meta.get("rc") or {}).items():
                                if rc != 0:
                                    cell["rc"][rc] += 1
                            for line in tail(rdir / "stderr.log", 400):
                                if "rc=" in line and "rc=0" not in line:
                                    cell["clientlog"][norm(re.sub(r"^\[[^\]]*\]\s*", "", line))[:200]] += 1

    bad = {k: v for k, v in cells.items() if v["rejected"] or v["accepted"] < v["final"]}
    out += ["", f"CELLS: {total_cells} total, {done_cells} finished, {len(bad)} with invalid or redone runs, {len(failed)} failed group(s)"]

    if failed:
        out += ["", "FAILED GROUPS"]
        for setup, scenario, n, last, logs in failed[:8]:
            out.append(f"  {setup} {scenario} n={n}: {last}")
            for name, lines in logs:
                out += [f"      {name}: " + (lines[0] if lines else "(empty)")] + [f"      {l[:160]}" for l in lines[1:]]
        out += capped([], 0) if len(failed) <= 8 else [f"  (+{len(failed) - 8} more failed groups)"]
    if probes_bad:
        out += ["", "FAILED PROBES"] + capped(probes_bad, 14)
    if bad:
        out += ["", "INVALID OR REDONE RUNS (per cell: runs accepted / final, rejected attempts)"]
        rows = []
        for (setup, scenario, bench, n), c in sorted(bad.items()):
            rows.append(f"  {setup} {scenario} {bench} n={n}: accepted {c['accepted']}/{c['final']}, rejected attempts {c['rejected']}")
            for reason, cnt in c["reasons"].most_common(2):
                rows.append(f"      x{cnt} {c['examples'][reason][:150]}")
            if c["errors"]:
                rows.append("      errors: " + ", ".join(f"{k} {v}" for k, v in c["errors"].most_common(4)))
            if c["rc"]:
                rows.append(f"      client exit codes: {dict(c['rc'])}")
            for line, cnt in c["clientlog"].most_common(1):
                rows.append(f"      x{cnt} {line}")
        out += capped(rows, 40)

    log = exp / "orchestrator.log"
    if log.exists():
        warn = collections.Counter()
        first = {}
        for l in log.read_text(errors="replace").splitlines():
            m = re.match(r"\d\d:\d\d:\d\d (WARNING|ERROR) (.*)", l)
            if m:
                k = norm(m.group(2))[:160]
                warn[k] += 1
                first.setdefault(k, m.group(2)[:200])
        out += ["", "orchestrator.log: warnings and errors (distinct)"] + capped([f"  x{c} {first[k]}" for k, c in warn.most_common()], 8)
        out += ["orchestrator.log: last lines"] + [f"  {l[:200]}" for l in tail(log, 8)]
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv)
