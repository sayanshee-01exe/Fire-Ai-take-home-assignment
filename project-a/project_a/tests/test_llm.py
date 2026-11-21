"""Gemini client, record/replay cache and the LLM-at-query-time engine. Fake transport only:
an autouse fixture makes any real network call fail the test."""
import json
import urllib.request

import pytest

from project_a.core.schema import TaxInput
from project_a.engines.hand_coded import HandCodedEngine
from project_a.engines.llm_query import LLMQueryEngine, parse_output
from project_a.eval import run as eval_run
from project_a.llm.client import CacheMiss, GeminiClient, LLMError
from project_a.llm.config import get_config
from project_a.tests.oracle import oracle

FAKE_KEY = "FAKEKEY-abc123-do-not-leak"
CASE = TaxInput.from_dict({"fy": "2024-25", "age": 30, "gross_salary": 780000})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to use the real network")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)        # load_dotenv then never reads .env
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-flash")


def ok(text, prompt_tokens=100, output_tokens=40, thoughts=10):
    body = {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": prompt_tokens, "candidatesTokenCount": output_tokens,
                              "thoughtsTokenCount": thoughts}}
    return 200, {}, json.dumps(body).encode()


def llm_json(old_ti, old_tax, new_ti, new_tax):
    trace = [{"step": "slab tax", "amount": 1, "clause": "C-SLAB-NEW"}]
    return json.dumps({"old_regime": {"taxable_income": old_ti, "tax": old_tax, "trace": trace},
                       "new_regime": {"taxable_income": new_ti, "tax": new_tax, "trace": trace}})


class FakeTransport:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return r(json.loads(body)) if callable(r) else r


def client_for(tmp_path, monkeypatch, transport, mode="auto"):
    monkeypatch.setenv("LLM_MODE", mode)
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    sleeps = []
    c = GeminiClient(config=get_config(), transport=transport, sleep=sleeps.append)
    c.sleeps = sleeps
    return c


# ---- cache ----------------------------------------------------------------------------------------
def test_cache_miss_then_hit_and_run_index_is_part_of_the_key(tmp_path, monkeypatch):
    t = FakeTransport(ok('{"a": 1}'))
    c = client_for(tmp_path, monkeypatch, t)
    first = c.generate("p", system="s")
    second = c.generate("p", system="s")
    assert (first.cached, second.cached, len(t.calls)) == (False, True, 1)
    assert second.text == first.text and second.latency_s == first.latency_s     # original latency kept
    assert (second.prompt_tokens, second.output_tokens) == (100, 50)            # thinking counted as output
    c.generate("p", system="s", run_index=1)
    assert len(t.calls) == 2 and len(list((tmp_path / "cache").glob("*.json"))) == 2


def test_replay_mode_never_touches_the_network(tmp_path, monkeypatch):
    t = FakeTransport(ok('{"a": 1}'))
    with pytest.raises(CacheMiss, match="replay mode"):
        client_for(tmp_path, monkeypatch, t, mode="replay").generate("p")
    assert t.calls == []
    client_for(tmp_path, monkeypatch, t, mode="auto").generate("p")             # record it
    replay = client_for(tmp_path, monkeypatch, t, mode="replay")
    assert replay.generate("p").cached and len(t.calls) == 1
    assert replay.stats["network_calls"] == 0


def test_live_mode_calls_even_when_cached(tmp_path, monkeypatch):
    t = FakeTransport(ok('{"a": 1}'))
    client_for(tmp_path, monkeypatch, t, mode="auto").generate("p")
    assert not client_for(tmp_path, monkeypatch, t, mode="live").generate("p").cached
    assert len(t.calls) == 2


