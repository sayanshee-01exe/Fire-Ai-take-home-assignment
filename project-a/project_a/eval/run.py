"""Runs golden cases through an engine and compares with the oracle's expected values.

    python3 -m project_a.eval.run --engine llm_query --split dev --limit 10 [--repeats R]
    python3 -m project_a.eval.run --engine llm_compiled --artifact reviewed --split official

Splits: dev, heldout (golden.jsonl) and official (the 30 official-calculator cases, expected values
from the calculator). The held-out split is refused unless --allow-heldout is passed.
LLM calls follow LLM_MODE (live | replay | auto). Writes a JSON file under project_a/eval/results/.
"""
import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from project_a.core.schema import TaxInput
from project_a.eval import RESULTS_DIR
from project_a.llm.config import PRICES_CHECKED
from project_a.engines.hand_coded import HandCodedEngine

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parent / "tests" / "cases" / "golden.jsonl"
OFFICIAL = HERE.parent / "tests" / "cases" / "official_30.jsonl"
FIELDS = ("old_total_income", "old_tax", "new_total_income", "new_tax")
SUITES = {  # case set per financial year: (golden suite, official rows)
    "fy2024_25": (GOLDEN, OFFICIAL),
    "fy2025_26": (HERE.parent / "tests" / "cases" / "golden_fy2025_26.jsonl",
                  HERE.parent / "tests" / "cases" / "official_26.jsonl"),
}


def load_cases(split, limit=None, suite="fy2024_25"):
    golden, official = SUITES[suite]
    if split == "official":
        with open(official, encoding="utf-8") as f:
            cases = [json.loads(l) for l in f if l.strip()]
    else:
        with open(golden, encoding="utf-8") as f:
            cases = [c for c in (json.loads(l) for l in f if l.strip()) if split == "all" or c["split"] == split]
    return cases[:limit] if limit else cases


def _got(res):
    return {"old_total_income": res.old.total_income, "old_tax": res.old.tax,
            "new_total_income": res.new.total_income, "new_tax": res.new.tax, "best": res.best}


def _within(got, exp, tol_abs=None, tol_rel=None):
    if got is None:
        return False
    d = abs(got - exp)
    return d <= tol_abs if tol_abs is not None else d <= tol_rel * exp


