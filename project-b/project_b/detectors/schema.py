"""Schema checks: renamed or dropped column (a), column type change (b) and a date
format switch partway through a batch (c).

All three are properties of the delivery rather than of single service requests, so
every detector here judges the whole batch; for c the rows after the switch are also
returned as row_flags.

Why these designs
* Rules (R-a1, R-b1, R-c1) are the written contract in `config` - column names, dtypes,
  the ISO timestamp layout - and learn nothing from data. They are exact and nearly free,
  but blind to anything the contract does not spell out: a rename whose new name looks
  nothing like the old one cannot be paired, a float column holding "10463.0" strings
  passes a dtype check, and a day/month swap with day <= 12 is still a valid ISO date.
* Statistical detectors compare the batch with what reference batches looked like: a
  content profile per column (recognises a renamed column by its values, whatever its
  new name), the mix of value types per column (sees partial and value-level type drift),
  and the shape of date strings plus the arrival-order timeline (catches day/month swaps
  that keep the ISO shape). Thresholds are calibrated in fit() on reference batches of
  config.BATCH_SIZE contiguous rows. Known limits of the column profile: it cannot tell
  sibling columns apart (borough / park_borough, created_date / closed_date, latitude /
  longitude score > 0.98 against each other; even incident_address vs a date column
  scores ~0.93), and it moves with the complaint mix (see PROFILE_SIMILARITY_CAP).
* LLM reviewers see the contract and a small deterministic sample, never the whole
  batch: a batch-level verdict costs one call, and the prompt holds only what the check
  needs.

fit() subsamples: it uses at most FIT_MAX_BATCHES evenly spaced reference batches. The
statistics are per batch anyway, so fit() costs the same on the ~2.1 M-row reference as on
50 batches: measured on 50 real reference batches, ~9 s for the column profile and ~2.5 s
each for the type mix and the date shapes.
"""
from __future__ import annotations

import difflib
import re
import string
from typing import ClassVar

import numpy as np
import pandas as pd

from project_b import config, llm
from project_b.core import Alert, DetectionResult, Detector

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
FIT_MAX_BATCHES = 50  # reference batches used to fit and calibrate (evenly spaced)
MAX_EXAMPLE_ROWS = 20  # example row positions per alert

# a - renamed or dropped column
NAME_SIMILARITY_MIN = 0.6  # difflib ratio of normalised names to call a pair a likely rename
PROFILE_TOP_K = 20  # most frequent recurring reference values kept per column profile
MIN_PROFILE_VALUES = 100  # fewer non-null values (e.g. due_date, ~0.5 % filled) give too noisy a profile
# Upper bound for the calibrated similarity threshold: a column at least this similar to its
# reference profile is never alerted. Measured on real reference batches (Jan-Aug, 5,000
# contiguous rows): the 5 % quantile of the 50 fit batches is 0.94, yet 4 of 49 other reference
# batches fell below it (a 5 % tail estimated from 50 batches is noisy), while none of the 99
# went below 0.90. The least similar column is usually descriptor_2: its null rate follows the
# complaint mix of the hours a batch covers.
PROFILE_SIMILARITY_CAP = 0.9

# b - column type change
# A type-mix shift below 1 % of the values is never alerted. Real reference batches have pure
# mixes (shift 0), so this floor is the working threshold: a few stray values are not a type change.
TYPE_SHIFT_MIN = 0.01

# c - date format switch
COHERENCE_DAYS = 2.0  # consecutive created_dates are seconds apart: a jump this big cannot be real
FORMAT_GUESS_SAMPLE = 200  # offending values tried against each candidate format
FORMAT_GUESS_MIN_SHARE = 0.9  # share of them a format must parse to be named as the guess
CANDIDATE_DATE_FORMATS = [  # hand-written list of common layouts, used only to explain an alert
    "%Y-%d-%mT%H:%M:%S.%f",  # ISO with day and month swapped
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y",
]

# LLM sample sizes (c shows config.LLM_BATCH_SAMPLE_ROWS rows)
LLM_SCHEMA_SAMPLE_ROWS = 3  # evenly spaced rows shown next to the header (a, b)
LLM_VALUE_MAX_CHARS = 60  # long values are cut in the prompt to save tokens

# --------------------------------------------------------------------------
# LLM task prompts: what counts as the problem and what does not
# --------------------------------------------------------------------------
TASK_RENAMED_OR_DROPPED = """
Check ONE problem: a contract column was RENAMED or DROPPED in this batch of NYC 311 service requests.
Compare the contract with the batch as received.
Counts: a contract column absent from the batch (dropped), or present under another name - a different
spelling, case, spacing or abbreviation, or a different word. Use the sample values to tell which contract
column a renamed column holds.
Does not count: a different column order; a different dtype or value format of a column that keeps its
contract name; an extra column while no contract column is missing.
If you find it: issue_found=true; columns = the contract name(s) and the new name(s); observed = what the
batch has; expected = what the contract requires.
"""

TASK_TYPE_CHANGE = """
Check ONE problem: a column's data TYPE differs from the contract in this batch of NYC 311 service requests.
Values are shown with Python repr, so '40.71' is a string and 40.71 is a number.
Counts: a column whose dtype differs from its contract dtype (e.g. numbers delivered as strings, text
delivered as numbers), or an object column that mixes types (e.g. numbers in early rows, strings later).
Does not count: missing or renamed columns; null values (nan), even a column that is entirely null; unusual
but correctly typed values.
If you find it: issue_found=true; columns = the affected column(s); observed = the dtype and an example value
as shown; expected = the contract dtype.
"""

