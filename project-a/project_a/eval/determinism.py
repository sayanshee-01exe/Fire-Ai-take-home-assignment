"""Determinism: the same input repeated 10 times. Writes results/determinism_<kind>_<stamp>.json.

    python3 -m project_a.eval.determinism offline   # hand-coded + reviewed compiled, every golden case x10, no LLM
    python3 -m project_a.eval.determinism query     # query-time LLM engine, 20 FY 2024-25 cases x run_index 0..9
    python3 -m project_a.eval.determinism parser    # parser, 10 dev NL queries x run_index 0..9

Live modes use the cache (LLM_MODE auto): run_index is part of the cache key, so each repeat is its own
call; a repeat recorded earlier is replayed and counted as a cache hit. A 429 or a network error stops the
run cleanly; finished items are written and the rest are marked "not run".

Case selection (deterministic):
  query:  every 3rd row of the official 30 (OC-01, OC-04, ..., OC-28; 10 cases) plus, for each golden
          FY 2024-25 category in file order, its first dev case not already chosen (10 cases) = 20.
  parser: the first 10 dev queries of tests/nl/queries.jsonl in id order.
"""
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from project_a.core.schema import TaxInput
from project_a.eval import RESULTS_DIR

HERE = Path(__file__).resolve().parent
CASES = HERE.parent / "tests" / "cases"
NL = HERE.parent / "tests" / "nl" / "queries.jsonl"
REPEATS = 10


