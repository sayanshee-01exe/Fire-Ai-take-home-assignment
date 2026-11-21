"""Task 5 - the hybrid pipeline: cheap checks on every row, statistics per batch, and the LLM
only where the measurements say it earns its cost.

Why it is built this way
* One step per problem type. A step runs one or more rule / statistical detectors on every row
  and ORs their flags. Optionally an LLM detector must CONFIRM the rows flagged by the detectors
  named in `confirm_flags_of` before they raise an alert (it can only remove false alarms, never
  add detections), and it judges each DISTINCT value once (`dedup_on`): 311 values are templated,
  so the LLM bill grows with how many new values appear, not with how many rows arrive.
* Fail safe. A row the LLM could not judge (no key, quota, budget cap, unparseable reply) keeps
  its cheap flag: an unconfirmed alert still goes out, it is just not filtered.
* Evidence first. plan_from_summary() picks each step from the detector benchmark
  (results/summary.json) with the written rule below; until that benchmark has run the
  pipeline uses DEFAULT_PLAN, whose every reason says "provisional". PLAN_OVERRIDES holds any
  decision a person makes against the rule, with its reason. All three are printed in the report.
* Quarantine. A schema or date-format break (problems a, b, c) makes every number in the batch
  suspect, so the whole batch is held back from the dashboards; other alerts flag rows / agencies.
* Two layers. The row layer above decides which rows are flagged. A separate batch alerting layer
  (AlertPolicy, DESIGN.md section 7a) decides whether a check raises a batch alert: critical checks
  (CRITICAL_PROBLEMS) always alert at once, the others only from a minimum number of flagged
  rows, calibrated on January-August data alone (calibrate_alert_policy). Flagged rows are always kept.

Evaluation (evaluate_pipeline): the SAME injected batches as the detector benchmark (single
problem), clean batches, batches carrying several corruption types at once (compose_mixed: each
type on its own rows, so every label stays exact) and real unsanitised batches. Every number
comes from results/pipeline_records.jsonl; nothing is estimated.

Usage: python -m project_b.pipeline [--quick] [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import pickle
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from project_b import config, evaluate, llm, metrics
from project_b.core import PROBLEMS, Usage, jsonable
from project_b.data.load import make_batches
from project_b.detectors import detector_classes

# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------
QUARANTINE_PROBLEMS = "abc"  # a broken schema or date format: hold the whole batch back
# Rule for choosing a step from the benchmark (decided before any benchmark result exists):
MAX_ROW_FPR = 0.002  # a row-level check may flag at most 0.2 % of clean rows ...
MAX_BATCH_FAR = 0.10  # ... a batch-level check may alert on at most 10 % of clean batches
DETECTION_TIE = 0.02  # detection within 2 points of the best counts as a tie: the cheaper / rule wins
MIN_PRECISION = 0.90  # below this precision at the injection rate, consider LLM confirmation
# Too little evidence decides nothing: a detector counts as measured only with at least this many
# corrupted batches behind it (a --quick run has 3 and leaves the provisional plan in place).
MIN_EVIDENCE_BATCHES = 10  # rule / stat (the full benchmark has config.N_CORRUPTED_BATCHES = 20)
MIN_LLM_EVIDENCE_BATCHES = 5  # LLM (the full benchmark samples config.LLM_N_BATCHES = 8)
# Detectors that see different evidence, so both run when both pass the false-alarm limit:
# c - the format rule cannot see a day/month swap that keeps the ISO shape, the timeline statistic can;
# f - the exact-key rule misses mistyped copies, the similarity statistic catches them;
# l - the regex catches phones and e-mails, the novelty score catches bare names.
UNION_PROBLEMS = "cfl"
# What an LLM confirmation is keyed on per problem (the fields its prompt shows); '?col' = only
# whether col is filled. Window problems (e, f) need neighbouring rows and are never confirmed.
LLM_DEDUP_ON: dict[str, tuple[str, ...]] = {
    "d": ("borough", "incident_zip", "latitude", "longitude"),
    "h": ("created_date", "closed_date", "status"),
    "i": ("incident_zip", "borough", "city", "incident_address"),
    "j": ("agency", "complaint_type", "descriptor"),
    "k": ("status", "resolution_description", "?closed_date"),
    "l": tuple(config.FREE_TEXT_COLUMNS),
}


@dataclass(frozen=True)
class Step:
    """How the pipeline checks one problem type."""

    problem: str
    detectors: tuple[str, ...]  # rule / stat detectors run on every batch; their flags are OR-ed
    confirm: str | None = None  # LLM detector that must confirm some flags before they alert
    confirm_flags_of: tuple[str, ...] = ()  # whose flags need that confirmation (others alert directly)
    reason: str = ""


def _llm(problem: str) -> str:
    return next(c.name for c in detector_classes() if c.problem == problem and c.family == "llm")


DEFAULT_PLAN: dict[str, Step] = {
    "a": Step("a", ("rule_schema_columns",), reason="provisional: an exact schema contract is free, deterministic and names the column"),
    "b": Step("b", ("rule_dtype_contract",), reason="provisional: an exact dtype contract is free, deterministic and names the column"),
    "c": Step("c", ("rule_datetime_format", "stat_datetime_shape_shift"),
              reason="provisional: the format rule misses day/month swaps that keep the ISO shape; the timeline statistic sees them"),
    "d": Step("d", ("rule_nyc_bbox",), reason="provisional: swapped coordinates leave the NYC box, which a rule checks exactly"),
    "e": Step("e", ("rule_exact_duplicate_rows",), reason="provisional: an exact duplicate is a hash match"),
    "f": Step("f", ("rule_near_duplicate_key", "stat_near_duplicate_similarity"),
              reason="provisional: the exact-key rule misses mistyped copies; the similarity statistic catches them"),
    "g": Step("g", ("stat_null_spike_binomial",), reason="provisional: normal null rates differ by agency and column, so a learned baseline beats one hand-set limit"),
    "h": Step("h", ("rule_closed_after_created",), reason="provisional: closed < created is a comparison"),
    "i": Step("i", ("stat_zip_borough_frequency",), reason="provisional: hand-coded ZIP prefixes false-alarm on ZIPs that span two boroughs"),
    "j": Step("j", ("rule_complaint_descriptor_allowlist",), confirm=_llm("j"), confirm_flags_of=("rule_complaint_descriptor_allowlist",),
              reason="provisional: unseen pairs are flagged for free; the LLM judges each distinct flagged pair once to drop legitimate new pairs"),
    "k": Step("k", ("stat_resolution_status_classifier",), confirm=_llm("k"), confirm_flags_of=("stat_resolution_status_classifier",),
              reason="provisional: the classifier flags unusual status/text pairs; the LLM reads each distinct flagged text once"),
    "l": Step("l", ("rule_pii_regex", "stat_pii_template_novelty"), confirm=_llm("l"), confirm_flags_of=("stat_pii_template_novelty",),
              reason="provisional: regex hits (phones, e-mails) alert directly; texts only the novelty score flags go to the LLM, which recognises names"),
}
PLAN_OVERRIDES: dict[str, Step] = {}  # a person's decision against the rule, with its reason (shown in the report)


# --------------------------------------------------------------------------
# The batch alerting layer (DESIGN.md section 7a)
# --------------------------------------------------------------------------
# Critical checks alert on every flag, however few: a broken schema / type / date format (a, b, c,
# which also quarantine the batch), personal data in free text (l: one exposed record is an
# incident), and g, whose detector is already calibrated per batch.
CRITICAL_PROBLEMS = "abcgl"
# Calibration stays inside the reference period: fit on Jan-Jun, count flags on Jul-Aug.
ALERT_CALIBRATION_SPLIT = "2023-07-01T00:00:00"
ALERT_TARGET_FAR = config.TARGET_BATCH_FAR  # the target every batch-level statistical detector already uses
ALERT_SWEEP_BATCHES = 5  # corrupted batches per (problem, rate) in the alert sweep
ALERT_FILE = "alert_thresholds.json"


def thresholded(problem: str) -> bool:
    """Row-level, non-critical checks are the ones a minimum flagged-row count applies to."""
    return PROBLEMS[problem]["level"] == "row" and problem not in CRITICAL_PROBLEMS


@dataclass(frozen=True)
class AlertPolicy:
    """Whether a check raises a batch alert. It never changes which rows are flagged."""

    min_rows: dict[str, int] = field(default_factory=dict)  # problem -> flagged rows a batch needs to alert
    source: str = "none: every flag alerts"

    def threshold(self, problem: str) -> int:
        return max(1, int(self.min_rows.get(problem, 1))) if thresholded(problem) else 1


def min_rows_threshold(counts: list[int], target: float = ALERT_TARGET_FAR) -> int:
    """Smallest k >= 1 such that at most int(target x n) of the calibration batches have >= k
    flagged rows (the rule the batch-level statistical detectors use for their thresholds)."""
    allowed = int(target * len(counts))
    k = 1
    while sum(c >= k for c in counts) > allowed:
        k += 1
    return k


def plan_from_summary(summary: dict | None) -> dict[str, Step]:
    """Each step from the detector benchmark (summary.json) by the rule above; DEFAULT_PLAN where
    a problem has no usable measurement; PLAN_OVERRIDES last."""
    plan = {}
    for problem, default in DEFAULT_PLAN.items():
        plan[problem] = _choose_step(problem, default, summary) if summary else default
    return {**plan, **PLAN_OVERRIDES}


def _choose_step(problem: str, default: Step, summary: dict) -> Step:
    entries = {e["family"]: e for e in summary["detectors"] if e["problem"] == problem}
    cheap = [entries[f] for f in ("rule", "stat") if f in entries and _measured(entries[f])]
    if not cheap:
        return replace(default, reason=f"{default.reason} (kept: no rule/stat detector measured on >= {MIN_EVIDENCE_BATCHES} corrupted batches)")
    passing = [e for e in cheap if _false_alarm(e) is not None and _false_alarm(e) <= _limit(problem)]
    pool = passing or cheap
    best = max(_value(e["detection"]) for e in pool)
    near = [e for e in pool if _value(e["detection"]) >= best - DETECTION_TIE]
    chosen = sorted(near, key=lambda e: (e["family"] != "rule", _false_alarm(e) if _false_alarm(e) is not None else 1.0))[0]
    names = [chosen["detector"]]
    if problem in UNION_PROBLEMS and len(passing) == 2:
        names = [e["detector"] for e in passing]
    why = "; ".join(f"{e['detector']} detection {_pct(e['detection'])}, false alarms {_pct(_fa(e))}" for e in cheap)
    if not passing:
        why += f"; none within the false-alarm limit ({_limit(problem):.1%}), so the best detector is used anyway"
    step = Step(problem, tuple(names), reason=f"measured: {why} -> {' + '.join(names)}")
    return _maybe_confirm(step, chosen, entries.get("llm"), summary)


def _maybe_confirm(step: Step, chosen: dict, llm_entry: dict | None, summary: dict) -> Step:
    """Add LLM confirmation when the cheap check's precision is low and, on the very rows the LLM
    reviewed, the LLM raises fewer false alarms while catching about as much."""
    problem = step.problem
    if problem not in LLM_DEDUP_ON:
        return replace(step, reason=step.reason + "; LLM confirmation does not apply (batch-level or window check)")
    if not llm_entry or not _measured(llm_entry, MIN_LLM_EVIDENCE_BATCHES):
        return replace(step, reason=step.reason + "; no LLM confirmation (the LLM was not measured on enough batches)")
    precision = chosen.get("precision_at_rate")
    same = {r["detector"]: r for r in summary.get("same_rows", {}).get(problem, [])}
    cheap_sr, llm_sr = same.get(chosen["detector"]), same.get(llm_entry["detector"])
    if precision is None or precision >= MIN_PRECISION or not cheap_sr or not llm_sr:
        return replace(step, reason=step.reason + f"; no LLM confirmation (precision {_num(precision)} >= {MIN_PRECISION} or not measured)")
    fewer_alarms = _value(llm_sr["row_fpr"]) < _value(cheap_sr["row_fpr"])
    as_good = _value(llm_sr["detection"]) >= _value(cheap_sr["detection"]) - DETECTION_TIE
    verdict = (f"precision {precision:.2f} < {MIN_PRECISION}; on the LLM's rows: LLM FPR {_pct(llm_sr['row_fpr'])} vs "
               f"{_pct(cheap_sr['row_fpr'])}, detection {_pct(llm_sr['detection'])} vs {_pct(cheap_sr['detection'])}")
    if fewer_alarms and as_good:
        return replace(step, confirm=llm_entry["detector"], confirm_flags_of=step.detectors, reason=f"{step.reason}; LLM confirms: {verdict}")
    return replace(step, reason=f"{step.reason}; no LLM confirmation: {verdict}")


def _measured(e: dict, min_batches: int = MIN_EVIDENCE_BATCHES) -> bool:
    enough = (e.get("n_batches") or {}).get("corrupted", 0) >= min_batches
    return e.get("status") in ("ok", "partial") and _value(e.get("detection")) is not None and enough


def _limit(problem: str) -> float:
    return MAX_ROW_FPR if PROBLEMS[problem]["level"] == "row" else MAX_BATCH_FAR


def _fa(e: dict) -> dict | None:
    return e.get("row_fpr") if PROBLEMS[e["problem"]]["level"] == "row" else e.get("batch_far")


def _false_alarm(e: dict) -> float | None:
    return _value(_fa(e))


def _value(p: dict | None) -> float | None:
    return None if not p else p.get("value")


def _pct(p: dict | None) -> str:
    return "-" if _value(p) is None else f"{100 * p['value']:.2f}%"


def _num(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"


def load_plan(results_dir: Path = config.RESULTS_DIR) -> tuple[dict[str, Step], str]:
    """The plan and where it came from: 'measured', 'partly measured' or 'provisional', judged by
    the steps themselves (a summary from a --quick run leaves every step provisional)."""
    path = results_dir / evaluate.SUMMARY_FILE
    plan = plan_from_summary(json.loads(path.read_text(encoding="utf-8")) if path.exists() else None)
    measured = sum(step.reason.startswith("measured") for step in plan.values())
    return plan, "measured" if measured == len(plan) else "partly measured" if measured else "provisional"


# --------------------------------------------------------------------------
# Running the pipeline on one batch
# --------------------------------------------------------------------------
@dataclass
class ProblemOutcome:
    batch_flag: bool
    row_flags: np.ndarray | None
    n_alerts: int
    alerts: list[dict]
    llm_asked: int = 0  # distinct values sent to the LLM in this batch
    llm_reused: int = 0  # distinct values already judged earlier in this run
    llm_unanswered: int = 0  # distinct values left unjudged (budget, error, no LLM): kept flagged
    vetoed_rows: int = 0  # cheap flags the LLM removed
    errors: list[str] = field(default_factory=list)
    raw_batch_flag: bool = False  # before the alerting layer: any flag or detector alert
    n_flagged: int = 0  # rows the step flagged (kept whatever the alerting layer decides)
    alert_threshold: int = 1  # flagged rows this check needs to raise a batch alert
    n_alerts_raw: int = 0


@dataclass
class PipelineResult:
    outcomes: dict[str, ProblemOutcome]
    quarantined: bool
    seconds: dict[str, float]  # local work per problem step: the rule / stat detectors
    llm_usage: Usage  # tokens and API latency of the LLM confirmations (as recorded, also when replayed)


class HybridPipeline:
    def __init__(self, plan: dict[str, Step] | None = None, max_confirm_items: int = config.PIPELINE_MAX_CONFIRM_ITEMS,
                 alerting: AlertPolicy | None = None):
        self.plan = plan or plan_from_summary(None)
        self.max_confirm_items = max_confirm_items
        self.alerting = alerting or AlertPolicy()
        names = {n for s in self.plan.values() for n in (*s.detectors, s.confirm) if n}
        self.detectors = {c.name: c() for c in detector_classes() if c.name in names}
        missing = names - set(self.detectors)
        if missing:
            raise ValueError(f"unknown detectors in the plan: {sorted(missing)}")
        self.verdicts: dict[tuple, bool] = {}  # (problem, distinct value) -> LLM says it is a problem

    def fit(self, reference: pd.DataFrame) -> dict[str, float]:
        seconds = {}
        for name, det in self.detectors.items():
            t0 = time.perf_counter()
            det.fit(reference.copy(deep=False))
            seconds[name] = time.perf_counter() - t0
        return seconds

    def run(self, batch: pd.DataFrame) -> PipelineResult:
        outcomes, seconds, usage = {}, {}, Usage()
        for problem, step in self.plan.items():
            outcomes[problem], seconds[problem] = self._run_step(step, batch, usage)
        quarantined = any(outcomes[p].batch_flag for p in QUARANTINE_PROBLEMS if p in outcomes)
        return PipelineResult(outcomes, quarantined, seconds, usage)

    def _run_step(self, step: Step, batch: pd.DataFrame, usage: Usage) -> tuple[ProblemOutcome, float]:
        """The step's outcome and its local work in seconds (the rule / stat detectors)."""
        results = {name: evaluate.run_detector(self.detectors[name], batch) for name in step.detectors}
        local_s = sum(r.usage.wall_s for r in results.values())
        errors = [f"{name}: {r.error}" for name, r in results.items() if r.error]
        alerts = [a.to_dict() for r in results.values() for a in r.alerts]
        needs_llm = {name for name in results if step.confirm and name in step.confirm_flags_of}
        flags = {name: r.row_flags for name, r in results.items() if r.row_flags is not None}
        direct = np.zeros(len(batch), dtype=bool)
        to_confirm = np.zeros(len(batch), dtype=bool)
        for name, f in flags.items():
            if name in needs_llm:
                to_confirm |= f
            else:
                direct |= f
        outcome = ProblemOutcome(False, direct.copy() if flags else None, 0, [], errors=errors)
        if to_confirm.any():
            self._confirm(step, batch, to_confirm & ~direct, outcome, usage)
        # a batch verdict without row flags (schema checks) alerts directly; flags that needed the
        # LLM count only for the rows it confirmed (or could not judge)
        flagged = any(r.batch_flag for name, r in results.items() if name not in needs_llm)
        flagged = flagged or (outcome.row_flags is not None and bool(outcome.row_flags.any()))
        outcome.raw_batch_flag, outcome.n_alerts_raw = flagged, len(alerts) if flagged else 0
        outcome.n_flagged = int(outcome.row_flags.sum()) if outcome.row_flags is not None else 0
        # the batch alerting layer: a separate decision on top of the (unchanged) row flags
        outcome.alert_threshold = k = self.alerting.threshold(step.problem)
        structural = any(r.batch_flag and (r.row_flags is None or not np.asarray(r.row_flags).any())
                         for name, r in results.items() if name not in needs_llm)  # an alert without row flags passes at once
        alert = flagged if k <= 1 else (structural or outcome.n_flagged >= k)
        outcome.batch_flag = alert
        outcome.n_alerts = len(alerts) if alert else 0
        outcome.alerts = alerts[: evaluate.MAX_ALERTS_RECORDED] if alert else []
        return outcome, local_s

    def _confirm(self, step: Step, batch: pd.DataFrame, candidates: np.ndarray, outcome: ProblemOutcome, usage: Usage) -> None:
        """Ask the LLM about each distinct flagged value once; broadcast its verdict to every row with
        that value. Values it could not judge stay flagged."""
        keys = _dedup_keys(batch, LLM_DEDUP_ON[step.problem])
        rows = np.flatnonzero(candidates)
        first_row: dict[Any, int] = {}
        for pos in rows:
            first_row.setdefault(keys[pos], int(pos))
        new = [k for k in first_row if (step.problem, k) not in self.verdicts]
        outcome.llm_reused = len(first_row) - len(new)
        asked = new[: self.max_confirm_items]
        outcome.llm_asked = len(asked)
        if asked:
            review = np.array(sorted(first_row[k] for k in asked))
            result = evaluate.run_detector(self.detectors[step.confirm], batch, review)
            usage.add(result.usage)
            if result.error:
                outcome.errors.append(f"{step.confirm}: {result.error}")
            else:
                reviewed = np.asarray(result.reviewed, dtype=bool) if result.reviewed is not None else np.isin(np.arange(len(batch)), review)
                llm_flags = np.asarray(result.row_flags, dtype=bool) if result.row_flags is not None else np.zeros(len(batch), dtype=bool)
                for k in asked:
                    if reviewed[first_row[k]]:
                        self.verdicts[(step.problem, k)] = bool(llm_flags[first_row[k]])
        confirmed = np.array([self.verdicts.get((step.problem, keys[p]), True) for p in rows], dtype=bool)  # unjudged: stays flagged
        outcome.llm_unanswered = sum((step.problem, k) not in self.verdicts for k in first_row)
        outcome.vetoed_rows = int((~confirmed).sum())
        outcome.row_flags[rows[confirmed]] = True


