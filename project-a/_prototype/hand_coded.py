"""Implementation (a): hand-coded rules for FY 2024-25 (AY 2025-26).

Every number is tagged with a clause ID from RULES_FY2024_25.md.
Exact arithmetic (Fraction) so rupee-level rounding never drifts.
"""
from fractions import Fraction as F
from math import floor

from schema import TaxInput, TraceStep, RegimeResult, TaxResult

# ---- rule data (config), each tied to a clause ID -------------------------

STD_OLD = 50_000            # C-STD-OLD
STD_NEW = 75_000            # C-STD-NEW
LIMIT_24B = 200_000         # C-24B
LIMIT_80C = 150_000         # C-80C
LIMIT_80CCD1B = 50_000      # C-80CCD1B
LIMIT_80D_NORMAL = 25_000   # C-80D-SELF / C-80D-PARENTS
LIMIT_80D_SENIOR = 50_000
CESS = F(4, 100)            # C-CESS

# (upper limit of slab, rate); None = no upper limit
SLABS_OLD = {
    "below60": [(250_000, F(0)), (500_000, F(5, 100)), (1_000_000, F(20, 100)), (None, F(30, 100))],
    "senior": [(300_000, F(0)), (500_000, F(5, 100)), (1_000_000, F(20, 100)), (None, F(30, 100))],
    "super": [(500_000, F(0)), (1_000_000, F(20, 100)), (None, F(30, 100))],
}
SLABS_NEW = [
    (300_000, F(0)), (700_000, F(5, 100)), (1_000_000, F(10, 100)),
    (1_200_000, F(15, 100)), (1_500_000, F(20, 100)), (None, F(30, 100)),
]
# C-SUR: (income above this, rate)
SURCHARGE_OLD = [(5_000_000, F(10, 100)), (10_000_000, F(15, 100)),
                 (20_000_000, F(25, 100)), (50_000_000, F(37, 100))]
SURCHARGE_NEW = [(5_000_000, F(10, 100)), (10_000_000, F(15, 100)),
                 (20_000_000, F(25, 100))]  # capped at 25% above 5 crore


def round10(x):
    """C-288A / C-288B: nearest Rs 10, a last digit of 5 or more rounds up."""
    return int(floor((F(x) + 5) / 10) * 10)


def slab_tax(income, slabs):
    tax, lower = F(0), 0
    for upper, rate in slabs:
        if income <= lower:
            break
        top = income if upper is None else min(income, upper)
        tax += (top - lower) * rate
        lower = upper if upper is not None else lower
        if upper is None:
            break
    return tax


def surcharge_amount(tax, income, bands, slabs):
    """C-SUR with C-SUR-MR marginal relief. Returns the surcharge in rupees."""
    band_i = -1
    for i, (th, _) in enumerate(bands):
        if income > th:
            band_i = i
    if band_i < 0:
        return F(0)
    th, rate = bands[band_i]
    prev_rate = bands[band_i - 1][1] if band_i > 0 else F(0)
    total = tax * (1 + rate)
    cap = slab_tax(th, slabs) * (1 + prev_rate) + (income - th)
    if total > cap:
        total = cap
    return total - tax


def old_slab_table(age):
    if age >= 80:
        return SLABS_OLD["super"]
    if age >= 60:
        return SLABS_OLD["senior"]
    return SLABS_OLD["below60"]


