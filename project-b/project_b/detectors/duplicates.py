"""Duplicate detectors: problem e (exact duplicates) and f (near-duplicates).

An exact duplicate (e) is the same record delivered twice. A near-duplicate (f) is the same
real-world request entered twice: a new unique_key, a created_date a few minutes later and maybe
small text edits. Every detector flags the LATER row of a pair (keep="first"), never the original,
and the f detectors never flag a row that repeats an earlier unique_key: that is an exact
duplicate (problem e), not a second entry of the same request.

Why these designs
* rule_exact_duplicate_rows - `duplicated(keep="first")` over every column IS the definition and
  costs milliseconds (R-e1). A unique_key that comes back with different values is not an exact
  copy, but a dashboard would double-count it just the same, so it is reported separately (R-e2).
* stat_bloom_duplicate - an exact check must remember every row it has seen: free inside one
  5,000-row batch, not for deduplicating a year of feed (3.2 M rows) ACROSS batches. A Bloom
  filter answers "seen before?" in fixed memory - 19.2 bits per row at a 1e-4 false-positive
  probability, ~7.7 MB for the whole year - with no false negatives and a false-positive rate
  that is known in advance (and measured in fit()). The evaluation batches are independent
  samples and the clean batches are re-used for every problem, so here the filter is reset per
  batch; in production one BloomFilter instance would live across batches.
* rule_near_duplicate_key - same (agency, complaint_type, descriptor, normalised location,
  borough) within 300 s is the project's definition of a near-duplicate (R-f1). The rule
  normalises what a rule can normalise deterministically (case, spaces, standard street
  abbreviations); a typo changes the key, so the rule misses that copy.
* stat_near_duplicate_similarity - scores every pair of rows with the same agency, complaint
  type and borough at most 600 s apart: a weighted sum of address character-3-gram Jaccard,
  descriptor, location_type and house-number agreement, closeness of the coordinates and
  "created within the 300 s horizon". A typo only lowers one term, so an edited copy still scores
  high. The threshold is the (1 - TARGET_ROW_FPR) quantile of row scores on contiguous reference
  batches: data, not a hand-set number, decides how many false alarms we accept. On real
  sanitised Feb/May/Aug windows it lands at 0.80 - exactly the score of "same building, other
  descriptor" - so another descriptor or a gap beyond 300 s never passes. The weights below were
  first chosen while looking at October/November windows (evaluation period); an audit re-ran the
  same comparison on held-out Jan-Aug windows only (Mar/Jun/Jul, days 1-11) and it selects the
  same weights: typo-copy recall 0.952, easy-edit recall 1.000, clean-row false-positive rate
  0.058 %. The closeness term matters: on the October test fixture, typo copies moved 10 m away
  are caught 12 % of the time instead of 92 % (test_stat_typo_recall_rests_on_unchanged_coordinates),
  which is why the injector now moves typo-edited copies a few metres, as a re-geocoded address would.
* llm_duplicate_review / llm_near_duplicate_review - window review (DESIGN.md "LLM detectors"):
  the model sees a run of consecutive rows and says which rows repeat an earlier row of the run.

Real near-duplicates the sanitiser keeps: from 2023-09-28 to 2023-10-06 HPD entered most requests
twice, in Title Case ("Unsanitary Condition" / "Mold") and ~100 s later in upper case under a new
unique_key. Both f detectors flag the second entry (6-7 % of the rows of early-October batches).
clean.py's near-duplicate key ignores case, so these re-entries are removed from the clean pool
(and reported by the profiler as a real issue) rather than charged as false alarms.

Columns missing from a batch (renamed or dropped, problem a) are never read as agreement: R-f1
needs its whole key and flags nothing without it (a dropped descriptor column made 3.6 % of a
sanitised batch look like re-entries), the similarity scores a missing descriptor / location_type
column as "different", and an alert names the columns it could not read. A batch without any
column has nothing to compare, so the e detectors flag nothing in it.
"""
from __future__ import annotations

import hashlib
import math
from typing import ClassVar

import numpy as np
import pandas as pd

from project_b import config, llm
from project_b.core import PROBLEMS, Detector, DetectionResult, Usage

EXAMPLE_ROWS = 20  # example row positions per alert (DESIGN.md: <= 20)

# --- e: Bloom filter -------------------------------------------------------------------------
BLOOM_FALSE_POSITIVE_RATE = 1e-4  # design false-positive probability once the filter is full
BLOOM_FIT_BATCHES = 20  # reference batches on which fit() measures the real false-positive rate
FIELD_SEPARATOR = "\x1f"  # ASCII "unit separator": joins values into one row text
NULL_TOKEN = "\x00"  # stands for a missing value in the row text
MAX_HASHES = 16  # one 64-byte blake2b digest = 16 independent 32-bit hash values