TASK_DATE_FORMAT_SWITCH = """
Check ONE problem: the DATE FORMAT changes partway through this batch of NYC 311 service requests.
The rows are evenly spaced samples of one batch in arrival order. The batch is sorted by created_date, so
created_date never moves backwards and moves forward by minutes or hours between the rows shown.
Contract format of both columns: YYYY-MM-DDTHH:MM:SS.fff (e.g. 2023-10-16T08:00:16.000).
Counts: from some position on, dates use another layout (other separators or field order, a 12-hour clock
with AM/PM, missing seconds or milliseconds), or have day and month swapped (an impossible month such as
2023-16-10, or a well-formed created_date that jumps weeks or months away from the rows before it).
Does not count: a null closed_date; a closed_date hours, days or weeks after created_date; a closed_date
before created_date; the batch crossing midnight; a column shown as (column missing).
If you find it: issue_found=true; columns = the affected column(s); segment = "from position N" (the first
affected position); row_ids = the positions of ALL shown rows whose dates are affected; observed = the
affected values exactly as shown, separated by "; "; expected = the contract format.
"""

CONTRACT_DATE_LAYOUT = "YYYY-MM-DDTHH:MM:SS.fff"
INT_PATTERN = r"[+-]?\d+"
NUMBER_PATTERN = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
SHAPE_TABLE = str.maketrans(string.digits + string.ascii_letters, "9" * 10 + "A" * 52)


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def _column(batch: pd.DataFrame, name: str) -> pd.Series | None:
    """The first column called `name`, or None. Positional, so a duplicated column
    name cannot turn the result into a DataFrame."""
    for i, col in enumerate(batch.columns):
        if str(col) == name:
            return batch.iloc[:, i]
    return None


def _reference_batches(reference: pd.DataFrame) -> list[pd.DataFrame]:
    """Up to FIT_MAX_BATCHES evenly spaced slices of config.BATCH_SIZE contiguous rows."""
    n_full = len(reference) // config.BATCH_SIZE
    if n_full == 0:
        return [reference]
    picks = np.unique(np.linspace(0, n_full - 1, min(n_full, FIT_MAX_BATCHES)).round().astype(int))
    return [reference.iloc[i * config.BATCH_SIZE : (i + 1) * config.BATCH_SIZE] for i in picks]


def _alert_threshold(values: list[float], upper: bool) -> float:
    """Threshold that at most config.TARGET_BATCH_FAR of the reference batches cross: their
    `values` lie above it (upper=True) or below it (upper=False) only for those few batches.
    An order statistic, not an interpolated quantile: with 50 batches np.quantile(values, 0.95)
    sits between the 3rd and 2nd largest value, so 3 of 50 (6 %) would alert."""
    ordered = np.sort(np.asarray(values, dtype=float))
    allowed = int(config.TARGET_BATCH_FAR * len(ordered))  # reference batches allowed to alert
    return float(ordered[len(ordered) - 1 - allowed] if upper else ordered[allowed])


def _example_rows(mask: np.ndarray) -> list[int]:
    return np.flatnonzero(mask)[:MAX_EXAMPLE_ROWS].tolist()


def _evenly_spaced(n_rows: int, k: int) -> np.ndarray:
    return np.unique(np.linspace(0, n_rows - 1, min(n_rows, k)).round().astype(int))


def _header_diff(columns: pd.Index) -> tuple[list[str], list[int]]:
    """(contract columns absent from the header, positions of the header columns outside the
    contract). Positions, because a rename can collide with a contract name: a second column
    called 'agency' is unexpected although its name is in the contract."""
    seen: set[str] = set()
    unexpected = []
    for pos, name in enumerate(map(str, columns)):
        if name not in config.FEED_DTYPES or name in seen:
            unexpected.append(pos)
        seen.add(name)
    return [c for c in config.COLUMNS if c not in seen], unexpected


def _pair_best(scores: dict[tuple[str, int], float]) -> list[tuple[str, int, float]]:
    """Greedy one-to-one pairing of (missing column, position of an unexpected column), best score first."""
    pairs, used_missing, used_unexpected = [], set(), set()
    for (missing, unexpected), score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])):
        if missing not in used_missing and unexpected not in used_unexpected:
            pairs.append((missing, unexpected, score))
            used_missing.add(missing)
            used_unexpected.add(unexpected)
    return pairs


def _isinstance_mask(s: pd.Series, types: type | tuple[type, ...]) -> np.ndarray:
    values = s.to_numpy(dtype=object)
    return np.fromiter((isinstance(v, types) for v in values), dtype=bool, count=len(values))


