"""Evaluates the plain-language parser on the NL query set.

    python3 -m project_a.eval.nl_eval --split dev [--prompt v1]

Held-out is refused unless --allow-heldout is passed. LLM calls follow LLM_MODE (live | replay | auto).
Writes project_a/eval/results/nl_<prompt>_<split>_<stamp>.json with the prompt's sha256.
"""
import argparse
import json
from collections import Counter
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from project_a.core.schema import TaxInput
from project_a.eval import RESULTS_DIR
from project_a.eval.run import p95
from project_a.parser import pipeline
from project_a.parser.llm_parse import load_prompt

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent / "tests"
BEHAVIOURS = ("compute", "ask", "unsupported", "law-change")
PREDICTED = BEHAVIOURS + ("parse_error",)
INPUT_FIELDS = [f.name for f in fields(TaxInput)]


def load(split):
    rows = [json.loads(l) for l in (TESTS / "nl" / "queries.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    tags = json.loads((TESTS / "nl" / "topic_map.json").read_text())["map"]
    official = {c["check_id"]: c for c in (json.loads(l) for l in
                (TESTS / "cases" / "official_30.jsonl").read_text().splitlines() if l.strip())}
    return [r for r in rows if r["split"] == split], tags, official


class CountingEngine:
    """Wraps the hand-coded engine to count calls (no call may happen unless the status is compute)."""
    def __init__(self):
        from project_a.engines.hand_coded import HandCodedEngine
        self.inner, self.calls = HandCodedEngine(), 0

    def calculate(self, inp):
        self.calls += 1
        return self.inner.calculate(inp)


def evaluate(split, version=None, client=None, out_dir=RESULTS_DIR):
    rows, tag_map, official = load(split)
    name, _, sha = load_prompt(version)
    if client is None:
        from project_a.llm.client import GeminiClient
        client = GeminiClient(role="parser")
    from project_a.llm.client import QuotaExhausted
    per, not_run, quota_error = [], [], None
    for r in rows:
        if quota_error:
            not_run.append({"id": r["id"], "status": "not run: quota"})
            continue
        engine = CountingEngine()
        try:
            pr = pipeline.parse(r["text"], client=client, version=name, engine=engine)
        except QuotaExhausted as e:                  # keep what was done; never crash on a quota
            quota_error = str(e)
            not_run.append({"id": r["id"], "status": "not run: quota"})
            continue
        got_tags = sorted({q["tag"] for q in pr.questions} | set(pr.unsupported_features))
        item = {"id": r["id"], "language": r["language"], "expected": r["expected_behavior"],
                "status": pr.status, "engine_calls": engine.calls, "expected_tags": tag_map[r["id"]],
                "got_tags": got_tags, "questions": pr.questions, "normalised_input": pr.normalised_input,
                "result": pr.result, "trace": pr.trace, "raw_output": pr.raw_output, "llm": pr.llm}
        if r["expected_behavior"] == "compute":
            gold = asdict(TaxInput.from_dict(r["gold_input"]))
            got = asdict(TaxInput.from_dict(pr.normalised_input)) if pr.normalised_input else {}
            item["field_match"] = {f: got.get(f) == gold[f] for f in INPUT_FIELDS}
            item["field_diff"] = {f: [got.get(f), gold[f]] for f in INPUT_FIELDS if got.get(f) != gold[f]}
            exp = official[r["source_case"]]["expected"]
            item["end_to_end"] = bool(pr.result) and (pr.result["old_tax"], pr.result["new_tax"]) == (
                exp["old_tax"], exp["new_tax"])
        else:
            item["tags_found"] = set(tag_map[r["id"]]) <= set(got_tags)
        per.append(item)

    def count(pred):
        return sum(1 for i in per if pred(i))
    comp = [i for i in per if i["expected"] == "compute"]
    non = [i for i in per if i["expected"] != "compute"]
    s = {
        "split": split, "prompt_version": name, "prompt_sha256": sha, "model": client.model,
        "mode": client.config.mode, "n": len(per),
        "behaviour_correct": count(lambda i: i["status"] == i["expected"]),
        "confusion": {e: dict(Counter(i["status"] for i in per if i["expected"] == e)) for e in BEHAVIOURS},
        "compute_rows": len(comp),
        "compute_fields_correct": sum(sum(i["field_match"].values()) for i in comp),
        "compute_fields_total": len(comp) * len(INPUT_FIELDS),
        "compute_rows_all_fields": count(lambda i: i["expected"] == "compute" and all(i["field_match"].values())),
        "end_to_end": count(lambda i: i["expected"] == "compute" and i["end_to_end"]),
        "per_field": {f: sum(i["field_match"][f] for i in comp) for f in INPUT_FIELDS},
        "ask_rows": count(lambda i: i["expected"] == "ask"),
        "ask_correct": count(lambda i: i["expected"] == "ask" and i["status"] == "ask" and i["tags_found"]),
        "unsupported_rows": count(lambda i: i["expected"] == "unsupported"),
        "unsupported_correct": count(lambda i: i["expected"] == "unsupported" and i["status"] == "unsupported"
                                     and i["tags_found"]),
        "engine_called_when_not_compute": count(lambda i: i["status"] != "compute" and i["engine_calls"] > 0),
        "engine_called_on_non_compute_rows": count(lambda i: i["expected"] != "compute" and i["engine_calls"] > 0),
        "non_compute_rows": len(non),
        "false_compute": count(lambda i: i["expected"] != "compute" and i["status"] == "compute"),
        "parse_errors": count(lambda i: i["status"] == "parse_error"),
        "llm_calls": sum(i["llm"].get("attempts", 0) for i in per),
        "latency_mean_s": sum(i["llm"].get("latency_s", 0) for i in per) / len(per) if per else 0,
        "latency_p95_s": p95([i["llm"].get("latency_s", 0) for i in per]),
        "prompt_tokens": sum(i["llm"].get("prompt_tokens", 0) for i in per),
        "output_tokens": sum(i["llm"].get("output_tokens", 0) for i in per),
        "client_stats": dict(client.stats),
        "incomplete": bool(not_run), "not_run": [i["id"] for i in not_run], "quota_error": quota_error,
    }
    s["false_compute_rate"] = s["false_compute"] / s["non_compute_rows"] if s["non_compute_rows"] else 0.0
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = out_dir / ("nl_%s_%s_%s_%s.json" % (name, split, client.config.mode, stamp))
    s["calls_today"] = client.calls_today_line()
    out.write_text(json.dumps({"summary": s, "rows": per, "not_run": not_run}, indent=1, ensure_ascii=False,
                              default=str),
                   encoding="utf-8")
    s["results_file"] = str(out)
    return s, per


def print_report(s, per):
    n = s["n"]
    print("NL eval: split %s, prompt %s (sha256 %s), model %s, mode %s"
          % (s["split"], s["prompt_version"], s["prompt_sha256"][:12], s["model"], s["mode"]))
    print("behaviour accuracy:        %d/%d" % (s["behaviour_correct"], n))
    print("confusion (rows expected, columns predicted):")
    print("  %-12s" % "" + "".join("%-12s" % p for p in PREDICTED))
    for e in BEHAVIOURS:
        print("  %-12s" % e + "".join("%-12s" % s["confusion"][e].get(p, 0) for p in PREDICTED))
    print("compute fields exact:      %d/%d; rows with every field exact %d/%d"
          % (s["compute_fields_correct"], s["compute_fields_total"], s["compute_rows_all_fields"], s["compute_rows"]))
    print("end-to-end (tax = official): %d/%d" % (s["end_to_end"], s["compute_rows"]))
    print("ask: right status and tags: %d/%d" % (s["ask_correct"], s["ask_rows"]))
    print("unsupported: right status and tag: %d/%d" % (s["unsupported_correct"], s["unsupported_rows"]))
    print("engine calls on non-compute outcomes: %d" % s["engine_called_when_not_compute"])
    print("FALSE-COMPUTE rate:        %d/%d (%.1f%%)" % (s["false_compute"], s["non_compute_rows"],
                                                         100 * s["false_compute_rate"]))
    print("parse errors:              %d" % s["parse_errors"])
    print("latency mean / p95 (s):    %.2f / %.2f over %d LLM calls" % (s["latency_mean_s"], s["latency_p95_s"], s["llm_calls"]))
    print("tokens prompt / output:    %d / %d" % (s["prompt_tokens"], s["output_tokens"]))
    st = s["client_stats"]
    print("network calls %d, cache hits %d, HTTP 429 %d, 5xx %d" % (st["network_calls"], st["cache_hits"],
                                                                   st["http_429"], st["http_5xx"]))
    wrong = [i for i in per if i["status"] != i["expected"] or (i["expected"] == "compute" and not i["end_to_end"])
             or (i["expected"] in ("ask", "unsupported") and not i["tags_found"])]
    for i in wrong:
        print("WRONG %s expected %s got %s tags %s (want %s) diff %s" % (
            i["id"], i["expected"], i["status"], i["got_tags"], i["expected_tags"], i.get("field_diff", "")))
    print("results:", s["results_file"])
    if s["incomplete"]:
        print("INCOMPLETE: quota exhausted; %d of %d queries marked 'not run: quota' (%s). Metrics above "
              "cover the %d completed queries only.\n  %s" % (len(s["not_run"]), n + len(s["not_run"]),
                                                             ", ".join(s["not_run"]), n, (s["quota_error"] or "")[:400]))
    print(s["calls_today"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--split", required=True, choices=["dev", "heldout"])
    ap.add_argument("--prompt", help="prompt version, e.g. v1 (default: latest)")
    ap.add_argument("--allow-heldout", action="store_true")
    ap.add_argument("--out-dir", default=str(RESULTS_DIR))
    args = ap.parse_args(argv)
    if args.split == "heldout" and not args.allow_heldout:
        ap.error("refusing the held-out split without --allow-heldout")
    s, per = evaluate(args.split, args.prompt, out_dir=args.out_dir)
    print_report(s, per)
    if s["incomplete"]:
        raise SystemExit(3)
    return s


if __name__ == "__main__":
    main()
