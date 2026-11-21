"""Task 5 part B plumbing: FY 2025-26 suite in the harness, per-artifact year guard, compile by year.
Fake transport only; no network."""
import json

import pytest

from project_a.compiler import compile as comp
from project_a.core.errors import UnknownRuleVersion
from project_a.core.schema import TaxInput
from project_a.engines.llm_compiled import COMPILED, LLMCompiledEngine
from project_a.eval import run as eval_run
from project_a.tests.test_compiler import GOOD
from project_a.tests.test_llm import FakeTransport, client_for, no_network, ok  # noqa: F401

IN_24 = TaxInput.from_dict({"fy": "2024-25", "age": 30, "gross_salary": 1_300_000})
IN_25 = TaxInput.from_dict({"fy": "2025-26", "age": 30, "gross_salary": 1_300_000})


def test_harness_loads_the_fy2025_26_suite():
    assert len(eval_run.load_cases("dev", suite="fy2025_26")) == 44
    assert len(eval_run.load_cases("heldout", suite="fy2025_26")) == 129
    assert len(eval_run.load_cases("all", suite="fy2025_26")) == 173
    assert len(eval_run.load_cases("dev")) == 52                     # FY 2024-25 default unchanged


@pytest.mark.parametrize("split", ["heldout", "all"])
def test_harness_refuses_heldout_and_all_without_the_flag(tmp_path, split):
    with pytest.raises(SystemExit) as e:
        eval_run.main(["--engine", "hand_coded", "--suite", "fy2025_26", "--split", split, "--out-dir", str(tmp_path)])
    assert e.value.code == 2 and list(tmp_path.iterdir()) == []


def test_year_guard_fy2024_25_artifact_refuses_fy2025_26_input():
    e = LLMCompiledEngine("reviewed", COMPILED)
    try:
        assert e.fy == "2024-25"
        with pytest.raises(UnknownRuleVersion, match="compiled for FY 2024-25"):
            e.calculate(IN_25)
        assert e.calculate(IN_24).error is None
    finally:
        e.close()


def test_year_guard_fy2025_26_artifact_refuses_fy2024_25_input(tmp_path):
    (tmp_path / "raw_v1.py").write_text(GOOD)
    (tmp_path / "manifest.json").write_text(json.dumps({"raw_v1": {"fy": "2025-26"}}))
    e = LLMCompiledEngine("raw_v1", tmp_path)
    try:
        with pytest.raises(UnknownRuleVersion, match="compiled for FY 2025-26"):
            e.calculate(IN_24)
        assert e.calculate(IN_25).error is None
    finally:
        e.close()


def test_fy2024_25_compile_prompt_is_unchanged_and_fy2025_26_uses_the_new_rules():
    assert comp.build_prompt()[2] == "27200f8c63a06ea490236d4074ad3e10c22addc1a217e77f161b783e7368f13d"
    _, user, _, _ = comp.build_prompt("2025-26")
    assert comp.YEARS["2025-26"][0].read_text() in user
    assert 'fy (str, "2025-26"), age (int, years on 31 March 2026)' in user
    for leaked in ("_slab_tax", "round_statutory", "Fraction", "NEW_STEPS", "def compute(inp):\n    "):
        assert leaked not in user


def test_compile_by_year_writes_to_its_own_directory(tmp_path, monkeypatch):
    import dataclasses
    c = client_for(tmp_path, monkeypatch, FakeTransport(ok(GOOD)))
    c.config = dataclasses.replace(c.config, temperature=0.2, max_retries=0)
    done, stopped, _ = comp.compile_rules(3, 0.2, client=c, out_dir=tmp_path / "fy26", fy="2025-26")
    assert stopped is None and [m["fy"] for m in done] == ["2025-26"] * 3
    assert "RULES_FY2025_26.md" in (tmp_path / "fy26" / "raw_v1.py").read_text()
    manifest = json.loads((tmp_path / "fy26" / "manifest.json").read_text())
    assert {k: v["fy"] for k, v in manifest.items()} == {"raw_v1": "2025-26", "raw_v2": "2025-26", "raw_v3": "2025-26"}


def test_year_guard_with_the_real_fy2025_26_reviewed_artifact():
    e = LLMCompiledEngine("reviewed", COMPILED / "fy2025_26")
    try:
        assert e.fy == "2025-26" and e.violations == []
        with pytest.raises(UnknownRuleVersion, match="compiled for FY 2025-26"):
            e.calculate(IN_24)
        res = e.calculate(IN_25)
        assert (res.error, res.new.tax) == (None, 26_000)            # paper case W02
    finally:
        e.close()


def test_fy2025_26_reviewed_matches_the_official_rows(tmp_path):
    s = eval_run.run("llm_compiled", "official", suite="fy2025_26", artifact="reviewed", out_dir=tmp_path)
    assert (s["tax_exact"], s["income_exact"], s["unparseable"]) == (12, 12, 0)


def test_query_engine_sends_the_fy2025_26_rules_for_a_fy2025_26_input(tmp_path, monkeypatch):
    from project_a.engines.llm_query import LLMQueryEngine
    from project_a.tests.test_llm import llm_json
    t = FakeTransport(ok(llm_json(1_250_000, 195_000, 1_225_000, 26_000)))
    res = LLMQueryEngine(client_for(tmp_path, monkeypatch, t)).calculate(IN_25)
    system = json.loads(t.calls[0]["body"])["systemInstruction"]["parts"][0]["text"]
    assert comp.YEARS["2025-26"][0].read_text() in system and "Salaried-individual income-tax rules: FY 2024-25" not in system
    assert '"fy": "2025-26"' in json.loads(t.calls[0]["body"])["contents"][0]["parts"][0]["text"]
    assert (res.error, res.new.tax, res.rule_version) == (None, 26_000, "fy2025_26.v1")


def test_harness_writes_partial_results_on_a_network_failure(tmp_path, monkeypatch):
    from project_a.llm import client as client_mod
    from project_a.llm.client import TransportError
    from project_a.tests.test_llm_ops import oracle_llm
    calls = {"n": 0}

    def flaky(method, url, headers, body, timeout):
        calls["n"] += 1
        if calls["n"] > 1:
            raise TransportError("timed out")
        return oracle_llm(json.loads(body))
    fake = client_for(tmp_path, monkeypatch, flaky)
    fake.config = __import__("dataclasses").replace(fake.config, max_retries=1)
    monkeypatch.setattr(client_mod, "GeminiClient", lambda *a, **k: fake)
    with pytest.raises(SystemExit) as e:
        eval_run.main(["--engine", "llm_query", "--suite", "fy2025_26", "--split", "dev", "--limit", "3",
                       "--out-dir", str(tmp_path / "out")])
    assert e.value.code == 3
    data = json.loads(next((tmp_path / "out").iterdir()).read_text())
    assert data["summary"]["runs"] == 1 and [x["status"] for x in data["not_run"]] == ["not run: network error"] * 2
