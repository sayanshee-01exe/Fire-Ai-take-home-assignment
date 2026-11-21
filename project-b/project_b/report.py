"""Generate RESULTS_B.md and DECISION_MATRIX.md from the result files - and only from them.

The prose lives in project_b/templates/*.template.md; every {{block}} in a template is replaced
by a table or sentence computed here from project_b/results/*, data/raw/manifest.json and
tests/data/k_screen_decisions.json. A block whose input does not exist says so ("not measured
yet") instead of showing a number: nothing in the reports is typed by hand or estimated.

Usage: python -m project_b.report [--results-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from project_b import config, evaluate, llm, pipeline, profile
from project_b.core import PROBLEMS

TEMPLATES = config.PKG_DIR / "templates"
SCREEN_FILE = config.ROOT / "tests" / "data" / "k_screen_decisions.json"
# The stage-1 summary as first scored (rows without a readable LLM verdict counted as reviewed, not
# flagged), kept so the report can show exactly what the re-scoring changed.
RESCORE_BEFORE_DIR = config.RESULTS_DIR / "stage1_before_rescore"
OUTPUTS = {"RESULTS_B.template.md": "RESULTS_B.md", "DECISION_MATRIX.template.md": "DECISION_MATRIX.md"}
PENDING = "_Not measured yet: run `python -m project_b` (the full benchmark runs only after approval)._"
FAMILY_TRADEOFFS = {
    "rule": "deterministic, explains itself (check id, column, value), free to run; must be edited by hand when the data contract changes",
    "stat": "deterministic given its fitted baseline, explains with a score and threshold; must be re-fitted per customer and can drift",
    "llm": "reads meaning; not deterministic, costs tokens and seconds per call, needs the replay cache to be reproducible and auditable",
}


def _rel(path: Path) -> str:
    """Repository-relative path for links (absolute when the file lives elsewhere)."""
    try:
        return path.resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _file_time(path: Path) -> str:
    """When a result file was produced. A fresh clone resets modification times to the clone time,
    so a file that git tracks and that is unchanged reports its last commit time instead."""
    import subprocess
    try:
        cwd = str(config.ROOT)
        rel = path.resolve().relative_to(config.ROOT).as_posix()
        unchanged = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=cwd,
                                   capture_output=True, text=True, timeout=10)
        committed = subprocess.run(["git", "log", "-1", "--format=%ct", "--", rel], cwd=cwd,
                                   capture_output=True, text=True, timeout=10)
        if unchanged.returncode == 0 and not unchanged.stdout.strip() and committed.stdout.strip():
            when = datetime.fromtimestamp(int(committed.stdout.strip()), timezone.utc)
            return f"committed {when.strftime('%Y-%m-%d %H:%M UTC')}"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # not in a git checkout, git missing, or a path outside the repository
    when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return f"generated {when.strftime('%Y-%m-%d %H:%M UTC')}"


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class Inputs:
    def __init__(self, results_dir: Path):
        self.dir = results_dir
        self.summary = _read(results_dir / evaluate.SUMMARY_FILE)
        self.pipeline = _read(results_dir / pipeline.SUMMARY_FILE)
        self.scale = _read(results_dir / "scale.json")
        self.profile = _read(config.RESULTS_DIR / profile.JSON_FILE)
        self.manifest = _read(config.DATASET_MANIFEST)  # committed; data/raw/ is not
        self.screen = _read(SCREEN_FILE)


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------
def block_status(i: Inputs) -> str:
    rows = [("Task 1 profile", config.RESULTS_DIR / profile.REPORT_FILE),
            ("Detector benchmark (Tasks 3-4)", i.dir / evaluate.SUMMARY_FILE),
            ("Hybrid pipeline evaluation (Task 5)", i.dir / pipeline.SUMMARY_FILE),
            ("Scale projection (Task 6)", i.dir / "scale.json")]
    lines = ["| result | file | state |", "|---|---|---|"]
    for name, path in rows:
        present = path.exists()
        state = _file_time(path) if present else "**not run yet**"
        if present and name.startswith("Scale") and i.pipeline is None:
            state += " (assumption-only figures; every measured input is **pending**)"
        lines.append(f"| {name} | `{_rel(path)}` | {state} |")
    plan_source = (i.pipeline or {}).get("plan_source")
    llm_model = ((i.summary or {}).get("settings") or {}).get("llm_model")
    lines += ["", f"Report generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
              f"Pipeline plan: {plan_source or 'not evaluated'}. LLM model in the benchmark: {llm_model or 'not run'}."]
    return "\n".join(lines)


def block_data(i: Inputs) -> str:
    m = i.manifest
    if not m:
        return "_The raw snapshot has not been downloaded (`python -m project_b.data.download`)._"
    return (f"{m['total_rows']:,} rows from NYC Open Data `{m['dataset']}` ({m['dataset_name']}), filter `{m['filter']}`, "
            f"downloaded {m['downloaded_at_utc']} (source last updated {m['source_rows_updated_at_utc']}); every month's row "
            f"count matched the source's own count; parquet sha256 `{m['sha256'][:16]}...`. The snapshot is described in "
            f"`{config.DATASET_MANIFEST.name}` and checked against it before any step reads it (section 8).")


def block_profile_top(i: Inputs) -> str:
    path = config.RESULTS_DIR / profile.REPORT_FILE
    if not path.exists():
        return PENDING
    text = path.read_text(encoding="utf-8")
    match = re.search(r"### Top findings\s*\n(.*?)(?=\n### )", text, flags=re.S)
    return (match.group(1).strip() if match else text) + f"\n\nFull report with examples: [`{_rel(path)}`]({_rel(path)})."


def block_sanitizer(i: Inputs) -> str:
    """The clean pool, from the Task 1 profile (same clean.sanitize code the benchmark uses)."""
    s = (i.profile or {}).get("sanitizer")
    if not s:
        return PENDING
    lines = [f"{s['clean_rows']:,} rows kept in the clean pool ({s['rows_removed']:,} removed, {s['rows_removed_pct']:.2f} %): "
             f"{s['reference_clean_rows']:,} in the reference period (Jan-Aug, used to fit and calibrate) and "
             f"{s['evaluation_clean_rows']:,} in the evaluation period (Sep-Dec, cut into batches). A row can have several issues.", "",
             "| pre-existing issue | definition | rows flagged |", "|---|---|---:|"]
    lines += [evaluate.table_row([k, s["definitions"].get(k, ""), f"{v:,}"]) for k, v in s["removed_by_issue"].items()]
    return "\n".join(lines)


def block_settings(i: Inputs) -> str:
    """The experimental design as configured (config.py), not as measured."""
    c = config
    return "\n".join([
        "| setting | value |", "|---|---|",
        f"| reference period (fit, calibrate, choose) | created before {c.REFERENCE_END[:10]} (Jan-Aug) |",
        f"| evaluation period (batches) | created on or after {c.REFERENCE_END[:10]} (Sep-Dec) |",
        f"| batch | {c.BATCH_SIZE:,} contiguous rows in arrival order |",
        f"| detector benchmark | {c.N_CORRUPTED_BATCHES} corrupted batches per problem, {c.N_CLEAN_BATCHES} shared clean batches, "
        f"row injection rate {c.ROW_INJECTION_RATE:.0%}, rate sweep {', '.join(f'{r:.1%}' for r in c.RATE_SWEEP)} |",
        f"| statistical calibration targets | row FPR {c.TARGET_ROW_FPR:.1%}, batch FAR {c.TARGET_BATCH_FAR:.0%} (on reference data) |",
        f"| LLM sample | {c.LLM_ROW_SAMPLE_INJECTED} injected + as many non-injected rows and {c.LLM_ROW_SAMPLE_CLEAN} clean rows per "
        f"row-level problem from the first {c.LLM_N_BATCHES} batches; {c.LLM_ROWS_PER_CALL} rows per call; {c.LLM_REPEATS} repeats for determinism |",
        f"| LLM | `{c.GEMINI_DEFAULT_MODEL}` (temperature 0, JSON schema), prices {c.LLM_PRICES_AS_OF}; every answer cached for replay |",
        f"| hybrid pipeline evaluation | {c.PIPELINE_N_BATCHES} single-problem batches per problem, {c.PIPELINE_N_CLEAN} clean, "
        f"{c.PIPELINE_N_MIXED} mixed ({c.PIPELINE_PROBLEMS_PER_MIXED} corruption types each), at most {c.PIPELINE_MAX_CONFIRM_ITEMS} "
        "distinct values per batch and problem sent to the LLM |",
        f"| seed | {c.SEED} |",
    ])


def block_injection(i: Inputs) -> str:
    from project_b.inject import VARIANTS

    lines = ["| problem | level | variants |", "|---|---|---|"]
    lines += [f"| {p} - {info['title']} | {info['level']} | {', '.join(VARIANTS[p])} |" for p, info in PROBLEMS.items()]
    return "\n".join(lines)


def block_k_screen(i: Inputs) -> str:
    if not i.screen:
        return "_tests/data/k_screen_decisions.json is missing._"
    entries = i.screen["entries"]
    count = Counter((e["kind"], e["decision"]) for e in entries)
    lines = ["| candidate texts (Jan-Aug, >= 20 rows, >= 95 % in one status group) | read | kept | excluded | kept but borderline |",
             "|---|---:|---:|---:|---:|"]
    for kind, label in (("open", "open texts (may be put on a Closed row)"), ("closed", "closed texts (may stay on a reopened row)")):
        n = sum(1 for e in entries if e["kind"] == kind)
        border = sum(1 for e in entries if e["kind"] == kind and e.get("borderline"))
        lines.append(f"| {label} | {n} | {count[(kind, 'keep')]} | {count[(kind, 'exclude')]} | {border} |")
    by_agency = Counter(e["agency"] for e in entries if e["kind"] == "closed" and e["decision"] == "exclude")
    lines += ["", "Excluded closed texts per agency: " + ", ".join(f"{a} {n}" for a, n in sorted(by_agency.items())) + ".", "",
              "The borderline keeps (judged borderline while reading, kept because the text itself reports a finished step):", "",
              "| agency | Jan-Aug rows | text (start) |", "|---|---:|---|"]
    for e in sorted((e for e in entries if e.get("borderline")), key=lambda e: (e["agency"], -e["jan_aug_rows"])):
        lines.append(f"| {e['agency']} | {e['jan_aug_rows']:,} | {evaluate.table_row([e['text'][:110]])[2:-2]} |")
    return "\n".join(lines)


def block_detectors(i: Inputs) -> str:
    if not i.summary:
        return PENDING
    s = i.summary
    lines = []
    for problem in PROBLEMS:
        entries = [e for e in s["detectors"] if e["problem"] == problem]
        if entries:
            lines += evaluate.problem_table(problem, entries)
            lines += evaluate.same_rows_table(problem, s["same_rows"].get(problem, []))
    lines.append(f"Full tables, sweeps and settings: [`summary_tables.md`]({_rel(i.dir / evaluate.TABLES_FILE)}).")
    return re.sub(r"(?m)^## ", "#### ", "\n".join(lines))  # every problem heading, the first one included


def block_llm_coverage(i: Inputs) -> str:
    """Per corruption type: how often the LLM gave a readable verdict, and both views of its rates."""
    if not i.summary:
        return PENDING
    lines = evaluate.coverage_table(i.summary["detectors"])
    return "\n".join(lines[2:]).strip() if lines else "_No LLM detector ran._"


GROUP_TITLES = {
    "MAX_TOKENS": "Cut off at the output-token limit (`finishReason` MAX_TOKENS), inside a repetition loop",
    "RECITATION": "Blocked by Gemini's recitation filter (`finishReason` RECITATION)",
}


def block_unreadable_replies(i: Inputs) -> str:
    """Every cached Gemini reply without a readable verdict, grouped by cause, with the evidence."""
    audit = llm.audit_cache()
    if not audit["n_entries"]:
        return "_No replay cache yet._"
    bad = audit["unreadable"]
    if not bad:
        return f"All {audit['n_entries']:,} cached replies hold a readable verdict."
    ref = audit["readable_longest_string"]
    lines = [f"{len(bad)} of the {audit['n_entries']:,} cached replies (`project_b/llm_cache/{audit['cache_file']}`) hold no "
             "readable verdict. Every row they cover is an abstention; none was retried, so no new request was made to "
             "re-score them.", ""]
    for reason in dict.fromkeys(b["finish_reason"] for b in bad):
        group = [b for b in bad if b["finish_reason"] == reason]
        lines += [f"**{GROUP_TITLES.get(reason, 'finishReason ' + reason)}: {len(group)} repl{'y' if len(group) == 1 else 'ies'}.**", ""]
        if reason == "MAX_TOKENS":
            longest = [b["longest_string_chars"] for b in group]
            lines += [f"In each of these replies one text field never ends: it runs to {min(longest):,}-{max(longest):,} characters, "
                      f"against a median of {ref['median']:,} and a 99th percentile of {ref['p99']:,} for the longest field of the "
                      f"{ref['n']:,} readable per-row replies. Its last characters repeat the same few words or digits: the model looped "
                      f"until it hit the output limit (`maxOutputTokens` 4,096; {min(b['output_tokens'] for b in group):,}-"
                      f"{max(b['output_tokens'] for b in group):,} output tokens counted) without closing the JSON.", ""]
        elif reason == "RECITATION":
            lines += ["Gemini returned no text at all and 0 output tokens: its recitation filter stopped the reply. "
                      "The prompt held public 311 records only.", ""]
        lines += ["| detector | call | rows asked (ids) | output tokens | longest text field | reply ends with |", "|---|---|---|---:|---:|---|"]
        for b in group:
            call = "main run" if not b["repeat"] else f"determinism repeat {b['repeat']}"
            tail = b["tail"][-60:].replace("\n", " ") if b["tail"] else "(empty)"
            lines.append(evaluate.table_row([b["tag"].split("/")[-1], call, f"{b['rows_asked']} ({b['ids']})", f"{b['output_tokens']:,}",
                                             f"{b['longest_string_chars']:,} chars", f"`{tail.strip()}`"]))
        lines.append("")
    lines += _abstention_cross_check(i, bad)
    return "\n".join(lines).strip()


def _abstention_cross_check(i: Inputs, bad: list[dict]) -> list[str]:
    """The rows of these replies must be exactly the abstentions in the benchmark records."""
    if not i.summary:
        return []
    entries = {e["detector"]: e for e in i.summary["detectors"] if e.get("abstention")}
    checks = []
    for name in dict.fromkeys(b["tag"].split("/")[-1] for b in bad):
        e = entries.get(name)
        if not e or e["abstention"]["unit"] != "rows":
            continue
        main = sum(b["rows_asked"] for b in bad if b["tag"].endswith(name) and not b["repeat"])
        repeat = sum(b["rows_asked"] for b in bad if b["tag"].endswith(name) and b["repeat"])
        a = e["abstention"]
        recorded = a["asked"] - a["with_verdict"]
        recorded_repeat = (a.get("determinism_repeats") or {}).get("abstained", 0)
        ok = main == recorded and repeat == recorded_repeat
        checks.append(f"`{name}` {recorded:,} rows" + (f" (+{recorded_repeat:,} in determinism repeats)" if recorded_repeat else "")
                      + ("" if ok else f" - MISMATCH: the replies cover {main:,} (+{repeat:,})"))
    if not checks:
        return []
    return ["Cross-check against the benchmark records, abstentions per detector: " + "; ".join(checks) + ". "
            + ("Every abstention comes from these replies." if all("MISMATCH" not in c for c in checks) else "")]


def block_rescore_change(i: Inputs) -> str:
    """What changed when rows without a readable LLM verdict became abstentions."""
    before = _read(RESCORE_BEFORE_DIR / evaluate.SUMMARY_FILE)
    if not before or not i.summary:
        return "_No earlier scoring to compare with._"
    old = {e["detector"]: e for e in before["detectors"]}
    lines = ["| LLM detector | coverage | detection: first scoring -> overall / given a verdict | "
             "row FPR: first scoring -> overall / given a verdict | determinism: first scoring -> now |", "|---|---|---|---|---|"]
    for e in i.summary["detectors"]:
        a = e.get("abstention")
        if e["family"] != "llm" or e["level"] != "row" or not a or a["asked"] == a["with_verdict"]:
            continue
        o, op = old.get(e["detector"], {}), e["operational"]
        lines.append(evaluate.table_row([
            e["detector"], f"{evaluate.fmt_pct(a['coverage'])} ({a['with_verdict']:,}/{a['asked']:,})",
            f"{evaluate.fmt_rate(o.get('detection'))} -> {evaluate.fmt_rate(op['detection'])} / {evaluate.fmt_rate(e['detection'])}",
            f"{evaluate.fmt_rate(o.get('row_fpr'), 2)} -> {evaluate.fmt_rate(op['row_fpr'], 2)} / {evaluate.fmt_rate(e['row_fpr'], 2)}",
            f"{_pct_k_of_n(o.get('determinism'))} -> {_pct_k_of_n(e.get('determinism'))}",
        ]))
    if len(lines) == 2:
        return "_The re-scoring changed no LLM figure._"
    return "\n".join(lines + ["", f"First scoring: [`{_rel(RESCORE_BEFORE_DIR / evaluate.SUMMARY_FILE)}`]"
                               f"({_rel(RESCORE_BEFORE_DIR / evaluate.SUMMARY_FILE)}). Rule and statistical records were "
                               "kept unchanged (`--reuse-base-records`); only LLM records were re-scored, from the replay cache. "
                               "Determinism now compares only rows with a readable verdict in every repeat: where an unreadable "
                               "reply fell in the repeated batch, the first scoring counted its rows as disagreements and the "
                               "new figure rests on the few rows left."])


def _pct_k_of_n(p: dict | None) -> str:
    """'92% (24/26)': the share with its counts, so a measurement on very few items shows as such."""
    return "-" if not p or p["value"] is None else f"{evaluate.fmt_pct(p)} ({p['k']}/{p['n']})"


def _ba(before: dict | None, after: dict | None) -> str:
    """'9/10 -> 1/10' (before -> after the alerting layer); one value when unchanged."""
    k = evaluate.fmt_k_of_n
    if not after:
        return "-"
    return k(after) if not before or (before["k"], before["n"]) == (after["k"], after["n"]) else f"{k(before)} -> {k(after)}"


def block_end_to_end(i: Inputs) -> str:
    """The plan's consequence on clean batches, before and after the batch alerting layer."""
    p = i.pipeline
    if not p:
        return PENDING
    c = p["clean"]
    if "any_alert_before" not in c:
        return "_The pipeline results predate the batch alerting layer: re-run `python -m project_b --steps pipeline,scale,report`._"
    fprs = [(name, v["clean_row_fpr"]["value"]) for name, v in p["problems"].items() if (v.get("clean_row_fpr") or {}).get("value") is not None]
    top_name, top = max(fprs, key=lambda item: item[1]) if fprs else (None, None)
    within = "" if top is None else (f" The highest row false-alarm rate on clean rows is {top:.2%} ({top_name}), "
                                     + ("within" if top <= pipeline.MAX_ROW_FPR else "above") + f" the {pipeline.MAX_ROW_FPR:.1%} cap, "
                                     f"and the row layer is identical before and after.")
    b = p.get("background") or {}
    real = (f" On real, unsanitised batches (which carry real issues): {_ba(b.get('any_alert_before'), b.get('any_alert'))} alerted."
            if b.get("any_alert") else "")
    still = sorted(((name, e["clean_batch_far"]) for name, e in p["problems"].items() if (e.get("clean_batch_far") or {}).get("k")),
                   key=lambda item: -item[1]["k"])
    if still:
        within += (" The clean-batch alerts that remain come from: "
                   + ", ".join(f"{name} {evaluate.fmt_k_of_n(far)}" + (" (critical: alerts on every flag)" if p["problems"][name].get("critical")
                                                                        else f" (threshold {p['problems'][name]['alert_threshold']})")
                               for name, far in still) + ".")
    return (f"Clean batches with any alert: **{evaluate.fmt_k_of_n(c['any_alert_before'])} before** the batch alerting layer, "
            f"**{evaluate.fmt_k_of_n(c['any_alert'])} after** it ({evaluate.fmt_rate(c['any_alert'])}); alerts per clean batch "
            f"{c['alerts_per_batch_before']:.1f} -> {c['alerts_per_batch']:.1f}.{within}{real}")