# --------------------------------------------------------------------------
# a - renamed or dropped column
# --------------------------------------------------------------------------
def _normalised(name: str) -> str:
    """'Incident Zip' -> 'incident_zip': case and separators are not a real difference."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalised(a), _normalised(b)).ratio()


class RuleSchemaColumns(Detector):
    problem = "a"
    family = "rule"
    name = "rule_schema_columns"
    description = (
        "Compares the header with config.COLUMNS: reports missing and unexpected columns and pairs a "
        "missing with an unexpected column as a likely rename when their names are similar (difflib >= 0.6)."
    )

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        missing, unexpected_at = _header_diff(batch.columns)
        # Keyed by position: two unexpected columns may share a name, and each must be reported.
        name = {pos: str(batch.columns[pos]) for pos in unexpected_at}
        scores = {(m, pos): _name_similarity(m, name[pos]) for m in missing for pos in unexpected_at}
        renames = _pair_best({pair: sim for pair, sim in scores.items() if sim >= NAME_SIMILARITY_MIN})
        renamed_old, renamed_at = {old for old, _, _ in renames}, {pos for _, pos, _ in renames}
        alerts = [
            self.alert(
                f"'{old}' is missing and '{name[pos]}' is unexpected: likely renamed (name similarity {sim:.2f})",
                check_id="R-a1", columns=[old, name[pos]], observed=name[pos], expected=old, score=sim,
            )
            for old, pos, sim in renames
        ]
        alerts += [
            self.alert(
                f"contract column '{col}' is missing from the header",
                check_id="R-a1", columns=[col], observed="absent", expected=f"{col} ({config.FEED_DTYPES[col]})",
            )
            for col in missing
            if col not in renamed_old
        ]
        alerts += [
            self.alert(
                f"column '{name[pos]}' "
                f"{'repeats a contract column name' if name[pos] in config.FEED_DTYPES else 'is not in the contract'}",
                check_id="R-a1", columns=[name[pos]], observed=name[pos], expected="each column of config.COLUMNS once",
                severity="warning",
            )
            for pos in unexpected_at
            if pos not in renamed_at
        ]
        return self.result(bool(alerts), alerts=alerts)


SHARE_FEATURES = ("null_rate", "distinct_ratio", "numeric_share", "top_share")


def _recurring_top_values(batches: list[pd.DataFrame], col: str) -> list[str]:
    """The PROFILE_TOP_K most frequent reference values among those found in at least half
    of the reference batches: values that identify the column (agency codes, borough names,
    template texts), not timestamps or keys that never come back in a later batch."""
    per_batch = [b[col].dropna() for b in batches]
    counts = pd.concat(per_batch).value_counts()
    batches_with_value = pd.concat([v.drop_duplicates() for v in per_batch]).value_counts()
    recurring = counts[batches_with_value.reindex(counts.index) >= len(batches) / 2]
    return recurring.index[:PROFILE_TOP_K].astype("str").tolist()  # profiles compare values as text


def _column_content(s: pd.Series) -> tuple[dict, pd.Series]:
    """(profile features of one column in one batch, its non-null values as text). The features
    are everything but the top-value share, which depends on the contract column the content is
    read as. Values are compared as text, so a renamed column is recognised whatever dtype it
    arrives with."""
    text = s.dropna().astype("str")
    if text.empty:
        return {"dtype": s.dtype.kind, "values": 0, "mean_length": 0.0, "null_rate": 1.0,
                "distinct_ratio": 0.0, "numeric_share": 0.0}, text
    features = {
        "dtype": s.dtype.kind,
        "values": len(text),  # sample size, not a similarity feature
        "mean_length": float(text.str.len().mean()),
        "null_rate": 1 - len(text) / len(s),
        "distinct_ratio": text.nunique() / len(text),
        "numeric_share": float(text.str.fullmatch(NUMBER_PATTERN).mean()),
    }
    return features, text


def _profile(content: tuple[dict, pd.Series], top_values: list[str]) -> dict:
    """Content fingerprint of one column, read as the contract column whose recurring
    reference values are `top_values`."""
    features, text = content
    return {**features, "top_share": float(text.isin(top_values).mean()) if len(text) else 0.0}


def _median_profile(profiles: list[dict]) -> dict:
    frame = pd.DataFrame(profiles)
    return {"dtype": frame["dtype"].mode().iloc[0], **frame[["mean_length", *SHARE_FEATURES]].median().to_dict()}


def _profile_similarity(p: dict, q: dict) -> float:
    """Mean of per-feature similarities in [0, 1]; 1 means identical profiles."""
    longest = max(p["mean_length"], q["mean_length"], 1.0)
    parts = [
        float(p["dtype"] == q["dtype"]),
        1 - abs(p["mean_length"] - q["mean_length"]) / longest,  # relative: lengths are unbounded
        *(1 - abs(p[f] - q[f]) for f in SHARE_FEATURES),  # shares already lie in [0, 1]
    ]
    return float(np.mean(parts))


def _rounded(profile: dict) -> dict:
    return {k: round(v, 3) if isinstance(v, float) else v for k, v in profile.items()}


class StatColumnProfileMatch(Detector):
    problem = "a"
    family = "stat"
    name = "stat_column_profile_match"
    description = (
        "Recognises columns by a content profile learned on reference batches (dtype kind, null rate, distinct "
        "ratio, mean length, numeric share, top values): pairs each missing column with the unexpected column "
        "carrying its profile and flags a present column whose profile no longer matches."
    )

    def fit(self, reference: pd.DataFrame) -> "StatColumnProfileMatch":
        batches = _reference_batches(reference)
        columns = [c for c in config.COLUMNS if c in reference.columns]
        self.top_values_ = {c: _recurring_top_values(batches, c) for c in columns}
        per_batch = [{c: self._profile_as(_column_content(b[c]), c) for c in columns} for b in batches]
        self.profiles_ = {c: _median_profile([p[c] for p in per_batch]) for c in columns}
        # A batch alerts when its least similar column falls below the threshold, so calibrate on
        # each reference batch's least similar column: at most TARGET_BATCH_FAR of them would alert.
        worst = [min(self._judged_similarities(p).values(), default=1.0) for p in per_batch]
        self.calibrated_threshold_ = _alert_threshold(worst, upper=False)
        self.threshold_ = min(self.calibrated_threshold_, PROFILE_SIMILARITY_CAP)
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        missing, unexpected_at = _header_diff(batch.columns)
        missing = [c for c in missing if c in self.profiles_]
        # Each candidate column is read once, not once per missing column (a whole header renamed
        # is 25 x 25 pairs). An empty batch has names but no values to match.
        contents = {pos: _column_content(batch.iloc[:, pos]) for pos in unexpected_at} if missing and len(batch) else {}
        scores = {(m, pos): self._similarity(content, m) for m in missing for pos, content in contents.items()}
        renames = _pair_best({pair: sim for pair, sim in scores.items() if sim >= self.threshold_})
        renamed = {old for old, _, _ in renames}
        alerts = [self._renamed_alert(str(batch.columns[pos]), old, contents[pos], sim) for old, pos, sim in renames]
        alerts += [self._dropped_alert(batch, col, scores) for col in missing if col not in renamed]
        if len(batch):
            alerts += self._changed_alerts(batch)
        return self.result(bool(alerts), alerts=alerts)

    def _profile_as(self, content: tuple[dict, pd.Series], column: str) -> dict:
        """Profile of a column's content read as contract column `column` (top values are per column)."""
        return _profile(content, self.top_values_[column])

    def _similarity(self, content: tuple[dict, pd.Series], column: str) -> float:
        return _profile_similarity(self._profile_as(content, column), self.profiles_[column])

    def _judged_similarities(self, profiles: dict[str, dict]) -> dict[str, float]:
        """Similarity to the reference profile for every column with enough values to judge."""
        return {
            col: _profile_similarity(p, self.profiles_[col])
            for col, p in profiles.items()
            if p["values"] >= MIN_PROFILE_VALUES
        }

    def _renamed_alert(self, new: str, old: str, content: tuple[dict, pd.Series], sim: float) -> Alert:
        return self.alert(
            f"'{new}' carries the profile of missing column '{old}' (similarity {sim:.2f}): likely renamed",
            check_id="S-a1", columns=[old, new], score=sim,
            observed={"column": new, **_rounded(self._profile_as(content, old))},
            expected={"column": old, **_rounded(self.profiles_[old])},
        )

    def _dropped_alert(self, batch: pd.DataFrame, col: str, scores: dict[tuple[str, int], float]) -> Alert:
        closest = max(((sim, pos) for (m, pos), sim in scores.items() if m == col), default=None)
        observed = "absent" if closest is None else (
            f"absent; closest unexpected column '{batch.columns[closest[1]]}' (similarity {closest[0]:.2f})"
        )
        return self.alert(
            f"column '{col}' is missing and no column carries its profile (threshold {self.threshold_:.2f}): likely dropped",
            check_id="S-a1", columns=[col], observed=observed, expected=_rounded(self.profiles_[col]),
            score=None if closest is None else closest[0],
        )

    def _changed_alerts(self, batch: pd.DataFrame) -> list[Alert]:
        """Contract columns present by name whose content no longer matches their profile."""
        profiles = {}
        for col in self.profiles_:
            s = _column(batch, col)
            if s is not None:
                profiles[col] = self._profile_as(_column_content(s), col)
        return [
            self.alert(
                f"column '{col}' is present but its content no longer matches its reference profile "
                f"(similarity {sim:.2f} < {self.threshold_:.2f})",
                check_id="S-a2", columns=[col], observed=_rounded(profiles[col]), expected=_rounded(self.profiles_[col]), score=sim,
            )
            for col, sim in self._judged_similarities(profiles).items()
            if sim < self.threshold_
        ]