def _jsonl(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def query_cases():
    official = _jsonl(CASES / "official_30.jsonl")
    chosen = [c["id"] for c in official[::3]]
    golden = _jsonl(CASES / "golden.jsonl")
    seen_cats = []
    for c in golden:
        if c["split"] == "dev" and c["category"] not in seen_cats and c["id"] not in chosen:
            seen_cats.append(c["category"])
            chosen.append(c["id"])
    by_id = {c["id"]: c for c in golden}
    return [by_id[i] for i in chosen]


def parser_queries():
    rows = [r for r in _jsonl(NL) if r["split"] == "dev"]
    return sorted(rows, key=lambda r: r["id"])[:10]


class RunIndexClient:
    """Passes a fixed run_index to every generate() call, so the parser's code stays unchanged."""
    def __init__(self, inner, run_index):
        self.inner, self.run_index = inner, run_index
        self.model, self.config, self.stats = inner.model, inner.config, inner.stats

    def generate(self, prompt, system=None, json_mode=True, run_index=0):
        return self.inner.generate(prompt, system=system, json_mode=json_mode, run_index=self.run_index)


def _write(kind, payload):
    out = RESULTS_DIR / ("determinism_%s_%s.json" % (kind, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return out


# ---- offline ------------------------------------------------------------------------------------
def run_offline():
    from project_a.engines.hand_coded import HandCodedEngine
    from project_a.engines.llm_compiled import COMPILED, LLMCompiledEngine
    suites = {"2024-25": _jsonl(CASES / "golden.jsonl"), "2025-26": _jsonl(CASES / "golden_fy2025_26.jsonl")}
    impls = {"hand_coded": {fy: HandCodedEngine() for fy in suites},
             "llm_compiled_reviewed": {"2024-25": LLMCompiledEngine("reviewed", COMPILED),
                                       "2025-26": LLMCompiledEngine("reviewed", COMPILED / "fy2025_26")}}
    result = {"kind": "offline", "repeats": REPEATS, "implementations": {}}
    for name, by_fy in impls.items():
        cases_out = []
        for fy, cases in suites.items():
            engine = by_fy[fy]
            for c in cases:
                outs, lat, comp = [], [], []
                for _ in range(REPEATS):
                    t0 = time.perf_counter()
                    res = engine.calculate(TaxInput.from_dict(c["input"]))
                    wall = time.perf_counter() - t0
                    d = res.to_dict()
                    if name == "llm_compiled_reviewed":
                        d = {"returned": res.returned, "error": res.error}      # what the code returned
                        lat.append(res.latency_s)
                        comp.append(res.compute_s)
                    else:
                        lat.append(wall)
                    outs.append(json.dumps(d, sort_keys=True, default=str))
                cases_out.append({"id": c["id"], "fy": fy, "identical": len(set(outs)) == 1,
                                  "distinct_outputs": len(set(outs)), "latency_s": lat, "compute_s": comp})
        for e in by_fy.values():
            if hasattr(e, "close"):
                e.close()
        result["implementations"][name] = {
            "cases": len(cases_out), "identical": sum(x["identical"] for x in cases_out),
            "what_is_compared": ("TaxResult.to_dict() JSON (taxes, incomes, best, full trace)" if name == "hand_coded"
                                 else "the dict returned by compute() plus the engine error, as JSON"),
            "latency_includes": ("in-process call of HandCodedEngine.calculate()" if name == "hand_coded"
                                 else "sandbox round trip: JSON encode, pipe to the worker process, compute(), JSON back"),
            "rows": cases_out}
    return _write("offline", result)


# ---- live ---------------------------------------------------------------------------------------
def run_query():
    from project_a.engines.llm_query import LLMQueryEngine
    from project_a.llm.client import CacheMiss, GeminiClient, LLMError
    client = GeminiClient(role="query_engine")
    rows, stop = [], None
    for c in query_cases():
        runs = []
        for r in range(REPEATS):
            if stop:
                runs.append({"run_index": r, "status": "not run: " + stop[0]})
                continue
            try:
                res = LLMQueryEngine(client, run_index=r).calculate(TaxInput.from_dict(c["input"]))
            except CacheMiss as e:                  # replay mode only: stop as a quota error would
                stop = ("not in cache", str(e)[:300])
                runs.append({"run_index": r, "status": "not run: " + stop[0]})
                continue
            except LLMError as e:
                stop = ("quota" if "429" in str(e) else "network error", str(e)[:300])
                runs.append({"run_index": r, "status": "not run: " + stop[0]})
                continue
            runs.append({"run_index": r, "status": "done", "error": res.error,
                         "got": {"old_total_income": res.old.total_income, "old_tax": res.old.tax,
                                 "new_total_income": res.new.total_income, "new_tax": res.new.tax, "best": res.best},
                         "raw_output": res.raw_output, "latency_s": res.latency_s, "usage": res.usage,
                         "cached": res.cached, "llm_trace": res.llm_trace})
        rows.append({"id": c["id"], "category": c["category"], "split": c["split"], "input": c["input"],
                     "expected": c["expected"], "runs": runs})
    payload = {"kind": "query", "model": client.model, "repeats": REPEATS, "stopped": stop,
               "selection": "every 3rd official-30 row + first dev case of each other category",
               "client_stats": dict(client.stats), "calls_today": client.calls_today_line(), "rows": rows}
    return _write("query", payload), payload


def run_parser():
    from project_a.llm.client import CacheMiss, GeminiClient, LLMError
    from project_a.parser import pipeline
    from project_a.parser.llm_parse import load_prompt
    client = GeminiClient(role="parser")
    version, _, sha = load_prompt()
    rows, stop = [], None
    for q in parser_queries():
        runs = []
        for r in range(REPEATS):
            if stop:
                runs.append({"run_index": r, "status": "not run: " + stop[0]})
                continue
            try:
                pr = pipeline.parse(q["text"], client=RunIndexClient(client, r), version=version)
            except CacheMiss as e:                  # replay mode only: stop as a quota error would
                stop = ("not in cache", str(e)[:300])
                runs.append({"run_index": r, "status": "not run: " + stop[0]})
                continue
            except LLMError as e:
                stop = ("quota" if "429" in str(e) else "network error", str(e)[:300])
                runs.append({"run_index": r, "status": "not run: " + stop[0]})
                continue
            runs.append({"run_index": r, "status": "done", "outcome": pr.status,
                         "extraction": asdict(pr.extraction) if pr.extraction else None,
                         "normalised_input": pr.normalised_input, "raw_output": pr.raw_output, "llm": pr.llm})
        rows.append({"id": q["id"], "expected": q["expected_behavior"], "runs": runs})
    payload = {"kind": "parser", "model": client.model, "prompt_version": version, "prompt_sha256": sha,
               "repeats": REPEATS, "stopped": stop, "client_stats": dict(client.stats),
               "calls_today": client.calls_today_line(), "rows": rows}
    return _write("parser", payload), payload


def main(argv):
    kind = argv[1] if len(argv) > 1 else "offline"
    if kind == "offline":
        print("wrote", run_offline())
        return 0
    path, payload = run_query() if kind == "query" else run_parser()
    print("wrote", path)
    print(payload["calls_today"])
    if payload["stopped"]:
        print("STOPPED:", payload["stopped"])
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