def block_alerting(i: Inputs) -> str:
    """The final alerting policy per check: threshold, its calibration, and before -> after on Sep-Dec."""
    p = i.pipeline
    if not p or "alerting" not in p:
        return PENDING
    cal = (p["alerting"].get("calibration") or {}).get("checks", {})
    lines = ["| check | batch alert from | Jul-Aug calibration batches alerting: at 1 flag -> at the threshold | "
             "flagged rows per clean batch: Jul-Aug 95th pct / max (calibration) vs Sep-Dec max (evaluation) | "
             "clean Sep-Dec batches alerting (batch FAR): before -> after | row FPR, clean rows (unchanged) | "
             "injected batches alerting, single-problem: before -> after | injected batches alerting, mixed: before -> after |",
             "|---|---|---|---|---|---|---|---|"]
    for name, e in p["problems"].items():
        c = cal.get(name)
        k = e["alert_threshold"]
        rule = "every flag (critical)" if e.get("critical") else ("every flag (never flagged in calibration)" if k == 1 else f"{k} flagged rows")
        calib = f"{c['batches_alerting_at_1']}/{c['n_batches']} -> {c['batches_alerting_at_threshold']}/{c['n_batches']}" if c else "-"
        single = e.get("single_batch_alert") if e["level"] == "row" else e.get("single_detection")
        single_before = e.get("single_batch_alert_before") if e["level"] == "row" else e.get("single_detection_before")
        mixed = e.get("mixed_batch_alert") if e["level"] == "row" else e.get("mixed_detection")
        mixed_before = e.get("mixed_batch_alert_before") if e["level"] == "row" else e.get("mixed_detection_before")
        counts = (f"{c['flagged_rows']['p95']} / {c['flagged_rows']['max']} vs {max(e.get('clean_flagged_rows') or [0])}"
                  if c else "-")
        lines.append(evaluate.table_row([f"{name} - {PROBLEMS[name]['title']}", rule, calib, counts,
                                         _ba(e.get("clean_batch_far_before"), e["clean_batch_far"]),
                                         evaluate.fmt_rate(e.get("clean_row_fpr"), 2), _ba(single_before, single), _ba(mixed_before, mixed)]))
    return "\n".join(lines)