# --------------------------------------------------------------------------
# b - column type change
# --------------------------------------------------------------------------
EXPECTED_KIND = {"int64": "integer", "float64": "float", "str": "string"}
PYTHON_TYPES = {"integer": (int, np.integer), "float": (float, np.floating), "string": (str,)}


def _dtype_kind(s: pd.Series) -> str:
    """integer / float / string / empty, or pandas' inferred type for anything else (e.g. 'mixed')."""
    if not s.notna().any():  # all null: the dtype is a loader artefact, not evidence of a type
        return "empty"
    if pd.api.types.is_bool_dtype(s.dtype):
        return "boolean"
    if pd.api.types.is_integer_dtype(s.dtype):
        return "integer"
    if pd.api.types.is_float_dtype(s.dtype):
        return "float"
    inferred = pd.api.types.infer_dtype(s, skipna=True)  # str / object / other: look at the values
    return {"string": "string", "floating": "float", "integer": "integer", "mixed-integer-float": "float"}.get(inferred, inferred)


class RuleDtypeContract(Detector):
    problem = "b"
    family = "rule"
    name = "rule_dtype_contract"
    description = (
        "Compares each present column's dtype kind (integer / float / string) with config.FEED_DTYPES and "
        "reports observed vs expected dtype with example values and their Python types."
    )

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        alerts = []
        for col, contract_dtype in config.FEED_DTYPES.items():
            s = _column(batch, col)
            if s is None:  # a missing column is problem a
                continue
            expected, observed = EXPECTED_KIND[contract_dtype], _dtype_kind(s)
            if observed in (expected, "empty"):
                continue
            present = s.notna().to_numpy()
            # bool subclasses int, yet True is no unique_key: bools are the wrong type for every contract dtype
            right_type = _isinstance_mask(s, PYTHON_TYPES[expected]) & ~_isinstance_mask(s, (bool, np.bool_))
            wrong = present & ~right_type
            examples = (s[wrong] if wrong.any() else s.dropna()).iloc[:3].tolist()
            alerts.append(
                self.alert(
                    f"column '{col}' arrives as {s.dtype} ({observed}); the contract says {contract_dtype}",
                    check_id="R-b1", columns=[col], rows=_example_rows(wrong), n_rows=int(wrong.sum()),
                    observed={"dtype": str(s.dtype), "examples": [f"{v!r} ({type(v).__name__})" for v in examples]},
                    expected=contract_dtype,
                    score=float(wrong.sum() / present.sum()),  # share of values with the wrong Python type
                )
            )
        return self.result(bool(alerts), alerts=alerts)