# --- f: shared by rule and statistics ---------------------------------------------------------
STREET_ABBREVIATIONS = {  # USPS standard forms; both spellings occur in 311 addresses
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "BOULEVARD": "BLVD", "PLACE": "PL",
    "DRIVE": "DR", "PARKWAY": "PKWY", "EAST": "E", "WEST": "W", "NORTH": "N", "SOUTH": "S",
    # Truncated forms found in the Jan-Aug 2023 clean pool (as the last word of 15 and 8 addresses,
    # e.g. "9718 FORT HAMILTON PKW"); without them "3708 FORT HAMILTON PKW" and "... PARKWAY" were
    # two locations to R-f1. No other truncation of these street types occurs there.
    "PKW": "PKWY", "AV": "AVE",
}
ABBREVIATION_PATTERN = r"\b(" + "|".join(STREET_ABBREVIATIONS) + r")\b"
COORD_DECIMALS = 4  # rows without an address are located by lat/lon rounded to ~10 m
MAX_PAIRS_PER_ROW = 100  # compare a row with at most its 100 predecessors in the block (bounds cost)

# --- f: rule ----------------------------------------------------------------------------------
NEAR_DUPLICATE_MAX_GAP_S = 300  # a second entry of the same request arrives within 5 minutes
REQUEST_KEY_COLUMNS = ["agency", "complaint_type", "descriptor", "borough"]  # R-f1 key, besides the location
RULE_F_COLUMNS = [*REQUEST_KEY_COLUMNS, "incident_address", "latitude", "longitude", "created_date", "unique_key"]

# --- f: statistical similarity ----------------------------------------------------------------
CANDIDATE_WINDOW_S = 600  # compare rows of the same agency, complaint type and borough <= 10 min apart
SIMILARITY_WEIGHTS = {  # sum to 1, so the score is in [0, 1]; a text empty in both rows agrees
    "address": 0.3,  # character-3-gram Jaccard of the location text: a typo keeps most of it
    "descriptor": 0.2,  # same descriptor (case-insensitive)
    "location_type": 0.05,  # same location type
    "house_number": 0.05,  # same building number: weak, a typo there looks like the next building
    "closeness": 0.15,  # exp(-distance / CLOSENESS_SCALE_M): a re-entry keeps the point
    "within_horizon": 0.25,  # created <= NEAR_DUPLICATE_MAX_GAP_S apart (the definition's horizon)
}
HOUSE_NUMBER_PATTERN = r"^(\d[\d-]*[A-Z]?) "  # "146-19 HORACE ...", "105A HULL ST"
# Two rows whose addresses both carry a house number and whose numbers differ (digits and hyphens;
# a letter suffix may be a typo) are different buildings, so never the same request (DESIGN section
# 2). Without this, consecutive reports along one block (139-10, 139-12, ... 97 AVENUE) that share
# one block-level geocode scored 0.82 on the other terms alone. The injector's typo edits letters
# only, so a near-duplicate copy always keeps its original's house number digits.
DIFFERENT_BUILDING_IS_NOT_A_COPY = True
CLOSENESS_SCALE_M = 5  # 1 at the same point; ~0 for the next building (reference median: 44 m)
M_PER_DEG_LAT, M_PER_DEG_LON = 111_000, 84_000  # metres per degree at New York's latitude
SIMILARITY_FIT_BATCHES = 40  # reference batches scored in fit(): 200k rows keep fit() at ~10 s
STAT_F_COLUMNS = [
    "agency", "complaint_type", "borough", "incident_address", "descriptor", "location_type",
    "latitude", "longitude", "created_date", "unique_key",
]

# --- LLM window review: the only columns the model sees (cost) --------------------------------
LLM_E_COLUMNS = ["unique_key", "created_date", "agency", "complaint_type", "descriptor", "incident_address", "status"]
LLM_F_COLUMNS = [
    "unique_key", "created_date", "agency", "complaint_type", "descriptor",
    "location_type", "incident_address", "borough", "latitude", "longitude",
]


# ==============================================================================================
# helpers
# ==============================================================================================
def _column(batch: pd.DataFrame, name: str) -> pd.Series:
    """Column `name` (the first one if the name is duplicated), all-null if it is missing."""
    if name not in batch.columns:
        return pd.Series(np.nan, index=batch.index, dtype=object)
    column = batch.loc[:, name]
    return column.iloc[:, 0] if isinstance(column, pd.DataFrame) else column


def _hashable(value):
    """Lists / dicts (also inside a tuple) cannot be grouped on; compare them by their text."""
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)


def _first_occurrence(frame: pd.DataFrame) -> np.ndarray:
    """For every row, the position of the first row with exactly the same values (its own
    position if it is the first). Missing values compare equal, like `duplicated()`."""
    if frame.shape[1] == 0:
        return np.arange(len(frame))  # no columns, no values to compare: no row repeats another
    frame = frame.set_axis(range(frame.shape[1]), axis=1)  # positional labels: duplicate names are fine
    for j in frame.columns:
        if pd.api.types.is_object_dtype(frame[j]):
            frame[j] = frame[j].map(_hashable)
    group = frame.groupby(list(frame.columns), dropna=False, sort=False).ngroup().to_numpy()
    return pd.Series(np.arange(len(frame))).groupby(group).transform("min").to_numpy()


