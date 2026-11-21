"""Minimal Gemini client: JSON answers, replay cache, rate limiting, token accounting.

Why a replay cache: the brief requires that one command reproduces every number.
LLM answers are not reproducible by nature (and the free tier is rate-limited),
so every response is appended to project_b/llm_cache/<model>.jsonl together with
its token counts and measured latency. Re-running replays those answers, so the
reported accuracy, cost and latency are identical without an API key.

Modes (env LLM_MODE):  auto   - use the cache, call the API on a miss (default)
                       replay - cache only; a miss raises LLMUnavailable
                       live   - always call the API (refreshes the cache)
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from project_b import config
from project_b.core import Usage

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

SYSTEM_PROMPT = (
    "You are a meticulous data-quality reviewer for New York City 311 service-request data. "
    "You check ONLY the specific problem you are asked about. Flag something only when the "
    "evidence in the data shown makes the problem clear; if unsure, do not flag. "
    "Respond with JSON that matches the given schema."
)

ROW_REVIEW_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "rows": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "INTEGER"},
                    "flagged": {"type": "BOOLEAN"},
                    "reason": {"type": "STRING"},
                    "observed": {"type": "STRING"},
                    "expected": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": ["id", "flagged"],
            },
        }
    },
    "required": ["rows"],
}

BATCH_REVIEW_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "issue_found": {"type": "BOOLEAN"},
        "columns": {"type": "ARRAY", "items": {"type": "STRING"}},
        "row_ids": {"type": "ARRAY", "items": {"type": "INTEGER"}},
        "segment": {"type": "STRING"},
        "reason": {"type": "STRING"},
        "observed": {"type": "STRING"},
        "expected": {"type": "STRING"},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["issue_found", "reason"],
}


class LLMUnavailable(RuntimeError):
    """No cached answer and no way to get a live one (no key, replay mode, quota exhausted,
    access refused). Detectors turn it into an 'llm_unavailable' result = "not run"."""


# Why a row the model was asked to judge got no readable verdict (DetectionResult.abstained).
# An abstention is scored as "no verdict": never as a clean row, never as a reviewed row.
ABSTAIN_UNREADABLE = "unreadable_reply"  # the reply holds no parseable verdict list; suffixed ":<finishReason>"
ABSTAIN_NO_VERDICT = "no_verdict_in_reply"  # a readable reply that skipped this row's id
ABSTAIN_NOT_SENT = "not_sent"  # the detector sent no prompt (the fields the check needs did not arrive)
FINISH_REASON_MEANING = {
    "MAX_TOKENS": "cut off at the output-token limit",
    "RECITATION": "blocked by Gemini's recitation filter",
    "SAFETY": "blocked by Gemini's safety filter",
    "STOP": "finished normally but held no readable verdict list",
}

ACCESS_REFUSED = (401, 403, 404)  # unauthenticated / no permission / unknown or retired model
API_KEY_PROBLEM = re.compile(r"API[ _]?key|API_KEY_INVALID", re.IGNORECASE)  # Gemini answers a bad key with HTTP 400


def _error_message(body: str) -> str:
    """The human message inside a Gemini JSON error body (falls back to the raw text)."""
    try:
        return str(json.loads(body)["error"]["message"])[:300]
    except (ValueError, KeyError, TypeError):
        return body[:300]


@dataclass
class LLMReply:
    data: Any  # parsed JSON, or None if the model returned something unparseable
    text: str
    usage: Usage  # exactly one logical call
    cached: bool
    model: str
    finish_reason: str = ""  # Gemini's finishReason (STOP, MAX_TOKENS, RECITATION, ...), kept in the cache


def unreadable_cause(finish_reason: str) -> str:
    """The abstention cause for every row of a reply without a parseable verdict list."""
    return f"{ABSTAIN_UNREADABLE}:{finish_reason or 'UNKNOWN'}"


def _load_dotenv() -> None:
    """Read KEY=VALUE lines from <repo>/.env without overriding real env vars."""
    if not config.ENV_FILE.exists():
        return
    for line in config.ENV_FILE.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}|\[.*\]", text or "", flags=re.S)  # tolerate stray prose
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None


class GeminiClient:
    def __init__(self, model: str | None = None, mode: str | None = None, rpm: float | None = None):
        _load_dotenv()
        self.model = model or os.environ.get("GEMINI_MODEL") or config.GEMINI_DEFAULT_MODEL
        self.mode = (mode or os.environ.get("LLM_MODE") or "auto").lower()
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.min_interval = 60.0 / float(rpm or os.environ.get("GEMINI_RPM") or config.GEMINI_RPM)
        self._lock = threading.Lock()
        self._last_call = 0.0
        # Send thinkingBudget=0 only to models known to accept it; gemini-3.5 models reject it with a
        # 400 (they do not think by default), which would waste one request per run.
        self._send_thinking_config = self.model.startswith(config.THINKING_BUDGET_MODEL_PREFIXES)
        # The harness sets this > 0 to re-ask identical prompts (determinism check)
        # without every detector having to thread a `repeat` argument through.
        self.default_repeat = 0
        # Circuit breaker: once the API refuses access or the daily quota is gone, every later
        # uncached prompt fails at once instead of sending (and waiting on) a doomed request.
        self._unavailable: str | None = None
        # Keys answered by a live call in THIS process (see generate_json, live mode).
        self._answered_this_run: set[str] = set()
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.model)
        self.cache_path = config.LLM_CACHE_DIR / f"{safe}.jsonl"
        self._cache: dict[str, dict] = self._read_cache()

    # ------------------------------------------------------------------ cache
    def _read_cache(self) -> dict[str, dict]:
        cache: dict[str, dict] = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    cache[entry["key"]] = entry
                except (json.JSONDecodeError, KeyError, TypeError):
                    # a run killed mid-write leaves a half line: it holds no answer, so it is skipped
                    # (that prompt is simply asked again) instead of blocking every rerun
                    continue
        return cache

    def _append(self, entry: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._cache[entry["key"]] = entry
            self._answered_this_run.add(entry["key"])
            with open(self.cache_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _key(self, prompt: str, schema: dict | None, system: str | None, repeat: int, max_output_tokens: int) -> str:
        blob = json.dumps(
            {"model": self.model, "prompt": prompt, "schema": schema, "system": system, "repeat": repeat, "temperature": 0.0, "max_out": max_output_tokens},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def can_call_api(self) -> bool:
        return bool(self.api_key) and self.mode != "replay"

    # ------------------------------------------------------------------ calls
    def generate_json(
        self,
        prompt: str,
        schema: dict | None = None,
        *,
        tag: str,
        system: str | None = SYSTEM_PROMPT,
        repeat: int | None = None,
        max_output_tokens: int = 4096,
    ) -> LLMReply:
        """One JSON-mode generation at temperature 0. `tag` labels the cache entry
        (e.g. 'k/llm_resolution_status'); `repeat` > 0 forces a distinct call with
        the same prompt (used only to measure determinism)."""
        repeat = self.default_repeat if repeat is None else repeat
        key = self._key(prompt, schema, system, repeat, max_output_tokens)
        # Live mode refreshes the cache, but a key this process has already answered is served
        # from memory: asking it twice would append a second, possibly different answer, and a
        # later replay (last line wins) would then disagree with the run that made the results.
        if key in self._cache and (self.mode != "live" or key in self._answered_this_run):
            e = self._cache[key]
            usage = Usage(llm_calls=1, llm_cached_calls=1, input_tokens=e["input_tokens"], output_tokens=e["output_tokens"], llm_latency_s=e["latency_s"])
            return LLMReply(data=_parse_json(e["text"]), text=e["text"], usage=usage, cached=True, model=self.model,
                            finish_reason=str(e.get("finish_reason") or ""))
        if self._unavailable:
            raise LLMUnavailable(f"{self._unavailable} (no further requests sent in this run)")
        if not self.can_call_api:
            raise LLMUnavailable(
                f"no cached answer for tag={tag} and no live API access (mode={self.mode}, key={'set' if self.api_key else 'missing'})"
            )
        text, in_tok, out_tok, latency, finish = self._call(prompt, schema, system, max_output_tokens)
        entry = {
            "key": key,
            "tag": tag,
            "model": self.model,
            "repeat": repeat,
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "latency_s": round(latency, 4),
            "finish_reason": finish,
            "prompt": prompt,
            "text": text,
        }
        self._append(entry)
        usage = Usage(llm_calls=1, input_tokens=in_tok, output_tokens=out_tok, llm_latency_s=latency)
        return LLMReply(data=_parse_json(text), text=text, usage=usage, cached=False, model=self.model, finish_reason=finish)

    def _call(self, prompt: str, schema: dict | None, system: str | None, max_output_tokens: int) -> tuple[str, int, int, float, str]:
        url = f"{API_ROOT}/models/{self.model}:generateContent"
        last_status = None
        for attempt in range(8):
            gen_cfg: dict[str, Any] = {"temperature": 0.0, "maxOutputTokens": max_output_tokens, "responseMimeType": "application/json"}
            if schema:
                gen_cfg["responseSchema"] = schema
            if self._send_thinking_config:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}  # no hidden reasoning tokens: cheaper, faster
            body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen_cfg}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            self._throttle()
            t0 = time.perf_counter()
            try:
                resp = requests.post(url, headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"}, json=body, timeout=config.GEMINI_TIMEOUT_S)
            except (requests.ConnectionError, requests.Timeout):
                time.sleep(min(60, 2 ** attempt * 2))
                continue
            latency = time.perf_counter() - t0
            last_status = resp.status_code
            if resp.status_code == 200:
                js = resp.json()
                cand = (js.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or [{}]
                text = "".join(p.get("text", "") for p in parts)
                um = js.get("usageMetadata", {})
                in_tok = int(um.get("promptTokenCount") or 0)
                out_tok = int(um.get("candidatesTokenCount") or 0) + int(um.get("thoughtsTokenCount") or 0)
                return text, in_tok, out_tok, latency, str(cand.get("finishReason", ""))
            msg = resp.text[:2000]
            if resp.status_code in ACCESS_REFUSED or (resp.status_code == 400 and API_KEY_PROBLEM.search(msg)):
                # Bad or missing key, no permission, unknown or retired model: the LLM cannot be
                # used in this run, so detectors must report "not run" rather than crash.
                raise self._give_up(f"Gemini refused access ({resp.status_code}) for {self.model}: {_error_message(msg)}")
            if resp.status_code == 400 and self._send_thinking_config:
                # Gemini 3.5 models reject thinkingBudget with a generic INVALID_ARGUMENT
                # (they do not think by default); retry once without it.
                self._send_thinking_config = False
                continue
            if resp.status_code == 429:
                if re.search(r"per ?day|PerDay|daily", msg, flags=re.I):
                    raise self._give_up(f"Gemini daily quota exhausted for {self.model}: rerun later, cached answers are kept")
                m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', msg)
                time.sleep(float(m.group(1)) + 1 if m else min(90, 2 ** attempt * 5))
                continue
            if resp.status_code in (500, 502, 503, 504):
                time.sleep(min(60, 2 ** attempt * 3))
                continue
            raise RuntimeError(f"Gemini API error {resp.status_code}: {msg}")
        if last_status == 429:
            # still throttled after every retry: a quota whose message we did not recognise. Stop all
            # further calls in this run (cached answers stay) instead of waiting this out per prompt.
            raise self._give_up(f"Gemini kept answering 429 for {self.model} after {attempt + 1} attempts: rerun later, cached answers are kept")
        raise LLMUnavailable("Gemini API kept failing after retries")

    def _give_up(self, reason: str) -> LLMUnavailable:
        """Trip the circuit breaker and return the exception to raise."""
        self._unavailable = reason
        return LLMUnavailable(reason)

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def list_models(self) -> list[str]:
        if not self.api_key:
            raise LLMUnavailable("GEMINI_API_KEY missing")
        resp = requests.get(f"{API_ROOT}/models", headers={"x-goog-api-key": self.api_key}, params={"pageSize": 1000}, timeout=60)
        resp.raise_for_status()
        return [m["name"].removeprefix("models/") for m in resp.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]


_CLIENT: GeminiClient | None = None


def get_client() -> GeminiClient:
    """Process-wide client (one cache, one rate limiter)."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = GeminiClient()
    return _CLIENT