VALUE_CLASSES = ("number", "int_text", "decimal_text", "text", "other")


def _value_classes(s: pd.Series) -> np.ndarray:
    """Index into VALUE_CLASSES for every value, -1 for nulls. Numeric and str dtypes are
    classified vectorised; object columns are inspected value by value."""
    null = s.isna().to_numpy()
    if pd.api.types.is_numeric_dtype(s.dtype) and not pd.api.types.is_bool_dtype(s.dtype):
        is_text, is_number = np.zeros(len(s), bool), ~null
    elif isinstance(s.dtype, pd.StringDtype):
        is_text, is_number = ~null, np.zeros(len(s), bool)
    else:
        is_text = _isinstance_mask(s, str)
        is_number = ~null & _isinstance_mask(s, (int, float, np.integer, np.floating)) & ~_isinstance_mask(s, (bool, np.bool_))
    text = s[is_text].astype("str")
    int_like, number_like = np.zeros(len(s), bool), np.zeros(len(s), bool)
    int_like[is_text] = text.str.fullmatch(INT_PATTERN).to_numpy(dtype=bool)
    number_like[is_text] = text.str.fullmatch(NUMBER_PATTERN).to_numpy(dtype=bool)
    # The first true condition wins (so int-like text is never decimal); unmatched = 4 "other".
    return np.select([null, is_number, int_like, number_like, is_text], [-1, 0, 1, 2, 3], default=4)


def _type_mix(classes: np.ndarray) -> np.ndarray | None:
    """Share of non-null values per VALUE_CLASSES entry; None when every value is null."""
    present = classes[classes >= 0]
    if present.size == 0:
        return None
    return np.bincount(present, minlength=len(VALUE_CLASSES)) / present.size


def _mix_dict(mix: np.ndarray) -> dict[str, float]:
    return {c: round(float(share), 4) for c, share in zip(VALUE_CLASSES, mix) if share > 0}


class StatValueTypeProfile(Detector):
    problem = "b"
    family = "stat"
    name = "stat_value_type_profile"
    description = (
        "Learns per column the share of non-null values that are numbers, integer-like text, decimal-like text, "
        "other text or other objects; flags a column whose mix moves further from the reference than reference "
        "batches do (catches partial and value-level type drift)."
    )

    def fit(self, reference: pd.DataFrame) -> "StatValueTypeProfile":
        batches = _reference_batches(reference)
        columns = [c for c in config.COLUMNS if c in reference.columns]
        mixes = [{c: _type_mix(_value_classes(b[c])) for c in columns} for b in batches]  # None: all null
        self.reference_mix_ = {}
        for col in columns:
            seen = [m[col] for m in mixes if m[col] is not None]
            if seen:
                self.reference_mix_[col] = np.median(seen, axis=0)
        # A batch alerts when its most-shifted column moves further than the threshold, so
        # calibrate on each reference batch's most-shifted column.
        worst = [
            max((np.abs(m[c] - ref).max() for c, ref in self.reference_mix_.items() if m[c] is not None), default=0.0)
            for m in mixes
        ]
        self.threshold_ = max(_alert_threshold(worst, upper=True), TYPE_SHIFT_MIN)
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        alerts = []
        for col, reference_mix in self.reference_mix_.items():
            s = _column(batch, col)
            if s is None:  # a missing column is problem a
                continue
            classes = _value_classes(s)
            mix = _type_mix(classes)
            if mix is not None and np.abs(mix - reference_mix).max() > self.threshold_:
                alerts.append(self._shift_alert(col, mix, classes))
        return self.result(bool(alerts), alerts=alerts)

    def _shift_alert(self, col: str, mix: np.ndarray, classes: np.ndarray) -> Alert:
        reference_mix = self.reference_mix_[col]
        grown = int(np.argmax(mix - reference_mix))  # the value class that took over
        rows = classes == grown
        return self.alert(
            f"column '{col}': {VALUE_CLASSES[grown]} values went from {reference_mix[grown]:.1%} to "
            f"{mix[grown]:.1%} of non-null values (tolerance {self.threshold_:.1%})",
            check_id="S-b1", columns=[col], rows=_example_rows(rows), n_rows=int(rows.sum()),
            observed=_mix_dict(mix), expected=_mix_dict(reference_mix), score=float(np.abs(mix - reference_mix).max()),
        )


# --------------------------------------------------------------------------
# c - date format switch partway through a batch
# --------------------------------------------------------------------------
def _parse_source(text: pd.Series) -> pd.Series:
    return pd.to_datetime(text, format=config.SOURCE_DATETIME_FORMAT, errors="coerce")


def _nonconforming(text: pd.Series) -> np.ndarray:
    """Non-null values that do not match the source ISO pattern or do not parse with it
    (e.g. month 16 after a day/month swap)."""
    matches = text.str.fullmatch(config.SOURCE_DATETIME_REGEX).to_numpy(dtype=bool)
    parses = _parse_source(text).notna().to_numpy()
    return text.notna().to_numpy() & ~(matches & parses)


def _guess_format(values: pd.Series) -> str:
    sample = values.iloc[:FORMAT_GUESS_SAMPLE]
    for fmt in CANDIDATE_DATE_FORMATS:
        if pd.to_datetime(sample, format=fmt, errors="coerce").notna().mean() >= FORMAT_GUESS_MIN_SHARE:
            return fmt
    return "an unrecognised format"


