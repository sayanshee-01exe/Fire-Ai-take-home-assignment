"""Evaluation harness: inject known corruptions into clean NYC 311 batches, run every
detector, and score detection, false alarms, cost, speed and explanations (DESIGN.md section 5).

Why it is built this way
* Ground truth by injection. Real 311 data has no labels, so batches are cut from a
  sanitised pool (rows with pre-existing issues removed by data/clean.py) and corrupted by
  inject.py, which records exactly which rows / columns it broke. False alarms are counted
  on clean batches, where every alert is wrong by construction.
* Like for like. Rule and statistical detectors see every row of every batch. The LLM (slow,
  rate-limited, paid per token) reviews a sample from the first config.LLM_N_BATCHES of the
  SAME batches, and the rule / stat verdicts are also scored on exactly the rows it reviewed.
  For duplicates the LLM sees windows of contiguous rows, so a copy counts only when its
  original is in the same window; window_reach reports how often that can happen at all.
* Records first, numbers second. Every detect() call becomes one line of
  results/eval_records.jsonl, written as it happens; summary.json and summary_tables.md are
  computed from those records alone, so every reported number traces back to its runs.
* Reproducible. Every random draw is seeded from (config.SEED, problem, batch, ...): a rerun
  gives identical records except timings, and LLM answers replay from llm.py's cache.
* Fail soft, never invent. A detector that crashes, or returns output the harness cannot score,
  is recorded with its error: on a corrupted batch that is a miss, on a clean batch it is left out
  of the false-alarm rates (an error is not a correct "no alarm"), but an alert it raised anyway
  counts like any other; an LLM that cannot be reached is reported as "not run".
* Abstain, never guess. A row the LLM was asked about but gave no readable verdict for (a reply cut
  off at the output limit, blocked by a filter, or skipping the row) is an abstention: the LLM's
  rates are conditional on a readable verdict, its coverage says how often it gave one, and the
  "operational" rates count every abstention as "no alert". Nothing it did not judge reads as clean.

Usage: python -m project_b.evaluate [--problems abcdefghijkl] [--families rule,stat,llm]
           [--quick] [--skip-sweep] [--skip-background] [--out-dir DIR]
           [--reuse-base-records EARLIER/eval_records.jsonl]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import pickle
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from project_b import config, llm, metrics
from project_b.core import FAMILIES, PROBLEMS, DetectionResult, Detector, jsonable, timed_detect
from project_b.data.load import load_raw, make_batches, to_feed
from project_b.detectors import all_detectors

# Derived data: rebuilt when the raw snapshot or the code that derives it changes.
DERIVED_DIR = config.DATA_DIR / "derived"
FEED_FILE = "feed.parquet"  # full year, typed, UNsanitised
POOL_FILE = "clean_pool.parquet"  # full year after clean.sanitize
COUNTS_FILE = "sanitize_counts.json"  # rows in / kept, rows flagged per pre-existing issue
CONTEXT_FILE = "injection_context.pkl"  # inject.InjectionContext built from the feed
KEY_FILE = "cache_key.json"
DERIVED_FILES = (FEED_FILE, POOL_FILE, COUNTS_FILE, CONTEXT_FILE)
# config.py is included because the derivation reads it (REFERENCE_END for the context's null
# rates, the NYC box and status groups for sanitising, ...): editing it must rebuild.
DERIVING_CODE = [config.PKG_DIR / "config.py", config.PKG_DIR / "data" / "load.py", config.PKG_DIR / "data" / "clean.py",
                 config.PKG_DIR / "inject.py"]
RECORDS_FILE, SUMMARY_FILE, TABLES_FILE = "eval_records.jsonl", "summary.json", "summary_tables.md"

PROBLEM_IDS = "".join(PROBLEMS)  # 'abcdefghijkl'; a problem's position in it keys its random draws
MAIN_KINDS = ("corrupted", "clean")  # the record kinds behind the headline metrics
WINDOW_PROBLEMS = "ef"  # duplicates need context: the LLM reviews runs of contiguous rows
LLM_WINDOWS_PER_BATCH = 3  # windows of config.LLM_BATCH_SAMPLE_ROWS rows per LLM batch (e, f)
WINDOW_ROWS_AFTER_TARGET = 5  # a window ends just after its injected copy: maximal look-back
SWEEP_N_BATCHES = 5  # batches per setting in the rate and null-spike sweeps
NULL_SPIKE_FRACTIONS = [0.1, 0.25, 0.5, 0.9]
MAX_ALERTS_RECORDED = 5
N_EXAMPLE_ALERTS = 3  # example alert messages per detector in the background table
MAX_NOTE_CHARS = 110  # status notes and example alerts are cut to this length in the tables
# Seed keys: every draw comes from np.random.default_rng([config.SEED, *keys]), so any batch
# can be regenerated on its own and adding a draw in one place never shifts another.
#   (problem, batch)               the injection itself (DESIGN section 5)
#   (problem, batch, LLM_SAMPLE)   the rows / windows the LLM reviews
#   (BATCH_CHOICE, problem)        which batches that problem corrupts
#   (CLEAN_SET,), (BACKGROUND,)    which evaluation batches stay clean / run unsanitised
# numpy pads a short seed with zeros, so (p,) and (p, 0) would be ONE stream: every key that is
# not a (problem, batch, ...) key starts with a tag >= 100, which no problem index reaches.
LLM_SAMPLE_KEY = 1
CLEAN_SET_KEY = 100
BACKGROUND_KEY = 101
BATCH_CHOICE_KEY = 102
USAGE_KEYS = ("llm_calls", "llm_cached_calls", "input_tokens", "output_tokens", "llm_latency_s")
RECORD_CONTEXT = {"rate": None, "sweep": None, "repeat": None, "same_rows": None}  # every record has every key


@dataclass(frozen=True)
class Budget:
    """How much work one run does. The defaults are the full experiment of DESIGN section 5."""

    n_corrupted: int = config.N_CORRUPTED_BATCHES  # per problem
    n_clean: int = config.N_CLEAN_BATCHES  # shared by all problems; also the background batch count
    llm_n_batches: int = config.LLM_N_BATCHES  # corrupted and, separately, clean batches the LLM sees
    llm_rows_injected: int = config.LLM_ROW_SAMPLE_INJECTED  # per problem, plus as many non-injected rows
    llm_rows_clean: int = config.LLM_ROW_SAMPLE_CLEAN  # per problem, from clean batches
    llm_windows_per_batch: int = LLM_WINDOWS_PER_BATCH
    llm_repeats: int = config.LLM_REPEATS
    sweep_n_batches: int = SWEEP_N_BATCHES
    batch_size: int = config.BATCH_SIZE


# --quick: every code path in a few minutes, for development.
QUICK = Budget(
    n_corrupted=3, n_clean=3, llm_n_batches=1, llm_rows_injected=5, llm_rows_clean=5,
    llm_windows_per_batch=1, llm_repeats=2, sweep_n_batches=2,
)


def log(message: str) -> None:
    """Progress line. Messages can quote data and a Windows console may be cp1252, so
    non-ASCII is escaped: a console encoding error must never stop a long run."""
    print(message.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def rng(*keys: int) -> np.random.Generator:
    return np.random.default_rng([config.SEED, *keys])


TIMING_KEYS = ("wall_s",)  # the only record fields a rerun may change (rule / stat: no LLM latency)


class BaseRecordMismatch(RuntimeError):
    """--reuse-base-records: this run's rule / stat record differs from the earlier one."""


class Recorder:
    """Keeps the records and appends each one to the JSONL file immediately, so a long run
    that dies halfway still leaves its records on disk. A value JSON cannot hold (a timedelta
    in an alert, say) is written as its text instead of stopping the run, and the record kept
    in memory is the one read back from that line: the summary sees exactly the file.

    `reuse` (an earlier eval_records.jsonl) keeps that run's rule / stat records, timings
    included, so re-scoring the LLM leaves every rule / stat number unchanged. Each rule / stat
    record of this run is first checked against the earlier one, in order: every field but
    TIMING_KEYS must be identical, otherwise the run stops (BaseRecordMismatch). Records of the
    detectors named in `rescore` (whose code changed) are this run's own: only their identity
    (detector, kind, batch, setting) must match the earlier record in that place."""

    def __init__(self, path: Path, reuse: Path | None = None, rescore: frozenset[str] = frozenset()):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._reuse = None
        if reuse is not None:  # read before the (possibly same) output file is emptied
            earlier = [json.loads(line) for line in reuse.read_text(encoding="utf-8").splitlines() if line.strip()]
            self._reuse = iter([r for r in earlier if r["family"] != "llm"])
        path.write_text("", encoding="utf-8")
        self.path = path
        self.records: list[dict] = []
        self.reused = 0
        self.rescore = frozenset(rescore)
        self.rescored = 0

    def add(self, record: dict) -> None:
        line = json.dumps(jsonable(record), ensure_ascii=False, default=str)
        kept = json.loads(line)
        if self._reuse is not None and kept["family"] != "llm" and kept["detector"] in self.rescore:
            earlier = next(self._reuse, None)
            if earlier is None or _identity(earlier) != _identity(kept):
                raise BaseRecordMismatch(f"re-scored record out of step with the earlier run ({kept['detector']} {kept['kind']} batch {kept['batch_id']})")
            self.rescored += 1
        elif self._reuse is not None and kept["family"] != "llm":
            earlier = next(self._reuse, None)
            if earlier is None or _without_timing(earlier) != _without_timing(kept):
                where = f"{kept['detector']} {kept['kind']} batch {kept['batch_id']}"
                raise BaseRecordMismatch(f"rule / stat record differs from the earlier run ({where}): not reusing it")
            kept, line = earlier, json.dumps(earlier, ensure_ascii=False)
            self.reused += 1
        self.records.append(kept)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def finish(self) -> None:
        """With `reuse`: every earlier rule / stat record must have been matched."""
        if self._reuse is not None and next(self._reuse, None) is not None:
            raise BaseRecordMismatch("the earlier run has rule / stat records this run did not produce")