def _dedup_keys(batch: pd.DataFrame, columns: tuple[str, ...]) -> list[tuple]:
    """One hashable key per row from the fields the LLM sees ('?col' = is col filled). Missing
    columns read as missing values, so a renamed column never crashes the pipeline."""
    parts = []
    for spec in columns:
        name = spec.lstrip("?")
        values = batch[name] if name in batch.columns else pd.Series(np.nan, index=batch.index, dtype=object)
        if isinstance(values, pd.DataFrame):  # duplicated column name
            values = values.iloc[:, 0]
        parts.append(values.notna().tolist() if spec.startswith("?") else values.astype(object).where(values.notna(), None).tolist())
    return list(zip(*parts)) if parts else [()] * len(batch)


# --------------------------------------------------------------------------
# Calibrating the alerting layer: January-August only
# --------------------------------------------------------------------------
def calibrate_alert_policy(derived_dir: Path, plan: dict[str, Step], batch_size: int = config.BATCH_SIZE,
                           max_batches: int | None = None) -> tuple[AlertPolicy, dict]:
    """min_rows per thresholded check from the reference period alone (DESIGN.md 7a): fit the step's
    rule / stat detectors on the clean pool before ALERT_CALIBRATION_SPLIT (Jan-Jun), count the rows
    each step flags in every clean batch from the split to config.REFERENCE_END (Jul-Aug), and take
    min_rows_threshold of those counts. LLM confirmation is off here (no Gemini call); it could only
    remove flags. The evaluation period is never read."""
    reference = evaluate.read_period(derived_dir / evaluate.POOL_FILE, reference=True)
    created = reference["created_date"].astype(str)
    if not (created < config.REFERENCE_END).all():
        raise ValueError("calibration data reaches the evaluation period")
    fit_part = reference[created < ALERT_CALIBRATION_SPLIT]
    calibration = reference[created >= ALERT_CALIBRATION_SPLIT].reset_index(drop=True)
    steps = {p: replace(step, confirm=None, confirm_flags_of=()) for p, step in plan.items() if thresholded(p)}
    pipe = HybridPipeline(steps)
    pipe.fit(fit_part)
    batches = make_batches(calibration, batch_size)[:max_batches]
    counts: dict[str, list[int]] = {p: [] for p in steps}
    for batch in batches:
        result = pipe.run(batch)
        for p, outcome in result.outcomes.items():
            counts[p].append(outcome.n_flagged)
    min_rows = {p: min_rows_threshold(c) for p, c in counts.items()}
    checks = {}
    for p, c in counts.items():
        ordered = sorted(c)
        checks[p] = {
            "threshold": min_rows[p], "n_batches": len(c), "allowed_to_alert": int(ALERT_TARGET_FAR * len(c)),
            "batches_alerting_at_1": sum(x >= 1 for x in c), "batches_alerting_at_threshold": sum(x >= min_rows[p] for x in c),
            "flagged_rows": {"mean": float(np.mean(c)) if c else None, "median": ordered[len(c) // 2] if c else None,
                             "p95": ordered[min(len(c) - 1, int(0.95 * len(c)))] if c else None, "max": max(c, default=None)},
            "flagged_rows_per_batch": c,
        }
    report = {
        "rule": (f"min_rows = the smallest k >= 1 such that at most int({ALERT_TARGET_FAR} x n) calibration batches have >= k "
                 "flagged rows; critical checks alert on every flag"),
        "target_batch_far": ALERT_TARGET_FAR, "critical": sorted(CRITICAL_PROBLEMS),
        "fit_period": f"clean pool, created before {ALERT_CALIBRATION_SPLIT[:10]}", "fit_rows": len(fit_part),
        "calibration_period": f"clean pool, created {ALERT_CALIBRATION_SPLIT[:10]} to before {config.REFERENCE_END[:10]}",
        "calibration_rows": len(calibration), "batch_size": batch_size, "llm_confirmation": "off (no Gemini call)",
        "checks": checks,
    }
    return AlertPolicy(min_rows, source=f"calibrated on Jan-Aug ({len(batches)} held-out Jul-Aug batches)"), report


# --------------------------------------------------------------------------
# Batches with several corruption types
# --------------------------------------------------------------------------
INSERTING_PROBLEMS = "ef"  # add rows, so they go last and earlier labels follow the rows they label
MIXED_POOL = "cdefghijkl"  # a / b break the schema: the gate quarantines such a batch whatever it holds,
# so they are measured on single-problem batches only (a dropped column would also erase the evidence
# of the row problems in the same batch and turn their labels into unanswerable questions)
MIXED_KEY = 103  # seed tag for the mixed experiment (evaluate.py's tags are 100-102)


@dataclass
class Mixed:
    df: pd.DataFrame
    labels: dict[str, np.ndarray]  # problem -> rows that carry it
    batch_labels: dict[str, bool]
    params: dict[str, dict]


def compose_mixed(batch: pd.DataFrame, problems: list[str], injector: Any, ctx: Any, generator: np.random.Generator) -> Mixed:
    """Several corruption types in one batch, each on its OWN rows, so every label stays exact:
    each non-inserting type is injected into the clean batch on its own and only its rows that no
    earlier type took are copied over; then at most one inserting type (e / f) runs on the result,
    and earlier labels follow their rows (a copy of a corrupted row carries that corruption too)."""
    base = batch.reset_index(drop=True)
    combined, taken = base.copy(), np.zeros(len(base), dtype=bool)
    labels, params = {}, {}
    for problem in [p for p in problems if p not in INSERTING_PROBLEMS]:
        inj = injector.inject(base, problem, generator, ctx)
        rows = np.asarray(inj.row_labels, dtype=bool) & ~taken
        for column in base.columns:
            combined.loc[rows, column] = inj.df.loc[rows, column]
        labels[problem], params[problem] = rows, inj.params
        taken |= rows
    inserting = [p for p in problems if p in INSERTING_PROBLEMS]
    if len(inserting) > 1:
        raise ValueError("at most one inserting problem (e or f) per mixed batch")
    if inserting:
        problem = inserting[0]
        inj = injector.inject(combined, problem, generator, ctx)
        old_of_new = _old_positions(len(inj.df), inj.params.get("copy_of") or {})
        labels = {p: lab[old_of_new] for p, lab in labels.items()}
        labels[problem], params[problem] = np.asarray(inj.row_labels, dtype=bool), inj.params
        combined = inj.df
    return Mixed(combined.reset_index(drop=True), labels, {p: bool(lab.any()) for p, lab in labels.items()}, params)


def _old_positions(n_new: int, copy_of: dict) -> np.ndarray:
    """For each row of the frame after an inserting injection, its row before it: originals keep
    their order; a copy maps to its original's old row."""
    copies = {int(c): int(o) for c, o in copy_of.items()}
    old = np.empty(n_new, dtype=int)
    originals = [p for p in range(n_new) if p not in copies]
    old[originals] = np.arange(len(originals))
    for copy, original in copies.items():
        old[copy] = old[original]
    return old


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------
RECORDS_FILE, SUMMARY_FILE, TABLES_FILE = "pipeline_records.jsonl", "pipeline_summary.json", "pipeline_tables.md"


@dataclass(frozen=True)
class PipelineBudget:
    n_single: int = config.PIPELINE_N_BATCHES
    n_clean: int = config.PIPELINE_N_CLEAN
    n_mixed: int = config.PIPELINE_N_MIXED
    problems_per_mixed: int = config.PIPELINE_PROBLEMS_PER_MIXED
    n_background: int = config.PIPELINE_N_CLEAN
    n_calibration: int | None = None  # Jul-Aug calibration batches (None = all)
    n_sweep: int = ALERT_SWEEP_BATCHES  # corrupted batches per (problem, rate) in the alert sweep


QUICK = PipelineBudget(n_single=2, n_clean=2, n_mixed=3, n_background=2, n_calibration=6, n_sweep=1)


def record(kind: str, batch_id: int, df: pd.DataFrame, result: PipelineResult, wall_s: float,
           labels: dict[str, np.ndarray] | None = None, batch_labels: dict[str, bool] | None = None,
           sweep: dict | None = None) -> dict:
    """One JSON line per pipeline run: for every problem, what the pipeline said and (where known)
    the truth. Problems not injected into a corrupted / mixed batch have all-False truth there."""
    labels, batch_labels = labels or {}, batch_labels or {}
    per_problem = {}
    for problem, out in result.outcomes.items():
        truth = labels.get(problem, np.zeros(len(df), dtype=bool))
        entry = {"batch_label": batch_labels.get(problem, False), "batch_flag": out.batch_flag, "n_alerts": out.n_alerts,
                 "llm_asked": out.llm_asked, "llm_reused": out.llm_reused, "llm_unanswered": out.llm_unanswered,
                 "vetoed_rows": out.vetoed_rows, "errors": out.errors, "alerts": out.alerts[:2],
                 # the alerting layer: raw_batch_flag is the verdict before it ("every flag alerts")
                 "raw_batch_flag": out.raw_batch_flag, "n_alerts_raw": out.n_alerts_raw, "n_flagged": out.n_flagged,
                 "alert_threshold": out.alert_threshold}
        if out.row_flags is not None:
            entry.update(metrics.confusion(out.row_flags, truth, np.ones(len(df), dtype=bool)))
        per_problem[problem] = entry
    return {"kind": kind, "batch_id": batch_id, "injected": sorted(batch_labels), "sweep": sweep, "n_rows": len(df), "quarantined": result.quarantined,
            "wall_s": wall_s, "seconds": result.seconds, "usage": {k: getattr(result.llm_usage, k) for k in evaluate.USAGE_KEYS},
            "problems": per_problem}


def evaluate_pipeline(derived_dir: Path, out_dir: Path, plan: dict[str, Step], plan_source: str,
                      budget: PipelineBudget = PipelineBudget(), bench: evaluate.Budget = evaluate.Budget(),
                      injector: Any = None) -> dict:
    """Run the pipeline on single-problem, clean, mixed and real batches; write records, summary, tables."""
    import importlib

    injector = injector or importlib.import_module("project_b.inject")
    # 1. the alerting layer, from January-August only, before any evaluation data is read
    alerting, alert_report = calibrate_alert_policy(derived_dir, plan, bench.batch_size, budget.n_calibration)
    (out_dir / ALERT_FILE).write_text(json.dumps(jsonable(alert_report), indent=2), encoding="utf-8")
    evaluate.log(f"[pipeline] alert thresholds ({alerting.source}): "
                 + ", ".join(f"{p} {k}" for p, k in alerting.min_rows.items()))
    # 2. the pipeline itself, fitted on all of January-August as before
    pipe = HybridPipeline(plan, alerting=alerting)
    fit_s = pipe.fit(evaluate.read_period(derived_dir / evaluate.POOL_FILE, reference=True))
    recorder = evaluate.Recorder(out_dir / RECORDS_FILE)
    batches = make_batches(evaluate.read_period(derived_dir / evaluate.POOL_FILE, reference=False), bench.batch_size)
    clean_ids, corrupted_ids = evaluate.choose_batches(len(batches), bench, "".join(PROBLEMS))
    with open(derived_dir / evaluate.CONTEXT_FILE, "rb") as fh:
        ctx = pickle.load(fh)
    exp = evaluate.Experiment(batches, clean_ids, corrupted_ids, injector, ctx, bench, recorder)

    def run_one(kind, batch_id, df, labels=None, batch_labels=None, sweep=None):
        t0 = time.perf_counter()
        result = pipe.run(df)
        recorder.add(record(kind, batch_id, df, result, time.perf_counter() - t0, labels, batch_labels, sweep))

    for problem in PROBLEMS:  # the benchmark's own injected batches: like for like
        for b in corrupted_ids[problem][: budget.n_single]:
            inj = exp.corrupt(problem, b)
            run_one("single", b, inj.df, {problem: np.asarray(inj.row_labels, dtype=bool)}, {problem: bool(inj.batch_label)})
        evaluate.log(f"[pipeline] {problem}: {min(budget.n_single, len(corrupted_ids[problem]))} single-problem batches")
    for b in clean_ids[: budget.n_clean]:
        run_one("clean", b, batches[b].reset_index(drop=True))
    used = set(clean_ids)
    rest = [b for b in range(len(batches)) if b not in used]
    mixed_ids = evaluate.rng(MIXED_KEY).permutation(rest)[: budget.n_mixed].tolist() if rest else []
    for i, b in enumerate(mixed_ids):
        generator = evaluate.rng(MIXED_KEY, i)
        problems = _draw_mix(generator, budget.problems_per_mixed)
        mixed = compose_mixed(batches[b], problems, injector, ctx, generator)
        run_one("mixed", b, mixed.df, mixed.labels, mixed.batch_labels)
    evaluate.log(f"[pipeline] {budget.n_clean} clean, {len(mixed_ids)} mixed batches")
    eval_raw = evaluate.read_period(derived_dir / evaluate.FEED_FILE, reference=False)
    raw_batches = make_batches(eval_raw, bench.batch_size)
    for b in evaluate.rng(evaluate.BACKGROUND_KEY).permutation(len(raw_batches))[: budget.n_background].tolist():
        run_one("raw_background", b, raw_batches[b])
    # 3. what the thresholds cost: each thresholded problem at every sweep rate, on the benchmark's own batches
    for problem in (p for p in plan if thresholded(p)):
        for rate in config.RATE_SWEEP:
            for b in corrupted_ids[problem][: budget.n_sweep]:
                inj = exp.corrupt(problem, b, rate=rate)
                run_one("alert_sweep", b, inj.df, {problem: np.asarray(inj.row_labels, dtype=bool)}, {problem: bool(inj.batch_label)},
                        {"problem": problem, "rate": rate})
    evaluate.log(f"[pipeline] alert sweep: rates {config.RATE_SWEEP} x {budget.n_sweep} batches per thresholded problem")
    model = llm.get_client().model
    summary = jsonable(summarize(recorder.records, plan, plan_source, fit_s, model, budget, bench, alerting, alert_report))
    (out_dir / SUMMARY_FILE).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / TABLES_FILE).write_text(render_tables(summary), encoding="utf-8")
    evaluate.log(f"[pipeline] {len(recorder.records)} records; summary and tables written to {out_dir}")
    return summary


