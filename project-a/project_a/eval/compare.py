"""Builds RESULTS_TABLES.md from files in the repo. No LLM calls: it only reads recorded results.

    python3 -m project_a.eval.compare [--out RESULTS_TABLES.md]

Every number in the output is computed here from: eval/results/*.json (harness runs, determinism,
traceability, NL evaluations), compiler/compiled/**/manifest.json (compile tokens), llm/config.py (notional
prices), and task5/TASK5_LOG.md (review time and the Task 5 summary table). The newest file per configuration
is used, and every file used is listed at the end.
"""
import argparse
import glob
import json
import re
import statistics
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
ROOT = PKG.parent
from project_a.eval import RESULTS_DIR as RESULTS  # noqa: E402
CASES = PKG / "tests" / "cases"
TASK5_LOG = PKG / "task5" / "TASK5_LOG.md"
ASSUMED_QUERIES = 1_000_000          # amortisation base for the one-time compile cost
# Results files run by the author from a terminal with the same harness and cache (not from the assistant's
# sessions). They are used as recorded, and marked.
EXTERNAL = {"llm_query_dev_n52_r1_auto_20261120T190205Z.json", "llm_query_heldout_n156_r1_auto_20261120T193052Z.json",
            "nl_v1_heldout_auto_20261120T195740Z.json"}
YEARS = (("2024-25", "fy2024_25", ""), ("2025-26", "fy2025_26", "fy2025_26-"))
USED = []


def latest(pattern):
    files = sorted(glob.glob(str(RESULTS / pattern)))
    if not files:
        return None
    USED.append(Path(files[-1]).name)
    return json.loads(Path(files[-1]).read_text(encoding="utf-8"))


def _jsonl(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def official_ids(fy):
    path = CASES / ("official_30.jsonl" if fy == "2024-25" else "official_26.jsonl")
    return {c["id"] for c in _jsonl(path)} if path.exists() else set()


def metrics(rows):
    n = len(rows)
    if not n:
        return None
    return {"n": n, "exact": sum(bool(r["tax_exact"]) for r in rows), "w10": sum(bool(r["tax_within_10"]) for r in rows),
            "income": sum(bool(r["income_exact"]) for r in rows), "best": sum(bool(r["best_match"]) for r in rows),
            "errors": sum(1 for r in rows if r.get("error"))}


def fmt(m, key="exact"):
    return "n/a" if not m else "%d/%d (%.1f%%)" % (m[key], m["n"], 100.0 * m[key] / m["n"])


def pct(a, b):
    return "n/a" if not b else "%d/%d (%.1f%%)" % (a, b, 100.0 * a / b)


def q(xs, p):
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))] if s else None


# ---- collecting rows per implementation ----------------------------------------------------------
def harness_rows(engine, fy_label, split, artifact=None, diag=False):
    if engine == "llm_query":
        if fy_label == "":
            d = latest("llm_query_%s_n%s_r1_*_*.json" % (split, "52" if split == "dev" else "156"))
            return d["rows"] if d else []
        d = latest("llm_query_fy2025_26-all_n173_r1_*_*.json")
        return [r for r in d["rows"] if r.get("split") == split] if d else []
    if engine == "hand_coded":
        d = latest("hand_coded_%s%s_n*_r1_na_*.json" % (fy_label, split))
    else:
        d = latest("llm_compiled-%s%s_%s%s_n*_r1_no-llm_*.json" % (artifact, "-DIAG-helpers" if diag else "", fy_label, split))
    return d["rows"] if d else []


IMPLS = [("hand-coded", dict(engine="hand_coded")),
         ("LLM at query time", dict(engine="llm_query")),
         ("compiled raw_v1 strict", dict(engine="llm_compiled", artifact="raw_v1")),
         ("compiled raw_v2 strict", dict(engine="llm_compiled", artifact="raw_v2")),
         ("compiled raw_v3 strict", dict(engine="llm_compiled", artifact="raw_v3")),
         ("compiled raw_v1 diagnostic", dict(engine="llm_compiled", artifact="raw_v1", diag=True)),
         ("compiled raw_v2 diagnostic", dict(engine="llm_compiled", artifact="raw_v2", diag=True)),
         ("compiled raw_v3 diagnostic", dict(engine="llm_compiled", artifact="raw_v3", diag=True)),
         ("compiled reviewed", dict(engine="llm_compiled", artifact="reviewed"))]


