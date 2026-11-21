"""Generates the golden test suite. Run:  python -m project_a.tests.gen_cases

208 cases in 10 categories, expected values from the independent oracle (source="oracle"),
plus 10 hand-worked cases whose expected values are hard-coded (source="hand") and are used
to check the oracle itself. Boundary inputs are derived from the FY 2024-25 thresholds.

Split: every 4th case is "dev" (prompt tuning allowed), the rest "heldout" (report once).
Cases verified on the official calculator get source="official" later (see PLAN.md, R1).
"""
import json
import random
from pathlib import Path

from project_a.tests.oracle import oracle

FY = "2024-25"
STD_OLD, STD_NEW = 50_000, 75_000
CASES_DIR = Path(__file__).resolve().parent / "cases"


def _c(cat, **inp):
    inp.setdefault("age", 30)
    return {"category": cat, "input": dict(fy=FY, **inp)}


def slab_boundaries():
    out = []
    for t in (300_000, 700_000, 1_000_000, 1_200_000, 1_500_000):          # new regime
        for d in (-10, 0, 10):
            out.append(_c("slab_boundary", gross_salary=t + d + STD_NEW))
    for t in (250_000, 500_000, 1_000_000):                                # old, below 60
        for d in (-10, 0, 10):
            out.append(_c("slab_boundary", gross_salary=t + d + STD_OLD))
    for age, thresholds in ((65, (300_000, 500_000)), (82, (500_000, 1_000_000))):
        for t in thresholds:
            for d in (-10, 0, 10):
                out.append(_c("slab_boundary", age=age, gross_salary=t + d + STD_OLD))
    return out                                                              # 36


def rebate_87a():
    out = []
    for age in (30, 65):                                                    # old regime, 5 lakh
        for d in (-10, 0, 10):
            out.append(_c("rebate_87a", age=age, gross_salary=500_000 + d + STD_OLD))
    # new regime, 7 lakh: edge, rounding on the edge, and the marginal-relief zone (ends ~22,222)
    for d in (-10, -5, 0, 5, 10, 50, 5_000, 10_000, 20_000, 22_220, 22_230, 30_000):
        out.append(_c("rebate_87a", gross_salary=700_000 + d + STD_NEW))
    return out                                                              # 18


def surcharge():
    out = []
    for t in (5_000_000, 10_000_000, 20_000_000):                           # new regime
        for d in (0, 10, 50_000, 1_500_000):
            out.append(_c("surcharge", gross_salary=t + d + STD_NEW))
    for t in (5_000_000, 10_000_000, 20_000_000, 50_000_000):               # old regime
        for d in (0, 10, 50_000, 1_500_000):
            out.append(_c("surcharge", gross_salary=t + d + STD_OLD))
    return out                                                              # 28


HRA_ROWS = [  # (basic, hra received, annual rent, metro); gross is always 12 lakh
    (600_000, 100_000, 300_000, False), (600_000, 100_000, 300_000, True),   # HRA received binds
    (400_000, 250_000, 350_000, False), (400_000, 250_000, 350_000, True),   # 40% / 50% of basic binds
    (600_000, 300_000, 120_000, False), (600_000, 300_000, 120_000, True),   # rent minus 10% binds
    (600_000, 200_000, 50_000, False), (600_000, 200_000, 50_000, True),     # rent below 10% of basic
    (600_000, 200_000, 0, False), (600_000, 200_000, 0, True),               # no rent paid
    (600_000, 0, 200_000, False),                                            # no HRA at all
    (500_000, 200_000, 400_000, False),                                      # HRA received == 40% of basic
    (400_001, 200_000, 300_000, False), (333_333, 200_000, 300_000, True),   # fractional percentages
    (300_000, 150_000, 240_000, False), (300_000, 150_000, 240_000, True),
]


def hra():
    return [_c("hra", gross_salary=1_200_000, basic_salary=b, hra_received=h, rent_paid=r, metro=m)
            for b, h, r, m in HRA_ROWS]                                     # 16