def _draw_mix(generator: np.random.Generator, k: int) -> list[str]:
    """k distinct problem types from MIXED_POOL with at most one inserting type, in a random order."""
    while True:
        picks = generator.choice(list(MIXED_POOL), size=k, replace=False).tolist()
        if sum(p in INSERTING_PROBLEMS for p in picks) <= 1:
            return picks


MAIN_KINDS = ("single", "clean", "mixed")  # what cost and throughput are measured on (not the real or sweep batches)


def summarize(records: list[dict], plan: dict[str, Step], plan_source: str, fit_s: dict, model: str | None,
              budget: PipelineBudget, bench: evaluate.Budget, alerting: AlertPolicy | None = None,
              alert_report: dict | None = None) -> dict:
    per_problem = {p: _problem_summary(p, records) for p in plan}
    clean = [r for r in records if r["kind"] == "clean"]
    judged = [r for r in records if r["kind"] in MAIN_KINDS]
    alerting = alerting or AlertPolicy()
    rows = sum(r["n_rows"] for r in judged)
    usage = {k: sum(r["usage"][k] for r in judged) for k in evaluate.USAGE_KEYS}
    # local work = the rule / stat detectors; the LLM's time is waiting on the API (its recorded
    # latency, the same whether answered live or replayed) and is priced by tokens, not CPU
    cpu = sum(sum(r["seconds"].values()) for r in judged)
    asked = sum(e["llm_asked"] for r in judged for e in r["problems"].values())
    return {
        "plan_source": plan_source,
        "plan": {p: asdict(s) for p, s in plan.items()},
        "settings": {"budget": asdict(budget), "batch_size": bench.batch_size, "seed": config.SEED, "llm_model": model,
                     "max_confirm_items": config.PIPELINE_MAX_CONFIRM_ITEMS, "injection_rate": config.ROW_INJECTION_RATE},
        "fit_s": fit_s,
        "alerting": {"source": alerting.source, "critical": sorted(CRITICAL_PROBLEMS),
                     "min_rows": {p: alerting.threshold(p) for p in plan}, "calibration": alert_report},
        "problems": per_problem,
        "clean": {"n_batches": len(clean),
                  "any_alert": metrics.proportion(sum(any(e["batch_flag"] for e in r["problems"].values()) for r in clean), len(clean)),
                  "any_alert_before": metrics.proportion(sum(any(e.get("raw_batch_flag", e["batch_flag"]) for e in r["problems"].values())
                                                             for r in clean), len(clean)),
                  "quarantined": metrics.proportion(sum(r["quarantined"] for r in clean), len(clean)),
                  "alerts_per_batch": float(np.mean([sum(e["n_alerts"] for e in r["problems"].values()) for r in clean])) if clean else None,
                  "alerts_per_batch_before": float(np.mean([sum(e.get("n_alerts_raw", e["n_alerts"]) for e in r["problems"].values())
                                                            for r in clean])) if clean else None},
        "alert_sweep": _alert_sweep(records),
        "background": _background(records),
        "llm_by_kind": _llm_by_kind(records),
        "cost": {"rows": rows, "llm_usage": usage, "llm_distinct_values_asked": asked,
                 "llm_values_per_million_rows": 1e6 * asked / rows if rows else None,
                 "cost_per_million_rows": metrics.cost_per_million_rows(rows, cpu_seconds=cpu, input_tokens=usage["input_tokens"],
                                                                        output_tokens=usage["output_tokens"], model=model)},
        "throughput": {"rows_per_s_excluding_llm_wait": rows / cpu if cpu > 0 else None,
                       "rows_per_s_with_llm_wait": rows / (cpu + usage["llm_latency_s"]) if cpu > 0 else None,
                       "median_batch_local_s": float(np.median([sum(r["seconds"].values()) for r in judged])) if judged else None,
                       "llm_latency_s_total": usage["llm_latency_s"],
                       "seconds_per_step": {p: float(np.median([r["seconds"][p] for r in judged])) for p in plan} if judged else {}},
    }