def p95(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def run(engine_name, split, limit=None, repeats=1, client=None, out_dir=RESULTS_DIR, artifact="reviewed",
        compiled_dir=None, allow_helpers=False, suite="fy2024_25"):
    cases = load_cases(split, limit, suite)
    compiled = None
    if engine_name == "llm_compiled":
        from project_a.engines.llm_compiled import COMPILED, LLMCompiledEngine
        default_dir = COMPILED if suite == "fy2024_25" else COMPILED / suite
        compiled = LLMCompiledEngine(artifact, compiled_dir or default_dir, allow_helpers=allow_helpers)
        engines = [compiled] * repeats
    elif engine_name == "llm_query":
        from project_a.engines.llm_query import LLMQueryEngine
        from project_a.llm.client import GeminiClient
        client = client or GeminiClient(role="query_engine")
        engines = [LLMQueryEngine(client, run_index=r) for r in range(repeats)]
    elif engine_name == "hand_coded":
        engines = [HandCodedEngine()] * repeats
    else:
        raise SystemExit("unknown engine %r" % engine_name)

    from project_a.llm.client import CacheMiss, LLMError, QuotaExhausted
    rows, not_run, quota_error, stop_status = [], [], None, None
    for case in cases:
        for r, engine in enumerate(engines):
            if quota_error:
                not_run.append({"id": case["id"], "run_index": r, "status": stop_status})
                continue
            t0 = time.perf_counter()
            try:
                res = engine.calculate(TaxInput.from_dict(case["input"]))
            except QuotaExhausted as e:              # keep what was done; never crash on a quota
                quota_error, stop_status = str(e), "not run: quota"
                not_run.append({"id": case["id"], "run_index": r, "status": stop_status})
                continue
            except LLMError as e:                    # network failure after retries: same, stop cleanly
                if isinstance(e, CacheMiss):
                    raise
                quota_error, stop_status = str(e), "not run: network error"
                not_run.append({"id": case["id"], "run_index": r, "status": stop_status})
                continue
            wall = time.perf_counter() - t0
            got = _got(res)
            exp = case["expected"]
            err = getattr(res, "error", None)
            rows.append({
                "id": case["id"], "category": case["category"], "split": case.get("split"), "run_index": r,
                "expected": exp, "got": got, "error": err,
                "raw_output": getattr(res, "raw_output", None) if err else None,
                "tax_exact": not err and all(got[k] == exp[k] for k in ("old_tax", "new_tax")),
                "tax_within_10": not err and all(_within(got[k], exp[k], tol_abs=10) for k in ("old_tax", "new_tax")),
                "tax_within_1pct": not err and all(_within(got[k], exp[k], tol_rel=0.01) for k in ("old_tax", "new_tax")),
                "income_exact": not err and all(got[k] == exp[k] for k in ("old_total_income", "new_total_income")),
                "best_match": not err and got["best"] == exp["best"],
                "latency_s": getattr(res, "latency_s", wall),
                "usage": getattr(res, "usage", {}), "cached": getattr(res, "cached", False),
                "llm_trace": getattr(res, "llm_trace", None),
                "compute_s": getattr(res, "compute_s", None),
            })

    n = len(rows)
    lat = [r["latency_s"] for r in rows]
    ptok = sum(r["usage"].get("prompt_tokens", 0) for r in rows)
    otok = sum(r["usage"].get("output_tokens", 0) for r in rows)
    summary = {
        "engine": engine_name, "suite": suite, "split": split, "cases": len(cases), "repeats": repeats, "runs": n,
        "tax_exact": sum(r["tax_exact"] for r in rows),
        "tax_within_10": sum(r["tax_within_10"] for r in rows),
        "tax_within_1pct": sum(r["tax_within_1pct"] for r in rows),
        "income_exact": sum(r["income_exact"] for r in rows),
        "best_match": sum(r["best_match"] for r in rows),
        "unparseable": sum(1 for r in rows if r["error"]),
        "latency_mean_s": sum(lat) / n if n else 0.0, "latency_p95_s": p95(lat),
        "prompt_tokens": ptok, "output_tokens": otok,
        "cache_hits": sum(r["cached"] for r in rows),
        "incomplete": bool(not_run), "not_run": len(not_run), "quota_error": quota_error,
    }
    if client is not None:
        cfg = client.config
        summary.update(model=client.model, mode=cfg.mode, client_stats=dict(client.stats),
                       notional_cost_per_1000_usd=(cfg.notional_cost(ptok, otok) / n * 1000 if n else 0.0),
                       notional_prices_per_m_usd={"input": cfg.price_input_per_m,
                                                  "output": cfg.price_output_per_m, "source": PRICES_CHECKED})
    if compiled is not None:
        comp = [r["compute_s"] for r in rows if r["compute_s"]]
        m = compiled.artifact_meta
        summary.update(artifact=compiled.artifact, mode="no-llm", query_tokens=0, diagnostic_allow_helpers=allow_helpers,
                       allow_list_violations=compiled.violations,
                       compute_mean_s=sum(comp) / len(comp) if comp else 0.0, compute_p95_s=p95(comp),
                       compile_cost={"one_time": True, "model": m.get("model"), "prompt_tokens": m.get("prompt_tokens"),
                                     "output_tokens": m.get("output_tokens"), "latency_s": m.get("latency_s"),
                                     "note": "cost of the raw compile this artifact derives from"
                                             + (" (reviewed: plus human review time)" if compiled.artifact == "reviewed" else "")})
        compiled.close()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = engine_name + ("-" + compiled.artifact + ("-DIAG-helpers" if allow_helpers else "") if compiled is not None else "")
    split_label = split if suite == "fy2024_25" else "%s-%s" % (suite, split)
    out = out_dir / ("%s_%s_n%d_r%d_%s_%s.json" % (label, split_label, len(cases), repeats,
                                                    summary.get("mode", "na"), stamp))
    out.write_text(json.dumps({"summary": summary, "rows": rows, "not_run": not_run}, indent=1,
                              ensure_ascii=False), encoding="utf-8")
    summary["results_file"] = str(out)
    if client is not None:
        summary["calls_today"] = client.calls_today_line()
    return summary


def print_summary(s):
    n = s["runs"]
    pct = lambda k: "%d/%d (%.1f%%)" % (s[k], n, 100.0 * s[k] / n if n else 0)
    print("engine %s, suite %s, split %s, %d cases x %d repeats = %d runs%s" % (
        s["engine"], s.get("suite", "fy2024_25"), s["split"], s["cases"], s["repeats"], n,
        ", model %s, mode %s" % (s["model"], s["mode"]) if "model" in s else ""))
    print("tax exact match (both regimes):  ", pct("tax_exact"))
    print("tax within Rs 10 (both regimes): ", pct("tax_within_10"))
    print("tax within 1% (both regimes):    ", pct("tax_within_1pct"))
    print("taxable income exact (both):     ", pct("income_exact"))
    print("best regime matches:             ", pct("best_match"))
    print("unparseable outputs:             ", s["unparseable"])
    if "artifact" in s:
        print("artifact:                         %s (no LLM call at query time; tokens 0)%s" % (
            s["artifact"], "  [DIAGNOSTIC: helper functions allowed]" if s["diagnostic_allow_helpers"] else ""))
        if s["allow_list_violations"]:
            print("allow-list:                       REJECTED, %d violations (every query is an error result)"
                  % len(s["allow_list_violations"]))
        print("latency mean / p95 (round trip):  %.0f / %.0f us" % (s["latency_mean_s"] * 1e6, s["latency_p95_s"] * 1e6))
        print("compute() time mean / p95:        %.1f / %.1f us" % (s["compute_mean_s"] * 1e6, s["compute_p95_s"] * 1e6))
        c = s["compile_cost"]
        print("one-time compile cost:            model %s, tokens %s / %s, %s s" % (
            c["model"], c["prompt_tokens"], c["output_tokens"],
            "%.1f" % c["latency_s"] if c["latency_s"] is not None else "n/a"))
    else:
        print("latency mean / p95 (s):           %.2f / %.2f" % (s["latency_mean_s"], s["latency_p95_s"]))
    print("tokens prompt / output (total):   %d / %d (mean %.0f / %.0f per run)" % (
        s["prompt_tokens"], s["output_tokens"], s["prompt_tokens"] / n if n else 0,
        s["output_tokens"] / n if n else 0))
    if "notional_cost_per_1000_usd" in s:
        print("NOTIONAL cost per 1,000 queries:  $%.4f (paid-tier list price; free tier costs $0)"
              % s["notional_cost_per_1000_usd"])
        st = s["client_stats"]
        print("network calls %d, cache hits %d, retries %d, HTTP 429 %d, 5xx %d, reported delays %s"
              % (st["network_calls"], st["cache_hits"], st["retries"], st["http_429"], st["http_5xx"],
                 st["reported_delays_s"]))
    print("results:", s["results_file"])
    if s.get("incomplete"):
        print("INCOMPLETE: stopped by an LLM error (quota or network); %d run(s) marked 'not run'. Metrics above "
              "cover the %d completed run(s) only.\n  %s" % (s["not_run"], n, (s["quota_error"] or "")[:400]))
    if "calls_today" in s:
        print(s["calls_today"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine", required=True, choices=["llm_query", "hand_coded", "llm_compiled"])
    ap.add_argument("--artifact", default="reviewed", help="llm_compiled only: raw_v1 | raw_v2 | raw_v3 | reviewed")
    ap.add_argument("--split", required=True, choices=["dev", "heldout", "official", "all"],
                    help="all = dev and heldout together (needs --allow-heldout)")
    ap.add_argument("--suite", default="fy2024_25", choices=sorted(SUITES), help="which financial year's cases")
    ap.add_argument("--compiled-dir", help="llm_compiled only: directory holding the artifact and its manifest")
    ap.add_argument("--diagnostic-allow-helpers", action="store_true",
                    help="llm_compiled only, DIAGNOSTIC: permit helper functions in the allow-list")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--allow-heldout", action="store_true",
                    help="required to run the held-out split (report it once, at the end)")
    ap.add_argument("--out-dir", default=str(RESULTS_DIR))
    args = ap.parse_args(argv)
    if args.split in ("heldout", "all") and not args.allow_heldout:
        ap.error("refusing the held-out split without --allow-heldout")
    s = run(args.engine, args.split, args.limit, args.repeats, out_dir=args.out_dir, artifact=args.artifact,
            allow_helpers=args.diagnostic_allow_helpers, suite=args.suite, compiled_dir=args.compiled_dir)
    print_summary(s)
    if s.get("incomplete"):
        raise SystemExit(3)
    return s


if __name__ == "__main__":
    main()
