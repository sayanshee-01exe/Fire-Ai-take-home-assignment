"""Usage log, per-role models, and quota errors that must not crash the harnesses. Fake transport only."""
import json

import pytest

from project_a.eval import nl_eval
from project_a.eval import run as eval_run
from project_a.llm import client as client_mod
from project_a.llm.client import QuotaExhausted, calls_today
from project_a.llm.config import ROLE_DEFAULTS, get_config
from project_a.tests.oracle import oracle
from project_a.tests.test_llm import FAKE_KEY, FakeTransport, client_for, llm_json, no_network, ok  # noqa: F401

QUOTA = (429, {}, json.dumps({"error": {"code": 429, "message": "Quota exceeded ... limit: 20 ... per day",
                                        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                     "retryDelay": "49440s"}]}}).encode())


def test_every_live_request_is_logged_and_counted(tmp_path, monkeypatch):
    t = FakeTransport((503, {}, b"busy"), ok('{"a": 1}'))
    c = client_for(tmp_path, monkeypatch, t)
    c.generate("p")
    c.generate("p")                                     # cache hit: not a request, not logged
    log = [json.loads(l) for l in (tmp_path / "cache" / "usage.jsonl").read_text().splitlines()]
    assert [(u["model"], u["method"], u["status"]) for u in log] == [
        ("gemini-test-flash", "generateContent", 503), ("gemini-test-flash", "generateContent", 200)]
    assert calls_today("gemini-test-flash", tmp_path / "cache") == {"requests": 2, "ok": 1}
    assert "calls today on gemini-test-flash: 2 requests (1 OK)" in c.calls_today_line()
    assert FAKE_KEY not in (tmp_path / "cache" / "usage.jsonl").read_text()


def test_each_role_has_its_own_configurable_model(monkeypatch):
    for var in ("GEMINI_MODEL", "GEMINI_MODEL_PARSER", "GEMINI_MODEL_QUERY_ENGINE", "GEMINI_MODEL_COMPILER"):
        monkeypatch.delenv(var, raising=False)
    assert {r: get_config(r).model for r in ROLE_DEFAULTS} == ROLE_DEFAULTS
    monkeypatch.setenv("GEMINI_MODEL_PARSER", "model-a")
    monkeypatch.setenv("GEMINI_MODEL", "model-b")
    assert (get_config("parser").model, get_config("query_engine").model) == ("model-a", "model-b")
    with pytest.raises(ValueError):
        get_config("poet")


def test_cache_key_changes_with_the_model():
    k = lambda m: client_mod.cache_key(m, "s", "p", 0.0, 0)
    assert k("gemini-3.5-flash-lite") != k("gemini-3.1-flash-lite")


def test_a_quota_429_raises_quota_exhausted_without_waiting(tmp_path, monkeypatch):
    c = client_for(tmp_path, monkeypatch, FakeTransport(QUOTA))
    with pytest.raises(QuotaExhausted, match="49440"):
        c.generate("p")
    assert 49440.0 not in c.sleeps and c.stats["network_calls"] == 1


def oracle_llm(payload):
    case = json.loads(payload["contents"][0]["parts"][0]["text"].split("\n", 1)[1])
    o = oracle(case)
    return ok(llm_json(o["old_total_income"], o["old_tax"], o["new_total_income"], o["new_tax"]))


def test_engine_harness_writes_partial_results_on_quota(tmp_path, monkeypatch):
    fake = client_for(tmp_path, monkeypatch, FakeTransport(oracle_llm, QUOTA))
    monkeypatch.setattr(client_mod, "GeminiClient", lambda *a, **k: fake)
    with pytest.raises(SystemExit) as e:
        eval_run.main(["--engine", "llm_query", "--split", "dev", "--limit", "3", "--out-dir", str(tmp_path / "out")])
    assert e.value.code == 3
    data = json.loads(next((tmp_path / "out").iterdir()).read_text())
    assert data["summary"]["incomplete"] and data["summary"]["runs"] == 1 and data["summary"]["tax_exact"] == 1
    assert [x["status"] for x in data["not_run"]] == ["not run: quota"] * 2


def test_nl_harness_writes_partial_results_on_quota(tmp_path, monkeypatch, capsys):
    extraction = json.dumps({"age": {"value": "30", "text": "30"},
                             "year": {"label": "FY 2024-25", "type": "FY", "text": "x"}, "city": None,
                             "parents_senior": None, "amounts": {"gross_salary": {
                                 "value": "12", "scale": "lakh", "period": "annual", "text": "12 lakh"}},
                             "unsupported": [], "ambiguities": []})
    fake = client_for(tmp_path, monkeypatch, FakeTransport(ok(extraction), QUOTA))
    monkeypatch.setattr(client_mod, "GeminiClient", lambda *a, **k: fake)
    with pytest.raises(SystemExit) as e:
        nl_eval.main(["--split", "dev", "--prompt", "v1", "--out-dir", str(tmp_path / "out")])
    assert e.value.code == 3
    data = json.loads(next((tmp_path / "out").iterdir()).read_text())
    assert data["summary"]["n"] == 1 and len(data["not_run"]) == 14
    assert all(x["status"] == "not run: quota" for x in data["not_run"])
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out and "calls today on gemini-test-flash" in out
