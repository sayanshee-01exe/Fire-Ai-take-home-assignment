"""Completeness and date-order checks: (g) null spike in one agency's records, (h) closed date
earlier than created date.

g - null spike (batch level). What "normal" means differs by agency and column (Jan-Aug 2023 rates):
DEP and DOB never fill location_type, DHS leaves most descriptors and ~22 % of ZIPs empty, NYPD fills
ZIP and coordinates for > 99 % of its rows. The three families differ in how they know what is normal.
  rule_null_rate_thresholds  One hand-written maximum null rate per column, the same for every
                             agency, checked for agencies with >= MIN_AGENCY_ROWS rows. Each limit
                             sits just above the highest rate an agency reaches in normal Jan-Aug
                             (reference) batches. Where one agency legitimately leaves a column
                             (almost) always empty, no single limit is both tight and quiet, so the
                             limit is 1.0 (unenforced). The resulting blind spots (descriptor,
                             location_type, resolution_description) are a finding about global rules.
  stat_null_spike_binomial   Per (agency, column) cell: the pooled reference null rate and how much
                             it varies between contiguous BATCH_SIZE-row reference batches
                             (beta-binomial overdispersion rho, method of moments). A cell spikes
                             when its null rate is above the reference range (RANGE_QUANTILE of the
                             reference batch rates) AND its beta-binomial upper-tail p-value,
                             Bonferroni-adjusted over the cells judged in the batch, is below
                             threshold_ (MAX_P_VALUE, lowered in fit() if more than
                             TARGET_BATCH_FAR of reference batches would alert). Beta-binomial, not
                             binomial, because real null rates drift between batches (DHS
                             closed_date goes from ~0 % to ~70 % mid-year; EDC leaves weeks of
                             helicopter-noise requests open): a binomial calls that drift a spike.
                             p-values are exact tail sums kept as log10 (a real spike's p-value is
                             far below the smallest float), not scipy's betabinom.sf: that is
                             1 - cdf, which returns 0 or ~1e-10 rounding noise in the far tail, so a
                             90 % spike could look less significant than a 7 % one.
  llm_null_spike_review      Per-agency row counts plus a stratified sample (<= LLM_ROWS_PER_AGENCY
                             rows of each agency with >= MIN_AGENCY_ROWS rows, at most
                             config.LLM_BATCH_SAMPLE_ROWS in all, nulls visible). The model gets no
                             baseline: it has to judge from the sample alone. To keep the prompt
                             small it sees only NULL_LLM_COLUMNS (the columns DESIGN section 3 lists
                             for this problem), while the rule and the stat check every column: the
                             LLM has fewer columns to raise a false alarm on.
  The rule and the stat raise one alert per (agency, column) spike, the LLM one alert for the spike it
  reports; all three return that agency's null values in the alert's columns as row flags (the rows a
  pipeline would quarantine).

h - closed before created (row level). A hard logical constraint, so the rule is the yardstick.
  rule_closed_after_created  Parse both dates with config.SOURCE_DATETIME_FORMAT; flag closed < created.
  stat_duration_outlier      Per complaint_type, the TARGET_ROW_FPR quantile of closed - created on
                             reference rows (types with < MIN_TYPE_ROWS rows share the all-types
                             quantile); flags rows below their type's bound. It knows nothing about
                             "negative", only "shorter than nearly every reference request of this
                             type", so it expects the sanitised reference (no negative durations).
                             Known weakness, measured inside the reference period (fit on Jan-Jun,
                             scored on Jul-Aug): types closed in bulk (DOHMH Food Establishment, Mobile
                             Food Vendor, Smoking) have a lower tail that shrinks with the created date,
                             so every closed Jul-Aug request of those types falls under its Jan-Jun
                             bound (row FPR 0.73 % out of time vs 0.085 % in sample).
  llm_date_order_review      created_date, closed_date and status per row.
  A missing or unparseable date is not checked: date formats are problem c's business.

fit() on the ~2.1 M-row reference needs no subsampling (a few seconds each): g is one groupby
over null flags, with p-values only for cells above the reference range (an exact tail sum costs
~0.3 ms per cell); h parses two date columns and takes per-type quantiles.
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd
from scipy import stats

from project_b import config, llm
from project_b.core import Alert, DetectionResult, Detector

MAX_EXAMPLE_ROWS = 20  # example rows listed in an alert

# --- g: null spike in one agency's records ---------------------------------------------------
# Every contract column except the key, the arrival timestamp and the grouping column itself.
NULL_CHECK_COLUMNS = [c for c in config.COLUMNS if c not in ("unique_key", "created_date", "agency")]
MIN_AGENCY_ROWS = 50  # rule and LLM: an agency needs this many rows in the batch to be judged
# Rule: the largest share of an agency's rows that may be null - ONE number per column, the same
# for every agency, read off the per-agency null rates of the Jan-Aug 2023 reference batches only
# (contiguous 5,000-row batches, agencies with >= MIN_AGENCY_ROWS rows). 1.0 = not enforceable.
MAX_NULL_RATE = {
    # filled by the 311 system for every request: never null in a Jan-Aug batch
    "agency_name": 0.05,
    "complaint_type": 0.05,
    "status": 0.05,
    "community_board": 0.05,
    "borough": 0.05,
    "open_data_channel_type": 0.05,
    "park_borough": 0.05,
    # DOT street-segment requests often have no point: DOT's Jan-Aug p99 is 25.8 %, its second-highest
    # batch 28.75 %; the one DOT burst on 2023-04-30 (53.3 %) is treated as a real spike
    "latitude": 0.30,
    "longitude": 0.30,
    "x_coordinate_state_plane": 0.30,
    "y_coordinate_state_plane": 0.30,
    # DHS requests often have no address: at most 56.9 % of DHS's rows in a Jan-Aug batch
    "incident_zip": 0.60,
    "city": 0.60,
    # DOT street segments have no house address: at most 71.5 % of DOT's rows in a Jan-Aug batch
    "incident_address": 0.75,
    "street_name": 0.75,
    # some agency legitimately leaves these (almost) always empty (Jan-Aug batch rates)
    "descriptor": 1.0,  # DHS: up to 98 %
    "location_type": 1.0,  # DEP and DOB: 100 %
    "closed_date": 1.0,  # open requests: whole EDC batches (February), up to 88 % of DHS's rows (July-August)
    "resolution_description": 1.0,  # the same open requests
    "resolution_action_updated_date": 1.0,  # the same open requests
    "descriptor_2": 1.0,  # optional detail: 54 % empty over Jan-Aug, up to 100 % of an agency's rows
    "due_date": 1.0,  # almost never filled (99.7 % empty over Jan-Aug)
}
# Stat
CELL = ["agency", "column"]
MIN_CELL_ROWS = 30  # (agency, column) cells with fewer rows in a batch are not judged
# RANGE_QUANTILE and MIN_RHO, checked on the reference period (Jan-Aug) only: fit() on the Jan-Jun 2023
# batches, scored on the 114 Jul-Aug batches. All 12 settings of RANGE_QUANTILE {0.95, 0.99, 1.0} x MIN_RHO
# {1e-9, 1e-7, 1e-5, 1e-3} gave identical verdicts: 2 of 114 clean batches alerted, 99.4 % of 342 hand-made
# spikes (U(0.3, 0.9) of one agency's values in one column nulled) and 86.3 % of 342 weak ones (10 %) were
# caught. A tie keeps the values below.
RANGE_QUANTILE = 0.99  # top of the normal range: robust to the odd real spike in the reference
MAX_P_VALUE = 1e-6  # a spike needs a Bonferroni-adjusted p-value below this (fit() may lower it)
MIN_RHO = 1e-5  # rho floor (Jan-Aug check above): practically binomial (rho = 0 would make the beta distribution infinitely narrow)
MAX_RHO = 0.99  # rho must stay below 1 for the beta distribution to exist
# LLM
LLM_ROWS_PER_AGENCY = 6
LLM_MAX_CHARS = 40  # long texts are cut: the model only needs to see whether a value is there
NULL_LLM_COLUMNS = [
    "agency", "complaint_type", "descriptor", "location_type", "incident_zip", "incident_address",
    "city", "community_board", "latitude", "longitude", "resolution_description",
]  # fmt: skip
NULL_SPIKE_LLM_TASK = f"""
Problem to check: NULL SPIKE in one agency's records.
You see one batch of New York City 311 service requests: how many rows each agency has, and a random sample of rows of every agency with at least {MIN_AGENCY_ROWS} rows (null = missing value; long texts are cut).
Report an issue only if ONE agency's rows miss a value in a column (or a pair such as latitude and longitude) far more often than normal: e.g. many of that agency's sample rows have a null incident_zip although they have an address, while the column is filled in the agency's other rows and in other agencies' rows.
Do NOT report: a column an agency leaves empty by design for all its rows (e.g. location_type or descriptor for some agencies); an occasional missing value; missing values spread over many agencies; columns that are not shown.
If you find a spike: issue_found=true, segment = that agency's code only, exactly as written in the row counts, columns = the affected column names exactly as shown, row_ids = ids of the sample rows missing the value, observed = how many of that agency's sample rows miss it, expected = what is normal. Otherwise issue_found=false.
"""

# --- h: closed date earlier than created date ------------------------------------------------
DATE_COLUMNS = ["created_date", "closed_date"]
MIN_TYPE_ROWS = 1_000  # >= 1 / TARGET_ROW_FPR rows, so a type's tail quantile is observed, not extrapolated
DATE_LLM_COLUMNS = ["created_date", "closed_date", "status"]
DATE_ORDER_LLM_TASK = """
Problem to check: CLOSED DATE EARLIER THAN CREATED DATE.
Each row is one New York City 311 service request: created_date (when it was opened), closed_date (when it was closed; null while still open) and status. Timestamps look like 2023-10-16T08:00:16.000 (year-month-day, then the time).
Flag a row (flagged=true) only if closed_date is strictly earlier than created_date. Compare the full timestamps, date and time: closing a few minutes, a few days or a whole year before the opening all count.
Do NOT flag: closed_date equal to or later than created_date, however short or long the gap; a null closed_date; a status that does not fit the dates; a value that is not a readable timestamp.
For a flagged row: observed = "created <created_date>, closed <closed_date>"; expected = "closed_date at or after created_date".
"""


# ==========================================================================================
# g - null spike in one agency's records
# ==========================================================================================
class RuleNullRateThresholds(Detector):
    problem = "g"
    family = "rule"
    name = "rule_null_rate_thresholds"
    description = (
        f"Flags an agency (>= {MIN_AGENCY_ROWS} rows in the batch) whose share of nulls in a column exceeds "
        "that column's hand-set maximum, one number per column for every agency."
    )

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        cells = _agency_null_counts(batch, np.zeros(len(batch), dtype=int))
        cells = cells[cells.n >= MIN_AGENCY_ROWS].assign(
            null_rate=lambda c: c.k / c.n,
            limit=lambda c: c.column.map(MAX_NULL_RATE),
        )
        spikes = cells[cells.null_rate > cells.limit]
        alerts = [
            self.alert(
                f"{s.agency}: {s.column} is null in {s.k} of {s.n} rows ({s.null_rate:.1%}), above the {s.limit:.0%} limit for that column",
                check_id="R-g1",
                columns=[s.column],
                segment={"agency": s.agency},
                observed=f"{s.null_rate:.1%} null ({s.k} of {s.n} rows)",
                expected=f"<= {s.limit:.0%} null (same limit for every agency)",
                score=float(s.null_rate / s.limit),  # how many times the limit
            )
            for s in spikes.itertuples(index=False)
        ]
        return _null_spike_result(self, batch, alerts)


class StatNullSpikeBinomial(Detector):
    problem = "g"
    family = "stat"
    name = "stat_null_spike_binomial"
    description = (
        "Per (agency, column) reference null rate and batch-to-batch spread; flags a batch rate above the reference "
        "range whose beta-binomial p-value, Bonferroni-adjusted over cells, is below a calibrated cut-off."
    )

    def fit(self, reference: pd.DataFrame) -> "StatNullSpikeBinomial":
        batch_ids = np.arange(len(reference)) // config.BATCH_SIZE
        cells = _agency_null_counts(reference, batch_ids)
        self.baseline_ = _null_baseline(cells)
        # Calibrate on the reference batches themselves: each batch's most significant candidate cell
        # (log10 of its adjusted p-value; 0 = p of 1 for a batch without candidates).
        candidates = _spike_candidates(cells, self.baseline_)
        batch_log10_p = candidates.groupby("batch").log10_p_adj.min().reindex(np.unique(batch_ids), fill_value=0.0).to_numpy()
        self.log10_threshold_ = _calibrated_cutoff(batch_log10_p)  # alert: log10 p_adj below this
        self.threshold_ = 10.0**self.log10_threshold_  # the same cut-off as a p-value
        self.reference_batch_far_ = float(np.mean(batch_log10_p < self.log10_threshold_)) if len(batch_log10_p) else 0.0
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        cells = _agency_null_counts(batch, np.zeros(len(batch), dtype=int))
        candidates = _spike_candidates(cells, self.baseline_)
        spikes = candidates[candidates.log10_p_adj < self.log10_threshold_]
        alerts = [
            self.alert(
                f"{s.agency}: {s.column} is null in {s.k} of {s.n} rows ({s.null_rate:.1%}); reference {s.base_rate:.1%}, "
                f"normal batches up to {s.upper:.1%}; beta-binomial p = {_p_value_text(s.log10_p_adj)} "
                f"after Bonferroni over {s.n_judged} cells",
                check_id="S-g1",
                columns=[s.column],
                segment={"agency": s.agency},
                observed=f"{s.null_rate:.1%} null ({s.k} of {s.n} rows)",
                expected=f"<= {s.upper:.1%} null (reference rate {s.base_rate:.1%})",
                score=float(10.0**s.log10_p_adj),  # the adjusted p-value (0.0 when below the smallest float)
            )
            for s in spikes.itertuples(index=False)
        ]
        return _null_spike_result(self, batch, alerts)


class LlmNullSpikeReview(Detector):
    problem = "g"
    family = "llm"
    name = "llm_null_spike_review"
    description = (
        "Gemini sees per-agency row counts and a stratified sample of rows (nulls visible) "
        "and says whether one agency shows a spike of missing values."
    )

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        agency = _text(batch, "agency")
        rows_per_agency = agency.value_counts()
        judged = rows_per_agency[rows_per_agency >= MIN_AGENCY_ROWS].index
        if judged.empty:  # no agency column, or no agency big enough to judge
            return self.result(False)
        shown = [c for c in NULL_LLM_COLUMNS if c in batch.columns]
        sample = _stratified_sample(agency, judged)
        payload = _null_payload(batch, sample, shown, rows_per_agency)
        try:
            reply, usage = llm.review_batch(NULL_SPIKE_LLM_TASK, payload, tag=f"{self.problem}/{self.name}")
        except llm.LLMUnavailable as exc:
            return self.result(False, error=f"llm_unavailable: {exc}")
        if reply is None:  # never read an unparseable answer as "no issue"
            return self.result(False, usage=usage, error="llm_unparseable_reply")
        if reply.get("issue_found") is not True:
            return self.result(False, usage=usage)
        named = _named_agency(reply.get("segment"), judged)
        sampled = set(sample.tolist())
        model_rows = list(dict.fromkeys(int(r) for r in _as_list(reply.get("row_ids")) if _number(r) is not None and r in sampled))
        alert = self.alert(
            str(reply.get("reason") or "the model reported a null spike without a reason"),
            check_id="L-g1",
            columns=_matching_columns(reply.get("columns"), shown),
            segment={"agency": named} if named else {"text": str(reply.get("segment") or "")},
            rows=model_rows[:MAX_EXAMPLE_ROWS],
            n_rows=len(model_rows),
            observed=reply.get("observed"),
            expected=reply.get("expected"),
            score=_number(reply.get("confidence")),
        )
        return _null_spike_result(self, batch, [alert], usage=usage)


# ==========================================================================================
# h - closed date earlier than created date
# ==========================================================================================
class RuleClosedAfterCreated(Detector):
    problem = "h"
    family = "rule"
    name = "rule_closed_after_created"
    description = "Parses created_date and closed_date in the source format and flags rows whose closed_date is earlier than created_date."

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        created, closed, gap = _date_gap(batch)
        flags = gap < 0  # NaN (missing or unparseable date) compares False
        if not flags.any():
            return self.result(False, row_flags=flags)
        reasons = {
            int(p): f"closed_date {closed[p]} is {_duration_text(-gap[p])} before created_date {created[p]}"
            for p in np.flatnonzero(flags)
        }
        first = int(np.flatnonzero(flags)[0])
        alert = _summary_alert(
            self,
            flags,
            f"{flags.sum()} rows were closed before they were created (closed_date < created_date)",
            check_id="R-h1",
            columns=DATE_COLUMNS,
            observed=f"created {created[first]}, closed {closed[first]} (row {first})",
            expected="closed_date >= created_date",
            score=float(flags.sum() / np.isfinite(gap).sum()),
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_reasons=reasons)


class StatDurationOutlier(Detector):
    problem = "h"
    family = "stat"
    name = "stat_duration_outlier"
    description = "Per complaint_type lower-tail bound (TARGET_ROW_FPR quantile) of closed - created learned from reference; flags rows below it."

    def fit(self, reference: pd.DataFrame) -> "StatDurationOutlier":
        _, _, gap = _date_gap(reference)
        durations = pd.DataFrame({"type": _text(reference, "complaint_type"), "gap": gap}).dropna(subset=["gap"])
        q = config.TARGET_ROW_FPR
        self.default_bound_ = float(durations.gap.quantile(q)) if len(durations) else 0.0
        type_rows = durations.type.value_counts()
        common = durations[durations.type.isin(type_rows.index[type_rows >= MIN_TYPE_ROWS])]
        self.bounds_ = common.groupby("type").gap.quantile(q).to_dict()  # seconds, per complaint type
        self.threshold_ = q  # lower-tail probability below which a row is flagged
        self.reference_fpr_ = float(np.mean(durations.gap.to_numpy() < self._bounds(durations.type))) if len(durations) else 0.0
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        created, closed, gap = _date_gap(batch)
        types = _text(batch, "complaint_type")
        bound = self._bounds(types)
        flags = gap < bound  # NaN gap compares False
        row_scores = np.nan_to_num((bound - gap) / 3600)  # hours below the bound; > 0 = flagged
        if not flags.any():
            return self.result(False, row_flags=flags, row_scores=row_scores)
        reasons = {int(p): self._reason(types.iloc[p], gap[p], bound[p]) for p in np.flatnonzero(flags)}
        first = int(np.flatnonzero(flags)[0])
        alert = _summary_alert(
            self,
            flags,
            f"{flags.sum()} rows were closed implausibly early for their complaint type ({(flags & (gap < 0)).sum()} before they were created)",
            check_id="S-h1",
            columns=DATE_COLUMNS + ["complaint_type"],
            observed=f"created {created[first]}, closed {closed[first]} (row {first}): {_signed_duration(gap[first])}",
            expected=f"closed - created >= the {self.threshold_:.1%} quantile of the complaint type's reference durations "
            f"(here {_signed_duration(bound[first])})",
            score=float(flags.sum() / np.isfinite(gap).sum()),
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_reasons=reasons, row_scores=row_scores)

    def _bounds(self, types: pd.Series) -> np.ndarray:
        """Each row's lower bound in seconds: its complaint type's, else the all-types one."""
        return types.map(self.bounds_).fillna(self.default_bound_).to_numpy(dtype=float)

    def _reason(self, complaint_type: str, gap: float, bound: float) -> str:
        return (
            f"closed - created = {_signed_duration(gap)}, below the {self.threshold_:.1%} reference quantile "
            f"{_signed_duration(bound)} for complaint type '{complaint_type}'"
        )


class LlmDateOrderReview(Detector):
    problem = "h"
    family = "llm"
    name = "llm_date_order_review"
    description = "Gemini reviews created_date, closed_date and status of each row and flags requests closed before they were created."

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        positions = _review_positions(review_rows, len(batch))
        nothing = np.zeros(len(batch), dtype=bool)
        reviewed = nothing.copy()
        reviewed[positions] = True
        if any(c not in batch.columns for c in DATE_COLUMNS):  # nothing to compare: no row can be flagged
            return self.result(False, row_flags=nothing, reviewed=reviewed)
        rows = _subset(batch, DATE_LLM_COLUMNS).iloc[positions].to_dict("records")
        records = [{"id": int(p), **values} for p, values in zip(positions, rows)]
        abstained: dict[int, str] = {}
        try:
            verdicts, usage, _ = llm.review_rows(DATE_ORDER_LLM_TASK, records, tag=f"{self.problem}/{self.name}", abstained=abstained)
        except llm.LLMUnavailable as exc:
            return self.result(False, row_flags=nothing, reviewed=nothing, error=f"llm_unavailable: {exc}")
        answered = [p for p in positions.tolist() if p in verdicts]
        # Only rows with a readable verdict are reviewed; the rest abstained (never read as "clean").
        reviewed = nothing.copy()
        reviewed[answered] = True

        flags = nothing.copy()
        reasons, evidence = {}, {}
        for p in answered:
            verdict = verdicts[p]
            if verdict.get("flagged") is True:
                flags[p] = True
                reasons[p] = str(verdict.get("reason") or "flagged by the model without a reason")
                evidence[p] = {key: verdict.get(key) for key in ("observed", "expected", "confidence")}
        if not flags.any():
            return self.result(False, row_flags=flags, reviewed=reviewed, abstained=abstained, usage=usage)
        first = int(np.flatnonzero(flags)[0])
        unanswered = len(positions) - len(answered)
        confidences = [e["confidence"] for e in evidence.values() if _number(e["confidence"]) is not None]
        alert = _summary_alert(
            self,
            flags,
            f"LLM review flagged {flags.sum()} of {len(answered)} reviewed rows as closed before created"
            + (f" ({unanswered} rows got no verdict)" if unanswered else "")
            + f"; e.g. row {first}: {reasons[first]}",
            check_id="L-h1",
            columns=DATE_COLUMNS,
            observed=evidence[first]["observed"],
            expected=evidence[first]["expected"],
            score=float(np.mean(confidences)) if confidences else None,
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_reasons=reasons, row_evidence=evidence, reviewed=reviewed,
                           abstained=abstained, usage=usage)


# ==========================================================================================
# g helpers
# ==========================================================================================
def _agency_null_counts(frame: pd.DataFrame, batch_ids: np.ndarray) -> pd.DataFrame:
    """One row per (batch, agency, column): n = the agency's rows in that batch, k = its nulls in
    the column. Empty without an agency column: nulls cannot be attributed to an agency."""
    columns = [c for c in NULL_CHECK_COLUMNS if c in frame.columns]
    nulls = pd.DataFrame({c: _column(frame, c).isna().to_numpy() for c in columns}, index=range(len(frame)))
    groups = nulls.groupby([batch_ids, _text(frame, "agency").to_numpy()])  # rows without an agency drop out
    counts = groups.sum()
    counts.insert(0, "n", groups.size())
    counts = counts.rename_axis(["batch", "agency"]).reset_index()
    return counts.melt(id_vars=["batch", "agency", "n"], var_name="column", value_name="k")


def _null_baseline(cells: pd.DataFrame) -> pd.DataFrame:
    """Per (agency, column) of the reference: pooled null rate, overdispersion rho, top of the normal range."""
    totals = cells.groupby(CELL)[["n", "k"]].sum()
    base_rate = (totals.k + 0.5) / (totals.n + 1)  # +0.5/+1: a never-null column keeps a tiny, non-zero rate
    # Method of moments: a beta-binomial batch count varies (1 + (n-1) rho) times more than a binomial one,
    # so the Pearson chi-square over B batches is about (B - 1) + rho * sum(n - 1).
    p = cells.join(base_rate.rename("p"), on=CELL).p
    pearson = (cells.k - cells.n * p) ** 2 / (cells.n * p * (1 - p))
    by_cell = [cells.agency, cells.column]
    chi2 = pearson.groupby(by_cell).sum()
    n_batches = cells.groupby(CELL).size()
    extra_rows = (cells.n - 1).groupby(by_cell).sum()
    rho = ((chi2 - (n_batches - 1)) / extra_rows.where(extra_rows > 0)).fillna(0.0).clip(MIN_RHO, MAX_RHO)
    judged = cells[cells.n >= MIN_CELL_ROWS]
    upper = (judged.k / judged.n).groupby([judged.agency, judged.column]).quantile(RANGE_QUANTILE)
    baseline = pd.DataFrame({"base_rate": base_rate, "rho": rho, "upper": upper})
    baseline["upper"] = baseline.upper.fillna(baseline.base_rate)  # never judged in the reference: the rate is the range
    return baseline


def _spike_candidates(cells: pd.DataFrame, baseline: pd.DataFrame) -> pd.DataFrame:
    """Judged cells (>= MIN_CELL_ROWS rows, known baseline) whose null rate is above the reference
    range, with log10 of their beta-binomial upper-tail p-value Bonferroni-adjusted over all cells
    judged in the same batch (log10_p_adj, at most 0). Only these cells get a p-value: a cell inside
    the range can never spike."""
    judged = cells[cells.n >= MIN_CELL_ROWS].join(baseline, on=CELL, how="inner")
    judged = judged.assign(null_rate=judged.k / judged.n, n_judged=judged.groupby("batch").n.transform("size"))
    above = judged[judged.null_rate > judged.upper]
    strength = 1 / above.rho - 1  # a + b of the beta distribution of the batch null rate
    a, b = above.base_rate * strength, (1 - above.base_rate) * strength
    log10_p = _log10_upper_tail(above.k.to_numpy(), above.n.to_numpy(), a.to_numpy(), b.to_numpy())
    return above.assign(log10_p_adj=np.minimum(0.0, log10_p + np.log10(above.n_judged.to_numpy(dtype=float))))


def _log10_upper_tail(k: np.ndarray, n: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """log10 P(K >= k) for K ~ BetaBinomial(n, a, b), one value per cell: the probabilities of
    k, k+1, ..., n summed in log space, so even a p-value of 1e-1000 keeps its size and order."""
    tails = [np.logaddexp.reduce(stats.betabinom.logpmf(np.arange(ki, ni + 1), ni, ai, bi)) for ki, ni, ai, bi in zip(k, n, a, b)]
    return np.asarray(tails, dtype=float) / np.log(10)


def _calibrated_cutoff(batch_log10_p: np.ndarray) -> float:
    """Largest log10 p-value cut-off (at most log10 MAX_P_VALUE) at which no more than TARGET_BATCH_FAR
    of the reference batches alert, given each batch's smallest adjusted log10 p-value (alert: below
    the cut-off, so a batch exactly at it does not alert)."""
    cap = float(np.log10(MAX_P_VALUE))
    allowed = int(config.TARGET_BATCH_FAR * len(batch_log10_p))
    if allowed >= len(batch_log10_p):
        return cap
    return float(min(cap, np.sort(batch_log10_p)[allowed]))


def _p_value_text(log10_p: float) -> str:
    """A p-value for people: '3.2e-09', or '1e-412' when it is too small for a float."""
    return f"{10.0**log10_p:.1e}" if log10_p > -300 else f"1e{log10_p:.0f}"


def _null_spike_result(det: Detector, batch: pd.DataFrame, alerts: list[Alert], **fields) -> DetectionResult:
    """Batch result with one alert per spike. The spiking agency's null values in the alert's
    columns (absent columns ignored) become row flags - the rows a pipeline would quarantine - and
    the alert messages their reason. The agency column and each column's nulls are read once, not
    once per alert: a batch with many spikes stays fast."""
    agency = _text(batch, "agency").to_numpy()
    named = {c for alert in alerts for c in alert.columns if c in batch.columns}
    nulls = {c: _column(batch, c).isna().to_numpy() for c in named}
    flags = np.zeros(len(batch), dtype=bool)
    messages: dict[int, list[str]] = {}
    for alert in alerts:
        spiking = alert.segment.get("agency")
        columns = [nulls[c] for c in alert.columns if c in nulls]
        if spiking is None or not columns:
            continue
        rows = np.flatnonzero((agency == spiking) & np.logical_or.reduce(columns))
        if len(rows):
            alert.rows, alert.n_rows = rows[:MAX_EXAMPLE_ROWS].tolist(), len(rows)
        flags[rows] = True
        for p in rows.tolist():
            messages.setdefault(p, []).append(alert.message)
    reasons = {p: "; ".join(texts) for p, texts in messages.items()}
    return det.result(bool(alerts), alerts=alerts, row_flags=flags, row_reasons=reasons, **fields)


def _stratified_sample(agency: pd.Series, agencies: pd.Index) -> np.ndarray:
    """Sorted batch positions: the same number of random rows (seeded) from each judged agency (largest
    first), never more than config.LLM_BATCH_SAMPLE_ROWS rows: past that many agencies, one row each."""
    agencies = agencies[: config.LLM_BATCH_SAMPLE_ROWS]
    per_agency = int(np.clip(config.LLM_BATCH_SAMPLE_ROWS // len(agencies), 1, LLM_ROWS_PER_AGENCY))
    rng = np.random.default_rng(config.SEED)
    picks = [rng.choice(np.flatnonzero((agency == a).to_numpy()), size=per_agency, replace=False) for a in agencies]
    return np.sort(np.concatenate(picks))


def _null_payload(batch: pd.DataFrame, sample: np.ndarray, shown: list[str], rows_per_agency: pd.Series) -> str:
    """What the model sees: row counts per agency, then one JSON line per sample row (id = batch position)."""
    counts = {str(a): int(n) for a, n in rows_per_agency.items()}
    rows = _subset(batch, shown).iloc[sample].to_dict("records")
    lines = [json.dumps({"id": int(p), **{k: _shown(v) for k, v in row.items()}}, ensure_ascii=False, default=str) for p, row in zip(sample, rows)]
    return f"Rows per agency in this batch of {len(batch)} rows: {json.dumps(counts)}\n\nSample rows (JSON Lines):\n" + "\n".join(lines)


def _shown(value):
    """A cell as the model sees it: None when missing, long text cut to LLM_MAX_CHARS."""
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    if isinstance(value, str):
        return value[:LLM_MAX_CHARS]
    return value.item() if isinstance(value, np.generic) else value


def _named_agency(segment, agencies: pd.Index) -> str | None:
    """The one judged agency whose code appears as a word in the model's `segment`, else None."""
    words = set(re.findall(r"[A-Z]+", str(segment or "").upper()))
    named = [a for a in agencies if str(a).upper() in words]
    return named[0] if len(named) == 1 else None


