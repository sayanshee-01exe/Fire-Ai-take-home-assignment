"""FY 2025-26 (Task 5): golden suite, hand-worked cases, and properties. FY 2024-25 tests are untouched."""
import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from project_a.core.schema import TaxInput
from project_a.engines.hand_coded import HandCodedEngine
from project_a.tests.gen_cases_fy2025_26 import build_cases
from project_a.tests.oracle import oracle

CASES_DIR = Path(__file__).resolve().parent / "cases"
ENGINE = HandCodedEngine()
FY = "2025-26"


def _load(name):
    with open(CASES_DIR / name, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


GOLDEN = _load("golden_fy2025_26.jsonl")
HAND = _load("hand_worked_fy2025_26.jsonl")


def _view(inp):
    r = ENGINE.calculate(TaxInput.from_dict(inp))
    return {"old_total_income": r.old.total_income, "old_tax": r.old.tax,
            "new_total_income": r.new.total_income, "new_tax": r.new.tax, "best": r.best}


# ---- suite shape ----------------------------------------------------------------------------------
def test_suite_shape():
    assert len(GOLDEN) >= 100 and len({c["id"] for c in GOLDEN}) == len(GOLDEN)
    assert all(c["input"]["fy"] == FY and c["source"] == "oracle" for c in GOLDEN)
    assert Counter(c["category"] for c in GOLDEN) == {
        "rebate_12l": 17, "new_slab_boundary": 15, "old_regime": 27, "surcharge": 28, "hra": 16,
        "deduction_caps": 24, "age_boundary": 8, "realistic": 30, "degenerate": 8}
    assert [c["input"] for c in GOLDEN] == [c["input"] for c in build_cases()]   # file matches the generator


def test_rebate_zone_points_are_present():
    gross = {c["input"]["gross_salary"] for c in GOLDEN if c["category"] == "rebate_12l"}
    assert {1_199_999, 1_200_000, 1_200_001, 1_210_000, 1_275_000, 1_275_001} <= gross
    ti = {c["expected"]["new_total_income"] for c in GOLDEN if c["category"] == "rebate_12l"}
    assert {1_270_580, 1_270_590} <= ti                           # the last relief point and the first after it


def test_split_follows_the_hash_rule():
    by_cat = {}
    for c in GOLDEN:
        by_cat.setdefault(c["category"], []).append(c)
    for cases in by_cat.values():
        k = max(1, round(len(cases) / 4))
        order = sorted(cases, key=lambda c: hashlib.sha256(c["id"].encode()).hexdigest())
        assert [c["split"] for c in order] == ["dev"] * k + ["heldout"] * (len(cases) - k)


# ---- expected values ------------------------------------------------------------------------------
@pytest.mark.parametrize("case", HAND, ids=[c["id"] for c in HAND])
def test_oracle_matches_paper(case):
    assert oracle(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", HAND, ids=[c["id"] for c in HAND])
def test_engine_matches_paper(case):
    assert _view(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", GOLDEN, ids=[c["id"] for c in GOLDEN])
def test_engine_matches_golden(case):
    assert _view(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", GOLDEN, ids=[c["id"] for c in GOLDEN])
def test_stored_expected_is_not_stale(case):
    assert oracle(case["input"]) == case["expected"]


def test_old_regime_is_unchanged_from_fy_2024_25():
    for c in GOLDEN:
        a = ENGINE.calculate(TaxInput.from_dict(c["input"]))
        b = ENGINE.calculate(TaxInput.from_dict(dict(c["input"], fy="2024-25")))
        assert (a.old.total_income, a.old.tax) == (b.old.total_income, b.old.tax), c["id"]


def test_engine_reports_the_fy_2025_26_rule_version():
    assert ENGINE.calculate(TaxInput(fy=FY, age=30, gross_salary=1_300_000)).rule_version == "fy2025_26.v1"


# ---- properties -----------------------------------------------------------------------------------
def _taxes(gross, age=30):
    r = ENGINE.calculate(TaxInput(fy=FY, age=age, gross_salary=gross))
    return r.old.tax, r.new.tax


WINDOWS = [  # gross salary windows: the 12L rebate edge and its relief zone, new slabs, surcharge thresholds
    (1_255_000, 1_365_000, 100), (1_640_000, 1_700_000, 500), (2_040_000, 2_120_000, 500),
    (2_440_000, 2_520_000, 500), (4_900_000, 5_700_000, 10_000), (9_900_000, 11_300_000, 10_000),
    (19_900_000, 22_500_000, 10_000), (49_900_000, 62_500_000, 10_000),
]


@pytest.mark.parametrize("start,stop,step", WINDOWS)
def test_tax_is_monotone_and_has_no_cliff(start, stop, step):
    previous = None
    for gross in range(start, stop + 1, step):
        taxes = _taxes(gross)
        if previous is not None:
            for now, before in zip(taxes, previous):
                assert now >= before, gross                        # monotone
                assert now - before <= 1.04 * step + 30, gross      # no cliff (rounding slack)
        previous = taxes


def test_old_regime_cliff_at_5_lakh_is_the_known_exception():
    # Unchanged from FY 2024-25 (FAILURES.md F-001): no marginal relief in the old regime.
    assert _taxes(550_000)[0] == 0 and _taxes(550_010)[0] == 13_000


def test_tax_never_decreases_from_0_to_30_lakh():
    # Monotone everywhere, including the old-regime 5L cliff (a jump up); the cliff is only excluded
    # from the no-cliff windows above.
    previous = (0, 0)
    for gross in range(0, 3_000_001, 2_500):
        taxes = _taxes(gross)
        assert taxes[0] >= previous[0] and taxes[1] >= previous[1], gross
        previous = taxes


def test_marginal_relief_never_makes_tax_exceed_the_extra_income():
    for ti in range(1_200_010, 1_280_001, 10):
        _, new_tax = _taxes(ti + 75_000)
        assert new_tax <= (ti - 1_200_000) * 1.04 + 5, ti          # C-87A-NEW-MR, before rounding to 10


def test_official_sheet_fy2025_26_has_12_rows_and_no_expected_values():
    import csv
    from project_a.tests.official import make_sheet as ms
    path = Path(__file__).resolve().parent / "official" / "official_checks_fy2025_26.csv"
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 12 and list(rows[0]) == ms.COLUMNS
    assert [r["check_id"] for r in rows] == ["OC26-%02d" % i for i in range(1, 13)]
    values = [r[k] for r in rows for k in ms.OFFICIAL_COLUMNS]   # empty before filling, whole numbers after
    assert all(v == "" for v in values) or all(v.strip().isdigit() for v in values)
    golden = {c["id"]: c for c in GOLDEN}
    assert all(golden[r["case_id"]]["input"]["fy"] == FY for r in rows)
    md = (path.parent / "official_checks_fy2025_26.md").read_text()
    assert "AY 2026-27" in md and "AY 2025-26" not in md and "Balance Tax Payable" in md


# ---- FY 2025-26 official-calculator rows (AY 2026-27) ---------------------------------------------
OFFICIAL_26 = CASES_DIR / "official_26.jsonl"


def test_engine_and_oracle_match_the_fy2025_26_official_rows():
    if not OFFICIAL_26.exists():
        pytest.skip("official_26.jsonl not created yet (fill the sheet, run import_official --fy 2025-26)")
    excepted = {e["case_id"] for e in json.loads((CASES_DIR / "official_exceptions.json").read_text())}
    cases = [json.loads(l) for l in OFFICIAL_26.read_text().splitlines() if l.strip()]
    assert len(cases) == 12 and all(c["source"] == "official" and c["input"]["fy"] == FY for c in cases)
    fields = ("old_total_income", "old_tax", "new_total_income", "new_tax")
    failures = []
    for c in cases:
        if c["id"] in excepted:
            continue
        want = {k: c["expected"][k] for k in fields}
        eng = {k: v for k, v in _view(c["input"]).items() if k in fields}
        ora = {k: v for k, v in oracle(c["input"]).items() if k in fields}
        if eng != want or ora != want:
            failures.append((c["check_id"], want, eng, ora))
    assert not failures, failures


def test_fy2025_26_import_flags_one_altered_value(tmp_path):
    import csv
    from project_a.tests.official import import_official, make_sheet_fy2025_26 as m26
    rows, _ = m26.build_rows()
    golden = {c["id"]: c for c in m26.load_golden()}
    for r in rows:                                          # a fake filled sheet from the oracle
        o = oracle(golden[r["case_id"]]["input"])
        for k in import_official.FIELDS:
            r["official_" + k] = o[k]
        r["notes"] = ""
    rows[2]["official_new_tax"] += 10
    path = tmp_path / "filled26.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=m26.ms.COLUMNS)
        w.writeheader()
        w.writerows(rows)
    mismatches = import_official.run(path, tmp_path / "o26.jsonl", tmp_path / "r26.md", fy="2025-26")
    assert [(m["check_id"], m["field"]) for m in mismatches] == [("OC26-03", "new_tax")]
    assert "FY 2025-26" in (tmp_path / "r26.md").read_text()
