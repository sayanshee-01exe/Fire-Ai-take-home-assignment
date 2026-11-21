import re
from fractions import Fraction
from pathlib import Path

import pytest

from project_a.core.errors import InvalidInput, UnknownRuleVersion
from project_a.core.rounding import round_statutory
from project_a.core.schema import TaxInput
from project_a.engines.hand_coded import HandCodedEngine

ENGINE = HandCodedEngine()
RULES_TEXT = Path(__file__).resolve().parent.parent / "rules" / "RULES_FY2024_25.md"


@pytest.mark.parametrize("amount,expected", [
    (0, 0), (4, 0), (5, 10), (14, 10), (15, 20), (699_995, 700_000), (700_005, 700_010),
    (Fraction(49, 10), 0),        # 4.90: paise are ignored, so the last figure is 4
    (Fraction(95, 10), 10),       # 9.50: ignore paise -> 9 -> rounds up to 10
    (Fraction(1_234_567_5, 10), 1_234_570),
])
def test_round_to_ten(amount, expected):
    assert round_statutory(amount, 10) == expected


def test_round_unit_one_only_drops_paise():
    assert round_statutory(Fraction(1234, 10), 1) == 123


@pytest.mark.parametrize("bad", [
    dict(gross_salary=-1),
    dict(gross_salary=1_000_000.5),
    dict(gross_salary=True),
    dict(gross_salary=500_000, basic_salary=600_000),
    dict(gross_salary=500_000, hra_received=600_000),
    dict(gross_salary=500_000, sec_80c=-5),
    dict(gross_salary=500_000, metro="yes"),
])
def test_invalid_inputs_are_rejected_loudly(bad):
    with pytest.raises(InvalidInput):
        ENGINE.calculate(TaxInput(fy="2024-25", age=30, **bad))


def test_age_out_of_range_is_rejected():
    with pytest.raises(InvalidInput):
        ENGINE.calculate(TaxInput(fy="2024-25", age=200, gross_salary=500_000))


@pytest.mark.parametrize("fy", ["2030-31", "2024", "../secrets", "2024-25 "])
def test_unknown_or_malformed_fy_raises(fy):
    with pytest.raises(UnknownRuleVersion):
        ENGINE.calculate(TaxInput(fy=fy, age=30, gross_salary=500_000))


def test_empty_fy_is_an_invalid_input():
    with pytest.raises(InvalidInput):
        ENGINE.calculate(TaxInput(fy="", age=30, gross_salary=500_000))


def test_unknown_and_missing_fields_are_rejected():
    with pytest.raises(InvalidInput):
        TaxInput.from_dict({"fy": "2024-25", "age": 30, "gross_salary": 1, "salary": 5})
    with pytest.raises(InvalidInput):
        TaxInput.from_dict({"fy": "2024-25", "age": 30})


def test_old_only_items_are_reported_not_silently_dropped():
    r = ENGINE.calculate(TaxInput(fy="2024-25", age=30, gross_salary=2_000_000,
                                  sec_80c=150_000, home_loan_interest=200_000))
    joined = " ".join(r.new.notes)
    assert "80C" in joined and "home-loan" in joined


def test_result_names_its_rule_version_and_engine():
    r = ENGINE.calculate(TaxInput(fy="2024-25", age=30, gross_salary=1_000_000))
    assert r.rule_version == "fy2024_25.v1" and r.engine == "hand_coded"
    assert r.to_dict()["new"]["tax"] == r.new.tax


def test_every_clause_in_a_trace_exists_in_the_rule_text():
    known = set(re.findall(r"\*\*(C-[A-Z0-9-]+)\*\*", RULES_TEXT.read_text(encoding="utf-8")))
    probes = [
        dict(gross_salary=780_000),
        dict(gross_salary=5_085_000),
        dict(age=65, gross_salary=1_500_000, basic_salary=600_000, hra_received=300_000,
             rent_paid=360_000, metro=True, sec_80c=150_000, sec_80ccd1b=50_000,
             sec_80d_self=50_000, sec_80d_parents=50_000, parents_senior=True,
             home_loan_interest=200_000),
        dict(gross_salary=400_000, sec_80c=150_000),
        dict(gross_salary=60_000_000),
    ]
    used = set()
    for kw in probes:
        kw.setdefault("age", 30)
        r = ENGINE.calculate(TaxInput(fy="2024-25", **kw))
        for step in r.old.trace + r.new.trace:
            used.add(step.clause)
    used.discard("input")
    assert used and used <= known, used - known
