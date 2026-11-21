"""Task 6 - project the hybrid pipeline to 50 million rows a day across 120 customers.

Every figure is labelled with where it comes from:
  measured   - read from results/pipeline_summary.json (this laptop, this data)
  assumption - a constant below, chosen by a person and stated with its reason
  projection - arithmetic on the two
A figure that needs a measurement that does not exist yet is reported as "pending", never guessed.

Usage: python -m project_b.scale [--results-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from project_b import config, pipeline

# ---- assumptions (each one is a judgement, shown in the report) ----------------------------
ROWS_PER_DAY = 50_000_000  # from the brief
CUSTOMERS = 120  # from the brief
PEAK_TO_MEAN = 3.0  # busiest hour vs the daily mean (311 traffic is strongly diurnal)
CUSTOMER_SIZE_ZIPF = 1.0  # customer volumes fall like 1/rank: a few large tenants dominate
HEADROOM = 2.0  # run workers at <= 50 % of measured capacity (noisy neighbours, retries)
LLM_CONCURRENCY = 8  # parallel LLM requests per worker pool (a paid tier allows far more)
NOVELTY_DECAY = 1.0  # share of today's escalated values that are new; 1.0 = no cross-day cache (upper bound)
# --------------------------------------------------------------------------------------------
SECONDS_PER_DAY = 86_400


def customer_shares(n: int = CUSTOMERS, s: float = CUSTOMER_SIZE_ZIPF) -> list[float]:
    weights = [1 / (rank ** s) for rank in range(1, n + 1)]
    total = sum(weights)
    return [w / total for w in weights]


def project(pipe: dict | None) -> dict:
    """Projection rows: {name: {"value", "unit", "source", "how"}} (value None = pending)."""
    rows = {}

    def put(name, value, unit, source, how):
        rows[name] = {"value": value, "unit": unit, "source": source, "how": how}

    shares = customer_shares()
    mean_rps = ROWS_PER_DAY / SECONDS_PER_DAY
    put("rows per second, daily mean", mean_rps, "rows/s", "projection", "50 M / 86,400 s")
    put("rows per second, peak hour", mean_rps * PEAK_TO_MEAN, "rows/s", "projection", f"mean x PEAK_TO_MEAN ({PEAK_TO_MEAN})")
    put("batches per day", ROWS_PER_DAY / config.BATCH_SIZE, "batches", "projection", f"50 M / {config.BATCH_SIZE:,}-row batches")
    put("largest customer, rows per day", ROWS_PER_DAY * shares[0], "rows", "projection", f"Zipf s={CUSTOMER_SIZE_ZIPF} over {CUSTOMERS} customers")
    put("smallest customer, rows per day", ROWS_PER_DAY * shares[-1], "rows", "projection", "same; its baselines take longest to learn")
    put("largest customer, batches per hour at peak", ROWS_PER_DAY * shares[0] / 24 * PEAK_TO_MEAN / config.BATCH_SIZE, "batches/h",
        "projection", "its rows/day / 24 x PEAK_TO_MEAN / batch size")

    tp = (pipe or {}).get("throughput", {})
    core_rps = tp.get("rows_per_s_excluding_llm_wait")
    put("local throughput, one laptop core", core_rps, "rows/s", "measured" if core_rps else "pending", "pipeline_summary.json")
    put("cores needed at peak", math.ceil(mean_rps * PEAK_TO_MEAN * HEADROOM / core_rps) if core_rps else None, "cores",
        "projection" if core_rps else "pending", f"peak rows/s x HEADROOM ({HEADROOM}) / measured rows/s per core")
    put("CPU-hours per day", ROWS_PER_DAY / core_rps / 3600 if core_rps else None, "core-h", "projection" if core_rps else "pending",
        "50 M / measured rows/s / 3,600")
    put("compute cost per day", ROWS_PER_DAY / core_rps / 3600 * config.COMPUTE_USD_PER_VCPU_HOUR if core_rps else None, "USD",
        "projection" if core_rps else "pending", f"CPU-hours x ${config.COMPUTE_USD_PER_VCPU_HOUR}/vCPU-h (config)")

    normal = _normal_traffic((pipe or {}).get("llm_by_kind", {}))
    model = ((pipe or {}).get("settings") or {}).get("llm_model")
    price = config.LLM_PRICES_PER_MTOK.get(model or "")
    if normal:
        values_day = normal["values_per_million_rows"] * ROWS_PER_DAY / 1e6 * NOVELTY_DECAY
        calls_day = normal["calls_per_value"] * values_day
        put("LLM values escalated per 1M rows (clean + real batches)", normal["values_per_million_rows"], "values", "measured", "pipeline_summary.json llm_by_kind")
        put("LLM values per day", values_day, "values", "projection", f"rate x 50 M x NOVELTY_DECAY ({NOVELTY_DECAY}): upper bound without a cross-day cache")
        put("LLM requests per day", calls_day, "requests", "projection", "values/day x measured requests per value")
        put("LLM input / output tokens per day", (normal["in_per_value"] * values_day, normal["out_per_value"] * values_day), "tokens", "projection",
            "values/day x measured tokens per value")
        usd = None if price is None else values_day * (normal["in_per_value"] * price["input"] + normal["out_per_value"] * price["output"]) / 1e6
        put("LLM cost per day", usd, "USD", "projection" if usd is not None else "pending", f"tokens x list price for {model} (config, {config.LLM_PRICES_AS_OF})")
        put("LLM requests per day vs free-tier cap", calls_day / config.GEMINI_FREE_TIER_RPD, "x the cap", "projection",
            f"requests/day / {config.GEMINI_FREE_TIER_RPD} (free-tier requests/day, AI Studio rate-limit page)")
        lat = normal["latency_per_call"]
        calls_per_batch = normal["calls_per_value"] * normal["values_per_million_rows"] * config.BATCH_SIZE / 1e6
        if calls_per_batch == 0:  # the measured plan sends nothing to the LLM: there is no wait, not a missing measurement
            put("LLM wait per batch at peak, largest customer", 0.0, "s", "projection", "no LLM request in the measured plan")
        else:
            put("LLM wait per batch at peak, largest customer", calls_per_batch * lat / LLM_CONCURRENCY if lat else None, "s",
                "projection" if lat else "pending", f"requests per batch x measured latency / {LLM_CONCURRENCY} in parallel")
    else:
        for name in ("LLM values escalated per 1M rows (clean + real batches)", "LLM values per day", "LLM requests per day", "LLM cost per day"):
            put(name, None, "", "pending", "needs the pipeline run on clean / real batches")
    return rows


def _normal_traffic(by_kind: dict) -> dict | None:
    """LLM traffic on clean and real (unsanitised) batches: no injected corruption, i.e. a normal day."""
    kinds = [by_kind[k] for k in ("clean", "raw_background") if k in by_kind]
    rows = sum(k["rows"] for k in kinds)
    if not rows:
        return None
    values = sum(k["values_asked"] for k in kinds)
    calls = sum(k["llm_calls"] for k in kinds)
    return {"values_per_million_rows": 1e6 * values / rows,
            "calls_per_value": calls / values if values else 0.0,
            "in_per_value": sum(k["input_tokens"] for k in kinds) / values if values else 0.0,
            "out_per_value": sum(k["output_tokens"] for k in kinds) / values if values else 0.0,
            "latency_per_call": sum(k["llm_latency_s"] for k in kinds) / calls if calls else None}


def render(rows: dict, pipe_source: str | None) -> str:
    lines = ["| quantity | value | source | how |", "|---|---|---|---|"]
    for name, r in rows.items():
        v = r["value"]
        if v is None:
            text = "pending"
        elif isinstance(v, tuple):
            text = " / ".join(f"{x:,.0f}" for x in v) + f" {r['unit']}"
        else:
            text = (f"{v:,.2f}" if v < 100 else f"{v:,.0f}") + f" {r['unit']}"
        lines.append(f"| {name} | {text} | {r['source']} | {r['how']} |")
    head = [f"Pipeline measurements: {pipe_source or 'none yet (run the pipeline evaluation)'}.", "",
            "Assumptions: " + "; ".join(f"{k} = {v}" for k, v in (("PEAK_TO_MEAN", PEAK_TO_MEAN), ("CUSTOMER_SIZE_ZIPF", CUSTOMER_SIZE_ZIPF),
                                                                    ("HEADROOM", HEADROOM), ("LLM_CONCURRENCY", LLM_CONCURRENCY),
                                                                    ("NOVELTY_DECAY", NOVELTY_DECAY))) + ".", ""]
    return "\n".join(head + lines) + "\n"


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(prog="python -m project_b.scale", description="Project the pipeline to 50 M rows/day across 120 customers (Task 6).")
    ap.add_argument("--results-dir", type=Path, default=config.RESULTS_DIR)
    args = ap.parse_args(argv)
    path = args.results_dir / pipeline.SUMMARY_FILE
    pipe = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    rows = project(pipe)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    (args.results_dir / "scale.json").write_text(json.dumps({"assumptions_and_projection": rows}, indent=2, default=list), encoding="utf-8")
    try:  # a repository-relative path in the report, not this machine's folder layout
        shown = path.resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        shown = path.as_posix()
    (args.results_dir / "scale_tables.md").write_text(render(rows, shown if pipe else None), encoding="utf-8")
    return rows


if __name__ == "__main__":
    main()