def _problem_summary(problem: str, records: list[dict]) -> dict:
    """Detection where the problem was injected (single-problem and mixed batches separately) and
    false alarms where it was not (clean batches, and mixed batches without it)."""
    level = PROBLEMS[problem]["level"]
    out = {"level": level, "critical": problem in CRITICAL_PROBLEMS}

    def alerted(entries: list[dict], before: bool) -> dict:
        return metrics.proportion(sum(e.get("raw_batch_flag", e["batch_flag"]) if before else e["batch_flag"] for e in entries), len(entries))

    for kind in ("single", "mixed"):
        hit = [r["problems"][problem] for r in records if r["kind"] == kind and r["problems"][problem]["batch_label"]]
        if level == "row":
            c = metrics.sum_counts([e for e in hit if e.get("tp") is not None])
            out[f"{kind}_detection"] = metrics.proportion(c["tp"], c["tp"] + c["fn"]) if hit else None  # row recall: the row layer
            out[f"{kind}_batch_alert"] = alerted(hit, before=False) if hit else None  # injected batches that raise an alert
            out[f"{kind}_batch_alert_before"] = alerted(hit, before=True) if hit else None
        else:
            out[f"{kind}_detection"] = alerted(hit, before=False) if hit else None
            out[f"{kind}_detection_before"] = alerted(hit, before=True) if hit else None
    clean = [r["problems"][problem] for r in records if r["kind"] == "clean"]
    others = [r["problems"][problem] for r in records if r["kind"] == "mixed" and not r["problems"][problem]["batch_label"]]
    out["alert_threshold"] = max((e.get("alert_threshold", 1) for r in records for e in [r["problems"][problem]]), default=1)
    # injected batches whose alert the layer held back, with why (fewer flagged rows than the threshold)
    out["held_back"] = [{"kind": r["kind"], "batch_id": r["batch_id"], "injected": r["injected"],
                         "injected_rows": (e.get("tp") or 0) + (e.get("fn") or 0), "flagged_rows": e.get("n_flagged"),
                         "threshold": e.get("alert_threshold")}
                        for r in records if r["kind"] in ("single", "mixed") for e in [r["problems"][problem]]
                        if e["batch_label"] and e.get("raw_batch_flag") and not e["batch_flag"]]
    out["clean_flagged_rows"] = [r["problems"][problem].get("n_flagged", 0) for r in records if r["kind"] == "clean"]
    out["clean_batch_far"] = alerted(clean, before=False)
    out["clean_batch_far_before"] = alerted(clean, before=True)
    out["mixed_batch_far"] = alerted(others, before=False)
    out["mixed_batch_far_before"] = alerted(others, before=True)
    if level == "row":
        c = metrics.sum_counts([e for e in clean if e.get("tp") is not None])
        out["clean_row_fpr"] = metrics.proportion(c["fp"], c["fp"] + c["tn"])
    judged = [r["problems"][problem] for r in records if r["kind"] in MAIN_KINDS]
    out["llm"] = {k: sum(e[k] for e in judged) for k in ("llm_asked", "llm_reused", "llm_unanswered", "vetoed_rows")}
    out["n_errors"] = sum(bool(e["errors"]) for e in judged)
    return out