def block_alerting_calibration_gap(i: Inputs) -> str:
    """Where the Jul-Aug calibration counts run far above what clean Sep-Dec batches flag (measured, not acted on)."""
    p = i.pipeline
    cal = ((p or {}).get("alerting") or {}).get("calibration") or {}
    if not cal:
        return PENDING
    gaps = []
    for name, c in cal["checks"].items():
        sep_dec = max(p["problems"][name].get("clean_flagged_rows") or [0])
        if c["threshold"] > 1 and c["flagged_rows"]["max"] > 2 * max(sep_dec, 1):
            gaps.append(f"{name} (Jul-Aug max {c['flagged_rows']['max']}, 95th percentile {c['flagged_rows']['p95']}; "
                        f"Sep-Dec clean max {sep_dec})")
    if not gaps:
        return "The flagged-row counts of the calibration batches are in line with those of clean Sep-Dec batches."
    return ("Calibration counts can run above what the final detectors flag. They come from detectors fitted on January-June "
            "alone and applied to July-August, so summer values unseen in January-June count as false flags there, while the "
            "final detectors are fitted on all of January-August. Where this shows: " + "; ".join(gaps) + ". Those thresholds "
            "are therefore conservative (fewer alerts, less sensitivity to small corruptions). They were not lowered after "
            "seeing the September-December counts, which would be tuning on evaluation data. A production system would "
            "recalibrate on a rolling window that matches how its detectors are refitted.")


