"""Shape and correctness of the natural-language query set (tests/nl/queries.jsonl)."""
import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from project_a.core.schema import TaxInput, validate_input
from project_a.engines.hand_coded import HandCodedEngine
from project_a.tests.nl import freeze

TESTS = Path(__file__).resolve().parent
ROWS = [json.loads(l) for l in (TESTS / "nl" / "queries.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()]
OFFICIAL = {c["check_id"]: c for c in (json.loads(l) for l in
            (TESTS / "cases" / "official_30.jsonl").read_text().splitlines() if l.strip())}
COMPUTE = [r for r in ROWS if r["expected_behavior"] == "compute"]
DEV_QUOTA = {"compute": 9, "ask": 3, "unsupported": 2, "law-change": 1}
FIELDS = ("id", "text", "language", "split", "expected_behavior", "gold_input",
          "clarification_topics", "unsupported_features", "source_case", "notes")


def test_fifty_rows_unique_ids_and_texts():
    assert len(ROWS) == 50
    assert [r["id"] for r in ROWS] == ["NL-%02d" % i for i in range(1, 51)]
    assert len({r["text"].strip().lower() for r in ROWS}) == 50
    assert all(list(r) == list(FIELDS) and r["notes"].strip() for r in ROWS)


def test_behaviour_counts():
    assert Counter(r["expected_behavior"] for r in ROWS) == {
        "compute": 30, "ask": 10, "unsupported": 7, "law-change": 3}


def test_language_counts_roughly_as_planned():
    langs = Counter(r["language"] for r in ROWS)
    assert set(langs) == {"en", "hinglish", "mixed"}
    for lang, target in {"en": 19, "hinglish": 19, "mixed": 12}.items():
        assert abs(langs[lang] - target) <= 2, langs


def test_split_counts_and_rule():
    assert Counter(r["split"] for r in ROWS) == {"dev": 15, "heldout": 35}
    for behavior, k in DEV_QUOTA.items():
        group = sorted((r for r in ROWS if r["expected_behavior"] == behavior),
                       key=lambda r: hashlib.sha256(r["id"].encode()).hexdigest())
        assert [r["split"] for r in group] == ["dev"] * k + ["heldout"] * (len(group) - k)
    dev = [r for r in ROWS if r["split"] == "dev"]
    assert {r["expected_behavior"] for r in dev} == set(DEV_QUOTA)
    assert {r["language"] for r in dev} == {"en", "hinglish", "mixed"}


def test_compute_rows_map_one_to_one_onto_official_checks():
    assert sorted(r["source_case"] for r in COMPUTE) == ["OC-%02d" % i for i in range(1, 31)]
    for r in COMPUTE:
        assert r["gold_input"] == OFFICIAL[r["source_case"]]["input"]
        assert r["clarification_topics"] == [] and r["unsupported_features"] == []


@pytest.mark.parametrize("row", COMPUTE, ids=[r["id"] for r in COMPUTE])
def test_gold_input_validates_and_engine_matches_official(row):
    inp = TaxInput.from_dict(row["gold_input"])
    assert validate_input(inp) == []
    res = HandCodedEngine().calculate(inp)
    official = OFFICIAL[row["source_case"]]["expected"]
    assert (res.old.tax, res.new.tax) == (official["old_tax"], official["new_tax"])
    assert (res.old.total_income, res.new.total_income) == (
        official["old_total_income"], official["new_total_income"])


def test_non_compute_rows():
    for r in ROWS:
        b = r["expected_behavior"]
        if b == "compute":
            continue
        assert r["gold_input"] is None and r["source_case"] is None
        assert bool(r["clarification_topics"]) == (b == "ask"), r["id"]
        assert bool(r["unsupported_features"]) == (b == "unsupported"), r["id"]


def test_manifest_hash_holds_once_frozen():
    assert freeze.check() is None


def test_freeze_detects_a_later_edit(tmp_path):
    q, m = tmp_path / "queries.jsonl", tmp_path / "MANIFEST.json"
    q.write_text('{"id": "NL-01"}\n')
    freeze.write_manifest(q, m, frozen=False)
    q.write_text('{"id": "NL-01", "edited": true}\n')
    assert freeze.check(q, m) is None                  # not frozen: edits allowed
    assert freeze.freeze(q, m)["frozen"] is True
    q.write_text('{"id": "NL-01", "edited": 2}\n')
    assert "changed after it was frozen" in freeze.check(q, m)
    with pytest.raises(SystemExit, match="refusing to re-freeze"):
        freeze.freeze(q, m)
