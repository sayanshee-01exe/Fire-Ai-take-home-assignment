"""Traceability: does every rupee in an answer trace back to a clause of the rules?

    python3 -m project_a.eval.traceability       # no LLM calls; writes results/traceability_<stamp>.json

Three levels, checked per answer (one case = one answer):
  (a) TAGGED:  every trace step carries a clause identifier (or "input" for a figure copied from the input),
               and the four reported amounts (old/new total income, old/new tax) each appear as a step amount.
  (b) KNOWN:   every clause identifier used exists in the rules text for that year (RULES_FY<YYYY_YY>.md).
  (c) CORRECT: every step amount equals the value that clause yields in an independent recomputation.

The oracle does not expose its intermediate steps, so (c) recomputes them here, independently of every engine,
from the rules: deduction caps, HRA and standard deduction are written out below, and the slab tax, surcharge
and its marginal relief come from the oracle's own helper functions (_tax_differential, _with_surcharge) and
rate tables, called on the oracle's total income. A step may record either the clause's own amount (for example
the rebate) or the running figure after applying it (the tax after the rebate); both are accepted. For C-HRA and
the standard deductions the income after the deduction is also accepted (it does not depend on order); for the
Chapter VI-A caps only the deduction itself. "input" steps must equal an input amount. Level (c) is reported at
a 0.01-rupee tolerance (strict) and, separately, at Rs 1 (paise truncated in an intermediate step).

Implementations checked: hand-coded (both years, all golden cases), the reviewed compiled artifacts (both
years) and FY 2025-26 raw_v2 (the only raw compile that passes the strict allow-list), and the query-time LLM
engine over the answers already recorded in results/ (no new calls). The parser computes no tax itself (it
hands the hand-coded engine a schema input), so its traceability is the hand-coded engine's.
"""
import glob
import json
import re
from datetime import datetime, timezone
from fractions import Fraction as Fr
from pathlib import Path

from project_a.eval import RESULTS_DIR
from project_a.tests import oracle as O

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
CASES = PKG / "tests" / "cases"
RULES = {"2024-25": PKG / "rules" / "RULES_FY2024_25.md", "2025-26": PKG / "rules" / "RULES_FY2025_26.md"}
GOLDEN = {"2024-25": CASES / "golden.jsonl", "2025-26": CASES / "golden_fy2025_26.jsonl"}
TOL = Fr(1, 100)


def clause_ids(fy):
    return set(re.findall(r"\bC-[A-Z0-9]+(?:-[A-Z0-9]+)*\b", RULES[fy].read_text(encoding="utf-8")))


def _f(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float, Fr)):
        return Fr(v) if not isinstance(v, float) else Fr(str(v))
    if isinstance(v, str):
        try:
            return Fr(v)                                  # "333311/2" from the engine trace, or "123.5"
        except (ValueError, ZeroDivisionError):
            return None
    return None


def reference(inp):
    """Per regime: {clause: set of acceptable amounts}, from the rules, independent of the engines."""
    d = dict(O.DEFAULTS)
    d.update(inp)
    fy, age = d["fy"], d["age"]
    gross, basic = Fr(d["gross_salary"]), Fr(d["basic_salary"])
    out = {"old": {}, "new": {}}
    expected = O.oracle(inp)

    # old regime
    pct = Fr(50, 100) if d["metro"] else Fr(40, 100)
    hra = max(Fr(0), min(Fr(d["hra_received"]), pct * basic, Fr(d["rent_paid"]) - basic / 10))
    std = min(Fr(50_000), gross - hra)
    caps = {"C-24B": min(d["home_loan_interest"], 200_000), "C-80C": min(d["sec_80c"], 150_000),
            "C-80CCD1B": min(d["sec_80ccd1b"], 50_000),
            "C-80D-SELF": min(d["sec_80d_self"], 50_000 if age >= 60 else 25_000),
            "C-80D-PARENTS": min(d["sec_80d_parents"], 50_000 if d["parents_senior"] else 25_000)}
    ti = expected["old_total_income"]
    steps = O.OLD_STEPS["super"] if age >= 80 else O.OLD_STEPS["senior"] if age >= 60 else O.OLD_STEPS["below_60"]
    o = out["old"]
    o["C-HRA"] = {hra, gross - hra}
    o["C-STD-OLD"] = {std, gross - hra - std}
    o.update({k: {Fr(v)} for k, v in caps.items()})
    _tax_chain(o, ti, steps, O.OLD_SURCHARGE, (500_000, 12_500, False), expected["old_tax"], "OLD")

    # new regime
    new_steps, limit, rmax = O.NEW_REGIME[fy]
    n = out["new"]
    n["C-STD-NEW"] = {min(Fr(75_000), gross), gross - min(Fr(75_000), gross)}
    _tax_chain(n, expected["new_total_income"], new_steps, O.NEW_SURCHARGE, (limit, rmax, True),
               expected["new_tax"], "NEW")
    inputs = {Fr(v) for k, v in d.items() if isinstance(v, int) and not isinstance(v, bool)}
    return out, inputs, expected