def block_alerting_effect(i: Inputs) -> str:
    """Where the thresholds cost detection, read from the alert sweep and the evaluation batches."""
    p = i.pipeline
    if not p or "alert_sweep" not in p:
        return PENDING
    losses = [r for r in p["alert_sweep"] if r["alerted_after"]["k"] < r["alerted_before"]["k"]]
    rate = p["settings"]["injection_rate"]
    main_loss = [n for n, e in p["problems"].items() if e["level"] == "row" and e.get("single_batch_alert")
                 and e["single_batch_alert"]["k"] < (e.get("single_batch_alert_before") or e["single_batch_alert"])["k"]]
    mixed_loss = [n for n, e in p["problems"].items() if e["level"] == "row" and e.get("mixed_batch_alert")
                  and e["mixed_batch_alert"]["k"] < (e.get("mixed_batch_alert_before") or e["mixed_batch_alert"])["k"]]
    parts = [f"Row-level detection (recall) is unchanged by construction: the alerting layer reads the row flags and never edits them. "
             f"At the benchmark's {rate:.0%} injection rate, "
             + (f"single-problem batches lost their alert for {', '.join(main_loss)}" if main_loss else
                "no single-problem injected batch lost its alert")
             + (f"; in mixed batches, {', '.join(mixed_loss)} lost one." if mixed_loss else ", and no mixed batch lost one.")]
    held = [(n, h) for n, e in p["problems"].items() for h in e.get("held_back") or []]
    if held:
        parts.append("Held back: " + "; ".join(
            f"{n} in {h['kind']} batch {h['batch_id']} ({'+'.join(h['injected'])}): {h['injected_rows']} injected rows, "
            f"{h['flagged_rows']} flagged, threshold {h['threshold']}" for n, h in held)
            + ". In a mixed batch a type gets only the rows no earlier type took, so it can carry far fewer than "
            f"{rate:.0%} of the rows; every injected row was still flagged.")
    if losses:
        detail = "; ".join(f"{r['problem']} at {r['rate']:.1%} ({r['injected_rows_median']:,.0f} injected rows per batch, threshold "
                           f"{r['threshold']}): {evaluate.fmt_k_of_n(r['alerted_before'])} -> {evaluate.fmt_k_of_n(r['alerted_after'])}"
                           for r in losses)
        parts.append("The thresholds do cost detection when a corruption touches fewer rows than the threshold: a batch then alerts "
                     "only if false flags make up the difference. Measured in the alert sweep: " + detail + ". Those rows are still "
                     "flagged on the rows themselves; only the batch alert is held back.")
    else:
        parts.append("In the alert sweep no injected batch lost its alert at any rate.")
    return " ".join(parts)