class RuleDatetimeFormat(Detector):
    problem = "c"
    family = "rule"
    name = "rule_datetime_format"
    description = (
        "Every non-null value of each date column must match SOURCE_DATETIME_REGEX and parse with "
        "SOURCE_DATETIME_FORMAT; flags non-conforming rows and reports first position, count, example and a "
        "guessed format."
    )

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        row_flags = np.zeros(len(batch), bool)
        reasons: dict[int, str] = {}
        alerts = []
        for col in config.DATETIME_COLUMNS:
            s = _column(batch, col)
            if s is None:  # a missing column is problem a
                continue
            text = s.astype("str")
            bad = _nonconforming(text)
            if not bad.any():
                continue
            positions, values = np.flatnonzero(bad), text[bad]
            guess = _guess_format(values)
            alerts.append(
                self.alert(
                    f"{col}: {len(positions):,} values do not match {CONTRACT_DATE_LAYOUT} "
                    f"(first at row {positions[0]}: {values.iloc[0]!r}, looks like {guess})",
                    check_id="R-c1", columns=[col], rows=positions[:MAX_EXAMPLE_ROWS].tolist(), n_rows=len(positions),
                    segment={"first_position": int(positions[0]), "guessed_format": guess},
                    observed=values.iloc[0], expected=CONTRACT_DATE_LAYOUT, score=float(bad.mean()),
                )
            )
            for pos, value in zip(positions.tolist(), values.tolist()):
                reasons.setdefault(pos, f"{col}={value!r} does not match {CONTRACT_DATE_LAYOUT} (looks like {guess})")
            row_flags |= bad
        return self.result(bool(alerts), alerts=alerts, row_flags=row_flags, row_reasons=reasons)


def _shape(text: pd.Series) -> pd.Series:
    """'2023-10-16T08:00:16.000' -> '9999-99-99A99:99:99.999' (digit -> 9, letter -> A)."""
    return text.str.translate(SHAPE_TABLE)


def _common_shapes(shares: pd.Series) -> set[str]:
    """Reference shapes minus the rarest ones that together cover at most TARGET_ROW_FPR
    of the reference values (so at most that share of reference rows would be flagged)."""
    ascending = shares.sort_values()
    return set(ascending.index[ascending.cumsum() > config.TARGET_ROW_FPR])


def _segment_offsets(when: pd.Series) -> np.ndarray:
    """Days between each row's arrival-order segment and the batch's opening segment
    (NaN where the date did not parse).

    In arrival order consecutive created_dates are seconds or minutes apart, so a jump of
    more than COHERENCE_DAYS starts a new segment. Comparing whole segments with the
    opening one flags an entire shifted block; a rolling median would follow the block as
    soon as it is longer than the window. The opening rows are trusted (a format switch
    happens partway through): a lone bad date in row 0 would put every other row off.
    """
    days = ((when - when.min()).dt.total_seconds() / 86_400).to_numpy()
    valid = ~np.isnan(days)
    offsets = np.full(len(days), np.nan)
    if valid.any():
        d = days[valid]
        segment = np.concatenate([[0], np.cumsum(np.abs(np.diff(d)) > COHERENCE_DAYS)])
        medians = pd.Series(d).groupby(segment).transform("median").to_numpy()
        offsets[valid] = medians - medians[0]
    return offsets