def deduction_caps():
    out = []
    base = dict(gross_salary=2_000_000)
    for v in (100_000, 150_000, 300_000):
        out.append(_c("deduction_caps", sec_80c=v, **base))
    for v in (30_000, 50_000, 80_000):
        out.append(_c("deduction_caps", sec_80ccd1b=v, **base))
    for v in (20_000, 25_000, 40_000):
        out.append(_c("deduction_caps", sec_80d_self=v, **base))
    for v in (40_000, 50_000, 70_000):
        out.append(_c("deduction_caps", age=65, sec_80d_self=v, **base))
    for v in (20_000, 25_000, 35_000):
        out.append(_c("deduction_caps", sec_80d_parents=v, parents_senior=False, **base))
    for v in (40_000, 50_000, 70_000):
        out.append(_c("deduction_caps", sec_80d_parents=v, parents_senior=True, **base))
    for v in (150_000, 200_000, 350_000):
        out.append(_c("deduction_caps", home_loan_interest=v, **base))
    out.append(_c("deduction_caps", sec_80c=50_000, sec_80ccd1b=20_000, sec_80d_self=10_000,
                  sec_80d_parents=10_000, home_loan_interest=50_000, **base))
    out.append(_c("deduction_caps", sec_80c=100_000, sec_80ccd1b=30_000, sec_80d_self=20_000,
                  sec_80d_parents=20_000, home_loan_interest=150_000, **base))
    out.append(_c("deduction_caps", sec_80c=150_000, sec_80ccd1b=50_000, sec_80d_self=25_000,
                  sec_80d_parents=50_000, parents_senior=True, home_loan_interest=200_000, **base))
    return out                                                              # 24


def degenerate():
    all_deductions = dict(sec_80c=150_000, sec_80ccd1b=50_000, sec_80d_self=25_000,
                          home_loan_interest=200_000)
    return [
        _c("degenerate", gross_salary=0),
        _c("degenerate", gross_salary=1),
        _c("degenerate", gross_salary=40_000),          # below both standard deductions
        _c("degenerate", gross_salary=50_000),
        _c("degenerate", gross_salary=75_000),
        _c("degenerate", gross_salary=300_000, **all_deductions),
        _c("degenerate", gross_salary=400_000, **all_deductions),   # deductions exceed income
        _c("degenerate", gross_salary=100_000, basic_salary=60_000, hra_received=40_000, rent_paid=60_000),
    ]                                                                       # 8


def regime_comparison():
    all80 = dict(sec_80c=150_000, sec_80ccd1b=50_000, sec_80d_self=25_000)
    c80 = dict(sec_80c=150_000)
    loan = dict(home_loan_interest=200_000)

    def renter(basic, hra_amount, rent):
        return dict(basic_salary=basic, hra_received=hra_amount, rent_paid=rent, metro=True)

    rows = [
        (350_000, {}), (550_000, {}), (600_000, c80),                        # ties at zero
        (1_000_000, {}), (1_000_000, c80), (1_000_000, all80), (1_000_000, dict(all80, **loan)),
        (1_500_000, {}), (1_500_000, c80), (1_500_000, all80), (1_500_000, dict(all80, **loan)),
        (1_500_000, dict(all80, **loan, **renter(600_000, 300_000, 360_000))),
        (2_500_000, {}), (2_500_000, dict(all80, **loan)),
        (2_500_000, dict(all80, **loan, **renter(1_000_000, 500_000, 600_000))),
        (750_000, c80), (750_000, all80),
    ]
    out = [_c("regime_comparison", gross_salary=g, **extra) for g, extra in rows]
    out.append(_c("regime_comparison", age=65, gross_salary=900_000, sec_80d_self=50_000))
    out.append(_c("regime_comparison", age=82, gross_salary=1_200_000))
    out.append(_c("regime_comparison", age=62, gross_salary=1_500_000, **c80, **loan))
    return out                                                              # 20