def _without_timing(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in TIMING_KEYS}


def _identity(record: dict) -> tuple:
    """What places a record in a run: detector, batch kind and id, injection setting."""
    return tuple(json.dumps(record.get(k), sort_keys=True) for k in ("detector", "kind", "batch_id", "rate", "sweep", "repeat"))


# --------------------------------------------------------------------------
# Step 1: derived data (feed, clean pool, injection context), cached
# --------------------------------------------------------------------------
def ensure_derived(derived_dir: Path = DERIVED_DIR) -> Path:
    """Build the derived data unless it exists and was built from this raw snapshot by this code."""
    key = cache_key()
    key_file = derived_dir / KEY_FILE
    complete = all((derived_dir / name).exists() for name in (KEY_FILE, *DERIVED_FILES))
    if complete and json.loads(key_file.read_text()) == key:
        log(f"[data] using cached derived data in {derived_dir}")
        return derived_dir
    key_file.unlink(missing_ok=True)  # removed first and written last: an interrupted build stays stale
    build_derived(derived_dir)
    key_file.write_text(json.dumps(key, indent=2))
    return derived_dir


def cache_key() -> dict[str, str]:
    """The raw snapshot's checksum and a hash of the code that derives from it: the DERIVING_CODE
    files and build_derived itself (the rest of this module does not change the derived data).
    The snapshot is verified first (snapshot.verify), so derived data always come from the
    snapshot in dataset_manifest.json, even when they are served from the cache."""
    from project_b.data import snapshot

    code = b"".join(path.read_bytes() for path in DERIVING_CODE) + inspect.getsource(build_derived).encode("utf-8")
    raw_sha = snapshot.sha256(config.RAW_FILE) if snapshot.verify(config.RAW_FILE) else ""
    return {"raw_sha256": raw_sha, "code_sha256": hashlib.sha256(code).hexdigest()}


def build_derived(derived_dir: Path) -> None:
    """Full-year feed -> injection context (from the UNsanitised feed) and the clean pool."""
    from project_b import inject
    from project_b.data import clean

    derived_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    feed = to_feed(load_raw())  # the raw frame is not kept: only the typed feed stays in memory
    feed.to_parquet(derived_dir / FEED_FILE, index=False)
    log(f"[data] feed: {len(feed):,} rows ({time.perf_counter() - t0:.0f} s)")
    with open(derived_dir / CONTEXT_FILE, "wb") as fh:
        pickle.dump(inject.InjectionContext.build(feed), fh)
    pool, removed = clean.sanitize(feed)
    counts = {"rows_in": len(feed), "rows_kept": len(pool), "flagged_per_issue": removed}  # a row can have several issues
    del feed
    pool.to_parquet(derived_dir / POOL_FILE, index=False)
    (derived_dir / COUNTS_FILE).write_text(json.dumps(jsonable(counts), indent=2))
    log(f"[data] clean pool: {len(pool):,} rows kept; flagged per issue {jsonable(removed)} ({time.perf_counter() - t0:.0f} s)")


def read_period(path: Path, reference: bool) -> pd.DataFrame:
    """One side of the reference / evaluation split (same rule as load.split_reference_eval),
    read with a parquet row filter so the other side never enters memory. Row order is kept."""
    op = "<" if reference else ">="
    return pd.read_parquet(path, filters=[("created_date", op, config.REFERENCE_END)])


# --------------------------------------------------------------------------
# Steps 2-3: fit, choose batches, choose what the LLM reviews
# --------------------------------------------------------------------------
def fit_all(detectors: list[Detector], reference: pd.DataFrame) -> dict[str, dict]:
    """Fit every detector once on the sanitised reference period. A failed fit is recorded and
    that detector is left out of the run: its verdicts would mean nothing."""
    fits = {}
    for det in detectors:
        t0 = time.perf_counter()
        try:
            det.fit(reference.copy(deep=False))  # shallow copy: Copy-on-Write protects the next detector
            error = None
        except Exception as exc:  # noqa: BLE001 - one broken detector must not stop the run
            error = f"{type(exc).__name__}: {exc}"
        fit_s = time.perf_counter() - t0
        fits[det.name] = {"problem": det.problem, "family": det.family, "description": det.description, "fit_s": fit_s, "fit_error": error}
        log(f"[fit] {det.name:<40} {fit_s:7.1f} s" + (f"  FAILED: {error}" if error else ""))
    return fits


def keep_fit_timings(fits: dict[str, dict], earlier_summary: Path, rescore: frozenset[str] = frozenset()) -> None:
    """--reuse-base-records: also keep the earlier run's fit time of each rule / stat detector that
    fitted in both runs, so their summary entries stay identical too (fit_s is a timing, like wall_s)."""
    if not earlier_summary.exists():
        return
    earlier = {e["detector"]: e for e in json.loads(earlier_summary.read_text(encoding="utf-8"))["detectors"]}
    for name, fit in fits.items():
        before = earlier.get(name)
        if (fit["family"] != "llm" and name not in rescore and before and not fit["fit_error"]
                and not (before.get("note") or "").startswith("fit:")):
            fit["fit_s"] = before["fit_s"]


def choose_batches(n_batches: int, budget: Budget, problems: str) -> tuple[list[int], dict[str, list[int]]]:
    """One shared set of clean batches and, per problem, its own corrupted batches from the
    rest (never both clean and corrupted). Kept in random order, so "the first
    llm_n_batches" corrupted batches are a random subset too."""
    order = rng(CLEAN_SET_KEY).permutation(n_batches)
    clean, rest = order[: budget.n_clean].tolist(), order[budget.n_clean :]
    corrupted = {p: rng(BATCH_CHOICE_KEY, PROBLEM_IDS.index(p)).permutation(rest)[: budget.n_corrupted].tolist() for p in problems}
    return clean, corrupted