def _tax_chain(ref, ti, steps, bands, rebate_rule, final, tag):
    limit, rmax, relief_allowed = rebate_rule
    slab = O._tax_differential(ti, steps)
    rebate = min(slab, Fr(rmax)) if ti <= limit else Fr(0)
    relief = max(Fr(0), slab - (ti - limit)) if (relief_allowed and ti > limit) else Fr(0)
    after = slab - rebate - relief
    with_sur = O._with_surcharge(after, ti, bands, steps)
    rate = 0
    for threshold, r in bands:
        if ti > threshold:
            rate = r
    uncapped = after * Fr(rate, 100)
    sur = with_sur - after
    ref["C-288A"] = {Fr(ti)}
    ref["C-SLAB-" + tag] = {slab}
    ref["C-87A-" + tag] = {rebate, after, rebate + relief}
    if relief_allowed:
        ref["C-87A-NEW-MR"] = {relief, after}
    ref["C-SUR"] = {uncapped, sur, with_sur}
    ref["C-SUR-MR"] = {uncapped - sur, with_sur}
    ref["C-CESS"] = {with_sur * Fr(4, 100), with_sur * Fr(104, 100)}
    ref["C-288B"] = {Fr(final)}


def _match(value, accepted, tol=TOL):
    v = _f(value)
    return v is not None and any(abs(v - a) <= tol for a in accepted)


def check(inp, reported, steps, regime_known):
    """steps: list of (regime or None, clause, amount). Returns dict(a, b, c, steps, bad)."""
    fy = inp["fy"]
    known = clause_ids(fy)
    ref, inputs, _ = reference(inp)
    tagged = all(isinstance(c, str) and c.strip() for _, c, _ in steps) and bool(steps)
    amounts = [_f(a) for _, _, a in steps]
    reported_ok = all(_f(v) is not None and any(a is not None and abs(a - _f(v)) <= TOL for a in amounts)
                      for v in reported)
    ids_ok = all(c == "input" or c in known for _, c, _ in steps)
    bad, bad_1 = [], 0
    for regime, clause, amount in steps:
        if clause == "input":
            accepted = inputs
        else:
            pools = [ref[regime]] if (regime_known and regime in ref) else list(ref.values())
            accepted = set().union(*[p.get(clause, set()) for p in pools])
        if not (accepted and _match(amount, accepted)):
            bad.append({"regime": regime, "clause": clause, "amount": str(amount)})
        if not (accepted and _match(amount, accepted, Fr(1))):
            bad_1 += 1
    return {"a": tagged and reported_ok, "a_tagged": tagged, "a_reported": reported_ok, "b": ids_ok,
            "c": not bad and bool(steps), "c_within_1": bad_1 == 0 and bool(steps),
            "steps": len(steps), "bad_steps": len(bad), "bad_steps_1": bad_1, "bad": bad[:6]}