def set_client(client: GeminiClient | None) -> None:
    """Tests inject a fake client here."""
    global _CLIENT
    _CLIENT = client


# --------------------------------------------------------------------------
# Shared review helpers used by every LLM detector
# --------------------------------------------------------------------------
def _clean_record(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out[k] = None
        elif hasattr(v, "item"):  # numpy scalar
            out[k] = v.item()
        else:
            out[k] = v
    return out


def review_rows(
    task: str,
    records: list[dict],
    *,
    tag: str,
    rows_per_call: int = config.LLM_ROWS_PER_CALL,
    client: GeminiClient | None = None,
    repeat: int | None = None,
    abstained: dict[int, str] | None = None,
) -> tuple[dict[int, dict], Usage, list[str]]:
    """Per-row LLM review. Each record must contain an integer 'id' (any other keys are
    the fields the model may look at). Rows are packed `rows_per_call` per prompt.
    Returns (verdict by id, summed usage, error messages). A row without a verdict is an
    abstention, never a clean row: when `abstained` is given it receives {id: cause} for every
    such row - unreadable_cause(finishReason) for each row of an unreadable reply,
    ABSTAIN_NO_VERDICT for a row a readable reply skipped. Callers mark only rows WITH a verdict
    as reviewed."""
    client = client or get_client()
    verdicts: dict[int, dict] = {}
    usage = Usage()
    errors: list[str] = []
    causes: dict[int, str] = {}
    for start in range(0, len(records), rows_per_call):
        chunk = [_clean_record(r) for r in records[start : start + rows_per_call]]
        prompt = (
            f"{task.strip()}\n\n"
            "Rows to review (JSON Lines, one service request per line):\n"
            + "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in chunk)
            + "\n\nReturn exactly one verdict per row id. For flagged rows give a one-sentence reason, "
            "the observed value(s) that show the problem, and what you expected instead."
        )
        reply = client.generate_json(prompt, ROW_REVIEW_SCHEMA, tag=tag, repeat=repeat)
        usage.add(reply.usage)
        rows = (reply.data or {}).get("rows") if isinstance(reply.data, dict) else None
        if rows is None:
            errors.append(f"unparseable reply for ids {chunk[0]['id']}..{chunk[-1]['id']}"
                          + (f" (finish reason {reply.finish_reason})" if reply.finish_reason else ""))
            for r in chunk:
                causes[int(r["id"])] = unreadable_cause(reply.finish_reason)
            continue
        for v in rows:
            try:
                verdicts[int(v["id"])] = v
            except (KeyError, TypeError, ValueError):
                errors.append(f"bad verdict {v!r}"[:200])
    if abstained is not None:
        for r in records:
            rid = int(r["id"])
            if rid not in verdicts:
                abstained[rid] = causes.get(rid, ABSTAIN_NO_VERDICT)
    return verdicts, usage, errors


def review_batch(task: str, payload: str, *, tag: str, client: GeminiClient | None = None, repeat: int | None = None) -> tuple[dict | None, Usage]:
    """One batch-level LLM review: `payload` is the text the model sees (schema,
    sample rows, ...). Returns (parsed BATCH_REVIEW_SCHEMA dict or None, usage)."""
    client = client or get_client()
    prompt = f"{task.strip()}\n\n{payload.strip()}\n\nAnswer with the JSON schema: issue_found, columns, row_ids, segment, reason, observed, expected, confidence."
    reply = client.generate_json(prompt, BATCH_REVIEW_SCHEMA, tag=tag, repeat=repeat)
    return (reply.data if isinstance(reply.data, dict) else None), reply.usage


# --------------------------------------------------------------------------
# Cache audit: which cached replies held no readable verdict, and why (read-only)
# --------------------------------------------------------------------------
ROWS_MARKER = "Rows to review (JSON Lines"  # review_rows() prompts carry their records after this line
TAIL_EXCERPT_CHARS = 80


def _prompt_row_ids(prompt: str) -> list[int]:
    """The record ids embedded in a review_rows() prompt."""
    ids = []
    for line in prompt.split(ROWS_MARKER, 1)[-1].splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("id"), int):
                ids.append(rec["id"])
    return ids


