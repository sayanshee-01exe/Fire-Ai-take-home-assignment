"""Guards the shape of the suite itself, so it cannot quietly shrink (failure TEST-4)."""
import json
from collections import Counter
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent / "cases"

EXPECTED_COUNTS = {
    "slab_boundary": 36, "rebate_87a": 18, "surcharge": 28, "hra": 16, "deduction_caps": 24,
    "degenerate": 8, "regime_comparison": 20, "realistic": 30, "random": 20, "age_boundary": 8,
}


def _golden():
    with open(CASES_DIR / "golden.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_category_counts():
    assert Counter(c["category"] for c in _golden()) == EXPECTED_COUNTS


def test_at_least_200_cases_and_unique_ids():
    cases = _golden()
    assert len(cases) >= 200
    assert len({c["id"] for c in cases}) == len(cases)


def test_dev_and_heldout_split():
    split = Counter(c["split"] for c in _golden())
    assert split == {"dev": 52, "heldout": 156}      # every 4th case is dev


def test_every_case_names_its_year():
    assert all(c["input"]["fy"] == "2024-25" for c in _golden())


def test_both_regimes_each_win_somewhere():
    best = Counter(c["expected"]["best"] for c in _golden())
    assert best["old"] > 0 and best["new"] > 0
