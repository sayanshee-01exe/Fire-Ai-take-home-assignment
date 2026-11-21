"""Generates the FY 2025-26 golden suite. Run:  python -m project_a.tests.gen_cases_fy2025_26

Inputs only come from build_cases(); expected values come from the independent oracle (source
"oracle"). Hand-worked cases (HAND) are derived on paper in the comments and check the oracle and
the engine. The 2024-25 generator (gen_cases.py) and its suite are not changed: categories whose
rules did not change are reused from it with fy set to 2025-26.

Split: within each category, ids are sorted by sha256(id) and the first quarter (rounded, at least
one) is "dev", the rest "heldout" (the same hash rule as the NL query set).
"""
import hashlib
import json
from pathlib import Path

from project_a.tests import gen_cases as g24

FY = "2025-26"
STD_NEW = 75_000
CASES_DIR = Path(__file__).resolve().parent / "cases"
GOLDEN = CASES_DIR / "golden_fy2025_26.jsonl"
HAND_FILE = CASES_DIR / "hand_worked_fy2025_26.jsonl"


def _c(cat, **inp):
    inp.setdefault("age", 30)
    return {"category": cat, "input": dict(fy=FY, **inp)}


def _refy(cases, cat=None):
    for c in cases:
        c["input"]["fy"] = FY
        if cat:
            c["category"] = cat
    return cases


def rebate_12l():
    """The new 87A limit (12 lakh) and its marginal-relief zone, which ends near 12,70,588."""
    out = [_c("rebate_12l", gross_salary=g) for g in              # as gross salary (TI = gross - 75,000)
           (1_199_999, 1_200_000, 1_200_001, 1_210_000, 1_275_000, 1_275_001)]
    for ti in (1_199_990, 1_200_010, 1_210_000, 1_250_000, 1_270_580, 1_270_590,
               1_270_600, 1_280_000, 1_300_000):                  # as total income (12,00,000 is gross 12,75,000 above)
        out.append(_c("rebate_12l", gross_salary=ti + STD_NEW))
    out += [_c("rebate_12l", gross_salary=g) for g in (1_275_004, 1_275_005)]   # C-288A rounding on the edge
    return out                                                     # 17


def new_slab_boundary():
    out = []
    for t in (400_000, 800_000, 1_600_000, 2_000_000, 2_400_000):
        for d in (-10, 0, 10):
            out.append(_c("new_slab_boundary", gross_salary=t + d + STD_NEW))
    return out                                                     # 15


def old_regime():
    """Old regime is unchanged: reuse the 2024-25 old-slab and 5-lakh rebate boundaries."""
    old_slabs = g24.slab_boundaries()[15:]                         # 21 old-regime rows (three age bands)
    old_rebate = g24.rebate_87a()[:6]                              # 6 rows at the 5-lakh limit
    return _refy(old_slabs + old_rebate, "old_regime")             # 27


def build_cases():
    groups = [rebate_12l(), new_slab_boundary(), old_regime(), _refy(g24.surcharge()), _refy(g24.hra()),
              _refy(g24.deduction_caps()), _refy(g24.age_boundary()), _refy(g24.realistic()),
              _refy(g24.degenerate())]
    cases, counters = [], {}
    for group in groups:
        for c in group:
            counters[c["category"]] = counters.get(c["category"], 0) + 1
            c["id"] = "fy26-%s-%02d" % (c["category"], counters[c["category"]])
            cases.append(c)
    by_cat = {}
    for c in cases:
        by_cat.setdefault(c["category"], []).append(c)
    for group in by_cat.values():
        k = max(1, round(len(group) / 4))
        for i, c in enumerate(sorted(group, key=lambda c: hashlib.sha256(c["id"].encode()).hexdigest())):
            c["split"] = "dev" if i < k else "heldout"
    return cases


