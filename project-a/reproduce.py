"""Reproduce every recorded result from the repo alone: no network, no API key, no .env needed.

    python3 reproduce.py

All LLM calls run with LLM_MODE=replay, answered from llm_cache/; a call that was never recorded raises
CacheMiss and is reported, never sent. New results go to reproduce_out/<stamp>/ (the committed
project_a/eval/results/ is not touched). The steps:

  1. the whole test suite (pytest, against the committed results);
  2. hand-coded and LLM-compiled engines on both years' golden suites and official rows (no LLM);
  3. the LLM-at-query-time engine on both years' golden suites (replay);
  4. the parser evaluations, dev and heldout (replay);
  5. determinism (offline, query engine, parser), latency and cost are computed from steps 2-5;
  6. traceability;
  7. RESULTS_TABLES.md regenerated into reproduce_out/<stamp>/ and compared, table by table, with the committed
     copy. Wall-clock timings of the in-process engines are re-measured, so those cells are shown, not
     compared. Exit code 0 only if every step ran and every table matches.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMMITTED_TABLES = ROOT / "RESULTS_TABLES.md"
COMMITTED_RESULTS = ROOT / "project_a" / "eval" / "results"
PY = sys.executable
# Latency rows timed in-process on this machine: re-measured, so their four timing cells are not compared.
TIMED_ROWS = {"hand-coded", "compiled reviewed: round trip", "compiled reviewed: compute() only"}
# Prose fragments that describe the run itself (in replay every call is a cache hit, and a run stops where
# the recording stopped, for "not in cache" instead of the original reason).
RUN_SPECIFIC = [r"Live calls in this run: \d+", r"Parser live calls in this run: \d+", r"; STOPPED: [\w ]+"]


def steps(out):
    q = [PY, "-m", "project_a.eval.run", "--engine", "llm_query"]
    nl = [PY, "-m", "project_a.eval.nl_eval", "--prompt", "v1"]
    det = [PY, "-m", "project_a.eval.determinism"]
    return [
        ("test suite (pytest)", [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], (0,)),
        ("hand-coded + compiled engines, both years (no LLM)", [PY, "-m", "project_a.eval.measure_offline"], (0,)),
        ("query-time engine FY 2024-25 dev", q + ["--split", "dev"], (0,)),
        ("query-time engine FY 2024-25 heldout", q + ["--split", "heldout", "--allow-heldout"], (0,)),
        ("query-time engine FY 2025-26 dev+heldout", q + ["--suite", "fy2025_26", "--split", "all", "--allow-heldout"], (0,)),
        ("parser dev", nl + ["--split", "dev"], (0,)),
        ("parser heldout", nl + ["--split", "heldout", "--allow-heldout"], (0,)),
        ("determinism offline (10 repeats, no LLM)", det + ["offline"], (0,)),
        # exit 3 = stopped; the recording itself stopped on a 429, so replay stops at the same run.
        ("determinism query engine (replay)", det + ["query"], (0, 3)),
        ("determinism parser (replay)", det + ["parser"], (0,)),
        ("traceability", [PY, "-m", "project_a.eval.traceability"], (0,)),
        ("RESULTS_TABLES.md", [PY, "-m", "project_a.eval.compare", "--out", str(out / "RESULTS_TABLES.md")], (0,)),
    ]


def tables(md):
    """{'<section> / <subsection> #i': [row cells]}, plus the prose lines outside tables and 'Files used'."""
    found, prose, h2, h3, block, idx = {}, [], "", "", None, {}
    for line in md.splitlines() + [""]:
        if line.startswith("|"):
            if block is None:
                key = "%s / %s" % (h2, h3) if h3 else h2
                idx[key] = idx.get(key, 0) + 1
                block = found.setdefault("%s #%d" % (key, idx[key]), [])
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(set(c) <= set("-: ") for c in cells):
                block.append(cells)
            continue
        block = None
        if line.startswith("## "):
            h2, h3 = line[3:].strip(), ""
        elif line.startswith("### "):
            h3 = line[4:].strip()
        elif line.strip() and not h2.startswith("Files used"):
            prose.append(line)
    return found, prose


def compare_tables(old_md, new_md):
    old, old_prose = tables(old_md)
    new, new_prose = tables(new_md)
    report, ok = [], True
    for key, rows in old.items():
        got = new.get(key)
        if got is None:
            report.append(("FAIL", key, "table missing from the regenerated file"))
            ok = False
            continue
        diffs, remeasured = [], []
        for i in range(max(len(rows), len(got))):
            a = rows[i] if i < len(rows) else None
            b = got[i] if i < len(got) else None
            if a == b:
                continue
            if a and b and key.startswith("5. Latency") and a[0] in TIMED_ROWS and len(a) == len(b) \
                    and a[:2] + a[6:] == b[:2] + b[6:]:
                remeasured.append("%s mean %s -> %s, p95 %s -> %s" % (a[0], a[2], b[2], a[4], b[4]))
                continue
            diffs.append("row %d: committed %s | regenerated %s" % (i, a, b))
        if diffs:
            ok = False
            report.append(("FAIL", key, "; ".join(diffs[:5]) + (" (+%d more)" % (len(diffs) - 5) if len(diffs) > 5 else "")))
        else:
            report.append(("PASS", key, ("timings re-measured, not compared: " + "; ".join(remeasured)) if remeasured else ""))
    for key in new:
        if key not in old:
            report.append(("FAIL", key, "table not in the committed file"))
            ok = False

    def mask(s):
        for pat in RUN_SPECIFIC:
            s = re.sub(pat, "<run-specific>", s)
        return s
    prose_notes = []
    if [mask(s) for s in old_prose] != [mask(s) for s in new_prose]:
        ok = False
        prose_notes.append(("FAIL", "text outside the tables differs beyond run-specific counts"))
    for a, b in zip(old_prose, new_prose):
        if a != b:
            prose_notes.append(("NOTE", "committed: %s\n         regenerated: %s" % (a.strip(), b.strip())))
    return ok, report, prose_notes


def _latest(folder, pattern):
    files = sorted(folder.glob(pattern))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def determinism_query_note(out):
    """Explains which recorded query-engine runs could not be replayed, and why."""
    rec, rep = _latest(COMMITTED_RESULTS, "determinism_query_*.json"), _latest(out, "determinism_query_*.json")
    if not rec or not rep:
        return "determinism query: results file missing"
    status = lambda d: [(r["id"], x["run_index"], x["status"] == "done") for r in d["rows"] for x in r["runs"]]
    same = status(rec) == status(rep)
    done = sum(1 for *_, d in status(rep) if d)
    return ("determinism query engine: %d of %d runs replayed; the other %d were never recorded, because the live run "
            "stopped on %s (%s). Replay stops at the same run: %s." % (
                done, len(status(rep)), len(status(rep)) - done, (rec.get("stopped") or ["-"])[0],
                "HTTP 429, free-tier daily quota" if (rec.get("stopped") or [""])[0] == "quota" else "see recording",
                "yes" if same else "NO, the replay differs from the recording"))


def main():
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is needed (this is %s)." % sys.version.split()[0])
        return 1
    t_start = time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "reproduce_out" / stamp
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, LLM_MODE="replay", PROJECT_A_RESULTS_DIR=str(out), PYTHONDONTWRITEBYTECODE="1")
    print("Reproducing Project A in replay mode (no network). Output: %s\n" % out.relative_to(ROOT))
    results, log = [], open(out / "reproduce.log", "w", encoding="utf-8")
    for name, cmd, ok_codes in steps(out):
        t0 = time.time()
        step_env = env if "pytest" not in cmd else {k: v for k, v in env.items() if k != "PROJECT_A_RESULTS_DIR"}
        p = subprocess.run(cmd, cwd=ROOT, env=step_env, capture_output=True, text=True)
        dt = time.time() - t0
        log.write("=== %s (exit %d, %.1f s)\n$ %s\n%s\n%s\n" % (name, p.returncode, dt, " ".join(cmd), p.stdout, p.stderr))
        good = p.returncode in ok_codes
        tail = [l for l in (p.stdout + p.stderr).strip().splitlines() if l.strip()][-1:] or [""]
        miss = re.findall(r"CacheMiss: .*", p.stdout + p.stderr)
        print("%-4s %-50s %6.1f s  %s" % ("ok" if good else "FAIL", name, dt, (miss[0] if miss else tail[0])[:110]))
        results.append((name, good))
    log.close()
    print("\n" + determinism_query_note(out))

    regenerated = out / "RESULTS_TABLES.md"
    print("\nTables in RESULTS_TABLES.md, regenerated vs committed:")
    tables_ok = False
    if regenerated.exists():
        tables_ok, report, notes = compare_tables(COMMITTED_TABLES.read_text(encoding="utf-8"),
                                                  regenerated.read_text(encoding="utf-8"))
        for status, key, detail in report:
            print("  %-4s %s%s" % (status, key, ("\n         " + detail) if detail else ""))
        if notes:
            print("\nText outside the tables (run-specific counts differ by design in replay):")
            for status, detail in notes:
                print("  %-4s %s" % (status, detail))
        print("\n('Files used' is not compared: its file names carry the run's timestamps.)")
    else:
        print("  FAIL RESULTS_TABLES.md was not regenerated")

    steps_ok = all(g for _, g in results)
    overall = steps_ok and tables_ok
    print("\nSteps: %d/%d ran as expected. Tables: %s. Runtime %.0f s. Full log: %s" % (
        sum(g for _, g in results), len(results), "all match" if tables_ok else "MISMATCH", time.time() - t_start,
        (out / "reproduce.log").relative_to(ROOT)))
    print("REPRODUCE: %s" % ("PASS" if overall else "FAIL"))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
