"""LLM settings. Every value can be overridden by an environment variable of the same name.

The API key is never stored here: it is read from the GEMINI_API_KEY environment variable,
which `load_dotenv()` can set from the repo-root `.env` file. Nothing in this module prints it.
"""
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOTENV = REPO_ROOT / ".env"
KEY_VAR = "GEMINI_API_KEY"

# Model per role. Override one role with GEMINI_MODEL_<ROLE> (e.g. GEMINI_MODEL_PARSER), or every
# role with GEMINI_MODEL. The model name is part of the cache key, so a change never replays answers
# recorded with another model. History: 2026-11-20 gemini-2.5-flash was listed but returned 404 ("no
# longer available to new users"); 3.8-flash and 3.7-flash returned 503 "high demand"; gemini-3.5-flash
# answered, then hit its free-tier limit of 20 requests/day (the error named gemini-3.6-flash).
ROLES = ("parser", "query_engine", "compiler")
# 2026-11-20 probes (one request each): gemini-3.5-flash-lite and gemini-3.1-flash-lite answered and
# both returned strict JSON obeying a system instruction; gemini-2.5-flash-lite 404 (no longer
# available to new users); gemma-4-26b-a4b-it 503; gemma-4-31b-it answered once, then 503 (JSON and
# system instruction unverified). Daily limits of the lite models were not stated anywhere.
# Each role gets its own model where possible so the per-model daily quotas do not compete.
ROLE_DEFAULTS = {
    "parser": "gemini-3.5-flash-lite",
    "query_engine": "gemini-3.1-flash-lite",
    "compiler": "gemini-3.5-flash-lite",     # few calls; revisit if gemma-4-31b-it is verified
}

DEFAULTS = {
    "GEMINI_MODEL": "",                 # empty: use the role's default above
    "GEMINI_API_BASE": "https://generativelanguage.googleapis.com/v1beta",
    "LLM_RPM": "8",                     # client-side requests-per-minute limit (free tier is low)
    "LLM_MAX_RETRIES": "5",
    "LLM_TIMEOUT_S": "120",
    "LLM_TEMPERATURE": "0",
    "LLM_MODE": "auto",                 # live | replay | auto
    "LLM_CACHE_DIR": str(REPO_ROOT / "llm_cache"),
    # Optional overrides of the per-model prices below (US$ per million tokens); empty = use MODEL_PRICES.
    "LLM_PRICE_INPUT_PER_M": "",
    "LLM_PRICE_OUTPUT_PER_M": "",
}

# NOTIONAL paid-tier prices, US$ per million tokens (input, output), from the official page
# https://ai.google.dev/gemini-api/docs/pricing, checked 2026-11-21. The free tier used in this project
# costs $0; these only turn recorded token counts into a comparable cost figure. A model not listed
# here has no price on file and costs 0.0 unless the LLM_PRICE_* overrides are set.
PRICES_CHECKED = "2026-11-21, https://ai.google.dev/gemini-api/docs/pricing"
MODEL_PRICES = {
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}


@dataclass(frozen=True)
class LLMConfig:
    model: str
    role: str
    api_base: str
    rpm: float
    max_retries: int
    timeout_s: float
    temperature: float
    mode: str
    cache_dir: Path
    price_input_per_m: float        # notional paid-tier price (see MODEL_PRICES)
    price_output_per_m: float       # notional paid-tier price

    def notional_cost(self, prompt_tokens, output_tokens):
        return (prompt_tokens * self.price_input_per_m + output_tokens * self.price_output_per_m) / 1e6


def model_for(role):
    if role not in ROLES:
        raise ValueError("unknown LLM role %r (expected one of %s)" % (role, ROLES))
    return (os.environ.get("GEMINI_MODEL_" + role.upper()) or os.environ.get("GEMINI_MODEL")
            or ROLE_DEFAULTS[role])


def get_config(role="query_engine"):
    """Read the settings now (so tests and callers can override them through the environment)."""
    v = {k: os.environ.get(k, d) for k, d in DEFAULTS.items()}
    mode = v["LLM_MODE"].strip().lower()
    if mode not in ("live", "replay", "auto"):
        raise ValueError("LLM_MODE must be live, replay or auto (got %r)" % mode)
    model = model_for(role)
    pin, pout = MODEL_PRICES.get(model, (0.0, 0.0))
    return LLMConfig(
        model=model, role=role, api_base=v["GEMINI_API_BASE"].rstrip("/"),
        rpm=float(v["LLM_RPM"]), max_retries=int(v["LLM_MAX_RETRIES"]),
        timeout_s=float(v["LLM_TIMEOUT_S"]), temperature=float(v["LLM_TEMPERATURE"]), mode=mode,
        cache_dir=Path(v["LLM_CACHE_DIR"]),
        price_input_per_m=float(v["LLM_PRICE_INPUT_PER_M"] or pin),
        price_output_per_m=float(v["LLM_PRICE_OUTPUT_PER_M"] or pout),
    )


def load_dotenv(path=DOTENV):
    """Set GEMINI_API_KEY from a KEY=VALUE file if it is not already set. Sets nothing else, prints nothing."""
    if os.environ.get(KEY_VAR) or not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == KEY_VAR:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                os.environ[KEY_VAR] = value
            return