# ---- retries --------------------------------------------------------------------------------------
def test_retry_on_429_respects_the_reported_delay(tmp_path, monkeypatch):
    rate_limited = (429, {}, json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3s"}]}}).encode())
    t = FakeTransport(rate_limited, ok('{"a": 1}'))
    c = client_for(tmp_path, monkeypatch, t)
    assert c.generate("p").text == '{"a": 1}'
    assert len(t.calls) == 2 and 3.0 in c.sleeps
    assert c.stats["http_429"] == 1 and c.stats["reported_delays_s"] == [3.0]


def test_retry_on_5xx_uses_exponential_backoff(tmp_path, monkeypatch):
    t = FakeTransport((503, {}, b"busy"), (500, {}, b"oops"), ok('{"a": 1}'))
    c = client_for(tmp_path, monkeypatch, t)
    c.generate("p")
    backoffs = [s for s in c.sleeps if s in (2.0, 4.0)]
    assert backoffs == [2.0, 4.0] and c.stats["http_5xx"] == 2


def test_long_reported_delay_stops_instead_of_waiting(tmp_path, monkeypatch):
    quota = (429, {"Retry-After": "3600"}, b'{"error": {"code": 429}}')
    t = FakeTransport(quota)
    c = client_for(tmp_path, monkeypatch, t)
    with pytest.raises(LLMError, match="HTTP 429.*3600"):
        c.generate("p")
    assert len(t.calls) == 1 and 3600.0 not in c.sleeps


# ---- secrets --------------------------------------------------------------------------------------
def test_key_goes_in_the_header_and_never_in_cache_or_errors(tmp_path, monkeypatch):
    t = FakeTransport(ok("model echoed " + FAKE_KEY))
    c = client_for(tmp_path, monkeypatch, t)
    c.generate("p", system="s")
    call = t.calls[0]
    assert call["headers"]["x-goog-api-key"] == FAKE_KEY
    assert FAKE_KEY not in call["url"] and "key=" not in call["url"]
    for f in (tmp_path / "cache").iterdir():
        assert FAKE_KEY not in f.read_text()

    leaky = FakeTransport((400, {}, ("bad request for key " + FAKE_KEY).encode()))
    with pytest.raises(LLMError) as e:
        client_for(tmp_path, monkeypatch, leaky).generate("other prompt")
    assert FAKE_KEY not in str(e.value) and "[REDACTED]" in str(e.value)
    with pytest.raises(LLMError) as e:
        client_for(tmp_path, monkeypatch, FakeTransport((503, {}, FAKE_KEY.encode())))\
            .generate("third prompt")
    assert FAKE_KEY not in str(e.value)


# ---- engine: strict parsing, no repair ------------------------------------------------------------
def engine_with(tmp_path, monkeypatch, text):
    return LLMQueryEngine(client_for(tmp_path, monkeypatch, FakeTransport(ok(text))))


@pytest.mark.parametrize("text, problem", [
    ("```json\n" + llm_json(705000, 0, 705000, 5200) + "\n```", "not valid JSON"),
    ('{"old_regime": {"taxable_income": 730000, "tax": 0, "trace": []}}', "missing object new_regime"),
    (llm_json(730000, "59,800", 705000, 5200), "not a number"),
    (json.dumps({"old_regime": {"tax": 1, "trace": []}, "new_regime": {"taxable_income": 1, "tax": 1,
                                                                        "trace": []}}), "taxable_income missing"),
    ("[1, 2]", "not an object"),
])
def test_unparseable_output_is_an_error_result_that_keeps_the_text(tmp_path, monkeypatch, text, problem):
    res = engine_with(tmp_path, monkeypatch, text).calculate(CASE)
    assert res.error and problem in res.error
    assert res.raw_output == text and res.old.tax is None and res.new.tax is None


def test_wrong_numbers_are_reported_exactly_not_repaired(tmp_path, monkeypatch):
    res = engine_with(tmp_path, monkeypatch, llm_json(730001, 59801.7, 705003, 21320)).calculate(CASE)
    right = HandCodedEngine().calculate(CASE)
    assert res.error is None
    assert (res.old.total_income, res.old.tax, res.new.total_income, res.new.tax) == (730001, 59801.7, 705003, 21320)
    assert (res.new.tax, right.new.tax) == (21320, 5200)          # the model's wrong figure survives
    assert res.llm_trace["new"][0]["clause"] == "C-SLAB-NEW"
    assert res.usage == {"prompt_tokens": 100, "output_tokens": 50, "thoughts_tokens": 10}


def test_prompt_contains_the_rules_and_the_case(tmp_path, monkeypatch):
    t = FakeTransport(ok(llm_json(730000, 59800, 705000, 5200)))
    LLMQueryEngine(client_for(tmp_path, monkeypatch, t)).calculate(CASE)
    sent = json.loads(t.calls[0]["body"])
    assert "C-87A-NEW-MR" in sent["systemInstruction"]["parts"][0]["text"]
    assert '"gross_salary": 780000' in sent["contents"][0]["parts"][0]["text"]
    assert sent["generationConfig"] == {"temperature": 0.0, "responseMimeType": "application/json"}


def test_parse_output_accepts_a_well_formed_answer():
    data, problems = parse_output(llm_json(730000, 59800, 705000, 5200))
    assert problems == [] and data["new_regime"]["tax"] == 5200


# ---- harness ------------------------------------------------------------------------------------
def test_harness_refuses_heldout_without_the_flag(tmp_path):
    with pytest.raises(SystemExit) as e:
        eval_run.main(["--engine", "hand_coded", "--split", "heldout", "--out-dir", str(tmp_path)])
    assert e.value.code == 2 and list(tmp_path.iterdir()) == []


def test_harness_on_dev_with_a_fake_llm_then_replay(tmp_path, monkeypatch):
    def oracle_llm(payload):                                       # a "model" that is always right
        case = json.loads(payload["contents"][0]["parts"][0]["text"].split("\n", 1)[1])
        o = oracle(case)
        return ok(llm_json(o["old_total_income"], o["old_tax"], o["new_total_income"], o["new_tax"]))
    t = FakeTransport(oracle_llm)
    live = eval_run.run("llm_query", "dev", limit=3, client=client_for(tmp_path, monkeypatch, t, "live"),
                        out_dir=tmp_path / "out")
    assert (live["tax_exact"], live["unparseable"], live["client_stats"]["network_calls"]) == (3, 0, 3)
    replay = eval_run.run("llm_query", "dev", limit=3, client=client_for(tmp_path, monkeypatch, t, "replay"),
                          out_dir=tmp_path / "out")
    assert replay["client_stats"]["network_calls"] == 0 and replay["cache_hits"] == 3 and len(t.calls) == 3
    rows = lambda s: [{k: v for k, v in r.items() if k != "cached"}
                      for r in json.loads(open(s["results_file"]).read())["rows"]]
    assert rows(live) == rows(replay)
    for f in (tmp_path / "out").iterdir():
        assert FAKE_KEY not in f.read_text()


def test_harness_hand_coded_dev_is_exact(tmp_path):
    s = eval_run.run("hand_coded", "dev", limit=5, out_dir=tmp_path)
    assert s["tax_exact"] == 5 and s["unparseable"] == 0
