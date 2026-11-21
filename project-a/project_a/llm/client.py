"""Gemini REST client: stdlib urllib only, client-side rate limit, retries, record/replay cache.

The API key travels only in the x-goog-api-key header. It is redacted from every error message,
and cache entries never contain request headers.

Modes (LLM_MODE): live = call the API and record; replay = cache only, a miss raises CacheMiss and
the network is never touched; auto = replay if cached, else live.

Run `python3 -m project_a.llm.client` to list the models that support generateContent.
"""
import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from project_a.llm.config import KEY_VAR, get_config, load_dotenv

REDACTED = "[REDACTED]"
MAX_REPORTED_WAIT_S = 120        # a longer reported retry delay means a quota is exhausted: stop, don't wait


class LLMError(Exception):
    pass


class CacheMiss(LLMError):
    pass


class QuotaExhausted(LLMError):
    """HTTP 429 that retrying will not fix now (retries used up, or the API asks for a long wait)."""


# ---- usage log: one line per live request, so the free-tier quota can be budgeted ----------------
USAGE_FILE = "usage.jsonl"
try:
    from zoneinfo import ZoneInfo
    QUOTA_TZ = ZoneInfo("America/Los_Angeles")      # Gemini daily quotas reset at midnight Pacific
except Exception:                                    # no tz database: fall back to UTC days
    QUOTA_TZ = timezone.utc


def quota_day(ts=None):
    return (ts or datetime.now(timezone.utc)).astimezone(QUOTA_TZ).date().isoformat()


def calls_today(model, cache_dir=None):
    """{'requests': n, 'ok': m} for `model` on the current quota day, from usage.jsonl."""
    path = (cache_dir or get_config().cache_dir) / USAGE_FILE
    out = {"requests": 0, "ok": 0}
    if path.exists():
        today = quota_day()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            u = json.loads(line)
            if u.get("model") == model and u.get("quota_day") == today:
                out["requests"] += 1
                out["ok"] += u.get("status") == 200
    return out


def calls_today_line(model, cache_dir=None):
    c = calls_today(model, cache_dir)
    return "calls today on %s: %d requests (%d OK) [quota day %s, Pacific time]" % (
        model, c["requests"], c["ok"], quota_day())


class TransportError(Exception):
    """Network-level failure (no HTTP status): timeout, DNS, connection reset."""


def urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise TransportError(str(e)) from None


@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int
    output_tokens: int            # candidates + thinking tokens (both are billed as output)
    thoughts_tokens: int
    latency_s: float              # round trip of the successful request (original value on a cache hit)
    cached: bool
    cache_key: str
    finish_reason: str
    raw: dict