class StatDatetimeShapeShift(Detector):
    problem = "c"
    family = "stat"
    name = "stat_datetime_shape_shift"
    description = (
        "Flags date values whose character shape (digit->9, letter->A) is rare in the reference or whose usual "
        "shape hides an impossible date, and created_dates more than 2 days off the batch's arrival-order "
        "timeline (day/month swaps keep the ISO shape); the batch alerts when the flagged share exceeds a "
        "calibrated threshold."
    )

    def fit(self, reference: pd.DataFrame) -> "StatDatetimeShapeShift":
        batches = _reference_batches(reference)
        self.shape_shares_ = {}
        for col in config.DATETIME_COLUMNS:
            if col not in reference.columns:
                continue
            values = pd.concat([b[col] for b in batches]).dropna().astype("str")
            if len(values):
                self.shape_shares_[col] = _shape(values).value_counts(normalize=True)
        self.common_shapes_ = {c: _common_shapes(s) for c, s in self.shape_shares_.items()}
        flagged_share = [self._evidence(b)[2].mean() for b in batches]
        self.threshold_ = _alert_threshold(flagged_share, upper=True)
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        odd, timeline, flags = self._evidence(batch)
        if len(batch) == 0 or flags.mean() <= self.threshold_:  # reference-level noise: no alert, no rows
            return self.result(False, row_flags=np.zeros(len(batch), bool))
        alerts, reasons = [], {}
        for col, (rare, impossible, text) in odd.items():
            alerts.append(self._value_alert(col, rare, impossible, text))
            for pos, value in zip(np.flatnonzero(rare).tolist(), text[rare].tolist()):
                shape = value.translate(SHAPE_TABLE)
                reasons.setdefault(pos, f"{col}={value!r} has shape {shape}, rare or unseen in the reference")
            for pos, value in zip(np.flatnonzero(impossible).tolist(), text[impossible].tolist()):
                reasons.setdefault(pos, f"{col}={value!r} has the usual shape but is not a real date")
        if timeline is not None and timeline[0].any():
            off, offsets, created = timeline
            alerts.append(self._timeline_alert(off, offsets, created))
            for pos, value, offset in zip(np.flatnonzero(off).tolist(), created[off].tolist(), offsets[off].tolist()):
                reasons.setdefault(pos, f"created_date={value!r} sits {offset:+.1f} days off the batch timeline")
        return self.result(True, alerts=alerts, row_flags=flags, row_reasons=reasons)

    def _evidence(self, batch: pd.DataFrame) -> tuple[dict, tuple | None, np.ndarray]:
        """(odd values per date column, timeline check, union of the rows either flags)."""
        odd, timeline = self._odd_values(batch), self._off_timeline(batch)
        flags = np.zeros(len(batch), bool)
        for rare, impossible, _ in odd.values():
            flags |= rare | impossible
        if timeline is not None:
            flags |= timeline[0]
        return odd, timeline, flags

    def _odd_values(self, batch: pd.DataFrame) -> dict[str, tuple[np.ndarray, np.ndarray, pd.Series]]:
        """Per date column with any odd value: (rare-shape mask, impossible-date mask, values as
        text). An impossible date has the usual shape but no calendar date (month 16 after a
        day/month swap)."""
        odd = {}
        for col, common in self.common_shapes_.items():
            s = _column(batch, col)
            if s is None:  # a missing column is problem a
                continue
            text = s.astype("str")
            present = text.notna().to_numpy()
            usual = _shape(text).isin(common).to_numpy()
            rare = present & ~usual
            impossible = present & usual & _parse_source(text).isna().to_numpy()
            if rare.any() or impossible.any():
                odd[col] = (rare, impossible, text)
        return odd

    def _off_timeline(self, batch: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, pd.Series] | None:
        """(mask, offset in days, created_date as text) for created_dates that parse but sit more
        than COHERENCE_DAYS off the batch's opening timeline; None without a created_date column."""
        s = _column(batch, "created_date")
        if s is None:  # a missing column is problem a
            return None
        created = s.astype("str")
        offsets = _segment_offsets(_parse_source(created))
        return np.abs(offsets) > COHERENCE_DAYS, offsets, created  # unparsed rows (NaN) compare False

    def _value_alert(self, col: str, rare: np.ndarray, impossible: np.ndarray, text: pd.Series) -> Alert:
        flagged = rare | impossible
        first = int(np.flatnonzero(flagged)[0])
        usual = self.shape_shares_[col].index[0]
        counts = []
        if rare.any():
            counts.append(f"{rare.sum():,} values have a date shape rare or unseen in the reference")
        if impossible.any():
            counts.append(f"{impossible.sum():,} values have the usual shape but are not real dates")
        return self.alert(
            f"{col}: {' and '.join(counts)} (first at row {first}: {text.iloc[first]!r}; usual shape {usual})",
            check_id="S-c1", columns=[col], rows=_example_rows(flagged), n_rows=int(flagged.sum()),
            segment={"first_position": first},
            observed={"value": text.iloc[first], "shape": text.iloc[first].translate(SHAPE_TABLE)},
            expected={"shape": usual, "reference_share": round(float(self.shape_shares_[col].iloc[0]), 4)},
            score=float(flagged.mean()),
        )

    def _timeline_alert(self, off: np.ndarray, offsets: np.ndarray, created: pd.Series) -> Alert:
        first = int(np.flatnonzero(off)[0])
        opening = created.iloc[int(np.flatnonzero(~np.isnan(offsets))[0])]
        return self.alert(
            f"created_date: {off.sum():,} rows are more than {COHERENCE_DAYS:g} days off the batch's "
            f"arrival-order timeline (first at row {first}: {created.iloc[first]!r}, {offsets[first]:+.1f} days)",
            check_id="S-c2", columns=["created_date"], rows=_example_rows(off), n_rows=int(off.sum()),
            segment={"first_position": first},
            observed={"value": created.iloc[first], "offset_days": round(float(offsets[first]), 1)},
            expected=f"within {COHERENCE_DAYS:g} days of the opening rows (first: {opening!r})",
            score=float(off.mean()),
        )


