"""The CLI's steps as data instead of printed text, for the demo UI.

Uses only the existing pipeline: pipeline.parse (one LLM extraction call), pipeline.answer (no LLM call),
and cli.assumption_note. Nothing here computes tax; the hand-coded engine does, inside the pipeline.

LLM modes:
  replay: answered from llm_cache/ only; a query that was never recorded raises CacheMiss (no network).
  live:   calls Gemini. The reply is recorded in ui/live_cache/, never in llm_cache/, so the committed
          recordings that reproduce.py replays are never overwritten.
"""
import dataclasses
import json
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path

from project_a.cli import assumption_note
from project_a.engines.hand_coded import HandCodedEngine
from project_a.llm.client import CacheMiss, GeminiClient, LLMError, QuotaExhausted
from project_a.llm.config import get_config
from project_a.parser.pipeline import answer, parse
from project_a.tests.official.make_sheet import indian

ROOT = Path(__file__).resolve().parents[1]
LIVE_CACHE = Path(__file__).resolve().parent / "live_cache"
MAX_ROUNDS = 5                                  # the CLI's --interactive loop stops after 5 rounds too
ENGINE_BADGE = "Tax computed by: hand-coded rules (no LLM)"

DEMO_QUERIES = [
    ("Demo: HRA, details missing (asks)",
     "FY 2024-25: my salary is 12 lakh. I pay 25,000 monthly rent and receive HRA. Age 30."),
    ("Demo: 12 lakh, no deductions",
     "FY 2024-25: my annual salary is 12 lakh. I don't pay rent, have no home loan, and don't claim any deductions. Age 30."),
    ("Demo: CLI example (Hinglish)", "Main 30 ka hoon, FY 2024-25 mein 65k per month salary"),
]
NL_EXAMPLES = [("NL set, Hinglish: NL-14", "NL-14"), ("NL set, FY 2025-26: NL-49", "NL-49")]


