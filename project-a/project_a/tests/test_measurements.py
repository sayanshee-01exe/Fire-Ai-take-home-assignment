"""Task 4 measurement tooling: traceability check, determinism helpers, the comparison script. No network."""
import json
from fractions import Fraction as Fr

import pytest

from project_a.core.schema import TaxInput
from project_a.engines.hand_coded import HandCodedEngine
from project_a.eval import compare, determinism, traceability as tr
from project_a.tests.test_llm import no_network  # noqa: F401 (autouse: any real network call fails)

INP = {"fy": "2024-25", "age": 30, "gross_salary": 780000}


def _hand(inp):
    r = HandCodedEngine().calculate(TaxInput.from_dict(inp))
    steps = [(reg.regime, t.clause, t.amount) for reg in (r.old, r.new) for t in reg.trace]
    return (r.old.total_income, r.old.tax, r.new.total_income, r.new.tax), steps


def test_clause_ids_come_from_each_years_rules_text():
    for fy in ("2024-25", "2025-26"):
        ids = tr.clause_ids(fy)
        assert {"C-87A-NEW-MR", "C-80D-SELF", "C-288B", "C-SUR-MR"} <= ids and "C-FAKE" not in ids


@pytest.mark.parametrize("inp", [INP, {"fy": "2025-26", "age": 65, "gross_salary": 5_100_000, "sec_80c": 150_000},
                                 {"fy": "2024-25", "age": 30, "gross_salary": 1_200_000, "basic_salary": 333_333,
                                  "hra_received": 200_000, "rent_paid": 300_000, "metro": True}])
def test_hand_coded_trace_passes_all_three_levels(inp):
    reported, steps = _hand(inp)
    res = tr.check(inp, reported, steps, True)
    assert (res["a"], res["b"], res["c"]) == (True, True, True), res["bad"]


def test_each_level_catches_its_own_fault():
    reported, steps = _hand(INP)
    untagged = [(r, "" if c == "C-STD-OLD" else c, a) for r, c, a in steps]
    assert not tr.check(INP, reported, untagged, True)["a"]
    assert not tr.check(INP, (1, 2, 3, 4), steps, True)["a"]                     # reported amount not in the trace
    unknown = [(r, "C-NOPE" if c == "C-CESS" else c, a) for r, c, a in steps]
    res = tr.check(INP, reported, unknown, True)
    assert res["a"] and not res["b"]
    wrong = [(r, c, a + 7 if c == "C-SLAB-OLD" else a) for r, c, a in steps]
    res = tr.check(INP, reported, wrong, True)
    assert res["a"] and res["b"] and not res["c"] and res["bad"][0]["clause"] == "C-SLAB-OLD"
    off_by_paise = [(r, c, a - Fr(1, 2) if c == "C-SLAB-OLD" else a) for r, c, a in steps]
    res = tr.check(INP, reported, off_by_paise, True)
    assert not res["c"] and res["c_within_1"]


def test_a_running_figure_is_accepted_for_a_tax_step():
    ref, _, _ = tr.reference(INP)
    assert Fr(5_000) in ref["new"]["C-87A-NEW-MR"] or Fr(5_000) in ref["new"]["C-87A-NEW"]   # tax after relief


def test_determinism_selections_are_fixed():
    q = [c["id"] for c in determinism.query_cases()]
    assert len(q) == 20 == len(set(q)) and q[:3] == ["realistic-02", "realistic-12", "realistic-22"]
    p = [r["id"] for r in determinism.parser_queries()]
    assert p == ["NL-04", "NL-08", "NL-12", "NL-14", "NL-17", "NL-20", "NL-24", "NL-27", "NL-30", "NL-31"]


def test_run_index_client_injects_the_run_index():
    seen = []

    class Inner:
        model, config, stats = "m", object(), {}

        def generate(self, prompt, system=None, json_mode=True, run_index=0):
            seen.append(run_index)
            return "ok"
    c = determinism.RunIndexClient(Inner(), 7)
    assert c.generate("p", system="s") == "ok" and seen == [7] and c.model == "m"


def test_compare_builds_every_section_from_repo_files():
    text = compare.build()
    for head in ("## 1. Accuracy", "## 2. Tax exact by category", "## 3. Plain-language parser", "## 4. Determinism",
                 "## 5. Latency", "## 6. Cost per 1,000", "## 7. Traceability", "## 8. Task 5", "## Files used"):
        assert head in text
    assert "| hand-coded | dev | 52/52 (100.0%)" in text and "NOTIONAL" in text


def test_compare_survives_missing_results(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "RESULTS", tmp_path)
    text = compare.build()
    assert "| hand-coded | dev | n/a |" in text and "Not run." in text and "not run" in text


def test_prices_are_per_model(monkeypatch):
    from project_a.llm.config import get_config
    for var in ("GEMINI_MODEL", "GEMINI_MODEL_PARSER", "GEMINI_MODEL_QUERY_ENGINE", "LLM_PRICE_INPUT_PER_M",
                "LLM_PRICE_OUTPUT_PER_M"):
        monkeypatch.delenv(var, raising=False)
    q, p = get_config("query_engine"), get_config("parser")
    assert (q.model, q.price_input_per_m, q.price_output_per_m) == ("gemini-3.1-flash-lite", 0.25, 1.50)
    assert (p.model, p.price_input_per_m, p.price_output_per_m) == ("gemini-3.5-flash-lite", 0.30, 2.50)
    assert round(q.notional_cost(2511, 527) * 1000, 2) == 1.42
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_M", "1")
    assert get_config("query_engine").price_input_per_m == 1.0


def test_reproduce_table_comparison_detects_a_changed_cell():
    import importlib.util
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("reproduce", ROOT / "reproduce.py")
    rep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rep)
    md = (ROOT / "RESULTS_TABLES.md").read_text(encoding="utf-8")
    ok, report, _ = rep.compare_tables(md, md)
    assert ok and report and all(s == "PASS" for s, _, _ in report)
    changed = md.replace("| hand-coded | dev | 52/52 (100.0%)", "| hand-coded | dev | 51/52 (98.1%)", 1)
    ok, report, _ = rep.compare_tables(md, changed)
    assert not ok and any(s == "FAIL" and k.startswith("1. Accuracy") for s, k, _ in report)
    timed = md.replace("| hand-coded | 3810 | 33.2 us", "| hand-coded | 3810 | 99.9 us", 1)
    assert rep.compare_tables(md, timed)[0]          # re-measured timing cells are not compared