# Hand-worked FY 2025-26 cases, derived on paper; they never come from running code.
# New slabs: 0-4L nil, 4-8L 5%, 8-12L 10%, 12-16L 15%, 16-20L 20%, 20-24L 25%, above 24L 30%.
# 87A new: TI <= 12L -> rebate up to 60,000; above 12L tax before cess <= TI - 12L. Old regime unchanged.
HAND = [
    # W01 gross 12,75,000, age 30. New: TI 12,00,000; slab 20,000 + 40,000 = 60,000; rebate 60,000 -> 0.
    #     Old: TI 12,25,000; 12,500 + 1,00,000 + 30% of 2,25,000 (67,500) = 1,80,000; x1.04 = 1,87,200.
    ("W01", dict(age=30, gross_salary=1_275_000), (1_225_000, 187_200, 1_200_000, 0)),
    # W02 gross 13,00,000. New: TI 12,25,000; slab 60,000 + 15% of 25,000 = 63,750 > excess 25,000,
    #     so tax 25,000; x1.04 = 26,000. Old: TI 12,50,000; 1,12,500 + 75,000 = 1,87,500; x1.04 = 1,95,000.
    ("W02", dict(age=30, gross_salary=1_300_000), (1_250_000, 195_000, 1_225_000, 26_000)),
    # W03 gross 13,45,580. New: TI 12,70,580; slab 60,000 + 10,587 = 70,587 > excess 70,580 -> 70,580;
    #     x1.04 = 73,403.20 -> 73,400. Old: TI 12,95,580; 1,12,500 + 88,674 = 2,01,174; x1.04 = 2,09,220.96
    #     -> paise dropped 2,09,220 -> 2,09,220.
    ("W03", dict(age=30, gross_salary=1_345_580), (1_295_580, 209_220, 1_270_580, 73_400)),
    # W04 gross 13,45,590 (relief has ended). New: TI 12,70,590; slab 60,000 + 10,588.50 = 70,588.50
    #     < excess 70,590 -> 70,588.50; x1.04 = 73,412.04 -> 73,410. Old: TI 12,95,590; 1,12,500 + 88,677
    #     = 2,01,177; x1.04 = 2,09,224.08 -> 2,09,220.
    ("W04", dict(age=30, gross_salary=1_345_590), (1_295_590, 209_220, 1_270_590, 73_410)),
    # W05 gross 25,00,000. New: TI 24,25,000; 20,000 + 40,000 + 60,000 + 80,000 + 1,00,000 + 30% of 25,000
    #     (7,500) = 3,07,500; x1.04 = 3,19,800. Old: TI 24,50,000; 1,12,500 + 30% of 14,50,000 (4,35,000)
    #     = 5,47,500; x1.04 = 5,69,400.
    ("W05", dict(age=30, gross_salary=2_500_000), (2_450_000, 569_400, 2_425_000, 319_800)),
    # W06 gross 8,75,000. New: TI 8,00,000; slab 20,000; rebate -> 0. Old: TI 8,25,000; 12,500 + 20% of
    #     3,25,000 (65,000) = 77,500; x1.04 = 80,600.
    ("W06", dict(age=30, gross_salary=875_000), (825_000, 80_600, 800_000, 0)),
    # W07 age 65, gross 6,00,000. New: TI 5,25,000; 5% of 1,25,000 = 6,250; rebate -> 0. Old (senior):
    #     TI 5,50,000; 5% of 2,00,000 (10,000) + 20% of 50,000 (10,000) = 20,000; x1.04 = 20,800.
    ("W07", dict(age=65, gross_salary=600_000), (550_000, 20_800, 525_000, 0)),
]


def hand_cases():
    out = []
    for hid, inp, (oti, otax, nti, ntax) in HAND:
        out.append({"id": "fy26-hand-" + hid, "source": "hand", "input": dict(fy=FY, **inp),
                    "expected": {"old_total_income": oti, "old_tax": otax, "new_total_income": nti,
                                 "new_tax": ntax, "best": "new" if ntax <= otax else "old"}})
    return out


def main():
    from project_a.tests.oracle import oracle
    cases = build_cases()
    with open(GOLDEN, "w", encoding="utf-8") as f:
        for c in cases:
            row = {"category": c["category"], "expected": oracle(c["input"]), "id": c["id"],
                   "input": c["input"], "source": "oracle", "split": c["split"]}
            f.write(json.dumps(row, sort_keys=True) + "\n")
    with open(HAND_FILE, "w", encoding="utf-8") as f:
        for c in hand_cases():
            f.write(json.dumps(c, sort_keys=True) + "\n")
    print("wrote %d cases to %s and %d hand-worked cases" % (len(cases), GOLDEN.name, len(HAND)))


if __name__ == "__main__":
    main()