def examples():
    """[(label, text)]: the three demo queries, then two queries read verbatim from the frozen NL set."""
    rows = {}
    for line in (ROOT / "project_a" / "tests" / "nl" / "queries.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["id"]] = r["text"]
    return DEMO_QUERIES + [(label, rows[i]) for label, i in NL_EXAMPLES]


def client_for(mode):
    if mode not in ("replay", "live"):
        raise ValueError("mode must be replay or live, got %r" % mode)
    cfg = dataclasses.replace(get_config("parser"), mode=mode)
    if mode == "live":
        cfg = dataclasses.replace(cfg, cache_dir=LIVE_CACHE)
    return GeminiClient(config=cfg)


class RecordingEngine(HandCodedEngine):
    """The hand-coded engine, unchanged, keeping the TaxResult (with its clause trace) that the pipeline
    discards. The pipeline calls it once; the UI never calls the engine itself."""
    last = None

    def calculate(self, inp):
        self.last = super().calculate(inp)
        return self.last


def run_query(text, mode="replay"):
    """(ParseResult or None, steps dict). Errors are returned in the steps, never raised."""
    engine = RecordingEngine()
    try:
        pr = parse(text, client=client_for(mode), engine=engine)
    except CacheMiss as e:
        return None, error_steps("This query is not in the replay cache: %s" % e,
                                 "Pick one of the example queries, or switch to live mode.")
    except QuotaExhausted as e:
        return None, error_steps("The model's quota is exhausted: %s" % e, "Switch to replay mode.")
    except LLMError as e:                       # missing key, network error, HTTP error (messages are redacted)
        return None, error_steps("The live LLM call failed: %s" % e, "Switch to replay mode.")
    return pr, steps(pr, engine.last)


def submit_answers(pr, answers):
    """Exactly what the CLI's --interactive loop does: answer(pr, {question id: typed text}). No LLM call."""
    engine = RecordingEngine()
    pr = answer(pr, answers, engine=engine)
    return pr, steps(pr, engine.last)


def error_steps(message, hint):
    return {"status": "error", "error": message, "hint": hint, "extraction": None, "normalised_input": None,
            "decisions": [], "questions": [], "unsupported": [], "result": None, "llm": {}}


def steps(pr, tax_result=None):
    """The CLI's show(pr), as data, in the same order, plus the breakup and assumptions on a compute."""
    s = {"status": pr.status, "error": None, "hint": None,
         "extraction": asdict(pr.extraction) if pr.extraction is not None else None,
         "normalised_input": pr.normalised_input, "decisions": list(pr.trace),
         "questions": pr.questions, "unsupported": pr.unsupported_features, "result": None, "llm": pr.llm}
    if pr.status == "parse_error":
        s.update(status="error", error="The model's reply could not be parsed. Raw text:\n%s" % pr.raw_output,
                 hint="Try again, or rephrase the question.")
    if pr.status == "compute":
        r, ni = pr.result, pr.normalised_input
        s["result"] = {
            "fy": ni["fy"],
            "rows": [{"Regime": "Old", "Taxable income (Rs)": indian(r["old_total_income"]), "Tax (Rs)": indian(r["old_tax"])},
                     {"Regime": "New", "Taxable income (Rs)": indian(r["new_total_income"]), "Tax (Rs)": indian(r["new_tax"])}],
            "old_tax": r["old_tax"], "new_tax": r["new_tax"],
            "better": "Better: %s regime (saves Rs %s). %s" % (r["best"], indian(r["saving"]), r["reason"]),
            "assumption": assumption_note(ni), "badge": ENGINE_BADGE, "rule_version": r["rule_version"]}
        if tax_result is not None and (tax_result.old.tax, tax_result.new.tax) == (r["old_tax"], r["new_tax"]):
            s["result"]["breakup"] = {"old": breakup(tax_result.old, ni), "new": breakup(tax_result.new, ni)}
            s["result"]["assumptions"] = assumptions(ni, tax_result)
        else:                                   # never expected: the recorded result is the pipeline's own
            s["result"]["breakup"] = None
            s["result"]["assumptions"] = assumptions(ni, None)
    return s


# ---- tax breakup, from the engine's clause trace only --------------------------------------------
DEDUCTION_STEPS = (                      # (trace step label, input field, section shown)
    ("home-loan interest", "home_loan_interest", "Section 24(b)"),
    ("80C", "sec_80c", "Section 80C"),
    ("80CCD(1B)", "sec_80ccd1b", "Section 80CCD(1B)"),
    ("80D self and family", "sec_80d_self", "Section 80D (self and family)"),
    ("80D parents", "sec_80d_parents", "Section 80D (parents)"),
)
TAX_SIGN = {"slab tax": 1, "87A rebate": -1, "87A marginal relief": -1, "surcharge before relief": 1,
            "surcharge marginal relief": -1, "health and education cess": 1}
ROUNDING_UNIT_BOUND = 10                 # a genuine C-288A/C-288B rounding moves the amount by less than Rs 10


def money(x):
    """Display only: whole rupees in Indian grouping, paise shown when the trace amount has them."""
    x = Fraction(x)
    neg, x = x < 0, abs(x)
    whole, frac = divmod(x, 1)
    text = indian(int(whole)) + (".%02d" % round(frac * 100) if frac else "")
    return ("-" if neg else "") + "Rs " + text


def _line(label, clause, amount=None, sign=0, kind="step", note=""):
    return {"label": label, "clause": clause, "amount": None if amount is None else Fraction(amount),
            "sign": sign, "kind": kind, "note": note,
            "shown": "" if amount is None else ("%s%s" % ({1: "+ ", -1: "- ", 0: ""}[sign], money(amount)))}


def breakup(regime, ni):
    """Lines for one regime, in the order: gross, standard deduction, HRA, other deductions, taxable income,
    slab tax, rebate/relief, surcharge, cess, rounding, final tax. Every amount is a trace step's amount;
    the two rounding lines are the gap between trace steps, because the trace has no rounding step.
    kind: step (from the trace), subtotal (from the trace, not added), derived (gap), missing (not in trace)."""
    by = {}
    for t in regime.trace:
        by.setdefault(t.step, t)
    lines, missing = [], []
    gross, std, ti, payable = by["gross salary"], by["standard deduction"], by["total income"], by["tax payable"]
    lines.append(_line("Gross salary", gross.clause, gross.amount, 1))
    lines.append(_line("Standard deduction", std.clause, std.amount, -1))
    hra_given = ni.get("hra_received", 0) > 0 or ni.get("rent_paid", 0) > 0
    if regime.regime == "old":
        if "HRA exempt" in by:
            h = by["HRA exempt"]
            lines.append(_line("HRA exemption (the least of the three amounts)", h.clause, h.amount, -1))
        elif hra_given:
            lines.append(_line("HRA exemption", "C-HRA", kind="missing",
                               note="no C-HRA step in the trace: the engine records it only when the exemption is above 0"))
        if hra_given:
            part = ("HRA received, %d%% of basic, rent minus 10%% of basic, and which one was the minimum"
                    % (50 if ni.get("metro") else 40))
            lines.append(_line(part, "C-HRA", kind="missing",
                               note="not in the trace: it records only the final exemption"))
            missing.append("HRA: the three amounts and the minimum")
        for step, _field, section in DEDUCTION_STEPS:
            if step in by:
                lines.append(_line("%s: %s" % (section, step), by[step].clause, by[step].amount, -1))
    income_steps = [l for l in lines if l["kind"] == "step"]
    before_round = sum(l["sign"] * l["amount"] for l in income_steps)
    if before_round < 0 and ti.amount == 0:
        lines.append(_line("Total income cannot be below 0", "C-288A", -before_round, 1, "floor",
                           "deductions exceed the salary; the trace gives the total as 0"))
    elif ti.amount != before_round:
        lines.append(_line("Rounding of total income", "C-288A", ti.amount - before_round, 1, "derived",
                           "gap between the lines above and the rounded total: the trace has no rounding step"))
    lines.append(_line("Taxable income", ti.clause, ti.amount, 0, "subtotal"))

    slab = by["slab tax"]
    lines.append(_line("Slab tax (all slabs together)", slab.clause, slab.amount, 1))
    lines.append(_line("Amount in each slab and the tax on it", slab.clause, kind="missing",
                       note="not in the trace: it records only the total slab tax"))
    missing.append("slabs: the amount in each slab and the tax on it")
    for step in ("87A rebate", "87A marginal relief"):
        if step in by:
            lines.append(_line("Section %s" % step, by[step].clause, by[step].amount, TAX_SIGN[step]))
    sur_steps = [t for t in regime.trace if t.step in ("surcharge before relief", "surcharge marginal relief")]
    for t in sur_steps:
        lines.append(_line(t.step.capitalize(), t.clause, t.amount, TAX_SIGN[t.step]))
    sp = by["surcharge payable"]
    if sur_steps:
        lines.append(_line("Surcharge payable", sp.clause, sp.amount, 0, "subtotal"))
    else:
        lines.append(_line("Surcharge", sp.clause, sp.amount, 1))
    cess = by["health and education cess"]
    lines.append(_line("Health and education cess", cess.clause, cess.amount, 1))
    tax_steps = [l for l in lines[lines.index(next(l for l in lines if l["label"].startswith("Slab tax"))):]
                 if l["kind"] == "step"]
    before_final = sum(l["sign"] * l["amount"] for l in tax_steps)
    if payable.amount != before_final:
        lines.append(_line("Rounding of tax", "C-288B", payable.amount - before_final, 1, "derived",
                           "gap between the lines above and the rounded tax: the trace has no rounding step"))
    lines.append(_line("Final tax", payable.clause, payable.amount, 0, "total"))
    return {"regime": regime.regime, "lines": lines, "notes": list(regime.notes), "missing": missing,
            "final_tax": regime.tax, "taxable_income": regime.total_income}


def breakup_sums(b):
    """Checks used by ui/check_demo.py: the signed lines add up to the trace's totals, and each derived
    rounding gap is smaller than the rounding unit (otherwise the gap would hide an error)."""
    lines = b["lines"]
    cut = next(i for i, l in enumerate(lines) if l["label"] == "Taxable income")
    inc = sum(l["sign"] * l["amount"] for l in lines[:cut] if l["kind"] in ("step", "derived", "floor"))
    tax = sum(l["sign"] * l["amount"] for l in lines[cut + 1:] if l["kind"] in ("step", "derived"))
    gaps = [abs(l["amount"]) for l in lines if l["kind"] == "derived"]
    sur = {l["label"]: l["amount"] for l in lines}
    sur_ok = ("Surcharge payable" not in sur or
              sur["Surcharge payable"] == sur.get("Surcharge before relief", 0) - sur.get("Surcharge marginal relief", 0))
    return {"income_adds_up": inc == lines[cut]["amount"] == b["taxable_income"],
            "tax_adds_up": tax == lines[-1]["amount"] == b["final_tax"],
            "rounding_gaps_below_unit": all(g < ROUNDING_UNIT_BOUND for g in gaps),
            "surcharge_consistent": sur_ok}


# ---- assumptions, shown only when relevant --------------------------------------------------------
CLOSE_RS = 2_000


def assumptions(ni, tax_result):
    has = lambda f: ni.get(f, 0) > 0
    out = []
    out.append("Gross salary is taken as total annual salary%s." % (" including HRA" if has("hra_received") else ""))
    if has("hra_received"):
        out.append("HRA is computed on the basic salary figure given (DA and commission are not added unless "
                   "you included them in basic).")
    if any(f in ni for f in ("rent_paid", "hra_received", "basic_salary")):
        out.append("Rent, HRA and basic are assumed to apply for the full financial year.")
    stated = [(step, f, sec) for step, f, sec in DEDUCTION_STEPS if has(f)]
    if stated:
        allowed = {t.step: t.amount for t in tax_result.old.trace} if tax_result else {}
        caps = ["%s: stated %s, engine allowed %s" % (sec, money(ni[f]), money(allowed[step]))
                for step, f, sec in stated if step in allowed and allowed[step] < ni[f]]
        out.append("Stated deductions (%s) are assumed eligible and taken as given, capped only where the "
                   "engine caps them%s." % (", ".join(sec for _, _, sec in stated),
                                            ("; capped: " + "; ".join(caps)) if caps else "; none was capped"))
        out.append("No other deductions are assumed.")
    if tax_result is not None and abs(tax_result.old.tax - tax_result.new.tax) <= CLOSE_RS:
        out.append("These are close; a small change in an eligible deduction could change which regime is better.")
    return out