def block_llm_evidence(i: Inputs) -> str:
    """Per problem, the measured LLM detector next to the choice: coverage and both views of its rates."""
    if not i.summary:
        return PENDING
    lines = ["| check | LLM detector | coverage | detection: overall / given a verdict | false alarms: overall / given a verdict | "
             "determinism | $ per 1M rows asked |", "|---|---|---|---|---|---|---|"]
    for e in i.summary["detectors"]:
        a, op = e.get("abstention"), e.get("operational")
        if e["family"] != "llm" or not a:
            continue
        det_key = "batch_detection" if e["level"] == "batch" else "detection"
        fa_key, dec = ("batch_far", 1) if e["level"] == "batch" else ("row_fpr", 2)
        lines.append(evaluate.table_row([
            f"{e['problem']} - {PROBLEMS[e['problem']]['title']}", e["detector"],
            f"{evaluate.fmt_pct(a['coverage'])} of {a['asked']:,} {a['unit']}",
            f"{evaluate.fmt_pct(op[det_key])} / {evaluate.fmt_pct(e[det_key])}",
            f"{evaluate.fmt_rate(op[fa_key], dec)} / {evaluate.fmt_rate(e[fa_key], dec)}",
            _pct_k_of_n(e.get("determinism")), evaluate.fmt_usd(op["cost_per_million_rows"]),
        ]))
    return "\n".join(lines)


