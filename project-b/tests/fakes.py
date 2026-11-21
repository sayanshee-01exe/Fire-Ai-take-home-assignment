"""A fake Gemini client for tests: no network, scripted answers.

Usage in a test:
    from project_b import llm
    from tests.fakes import FakeClient, rows_in_prompt

    def responder(prompt, schema):
        rows = rows_in_prompt(prompt)            # the JSON records the detector sent
        return {"rows": [{"id": r["id"], "flagged": r["latitude"] < 0, "reason": "x"} for r in rows]}

    llm.set_client(FakeClient(responder))
    ...
    llm.set_client(None)                         # restore (or use the `fake_llm` fixture pattern)
"""
from __future__ import annotations

import json
from typing import Any, Callable

from project_b.core import Usage
from project_b.llm import LLMReply, LLMUnavailable


class FakeClient:
    model = "fake-model"

    def __init__(self, responder: Callable[[str, dict | None], Any] | None = None, unavailable: bool = False,
                 finish_reason: str = ""):
        self.responder = responder or (lambda prompt, schema: {"rows": [], "issue_found": False, "reason": "fake"})
        self.unavailable = unavailable
        self.finish_reason = finish_reason  # Gemini's finishReason on every reply (e.g. "MAX_TOKENS")
        self.calls: list[dict] = []
        self.default_repeat = 0

    def generate_json(self, prompt: str, schema: dict | None = None, *, tag: str, system: str | None = None, repeat: int | None = None, max_output_tokens: int = 4096) -> LLMReply:
        if self.unavailable:
            raise LLMUnavailable("fake: no API access")
        self.calls.append({"prompt": prompt, "tag": tag, "schema": schema})
        data = self.responder(prompt, schema)
        usage = Usage(llm_calls=1, input_tokens=max(1, len(prompt) // 4), output_tokens=60, llm_latency_s=0.8)
        return LLMReply(data=data, text=json.dumps(data), usage=usage, cached=False, model=self.model, finish_reason=self.finish_reason)


def rows_in_prompt(prompt: str) -> list[dict]:
    """Return the JSON-lines records embedded in a review_rows() prompt."""
    out = []
    for line in prompt.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and "id" in rec:
                out.append(rec)
    return out
