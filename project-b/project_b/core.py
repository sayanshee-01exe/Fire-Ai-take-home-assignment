"""Shared contracts: problem catalogue, Alert / DetectionResult, Detector base class.

Every detector - rule, statistical or LLM - implements the same two methods:

    fit(reference_df)   learn baselines from the reference period (no-op for rules)
    detect(batch_df)    return a DetectionResult for one incoming batch

The harness (evaluate.py) only talks to detectors through this interface, so a
detector can be swapped, re-thresholded or deleted without touching anything else.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Problem catalogue (the 12 problem types from the brief)
# --------------------------------------------------------------------------
# level = "batch": the corruption is a property of the whole batch (schema, format,
#                  distribution). Detection is scored per batch.
# level = "row":   individual rows are corrupted. Detection is scored per row.
PROBLEMS: dict[str, dict[str, str]] = {
    "a": {"name": "renamed_or_dropped_column", "level": "batch", "title": "Renamed or dropped column"},
    "b": {"name": "column_type_change", "level": "batch", "title": "Column type change"},
    "c": {"name": "date_format_switch", "level": "batch", "title": "Date format switch partway through a batch"},
    "d": {"name": "lat_lon_swapped", "level": "row", "title": "Latitude and longitude swapped"},
    "e": {"name": "exact_duplicates", "level": "row", "title": "Exact duplicates"},
    "f": {"name": "near_duplicates", "level": "row", "title": "Near-duplicates"},
    "g": {"name": "null_spike_one_agency", "level": "batch", "title": "Null spike in one agency's records"},
    "h": {"name": "closed_before_created", "level": "row", "title": "Closed date earlier than created date"},
    "i": {"name": "zip_borough_mismatch", "level": "row", "title": "ZIP code inconsistent with borough"},
    "j": {"name": "complaint_descriptor_mismatch", "level": "row", "title": "Complaint Type inconsistent with Descriptor"},
    "k": {"name": "resolution_status_contradiction", "level": "row", "title": "Resolution Description contradicting Status"},
    "l": {"name": "pii_in_free_text", "level": "row", "title": "Personal data inside free-text fields"},
}
FAMILIES = ("rule", "stat", "llm")


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------
@dataclass
class Alert:
    """One human-facing alert. Row-level detectors emit ONE aggregated alert per
    batch (count + examples) and put per-row detail in DetectionResult.row_reasons;
    that is what an on-call person would actually receive."""

    problem: str  # 'a'..'l'
    family: str  # 'rule' | 'stat' | 'llm'
    detector: str  # detector name, e.g. 'rule_schema_contract'
    message: str  # one-line human explanation
    check_id: str | None = None  # stable id of the check / rule clause, e.g. 'R-h1'
    columns: list[str] = field(default_factory=list)  # columns involved
    rows: list[int] = field(default_factory=list)  # example row positions (<= 20)
    n_rows: int = 0  # how many rows the alert covers (0 for pure batch alerts)
    segment: dict[str, Any] = field(default_factory=dict)  # e.g. {"agency": "NYPD"}
    observed: Any = None  # observed value / statistic
    expected: Any = None  # expected value / range / baseline
    score: float | None = None  # anomaly score, p-value, similarity or LLM confidence
    deterministic: bool = True  # same input -> same alert? (False for LLM review)
    severity: str = "error"  # 'error' (block / quarantine) | 'warning' (review)

    def to_dict(self) -> dict:
        return _jsonable(asdict(self))


@dataclass
class Usage:
    """Resource usage of one detect() call. The harness fills wall_s; LLM
    detectors add their token / call counts (from the API's usage metadata)."""

    wall_s: float = 0.0
    llm_calls: int = 0
    llm_cached_calls: int = 0  # served from the replay cache (latency not re-measured)
    input_tokens: int = 0
    output_tokens: int = 0  # includes thinking tokens (billed as output)
    llm_latency_s: float = 0.0  # summed API latency of live calls

    def add(self, other: "Usage") -> None:
        for k in ("wall_s", "llm_calls", "llm_cached_calls", "input_tokens", "output_tokens", "llm_latency_s"):
            setattr(self, k, getattr(self, k) + getattr(other, k))


@dataclass
class DetectionResult:
    problem: str
    family: str
    detector: str
    batch_flag: bool  # did this detector raise anything for this problem on this batch?
    alerts: list[Alert] = field(default_factory=list)
    # Row-level output (None for detectors that only judge the whole batch).
    row_flags: np.ndarray | None = None  # bool[len(batch)]
    row_scores: np.ndarray | None = None  # float[len(batch)], higher = more suspicious (optional)
    row_reasons: dict[int, str] = field(default_factory=dict)  # row position -> short reason
    # Optional structured evidence per flagged row: {"observed": ..., "expected": ..., "confidence": ...}.
    # LLM detectors copy the model's own fields here so the harness can check that the
    # explanation is grounded (the quoted value really is in the row).
    row_evidence: dict[int, dict] = field(default_factory=dict)
    # Which rows were actually examined. None = all rows. LLM detectors that review a
    # sample set this so recall / false-alarm rates are computed only over reviewed rows.
    # For a per-row LLM review "reviewed" means "got a readable verdict": a row the model was
    # asked about but gave no readable verdict for is NOT reviewed - it is listed in `abstained`.
    reviewed: np.ndarray | None = None  # bool[len(batch)]
    # Per-row LLM review only: row position -> why the model gave no readable verdict for a row it
    # was asked to judge (llm.ABSTAIN_* causes). An abstention is never read as "clean".
    abstained: dict[int, str] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None  # detector crashed / could not run (e.g. LLM unavailable)


# --------------------------------------------------------------------------
# Detector base class
# --------------------------------------------------------------------------
class Detector:
    problem: ClassVar[str] = ""  # 'a'..'l'
    family: ClassVar[str] = ""  # 'rule' | 'stat' | 'llm'
    name: ClassVar[str] = ""  # unique, e.g. 'stat_null_spike_binomial'
    description: ClassVar[str] = ""  # one sentence: what it checks and how

    def fit(self, reference: pd.DataFrame) -> "Detector":
        """Learn from reference-period data (feed dtypes). Rules usually do nothing."""
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        """Inspect one batch (feed dtypes, may be corrupted in any way - never assume
        columns exist or have the contracted dtype; degrade gracefully instead of
        raising). `review_rows` (int positions) is only used by per-row LLM
        detectors: review just those rows and mark them in `reviewed`."""
        raise NotImplementedError

    # convenience for subclasses
    def result(self, batch_flag: bool, **kw) -> DetectionResult:
        return DetectionResult(problem=self.problem, family=self.family, detector=self.name, batch_flag=bool(batch_flag), **kw)

    def alert(self, message: str, **kw) -> Alert:
        kw.setdefault("deterministic", self.family != "llm")
        return Alert(problem=self.problem, family=self.family, detector=self.name, message=message, **kw)


def timed_detect(det: Detector, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
    """Run detect() with wall-clock timing and crash isolation (a crashing detector
    is recorded as an error result, never takes the whole run down)."""
    t0 = time.perf_counter()
    try:
        res = det.detect(batch, review_rows=review_rows)
    except Exception as exc:  # noqa: BLE001 - we want to record any failure
        res = DetectionResult(problem=det.problem, family=det.family, detector=det.name, batch_flag=False, error=f"{type(exc).__name__}: {exc}")
    res.usage.wall_s = time.perf_counter() - t0
    return res


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _jsonable(x: Any) -> Any:
    """Make numpy / pandas scalars JSON-serialisable."""
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return [_jsonable(v) for v in x.tolist()]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return None if np.isnan(x) else float(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, float) and np.isnan(x):
        return None
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    if x is pd.NA or x is pd.NaT:
        return None
    return x


def jsonable(x: Any) -> Any:
    return _jsonable(x)
