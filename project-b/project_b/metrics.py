"""Metric definitions used by the evaluation harness (DESIGN.md section 6).

Why a separate module: every reported number is produced by one of these small, pure
functions, so a definition can be read, unit-tested and changed in one place without
running the harness.

Conventions
* A rate is a dict {"value", "ci", "k", "n"}: k successes out of n, with a Wilson 95 %
  interval. With n == 0, value and ci are None: something not measured is never "0 %".
* Row-level counts are a confusion dict {"tp", "fp", "fn", "tn"} over REVIEWED rows only
  (an LLM detector reviews a sample, rule / stat detectors review every row).
"""
from __future__ import annotations

import math
import re
import statistics

import numpy as np
import pandas as pd

from project_b import config
from project_b.core import Alert

Z_95 = 1.959963984540054  # two-sided 95 % normal quantile
COUNT_KEYS = ("tp", "fp", "fn", "tn")
# Schema problems break a whole column, so naming the column fully localises them; every
# other problem must also point at rows or a segment (e.g. an agency).
WHOLE_COLUMN_PROBLEMS = {"a", "b"}
MIN_QUOTE_CHARS = 3  # grounding: shorter quotes / values would match almost any row by chance
# Gemini free-tier requests per day: an unverified placeholder that lives in config so every
# rate-limit assumption is in one place (see config.GEMINI_FREE_TIER_RPD, env GEMINI_FREE_RPD).
FREE_TIER_REQUESTS_PER_DAY = config.GEMINI_FREE_TIER_RPD
MINUTES_PER_DAY = 24 * 60