def _cell_texts(batch: pd.DataFrame) -> list[list[str]]:
    """Every value as text, one list per column (missing -> NULL_TOKEN)."""
    return [batch.iloc[:, j].astype("str").fillna(NULL_TOKEN).tolist() for j in range(batch.shape[1])]


def _row_texts(batch: pd.DataFrame) -> list[str]:
    """The canonical text of each row: all its values in column order, joined by FIELD_SEPARATOR
    (a batch without columns has no texts: callers check that first)."""
    return [FIELD_SEPARATOR.join(values) for values in zip(*_cell_texts(batch))]


def _repeats_earlier_record(batch: pd.DataFrame) -> np.ndarray:
    """True where a row re-delivers an earlier record: same unique_key or, without that column,
    identical in every column. Those rows are exact duplicates (problem e), never f."""
    if "unique_key" not in batch.columns:
        return _first_occurrence(batch) < np.arange(len(batch))
    key = _column(batch, "unique_key")
    return (_first_occurrence(key.to_frame()) < np.arange(len(batch))) & key.notna().to_numpy()


def _normalise_text(values: pd.Series) -> pd.Series:
    """Upper case, single spaces, no surrounding blanks; missing -> ''."""
    return values.astype("str").str.upper().str.replace(r"\s+", " ", regex=True).str.strip().fillna("")


def _location(batch: pd.DataFrame) -> pd.Series:
    """Normalised address (standard street abbreviations), else lat/lon rounded to ~10 m, else ''."""
    address = _normalise_text(_column(batch, "incident_address"))
    address = address.str.replace(ABBREVIATION_PATTERN, lambda m: STREET_ABBREVIATIONS[m.group(0)], regex=True)
    lat, lon = (pd.to_numeric(_column(batch, c), errors="coerce").round(COORD_DECIMALS) for c in ("latitude", "longitude"))
    coordinates = (lat.astype("str") + "," + lon.astype("str")).fillna("")
    return address.where(address != "", coordinates)


def _created_seconds(batch: pd.DataFrame) -> np.ndarray:
    """created_date in seconds; NaN where it is missing or not in the source format (such rows
    cannot be placed in time, so they are never compared). A column that already holds parsed
    (time-zone-free) timestamps is used as it is."""
    created = _column(batch, "created_date")
    if not pd.api.types.is_datetime64_dtype(created):
        created = pd.to_datetime(created.astype("str"), format=config.SOURCE_DATETIME_FORMAT, errors="coerce")
    return ((created - pd.Timestamp(0)) / pd.Timedelta(seconds=1)).to_numpy(dtype=float)


def _window_pairs(block: np.ndarray, seconds: np.ndarray, window_s: float) -> pd.DataFrame:
    """Every (earlier, later) pair of rows with the same block code whose created times are at
    most window_s apart. Rows with a NaN time are left out; 'earlier' = older (ties: lower position)."""
    rows = np.flatnonzero(~np.isnan(seconds))
    rows = rows[np.lexsort((seconds[rows], block[rows]))]  # by block, then time (stable: ties keep position order)
    earlier_found, later_found = [np.empty(0, dtype=int)], [np.empty(0, dtype=int)]
    for lag in range(1, min(len(rows), MAX_PAIRS_PER_ROW + 1)):  # each row vs the row `lag` places before it
        earlier, later = rows[:-lag], rows[lag:]
        inside = (block[earlier] == block[later]) & (seconds[later] - seconds[earlier] <= window_s)
        if not inside.any():  # sorted by block and time, so larger lags cannot be inside either
            break
        earlier_found.append(earlier[inside])
        later_found.append(later[inside])
    earlier, later = np.concatenate(earlier_found), np.concatenate(later_found)
    return pd.DataFrame({"earlier": earlier, "later": later, "gap_s": seconds[later] - seconds[earlier]})


def _agree(values: pd.Series, earlier: np.ndarray, later: np.ndarray) -> np.ndarray:
    """values[earlier] == values[later] for every pair."""
    values = values.to_numpy()
    return values[earlier] == values[later]


def _column_agrees(batch: pd.DataFrame, name: str, earlier: np.ndarray, later: np.ndarray) -> np.ndarray:
    """Same text in column `name` (case and spacing aside; empty in both rows counts as the same).
    A column the batch lacks agrees nowhere: unknown is not a match - a renamed descriptor column
    must not make every pair "same descriptor"."""
    if name not in batch.columns:
        return np.zeros(len(earlier), dtype=bool)
    return _agree(_normalise_text(_column(batch, name)), earlier, later)