def _llm_by_kind(records: list[dict]) -> dict:
    """LLM traffic per kind of batch. Clean and real batches are normal traffic: what a customer's
    feed costs day to day (the scale projection uses these); corrupted batches show the worst case."""
    out = {}
    for kind in ("single", "mixed", "clean", "raw_background"):
        recs = [r for r in records if r["kind"] == kind]
        if not recs:
            continue
        rows = sum(r["n_rows"] for r in recs)
        values = sum(e["llm_asked"] for r in recs for e in r["problems"].values())
        usage = {k: sum(r["usage"][k] for r in recs) for k in evaluate.USAGE_KEYS}
        out[kind] = {"batches": len(recs), "rows": rows, "values_asked": values, "values_per_million_rows": 1e6 * values / rows,
                     "local_s": sum(sum(r["seconds"].values()) for r in recs), **usage}
    return out


def _alert_sweep(records: list[dict]) -> list[dict]:
    """Per thresholded problem and injection rate: injected batches that raise an alert before and after
    the alerting layer, with the rows injected and flagged (what a threshold can cost in detection)."""
    groups: dict[tuple, list[dict]] = {}
    for r in records:
        if r["kind"] == "alert_sweep" and r["sweep"]:
            groups.setdefault((r["sweep"]["problem"], r["sweep"]["rate"]), []).append(r["problems"][r["sweep"]["problem"]])
    out = []
    for (problem, rate), entries in sorted(groups.items()):
        hit = [e for e in entries if e["batch_label"]]
        c = metrics.sum_counts([e for e in hit if e.get("tp") is not None])
        out.append({"problem": problem, "rate": rate, "n_batches": len(hit), "threshold": max((e["alert_threshold"] for e in hit), default=1),
                    "injected_rows_median": float(np.median([e["tp"] + e["fn"] for e in hit])) if hit else None,
                    "flagged_rows_min": min((e["n_flagged"] for e in hit), default=None),
                    "row_recall": metrics.proportion(c["tp"], c["tp"] + c["fn"]) if hit else None,
                    "alerted_before": metrics.proportion(sum(e["raw_batch_flag"] for e in hit), len(hit)),
                    "alerted_after": metrics.proportion(sum(e["batch_flag"] for e in hit), len(hit))})
    return out