# --------------------------------------------------------------------------
# LLM reviewers (one call per batch on a deterministic sample)
# --------------------------------------------------------------------------
def _float_or_none(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _reviewed_positions(ids, reviewed: np.ndarray) -> list[int]:
    """Row ids from the model that are real, reviewed positions; anything else is ignored."""
    positions = set()
    for i in ids if isinstance(ids, list) else []:
        try:
            pos = int(i)
        except (TypeError, ValueError):
            continue
        if 0 <= pos < len(reviewed) and reviewed[pos]:
            positions.add(pos)
    return sorted(positions)


def _short_repr(value) -> str:
    text = repr(value)
    return text if len(text) <= LLM_VALUE_MAX_CHARS else text[: LLM_VALUE_MAX_CHARS - 3] + "..."


def _schema_payload(batch: pd.DataFrame) -> str:
    """Contract vs received header with dtypes, plus evenly spaced sample values per column (repr)."""
    positions = _evenly_spaced(len(batch), LLM_SCHEMA_SAMPLE_ROWS)
    contract = "\n".join(f"  {col}: {dtype}" for col, dtype in config.FEED_DTYPES.items())
    received = "\n".join(
        f"  {col}: {batch.dtypes.iloc[i]} | " + " | ".join(_short_repr(batch.iloc[p, i]) for p in positions)
        for i, col in enumerate(batch.columns)
    )
    rows = ", ".join(map(str, positions)) or "none (empty batch)"
    return (
        f"Contract (column: dtype):\n{contract}\n\n"
        f"Batch as received ({len(batch):,} rows). Each line: column: dtype | values at rows {rows} "
        f"(Python repr, values longer than {LLM_VALUE_MAX_CHARS} characters are cut):\n{received}"
    )


def _cell(s: pd.Series | None, pos: int) -> str:
    if s is None:
        return "(column missing)"
    value = s.iloc[pos]
    # pd.isna of a list or array cell is an array, whose truth value would raise
    return "null" if pd.api.types.is_scalar(value) and pd.isna(value) else str(value)


class _LlmBatchReview(Detector):
    """One review_batch call per batch: build a payload, ask, map the verdict to a result."""

    family = "llm"
    task: ClassVar[str] = ""
    check_id: ClassVar[str] = ""

    def payload(self, batch: pd.DataFrame) -> tuple[str | None, np.ndarray | None]:
        """(text the model sees or None if there is nothing to review, reviewed-row mask)."""
        raise NotImplementedError

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        text, reviewed = self.payload(batch)  # batch-sample review: review_rows is ignored
        if text is None:
            return self.result(False, reviewed=reviewed)
        try:
            answer, usage = llm.review_batch(self.task, text, tag=f"{self.problem}/{self.name}")
        except llm.LLMUnavailable as exc:
            return self.result(False, error=f"llm_unavailable: {exc}", reviewed=reviewed)
        if answer is None:  # never read an unparseable answer as "no issue"
            return self.result(False, error="llm_bad_reply: no JSON verdict", reviewed=reviewed, usage=usage)
        result = self.verdict(batch, answer, reviewed)
        result.usage.add(usage)
        return result

    def verdict(self, batch: pd.DataFrame, answer: dict, reviewed: np.ndarray | None) -> DetectionResult:
        flagged = answer.get("issue_found") is True
        return self.result(flagged, alerts=[self._alert(answer)] if flagged else [], reviewed=reviewed)

    def _alert(self, answer: dict, rows: list[int] | None = None) -> Alert:
        rows = rows or []
        columns, segment = answer.get("columns"), answer.get("segment")
        return self.alert(
            str(answer.get("reason") or "the model reported this problem"),
            check_id=self.check_id,
            columns=[str(c) for c in columns] if isinstance(columns, list) else [],
            rows=rows[:MAX_EXAMPLE_ROWS], n_rows=len(rows),
            segment={"model": str(segment)} if segment else {},
            observed=answer.get("observed"), expected=answer.get("expected"),
            score=_float_or_none(answer.get("confidence")),
        )


class LlmSchemaReview(_LlmBatchReview):
    problem = "a"
    name = "llm_schema_review"
    check_id = "L-a1"
    task = TASK_RENAMED_OR_DROPPED
    description = (
        "Shows the model the contract (names + dtypes), the received header + dtypes and 3 sample rows; "
        "asks whether a column was renamed or dropped."
    )

    def payload(self, batch: pd.DataFrame) -> tuple[str, None]:
        return _schema_payload(batch), None  # the verdict is about the header, i.e. the whole batch


class LlmTypeReview(_LlmBatchReview):
    problem = "b"
    name = "llm_type_review"
    check_id = "L-b1"
    task = TASK_TYPE_CHANGE
    description = (
        "Shows the model contract dtypes vs received dtypes with 3 sample rows in Python repr; "
        "asks whether a column's type changed."
    )

    def payload(self, batch: pd.DataFrame) -> tuple[str, None]:
        return _schema_payload(batch), None


class LlmDatetimeReview(_LlmBatchReview):
    problem = "c"
    name = "llm_datetime_review"
    check_id = "L-c1"
    task = TASK_DATE_FORMAT_SWITCH
    description = (
        "Shows the model 40 evenly spaced rows in arrival order (position, created_date, closed_date); "
        "asks whether and where the date format changes."
    )

    def payload(self, batch: pd.DataFrame) -> tuple[str | None, np.ndarray]:
        created, closed = _column(batch, "created_date"), _column(batch, "closed_date")
        positions = _evenly_spaced(len(batch), config.LLM_BATCH_SAMPLE_ROWS)
        reviewed = np.zeros(len(batch), bool)
        if (created is None and closed is None) or positions.size == 0:
            return None, reviewed
        reviewed[positions] = True
        lines = [f"{p} | {_cell(created, p)} | {_cell(closed, p)}" for p in positions]
        return "position | created_date | closed_date\n" + "\n".join(lines), reviewed

    def verdict(self, batch: pd.DataFrame, answer: dict, reviewed: np.ndarray) -> DetectionResult:
        flagged = answer.get("issue_found") is True
        rows = _reviewed_positions(answer.get("row_ids"), reviewed) if flagged else []
        row_flags = np.zeros(len(batch), bool)
        row_flags[rows] = True
        reason = str(answer.get("reason") or "")
        # One verdict per batch, so every flagged row carries the same evidence; the prompt asks
        # for all affected values in `observed`, which lets the harness check it quotes each row.
        evidence = {
            "observed": answer.get("observed"),
            "expected": answer.get("expected"),
            "confidence": _float_or_none(answer.get("confidence")),
        }
        return self.result(
            flagged,
            alerts=[self._alert(answer, rows)] if flagged else [],
            row_flags=row_flags,
            row_reasons={r: reason for r in rows},
            row_evidence={r: dict(evidence) for r in rows},
            reviewed=reviewed,
        )


DETECTORS = [
    RuleSchemaColumns,
    StatColumnProfileMatch,
    LlmSchemaReview,
    RuleDtypeContract,
    StatValueTypeProfile,
    LlmTypeReview,
    RuleDatetimeFormat,
    StatDatetimeShapeShift,
    LlmDatetimeReview,
]