def block_background(i: Inputs) -> str:
    if not i.summary or not i.summary.get("background"):
        return PENDING
    return "\n".join(evaluate.background_table(i.summary["background"])[4:])  # title and note are in the template


def _tables_without_title(path: Path) -> str | None:
    if not path.exists():
        return None
    return re.sub(r"^# .*\n", "", path.read_text(encoding="utf-8"), count=1).replace("\n## ", "\n#### ").strip()


def block_pipeline(i: Inputs) -> str:
    return _tables_without_title(i.dir / pipeline.TABLES_FILE) or PENDING


def block_scale(i: Inputs) -> str:
    path = i.dir / "scale_tables.md"
    return path.read_text(encoding="utf-8").strip() if path.exists() else PENDING


def block_plan_rule(i: Inputs) -> str:
    return (f"A rule / statistical detector qualifies when its false alarms stay within {pipeline.MAX_ROW_FPR:.1%} of clean rows "
            f"(row-level problems) or {pipeline.MAX_BATCH_FAR:.0%} of clean batches (batch-level problems). Among those, the "
            f"highest detection wins; within {100 * pipeline.DETECTION_TIE:.0f} points the rule wins (cheaper, deterministic, "
            f"self-explaining). For problems {', '.join(pipeline.UNION_PROBLEMS)} both run when both qualify (they see different "
            f"evidence). An LLM is added only as a confirmer, and only when the chosen detector's precision at the injection rate is "
            f"below {pipeline.MIN_PRECISION:.2f} and, on the very rows the LLM reviewed, it raises fewer false alarms while "
            f"catching at least as much (within {100 * pipeline.DETECTION_TIE:.0f} points). A detector counts as measured only with "
            f"at least {pipeline.MIN_EVIDENCE_BATCHES} corrupted batches behind it ({pipeline.MIN_LLM_EVIDENCE_BATCHES} for an LLM); "
            f"with less, the provisional step stays. Manual overrides: "
            f"{', '.join(pipeline.PLAN_OVERRIDES) or 'none'}.")


