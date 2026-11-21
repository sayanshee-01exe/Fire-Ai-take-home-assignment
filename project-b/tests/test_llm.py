"""Tests for the Gemini client's failure handling and replay cache (no network: requests.post
is replaced by a scripted fake, and the cache lives in a temporary folder).

Covers audit bugs:
  8  - access refused (bad key, no permission, unknown model) -> LLMUnavailable, not a crash
  9  - circuit breaker: after quota exhaustion / refusal no further requests are sent
  10 - live mode never asks the same key twice in one process, so replay == the live run
"""
from __future__ import annotations

import json

import pytest

from project_b import config, llm

OK_BODY = {
    "candidates": [{"content": {"parts": [{"text": '{"rows": []}'}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
}


class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._body if isinstance(self._body, dict) else json.loads(self._body)


@pytest.fixture()
def client_factory(tmp_path, monkeypatch):
    """Build clients whose cache is in tmp_path and whose HTTP replies are scripted."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")  # never the real key from .env
    monkeypatch.setattr(config, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    sent = []

    def make(replies, mode="auto"):
        queue = list(replies)

        def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002 - mirrors requests
            sent.append(json)
            return queue.pop(0) if len(queue) > 1 else queue[0]

        monkeypatch.setattr(llm.requests, "post", fake_post)
        return llm.GeminiClient(model="test-model", mode=mode, rpm=1e9)

    return make, sent


@pytest.mark.parametrize("status", [401, 403, 404])
def test_access_refused_is_llm_unavailable_and_trips_breaker(client_factory, status):
    make, sent = client_factory
    client = make([FakeResponse(status, {"error": {"message": "no access"}})])
    with pytest.raises(llm.LLMUnavailable, match=f"refused access \\({status}\\)"):
        client.generate_json("p1", tag="t")
    with pytest.raises(llm.LLMUnavailable, match="no further requests"):
        client.generate_json("p2", tag="t")
    assert len(sent) == 1  # the second prompt never reached the API


def test_bad_api_key_400_is_unavailable_without_retry(client_factory):
    make, sent = client_factory
    client = make([FakeResponse(400, {"error": {"message": "API key not valid. Please pass a valid API key."}})])
    with pytest.raises(llm.LLMUnavailable, match="API key not valid"):
        client.generate_json("p", tag="t")
    assert len(sent) == 1


def test_generic_400_retries_without_thinking_then_is_a_real_error(client_factory):
    make, sent = client_factory
    client = make([FakeResponse(400, {"error": {"message": "Request contains an invalid argument."}})])
    client._send_thinking_config = True  # as for a model that takes a thinking budget
    with pytest.raises(RuntimeError) as exc:
        client.generate_json("p", tag="t")
    assert not isinstance(exc.value, llm.LLMUnavailable)  # a malformed request is a bug, not "unavailable"
    assert "thinkingConfig" in sent[0]["generationConfig"] and "thinkingConfig" not in sent[1]["generationConfig"]


def test_the_default_model_gets_no_thinking_budget(client_factory, monkeypatch):
    """gemini-3.5 models reject thinkingBudget with HTTP 400: not sending it saves a wasted request."""
    from project_b import config
    make, sent = client_factory
    monkeypatch.setattr(config, "GEMINI_DEFAULT_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setattr(llm.requests, "post", lambda url, headers=None, json=None, timeout=None: (sent.append(json), FakeResponse(200, OK_BODY))[1])
    client = llm.GeminiClient(mode="auto", rpm=1e9)
    client.generate_json("p", tag="t")
    assert len(sent) == 1 and "thinkingConfig" not in sent[0]["generationConfig"]


def test_daily_quota_trips_breaker_but_cached_answers_still_served(client_factory):
    make, sent = client_factory
    client = make([FakeResponse(200, OK_BODY), FakeResponse(429, {"error": {"message": "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel"}})])
    first = client.generate_json("cached prompt", tag="t")
    with pytest.raises(llm.LLMUnavailable, match="daily quota"):
        client.generate_json("new prompt", tag="t")
    with pytest.raises(llm.LLMUnavailable, match="no further requests"):
        client.generate_json("another new prompt", tag="t")
    again = client.generate_json("cached prompt", tag="t")  # answered before the quota ran out
    assert again.cached and again.text == first.text
    assert len(sent) == 2


def test_live_mode_asks_each_key_once_per_process_so_replay_matches(client_factory, tmp_path):
    make, sent = client_factory
    live = make([FakeResponse(200, OK_BODY)], mode="live")
    a = live.generate_json("same prompt", tag="t")
    b = live.generate_json("same prompt", tag="t")  # e.g. determinism repeat 0 re-asking the main prompt
    assert len(sent) == 1 and not a.cached and b.cached and a.text == b.text
    lines = (tmp_path / "test-model.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # no duplicate cache line
    replay = llm.GeminiClient(model="test-model", mode="replay")
    assert replay.generate_json("same prompt", tag="t").text == a.text


def test_live_mode_still_refreshes_answers_from_earlier_runs(client_factory, tmp_path):
    make, sent = client_factory
    old = dict(OK_BODY, candidates=[{"content": {"parts": [{"text": '{"rows": ["old"]}'}]}, "finishReason": "STOP"}])
    make([FakeResponse(200, old)]).generate_json("p", tag="t")  # an earlier run cached an answer
    fresh = make([FakeResponse(200, OK_BODY)], mode="live").generate_json("p", tag="t")
    assert len(sent) == 2 and not fresh.cached
    replay = llm.GeminiClient(model="test-model", mode="replay")
    assert replay.generate_json("p", tag="t").text == fresh.text  # last line wins = the refresh


def test_unrecognised_quota_429_stops_the_run_after_one_prompt(client_factory):
    """A 429 whose message names no daily quota is retried; if every retry is still 429, all further
    calls stop (circuit breaker) instead of waiting out the retries for every remaining prompt."""
    make, sent = client_factory
    client = make([FakeResponse(429, {"error": {"message": "Resource has been exhausted (e.g. check quota)."}})])
    with pytest.raises(llm.LLMUnavailable, match="kept answering 429"):
        client.generate_json("p1", tag="t")
    with pytest.raises(llm.LLMUnavailable, match="no further requests"):
        client.generate_json("p2", tag="t")
    assert len(sent) == 8  # the retries of the first prompt only


def test_a_half_written_cache_line_does_not_block_a_rerun(client_factory, tmp_path):
    """A run killed while appending leaves a truncated last line; the answers before it still replay."""
    make, sent = client_factory
    make([FakeResponse(200, OK_BODY)]).generate_json("answered", tag="t")
    with open(tmp_path / "test-model.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"key": "abc", "text": "{\\"rows\\"')  # interrupted mid-write
    replay = llm.GeminiClient(model="test-model", mode="replay")
    assert replay.generate_json("answered", tag="t").cached and len(sent) == 1


def test_replay_mode_never_calls_the_api(client_factory):
    make, sent = client_factory
    client = make([FakeResponse(200, OK_BODY)], mode="replay")
    with pytest.raises(llm.LLMUnavailable, match="no cached answer"):
        client.generate_json("p", tag="t")
    assert sent == []



def test_review_rows_reports_why_each_row_has_no_verdict(client_factory):
    """An unreadable reply: every row of its prompt abstains with the reply's finish reason (kept in the
    cache, so replay gives the same cause); a readable reply that skips an id: that row abstains too."""
    make, sent = client_factory
    cut_off = {"candidates": [{"content": {"parts": [{"text": '{"rows": [{"id": 0, "flagged": false, "reason": "on and on'}]},
                               "finishReason": "MAX_TOKENS"}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4096}}
    skips = {"candidates": [{"content": {"parts": [{"text": '{"rows": [{"id": 2, "flagged": true}]}'}]}, "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}
    client = make([FakeResponse(200, cut_off), FakeResponse(200, skips)])
    records = [{"id": i, "v": i} for i in range(4)]
    abstained = {}
    verdicts, usage, errors = llm.review_rows("task", records, tag="t", rows_per_call=2, client=client, abstained=abstained)
    assert list(verdicts) == [2] and usage.llm_calls == 2 and "finish reason MAX_TOKENS" in errors[0]
    assert abstained == {0: "unreadable_reply:MAX_TOKENS", 1: "unreadable_reply:MAX_TOKENS", 3: llm.ABSTAIN_NO_VERDICT}
    replay = llm.GeminiClient(model="test-model", mode="replay")
    again = {}
    llm.review_rows("task", records, tag="t", rows_per_call=2, client=replay, abstained=again)
    assert again == abstained and len(sent) == 2  # same causes from the cache, no new request


def test_longest_json_string_finds_a_runaway_field():
    assert llm.longest_json_string('{"rows": [{"id": 1, "reason": "short"}]}') == 6  # "reason" is the longest
    assert llm.longest_json_string('{"reason": "' + "safely " * 500) == 3500  # unterminated: the reply was cut off
    assert llm.longest_json_string('{"a": "x\\"y"}') == 4  # an escaped quote does not end the string
    assert llm.longest_json_string("") == 0


def test_audit_cache_lists_unreadable_replies_and_never_changes_the_cache(tmp_path):
    lines = [
        {"key": "1", "tag": "f/x", "repeat": 0, "finish_reason": "MAX_TOKENS", "output_tokens": 4079,
         "prompt": "task\n\nRows to review (JSON Lines, one service request per line):\n{\"id\": 5}\n{\"id\": 6}\n\nReturn",
         "text": '{"rows": [{"id": 5, "reason": "' + "nicely " * 100},
        {"key": "2", "tag": "f/x", "repeat": 0, "finish_reason": "RECITATION", "output_tokens": 0,
         "prompt": "task\n\nRows to review (JSON Lines, one service request per line):\n{\"id\": 7}\n\nReturn", "text": ""},
        {"key": "3", "tag": "f/x", "repeat": 0, "finish_reason": "STOP", "output_tokens": 9,
         "prompt": "task\n\nRows to review (JSON Lines, one service request per line):\n{\"id\": 8}\n\nReturn",
         "text": '{"rows": [{"id": 8, "flagged": false}]}'},
        {"key": "4", "tag": "a/x", "repeat": 0, "finish_reason": "STOP", "output_tokens": 9, "prompt": "batch", "text": '{"issue_found": false}'},
    ]
    path = tmp_path / "cache.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    before = path.read_bytes()
    audit = llm.audit_cache(path)
    assert audit["n_entries"] == 4 and path.read_bytes() == before
    assert [(u["finish_reason"], u["rows_asked"], u["ids"]) for u in audit["unreadable"]] == [("MAX_TOKENS", 2, "5..6"), ("RECITATION", 1, "7..7")]
    assert audit["unreadable"][0]["longest_string_chars"] == 700 and audit["unreadable"][1]["meaning"].startswith("blocked")
    assert audit["readable_longest_string"]["n"] == 1  # only per-row replies are compared