def accuracy_section(out):
    out += ["## 1. Accuracy by implementation", "",
            "Tax exact = both regimes' final tax equal to the expected value to the rupee. Expected values: golden",
            "suites (from the independent oracle); official rows: the official calculator. \"Heldout excl. official\"",
            "drops heldout cases that are also official-calculator rows. Compiled \"strict\" is the production",
            "allow-list (a rejected artifact answers nothing); \"diagnostic\" relaxes only the one-function rule.", ""]
    for fy, _, label in YEARS:
        off = official_ids(fy)
        out += ["### FY %s" % fy, "",
                "| Implementation | Split | Tax exact | Within Rs 10 | Income exact | Best regime | Errors |",
                "|---|---|---|---|---|---|---|"]
        for name, cfg in IMPLS:
            for split in ("dev", "heldout", "official"):
                if cfg["engine"] == "llm_query" and split == "official":
                    continue
                rows = harness_rows(fy_label=label, split=split, **cfg)
                m = metrics(rows)
                out.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                    name, split, fmt(m), fmt(m, "w10"), fmt(m, "income"), fmt(m, "best"), m["errors"] if m else "n/a"))
                if split == "heldout" and rows:
                    m2 = metrics([r for r in rows if r["id"] not in off])
                    out.append("| %s | heldout excl. official (%d removed) | %s | %s | %s | %s | %s |" % (
                        name, len(rows) - m2["n"], fmt(m2), fmt(m2, "w10"), fmt(m2, "income"), fmt(m2, "best"), m2["errors"]))
        out.append("")


def category_section(out):
    out += ["## 2. Tax exact by category (dev and heldout together)", ""]
    pick = [IMPLS[0], IMPLS[1], IMPLS[5], IMPLS[6], IMPLS[7], IMPLS[8]]
    for fy, _, label in YEARS:
        per = {}
        for name, cfg in pick:
            rows = harness_rows(fy_label=label, split="dev", **cfg) + harness_rows(fy_label=label, split="heldout", **cfg)
            for r in rows:
                per.setdefault(r["category"], {}).setdefault(name, []).append(r)
        out += ["### FY %s" % fy, "", "| Category | " + " | ".join(n for n, _ in pick) + " |",
                "|---|" + "---|" * len(pick)]
        for cat in sorted(per):
            out.append("| %s | %s |" % (cat, " | ".join(fmt(metrics(per[cat].get(n, []))) for n, _ in pick)))
        out.append("")


def parser_section(out):
    out += ["## 3. Plain-language parser (NL query set)", "",
            "| Split | Behaviour accuracy | Compute fields exact | End-to-end tax = official | False-compute | Parse errors |",
            "|---|---|---|---|---|---|"]
    for split in ("dev", "heldout"):
        d = latest("nl_v1_%s_*.json" % split)
        if not d:
            out.append("| %s | not run | | | | |" % split)
            continue
        s = d["summary"]
        out.append("| %s (prompt %s, %s) | %s | %s | %s | %s | %d |" % (
            split, s["prompt_version"], s["model"], pct(s["behaviour_correct"], s["n"]),
            pct(s["compute_fields_correct"], s["compute_fields_total"]), pct(s["end_to_end"], s["compute_rows"]),
            pct(s["false_compute"], s["non_compute_rows"]), s["parse_errors"]))
    out.append("")
    # The frozen set labels its FY 2025-26 queries "law-change" (written before those rules were loaded).
    # The pipeline now computes FY 2025-26, so say what those rows do instead of leaving the score unexplained.
    rows = [(split, r) for split in ("dev", "heldout") for r in (latest("nl_v1_%s_*.json" % split) or {"rows": []})["rows"]]
    lc = [(split, r) for split, r in rows if r["expected"] == "law-change"]
    if lc and any(r["status"] != "law-change" for _, r in lc):
        def what(r):
            if r["status"] == "compute":
                ni, res = r["normalised_input"], r["result"]
                return "compute, FY %s engine (old Rs %d, new Rs %d)" % (ni["fy"], res["old_tax"], res["new_tax"])
            return r["status"] + (" (%s)" % ", ".join(r["got_tags"]) if r["got_tags"] else "")
        other_fc = sum(1 for _, r in rows if r["expected"] not in ("compute", "law-change") and r["status"] == "compute")
        out += ["Scored against the frozen labels. Its %d \"law-change\" rows were written when FY 2025-26 rules were not "
                "loaded; the pipeline now computes FY 2025-26, so they count as wrong: %s. False computes outside "
                "these rows: %d." % (len(lc), "; ".join("%s (%s) %s" % (r["id"], split, what(r)) for split, r in lc),
                                     other_fc), ""]