def block_decision_matrix(i: Inputs) -> str:
    plan, source = pipeline.load_plan(i.dir)
    det = {e["detector"]: e for e in (i.summary or {}).get("detectors", [])}
    families = {c.name: c.family for c in pipeline.detector_classes()}
    lines = [f"Plan source: **{source}**" + ("" if source == "measured" else " (no detector benchmark yet: every choice below is "
             "a design hypothesis, not a measured result)") + ".", "",
             "| check | choice | measurement / reason | trade-offs |", "|---|---|---|---|"]
    for p, step in plan.items():
        fams = sorted({families[n] for n in step.detectors}, key=["rule", "stat", "llm"].index)
        choice = " + ".join("statistical" if f == "stat" else f for f in fams)
        if step.confirm:
            choice = f"hybrid ({choice} + LLM confirmation)"
        trade = "; ".join(FAMILY_TRADEOFFS[f] for f in fams + (["llm"] if step.confirm else []))
        llm_det = det.get(step.confirm or "", {}).get("determinism")
        if step.confirm and llm_det and llm_det.get("value") is not None:
            trade += f" (measured determinism {100 * llm_det['value']:.0f}%)"
        lines.append(evaluate.table_row([f"{p} - {PROBLEMS[p]['title']}", choice, step.reason, trade]))
    lines += ["", "| pipeline component | choice | reason |", "|---|---|---|",
              "| schema / date-format gate (a, b, c) | rule (+ statistical for c) | a broken schema or date format makes every number in the batch suspect: the batch is quarantined |",
              "| LLM escalation | hybrid | only rows a cheap check flagged, one call per distinct value, a per-batch cap, verdicts cached; a value the LLM cannot judge keeps its cheap flag |",
              "| batch alerting layer | rule, with counts calibrated on Jan-Aug | critical checks (schema / type / date format, personal data, the per-batch null-spike statistic) alert on every flag; every other row check alerts from a minimum number of flagged rows set by the batch detectors' own 5 % rule on held-out Jul-Aug batches. Row flags are never changed |",
              "| ground truth | rule (hand-read screens) | contradictions (k) are only injected with texts a person read and kept: see RESULTS_B.md section 3 |"]
    return "\n".join(lines)


BLOCKS = {name.removeprefix("block_"): fn for name, fn in globals().items() if name.startswith("block_")}


def render(template: str, inputs: Inputs) -> str:
    def fill(match: re.Match) -> str:
        name = match.group(1)
        if name not in BLOCKS:
            raise KeyError(f"unknown block {{{{{name}}}}} in a template")
        return BLOCKS[name](inputs)

    return re.sub(r"\{\{(\w+)\}\}", fill, template)


def main(argv: list[str] | None = None) -> list[Path]:
    ap = argparse.ArgumentParser(prog="python -m project_b.report", description="Write RESULTS_B.md and DECISION_MATRIX.md from the result files.")
    ap.add_argument("--results-dir", type=Path, default=config.RESULTS_DIR)
    ap.add_argument("--out-dir", type=Path, default=config.ROOT)
    args = ap.parse_args(argv)
    inputs = Inputs(args.results_dir)
    written = []
    for template, output in OUTPUTS.items():
        path = args.out_dir / output
        path.write_text(render((TEMPLATES / template).read_text(encoding="utf-8"), inputs), encoding="utf-8")
        written.append(path)
    print(f"[report] wrote {', '.join(p.name for p in written)}")
    return written


if __name__ == "__main__":
    main()
