"""Implementation (a): hand-coded rules.

Every figure from the law comes from the parameter file for the requested FY.
No law constant lives in this file (failure LAW-1). Arithmetic is exact (Fraction).
Each trace step carries a clause ID from rules/RULES_FY2024_25.md.
"""
from fractions import Fraction

from project_a.core.errors import InvalidInput
from project_a.core.params import load_params
from project_a.core.rounding import round_statutory
from project_a.core.schema import RegimeResult, TaxInput, TaxResult, TraceStep, validate_input


def _slab_tax(income, slabs):
    """slabs: [[upper_limit_or_None, percent], ...]; the percent applies up to that limit."""
    tax = Fraction(0)
    lower = 0
    for upper, pct in slabs:
        top = income if upper is None else min(income, upper)
        if top > lower:
            tax += (top - lower) * Fraction(pct, 100)
        if upper is None or income <= upper:
            break
        lower = upper
    return tax


def _surcharge(tax, income, bands, slabs, trace):
    """C-SUR with C-SUR-MR. Relief is taken against the highest threshold crossed.

    Thresholds are 50 lakh and above, where no rebate applies, so the tax at the
    threshold is just the slab tax there.
    """
    crossed = [i for i, (th, _) in enumerate(bands) if income > th]
    if not crossed:
        return Fraction(0)
    i = crossed[-1]
    th, pct = bands[i]
    prev_pct = bands[i - 1][1] if i > 0 else 0
    uncapped_total = tax * (1 + Fraction(pct, 100))
    cap = _slab_tax(th, slabs) * (1 + Fraction(prev_pct, 100)) + (income - th)
    trace.append(TraceStep("surcharge before relief", uncapped_total - tax, "C-SUR"))
    if uncapped_total > cap:
        trace.append(TraceStep("surcharge marginal relief", uncapped_total - cap, "C-SUR-MR"))
        return cap - tax
    return uncapped_total - tax


def _rebate(tax, ti, rule, trace, clause, clause_mr):
    """Section 87A. Returns the tax after rebate or marginal relief."""
    if ti <= rule["limit"]:
        rebate = min(tax, Fraction(rule["max"]))
        if rebate > 0:
            trace.append(TraceStep("87A rebate", rebate, clause))
        return tax - rebate
    if rule["marginal_relief"]:
        allowed = Fraction(ti - rule["limit"])
        if tax > allowed:
            trace.append(TraceStep("87A marginal relief", tax - allowed, clause_mr))
            return allowed
    return tax


def _finish(tax, ti, slabs, bands, p, trace):
    sur = _surcharge(tax, ti, bands, slabs, trace)
    trace.append(TraceStep("surcharge payable", sur, "C-SUR"))
    cess = (tax + sur) * Fraction(p["cess_percent"], 100)
    trace.append(TraceStep("health and education cess", cess, "C-CESS"))
    payable = round_statutory(tax + sur + cess, p["rounding_unit"])
    trace.append(TraceStep("tax payable", payable, "C-288B"))
    return payable


def _age_band(age, bands):
    if age >= bands["super_senior_from"]:
        return "super_senior"
    if age >= bands["senior_from"]:
        return "senior"
    return "below_60"