def _missing_note(batch: pd.DataFrame, columns: list[str]) -> str:
    """'' or ' (not in the batch: a, b)' for the columns a detector reads that the batch lacks."""
    missing = [c for c in columns if c not in batch.columns]
    return f" (not in the batch: {', '.join(missing)})" if missing else ""


def _trigram_jaccard(texts: pd.Series, earlier: np.ndarray, later: np.ndarray) -> np.ndarray:
    """|A & B| / |A | B| of the sets of 3-character substrings of texts[earlier] and texts[later]
    (0 when both are empty). A one-character typo changes at most 3 of the substrings."""
    grams = [{text[i : i + 3] for i in range(len(text) - 2)} for text in texts.tolist()]
    return np.array([len(grams[a] & grams[b]) / max(len(grams[a] | grams[b]), 1) for a, b in zip(earlier, later)])


def _reference_batches(reference: pd.DataFrame, max_batches: int) -> list[pd.DataFrame]:
    """Up to max_batches contiguous BATCH_SIZE-row windows spread evenly over the reference
    (a subsample keeps fit() fast on the 2.1 M-row reference)."""
    starts = np.arange(0, max(len(reference) - config.BATCH_SIZE, 0) + 1, config.BATCH_SIZE)
    chosen = np.unique(np.linspace(0, len(starts) - 1, max_batches).round().astype(int))
    return [reference.iloc[s : s + config.BATCH_SIZE].reset_index(drop=True) for s in starts[chosen]]


def _examples(flags: np.ndarray) -> list[int]:
    return np.flatnonzero(flags)[:EXAMPLE_ROWS].tolist()


# ==============================================================================================
# e - exact duplicates
# ==============================================================================================
class RuleExactDuplicateRows(Detector):
    problem, family, name = "e", "rule", "rule_exact_duplicate_rows"
    description = (
        "Flags rows identical in every column to an earlier row (R-e1) and rows re-using "
        "an earlier unique_key with other values (R-e2)."
    )

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        batch = batch.reset_index(drop=True)
        positions = np.arange(len(batch))
        first_copy = _first_occurrence(batch)
        exact = first_copy < positions
        key = _column(batch, "unique_key")
        first_key = _first_occurrence(key.to_frame())
        reused = (first_key < positions) & key.notna().to_numpy() & ~exact
        reasons = {
            int(p): f"exact copy of row {first_copy[p]}: all {batch.shape[1]} columns identical (unique_key {key[p]})"
            for p in np.flatnonzero(exact)
        }
        if reused.any():
            reasons |= self._reuse_reasons(batch, np.flatnonzero(reused), first_key, key)
        alerts = []
        if exact.any():
            p = np.flatnonzero(exact)[0]
            alerts.append(self.alert(
                f"{exact.sum()} rows are exact copies of an earlier row",
                check_id="R-e1", columns=[str(c) for c in batch.columns], rows=_examples(exact), n_rows=int(exact.sum()),
                observed=f"{exact.sum()} repeated rows, e.g. row {p} = row {first_copy[p]} (unique_key {key[p]})",
                expected="no row identical to an earlier row", score=float(exact.mean()),
            ))
        if reused.any():
            p = np.flatnonzero(reused)[0]
            alerts.append(self.alert(
                f"{reused.sum()} rows re-use the unique_key of an earlier row with different values",
                check_id="R-e2", columns=["unique_key"], rows=_examples(reused), n_rows=int(reused.sum()),
                observed=f"unique_key {key[p]} at rows {first_key[p]} and {p}", expected="each unique_key once per batch",
                score=float(reused.mean()),
            ))
        return self.result(bool(alerts), alerts=alerts, row_flags=exact | reused, row_reasons=reasons)

    @staticmethod
    def _reuse_reasons(batch: pd.DataFrame, rows: np.ndarray, first_key: np.ndarray, key: pd.Series) -> dict[int, str]:
        cells = np.array(_cell_texts(batch), dtype=object)  # [columns, rows]
        differs = cells[:, rows] != cells[:, first_key[rows]]  # which values changed since the first use
        names = np.array([str(c) for c in batch.columns])
        return {
            int(p): f"unique_key {key[p]} already used by row {first_key[p]}; differs in {', '.join(names[d])}"
            for p, d in zip(rows, differs.T)
        }