def determinism_section(out):
    out += ["## 4. Determinism: the same input 10 times", ""]
    off = latest("determinism_offline_*.json")
    out += ["| Implementation | Cases | 10 identical outputs | What is compared |", "|---|---|---|---|"]
    if off:
        for name, v in off["implementations"].items():
            out.append("| %s (FY 2024-25 + FY 2025-26 golden) | %d | %s | %s |" % (
                name, v["cases"], pct(v["identical"], v["cases"]), v["what_is_compared"]))
    qd = latest("determinism_query_*.json")
    if qd:
        done = [r for r in qd["rows"] if all(x["status"] == "done" for x in r["runs"])]
        ident = num_ident = mixed = 0
        distinct = []
        for r in done:
            raws = {x["raw_output"] for x in r["runs"]}
            vals = {(x["got"]["old_tax"], x["got"]["new_tax"]) for x in r["runs"]}
            ident += len(raws) == 1
            num_ident += len(vals) == 1
            distinct.append(len(vals))
            right = [x["got"]["old_tax"] == r["expected"]["old_tax"] and x["got"]["new_tax"] == r["expected"]["new_tax"]
                     for x in r["runs"]]
            mixed += any(right) and not all(right)
        all_right = sum(all(x["got"]["old_tax"] == r["expected"]["old_tax"] and x["got"]["new_tax"] == r["expected"]["new_tax"]
                            for x in r["runs"]) for r in done)
        partial = []
        for r in qd["rows"]:
            ok = [x for x in r["runs"] if x["status"] == "done"]
            if ok and len(ok) < len(r["runs"]):
                partial.append("%s: %d of %d runs done, %d distinct raw output(s)" % (
                    r["id"], len(ok), len(r["runs"]), len({x["raw_output"] for x in ok})))
        live = sum(1 for r in qd["rows"] for x in r["runs"] if x["status"] == "done" and not x["cached"])
        out.append("| LLM at query time (%s; 20 FY 2024-25 cases x run_index 0..9) | %d of %d complete | raw text %s; both taxes %s | raw model text, and the (old tax, new tax) pair |" % (
            qd["model"], len(done), len(qd["rows"]), pct(ident, len(done)), pct(num_ident, len(done))))
        if distinct:
            out += ["", "Query-time engine: distinct (old tax, new tax) pairs per case over 10 runs: min %d, mean %.2f, max %d. "
                    "Cases with some runs right and some wrong: %d of %d; right on all runs: %d of %d. "
                    "Live calls in this run: %d (the rest replayed earlier recordings of the same run_index)%s.%s" % (
                        min(distinct), statistics.mean(distinct), max(distinct), mixed, len(done), all_right, len(done), live,
                        "; STOPPED: %s" % qd["stopped"][0] if qd.get("stopped") else "",
                        (" Incomplete (excluded above): " + "; ".join(partial) + ".") if partial else ""), ""]
    else:
        out.append("| LLM at query time | not run | | |")
    pd = latest("determinism_parser_*.json")
    if pd:
        done = [r for r in pd["rows"] if all(x["status"] == "done" for x in r["runs"])]
        same_struct = sum(len({json.dumps(x["extraction"], sort_keys=True) for x in r["runs"]}) == 1 for r in done)
        same_outcome = sum(len({x["outcome"] for x in r["runs"]}) == 1 for r in done)
        same_input = sum(len({json.dumps(x["normalised_input"], sort_keys=True) for x in r["runs"]}) == 1 for r in done)
        live = sum(1 for r in pd["rows"] for x in r["runs"] if x["status"] == "done" and not x["llm"].get("cached"))
        if not qd:
            out += ["| Implementation | Cases | 10 identical outputs | What is compared |", "|---|---|---|---|"]
        out += ["", "| Parser (%s, prompt %s; 10 dev queries x run_index 0..9) | Cases complete | Identical extraction | Identical outcome | Identical schema input |" % (pd["model"], pd["prompt_version"]),
                "|---|---|---|---|---|",
                "| | %d of %d | %s | %s | %s |" % (len(done), len(pd["rows"]), pct(same_struct, len(done)),
                                                pct(same_outcome, len(done)), pct(same_input, len(done))),
                "", "Parser live calls in this run: %d%s." % (live, "; STOPPED: %s" % pd["stopped"][0] if pd.get("stopped") else ""), ""]
    else:
        out += ["", "Parser determinism: not run.", ""]