def _old_regime(inp, p):
    lim = p["limits"]
    trace, notes = [], []
    gross = Fraction(inp.gross_salary)
    trace.append(TraceStep("gross salary", gross, "input"))

    hra_cfg = p["hra"]
    pct = hra_cfg["metro_pct" if inp.metro else "non_metro_pct"]
    basic = Fraction(inp.basic_salary)
    hra_exempt = max(Fraction(0), min(
        Fraction(inp.hra_received),
        basic * pct / 100,
        Fraction(inp.rent_paid) - basic * hra_cfg["rent_excess_over_pct_of_salary"] / 100,
    ))
    if hra_exempt > 0:
        trace.append(TraceStep("HRA exempt", hra_exempt, "C-HRA"))

    std = min(Fraction(p["standard_deduction"]["old"]), gross - hra_exempt)
    trace.append(TraceStep("standard deduction", std, "C-STD-OLD"))

    home_loan = min(Fraction(inp.home_loan_interest), Fraction(lim["sec_24b"]))
    c80c = min(Fraction(inp.sec_80c), Fraction(lim["sec_80c"]))
    c1b = min(Fraction(inp.sec_80ccd1b), Fraction(lim["sec_80ccd1b"]))
    senior_self = inp.age >= p["age_bands"]["senior_from"]
    d_self = min(Fraction(inp.sec_80d_self),
                 Fraction(lim["sec_80d"]["self_senior" if senior_self else "self"]))
    d_par = min(Fraction(inp.sec_80d_parents),
                Fraction(lim["sec_80d"]["parents_senior" if inp.parents_senior else "parents"]))
    for label, amount, clause in (
        ("home-loan interest", home_loan, "C-24B"),
        ("80C", c80c, "C-80C"),
        ("80CCD(1B)", c1b, "C-80CCD1B"),
        ("80D self and family", d_self, "C-80D-SELF"),
        ("80D parents", d_par, "C-80D-PARENTS"),
    ):
        if amount > 0:
            trace.append(TraceStep(label, amount, clause))

    raw = gross - hra_exempt - std - home_loan - c80c - c1b - d_self - d_par
    ti = round_statutory(max(Fraction(0), raw), p["rounding_unit"])
    trace.append(TraceStep("total income", ti, "C-288A"))

    slabs = p["slabs"]["old"][_age_band(inp.age, p["age_bands"])]
    tax = _slab_tax(ti, slabs)
    trace.append(TraceStep("slab tax", tax, "C-SLAB-OLD"))
    tax = _rebate(tax, ti, p["rebate_87a"]["old"], trace, "C-87A-OLD", "C-87A-OLD")
    payable = _finish(tax, ti, slabs, p["surcharge"]["old"], p, trace)
    return RegimeResult("old", ti, payable, trace, notes)


def _new_regime(inp, p):
    trace, notes = [], []
    gross = Fraction(inp.gross_salary)
    trace.append(TraceStep("gross salary", gross, "input"))
    std = min(Fraction(p["standard_deduction"]["new"]), gross)
    trace.append(TraceStep("standard deduction", std, "C-STD-NEW"))

    ti = round_statutory(max(Fraction(0), gross - std), p["rounding_unit"])
    trace.append(TraceStep("total income", ti, "C-288A"))

    # Old-regime items the user supplied are not allowed here: say so (failure ENG-11).
    if inp.hra_received > 0 or inp.rent_paid > 0:
        notes.append("HRA exemption is not available in the new regime (C-HRA)")
    for value, text, clause in (
        (inp.home_loan_interest, "home-loan interest on a self-occupied house", "C-24B"),
        (inp.sec_80c, "80C", "C-80C"),
        (inp.sec_80ccd1b, "80CCD(1B)", "C-80CCD1B"),
        (inp.sec_80d_self + inp.sec_80d_parents, "80D", "C-80D-SELF"),
    ):
        if value > 0:
            notes.append("%s is not allowed in the new regime (%s)" % (text, clause))

    slabs = p["slabs"]["new"]
    tax = _slab_tax(ti, slabs)
    trace.append(TraceStep("slab tax", tax, "C-SLAB-NEW"))
    tax = _rebate(tax, ti, p["rebate_87a"]["new"], trace, "C-87A-NEW", "C-87A-NEW-MR")
    payable = _finish(tax, ti, slabs, p["surcharge"]["new"], p, trace)
    return RegimeResult("new", ti, payable, trace, notes)


class HandCodedEngine:
    name = "hand_coded"

    def calculate(self, inp: TaxInput) -> TaxResult:
        errors = validate_input(inp)
        if errors:
            raise InvalidInput(errors)
        p = load_params(inp.fy)          # raises UnknownRuleVersion for an unknown FY
        old = _old_regime(inp, p)
        new = _new_regime(inp, p)

        best = "new" if new.tax <= old.tax else "old"   # tie goes to the default regime (D7)
        saving = abs(old.tax - new.tax)
        if old.tax == new.tax:
            reason = "Both regimes give Rs %d; the new regime is chosen as the default." % new.tax
        else:
            low, high = (new, old) if best == "new" else (old, new)
            reason = "The %s regime is cheaper: Rs %d against Rs %d, a saving of Rs %d." % (
                best, low.tax, high.tax, saving)
        return TaxResult(old=old, new=new, best=best, saving=saving, reason=reason,
                         rule_version=p["rule_version"], engine=self.name)
