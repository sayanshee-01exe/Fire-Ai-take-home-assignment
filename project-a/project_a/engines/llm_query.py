"""Implementation (b): the LLM computes the tax at query time from the rules text.

The model gets rules/RULES_FY2024_25.md and the input case and must answer with one JSON object.
This engine reports exactly what the model said. It never repairs, rounds, recomputes or fixes
the model's numbers, so the comparison with the hand-coded engine is honest. Only `best` and
`saving` are derived here, from the model's two tax figures, because TaxResult needs them.
Unparseable or incomplete output gives an error result that keeps the model's raw text.
"""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from project_a.core.errors import InvalidInput, UnknownRuleVersion
from project_a.core.params import load_params
from project_a.core.schema import RegimeResult, TaxInput, TaxResult, validate_input
from project_a.llm.client import GeminiClient

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"
RULES_FILES = {"2024-25": "RULES_FY2024_25.md", "2025-26": "RULES_FY2025_26.md"}

FIELD_MEANINGS = """\
Input fields (all money is annual, in whole rupees):
- fy: financial year. age: age in years on 31 March of that year.
- gross_salary: total salary, including HRA received and all allowances.
- basic_salary: basic + dearness allowance (the "salary" used for the HRA rule).
- hra_received: HRA received from the employer. rent_paid: rent paid in the year.
- metro: true if the rented home is in Delhi, Mumbai, Kolkata or Chennai.
- sec_80c, sec_80ccd1b: amounts invested (before applying the limits).
- sec_80d_self: health-insurance premium for self, spouse and children. sec_80d_parents: for parents.
- parents_senior: true if a parent is 60 or older.
- home_loan_interest: interest on a loan for a self-occupied house (before the limit)."""

OUTPUT_FORMAT = """\
Respond with exactly one JSON object and nothing else, in this shape:
{
  "old_regime": {"taxable_income": <integer rupees>, "tax": <integer rupees>,
                 "trace": [{"step": "<what was done>", "amount": <number>, "clause": "<clause ID>"}, ...]},
  "new_regime": {"taxable_income": <integer rupees>, "tax": <integer rupees>,
                 "trace": [...]}
}
"taxable_income" is total income after rounding (C-288A). "tax" is the final tax payable after rebate,
surcharge, cess and rounding (C-288B). Keep each trace short: one entry per step, citing the clause ID."""


def build_system(fy):
    rules = (RULES_DIR / RULES_FILES[fy]).read_text(encoding="utf-8")
    return ("You are an Indian income-tax calculator for salaried resident individuals. "
            "Apply ONLY the rules below, exactly as written, to compute tax under both regimes.\n\n"
            + rules + "\n\n" + FIELD_MEANINGS + "\n\n" + OUTPUT_FORMAT)


def build_prompt(inp):
    return ("Compute the tax for this case:\n"
            + json.dumps(asdict(inp), sort_keys=True, ensure_ascii=False))


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_output(text):
    """Strict parse. Returns (data, problems). No fence stripping, no number coercion."""
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, ["not valid JSON: %s" % e]
    if not isinstance(data, dict):
        return None, ["top level is %s, not an object" % type(data).__name__]
    problems = []
    for regime in ("old_regime", "new_regime"):
        r = data.get(regime)
        if not isinstance(r, dict):
            problems.append("missing object %s" % regime)
            continue
        for k in ("taxable_income", "tax"):
            if k not in r:
                problems.append("%s.%s missing" % (regime, k))
            elif not _is_number(r[k]):
                problems.append("%s.%s is %r, not a number" % (regime, k, r[k]))
        trace = r.get("trace")
        if not isinstance(trace, list) or not all(isinstance(s, dict) and isinstance(s.get("step"), str)
                                                  for s in trace):
            problems.append("%s.trace must be a list of objects with a 'step' string" % regime)
    return (data if not problems else None), problems


@dataclass
class LLMTaxResult(TaxResult):
    """A TaxResult plus what is needed to audit the LLM: raw output, trace, usage, error."""
    error: Optional[str] = None
    raw_output: str = ""
    llm_trace: Dict[str, Any] = field(default_factory=dict)
    usage: Dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0
    cached: bool = False
    model: str = ""

    def to_dict(self):
        d = super().to_dict()
        d.update(error=self.error, raw_output=self.raw_output, llm_trace=self.llm_trace,
                 usage=self.usage, latency_s=self.latency_s, cached=self.cached, model=self.model)
        return d


class LLMQueryEngine:
    name = "llm_query"

    def __init__(self, client=None, run_index=0):
        self.client = client or GeminiClient()
        self.run_index = run_index

    def calculate(self, inp: TaxInput) -> LLMTaxResult:
        errors = validate_input(inp)
        if errors:
            raise InvalidInput(errors)
        if inp.fy not in RULES_FILES:
            raise UnknownRuleVersion("no rules text for FY %s" % inp.fy)
        rule_version = load_params(inp.fy)["rule_version"]
        resp = self.client.generate(build_prompt(inp), system=build_system(inp.fy),
                                    json_mode=True, run_index=self.run_index)
        common = dict(rule_version=rule_version, engine=self.name, raw_output=resp.text,
                      usage={"prompt_tokens": resp.prompt_tokens, "output_tokens": resp.output_tokens,
                             "thoughts_tokens": resp.thoughts_tokens},
                      latency_s=resp.latency_s, cached=resp.cached, model=self.client.model)

        data, problems = parse_output(resp.text)
        if problems:
            return LLMTaxResult(
                old=RegimeResult("old", None, None), new=RegimeResult("new", None, None),
                best=None, saving=None, reason="LLM output rejected: " + "; ".join(problems),
                error="; ".join(problems) + (" (finish reason %s)" % resp.finish_reason
                                             if resp.finish_reason not in ("", "STOP") else ""),
                **common)

        old_r, new_r = data["old_regime"], data["new_regime"]
        old = RegimeResult("old", old_r["taxable_income"], old_r["tax"])     # exactly as the model said
        new = RegimeResult("new", new_r["taxable_income"], new_r["tax"])
        best = "new" if new.tax <= old.tax else "old"
        return LLMTaxResult(
            old=old, new=new, best=best, saving=abs(old.tax - new.tax),
            reason="As reported by the model: old %s, new %s." % (old.tax, new.tax),
            assumptions=["best and saving derived in code from the model's two tax figures"],
            llm_trace={"old": old_r["trace"], "new": new_r["trace"]}, **common)