def _matching_columns(names, shown: list[str]) -> list[str]:
    """The shown columns the model named, matched ignoring case, spaces and punctuation (the model may
    write 'Incident Zip' for incident_zip, as the localisation metric allows); other names are dropped."""
    by_key = {_name_key(c): c for c in shown}
    return list(dict.fromkeys(by_key[key] for key in map(_name_key, _as_list(names)) if key in by_key))


def _name_key(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _as_list(value) -> list:
    """A list field of the model's reply: None -> [], a single value -> [value], a list -> itself."""
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


# ==========================================================================================
# h helpers
# ==========================================================================================
def _date_gap(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """created_date and closed_date as delivered, and closed - created in seconds (NaN when either
    date is missing or not in the source format)."""
    created, closed = _text(frame, "created_date"), _text(frame, "closed_date")
    gap = (_parse_dates(closed) - _parse_dates(created)).dt.total_seconds()
    return created.to_numpy(), closed.to_numpy(), gap.to_numpy(dtype=float, na_value=np.nan)


def _parse_dates(texts: pd.Series) -> pd.Series:
    """Timestamps in config.SOURCE_DATETIME_FORMAT; anything else becomes NaT."""
    return pd.to_datetime(texts, format=config.SOURCE_DATETIME_FORMAT, errors="coerce")


def _duration_text(seconds: float) -> str:
    """A positive time span in readable units, e.g. '45 min', '3.5 h', '12.0 days'."""
    if seconds < 60:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def _signed_duration(seconds: float) -> str:
    return f"-{_duration_text(-seconds)}" if seconds < 0 else _duration_text(seconds)


# ==========================================================================================
# shared helpers
# ==========================================================================================
def _column(frame: pd.DataFrame, name: str) -> pd.Series | None:
    """The column called `name` (the first one if the name is duplicated), or None when absent."""
    if name not in frame.columns:
        return None
    values = frame[name]
    return values.iloc[:, 0] if isinstance(values, pd.DataFrame) else values


def _text(frame: pd.DataFrame, name: str) -> pd.Series:
    """Column as strings with a positional index (NaN where missing); all NaN when the column is absent."""
    values = _column(frame, name)
    if values is None:
        return pd.Series(np.nan, index=range(len(frame)), dtype="str")
    return values.astype("str").reset_index(drop=True)


def _subset(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """The named columns that exist (first copy of a duplicated name), with a positional index."""
    return pd.DataFrame({c: _column(frame, c).to_numpy() for c in columns if c in frame.columns}, index=range(len(frame)))


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _review_positions(review_rows: np.ndarray | None, n_rows: int) -> np.ndarray:
    """Batch positions to review: every row when the harness passes None, else the valid distinct ones."""
    if review_rows is None:
        return np.arange(n_rows)
    positions = np.unique(np.asarray(review_rows, dtype=int))
    return positions[(positions >= 0) & (positions < n_rows)]


def _summary_alert(det: Detector, flags: np.ndarray, message: str, **fields) -> Alert:
    """The one alert a row-level detector raises per batch: row count plus the first example rows."""
    rows = np.flatnonzero(flags)
    return det.alert(message, rows=rows[:MAX_EXAMPLE_ROWS].tolist(), n_rows=len(rows), **fields)


DETECTORS = [
    RuleNullRateThresholds,
    StatNullSpikeBinomial,
    LlmNullSpikeReview,
    RuleClosedAfterCreated,
    StatDurationOutlier,
    LlmDateOrderReview,
]