class BloomFilter:
    """Set membership in fixed memory: answers "probably seen" or "certainly new".

    Sized with the textbook optimum for `capacity` items at false-positive probability p:
    n_bits = -capacity * ln(p) / ln(2)^2 and n_hashes = n_bits / capacity * ln(2)
    (5,000 rows at 1e-4 -> 95,851 bits = 12 KB and 13 hashes). The bits are stored packed,
    8 per byte, which is what keeps a year-long filter at ~7.7 MB.
    """

    def __init__(self, capacity: int, fp_rate: float):
        self.n_bits = math.ceil(-capacity * math.log(fp_rate) / math.log(2) ** 2)
        self.n_hashes = max(1, round(self.n_bits / capacity * math.log(2)))
        if self.n_hashes > MAX_HASHES:
            raise ValueError(f"fp_rate {fp_rate} needs {self.n_hashes} hashes; one blake2b digest gives {MAX_HASHES}")
        self.packed_bits = np.zeros(math.ceil(self.n_bits / 8), dtype=np.uint8)

    def _bit_positions(self, keys: list[str]) -> np.ndarray:
        """[len(keys), n_hashes] bit positions: one blake2b digest per key, cut into 32-bit hashes."""
        digests = b"".join(
            hashlib.blake2b(k.encode("utf-8", "surrogatepass"), digest_size=4 * self.n_hashes).digest() for k in keys
        )
        return np.frombuffer(digests, dtype="<u4").reshape(len(keys), self.n_hashes) % self.n_bits

    def add_all(self, keys: list[str]) -> np.ndarray:
        """Insert keys in order; True where a key was probably inserted before (all its bits were
        already set by earlier keys). Sequential on purpose: each answer depends on every earlier
        insert (5,000 keys take ~40 ms)."""
        bits = np.unpackbits(self.packed_bits, bitorder="little").astype(bool)  # one bool per bit while working
        seen = np.zeros(len(keys), dtype=bool)
        for i, positions in enumerate(self._bit_positions(keys)):
            seen[i] = bits[positions].all()
            bits[positions] = True
        self.packed_bits = np.packbits(bits, bitorder="little")
        return seen


def _bloom_seen(batch: pd.DataFrame, bloom: BloomFilter) -> np.ndarray:
    """Insert the batch's row texts into `bloom` in order: True where a row was probably seen
    before. A batch without columns has no values to compare, so no row repeats another."""
    if batch.shape[1] == 0:
        return np.zeros(len(batch), dtype=bool)
    return bloom.add_all(_row_texts(batch))


class StatBloomDuplicate(Detector):
    problem, family, name = "e", "stat", "stat_bloom_duplicate"
    description = (
        "Streaming dedup: a Bloom filter (blake2b row fingerprints, 1e-4 false-positive probability) "
        "flags rows whose fingerprint was probably seen before."
    )

    def fit(self, reference: pd.DataFrame) -> "StatBloomDuplicate":
        """Nothing to learn - the operating point is the design false-positive probability. fit()
        measures the real false-positive rate on reference batches (reference_fpr_) to check it."""
        false_alarms = rows = 0
        for batch in _reference_batches(reference, BLOOM_FIT_BATCHES):
            flags = _bloom_seen(batch, BloomFilter(max(len(batch), 1), BLOOM_FALSE_POSITIVE_RATE))
            really_repeated = _first_occurrence(batch) < np.arange(len(batch))
            false_alarms += int((flags & ~really_repeated).sum())
            rows += len(batch)
        self.threshold_ = BLOOM_FALSE_POSITIVE_RATE  # design false-positive probability (operating point)
        self.reference_fpr_ = false_alarms / max(rows, 1)
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        batch = batch.reset_index(drop=True)
        bloom = BloomFilter(max(len(batch), 1), BLOOM_FALSE_POSITIVE_RATE)  # reset per batch, see module docstring
        flags = _bloom_seen(batch, bloom)
        reason = f"row fingerprint already in the Bloom filter (all {bloom.n_hashes} bits set): probably a copy of an earlier row"
        alerts = []
        if flags.any():
            alerts.append(self.alert(
                f"{flags.sum()} rows have a fingerprint seen earlier in the batch (Bloom filter)",
                check_id="S-e1", columns=[str(c) for c in batch.columns], rows=_examples(flags), n_rows=int(flags.sum()),
                observed=f"{flags.sum()} of {len(batch)} row fingerprints seen before",
                expected=f"0 (chance false positives <= {BLOOM_FALSE_POSITIVE_RATE * len(batch):.2f} per batch)",
                score=float(flags.mean()),
            ))
        reasons = {int(p): reason for p in np.flatnonzero(flags)}
        return self.result(bool(alerts), alerts=alerts, row_flags=flags, row_reasons=reasons)


# ==============================================================================================
# f - near-duplicates
# ==============================================================================================
def _same_request_pairs(batch: pd.DataFrame) -> pd.DataFrame:
    """R-f1's matches: for every row with the same (agency, complaint type, descriptor, normalised
    location, borough) as a row created at most NEAR_DUPLICATE_MAX_GAP_S before it, the most recent
    such row: columns earlier, later, gap_s, location."""
    location = _location(batch)
    parts = [_normalise_text(_column(batch, c)) for c in REQUEST_KEY_COLUMNS]
    key = location.str.cat(parts, sep=FIELD_SEPARATOR)
    usable = (location != "").to_numpy() & ~_repeats_earlier_record(batch)  # exact re-deliveries are problem e
    seconds = np.where(usable, _created_seconds(batch), np.nan)
    pairs = _window_pairs(pd.factorize(key)[0], seconds, NEAR_DUPLICATE_MAX_GAP_S)
    closest = pairs.loc[pairs.groupby("later")["gap_s"].idxmin()]
    return closest.assign(location=location.to_numpy()[closest["later"].to_numpy()])