def latency_section(out):
    out += ["## 5. Latency per request", "",
            "| Implementation | Samples | Mean | p50 | p95 | Max | What it includes |", "|---|---|---|---|---|---|---|"]
    off = latest("determinism_offline_*.json")

    def row(name, xs, unit, what):
        if not xs:
            out.append("| %s | 0 | n/a | n/a | n/a | n/a | %s |" % (name, what))
            return
        k = 1e6 if unit == "us" else 1.0
        out.append("| %s | %d | %.1f %s | %.1f %s | %.1f %s | %.1f %s | %s |" % (
            name, len(xs), statistics.mean(xs) * k, unit, q(xs, .5) * k, unit, q(xs, .95) * k, unit, max(xs) * k, unit, what))
    if off:
        hc = off["implementations"]["hand_coded"]["rows"]
        cr = off["implementations"]["llm_compiled_reviewed"]["rows"]
        row("hand-coded", [x for r in hc for x in r["latency_s"]], "us", "in-process engine call (381 golden cases x 10)")
        row("compiled reviewed: round trip", [x for r in cr for x in r["latency_s"]], "us",
            "JSON encode, pipe to the sandbox worker, compute(), JSON back (worker already started)")
        row("compiled reviewed: compute() only", [x for r in cr for x in r["compute_s"]], "us", "time inside compute() in the sandbox")
    qrows = harness_rows("llm_query", "", "dev") + harness_rows("llm_query", "", "heldout") + \
        harness_rows("llm_query", "fy2025_26-", "dev") + harness_rows("llm_query", "fy2025_26-", "heldout")
    row("LLM at query time", [r["latency_s"] for r in qrows], "s",
        "round trip of the successful HTTPS request (network + model); excludes client throttling and retries")
    prows = []
    for split in ("dev", "heldout"):
        d = latest("nl_v1_%s_*.json" % split)
        prows += d["rows"] if d else []
    row("parser (LLM extraction)", [r["llm"]["latency_s"] for r in prows if r.get("llm")], "s",
        "sum over the LLM attempts for one query (1, or 2 on a retry); code decisions and engine call add microseconds")
    out.append("")


def review_seconds():
    if not TASK5_LOG.exists():
        return None
    text = TASK5_LOG.read_text(encoding="utf-8")
    start = re.search(r"### (\S+ \S+) UTC .*review clock starts", text)
    end = re.search(r"### (\S+ \S+) UTC .*review clock stops", text)
    if not (start and end):
        return None
    t = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    return (t(end.group(1)) - t(start.group(1))).total_seconds()


def cost_section(out):
    from project_a.llm.config import PRICES_CHECKED, get_config
    price = {role: (get_config(role).model, get_config(role).price_input_per_m, get_config(role).price_output_per_m)
             for role in ("query_engine", "parser", "compiler")}
    out += ["## 6. Cost per 1,000 requests", "",
            "NOTIONAL: paid-tier list prices per million tokens (input / output) from llm/config.py, checked %s: "
            "%s. The free tier used here costs $0. Tokens per query are given so the cost can be recomputed." % (
                PRICES_CHECKED, "; ".join("%s (%s) US$ %.2f / %.2f" % (m, role, a, b) for role, (m, a, b) in price.items())), "",
            "| Implementation | Prompt tokens / query | Output tokens / query | Notional US$ per 1,000 | Notes |", "|---|---|---|---|---|",
            "| hand-coded | 0 | 0 | 0.00 | no model calls; the cost is the human effort of writing and testing the engine |"]
    qrows = harness_rows("llm_query", "", "dev") + harness_rows("llm_query", "", "heldout") + \
        harness_rows("llm_query", "fy2025_26-", "dev") + harness_rows("llm_query", "fy2025_26-", "heldout")
    for name, rows, get, role in (("LLM at query time", qrows, lambda r: r["usage"], "query_engine"),
                                  ("parser (per query, LLM extraction)",
                                   [r for s in ("dev", "heldout") for r in ((latest("nl_v1_%s_*.json" % s) or {"rows": []})["rows"])],
                                   lambda r: r["llm"], "parser")):
        _, pin, pout = price[role]
        if rows:
            p = statistics.mean(get(r).get("prompt_tokens", 0) for r in rows)
            o = statistics.mean(get(r).get("output_tokens", 0) for r in rows)
            out.append("| %s | %.0f | %.0f | %.2f | mean over %d recorded queries |" % (name, p, o, (p * pin + o * pout) / 1e6 * 1000, len(rows)))
    rev = review_seconds()
    _, pin, pout = price["compiler"]
    for fy, sub in (("2024-25", ""), ("2025-26", "fy2025_26/")):
        path = PKG / "compiler" / "compiled" / sub / "manifest.json"
        if not path.exists():
            continue
        USED.append(str(path.relative_to(PKG)))
        m = json.loads(path.read_text())
        raws = [v for k, v in m.items() if k.startswith("raw_v")]
        p, o = sum(v["prompt_tokens"] for v in raws), sum(v["output_tokens"] for v in raws)
        usd = (p * pin + o * pout) / 1e6
        review = ("review %.0f s (assistant), %.4f s per 1,000" % (rev, rev / (ASSUMED_QUERIES / 1000))
                  if fy == "2025-26" and rev is not None else "review time not recorded")
        out.append("| compiled FY %s | 0 per query | 0 per query | %.6f amortised | one-time: %d compiles, %d prompt + %d output tokens (US$ %.4f), amortised over %s queries; %s |" % (
            fy, usd / (ASSUMED_QUERIES / 1000), len(raws), p, o, usd, format(ASSUMED_QUERIES, ","), review))
    out.append("")


