"""Independent oracle for FY 2024-25 and FY 2025-26 salaried tax.

FY 2025-26 (added for Task 5) was written from rules/RULES_FY2025_26.md only, not from the engine or the
parameter file: the new-regime rate steps and the 87A limit/maximum below, everything else unchanged.

Deliberately formulated differently from the engine so a shared mistake is less likely:
  * slab tax in "differential rate" form: sum of (rate step) x (income above threshold)
  * Decimal rounding (ROUND_HALF_UP) instead of integer divmod
  * every constant is written inline here; it does NOT read the parameter file
  * marginal relief written as the statute states it: min(uncapped, tax at threshold + excess)

Limit of independence (be honest about it): one author wrote the engine and this oracle in the
same sitting. The official-calculator checks are the real independent test (risk R1, R6).
"""
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction as Fr

DEFAULTS = dict(
    age=30, gross_salary=0, basic_salary=0, hra_received=0, rent_paid=0, metro=False,
    sec_80c=0, sec_80ccd1b=0, sec_80d_self=0, sec_80d_parents=0, parents_senior=False,
    home_loan_interest=0,
)

# (threshold, rate in percent that applies ABOVE the threshold). Rate below the first one is 0.
OLD_STEPS = {
    "below_60": [(250_000, 5), (500_000, 20), (1_000_000, 30)],
    "senior": [(300_000, 5), (500_000, 20), (1_000_000, 30)],
    "super": [(500_000, 20), (1_000_000, 30)],
}
NEW_STEPS = [(300_000, 5), (700_000, 10), (1_000_000, 15), (1_200_000, 20), (1_500_000, 30)]
# FY 2025-26, from RULES_FY2025_26.md C-SLAB-NEW: nil to 4L, then 5/10/15/20/25/30 above 4/8/12/16/20/24L.
NEW_STEPS_FY26 = [(400_000, 5), (800_000, 10), (1_200_000, 15), (1_600_000, 20), (2_000_000, 25), (2_400_000, 30)]
# New-regime 87A per year: (income limit, maximum rebate); relief above the limit caps tax at the excess.
NEW_REGIME = {"2024-25": (NEW_STEPS, 700_000, 25_000),
              "2025-26": (NEW_STEPS_FY26, 1_200_000, 60_000)}    # C-87A-NEW / C-87A-NEW-MR (FY 2025-26)

# (income above which, surcharge percent)
OLD_SURCHARGE = [(5_000_000, 10), (10_000_000, 15), (20_000_000, 25), (50_000_000, 37)]
NEW_SURCHARGE = [(5_000_000, 10), (10_000_000, 15), (20_000_000, 25)]


def _tax_differential(income, steps):
    total, previous = Fr(0), 0
    for threshold, rate in steps:
        total += Fr(rate - previous, 100) * max(0, income - threshold)
        previous = rate
    return total


def _round10(x):
    rupees = int(x)                     # paise ignored; x >= 0
    return int((Decimal(rupees) / 10).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * 10


def _with_surcharge(tax, income, bands, steps):
    rate, prev_rate, hit = 0, 0, None
    for threshold, r in bands:
        if income > threshold:
            prev_rate, rate, hit = rate, r, threshold
    if hit is None:
        return tax
    uncapped = tax * (1 + Fr(rate, 100))
    limit = _tax_differential(hit, steps) * (1 + Fr(prev_rate, 100)) + (income - hit)
    return min(uncapped, limit)


def oracle(inp):
    d = dict(DEFAULTS)
    d.update(inp)
    assert d["fy"] in NEW_REGIME, "oracle only knows FY %s" % ", ".join(NEW_REGIME)
    new_steps, new_limit, new_max = NEW_REGIME[d["fy"]]
    gross, basic = Fr(d["gross_salary"]), Fr(d["basic_salary"])
    age = d["age"]

    # ---- old regime ----
    metro_cap = Fr(1, 2) if d["metro"] else Fr(2, 5)
    hra = max(Fr(0), min(Fr(d["hra_received"]), metro_cap * basic, Fr(d["rent_paid"]) - basic / 10))
    std_old = min(Fr(50_000), gross - hra)
    cap_self = 50_000 if age >= 60 else 25_000
    cap_par = 50_000 if d["parents_senior"] else 25_000
    deductions = (
        min(d["home_loan_interest"], 200_000) + min(d["sec_80c"], 150_000)
        + min(d["sec_80ccd1b"], 50_000) + min(d["sec_80d_self"], cap_self)
        + min(d["sec_80d_parents"], cap_par)
    )
    ti_old = _round10(max(Fr(0), gross - hra - std_old - deductions))
    steps = OLD_STEPS["super"] if age >= 80 else OLD_STEPS["senior"] if age >= 60 else OLD_STEPS["below_60"]
    tax = _tax_differential(ti_old, steps)
    if ti_old <= 500_000:
        tax -= min(tax, Fr(12_500))
    total = _with_surcharge(tax, ti_old, OLD_SURCHARGE, steps) * Fr(104, 100)
    tax_old = _round10(total)

    # ---- new regime ----
    ti_new = _round10(max(Fr(0), gross - min(Fr(75_000), gross)))
    tax = _tax_differential(ti_new, new_steps)
    if ti_new <= new_limit:
        tax -= min(tax, Fr(new_max))
    else:
        tax = min(tax, Fr(ti_new - new_limit))
    total = _with_surcharge(tax, ti_new, NEW_SURCHARGE, new_steps) * Fr(104, 100)
    tax_new = _round10(total)

    return {
        "old_total_income": ti_old, "old_tax": tax_old,
        "new_total_income": ti_new, "new_tax": tax_new,
        "best": "new" if tax_new <= tax_old else "old",
    }
