"""The engine and the oracle against every stored case.

Three-way check on purpose:
  hand-worked cases (expected derived on paper) -> check the oracle AND the engine
  golden cases (expected from the oracle)       -> check the engine; also a fresh oracle run
"""
import json
from pathlib import Path

import pytest

from project_a.core.schema import TaxInput
from project_a.engines.hand_coded import HandCodedEngine
from project_a.tests.oracle import oracle

CASES_DIR = Path(__file__).resolve().parent / "cases"
ENGINE = HandCodedEngine()


def _load(name):
    with open(CASES_DIR / name, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


HAND = _load("hand_worked.jsonl")
GOLDEN = _load("golden.jsonl")


def _engine_view(case):
    res = ENGINE.calculate(TaxInput.from_dict(case["input"]))
    return {"old_total_income": res.old.total_income, "old_tax": res.old.tax,
            "new_total_income": res.new.total_income, "new_tax": res.new.tax, "best": res.best}


@pytest.mark.parametrize("case", HAND, ids=[c["id"] for c in HAND])
def test_oracle_matches_hand_worked(case):
    assert oracle(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", HAND, ids=[c["id"] for c in HAND])
def test_engine_matches_hand_worked(case):
    assert _engine_view(case) == case["expected"]


@pytest.mark.parametrize("case", GOLDEN, ids=[c["id"] for c in GOLDEN])
def test_engine_matches_golden(case):
    assert _engine_view(case) == case["expected"]


@pytest.mark.parametrize("case", GOLDEN, ids=[c["id"] for c in GOLDEN])
def test_stored_expected_is_not_stale(case):
    assert oracle(case["input"]) == case["expected"]