def _background(records: list[dict]) -> dict:
    raw = [r for r in records if r["kind"] == "raw_background"]
    if not raw:
        return {}
    return {"n_batches": len(raw),
            "quarantined": metrics.proportion(sum(r["quarantined"] for r in raw), len(raw)),
            "alerts_per_batch": {p: float(np.mean([r["problems"][p]["n_alerts"] for r in raw])) for p in raw[0]["problems"]},
            "alerts_per_batch_before": {p: float(np.mean([r["problems"][p].get("n_alerts_raw", r["problems"][p]["n_alerts"]) for r in raw]))
                                        for p in raw[0]["problems"]},
            "any_alert": metrics.proportion(sum(any(e["batch_flag"] for e in r["problems"].values()) for r in raw), len(raw)),
            "any_alert_before": metrics.proportion(sum(any(e.get("raw_batch_flag", e["batch_flag"]) for e in r["problems"].values())
                                                       for r in raw), len(raw)),
            "flagged_per_1000_rows": {p: 1000 * sum(r["problems"][p].get("fp") or 0 for r in raw) / sum(r["n_rows"] for r in raw)
                                      for p in raw[0]["problems"]}}


# --------------------------------------------------------------------------
# pipeline_tables.md
# --------------------------------------------------------------------------
def render_tables(s: dict) -> str:
    fmt, num = evaluate.fmt_rate, evaluate.fmt_num
    lines = ["# Project B - hybrid pipeline", "",
             f"Plan source: **{s['plan_source']}**. Computed from `{RECORDS_FILE}` by `python -m project_b.pipeline`. "
             f"{s['settings']['budget']['n_single']} single-problem batches per problem (the detector benchmark's own), "
             f"{s['settings']['budget']['n_clean']} clean, {s['settings']['budget']['n_mixed']} mixed "
             f"({s['settings']['budget']['problems_per_mixed']} corruption types each), {s['settings']['budget']['n_background']} real "
             f"unsanitised batches of {s['settings']['batch_size']:,} rows. LLM model {s['settings']['llm_model'] or '-'}.", "",
             "## Plan", "", *evaluate.table_header(["problem", "detectors (flags OR-ed)", "LLM confirms", "why"])]
    for p, step in s["plan"].items():
        confirm = f"{step['confirm']} (flags of {', '.join(step['confirm_flags_of'])})" if step["confirm"] else "-"
        lines.append(evaluate.table_row([p, " + ".join(step["detectors"]), confirm, step["reason"]]))
    lines += ["", "## Detection and false alarms per problem", "",
              "Row columns come from the row layer, which the alerting layer never changes. Batch columns show the "
              "alert before the alerting layer -> after it (\"before\" = every flag alerts; a single value = unchanged).", "",
              *evaluate.table_header(["problem", "alert from (rows)", "row detection (single)", "row detection (mixed)",
                                      "row FPR (clean)", "batch alert, injected (single)", "batch alert, injected (mixed)",
                                      "batch FAR (clean)", "batch FAR (mixed, not injected)", "LLM values asked / reused / unanswered",
                                      "errors"])]
    for p, e in s["problems"].items():
        llm_counts = e["llm"]
        k = "every flag (critical)" if e.get("critical") else str(e.get("alert_threshold", 1))
        single_alert = _before_after(e.get("single_batch_alert_before"), e.get("single_batch_alert")) if e["level"] == "row" else \
            _before_after(e.get("single_detection_before"), e.get("single_detection"))
        mixed_alert = _before_after(e.get("mixed_batch_alert_before"), e.get("mixed_batch_alert")) if e["level"] == "row" else \
            _before_after(e.get("mixed_detection_before"), e.get("mixed_detection"))
        lines.append(evaluate.table_row([
            f"{p} - {PROBLEMS[p]['title']}", k,
            fmt(e["single_detection"]) if e["level"] == "row" else "-", fmt(e["mixed_detection"]) if e["level"] == "row" else "-",
            fmt(e.get("clean_row_fpr"), 2), single_alert, mixed_alert,
            _before_after(e.get("clean_batch_far_before"), e["clean_batch_far"]),
            _before_after(e.get("mixed_batch_far_before"), e["mixed_batch_far"]),
            f"{llm_counts['llm_asked']} / {llm_counts['llm_reused']} / {llm_counts['llm_unanswered']}", e["n_errors"]]))
    lines += alert_calibration_table(s.get("alerting") or {})
    lines += alert_sweep_table(s.get("alert_sweep") or [])
    c, cost, tp = s["clean"], s["cost"], s["throughput"]
    lines += ["", "## End to end", "",
              f"* Clean batches with any alert: before the alerting layer {fmt(c.get('any_alert_before'))}, after it "
              f"{fmt(c['any_alert'])}; quarantined: {fmt(c['quarantined'])}; alerts per clean batch: "
              f"{num(c.get('alerts_per_batch_before'), '.1f')} -> {num(c['alerts_per_batch'], '.1f')}.",
              f"* Cost per 1M rows: {evaluate.fmt_usd(cost['cost_per_million_rows'])} "
              f"(LLM: {cost['llm_usage']['llm_calls']} calls, {cost['llm_usage']['input_tokens']:,} input / "
              f"{cost['llm_usage']['output_tokens']:,} output tokens over {cost['rows']:,} rows; "
              f"{num(cost['llm_values_per_million_rows'], ',.0f')} distinct values sent per 1M rows).",
              f"* Throughput on this laptop (one core): {num(tp['rows_per_s_excluding_llm_wait'])} rows/s of local work, "
              f"{num(tp['rows_per_s_with_llm_wait'])} rows/s including the recorded LLM waits; median batch "
              f"{num(tp['median_batch_local_s'], '.2f')} s of local work.", ""]
    if s["background"]:
        b = s["background"]
        lines += ["## Real, unsanitised batches (nothing injected)", "",
                  f"Quarantined: {fmt(b['quarantined'])} of {b['n_batches']} batches. Batches with any alert: "
                  f"{_before_after(b.get('any_alert_before'), b.get('any_alert'))} (before -> after the alerting layer). "
                  "Real batches carry real issues, so these alerts are not all false.", "",
                  *evaluate.table_header(["problem", "alerts / batch (before -> after)", "flagged rows / 1,000"])]
        lines += [evaluate.table_row([p, f"{num((b.get('alerts_per_batch_before') or b['alerts_per_batch'])[p], '.2f')} -> "
                                         f"{num(b['alerts_per_batch'][p], '.2f')}", num(b["flagged_per_1000_rows"][p], ".2f")])
                  for p in b["alerts_per_batch"]]
    return "\n".join(lines) + "\n"