# --------------------------------------------------------------------------
# Rates with confidence intervals
# --------------------------------------------------------------------------
def wilson_ci(k: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval for k successes in n trials. Unlike the normal approximation it
    stays inside [0, 1] and is informative at k = 0 or k = n: "0 false alarms in 20 batches"
    still has an upper bound of 16 %."""
    if n <= 0:
        return None
    p = k / n
    shrink = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / shrink
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / shrink
    return max(0.0, centre - half), min(1.0, centre + half)


def proportion(k: int, n: int) -> dict:
    """k out of n as {"value", "ci", "k", "n"}."""
    ci = wilson_ci(k, n)
    return {"value": k / n if n > 0 else None, "ci": list(ci) if ci else None, "k": int(k), "n": int(n)}


# --------------------------------------------------------------------------
# Detection and false alarms
# --------------------------------------------------------------------------
def confusion(flags: np.ndarray | None, labels: np.ndarray, reviewed: np.ndarray) -> dict[str, int]:
    """Confusion counts over the reviewed rows. `flags` None means the detector gave no row
    verdicts (it crashed): it flagged nothing, so every injected row it should have caught
    is a miss."""
    labels = np.asarray(labels, dtype=bool)
    reviewed = np.asarray(reviewed, dtype=bool)
    flags = np.zeros_like(labels) if flags is None else np.asarray(flags, dtype=bool)
    return {
        "tp": int((flags & labels & reviewed).sum()),
        "fp": int((flags & ~labels & reviewed).sum()),
        "fn": int((~flags & labels & reviewed).sum()),
        "tn": int((~flags & ~labels & reviewed).sum()),
    }


def sum_counts(counts: list[dict]) -> dict[str, int]:
    """Pool confusion counts (any dicts carrying tp / fp / fn / tn, e.g. records)."""
    return {key: sum(c[key] for c in counts) for key in COUNT_KEYS}


def precision_at_rate(recall: float | None, fpr: float | None, rate: float) -> float | None:
    """Precision if a share `rate` of the rows were corrupted:
    rate * recall / (rate * recall + (1 - rate) * fpr).
    Reweighting by the injection rate makes an LLM's enriched (stratified) sample comparable
    with full batches; on a full batch whose injected share is `rate` it equals tp / (tp + fp)."""
    if recall is None or fpr is None:
        return None
    hits, false_alarms = rate * recall, (1 - rate) * fpr
    return hits / (hits + false_alarms) if hits + false_alarms > 0 else None


def row_metrics(corrupted: dict, clean: dict, rate: float) -> dict:
    """Row-level problems, from counts pooled over corrupted and over clean batches:
    detection = recall on injected rows; row FPR = flagged share of clean-batch rows (every
    flag there is false); precision at the injection rate from recall and the false-positive
    rate on the non-injected rows of corrupted batches."""
    detection = proportion(corrupted["tp"], corrupted["tp"] + corrupted["fn"])
    fpr_corrupted = proportion(corrupted["fp"], corrupted["fp"] + corrupted["tn"])
    return {
        "detection": detection,
        "row_fpr": proportion(clean["fp"], clean["fp"] + clean["tn"]),
        "precision_at_rate": precision_at_rate(detection["value"], fpr_corrupted["value"], rate),
    }


def batch_metrics(corrupted_flags: list[bool], clean_flags: list[bool]) -> dict:
    """Share of corrupted batches with an alert (detection) and of clean batches with an
    alert (false-alarm rate, FAR)."""
    return {
        "batch_detection": proportion(sum(corrupted_flags), len(corrupted_flags)),
        "batch_far": proportion(sum(clean_flags), len(clean_flags)),
    }


def localisation_ok(alerts: list[dict], params: dict, batch_columns) -> bool:
    """Does at least one alert name a corrupted column and, for a one-agency corruption (g),
    that agency? A renamed column may be named by its new name: the batch column that is not
    in the contract. Column names match ignoring case and punctuation ("Incident Zip" ==
    "incident_zip"); the agency must appear as a whole word in the alert's segment. Alerts come
    from other people's detectors, so a segment or column list of the wrong type is read, not fatal."""
    renamed = set(batch_columns) - set(config.COLUMNS)
    targets = {_name_key(c) for c in [*(params.get("columns") or []), *renamed]}
    agency = params.get("agency")
    for alert in alerts:
        segment = alert.get("segment") or {}
        segment_values = segment.values() if isinstance(segment, dict) else [segment]
        names_column = any(_name_key(c) in targets for c in _as_list(alert.get("columns")))
        names_agency = agency is None or any(_mentions(v, agency) for v in segment_values)
        if names_column and names_agency:
            return True
    return False


def _as_list(value) -> list:
    """None -> [], one name -> [name], a collection -> its items."""
    if value is None:
        return []
    return [value] if isinstance(value, str) or not hasattr(value, "__iter__") else list(value)


def _name_key(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _mentions(text, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", str(text), flags=re.IGNORECASE) is not None


# --------------------------------------------------------------------------
# Explanations
# --------------------------------------------------------------------------
def clarity_score(alert: Alert | dict) -> int:
    """Explanation clarity 0-5, one point each: names the check (check_id or problem);
    localises it (columns plus example rows or a segment; the column alone for whole-column
    schema problems); shows the observed value; shows the expected value / baseline;
    reproducible (deterministic)."""
    a = alert.to_dict() if isinstance(alert, Alert) else alert
    pinpointed = _shown(a.get("rows")) or _shown(a.get("segment")) or a.get("problem") in WHOLE_COLUMN_PROBLEMS
    points = [
        bool(a.get("check_id") or a.get("problem")),
        _shown(a.get("columns")) and pinpointed,
        _shown(a.get("observed")),
        _shown(a.get("expected")),
        bool(a.get("deterministic")),
    ]
    return sum(points)


def _shown(value) -> bool:
    """Missing or empty values are not shown; 0 and False are real values. A list or dict shows
    something only if one of its entries does: {"text": ""} or {12: None} shows nothing."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_shown(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_shown(v) for v in value)
    return value is not None


def is_grounded(observed, row_values: list) -> bool:
    """Does the model's `observed` evidence quote the row? True when the text is part of one
    of the row's values, or contains one of them ("zip 10031, borough BRONX" quotes two).
    Case and whitespace are ignored. Deliberately lenient - an upper bound on faithfulness:
    evidence that fails even this check was not taken from the row."""
    quote = _normalise(observed)
    if len(quote) < MIN_QUOTE_CHARS:
        return False
    values = [_normalise(v) for v in row_values]
    return any(quote in v or (len(v) >= MIN_QUOTE_CHARS and v in quote) for v in values)


def grounding_counts(batch: pd.DataFrame, flagged_positions, row_evidence: dict) -> dict[str, int]:
    """{"grounded", "flagged"}: how many flagged rows (int positions) carry `observed`
    evidence that quotes the row. A flagged row without evidence (or with evidence that is
    not a {"observed": ...} dict) is not grounded."""
    grounded = 0
    for pos in flagged_positions:
        evidence = row_evidence.get(int(pos))
        observed = evidence.get("observed") if isinstance(evidence, dict) else None
        grounded += is_grounded(observed, batch.iloc[int(pos)].dropna().tolist())
    return {"grounded": grounded, "flagged": len(flagged_positions)}


def _normalise(value) -> str:
    return "" if value is None else " ".join(str(value).lower().split())


# --------------------------------------------------------------------------
# Cost, throughput, determinism
# --------------------------------------------------------------------------
def cost_per_million_rows(
    rows: int, cpu_seconds: float = 0.0, input_tokens: int = 0, output_tokens: int = 0, model: str | None = None
) -> dict:
    """USD to check one million rows = compute + LLM tokens.
    compute: single-core seconds per row x 1e6 / 3600 x config.COMPUTE_USD_PER_VCPU_HOUR
    LLM:     tokens per row x list price per 1M tokens (config.LLM_PRICES_PER_MTOK[model])
    `rows` = rows the verdicts cover: every row (rule / stat), the reviewed rows (per-row and
    window review) or whole batches (batch review). No list price for `model`: usd is None."""
    if rows <= 0:
        return {"usd": None, "compute_usd": None, "llm_usd": None, "note": "no rows measured"}
    compute_usd = cpu_seconds / rows * 1e6 / 3600 * config.COMPUTE_USD_PER_VCPU_HOUR
    llm_usd = 0.0
    if input_tokens or output_tokens:
        price = config.LLM_PRICES_PER_MTOK.get(model or "")
        if price is None:
            note = f"no list price for model {model!r} in config.LLM_PRICES_PER_MTOK"
            return {"usd": None, "compute_usd": compute_usd, "llm_usd": None, "note": note}
        llm_usd = (input_tokens * price["input"] + output_tokens * price["output"]) / rows
    return {"usd": compute_usd + llm_usd, "compute_usd": compute_usd, "llm_usd": llm_usd, "note": None}


def rows_per_second(n_rows: list[int], wall_s: list[float]) -> float | None:
    """Rule / stat throughput: median over batches of rows / detect() wall seconds."""
    speeds = [n / w for n, w in zip(n_rows, wall_s) if w > 0]
    return statistics.median(speeds) if speeds else None


def llm_throughput(rows: int, calls: int, call_latencies_s: list[float]) -> dict:
    """LLM throughput with one request in flight: rows per call / median API latency; and the
    free-tier ceiling: rows per call x requests allowed per day (per-minute cap or daily quota)."""
    if calls <= 0 or not call_latencies_s:
        return {"rows_per_call": None, "median_latency_s": None, "rows_per_s": None, "free_tier_rows_per_day": None}
    rows_per_call = rows / calls
    latency = statistics.median(call_latencies_s)
    requests_per_day = min(config.GEMINI_RPM * MINUTES_PER_DAY, FREE_TIER_REQUESTS_PER_DAY)
    return {
        "rows_per_call": rows_per_call,
        "median_latency_s": latency,
        "rows_per_s": rows_per_call / latency if latency > 0 else None,
        "free_tier_rows_per_day": rows_per_call * requests_per_day,
    }


def determinism_agreement(flag_sets: list[set], n_items: int) -> dict:
    """Share of items (reviewed rows, or a single batch verdict) on which all repeated runs
    agree; an item disagrees when some repeats flagged it and others did not. Every flagged
    item is an item, so n is at least the number of distinct flagged items."""
    if not flag_sets:
        return proportion(0, 0)
    flagged_by_any = set.union(*flag_sets)
    unstable = flagged_by_any - set.intersection(*flag_sets)
    n_items = max(n_items, len(flagged_by_any))
    return proportion(n_items - len(unstable), n_items)