def cache_key(model, system, prompt, temperature, run_index):
    blob = json.dumps({"model": model, "system": system, "prompt": prompt,
                       "temperature": temperature, "run_index": run_index},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _reported_delay(headers, body):
    """Seconds the API asked us to wait (Retry-After header or google.rpc.RetryInfo), or None."""
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            try:
                return float(v)
            except ValueError:
                pass
    try:
        details = json.loads(body.decode("utf-8", "replace")).get("error", {}).get("details", [])
    except (ValueError, AttributeError):
        return None
    for d in details:
        if isinstance(d, dict) and d.get("@type", "").endswith("RetryInfo"):
            m = re.fullmatch(r"([\d.]+)s", str(d.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    return None


class GeminiClient:
    def __init__(self, config=None, transport=None, sleep=time.sleep, clock=time.monotonic, api_key=None,
                 role="query_engine"):
        self.config = config or get_config(role)
        self.transport = transport or urllib_transport
        self.sleep, self.clock = sleep, clock
        if api_key is None:
            load_dotenv()
            api_key = os.environ.get(KEY_VAR)
        self._key = api_key or None
        self._last_send = None
        self.stats = {"network_calls": 0, "cache_hits": 0, "retries": 0, "http_429": 0,
                      "http_5xx": 0, "reported_delays_s": []}

    # ---- helpers -------------------------------------------------------------------------------
    def redact(self, text):
        text = str(text)
        return text.replace(self._key, REDACTED) if self._key else text

    def _error(self, cls, msg):
        return cls(self.redact(msg))

    @property
    def model(self):
        m = self.config.model
        return m[len("models/"):] if m.startswith("models/") else m

    def _throttle(self):
        interval = 60.0 / self.config.rpm if self.config.rpm > 0 else 0.0
        if self._last_send is not None:
            wait = self._last_send + interval - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last_send = self.clock()

    def _log_usage(self, path, status):
        m = re.match(r"/models/([^:/?]+):(\w+)", path)
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "quota_day": quota_day(),
                 "model": m.group(1) if m else "-", "method": m.group(2) if m else path.split("?")[0].strip("/"),
                 "status": status}
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config.cache_dir / USAGE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def calls_today_line(self):
        return calls_today_line(self.model, self.config.cache_dir)

    def _request(self, method, path, payload=None):
        """Returns (parsed JSON body, latency of the successful attempt)."""
        if not self._key:
            raise LLMError("%s is not set (put it in .env or the environment)" % KEY_VAR)
        url = self.config.api_base + path
        headers = {"x-goog-api-key": self._key, "Content-Type": "application/json"}
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        for attempt in range(self.config.max_retries + 1):
            last = attempt == self.config.max_retries
            self._throttle()
            self.stats["network_calls"] += 1
            t0 = time.perf_counter()
            try:
                status, resp_headers, resp_body = self.transport(method, url, headers, body,
                                                                 self.config.timeout_s)
                self._log_usage(path, status)
            except TransportError as e:
                self._log_usage(path, "transport_error")
                if last:
                    raise self._error(LLMError, "network error after %d attempts: %s" % (attempt + 1, e))
                self.stats["retries"] += 1
                self.sleep(min(60.0, 2.0 * 2 ** attempt))
                continue
            latency = time.perf_counter() - t0
            if status == 200:
                try:
                    return json.loads(resp_body.decode("utf-8")), latency
                except ValueError:
                    raise self._error(LLMError, "HTTP 200 with a non-JSON body: %r" % resp_body[:300])
            text = resp_body.decode("utf-8", "replace")[:1500]
            if status == 429 or 500 <= status < 600:
                self.stats["http_429" if status == 429 else "http_5xx"] += 1
                reported = _reported_delay(resp_headers, resp_body)
                if reported is not None:
                    self.stats["reported_delays_s"].append(reported)
                if last or (reported is not None and reported > MAX_REPORTED_WAIT_S):
                    raise self._error(QuotaExhausted if status == 429 else LLMError,
                                      "HTTP %d after %d attempt(s); API retry delay %s s: %s"
                                      % (status, attempt + 1, reported, text))
                self.stats["retries"] += 1
                self.sleep(reported if reported is not None else min(60.0, 2.0 * 2 ** attempt))
                continue
            raise self._error(LLMError, "HTTP %d: %s" % (status, text))
        raise AssertionError("unreachable")

    # ---- API -----------------------------------------------------------------------------------
    def list_models(self):
        models, token = [], None
        while True:
            path = "/models?pageSize=1000" + ("&pageToken=" + token if token else "")
            data, _ = self._request("GET", path)
            models += data.get("models", [])
            token = data.get("nextPageToken")
            if not token:
                return models

    def generate(self, prompt, system=None, json_mode=True, run_index=0):
        if not self.model:
            raise LLMError("no model configured: set GEMINI_MODEL or the default in llm/config.py")
        cfg = self.config
        key = cache_key(self.model, system, prompt, cfg.temperature, run_index)
        path = cfg.cache_dir / (key + ".json")
        if cfg.mode in ("replay", "auto") and path.exists():
            self.stats["cache_hits"] += 1
            return self._from_entry(json.loads(path.read_text(encoding="utf-8")), cached=True)
        if cfg.mode == "replay":
            raise CacheMiss("replay mode: no cache entry %s for model %s (run_index %d); "
                            "record it with LLM_MODE=live or auto" % (key[:12], self.model, run_index))

        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"temperature": cfg.temperature}}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        raw, latency = self._request("POST", "/models/%s:generateContent" % self.model, payload)

        usage = raw.get("usageMetadata", {})
        entry = {
            "cache_key": key, "model": self.model, "temperature": cfg.temperature,
            "run_index": run_index, "json_mode": json_mode, "system": system, "prompt": prompt,
            "raw_response": raw, "prompt_tokens": usage.get("promptTokenCount", 0),
            "output_tokens": usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
            "thoughts_tokens": usage.get("thoughtsTokenCount", 0), "latency_s": latency,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        blob = self.redact(json.dumps(entry, ensure_ascii=False, indent=1))   # belt and braces
        tmp = path.with_suffix(".tmp")
        tmp.write_text(blob, encoding="utf-8")
        tmp.replace(path)
        return self._from_entry(entry, cached=False)

    @staticmethod
    def _from_entry(entry, cached):
        raw = entry["raw_response"]
        cands = raw.get("candidates") or [{}]
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return LLMResponse(text=text, prompt_tokens=entry["prompt_tokens"],
                           output_tokens=entry["output_tokens"], thoughts_tokens=entry["thoughts_tokens"],
                           latency_s=entry["latency_s"], cached=cached, cache_key=entry["cache_key"],
                           finish_reason=cands[0].get("finishReason", ""), raw=raw)


if __name__ == "__main__":
    client = GeminiClient()
    try:
        for m in client.list_models():
            if "generateContent" in m.get("supportedGenerationMethods", []):
                print(m["name"])
    except LLMError as e:
        raise SystemExit(str(e))