def spread(total: int, n_parts: int) -> list[int]:
    """`total` split as evenly as possible over n_parts (the first parts take the remainder)."""
    return [total // n_parts + (i < total % n_parts) for i in range(n_parts)]


def llm_review_rows(problem: str, labels: np.ndarray, n_rows: int, n_windows: int, generator,
                    copy_of: dict | None = None) -> np.ndarray | None:
    """Positions an LLM detector reviews in one batch: None for batch-level problems (one
    whole-batch verdict, the detector samples for itself), windows of contiguous rows for the
    duplicate problems (`copy_of`: injected copy -> its original, from the injector's params),
    otherwise a stratified row sample."""
    if PROBLEMS[problem]["level"] == "batch":
        return None
    if problem in WINDOW_PROBLEMS:
        return window_rows(labels, n_windows, config.LLM_BATCH_SAMPLE_ROWS, generator, copy_of)
    return stratified_rows(labels, n_rows, generator)


def stratified_rows(labels: np.ndarray, n_per_stratum: int, generator) -> np.ndarray:
    """Up to n injected and n non-injected rows, in position order. A clean batch has no
    injected rows, so there it is a plain random sample of n rows."""
    picks = []
    for stratum in (True, False):
        rows = np.flatnonzero(labels == stratum)
        picks.append(generator.choice(rows, size=min(n_per_stratum, len(rows)), replace=False))
    return np.sort(np.concatenate(picks))


def window_rows(labels: np.ndarray, n_windows: int, width: int, generator, copy_of: dict | None = None) -> np.ndarray:
    """Up to n_windows runs of `width` contiguous rows. Each run ends a few rows after an
    injected copy, because the model judges a row against the EARLIER rows of its run; in a
    clean batch the runs are anchored at random rows. Runs never touch, so each stays separate.
    Windows that also contain the copy's original (`copy_of`) are placed first: a copy shown
    without its original cannot be judged at all, and exact copies land anywhere later in the
    batch (most > 1,000 rows away), so random copies would score the placement, not the model
    (such copies are also left out of the scores: unjudgeable_copies)."""
    n = len(labels)
    anchors = generator.permutation(np.flatnonzero(labels) if labels.any() else np.arange(n))
    starts = np.clip(anchors + WINDOW_ROWS_AFTER_TARGET + 1 - width, 0, max(0, n - width))
    if copy_of:  # the window holds its anchor; it holds the original too iff that starts at or after `start`
        original = {int(copy): int(source) for copy, source in copy_of.items()}
        shows_original = np.array([original.get(int(a), -1) >= s for a, s in zip(anchors, starts)], dtype=bool)
        starts = starts[np.argsort(~shows_original, kind="stable")]
    taken = np.zeros(n, dtype=bool)
    placed = 0
    for start in starts.tolist():
        if placed == n_windows:
            break
        end = min(n, start + width)
        if taken[max(0, start - 1) : end + 1].any():
            continue
        taken[start:end] = True
        placed += 1
    return np.flatnonzero(taken)


# --------------------------------------------------------------------------
# Running one detector on one batch -> one record
# --------------------------------------------------------------------------
def run_detector(det: Detector, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
    """timed_detect on a shallow copy (under Copy-on-Write a detector that edits its input
    cannot change what the next one sees). Output the harness cannot score is a detector bug:
    a detect() that returns no DetectionResult, or per-row arrays that are not one boolean per
    row (wrong length, NaN or NA, scores). It is recorded as an error, with the counts of a
    detector that flagged nothing, instead of crashing the run or being guessed at."""
    try:
        result = timed_detect(det, batch.copy(deep=False), review_rows=review_rows)
    except AttributeError as exc:  # timed_detect sets result.usage after detect(): a None result fails there
        result = DetectionResult(problem=det.problem, family=det.family, detector=det.name, batch_flag=False,
                                 error=f"unusable detect() result: {exc}")
    arrays = {name: getattr(result, name) for name in ("row_flags", "reviewed") if getattr(result, name) is not None}
    masks = {name: row_mask(values, len(batch)) for name, values in arrays.items()}
    malformed = [name for name, mask in masks.items() if mask is None]
    if malformed:
        result.row_flags = result.reviewed = None
        result.error = result.error or f"malformed {'/'.join(malformed)}: expected {len(batch)} booleans"
    else:
        for name, mask in masks.items():
            setattr(result, name, mask)
    return result


def row_mask(values, n_rows: int) -> np.ndarray | None:
    """A detector's per-row array as bool[n_rows]; None unless it holds one True / False (or 1 / 0)
    per row. NaN, pd.NA or scores are not guessed at (cast to bool, NaN would read as True)."""
    try:
        array = np.asarray(values)
    except ValueError:  # e.g. a ragged list
        return None
    if array.shape != (n_rows,):
        return None
    if pd.api.types.infer_dtype(array, skipna=False) == "boolean":  # bool dtype, or Python bools and no NA
        return array.astype(bool)
    if array.dtype.kind in "iuf" and np.isin(array, (0, 1)).all():
        return array.astype(bool)
    return None


def rows_covered(record: dict) -> int:
    """Rows one verdict covers: the reviewed rows for row-level review, the whole batch for a
    batch-level verdict (however few rows the detector showed the model)."""
    return record["n_rows"] if PROBLEMS[record["problem"]]["level"] == "batch" else record["n_reviewed"]


def reviewed_mask(result: DetectionResult, review_rows: np.ndarray | None, n_rows: int) -> np.ndarray:
    """Rows a verdict is scored on: the rows the harness asked for (every row when it asked
    for none). Only an LLM detector that answered narrows this to the asked rows it gave a
    readable verdict for; the other asked rows are abstentions (abstention_fields), reported as
    coverage and in the operational rates, never as clean rows. Rule / stat detectors read every
    row, and a failure (any error but llm_unavailable, which is scored as "not run") is a miss on
    every row it was asked to judge, like a crash."""
    asked = np.ones(n_rows, dtype=bool) if review_rows is None else np.isin(np.arange(n_rows), review_rows)
    if result.family == "llm" and result.reviewed is not None and not result.error:
        return np.asarray(result.reviewed, dtype=bool) & asked
    return asked


def unjudgeable_copies(problem: str, review_rows: np.ndarray | None, truth: Any, n_rows: int) -> np.ndarray:
    """Window problems: the injected copies inside a window whose original is outside it.
    Nothing the reviewer was shown says they repeat anything, so they are left out of the
    window scores - for the LLM and for the rule / stat verdicts re-scored on its rows alike.
    How many copies one window can show with their original at all is window_reach."""
    unjudgeable = np.zeros(n_rows, dtype=bool)
    copy_of = (truth.params.get("copy_of") if truth is not None else None) or {}
    if problem not in WINDOW_PROBLEMS or review_rows is None or not copy_of:
        return unjudgeable
    rows = np.unique(np.asarray(review_rows, dtype=int))
    # Number the windows 1, 2, ... (0 = in no window): a new window starts wherever a position
    # does not follow the previous one; prepending -2 makes the first position start window 1.
    window_of = np.zeros(n_rows, dtype=int)
    window_of[rows] = np.cumsum(np.diff(rows, prepend=-2) != 1)
    for copy, original in copy_of.items():
        copy, original = int(copy), int(original)
        unjudgeable[copy] = window_of[copy] > 0 and window_of[original] != window_of[copy]
    return unjudgeable


def make_record(det: Detector, result: DetectionResult, kind: str, batch_id: int, batch: pd.DataFrame,
                truth: Any = None, review_rows: np.ndarray | None = None, **context) -> dict:
    """One JSON line: what `det` reported on one batch and, when the batch has ground truth
    (`truth`, an inject.Injected), how that compares with what was injected: confusion counts
    over the reviewed rows, less window copies nobody could judge. `context` sets
    rate / sweep / repeat / same_rows."""
    level = PROBLEMS[det.problem]["level"]
    flags = None if result.row_flags is None else np.asarray(result.row_flags, dtype=bool)
    reviewed = reviewed_mask(result, review_rows, len(batch))
    unjudgeable = unjudgeable_copies(det.problem, review_rows, truth, len(batch))
    scored = reviewed & ~unjudgeable
    flagged = np.flatnonzero(flags & reviewed) if flags is not None else np.array([], dtype=int)
    labels = None if truth is None else np.asarray(truth.row_labels, dtype=bool)
    counted = labels is not None and (level == "row" or flags is not None)
    # Grounding checks the rows the model itself flagged: every flagged row of a per-row review, but
    # only the rows a batch verdict quotes evidence for. A null-spike verdict's row flags are the
    # agency's nulls, filled in by the detector: there is no model evidence to check on them.
    quoted = flagged if level == "row" else np.array([p for p in flagged if int(p) in result.row_evidence], dtype=int)
    alerts = [a.to_dict() for a in result.alerts]
    localised = None
    if truth is not None and truth.batch_label and level == "batch" and result.batch_flag:
        localised = metrics.localisation_ok(alerts, truth.params, batch.columns)
    return {
        "problem": det.problem, "family": det.family, "detector": det.name, "batch_id": batch_id, "kind": kind,
        **RECORD_CONTEXT, **context,
        "params": None if truth is None else truth.params,
        "n_rows": len(batch),
        "n_reviewed": int(reviewed.sum()),
        "injected_reviewed": None if labels is None else int((labels & reviewed).sum()),
        **(metrics.confusion(flags, labels, scored) if counted else dict.fromkeys(metrics.COUNT_KEYS)),
        "n_flagged": None if flags is None else len(flagged),  # None: no row verdicts (batch-level, or crashed)
        "batch_label": None if truth is None else bool(truth.batch_label),
        "batch_flag": bool(result.batch_flag),
        "localisation_ok": localised,
        "wall_s": result.usage.wall_s,
        "usage": {key: getattr(result.usage, key) for key in USAGE_KEYS},
        "error": result.error,
        "n_alerts": len(alerts),
        "alerts": alerts[:MAX_ALERTS_RECORDED],
        "clarity": metrics.clarity_score(alerts[0]) if alerts else None,
        # LLM only: is the quoted evidence really in the row; which rows it flagged (determinism)
        "grounding": metrics.grounding_counts(batch, quoted, result.row_evidence) if det.family == "llm" else None,
        "flagged_positions": flagged.tolist() if det.family == "llm" else None,
        # per-row LLM review only (rule / stat records keep their exact earlier shape)
        **(abstention_fields(result, review_rows, reviewed, unjudgeable, labels) if det.family == "llm" and level == "row" else {}),
    }


ABSTENTION_KEYS = ("n_asked", "n_abstained", "abstained_injected", "abstained_non_injected", "abstain_causes", "abstained_positions")


def abstention_fields(result: DetectionResult, review_rows: np.ndarray | None, reviewed: np.ndarray,
                      unjudgeable: np.ndarray, labels: np.ndarray | None) -> dict:
    """Rows the LLM was asked to judge but gave no readable verdict for: abstentions. They are
    not reviewed (so not in the confusion counts, which are conditional on a readable verdict) and
    never counted as clean. abstained_injected / _non_injected count them over the rows the
    verdicts are scored on (less unjudgeable window copies), for the overall operational metrics
    where an abstention means "no alert". A detector error is not an abstention: it stays a miss
    on every row it was asked to judge (reviewed_mask)."""
    n_rows = len(reviewed)
    asked = np.ones(n_rows, dtype=bool) if review_rows is None else np.isin(np.arange(n_rows), review_rows)
    answered = result.error is None and result.reviewed is not None
    abstained = asked & ~reviewed if answered else np.zeros(n_rows, dtype=bool)
    countable = abstained & ~unjudgeable
    positions = np.flatnonzero(abstained)
    causes: dict[str, int] = {}
    for p in positions:
        cause = result.abstained.get(int(p), llm.ABSTAIN_NOT_SENT)
        causes[cause] = causes.get(cause, 0) + 1
    return {
        "n_asked": int(asked.sum()),
        "n_abstained": int(abstained.sum()),
        "abstained_injected": None if labels is None else int((countable & labels).sum()),
        "abstained_non_injected": None if labels is None else int((countable & ~labels).sum()),
        "abstain_causes": dict(sorted(causes.items())),
        "abstained_positions": positions.tolist(),
    }


# --------------------------------------------------------------------------
# Steps 4-7: the experiments
# --------------------------------------------------------------------------
@dataclass
class Experiment:
    """What every step of one run shares."""

    batches: list[pd.DataFrame]  # sanitised evaluation period, budget.batch_size rows each
    clean_ids: list[int]  # the shared clean batches
    corrupted_ids: dict[str, list[int]]  # per problem, in random order
    injector: Any  # project_b.inject (or a test fake): inject() and as_clean()
    context: Any  # inject.InjectionContext
    budget: Budget
    recorder: Recorder

    def corrupt(self, problem: str, batch_id: int, **settings):
        """Inject `problem` into one batch, seeded by (SEED, problem, batch) as DESIGN section 5 says."""
        settings = {"rate": config.ROW_INJECTION_RATE, **settings}
        generator = rng(PROBLEM_IDS.index(problem), batch_id)
        return self.injector.inject(self.batches[batch_id], problem, generator, self.context, **settings)


def run_problem(exp: Experiment, problem: str, dets: list[Detector]) -> None:
    """Rule + stat detectors on every corrupted and every clean batch, then each LLM detector
    on its budgeted sample of the same batches."""
    t0 = time.perf_counter()
    base = [d for d in dets if d.family != "llm"]
    corrupted = [(b, exp.corrupt(problem, b)) for b in exp.corrupted_ids[problem]]
    clean = [(b, exp.injector.as_clean(exp.batches[b])) for b in exp.clean_ids]
    base_flags: dict[tuple[str, int], dict[str, np.ndarray | None]] = {}  # (kind, batch) -> detector -> row flags
    for kind, items in (("corrupted", corrupted), ("clean", clean)):
        rate = config.ROW_INJECTION_RATE if kind == "corrupted" else None
        for b, inj in items:
            for det in base:
                result = run_detector(det, inj.df)
                exp.recorder.add(make_record(det, result, kind, b, inj.df, inj, rate=rate))
                base_flags.setdefault((kind, b), {})[det.name] = result.row_flags
    log(f"[{problem}] {PROBLEMS[problem]['title']}: {len(base)} rule/stat detectors x "
        f"({len(corrupted)} corrupted + {len(clean)} clean batches) in {time.perf_counter() - t0:.0f} s")
    n_llm = exp.budget.llm_n_batches
    for det in (d for d in dets if d.family == "llm"):
        run_llm(exp, det, corrupted[:n_llm], clean[:n_llm], base_flags)


def run_llm(exp: Experiment, det: Detector, corrupted: list, clean: list, base_flags: dict) -> None:
    """One LLM detector on its sample: per batch the rows or windows from llm_review_rows,
    with the rule / stat verdicts re-scored on exactly those rows; then the determinism
    repeats on the first corrupted batch."""
    budget, p = exp.budget, PROBLEM_IDS.index(det.problem)
    plan = [("corrupted", b, inj, n) for (b, inj), n in zip(corrupted, spread(budget.llm_rows_injected, len(corrupted)))]
    plan += [("clean", b, inj, n) for (b, inj), n in zip(clean, spread(budget.llm_rows_clean, len(clean)))]
    first_record = len(exp.recorder.records)
    reviews = []
    for kind, b, inj, n_rows in plan:
        review = llm_review_rows(det.problem, inj.row_labels, n_rows, budget.llm_windows_per_batch, rng(p, b, LLM_SAMPLE_KEY),
                                 copy_of=inj.params.get("copy_of"))
        result = run_detector(det, inj.df, review)
        same_rows = None
        if PROBLEMS[det.problem]["level"] == "row":  # rule / stat re-scored on exactly the rows the LLM's verdict is scored on
            scored = reviewed_mask(result, review, len(inj.df)) & ~unjudgeable_copies(det.problem, review, inj, len(inj.df))
            same_rows = {name: metrics.confusion(flags, inj.row_labels, scored) for name, flags in base_flags.get((kind, b), {}).items()}
        rate = config.ROW_INJECTION_RATE if kind == "corrupted" else None
        exp.recorder.add(make_record(det, result, kind, b, inj.df, inj, review, rate=rate, same_rows=same_rows))
        reviews.append(review)
    recs = exp.recorder.records[first_record:]
    log(f"[{det.problem}] {det.name}: {len(recs)} batches, {sum(rows_covered(r) for r in recs):,} rows covered"
        + (f" ({sum(r.get('n_abstained') or 0 for r in recs):,} abstained)" if any(r.get("n_abstained") for r in recs) else "") + ", "
        f"{sum(r['usage']['llm_calls'] for r in recs)} LLM calls ({sum(r['usage']['llm_cached_calls'] for r in recs)} cached), "
        f"{sum(bool(r['error']) for r in recs)} errors")
    if corrupted:
        b, inj = corrupted[0]
        run_determinism(exp, det, b, inj, reviews[0])


def run_determinism(exp: Experiment, det: Detector, batch_id: int, inj, review_rows: np.ndarray | None) -> None:
    """Re-run an LLM detector budget.llm_repeats times on one corrupted batch with the same
    rows. Repeat 0 replays the main run's answer; repeat i > 0 is a distinct, fresh call."""
    client = llm.get_client()
    try:
        for repeat in range(exp.budget.llm_repeats):
            client.default_repeat = repeat
            result = run_detector(det, inj.df, review_rows)
            exp.recorder.add(make_record(det, result, "determinism", batch_id, inj.df, inj, review_rows,
                                         rate=config.ROW_INJECTION_RATE, repeat=repeat))
    finally:
        client.default_repeat = 0


def run_sweeps(exp: Experiment, problem: str, dets: list[Detector]) -> None:
    """Rule + stat sensitivity: the injection-rate sweep (row-level problems) and the
    null-spike intensity sweep (g), on the problem's first corrupted batches, seeded exactly
    like the main run so only the setting changes."""
    base = [d for d in dets if d.family != "llm"]
    if PROBLEMS[problem]["level"] == "row":
        settings = [{"rate": rate} for rate in config.RATE_SWEEP]
    elif problem == "g":
        settings = [{"fraction": fraction} for fraction in NULL_SPIKE_FRACTIONS]
    else:
        return
    batch_ids = exp.corrupted_ids[problem][: exp.budget.sweep_n_batches]
    if not base or not batch_ids:
        return
    for setting in settings:
        for b in batch_ids:
            inj = exp.corrupt(problem, b, **setting)
            for det in base:
                exp.recorder.add(make_record(det, run_detector(det, inj.df), "sweep", b, inj.df, inj,
                                             rate=setting.get("rate", config.ROW_INJECTION_RATE), sweep=setting))
    log(f"[{problem}] sweep: {len(settings)} settings x {len(batch_ids)} batches x {len(base)} detectors")


def run_background(detectors: list[Detector], eval_raw: pd.DataFrame, budget: Budget, recorder: Recorder) -> None:
    """Rule + stat detectors on UNsanitised evaluation batches: what they report on real data,
    where nothing was injected (real issues and false alarms are not separated here)."""
    base = [d for d in detectors if d.family != "llm"]
    batches = make_batches(eval_raw, budget.batch_size)
    chosen = rng(BACKGROUND_KEY).permutation(len(batches))[: budget.n_clean].tolist()
    t0 = time.perf_counter()
    for b in chosen:
        for det in base:
            recorder.add(make_record(det, run_detector(det, batches[b]), "raw_background", b, batches[b]))
    log(f"[background] {len(base)} rule/stat detectors x {len(chosen)} unsanitised batches in {time.perf_counter() - t0:.0f} s")


def run_experiments(derived_dir: Path, detectors: list[Detector], budget: Budget, recorder: Recorder,
                    injector: Any = None, sweep: bool = True) -> int:
    """Every problem on its corrupted batches and on the shared clean batches, then the sweeps.
    The evaluation period is released on return. Returns the number of evaluation batches."""
    batches = make_batches(read_period(derived_dir / POOL_FILE, reference=False), budget.batch_size)
    problems = "".join(dict.fromkeys(d.problem for d in detectors))
    clean_ids, corrupted_ids = choose_batches(len(batches), budget, problems)
    with open(derived_dir / CONTEXT_FILE, "rb") as fh:
        context = pickle.load(fh)
    injector = injector or importlib.import_module("project_b.inject")
    exp = Experiment(batches, clean_ids, corrupted_ids, injector, context, budget, recorder)
    log(f"[batches] {len(batches)} evaluation batches of {budget.batch_size:,} rows; {len(clean_ids)} stay clean")
    for problem in problems:
        dets = [d for d in detectors if d.problem == problem]
        run_problem(exp, problem, dets)
        if sweep:
            run_sweeps(exp, problem, dets)
    return len(batches)


def run(derived_dir: Path, detectors: list[Detector], budget: Budget, out_dir: Path, *,
        injector: Any = None, sweep: bool = True, background: bool = True, reuse_base_records: Path | None = None,
        rescore: frozenset[str] = frozenset()) -> dict:
    """DESIGN section 5 steps 2-8 on the derived data in `derived_dir`; returns the summary.
    Each period is read when needed and released after, so memory holds one at a time."""
    fits = fit_all(detectors, read_period(derived_dir / POOL_FILE, reference=True))
    if reuse_base_records is not None:
        keep_fit_timings(fits, reuse_base_records.parent / SUMMARY_FILE, rescore)
    fitted = [d for d in detectors if not fits[d.name]["fit_error"]]
    recorder = Recorder(out_dir / RECORDS_FILE, reuse=reuse_base_records, rescore=rescore)
    n_batches = run_experiments(derived_dir, fitted, budget, recorder, injector, sweep)
    if background and any(d.family != "llm" for d in fitted):
        run_background(fitted, read_period(derived_dir / FEED_FILE, reference=False), budget, recorder)
    recorder.finish()
    if reuse_base_records is not None:
        log(f"[reuse] {recorder.reused} rule / stat records kept from {reuse_base_records} (identical but for {', '.join(TIMING_KEYS)})"
            + (f"; {recorder.rescored} re-scored for {', '.join(sorted(rescore))}" if rescore else ""))
    settings = {
        "seed": config.SEED,
        "injection_rate": config.ROW_INJECTION_RATE,
        "budget": asdict(budget),
        "n_eval_batches": n_batches,
        "llm_model": llm.get_client().model if any(d.family == "llm" for d in detectors) else None,
        "llm_prices_as_of": config.LLM_PRICES_AS_OF,
        "free_tier_requests_per_day": metrics.FREE_TIER_REQUESTS_PER_DAY,
        "sanitize": json.loads((derived_dir / COUNTS_FILE).read_text()),
    }
    return write_outputs(recorder.records, fits, settings, out_dir)


# --------------------------------------------------------------------------
# Step 8: summary.json - every number computed from the records alone
# --------------------------------------------------------------------------
def llm_unavailable(record: dict) -> bool:
    """The LLM could not be asked (no key, no cached answer, quota): a missing measurement, not a miss."""
    return (record["error"] or "").startswith("llm_unavailable")


def injected(recs: list[dict]) -> list[dict]:
    """Records of batches that really carry a corruption. An injector can come back empty-handed
    (e.g. no agency large enough for a null spike): such a batch has nothing to detect."""
    return [r for r in recs if r["batch_label"]]


def summarize(records: list[dict], fits: dict[str, dict], settings: dict) -> dict:
    """summary.json: one entry per detector, then the same-rows, sweep and background tables."""
    by_detector: dict[str, list[dict]] = {name: [] for name in fits}
    for r in records:
        by_detector[r["detector"]].append(r)
    same_rows = {}
    for problem in dict.fromkeys(fit["problem"] for fit in fits.values()):
        table = same_rows_summary(problem, records)
        if table:
            same_rows[problem] = table
    return {
        "settings": settings,
        "detectors": [detector_summary(name, fit, by_detector[name], settings["llm_model"]) for name, fit in fits.items()],
        "same_rows": same_rows,
        "rate_sweep": sweep_summary(records, "rate"),
        "null_spike_sweep": sweep_summary(records, "fraction"),
        "background": background_summary(records),
    }


def run_status(fit: dict, recs: list[dict]) -> dict:
    """ok | partial (some batches errored or got no LLM answer) | failed (fit failed, or every
    batch that ran errored) | not run (no LLM answer at all); the first error is the note."""
    if fit["fit_error"]:
        return {"status": "failed", "note": f"fit: {fit['fit_error']}", "n_errors": 0}
    main = [r for r in recs if r["kind"] in MAIN_KINDS]
    errors = [r["error"] for r in main if r["error"]]
    abstained, asked = sum(r.get("n_abstained") or 0 for r in main), sum(r.get("n_asked") or 0 for r in main)
    if all(llm_unavailable(r) for r in main):  # also true when nothing ran at all
        status = "not run"
    elif len(errors) == len(main):
        status = "failed"
    else:
        status = "partial" if errors or abstained else "ok"
    note = errors[0] if errors else (f"no readable verdict for {abstained:,} of {asked:,} rows asked (abstained)" if abstained else None)
    return {"status": status, "note": note, "n_errors": len(errors)}


def detector_summary(name: str, fit: dict, recs: list[dict], model: str | None) -> dict:
    """One detector: status and every metric with its n's and Wilson interval. A batch it errored on
    holds no verdict: a corrupted one still counts as a miss (conservative), a clean one is left out of
    the false-alarm rates - an error is not a correct "no alarm" - but an alert it raised anyway counts
    in the batch rates like any other. n_*_errors say how many there were. An LLM's rates are conditional
    on a readable verdict; its abstentions (rows or batches without one) are in "abstention", and
    "operational" holds the same rates with every abstention counted as "no alert"."""
    level = PROBLEMS[fit["problem"]]["level"]
    entry = {"problem": fit["problem"], "level": level, "family": fit["family"], "detector": name,
             "description": fit["description"], "fit_s": fit["fit_s"], **run_status(fit, recs)}
    ran = [r for r in recs if r["kind"] in MAIN_KINDS and not llm_unavailable(r)]
    if not ran:
        return entry
    corrupted = injected([r for r in ran if r["kind"] == "corrupted"])
    clean = [r for r in ran if r["kind"] == "clean"]
    if fit["family"] == "llm" and level == "batch":
        # an unreadable batch verdict is an abstention: out of the main (conditional) rates, in the operational ones
        entry["abstention"], entry["operational"] = batch_abstention(corrupted, clean)
        corrupted = [r for r in corrupted if not batch_abstained(r)]
    judged_clean = [r for r in clean if not r["error"]]  # the clean batches a false-alarm rate can count
    # an alert raised in spite of an error went out all the same: a false alarm (on a corrupted batch, a detection)
    alerted_or_judged = [r for r in clean if r["batch_flag"] or not r["error"]]
    entry["n_batches"] = {"corrupted": len(corrupted), "clean": len(clean)}
    entry["n_corrupted_errors"] = sum(bool(r["error"]) for r in corrupted)
    entry["n_clean_errors"] = len(clean) - len(judged_clean)
    batch_flags = metrics.batch_metrics([r["batch_flag"] for r in corrupted], [r["batch_flag"] for r in alerted_or_judged])
    if level == "batch":
        entry.update(batch_flags, detection=batch_flags["batch_detection"], localisation=localisation(corrupted))
    else:
        entry.update(metrics.row_metrics(metrics.sum_counts(corrupted), metrics.sum_counts(judged_clean), config.ROW_INJECTION_RATE))
        if fit["family"] != "llm":  # the LLM reads a small sample per batch: "any alert in the batch" would not compare
            entry.update(batch_flags)
        else:
            entry["abstention"], entry["operational"] = row_abstention(corrupted, judged_clean, recs)
            if fit["problem"] in WINDOW_PROBLEMS:
                entry["window_reach"] = window_reach(corrupted)
    entry.update(speed_and_cost(fit["family"], ran, model))
    if fit["family"] == "llm" and "operational" in entry:
        entry["operational"]["cost_per_million_rows"] = operational_cost(ran, model)
    scores = [r["clarity"] for r in ran if r["clarity"] is not None]
    entry["clarity"] = {"mean": float(np.mean(scores)) if scores else None, "n": len(scores)}
    if fit["family"] == "llm":
        grounding = [r["grounding"] for r in ran]
        entry["grounding"] = metrics.proportion(sum(g["grounded"] for g in grounding), sum(g["flagged"] for g in grounding))
        entry["determinism"] = determinism(level, recs)
    return entry


BATCH_ABSTAIN_ERRORS = ("llm_bad_reply", "llm_unparseable_reply")  # a batch verdict that could not be read


def batch_abstained(record: dict) -> bool:
    """A batch-level LLM record whose reply held no readable verdict."""
    return (record["error"] or "").startswith(BATCH_ABSTAIN_ERRORS)


def _abstention(asked: int, abstained: int, unit: str, causes: dict, **extra) -> dict:
    return {"unit": unit, "asked": asked, "with_verdict": asked - abstained,
            "coverage": metrics.proportion(asked - abstained, asked), "abstention_rate": metrics.proportion(abstained, asked),
            "causes": dict(sorted(causes.items())), **extra}


def row_abstention(corrupted: list[dict], clean: list[dict], all_recs: list[dict]) -> tuple[dict, dict]:
    """Per-row LLM review: coverage / abstention over the rows asked in the main batches, and the
    overall operational metrics, where an abstained row counts as "no alert" (a miss on an
    injected row, no false alarm on a clean one). The entry's own detection / row_fpr /
    precision are conditional on a readable verdict. Determinism repeats are counted apart."""
    recs = corrupted + clean
    causes: dict[str, int] = {}
    for r in recs:
        for cause, n in (r.get("abstain_causes") or {}).items():
            causes[cause] = causes.get(cause, 0) + n
    repeats = [r for r in all_recs if r["kind"] == "determinism" and (r.get("repeat") or 0) > 0 and not r["error"]]
    abstention = _abstention(
        sum(r.get("n_asked") or 0 for r in recs), sum(r.get("n_abstained") or 0 for r in recs), "rows", causes,
        abstained_injected=sum(r.get("abstained_injected") or 0 for r in recs),
        abstained_non_injected=sum(r.get("abstained_non_injected") or 0 for r in recs),
        # repeat 0 replays the main answer; repeats >= 1 are fresh calls, with their own abstentions
        determinism_repeats={"rows_asked": sum(r.get("n_asked") or 0 for r in repeats),
                             "abstained": sum(r.get("n_abstained") or 0 for r in repeats)},
    )

    def no_alert(r: dict) -> dict:  # an abstention as "no alert": injected -> fn, non-injected -> tn
        return {"tp": r["tp"], "fp": r["fp"], "fn": r["fn"] + (r.get("abstained_injected") or 0),
                "tn": r["tn"] + (r.get("abstained_non_injected") or 0)}

    operational = metrics.row_metrics(metrics.sum_counts([no_alert(r) for r in corrupted]),
                                      metrics.sum_counts([no_alert(r) for r in clean]), config.ROW_INJECTION_RATE)
    return abstention, operational


def batch_abstention(corrupted: list[dict], clean: list[dict]) -> tuple[dict, dict]:
    """Batch-level LLM review: coverage over the batches judged, and the operational batch rates
    (an unreadable verdict = no alert: a miss on a corrupted batch, no false alarm on a clean one)."""
    abstained = [r for r in corrupted + clean if batch_abstained(r)]
    causes: dict[str, int] = {}
    for r in abstained:
        cause = (r["error"] or "").split(":")[0]
        causes[cause] = causes.get(cause, 0) + 1
    abstention = _abstention(len(corrupted) + len(clean), len(abstained), "batches", causes)
    operational = metrics.batch_metrics([r["batch_flag"] for r in corrupted],
                                        [r["batch_flag"] for r in clean if r["batch_flag"] or not r["error"] or batch_abstained(r)])
    return abstention, operational


def operational_cost(ran: list[dict], model: str | None) -> dict:
    """LLM USD per 1M rows ASKED (row-level) or per 1M rows of the batches judged (batch-level):
    every token paid, over every row sent, whether or not a readable verdict came back."""
    ok = [r for r in ran if not r["error"] or batch_abstained(r)]
    rows = sum(r["n_asked"] if "n_asked" in r else rows_covered(r) for r in ok)
    return metrics.cost_per_million_rows(rows, input_tokens=sum(r["usage"]["input_tokens"] for r in ok),
                                         output_tokens=sum(r["usage"]["output_tokens"] for r in ok), model=model)


def localisation(recs: list[dict]) -> dict:
    """Share of alerted corrupted batches whose alerts name the right column / agency."""
    checked = [r["localisation_ok"] for r in recs if r["localisation_ok"] is not None]
    return metrics.proportion(sum(checked), len(checked))


def window_reach(recs: list[dict]) -> dict:
    """Share of injected copies whose original is fewer than LLM_BATCH_SAMPLE_ROWS rows earlier,
    i.e. that one window can show together with it: the most window review can ever catch."""
    gaps = [int(copy) - original for r in recs for copy, original in (r["params"].get("copy_of") or {}).items()]
    return metrics.proportion(sum(gap < config.LLM_BATCH_SAMPLE_ROWS for gap in gaps), len(gaps))


def speed_and_cost(family: str, ran: list[dict], model: str | None) -> dict:
    """Throughput and USD per 1M rows from the batches that ran without error. Rule / stat:
    detect() wall time over every row. LLM: API latency and tokens per covered row; its local
    CPU work is negligible and its wall time is mostly waiting, so it is not charged as compute."""
    ok = [r for r in ran if not r["error"]]
    if family != "llm":
        rows, seconds = [r["n_rows"] for r in ok], [r["wall_s"] for r in ok]
        return {"rows_per_s": metrics.rows_per_second(rows, seconds),
                "cost_per_million_rows": metrics.cost_per_million_rows(sum(rows), cpu_seconds=sum(seconds))}
    usage = {key: sum(r["usage"][key] for r in ok) for key in USAGE_KEYS}
    rows = sum(rows_covered(r) for r in ok)
    # a record is one detect() call; its mean latency per API call stands in for that call's latency
    latencies = [r["usage"]["llm_latency_s"] / r["usage"]["llm_calls"] for r in ok if r["usage"]["llm_calls"]]
    throughput = metrics.llm_throughput(rows, usage["llm_calls"], latencies)
    cost = metrics.cost_per_million_rows(rows, input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"], model=model)
    return {"rows_per_s": throughput["rows_per_s"], "llm_throughput": throughput, "llm_usage": usage, "cost_per_million_rows": cost}


def determinism(level: str, recs: list[dict]) -> dict | None:
    """Agreement across the repeated runs: per reviewed row (row-level) or on the one batch verdict."""
    repeats = [r for r in recs if r["kind"] == "determinism" and not r["error"]]
    if len(repeats) < 2:
        return None
    if level == "row":  # rows some repeat abstained on have no verdict to compare: left out of every repeat
        skip = set().union(*(set(r.get("abstained_positions") or []) for r in repeats))
        n_items = (repeats[0].get("n_asked") or repeats[0]["n_reviewed"]) - len(skip)
        return metrics.determinism_agreement([set(r["flagged_positions"]) - skip for r in repeats], n_items)
    return metrics.determinism_agreement([{0} if r["batch_flag"] else set() for r in repeats], 1)  # item 0 = the batch


def same_rows_summary(problem: str, records: list[dict]) -> list[dict]:
    """Rule, stat and LLM on identical inputs: the rows the LLM reviewed (row-level problems)
    or the batches it judged (batch-level problems). As in detector_summary, a clean batch a
    detector errored on is left out of its false-alarm rate, unless it raised an alert there anyway."""
    llm_recs = [r for r in records if r["problem"] == problem and r["family"] == "llm" and r["kind"] in MAIN_KINDS and not llm_unavailable(r)]
    if not llm_recs:
        return []
    if PROBLEMS[problem]["level"] == "row":
        errored = {(r["detector"], r["kind"], r["batch_id"]) for r in records if r["error"]}  # each detector's own runs
        pooled: dict[str, dict[str, list]] = {}
        for r in llm_recs:
            if r["kind"] == "corrupted" and not r["batch_label"]:  # nothing injected: left out, as in detector_summary
                continue
            own_counts = {key: r[key] for key in metrics.COUNT_KEYS}
            verdicts = {**(r["same_rows"] or {}), r["detector"]: own_counts}  # rule / stat first, then the LLM
            for name, counts in verdicts.items():
                pool = pooled.setdefault(name, {"corrupted": [], "clean": []})
                if r["kind"] == "corrupted" or (name, "clean", r["batch_id"]) not in errored:
                    pool[r["kind"]].append(counts)
        return [{"detector": name, **metrics.row_metrics(metrics.sum_counts(v["corrupted"]), metrics.sum_counts(v["clean"]), config.ROW_INJECTION_RATE)}
                for name, v in pooled.items()]
    judged = {(r["kind"], r["batch_id"]) for r in llm_recs}
    rows = []
    for name in dict.fromkeys(r["detector"] for r in records if r["problem"] == problem):
        same = [r for r in records if r["detector"] == name and (r["kind"], r["batch_id"]) in judged and not llm_unavailable(r)]
        corrupted = [r["batch_flag"] for r in injected(same) if r["kind"] == "corrupted"]
        clean = [r["batch_flag"] for r in same if r["kind"] == "clean" and (r["batch_flag"] or not r["error"])]
        rows.append({"detector": name, **metrics.batch_metrics(corrupted, clean)})
    return rows


def sweep_summary(records: list[dict], setting: str) -> list[dict]:
    """Per (detector, setting value): detection, plus precision (rate sweep, row-level) or
    localisation (null-spike sweep, batch-level)."""
    groups: dict[tuple, list[dict]] = {}
    for r in injected(records):
        if r["kind"] == "sweep" and setting in r["sweep"]:
            groups.setdefault((r["problem"], r["detector"], r["sweep"][setting]), []).append(r)
    out = []
    for (problem, detector, value), recs in groups.items():
        entry = {"problem": problem, "detector": detector, setting: value, "n_batches": len(recs)}
        if PROBLEMS[problem]["level"] == "row":
            c = metrics.sum_counts(recs)
            entry["detection"] = metrics.proportion(c["tp"], c["tp"] + c["fn"])
            entry["precision"] = metrics.proportion(c["tp"], c["tp"] + c["fp"])
        else:
            entry["detection"] = metrics.proportion(sum(r["batch_flag"] for r in recs), len(recs))
            entry["localisation"] = localisation(recs)
        out.append(entry)
    return out


def background_summary(records: list[dict]) -> list[dict]:
    """Per rule / stat detector on real, unsanitised batches: how often it alerts, how many
    rows it flags, and a few example alerts."""
    groups: dict[str, list[dict]] = {}
    for r in records:
        if r["kind"] == "raw_background":
            groups.setdefault(r["detector"], []).append(r)
    out = []
    for detector, recs in groups.items():
        ok = [r for r in recs if not r["error"]]
        with_rows = [r for r in ok if r["n_flagged"] is not None]  # batch-level detectors flag no rows
        rows = sum(r["n_rows"] for r in with_rows)
        out.append({
            "problem": recs[0]["problem"], "family": recs[0]["family"], "detector": detector,
            "n_batches": len(recs), "n_errors": len(recs) - len(ok),
            "batches_alerted": metrics.proportion(sum(r["batch_flag"] for r in ok), len(ok)),
            "alerts_per_batch": float(np.mean([r["n_alerts"] for r in ok])) if ok else None,
            "flagged_per_1000_rows": 1000 * sum(r["n_flagged"] for r in with_rows) / rows if rows else None,
            "examples": list(dict.fromkeys(a["message"] for r in ok for a in r["alerts"]))[:N_EXAMPLE_ALERTS],
        })
    return out


def write_outputs(records: list[dict], fits: dict[str, dict], settings: dict, out_dir: Path) -> dict:
    summary = jsonable(summarize(records, fits, settings))
    (out_dir / SUMMARY_FILE).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / TABLES_FILE).write_text(render_tables(summary), encoding="utf-8")
    log(f"[done] {len(records)} records, summary and tables written to {out_dir}")
    return summary


# --------------------------------------------------------------------------
# summary_tables.md
# --------------------------------------------------------------------------
COMPARISON_COLUMNS = ["detector", "status", "detection [95% CI]", "row FPR", "batch FAR", "precision @ injection rate",
                      "localisation", "rows/s", "$ per 1M rows", "clarity 0-5", "determinism"]


def render_tables(summary: dict) -> str:
    s = summary["settings"]
    lines = [
        "# Project B - detector comparison", "",
        f"Computed from `{RECORDS_FILE}` by `python -m project_b.evaluate`. Seed {s['seed']}, "
        f"{s['budget']['batch_size']:,}-row batches ({s['budget']['n_corrupted']} corrupted per problem, "
        f"{s['budget']['n_clean']} clean), row injection rate {s['injection_rate']:.0%}, "
        f"LLM model {s['llm_model'] or '-'}. Rates show the Wilson 95 % interval and (k/n). "
        "On row-level problems the LLM reads only a sample of each batch, so its batch FAR "
        "(any alert in a batch) would not compare with detectors that read every row and is left blank. "
        "LLM rates in the detector tables are conditional on a readable verdict: rows (or batches) the model "
        "gave no readable verdict for are abstentions, counted in the coverage table and in the overall "
        "operational rates, never as clean.", "",
    ]
    entries = summary["detectors"]
    for problem in dict.fromkeys(e["problem"] for e in entries):
        lines += problem_table(problem, [e for e in entries if e["problem"] == problem])
        lines += same_rows_table(problem, summary["same_rows"].get(problem, []))
    lines += coverage_table(entries)
    lines += rate_sweep_table(summary["rate_sweep"]) + null_spike_table(summary["null_spike_sweep"])
    lines += background_table(summary["background"])
    return "\n".join(lines) + "\n"


def problem_table(problem: str, entries: list[dict]) -> list[str]:
    info = PROBLEMS[problem]
    lines = [f"## {problem} - {info['title']} ({info['level']}-level)", "", *table_header(COMPARISON_COLUMNS)]
    for e in entries:
        status = e["status"] if not e["note"] else shorten(f"{e['status']}: {e['note']}")
        lines.append(table_row([e["detector"], status, *comparison_cells(e)]))
    lines.append("")
    for e in entries:
        if e.get("n_corrupted_errors") or e.get("n_clean_errors"):
            lines += [f"Errors: `{e['detector']}` errored on {e['n_corrupted_errors']} of {e['n_batches']['corrupted']} corrupted "
                      f"batches (scored as misses) and {e['n_clean_errors']} of {e['n_batches']['clean']} clean batches (left out "
                      "of its false-alarm rates: an error is no verdict, not a correct \"no alarm\"). An alert raised in "
                      "spite of an error still counts in the batch rates.", ""]
    for e in entries:
        lines += abstention_lines(e)
    for e in entries:
        if (e.get("window_reach") or {}).get("value") is not None:
            lines += [f"Window reach: a {config.LLM_BATCH_SAMPLE_ROWS}-row window shows an injected copy next to its original "
                      f"for {fmt_rate(e['window_reach'])} of copies. `{e['detector']}` (and the rule / stat rows it is "
                      "compared on) is scored only on copies shown with their original, so this share caps what "
                      "window review can catch in a full batch.", ""]
    return lines


def _causes_text(causes: dict) -> str:
    return ", ".join(f"{cause} {n:,}" for cause, n in causes.items()) or "none"


def abstention_lines(e: dict) -> list[str]:
    """One paragraph per LLM detector that abstained: coverage, causes, overall vs conditional rates."""
    a, op = e.get("abstention"), e.get("operational")
    if not a or not a["asked"] or not (a["asked"] - a["with_verdict"]):
        return []
    if e["level"] == "row":
        views = (f"Overall (an abstention counts as \"no alert\"): detection {fmt_rate(op['detection'])}, row FPR "
                 f"{fmt_rate(op['row_fpr'], 2)}, precision {fmt_num(op['precision_at_rate'], '.2f')}, "
                 f"{fmt_usd(op['cost_per_million_rows'])} per 1M rows asked. Given a readable verdict (table above): "
                 f"detection {fmt_rate(e['detection'])}, row FPR {fmt_rate(e['row_fpr'], 2)}.")
    else:
        views = (f"Overall: batch detection {fmt_rate(op['batch_detection'])}, batch FAR {fmt_rate(op['batch_far'])}; "
                 f"given a readable verdict: {fmt_rate(e['batch_detection'])}, {fmt_rate(e['batch_far'])}.")
    return [f"Abstentions: `{e['detector']}` gave a readable verdict for {a['with_verdict']:,} of {a['asked']:,} {a['unit']} "
            f"asked (coverage {fmt_pct(a['coverage'])}); it abstained on {a['asked'] - a['with_verdict']:,} "
            f"({_causes_text(a['causes'])}). " + views, ""]


COVERAGE_COLUMNS = ["problem", "LLM detector", "asked", "readable verdict", "coverage", "abstention rate", "causes",
                    "detection: overall / given a verdict", "false alarms: overall / given a verdict", "determinism (items compared)"]


def coverage_table(entries: list[dict]) -> list[str]:
    """Every LLM detector: how often it gave a readable verdict, and both views of its rates."""
    rows = [e for e in entries if e.get("abstention")]
    if not rows:
        return []
    lines = ["## LLM coverage and abstention", "",
             "Asked = rows sent for per-row review (or batches judged, for whole-batch checks) in the main corrupted "
             "and clean batches. An abstention is a row or batch the model gave no readable verdict for. Overall "
             "rates count an abstention as \"no alert\" (a miss on an injected row, no false alarm on a clean one); "
             "\"given a verdict\" rates leave abstentions out. False alarms = row FPR (row-level) or batch FAR.", "",
             *table_header(COVERAGE_COLUMNS)]
    for e in rows:
        a, op = e["abstention"], e["operational"]
        fa = ("row_fpr", 2) if e["level"] == "row" else ("batch_far", 1)
        lines.append(table_row([
            e["problem"], e["detector"], f"{a['asked']:,} {a['unit']}", f"{a['with_verdict']:,}", fmt_pct(a["coverage"]),
            fmt_rate(a["abstention_rate"], 2), _causes_text(a["causes"]),
            f"{fmt_pct(op['batch_detection' if e['level'] == 'batch' else 'detection'])} / {fmt_pct(e['detection'])}",
            f"{fmt_rate(op[fa[0]], fa[1])} / {fmt_rate(e[fa[0]], fa[1])}",
            "-" if not (e.get("determinism") or {}).get("n") else f"{fmt_pct(e['determinism'])} ({e['determinism']['k']}/{e['determinism']['n']})",
        ]))
    repeats = [(e["detector"], e["abstention"]["determinism_repeats"]) for e in rows if (e["abstention"].get("determinism_repeats") or {}).get("abstained")]
    if repeats:
        lines += ["", "Determinism repeats (fresh calls, not in the rates above): "
                  + "; ".join(f"`{name}` abstained on {d['abstained']:,} of {d['rows_asked']:,} rows asked" for name, d in repeats)
                  + "."]
    lines += ["", "Determinism compares only the rows (or the batch verdict) with a readable verdict in every repeat: an "
              "abstention in any repeat, including the main answer that repeat 0 replays, removes that row from the comparison."]
    return lines + [""]


def comparison_cells(e: dict) -> list[str]:
    if "n_batches" not in e:  # never produced a verdict: no numbers to show
        return ["-"] * (len(COMPARISON_COLUMNS) - 2)
    speed = fmt_num(e["rows_per_s"])
    free_tier = (e.get("llm_throughput") or {}).get("free_tier_rows_per_day")
    if free_tier is not None:
        speed += f" (free tier {fmt_num(free_tier)} rows/day)"
    clarity = fmt_num(e["clarity"]["mean"], ".1f")
    if (e.get("grounding") or {}).get("value") is not None:
        clarity += f" (grounded {fmt_pct(e['grounding'])})"
    return [fmt_rate(e["detection"]), fmt_rate(e.get("row_fpr"), 2), fmt_rate(e.get("batch_far")),
            fmt_num(e.get("precision_at_rate"), ".2f"), fmt_rate(e.get("localisation")), speed,
            fmt_usd(e["cost_per_million_rows"]), clarity, fmt_rate(e.get("determinism"))]


def same_rows_table(problem: str, rows: list[dict]) -> list[str]:
    if not rows:
        return []
    if PROBLEMS[problem]["level"] == "row":
        header = ["detector", "detection on the LLM's injected rows", "FPR on its clean-batch rows", "precision @ injection rate"]
        body = [[r["detector"], fmt_rate(r["detection"]), fmt_rate(r["row_fpr"], 2), fmt_num(r["precision_at_rate"], ".2f")] for r in rows]
    else:
        header = ["detector", "detection on the LLM's corrupted batches", "FAR on its clean batches"]
        body = [[r["detector"], fmt_rate(r["batch_detection"]), fmt_rate(r["batch_far"])] for r in rows]
    return [f"Same inputs as the LLM ({problem}):", "", *table_header(header), *map(table_row, body), ""]


def rate_sweep_table(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    rates = sorted({r["rate"] for r in rows})
    cells: dict[tuple[str, str], dict] = {}
    for r in rows:
        cells.setdefault((r["problem"], r["detector"]), {})[r["rate"]] = f"{fmt_pct(r['detection'])} / {fmt_pct(r['precision'])}"
    lines = ["## Injection-rate sweep (rule + stat)", "", f"Cell = detection / precision over {rows[0]['n_batches']} batches per rate.", "",
             *table_header(["problem", "detector", *[f"{rate:.1%}" for rate in rates]])]
    lines += [table_row([problem, detector, *[by_rate.get(rate, "-") for rate in rates]]) for (problem, detector), by_rate in cells.items()]
    return lines + [""]


def null_spike_table(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    fractions = sorted({r["fraction"] for r in rows})
    cells: dict[str, dict] = {}
    for r in rows:
        cells.setdefault(r["detector"], {})[r["fraction"]] = f"{fmt_k_of_n(r['detection'])} detected, {fmt_k_of_n(r['localisation'])} localised"
    lines = ["## Null-spike intensity sweep (g, rule + stat)", "", "Cell = corrupted batches detected, and of those correctly localised.", "",
             *table_header(["detector", *[f"{fraction:.0%} nulled" for fraction in fractions]])]
    lines += [table_row([detector, *[by_fraction.get(f, "-") for f in fractions]]) for detector, by_fraction in cells.items()]
    return lines + [""]


def background_table(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    lines = ["## Background: rule + stat on real, unsanitised evaluation batches", "",
             "Nothing was injected here: alerts are real pre-existing issues or false alarms.", "",
             *table_header(["problem", "detector", "batches alerted", "alerts / batch", "flagged rows / 1,000", "errors", "example alerts"])]
    for r in rows:
        examples = "<br>".join(shorten(message) for message in r["examples"]) or "-"
        lines.append(table_row([r["problem"], r["detector"], fmt_rate(r["batches_alerted"]), fmt_num(r["alerts_per_batch"], ".1f"),
                                fmt_num(r["flagged_per_1000_rows"], ".2f"), f"{r['n_errors']}/{r['n_batches']}", examples]))
    return lines + [""]


def table_header(columns: list[str]) -> list[str]:
    return [table_row(columns), "|" + "---|" * len(columns)]


def table_row(cells: list) -> str:
    """One markdown table row: each cell on one line, pipes escaped."""
    return "| " + " | ".join(" ".join(str(c).split()).replace("|", "\\|") for c in cells) + " |"


def shorten(text: str) -> str:
    return text if len(text) <= MAX_NOTE_CHARS else text[: MAX_NOTE_CHARS - 3] + "..."


def fmt_rate(p: dict | None, decimals: int = 1) -> str:
    """'93.0% [88.1-96.0] (93/100)'; '-' when nothing was measured."""
    if not p or p["value"] is None:
        return "-"
    lo, hi = (100 * x for x in p["ci"])
    return f"{100 * p['value']:.{decimals}f}% [{lo:.{decimals}f}-{hi:.{decimals}f}] ({p['k']}/{p['n']})"


def fmt_pct(p: dict | None) -> str:
    return "-" if not p or p["value"] is None else f"{100 * p['value']:.0f}%"


def fmt_k_of_n(p: dict | None) -> str:
    return "-" if not p or not p["n"] else f"{p['k']}/{p['n']}"


def fmt_num(x: float | None, spec: str = ",.0f") -> str:
    return "-" if x is None else format(x, spec)


def fmt_usd(cost: dict) -> str:
    return f"n/a ({cost['note']})" if cost["usd"] is None else f"${cost['usd']:.3g}"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m project_b.evaluate", description="Score every detector against injected corruptions (DESIGN.md section 5).")
    ap.add_argument("--problems", default=PROBLEM_IDS, help="problem letters to run, e.g. 'adg' (default: all)")
    ap.add_argument("--families", default=",".join(FAMILIES), help="comma-separated subset of rule,stat,llm")
    ap.add_argument("--quick", action="store_true", help="3 corrupted + 3 clean batches per problem, tiny LLM samples")
    ap.add_argument("--skip-sweep", action="store_true", help="skip the injection-rate and null-spike sweeps")
    ap.add_argument("--skip-background", action="store_true", help="skip the run on real, unsanitised batches")
    ap.add_argument("--out-dir", type=Path, help="where records, summary and tables are written (default: project_b/results "
                    "for the full experiment, project_b/results/partial for any reduced run, so it never overwrites them)")
    ap.add_argument("--reuse-base-records", type=Path, help="an earlier eval_records.jsonl whose rule / stat records (timings "
                    "included) are kept after checking that this run reproduces every other field; any difference stops the run. "
                    "Used to re-score the LLM from its replay cache without re-measuring rule / stat speed")
    ap.add_argument("--rescore-detectors", default="", help="with --reuse-base-records: comma-separated rule / stat detectors "
                    "whose code changed; their records are this run's own, every other one must match the earlier run")
    args = ap.parse_args(argv)
    args.rescore_detectors = frozenset(n.strip() for n in args.rescore_detectors.split(",") if n.strip())
    if args.rescore_detectors and not args.reuse_base_records:
        ap.error("--rescore-detectors needs --reuse-base-records")
    args.families = tuple(f.strip() for f in args.families.split(",") if f.strip())
    if not args.problems or not set(args.problems) <= set(PROBLEM_IDS):
        ap.error(f"--problems takes letters from {PROBLEM_IDS}")
    if not args.families or not set(args.families) <= set(FAMILIES):
        ap.error(f"--families takes a comma-separated subset of {','.join(FAMILIES)}")
    full = set(args.problems) == set(PROBLEM_IDS) and set(args.families) == set(FAMILIES)
    reduced = args.quick or args.skip_sweep or args.skip_background or not full
    args.out_dir = args.out_dir or (config.RESULTS_DIR / "partial" if reduced else config.RESULTS_DIR)
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    t0 = time.perf_counter()
    detectors = all_detectors(args.families, args.problems)
    budget = QUICK if args.quick else Budget()
    run(ensure_derived(), detectors, budget, args.out_dir, sweep=not args.skip_sweep, background=not args.skip_background,
        reuse_base_records=args.reuse_base_records, rescore=args.rescore_detectors)
    log(f"[done] total {time.perf_counter() - t0:.0f} s")


if __name__ == "__main__":
    main()