def _before_after(before: dict | None, after: dict | None) -> str:
    """'9/10 -> 1/10': batch counts before and after the alerting layer."""
    k_of_n = evaluate.fmt_k_of_n
    if not after:
        return "-"
    return k_of_n(after) if not before or before == after else f"{k_of_n(before)} -> {k_of_n(after)}"


def alert_calibration_table(a: dict) -> list[str]:
    cal = a.get("calibration")
    if not cal:
        return []
    lines = ["", "## Alerting layer: thresholds calibrated on January-August", "",
             f"{cal['rule']}. Detectors fitted on the {cal['fit_period']} ({cal['fit_rows']:,} rows); flags counted on the "
             f"{cal['calibration_period']} ({cal['calibration_rows']:,} rows, {cal['batch_size']:,}-row batches); LLM confirmation "
             f"{cal['llm_confirmation']}. Critical checks alerting on every flag: {', '.join(cal['critical'])}.", "",
             *evaluate.table_header(["check", "calibration batches", "flagged rows per batch: median / 95th pct / max",
                                     "batches alerting at 1 flag", f"allowed ({cal['target_batch_far']:.0%} of n)", "threshold (min flagged rows)",
                                     "batches alerting at the threshold"])]
    for p, c in cal["checks"].items():
        f = c["flagged_rows"]
        lines.append(evaluate.table_row([f"{p} - {PROBLEMS[p]['title']}", c["n_batches"], f"{f['median']} / {f['p95']} / {f['max']}",
                                         f"{c['batches_alerting_at_1']}/{c['n_batches']}", c["allowed_to_alert"], c["threshold"],
                                         f"{c['batches_alerting_at_threshold']}/{c['n_batches']}"]))
    return lines