def _house_numbers(location: pd.Series) -> np.ndarray:
    """The digits-and-hyphens part of each address's house number ('' without one): "105A" -> "105",
    "139-10" -> "139-10". A letter suffix is dropped because a typo can hit it."""
    house = location.str.extract(HOUSE_NUMBER_PATTERN, expand=False).fillna("")
    return house.str.replace(r"[A-Z]", "", regex=True).to_numpy(dtype=object)


def _similarity_reason(pair, threshold: float) -> str:
    """One flagged row's explanation from its best pair (a row of StatNearDuplicateSimilarity's pairs)."""
    position = "position unknown" if np.isnan(pair.distance_m) else f"{pair.distance_m:.0f} m apart"
    return (
        f"near-copy of row {pair.earlier} ({pair.gap_s:.0f} s earlier): location similarity {pair.address:.2f}, "
        f"house number {'same' if pair.house_number else 'different'}, "
        f"descriptor {'same' if pair.descriptor else 'different'}, {position}; "
        f"score {pair.score:.2f} > threshold {threshold:.2f}"
    )


class RuleNearDuplicateKey(Detector):
    problem, family, name = "f", "rule", "rule_near_duplicate_key"
    description = (
        "Flags a row whose (agency, complaint type, descriptor, normalised location, borough) matches "
        "an earlier row created <= 300 s before under another unique_key (R-f1)."
    )

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        batch = batch.reset_index(drop=True)
        flags = np.zeros(len(batch), dtype=bool)
        if any(c not in batch.columns for c in REQUEST_KEY_COLUMNS):
            # R-f1 cannot be checked: without e.g. the descriptor, two different complaints made at
            # one address minutes apart would look like one request entered twice.
            return self.result(False, row_flags=flags)
        pairs = _same_request_pairs(batch)
        flags[pairs["later"].to_numpy()] = True
        uk = _column(batch, "unique_key")
        reasons = {
            int(r.later): f"same agency, complaint type, descriptor, borough and location ({r.location}) as row "
            f"{r.earlier}, created {r.gap_s:.0f} s earlier; unique_key {uk[r.earlier]} -> {uk[r.later]}"
            for r in pairs.itertuples()
        }
        alerts = []
        if flags.any():
            r = next(pairs.itertuples())
            alerts.append(self.alert(
                f"{flags.sum()} rows repeat an earlier request within {NEAR_DUPLICATE_MAX_GAP_S} s under a new unique_key"
                + _missing_note(batch, RULE_F_COLUMNS),
                check_id="R-f1", rows=_examples(flags), n_rows=int(flags.sum()), columns=list(RULE_F_COLUMNS),
                observed=f"e.g. row {r.later} repeats row {r.earlier} ({r.location}) {r.gap_s:.0f} s later",
                expected=f"no repeated (agency, complaint type, descriptor, location, borough) within {NEAR_DUPLICATE_MAX_GAP_S} s",
                score=float(flags.mean()),
            ))
        return self.result(bool(alerts), alerts=alerts, row_flags=flags, row_reasons=reasons)