def realistic():
    out = []
    for g in (600_000, 1_000_000, 1_500_000, 2_500_000, 5_000_000, 10_000_000):
        basic = g // 2
        hra_amount = basic * 2 // 5
        rent = hra_amount * 6 // 5
        out.append(_c("realistic", age=26, gross_salary=g))
        out.append(_c("realistic", age=32, gross_salary=g, basic_salary=basic, hra_received=hra_amount,
                      rent_paid=rent, metro=False, sec_80c=150_000, sec_80d_self=25_000))
        out.append(_c("realistic", age=38, gross_salary=g, basic_salary=basic, hra_received=hra_amount,
                      rent_paid=rent, metro=True, sec_80c=150_000, sec_80ccd1b=50_000,
                      home_loan_interest=200_000))
        out.append(_c("realistic", age=55, gross_salary=g, sec_80c=150_000, sec_80ccd1b=50_000,
                      sec_80d_self=25_000, sec_80d_parents=50_000, parents_senior=True,
                      home_loan_interest=200_000))
        out.append(_c("realistic", age=63, gross_salary=g, sec_80c=100_000, sec_80d_self=50_000))
    return out                                                              # 30


def seeded_random():
    rng = random.Random(20241120)
    out = []
    for _ in range(20):
        gross = int(10 ** rng.uniform(5.0, 7.3))
        basic = int(gross * rng.uniform(0.3, 0.6))
        hra_amount = int(basic * rng.uniform(0.0, 0.5))
        rent = int(hra_amount * rng.uniform(0.5, 1.8))
        inp = dict(age=rng.choice([24, 31, 45, 58, 61, 67, 82]), gross_salary=gross,
                   basic_salary=basic, hra_received=hra_amount, rent_paid=rent,
                   metro=rng.random() < 0.4)
        for name, high in (("sec_80c", 150_000), ("sec_80ccd1b", 50_000), ("sec_80d_self", 60_000),
                           ("sec_80d_parents", 60_000), ("home_loan_interest", 250_000)):
            if rng.random() < 0.5:
                inp[name] = int(rng.uniform(0.2, 1.0) * high)
        inp["parents_senior"] = rng.random() < 0.5
        out.append(_c("random", **inp))
    return out                                                              # 20


def age_boundary():
    """Added after the mutation check showed no case sat exactly on 59/60 or 79/80 (FAILURES.md F-003)."""
    out = []
    for age in (59, 60, 79, 80):                       # old-regime slab table changes at 60 and 80
        out.append(_c("age_boundary", age=age, gross_salary=900_000))
    for age in (59, 60):                               # 80D self cap changes at 60
        out.append(_c("age_boundary", age=age, gross_salary=2_000_000, sec_80d_self=50_000))
    for age in (79, 80):
        out.append(_c("age_boundary", age=age, gross_salary=1_200_000))
    return out                                                              # 8


