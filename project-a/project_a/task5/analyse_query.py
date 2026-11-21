"""Task 5: classify LLM-engine answers on FY 2025-26 cases and measure anchoring on the old law. No LLM calls.

    python3 -m project_a.task5.analyse_query <results.json>

right  = both taxes equal the FY 2025-26 oracle; silent = an answer with at least one wrong tax and no
error; error = unparseable / error result. A wrong answer is consistent with the OLD law when its new-regime
tax equals the oracle's answer for the same input under FY 2024-25 (only the new regime changed).
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

from project_a.tests.oracle import oracle

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "cases" / "golden_fy2025_26.jsonl"
FIELDS = ("old_total_income", "old_tax", "new_total_income", "new_tax")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def analyse(path, examples=5):
    data = json.loads(Path(path).read_text())
    cases = {json.loads(l)["id"]: json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()}
    rows = []
    for r in data["rows"]:
        inp = cases[r["id"]]["input"]
        old_law = oracle(dict(inp, fy="2024-25"))
        new_law = r["expected"]
        got = r["got"]
        if r["error"]:
            kind = "error"
        elif got["old_tax"] == new_law["old_tax"] and got["new_tax"] == new_law["new_tax"]:
            kind = "right"
        else:
            kind = "silent"
        differs = new_law["new_tax"] != old_law["new_tax"]
        rows.append({**r, "kind": kind, "old_law": old_law, "input": inp, "laws_differ": differs,
                     "old_law_new_tax": kind == "silent" and got["new_tax"] == old_law["new_tax"] and differs,
                     "old_law_all4": kind == "silent" and all(got[k] == old_law[k] for k in FIELDS),
                     # direction: a wrong new-regime tax nearer the old-law answer than the new-law one
                     "nearer_old": (kind == "silent" and differs and got["new_tax"] != new_law["new_tax"]
                                    and _num(got["new_tax"]) and abs(got["new_tax"] - old_law["new_tax"])
                                    < abs(got["new_tax"] - new_law["new_tax"])),
                     "wrong_new_laws_differ": kind == "silent" and differs and got["new_tax"] != new_law["new_tax"]})
    return data["summary"], rows


def counts(rows):
    c = defaultdict(int)
    for r in rows:
        c[r["kind"]] += 1
        c["old_law_new_tax"] += r["old_law_new_tax"]
        c["old_law_all4"] += r["old_law_all4"]
        c["nearer_old"] += r["nearer_old"]
        c["wrong_new_laws_differ"] += r["wrong_new_laws_differ"]
        c["within_10"] += r["tax_within_10"]
        c["income_exact"] += r["income_exact"]
        c["best_match"] += r["best_match"]
        c["wrong_new_tax"] += r["kind"] == "silent" and r["got"]["new_tax"] != r["expected"]["new_tax"]
        c["wrong_old_tax"] += r["kind"] == "silent" and r["got"]["old_tax"] != r["expected"]["old_tax"]
    c["n"] = len(rows)
    return dict(c)


def line(label, c):
    n = c["n"]
    return ("%-22s n=%3d  right %3d  silent %3d  error %2d | within Rs10 %3d  income %3d  best %3d | "
            "silent: wrong new-tax %3d (= old-law new-tax %3d; all 4 = old law %3d; where laws differ %3d, "
            "nearer old law %3d), wrong old-tax %3d"
            % (label, n, c.get("right", 0), c.get("silent", 0), c.get("error", 0), c["within_10"],
               c["income_exact"], c["best_match"], c["wrong_new_tax"], c["old_law_new_tax"], c["old_law_all4"],
               c["wrong_new_laws_differ"], c["nearer_old"], c["wrong_old_tax"]))


def report(path, by_split=True, by_category=True, examples=5):
    summary, rows = analyse(path)
    out = [line("ALL", counts(rows))]
    if by_split:
        for sp in ("dev", "heldout"):
            sub = [r for r in rows if r.get("split") == sp]
            if sub:
                out.append(line("split " + sp, counts(sub)))
    if by_category:
        for cat in sorted({r["category"] for r in rows}):
            out.append(line("  " + cat, counts([r for r in rows if r["category"] == cat])))
    ex = ([r for r in rows if r["old_law_new_tax"]] + [r for r in rows if r["nearer_old"] and not r["old_law_new_tax"]])[:examples]
    for r in ex:
        i = {k: v for k, v in r["input"].items() if v not in (0, False) and k != "fy"}
        out.append("EXAMPLE %s %s | model %s | FY26 oracle %s | FY25 oracle %s" % (
            r["id"], i, {k: r["got"][k] for k in FIELDS}, {k: r["expected"][k] for k in FIELDS},
            {k: r["old_law"][k] for k in FIELDS}))
    return "\n".join(out), summary, rows


if __name__ == "__main__":
    text, summary, _ = report(sys.argv[1])
    print(text)