def traceability_section(out):
    d = latest("traceability_*.json")
    out += ["## 7. Traceability: does every rupee trace back to a clause?", ""]
    if not d:
        out += ["Not run.", ""]
        return
    out += ["(a) every step carries a clause ID and the four reported amounts appear in the trace; (b) every clause ID",
            "exists in that year's rules text; (c) every step amount equals an independent recomputation of that clause",
            "(strict: 0.01 rupee; also at Rs 1). See eval/traceability.py for exactly what is recomputed.", "",
            "| Implementation | Answers | (a) tagged + reported | (b) known clause | (c) correct, strict | (c) within Rs 1 | Wrong steps (strict / Rs 1) |",
            "|---|---|---|---|---|---|---|"]
    for name, s in d["implementations"].items():
        out.append("| %s | %d | %s | %s | %s | %s | %d of %d / %d |" % (
            name, s["cases"], pct(s["a"], s["cases"]), pct(s["b"], s["cases"]), pct(s["c"], s["cases"]),
            pct(s["c_within_1"], s["cases"]), s["bad_steps"], s["steps"], s["bad_steps_1"]))
    out += ["", "By year and split:", "",
            "| Implementation | FY | Split | Answers | (a) | (b) | (c) strict | (c) within Rs 1 |", "|---|---|---|---|---|---|---|---|"]
    for name, s in d["implementations"].items():
        groups = {}
        for r in s.get("rows", []):
            groups.setdefault((r["fy"], r.get("split")), []).append(r)
        for (fy, split), rs in sorted(groups.items()):
            n = len(rs)
            out.append("| %s | %s | %s | %d | %s | %s | %s | %s |" % (
                name, fy, split, n, *(pct(sum(bool(r[k]) for r in rs), n) for k in ("a", "b", "c", "c_within_1"))))
    out += ["", "The parser computes no tax itself; its answers come from the hand-coded engine, so its traceability is that row.", ""]


def task5_section(out):
    out += ["## 8. Task 5: the FY 2025-26 law change", ""]
    if not TASK5_LOG.exists():
        out += ["TASK5_LOG.md not found.", ""]
        return
    text = TASK5_LOG.read_text(encoding="utf-8")
    block = text[text.rfind("FINAL SUMMARY"):]
    table = [l for l in block.splitlines() if l.startswith("|")]
    out += (table or ["(no summary table found)"]) + ["", "Source: project_a/task5/TASK5_LOG.md, entry FINAL SUMMARY.", ""]


def build():
    del USED[:]
    out = ["# Results tables (Project A)", "",
           "Generated by `python3 -m project_a.eval.compare` from files in the repo; no LLM calls. Every number below",
           "is computed by that script. Do not edit by hand.", ""]
    for section in (accuracy_section, category_section, parser_section, determinism_section, latency_section,
                    cost_section, traceability_section, task5_section):
        section(out)
    out += ["## Files used", "", "(*) run by the author from a terminal with the same harness and cache; used as recorded.", ""]
    out += ["- `%s`%s" % (f, " (*)" if f in EXTERNAL else "") for f in sorted(set(USED))] + [""]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "RESULTS_TABLES.md"))
    args = ap.parse_args(argv)
    Path(args.out).write_text(build(), encoding="utf-8")
    print("wrote", args.out)


if __name__ == "__main__":
    main()