def alert_sweep_table(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    lines = ["", "## What the thresholds cost: injected batches that alert, by injection rate", "",
             "Each thresholded problem injected at every rate into the benchmark's first corrupted batches. Cell = "
             "injected batches alerted before -> after the alerting layer (median injected rows per batch).", ""]
    rates = sorted({r["rate"] for r in rows})
    lines += evaluate.table_header(["check", "threshold", *[f"{rate:.1%}" for rate in rates]])
    for problem in dict.fromkeys(r["problem"] for r in rows):
        cells = {r["rate"]: r for r in rows if r["problem"] == problem}
        lines.append(evaluate.table_row([
            f"{problem} - {PROBLEMS[problem]['title']}", cells[rates[0]]["threshold"],
            *[(f"{_before_after(cells[rate]['alerted_before'], cells[rate]['alerted_after'])} "
               f"({evaluate.fmt_num(cells[rate]['injected_rows_median'], ',.0f')} rows)") if rate in cells else "-" for rate in rates]]))
    return lines


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(prog="python -m project_b.pipeline", description="Evaluate the hybrid pipeline end to end (Task 5).")
    ap.add_argument("--quick", action="store_true", help="2 batches per problem, 2 clean, 3 mixed, 2 real: every code path, fast")
    ap.add_argument("--out-dir", type=Path, help="default: project_b/results (project_b/results/partial with --quick)")
    ap.add_argument("--summary-dir", type=Path, help="where the detector benchmark's summary.json is read (default: same as --out-dir)")
    args = ap.parse_args(argv)
    out_dir = args.out_dir or (config.RESULTS_DIR / "partial" if args.quick else config.RESULTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    plan, source = load_plan(args.summary_dir or out_dir)
    evaluate.log(f"[pipeline] plan source: {source}")
    budget, bench = (QUICK, evaluate.QUICK) if args.quick else (PipelineBudget(), evaluate.Budget())
    return evaluate_pipeline(evaluate.ensure_derived(), out_dir, plan, source, budget, bench)


if __name__ == "__main__":
    main()