class StatNearDuplicateSimilarity(Detector):
    problem, family, name = "f", "stat", "stat_near_duplicate_similarity"
    description = (
        "Fuzzy similarity (address 3-gram Jaccard, house number, descriptor, location type, distance, "
        "time gap) to earlier rows of the same agency, complaint type and borough within 600 s; "
        "threshold calibrated on reference batches."
    )

    def fit(self, reference: pd.DataFrame) -> "StatNearDuplicateSimilarity":
        """Threshold = (1 - TARGET_ROW_FPR) quantile of the row scores of up to
        SIMILARITY_FIT_BATCHES contiguous reference batches (a subsample: ~10 s however long the
        reference is). The reference must be sanitised (DESIGN.md section 2): a real
        near-duplicate left in it counts as a false alarm and lifts the threshold to the
        duplicates' own score."""
        batches = _reference_batches(reference, SIMILARITY_FIT_BATCHES)
        scores = np.concatenate([self._score_rows(batch)[0] for batch in batches])
        # "higher": a real reference score, so at most TARGET_ROW_FPR of the reference rows exceed it
        self.threshold_ = float(np.quantile(scores, 1 - config.TARGET_ROW_FPR, method="higher"))
        self.reference_fpr_ = float(np.mean(scores > self.threshold_))
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        batch = batch.reset_index(drop=True)
        scores, best = self._score_rows(batch)
        flags = scores > self.threshold_
        hits = best[best["score"] > self.threshold_]
        reasons = {int(pair.later): _similarity_reason(pair, self.threshold_) for pair in hits.itertuples()}
        alerts = []
        if flags.any():
            r = next(hits.nlargest(1, "score").itertuples())
            alerts.append(self.alert(
                f"{flags.sum()} rows are near-copies of an earlier row (similarity above the reference threshold)"
                + _missing_note(batch, STAT_F_COLUMNS),
                check_id="S-f1", rows=_examples(flags), n_rows=int(flags.sum()), columns=list(STAT_F_COLUMNS),
                observed=f"{flags.sum()} rows above the threshold; top: row {r.later} vs row {r.earlier}, similarity {r.score:.2f}",
                expected=f"similarity <= {self.threshold_:.3f} (the {1 - config.TARGET_ROW_FPR:.1%} quantile of reference row scores)",
                score=float(r.score),
            ))
        return self.result(bool(alerts), alerts=alerts, row_flags=flags, row_scores=scores, row_reasons=reasons)

    @staticmethod
    def _score_rows(batch: pd.DataFrame) -> tuple[np.ndarray, pd.DataFrame]:
        """(score of each row = similarity to its most similar candidate, 0 without one;
        the best pair of every row that has a candidate, with one column per similarity feature).

        Two choices come from reading the reference data. A typo in the house number makes an
        address look like the next building's, but a copy that keeps its coordinates (as injected
        copies do: see the caveat in the module docstring) sits at the same point while two
        buildings of one street are a median 44 m apart - so closeness carries the weight and
        the house number little (enough that copies without coordinates still pass). Exact repeats
        just past 300 s are common, so the time gap counts only as "within the near-duplicate
        horizon": a linearly decaying recency let them outrank text-edited copies and pinned the
        threshold at their level."""
        location = _location(batch)
        block = _normalise_text(_column(batch, "agency")).str.cat(
            [_normalise_text(_column(batch, c)) for c in ("complaint_type", "borough")], sep=FIELD_SEPARATOR
        )
        seconds = np.where(_repeats_earlier_record(batch), np.nan, _created_seconds(batch))  # exact re-deliveries are e
        pairs = _window_pairs(pd.factorize(block)[0], seconds, CANDIDATE_WINDOW_S)
        earlier, later = pairs["earlier"].to_numpy(), pairs["later"].to_numpy()
        lat, lon = (pd.to_numeric(_column(batch, c), errors="coerce").to_numpy(dtype=float) for c in ("latitude", "longitude"))
        distance_m = np.hypot((lat[later] - lat[earlier]) * M_PER_DEG_LAT, (lon[later] - lon[earlier]) * M_PER_DEG_LON)
        pairs = pairs.assign(
            address=_trigram_jaccard(location, earlier, later),
            house_number=_agree(location.str.extract(HOUSE_NUMBER_PATTERN, expand=False).fillna(""), earlier, later),
            descriptor=_column_agrees(batch, "descriptor", earlier, later),
            location_type=_column_agrees(batch, "location_type", earlier, later),
            distance_m=distance_m,
            closeness=np.nan_to_num(np.exp(-distance_m / CLOSENESS_SCALE_M)),  # unknown position counts as far
            within_horizon=pairs["gap_s"] <= NEAR_DUPLICATE_MAX_GAP_S,
        )
        # Rounded: the same weights added in another pattern (0.3+0.05+0.05+0.15+0.25 vs 0.3+0.2+0.05+0.25)
        # differ in the last bit, and a threshold must not split two equal similarities by rounding noise.
        pairs["score"] = sum(weight * pairs[feature] for feature, weight in SIMILARITY_WEIGHTS.items()).round(9)
        if DIFFERENT_BUILDING_IS_NOT_A_COPY:
            number = _house_numbers(location)
            other = (number[earlier] != "") & (number[later] != "") & (number[earlier] != number[later])
            pairs.loc[other, "score"] = 0.0
        best = pairs.loc[pairs.groupby("later")["score"].idxmax()]
        scores = np.zeros(len(batch))
        scores[best["later"].to_numpy()] = best["score"].to_numpy()
        return scores, best


# ==============================================================================================
# LLM window review (e and f)
# ==============================================================================================
def _runs(positions: np.ndarray, max_len: int) -> list[np.ndarray]:
    """Split sorted unique positions into runs of consecutive values, each at most max_len long."""
    runs = np.split(positions, np.flatnonzero(np.diff(positions) != 1) + 1)
    return [run[i : i + max_len] for run in runs for i in range(0, len(run), max_len)]