# Hand-worked cases. Expected values are derived on paper in the comments and hard-coded here.
# They check the ORACLE itself; they never come from running any code.
HAND = [
    # H01 gross 10L. Old: 9,50,000 -> 12,500 + 20% of 4,50,000 = 1,02,500; x1.04 = 1,06,600.
    #     New: 9,25,000 -> 20,000 + 10% of 2,25,000 = 42,500; x1.04 = 44,200.
    (dict(gross_salary=1_000_000), (950_000, 106_600, 925_000, 44_200, "new")),
    # H02 as H01 with 80C 1.5L. Old: 8,00,000 -> 12,500 + 60,000 = 72,500; x1.04 = 75,400.
    (dict(gross_salary=1_000_000, sec_80c=150_000), (800_000, 75_400, 925_000, 44_200, "new")),
    # H03 gross 7,75,000. New: 7,00,000 -> 20,000, rebate 20,000 -> 0. Old: 7,25,000 -> 57,500; x1.04 = 59,800.
    (dict(gross_salary=775_000), (725_000, 59_800, 700_000, 0, "new")),
    # H04 gross 7,80,000. New: 7,05,000 -> tax 20,500 capped at 5,000 by marginal relief; x1.04 = 5,200.
    #     Old: 7,30,000 -> 58,500; x1.04 = 60,840.
    (dict(gross_salary=780_000), (730_000, 60_840, 705_000, 5_200, "new")),
    # H05 gross 5,50,000. Old: 5,00,000 -> 12,500 rebated to 0. New: 4,75,000 -> 8,750 rebated to 0. Tie -> new.
    (dict(gross_salary=550_000), (500_000, 0, 475_000, 0, "new")),
    # H06 age 65, gross 7L. Old: 6,50,000 -> 5% of 2,00,000 + 20% of 1,50,000 = 40,000; x1.04 = 41,600.
    #     New: 6,25,000 -> 16,250, rebated to 0.
    (dict(age=65, gross_salary=700_000), (650_000, 41_600, 625_000, 0, "new")),
    # H07 age 82, gross 12L. Old: 11,50,000 -> 1,00,000 + 45,000 = 1,45,000; x1.04 = 1,50,800.
    #     New: 11,25,000 -> 20,000 + 30,000 + 15% of 1,25,000 = 68,750; x1.04 = 71,500.
    (dict(age=82, gross_salary=1_200_000), (1_150_000, 150_800, 1_125_000, 71_500, "new")),
    # H08 gross 12L, basic 4L, HRA 2L, rent 3L, non-metro. HRA exempt = min(2L, 1.6L, 2.6L) = 1.6L.
    #     Old: 12L - 1.6L - 50k = 9,90,000 -> 12,500 + 98,000 = 1,10,500; x1.04 = 1,14,920. New 71,500 as H07.
    (dict(gross_salary=1_200_000, basic_salary=400_000, hra_received=200_000, rent_paid=300_000),
     (990_000, 114_920, 1_125_000, 71_500, "new")),
    # H09 gross 50,85,000. New: 50,10,000 -> tax 11,93,000; capped total = 11,90,000 + 10,000 = 12,00,000;
    #     x1.04 = 12,48,000. Old: 50,35,000 -> 13,23,000; capped total = 13,12,500 + 35,000 = 13,47,500; x1.04 = 14,01,400.
    (dict(gross_salary=5_085_000), (5_035_000, 1_401_400, 5_010_000, 1_248_000, "new")),
    # H10 gross 15L, 80C 1.5L, 80CCD(1B) 50k, 80D 25k, loan interest 2L.
    #     Old: 15L - 50k - 2L - 1.5L - 50k - 25k = 10,25,000 -> 12,500 + 1,00,000 + 7,500 = 1,20,000; x1.04 = 1,24,800.
    #     New: 14,25,000 -> 20,000 + 30,000 + 30,000 + 45,000 = 1,25,000; x1.04 = 1,30,000. Old is cheaper.
    (dict(gross_salary=1_500_000, sec_80c=150_000, sec_80ccd1b=50_000, sec_80d_self=25_000,
          home_loan_interest=200_000), (1_025_000, 124_800, 1_425_000, 130_000, "old")),
]


def build():
    groups = [slab_boundaries(), rebate_87a(), surcharge(), hra(), deduction_caps(),
              degenerate(), regime_comparison(), realistic(), seeded_random(), age_boundary()]
    cases, index = [], 0
    for group in groups:
        for n, case in enumerate(group, start=1):
            case = dict(case)
            case["id"] = "%s-%02d" % (case["category"], n)
            case["split"] = "dev" if index % 4 == 0 else "heldout"
            case["source"] = "oracle"
            case["expected"] = oracle(case["input"])
            cases.append(case)
            index += 1
    hand = []
    for n, (inp, exp) in enumerate(HAND, start=1):
        keys = ("old_total_income", "old_tax", "new_total_income", "new_tax", "best")
        hand.append({"id": "hand-%02d" % n, "category": "hand", "split": "n/a", "source": "hand",
                     "input": dict(fy=FY, **dict({"age": 30}, **inp)), "expected": dict(zip(keys, exp))})
    return cases, hand


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


if __name__ == "__main__":
    golden, hand_cases = build()
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    write_jsonl(CASES_DIR / "golden.jsonl", golden)
    write_jsonl(CASES_DIR / "hand_worked.jsonl", hand_cases)
    dev = sum(1 for c in golden if c["split"] == "dev")
    print("golden: %d cases (%d dev, %d heldout); hand-worked: %d" % (
        len(golden), dev, len(golden) - dev, len(hand_cases)))