def longest_json_string(text: str) -> int:
    """Characters in the longest JSON string value of `text`, counting an unterminated last one.
    A reply whose model got stuck repeating itself inside one field (a 'reason' that never ends)
    shows up as one string far longer than any one-sentence reason."""
    best = current = 0
    inside = escaped = False
    for ch in text or "":
        if not inside:
            if ch == '"':
                inside, current = True, 0
        elif escaped:
            escaped, current = False, current + 1
        elif ch == "\\":
            escaped, current = True, current + 1
        elif ch == '"':
            inside, best = False, max(best, current)
        else:
            current += 1
    return max(best, current) if inside else best


def _quantile(values: list[int], q: float) -> int | None:
    return sorted(values)[min(len(values) - 1, int(q * len(values)))] if values else None


def audit_cache(path=None) -> dict:
    """Every cached reply that holds no readable verdict, with the evidence for why: finish
    reason, output tokens, reply length and its longest JSON string value. `readable_longest_string`
    gives that last measure over the readable per-row replies, for comparison. Reads the cache
    file only: it never calls the API and never rewrites the cache."""
    path = path or (config.LLM_CACHE_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', config.GEMINI_DEFAULT_MODEL)}.jsonl")
    entries = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    unreadable, readable_longest = [], []
    for e in entries:
        text, per_row = e.get("text") or "", ROWS_MARKER in e.get("prompt", "")
        data = _parse_json(text)
        if isinstance(data, dict) and (isinstance(data.get("rows"), list) or not per_row):
            if per_row:
                readable_longest.append(longest_json_string(text))
            continue
        ids = _prompt_row_ids(e["prompt"]) if per_row else []
        finish = e.get("finish_reason") or "UNKNOWN"
        unreadable.append({
            "tag": e.get("tag"), "repeat": e.get("repeat"), "finish_reason": finish,
            "meaning": FINISH_REASON_MEANING.get(finish, "unknown finish reason"),
            "output_tokens": e.get("output_tokens"), "chars": len(text), "rows_asked": len(ids),
            "ids": f"{ids[0]}..{ids[-1]}" if ids else None,
            "longest_string_chars": longest_json_string(text),
            "tail": text[-TAIL_EXCERPT_CHARS:],
        })
    return {
        "cache_file": path.name, "n_entries": len(entries), "unreadable": unreadable,
        "readable_longest_string": {"n": len(readable_longest), "median": _quantile(readable_longest, 0.5),
                                    "p99": _quantile(readable_longest, 0.99), "max": max(readable_longest, default=None)},
    }