class _WindowReview(Detector):
    """Shows each run of consecutive review_rows in arrival order in ONE prompt and asks which
    rows repeat an earlier row of that run (DESIGN.md "LLM detectors"). A run longer than
    LLM_BATCH_SAMPLE_ROWS is cut into consecutive pieces, so a copy whose original sits in the
    previous piece cannot be seen. Only rows with a readable verdict are reviewed: a row the model
    skipped, or every row of a run whose reply cannot be read, is an abstention (listed in
    `abstained` with its cause), so a broken reply never reads as "no duplicates"."""

    family = "llm"
    columns: ClassVar[list[str]] = []  # the only fields the model sees
    task: ClassVar[str] = ""

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        batch = batch.reset_index(drop=True)
        n = len(batch)
        positions = np.arange(n) if review_rows is None else np.asarray(review_rows, dtype=int).ravel()
        positions = np.unique(positions[(positions >= 0) & (positions < n)])
        shown = pd.DataFrame({c: _column(batch, c) for c in self.columns})
        tag = f"{self.problem}/{self.name}"
        nothing = np.zeros(n, dtype=bool)
        flags, reviewed, reasons, evidence, usage, abstained = nothing.copy(), nothing.copy(), {}, {}, Usage(), {}
        try:
            for run in _runs(positions, config.LLM_BATCH_SAMPLE_ROWS):
                records = [{"id": int(p), **row} for p, row in zip(run, shown.iloc[run].to_dict("records"))]
                verdicts, run_usage, _ = llm.review_rows(self.task, records, tag=tag, rows_per_call=len(run),  # one prompt
                                                         abstained=abstained)
                usage.add(run_usage)
                for p in run:  # verdicts for ids outside the run are ignored
                    verdict = verdicts.get(int(p))
                    if verdict is None:
                        continue  # no readable verdict: an abstention, not a clean row
                    reviewed[p] = True
                    if verdict.get("flagged") is True:
                        flags[p] = True
                        reasons[int(p)] = str(verdict.get("reason") or "flagged by the model")
                        evidence[int(p)] = {k: verdict.get(k) for k in ("observed", "expected", "confidence")}
        except llm.LLMUnavailable as exc:
            return self.result(False, error=f"llm_unavailable: {exc}", reviewed=nothing, usage=usage)
        alerts = []
        if flags.any():
            first = evidence[int(np.flatnonzero(flags)[0])]
            confidences = [e["confidence"] for e in evidence.values() if isinstance(e["confidence"], (int, float))]
            title = PROBLEMS[self.problem]["title"].lower()
            alerts.append(self.alert(
                f"{flags.sum()} of {int(reviewed.sum())} reviewed rows flagged as {title} by LLM window review"
                + (f" ({len(positions) - int(reviewed.sum())} rows got no verdict)" if len(positions) > reviewed.sum() else ""),
                check_id=f"L-{self.problem}1", columns=list(self.columns), rows=_examples(flags), n_rows=int(flags.sum()),
                observed=first["observed"], expected=first["expected"],
                score=float(np.mean(confidences)) if confidences else None,
            ))
        return self.result(
            bool(alerts), alerts=alerts, row_flags=flags, row_reasons=reasons, row_evidence=evidence,
            reviewed=reviewed, abstained=abstained, usage=usage,
        )


class LLMDuplicateReview(_WindowReview):
    problem, name = "e", "llm_duplicate_review"
    description = "LLM window review: which rows of a run of consecutive rows repeat an earlier row of the run exactly."
    columns = LLM_E_COLUMNS
    task = """Problem to check: EXACT DUPLICATES.
The rows are consecutive records of one data batch in arrival order; id = position in the batch.
Flag a row only if it repeats an EARLIER row of this list (smaller id) exactly: the same unique_key
and the same value in every field, i.e. one record delivered twice.
Not a duplicate: the first occurrence itself; rows that are merely alike (same complaint type,
address or time) but have a different unique_key or any different value.
For a flagged row set observed to its unique_key and expected to "first delivered as id <earlier id>"."""


class LLMNearDuplicateReview(_WindowReview):
    problem, name = "f", "llm_near_duplicate_review"
    description = "LLM window review: which rows of a run of consecutive rows re-enter the same real-world request as an earlier row."
    columns = LLM_F_COLUMNS
    task = """Problem to check: NEAR-DUPLICATES, i.e. the same real-world service request entered twice.
The rows are consecutive records of one data batch in arrival order; id = position in the batch.
Flag a row only if it re-enters the request of an EARLIER row of this list (smaller id): another
unique_key, created at most 5 minutes later, the same agency, complaint type and descriptor
(upper/lower case aside) and the same place: the same address up to small differences (STREET/ST,
AVENUE/AVE, extra spaces, a one-character typo) at the same coordinates, or the same coordinates
when there is no address.
Not a near-duplicate: the earlier row of a pair; another descriptor; another building (other
coordinates), even on the same street; a row with the same unique_key as an earlier row (an exact
re-delivery is a different problem).
For a flagged row set observed to its incident_address exactly as shown (its latitude and
longitude if it has no address) and expected to "a new request, not a repeat of id <earlier id>"."""


DETECTORS = [
    RuleExactDuplicateRows,
    StatBloomDuplicate,
    LLMDuplicateReview,
    RuleNearDuplicateKey,
    StatNearDuplicateSimilarity,
    LLMNearDuplicateReview,
]