# ---- collectors -----------------------------------------------------------------------------------
def _jsonl(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def hand_coded_items():
    from project_a.core.schema import TaxInput
    from project_a.engines.hand_coded import HandCodedEngine
    e = HandCodedEngine()
    for fy, path in GOLDEN.items():
        for c in _jsonl(path):
            r = e.calculate(TaxInput.from_dict(c["input"]))
            steps = [(reg.regime, t.clause, t.amount) for reg in (r.old, r.new) for t in reg.trace]
            yield fy, c, (r.old.total_income, r.old.tax, r.new.total_income, r.new.tax), steps, True


def compiled_items(artifact, compiled_dir, fy):
    from project_a.core.schema import TaxInput
    from project_a.engines.llm_compiled import LLMCompiledEngine
    e = LLMCompiledEngine(artifact, compiled_dir)
    try:
        for c in _jsonl(GOLDEN[fy]):
            r = e.calculate(TaxInput.from_dict(c["input"]))
            steps = [(None, s[0], s[1]) for s in (r.steps or []) if isinstance(s, (list, tuple)) and len(s) == 2]
            yield fy, c, (r.old.total_income, r.old.tax, r.new.total_income, r.new.tax), steps, False
    finally:
        e.close()


QUERY_FILES = ("llm_query_dev_n52_r1_*_*.json", "llm_query_heldout_n156_r1_*_*.json",
               "llm_query_fy2025_26-all_n173_r1_*_*.json")


def query_items():
    golden = {fy: {c["id"]: c for c in _jsonl(p)} for fy, p in GOLDEN.items()}
    for pattern in QUERY_FILES:
        files = sorted(glob.glob(str(RESULTS_DIR / pattern)))
        if not files:
            continue
        data = json.loads(Path(files[-1]).read_text())
        fy = "2025-26" if "fy2025_26" in pattern else "2024-25"
        for row in data["rows"]:
            c = golden[fy][row["id"]]
            got = row["got"]
            trace = row.get("llm_trace") or {}
            steps = [(reg, s.get("clause"), s.get("amount")) for reg in ("old", "new") for s in trace.get(reg, [])
                     if isinstance(s, dict)]
            yield fy, c, (got["old_total_income"], got["old_tax"], got["new_total_income"], got["new_tax"]), steps, True


def summarise(items):
    rows = []
    for fy, c, reported, steps, regime_known in items:
        res = check(c["input"], reported, steps, regime_known)
        rows.append({"id": c["id"], "fy": fy, "split": c.get("split"), **res})
    n = len(rows)
    steps = sum(r["steps"] for r in rows)
    return {"cases": n, "a": sum(r["a"] for r in rows), "b": sum(r["b"] for r in rows), "c": sum(r["c"] for r in rows),
            "c_within_1": sum(r["c_within_1"] for r in rows), "bad_steps_1": sum(r["bad_steps_1"] for r in rows),
            "a_tagged": sum(r["a_tagged"] for r in rows), "a_reported": sum(r["a_reported"] for r in rows),
            "steps": steps, "bad_steps": sum(r["bad_steps"] for r in rows), "rows": rows}


def run():
    from project_a.engines.llm_compiled import COMPILED
    impls = {
        "hand_coded": lambda: hand_coded_items(),
        "llm_compiled reviewed FY 2024-25": lambda: compiled_items("reviewed", COMPILED, "2024-25"),
        "llm_compiled reviewed FY 2025-26": lambda: compiled_items("reviewed", COMPILED / "fy2025_26", "2025-26"),
        "llm_compiled raw_v2 FY 2025-26": lambda: compiled_items("raw_v2", COMPILED / "fy2025_26", "2025-26"),
        "llm_query (recorded answers)": lambda: query_items(),
    }
    out = {"definition": __doc__, "implementations": {k: summarise(f()) for k, f in impls.items()}}
    path = RESULTS_DIR / ("traceability_%s.json" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    path.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return path, out


if __name__ == "__main__":
    path, out = run()
    for name, s in out["implementations"].items():
        print("%-34s cases %3d | (a) %3d | (b) %3d | (c) strict %3d, within Rs 1 %3d | steps %d, wrong %d (%d at Rs 1)"
              % (name, s["cases"], s["a"], s["b"], s["c"], s["c_within_1"], s["steps"], s["bad_steps"], s["bad_steps_1"]))
    print("wrote", path)