def compute_old(i: TaxInput) -> RegimeResult:
    r = RegimeResult(regime="old")
    t = r.trace
    gross = F(i.gross_salary)
    t.append(TraceStep("gross salary", gross, "input"))

    pct = F(50, 100) if i.metro else F(40, 100)
    hra = min(F(i.hra_received), pct * F(i.basic_salary), F(i.rent_paid) - F(i.basic_salary) / 10)
    hra = max(F(0), hra)
    t.append(TraceStep("HRA exempt", hra, "C-HRA"))

    std = min(F(STD_OLD), gross)
    t.append(TraceStep("standard deduction", std, "C-STD-OLD"))

    hl = min(F(i.home_loan_interest), F(LIMIT_24B))
    t.append(TraceStep("home-loan interest", hl, "C-24B"))

    c80c = min(F(i.sec_80c), F(LIMIT_80C))
    c1b = min(F(i.sec_80ccd1b), F(LIMIT_80CCD1B))
    cap_self = LIMIT_80D_SENIOR if i.age >= 60 else LIMIT_80D_NORMAL
    cap_par = LIMIT_80D_SENIOR if i.parents_senior else LIMIT_80D_NORMAL
    d_self = min(F(i.sec_80d_self), F(cap_self))
    d_par = min(F(i.sec_80d_parents), F(cap_par))
    t.append(TraceStep("80C", c80c, "C-80C"))
    t.append(TraceStep("80CCD(1B)", c1b, "C-80CCD1B"))
    t.append(TraceStep("80D self/family", d_self, "C-80D-SELF"))
    t.append(TraceStep("80D parents", d_par, "C-80D-PARENTS"))

    raw = gross - hra - std - hl - c80c - c1b - d_self - d_par
    ti = round10(max(F(0), raw))
    r.total_income = ti
    t.append(TraceStep("total income", ti, "C-288A"))

    slabs = old_slab_table(i.age)
    tax = slab_tax(ti, slabs)
    t.append(TraceStep("slab tax", tax, "C-SLAB-OLD"))

    if ti <= 500_000:
        rebate = min(tax, F(12_500))
        tax -= rebate
        t.append(TraceStep("87A rebate", rebate, "C-87A-OLD"))

    sur = surcharge_amount(tax, ti, SURCHARGE_OLD, slabs)
    t.append(TraceStep("surcharge", sur, "C-SUR"))
    cess = (tax + sur) * CESS
    t.append(TraceStep("cess", cess, "C-CESS"))
    r.tax = round10(tax + sur + cess)
    t.append(TraceStep("tax payable", r.tax, "C-288B"))
    return r


def compute_new(i: TaxInput) -> RegimeResult:
    r = RegimeResult(regime="new")
    t = r.trace
    gross = F(i.gross_salary)
    t.append(TraceStep("gross salary", gross, "input"))
    std = min(F(STD_NEW), gross)
    t.append(TraceStep("standard deduction", std, "C-STD-NEW"))

    ti = round10(max(F(0), gross - std))
    r.total_income = ti
    t.append(TraceStep("total income", ti, "C-288A"))

    tax = slab_tax(ti, SLABS_NEW)
    t.append(TraceStep("slab tax", tax, "C-SLAB-NEW"))

    if ti <= 700_000:
        rebate = min(tax, F(25_000))
        tax -= rebate
        t.append(TraceStep("87A rebate", rebate, "C-87A-NEW"))
    elif tax > ti - 700_000:
        relief = tax - (ti - 700_000)
        tax -= relief
        t.append(TraceStep("87A marginal relief", relief, "C-87A-NEW-MR"))

    sur = surcharge_amount(tax, ti, SURCHARGE_NEW, SLABS_NEW)
    t.append(TraceStep("surcharge", sur, "C-SUR"))
    cess = (tax + sur) * CESS
    t.append(TraceStep("cess", cess, "C-CESS"))
    r.tax = round10(tax + sur + cess)
    t.append(TraceStep("tax payable", r.tax, "C-288B"))
    return r


def calculate(i: TaxInput) -> TaxResult:
    old, new = compute_old(i), compute_new(i)
    best = "new" if new.tax <= old.tax else "old"
    saving = abs(old.tax - new.tax)
    if old.tax == new.tax:
        reason = f"Both regimes give Rs {new.tax}; new regime chosen as the default."
    else:
        lo, hi = (new, old) if best == "new" else (old, new)
        reason = (f"{best.capitalize()} regime is cheaper: Rs {lo.tax} vs Rs {hi.tax} "
                  f"(saves Rs {saving}).")
    return TaxResult(old=old, new=new, best=best, saving=saving, reason=reason)
