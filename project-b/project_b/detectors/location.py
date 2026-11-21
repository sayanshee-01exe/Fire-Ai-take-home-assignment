"""Location checks: (d) latitude/longitude swapped and (i) ZIP code inconsistent with borough.

Both problems live in single rows, so every detector returns row flags, a reason per flagged
row and ONE summary alert per batch. A row that cannot be checked (value missing, column
absent or renamed, not a number, not a 5-digit ZIP) is never flagged.

d - swapped coordinates
  rule_nyc_bbox            NYC latitudes are ~40.5-40.9 and longitudes ~-74.3 to -73.7, so a
                           swapped pair lands ~114 degrees away from the city. A fixed box is
                           the whole check; the reason says "swapped" when the reversed pair is
                           inside it.
  stat_coordinate_outlier  Knows nothing about swaps; it learns where a row's coordinates
                           should be. Main witness: the state-plane x/y columns, which the
                           city's geocoder writes for the same point. Over 50 km the projection
                           is almost linear, so an affine map fitted on reference rows predicts
                           lat/lon from x/y within ~0.07 km, and a swapped row misses by
                           thousands of km. Those 0.07 km are the map's own curvature error,
                           largest in the far south-west of Staten Island (x/y and lat/lon give
                           the same point exactly: a cubic map fits them to under a millimetre),
                           so the ~0.1 % of clean rows this signal flags are Tottenville rows,
                           not random ones. Rows without usable x/y (column dropped, renamed,
                           not a finite number) fall back to a robust median/MAD z-score of lat
                           and lon. Each signal has its own threshold: the reference score
                           exceeded by at most TARGET_ROW_FPR of reference rows.
  llm_coordinate_review    borough, ZIP, latitude, longitude per row.

i - ZIP vs borough
  rule_zip_prefix_borough  The USPS 3-digit prefix table, written by hand and NOT learned from
                           data. ZIPs that straddle a borough line (10463 includes Marble Hill,
                           which is Manhattan) raise false alarms: a finding about rules, not
                           something to tune away. A ZIP outside the city's prefixes contradicts
                           every borough and is flagged too.
  stat_zip_borough_frequency
                           P(borough | ZIP) from reference counts with add-one smoothing,
                           divided by the probability of the ZIP's most common borough, i.e.
                           (rows of this pair + 1) / (rows of the usual borough + 1). It is 1
                           for a ZIP's usual borough (for both halves of an evenly split ZIP
                           too), so the threshold only bites on minority boroughs, and a pair
                           never seen scores lower the more rows back the usual borough. ZIPs
                           with fewer than MIN_ZIP_ROWS reference rows are not judged (too
                           little evidence). On the 2023 reference under 0.1 % of rows sit in
                           a minority borough (the clean pool already drops pairs below 2 % of
                           a ZIP's rows), so the calibrated threshold comes out at exactly 1:
                           every minority borough of a well-supported ZIP is flagged, e.g.
                           Marble Hill (10463 with MANHATTAN), the rule's boundary false alarm.
  llm_zip_borough_review   ZIP, borough, city, address per row.
  Rows with an invalid ZIP or a borough outside the five names ('Unspecified') are skipped. A
  ZIP column delivered as numbers (11201.0) is read back as the 5-digit ZIP and still checked.

fit() needs only a few vectorised passes (least squares on 3 columns, one cross-tab), well
under the 60 s budget on the full ~2.1 M-row reference, so nothing is subsampled.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from project_b import config, llm
from project_b.core import Alert, DetectionResult, Detector

MAX_EXAMPLE_ROWS = 20  # example rows listed in a summary alert

# --- d: latitude / longitude swapped ------------------------------------------------------
LATLON_COLUMNS = ["latitude", "longitude"]
STATE_PLANE_COLUMNS = ["x_coordinate_state_plane", "y_coordinate_state_plane"]
NYC_BOX_TEXT = f"latitude {config.NYC_LAT_MIN}..{config.NYC_LAT_MAX}, longitude {config.NYC_LON_MIN}..{config.NYC_LON_MAX}"
KM_PER_DEG_LAT = 111.0
KM_PER_DEG_LON = 84.2  # 111.0 * cos(40.7 deg): one degree of longitude at New York's latitude
MAD_TO_SD = 1.4826  # turns a median absolute deviation into a standard deviation (normal data)

# --- i: ZIP code vs borough ---------------------------------------------------------------
ZIP_BOROUGH_COLUMNS = ["incident_zip", "borough"]
ZIP_PATTERN = r"\d{5}"
USPS_PREFIX_BOROUGH = {
    "100": "MANHATTAN", "101": "MANHATTAN", "102": "MANHATTAN",
    "103": "STATEN ISLAND",
    "104": "BRONX",
    "112": "BROOKLYN",
    "110": "QUEENS", "111": "QUEENS", "113": "QUEENS", "114": "QUEENS", "116": "QUEENS",
}
SMOOTHING = 1.0  # pseudo-count added to every (ZIP, borough) reference count
MIN_ZIP_ROWS = 20  # reference rows a ZIP needs before its borough mix is trusted

# --- LLM review: only the columns each check needs (cost) ---------------------------------
COORDINATE_LLM_COLUMNS = ["borough", "incident_zip", "latitude", "longitude"]
ZIP_BOROUGH_LLM_COLUMNS = ["incident_zip", "borough", "city", "incident_address"]
COORDINATE_LLM_TASK = """
Problem to check: latitude and longitude SWAPPED.
Each row is one New York City 311 service request: borough, incident_zip, latitude, longitude (WGS84 degrees).
In New York City a correct latitude is about 40.5 to 40.9 and a correct longitude about -74.3 to -73.7.
Flag a row (flagged=true) only if the latitude field holds the longitude and vice versa, e.g. latitude -73.95 with longitude 40.71.
Do NOT flag: rows with a missing (null) coordinate; correctly ordered coordinates, even if they seem slightly off or do not match the borough or ZIP code (other checks cover that).
For a flagged row: observed = the latitude and longitude as given; expected = the corrected latitude and longitude.
"""
ZIP_BOROUGH_LLM_TASK = """
Problem to check: ZIP code INCONSISTENT with borough.
Each row is one New York City 311 service request: incident_zip, borough, city, incident_address.
Flag a row (flagged=true) only if the ZIP code clearly lies in a different borough than the one recorded (e.g. ZIP 10467 is in the Bronx, so 10467 with borough BROOKLYN is inconsistent), or is not a New York City ZIP code at all.
Do NOT flag: rows with a missing ZIP code or borough; borough 'Unspecified'; ZIP codes that genuinely straddle a borough or city boundary; a city field holding a neighbourhood name (e.g. FLUSHING or JAMAICA in Queens). Use city and address only as supporting evidence.
For a flagged row: observed = the ZIP code and borough as given; expected = the borough that ZIP code belongs to.
"""


# ==========================================================================================
# d - latitude / longitude swapped
# ==========================================================================================
class RuleNycBbox(Detector):
    problem = "d"
    family = "rule"
    name = "rule_nyc_bbox"
    description = "Flags rows whose latitude/longitude lie outside the NYC bounding box; says 'swapped' when the reversed pair is inside."

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        lat, lon = _numeric(batch, "latitude"), _numeric(batch, "longitude")
        checked = ~np.isnan(lat) & ~np.isnan(lon)  # both non-null (an infinite value is outside the box too)
        flags = checked & ~_in_nyc_box(lat, lon)
        if not flags.any():
            return self.result(False, row_flags=flags)
        swapped = flags & _in_nyc_box(lon, lat)
        reasons = {int(p): _bbox_reason(lat[p], lon[p], swapped[p]) for p in np.flatnonzero(flags)}
        first = int(np.flatnonzero(flags)[0])
        alert = _summary_alert(
            self,
            flags,
            f"{flags.sum()} rows have coordinates outside New York City; "
            f"{swapped.sum()} of them are inside once latitude and longitude are swapped",
            check_id="R-d1",
            columns=list(LATLON_COLUMNS),
            observed=f"latitude {lat[first]:.6f}, longitude {lon[first]:.6f} (row {first})",
            expected=NYC_BOX_TEXT,
            score=float(flags.sum() / checked.sum()),
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_reasons=reasons)


class StatCoordinateOutlier(Detector):
    problem = "d"
    family = "stat"
    name = "stat_coordinate_outlier"
    description = (
        "Distance between lat/lon and the point implied by the state-plane x/y (affine map fitted on reference), "
        "or a robust median/MAD z-score of lat/lon when x/y are missing; thresholds calibrated to TARGET_ROW_FPR."
    )

    def fit(self, reference: pd.DataFrame) -> "StatCoordinateOutlier":
        lat, lon, x, y = _coordinates(reference)
        latlon = np.column_stack([lat, lon])
        has_latlon = np.isfinite(latlon).all(axis=1)
        self.center_ = np.median(latlon[has_latlon], axis=0)
        self.scale_ = MAD_TO_SD * np.median(np.abs(latlon[has_latlon] - self.center_), axis=0)
        has_all = has_latlon & np.isfinite(x) & np.isfinite(y)
        self.xy_map_ = np.linalg.lstsq(_affine(x[has_all], y[has_all]), latlon[has_all], rcond=None)[0]
        # Calibrate each signal on every reference row it can score (robust z: as if x/y were missing).
        xy_km, z = self._xy_distance_km(lat, lon, x, y), self._robust_z(lat, lon)
        self.threshold_ = {
            "xy_km": _upper_limit(xy_km[np.isfinite(xy_km)]),
            "robust_z": _upper_limit(z[np.isfinite(z)]),
        }
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        lat, lon, x, y = _coordinates(batch)
        score, limit, uses_xy = self._scores(lat, lon, x, y)
        flags = score > limit  # rows without lat/lon score NaN and are never flagged
        row_scores = np.nan_to_num(score / limit)  # > 1: beyond the limit
        if not flags.any():
            return self.result(False, row_flags=flags, row_scores=row_scores)
        swapped_score, _, _ = self._scores(lon, lat, x, y)
        swapped = flags & (swapped_score <= limit)
        xy_point = self._xy_point(x, y)
        reasons = {
            int(p): self._reason(lat[p], lon[p], score[p], xy_point[p] if uses_xy[p] else None, swapped[p])
            for p in np.flatnonzero(flags)
        }
        first = int(np.flatnonzero(flags)[0])
        unit = "km from its x/y point" if uses_xy[first] else "robust SDs from the centre"
        alert = _summary_alert(
            self,
            flags,
            f"{flags.sum()} rows have coordinates far from where the reference model puts them "
            f"({(flags & uses_xy).sum()} checked against state-plane x/y, {(flags & ~uses_xy).sum()} against the "
            f"robust lat/lon centre); {swapped.sum()} fit once latitude and longitude are swapped",
            check_id="S-d1",
            columns=LATLON_COLUMNS + (STATE_PLANE_COLUMNS if (flags & uses_xy).any() else []),
            observed=f"latitude {lat[first]:.6f}, longitude {lon[first]:.6f}: {score[first]:,.2f} {unit} (row {first})",
            expected=f"<= {self.threshold_['xy_km']:.3f} km from the x/y point; "
            f"robust z <= {self.threshold_['robust_z']:.2f} when x/y are missing",
            score=float(row_scores[flags].max()),
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_reasons=reasons, row_scores=row_scores)

    def _scores(self, lat, lon, x, y) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Per row: score, its threshold, and whether the x/y signal was used (else the robust z-score)."""
        xy_km = self._xy_distance_km(lat, lon, x, y)
        uses_xy = np.isfinite(xy_km)
        score = np.where(uses_xy, xy_km, self._robust_z(lat, lon))
        limit = np.where(uses_xy, self.threshold_["xy_km"], self.threshold_["robust_z"])
        return score, limit, uses_xy

    def _xy_point(self, x, y) -> np.ndarray:
        """(lat, lon) where the state-plane x/y put each row; NaN where x or y is missing or infinite (unusable:
        an infinite x and y would make the map compute inf - inf, a RuntimeWarning)."""
        usable = np.isfinite(x) & np.isfinite(y)
        return _affine(np.where(usable, x, np.nan), np.where(usable, y, np.nan)) @ self.xy_map_

    def _xy_distance_km(self, lat, lon, x, y) -> np.ndarray:
        """Distance between lat/lon and the point the state-plane x/y give; NaN if any value is missing."""
        xy_point = self._xy_point(x, y)
        return np.hypot((lat - xy_point[:, 0]) * KM_PER_DEG_LAT, (lon - xy_point[:, 1]) * KM_PER_DEG_LON)

    def _robust_z(self, lat, lon) -> np.ndarray:
        """The larger robust z-score of lat and lon around the reference median; NaN if either is missing."""
        return (np.abs(np.column_stack([lat, lon]) - self.center_) / self.scale_).max(axis=1)

    def _reason(self, lat, lon, score, xy_point, swapped) -> str:
        if xy_point is not None:
            where = (
                f"{score:,.2f} km from the point its state-plane x/y give ({xy_point[0]:.6f}, {xy_point[1]:.6f}); "
                f"limit {self.threshold_['xy_km']:.3f} km"
            )
        else:
            where = (
                f"{score:,.1f} robust SDs from the reference centre ({self.center_[0]:.4f}, {self.center_[1]:.4f}); "
                f"limit {self.threshold_['robust_z']:.2f}"
            )
        return f"lat/lon ({lat:.6f}, {lon:.6f}) is {where}" + ("; swapping latitude and longitude fits" if swapped else "")


class LlmCoordinateReview(Detector):
    problem = "d"
    family = "llm"
    name = "llm_coordinate_review"
    description = "Gemini reviews borough, ZIP, latitude and longitude of each row and flags swapped coordinates."

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        return _llm_row_review(self, batch, review_rows, COORDINATE_LLM_COLUMNS, COORDINATE_LLM_TASK, "L-d1", required=LATLON_COLUMNS)


# ==========================================================================================
# i - ZIP code inconsistent with borough
# ==========================================================================================
class RuleZipPrefixBorough(Detector):
    problem = "i"
    family = "rule"
    name = "rule_zip_prefix_borough"
    description = "Hand-coded USPS 3-digit ZIP prefixes per borough; flags rows whose ZIP prefix belongs to another borough or to none."

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        zips, boroughs, checkable = _zip_and_borough(batch)
        prefix_borough = zips.str[:3].map(USPS_PREFIX_BOROUGH)  # NaN: not a New York City prefix
        outside_nyc = prefix_borough.isna().to_numpy()
        # a ZIP outside the city contradicts every borough, so it is flagged as well
        flags = checkable & (outside_nyc | (prefix_borough != boroughs).to_numpy(dtype=bool))
        if not flags.any():
            return self.result(False, row_flags=flags)
        zip_at, borough_at, owner_at = _per_row(zips, boroughs, prefix_borough)
        reasons = {}
        for p in np.flatnonzero(flags):
            owner = "not a New York City" if outside_nyc[p] else f"a {owner_at[p]}"
            reasons[int(p)] = f"ZIP {zip_at[p]} is {owner} ZIP (USPS prefix {zip_at[p][:3]}) but borough is {borough_at[p]}"
        first = int(np.flatnonzero(flags)[0])
        alert = _summary_alert(
            self,
            flags,
            f"{flags.sum()} rows have a ZIP whose USPS prefix belongs to another borough "
            f"({(flags & outside_nyc).sum()} are not New York City ZIPs)",
            check_id="R-i1",
            columns=list(ZIP_BOROUGH_COLUMNS),
            observed=f"ZIP {zip_at[first]} with borough {borough_at[first]} (row {first})",
            expected="a New York City ZIP" if outside_nyc[first] else f"borough {owner_at[first]} for USPS prefix {zip_at[first][:3]}",
            score=float(flags.sum() / checkable.sum()),
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_reasons=reasons)


class StatZipBoroughFrequency(Detector):
    problem = "i"
    family = "stat"
    name = "stat_zip_borough_frequency"
    description = (
        "P(borough | ZIP) from add-one smoothed reference counts, relative to the ZIP's most common borough; flags rows "
        "below a threshold calibrated to TARGET_ROW_FPR, for ZIPs with >= MIN_ZIP_ROWS reference rows."
    )

    def fit(self, reference: pd.DataFrame) -> "StatZipBoroughFrequency":
        zips, boroughs, checkable = _zip_and_borough(reference)
        counts = pd.crosstab(zips[checkable], boroughs[checkable])  # ZIP x borough -> reference rows
        self.pair_rows_ = counts.stack()  # (ZIP, borough) -> rows, zeros included
        self.zip_rows_ = counts.sum(axis=1)
        self.top_rows_ = counts.max(axis=1)
        self.top_borough_ = counts.idxmax(axis=1)
        judged = counts[self.zip_rows_ >= MIN_ZIP_ROWS].to_numpy()
        likelihood = _relative_likelihood(judged, judged.max(axis=1, keepdims=True))
        # every reference row contributes the score of its own (ZIP, borough) pair
        self.threshold_ = _lower_limit(np.repeat(likelihood.ravel(), judged.ravel()))
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        zips, boroughs, checkable = _zip_and_borough(batch)
        pair_rows = self.pair_rows_.reindex(pd.MultiIndex.from_arrays([zips, boroughs]), fill_value=0).to_numpy()
        zip_rows = self.zip_rows_.reindex(zips).to_numpy(dtype=float)  # NaN: ZIP unseen in reference
        top_rows = self.top_rows_.reindex(zips).to_numpy(dtype=float)
        judged = checkable & (zip_rows >= MIN_ZIP_ROWS)
        likelihood = _relative_likelihood(pair_rows, top_rows)
        flags = judged & (likelihood < self.threshold_)
        row_scores = np.where(judged, 1 - likelihood, 0.0)
        if not flags.any():
            return self.result(False, row_flags=flags, row_scores=row_scores)
        top_borough = self.top_borough_.reindex(zips).to_numpy()
        zip_at, borough_at = _per_row(zips, boroughs)
        reasons = {
            int(p): f"ZIP {zip_at[p]} with borough {borough_at[p]}: {pair_rows[p]} of {zip_rows[p]:.0f} reference rows, "
            f"usually {top_borough[p]} ({top_rows[p]:.0f}); relative likelihood {likelihood[p]:.2g} < {self.threshold_:.2g}"
            for p in np.flatnonzero(flags)
        }
        first = int(np.flatnonzero(flags)[0])
        alert = _summary_alert(
            self,
            flags,
            f"{flags.sum()} rows have a borough that is unusual for their ZIP (relative likelihood < {self.threshold_:.3g}); "
            f"{(checkable & ~judged).sum()} rows not judged (ZIP unseen or under {MIN_ZIP_ROWS} reference rows)",
            check_id="S-i1",
            columns=list(ZIP_BOROUGH_COLUMNS),
            observed=f"ZIP {zip_at[first]} with borough {borough_at[first]}: {pair_rows[first]} reference rows (row {first})",
            expected=f"borough {top_borough[first]} ({top_rows[first]:.0f} of {zip_rows[first]:.0f} reference rows)",
            score=float(likelihood[flags].min()),
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_reasons=reasons, row_scores=row_scores)


class LlmZipBoroughReview(Detector):
    problem = "i"
    family = "llm"
    name = "llm_zip_borough_review"
    description = "Gemini reviews ZIP, borough, city and address of each row and flags ZIPs that belong to another borough."

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        return _llm_row_review(self, batch, review_rows, ZIP_BOROUGH_LLM_COLUMNS, ZIP_BOROUGH_LLM_TASK, "L-i1", required=ZIP_BOROUGH_COLUMNS)


# ==========================================================================================
# shared helpers
# ==========================================================================================
def _column(frame: pd.DataFrame, name: str) -> pd.Series | None:
    """The column called `name` (the first one if the name is duplicated), or None when absent."""
    if name not in frame.columns:
        return None
    values = frame[name]
    return values.iloc[:, 0] if isinstance(values, pd.DataFrame) else values


def _numeric(frame: pd.DataFrame, name: str) -> np.ndarray:
    """Column as float64, NaN where missing or not a number; all NaN when the column is absent."""
    values = _column(frame, name)
    if values is None:
        return np.full(len(frame), np.nan)
    return pd.to_numeric(values, errors="coerce").to_numpy(dtype=float, na_value=np.nan)


def _text(frame: pd.DataFrame, name: str) -> pd.Series:
    """Column as strings with a positional index, NaN where missing; all NaN when the column is absent."""
    values = _column(frame, name)
    if values is None:
        return pd.Series(np.nan, index=range(len(frame)), dtype="str")
    return values.astype("str").reset_index(drop=True)


def _coordinates(frame: pd.DataFrame) -> list[np.ndarray]:
    """latitude, longitude, state-plane x, state-plane y as float arrays."""
    return [_numeric(frame, name) for name in LATLON_COLUMNS + STATE_PLANE_COLUMNS]


def _affine(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Design matrix [1, x, y] of the affine map from state-plane x/y to lat/lon."""
    return np.column_stack([np.ones_like(x), x, y])


def _in_nyc_box(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    return (lat >= config.NYC_LAT_MIN) & (lat <= config.NYC_LAT_MAX) & (lon >= config.NYC_LON_MIN) & (lon <= config.NYC_LON_MAX)


def _bbox_reason(lat: float, lon: float, swapped: bool) -> str:
    if swapped:
        return f"latitude {lat:.6f} / longitude {lon:.6f} are outside NYC but inside once swapped (latitude {lon:.6f}, longitude {lat:.6f})"
    return f"latitude {lat:.6f} / longitude {lon:.6f} are outside NYC ({NYC_BOX_TEXT})"


def _zip_and_borough(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series, np.ndarray]:
    """ZIP and borough strings, plus the rows that can be judged: a 5-digit ZIP and one of the five boroughs.
    A ZIP column delivered as numbers reads '11201.0' as text; dropping the '.0' keeps those rows checkable."""
    zips, boroughs = _text(frame, "incident_zip").str.removesuffix(".0"), _text(frame, "borough")
    checkable = (zips.str.fullmatch(ZIP_PATTERN) & boroughs.isin(config.BOROUGHS)).to_numpy(dtype=bool)
    return zips, boroughs, checkable


def _per_row(*columns: pd.Series) -> list[np.ndarray]:
    """The columns as numpy object arrays, for the per-row reason loops: reading one value of a pandas string
    column (`.iloc[p]`) costs ~50 us, so a fully flagged 5,000-row batch spent most of its time budget there."""
    return [column.to_numpy(dtype=object) for column in columns]


def _relative_likelihood(pair_rows, top_rows):
    """P(borough | ZIP) / P(most common borough | ZIP) with add-one smoothing: 1 for the ZIP's usual borough."""
    return (pair_rows + SMOOTHING) / (top_rows + SMOOTHING)


def _upper_limit(reference_scores: np.ndarray) -> float:
    """A reference score with at most TARGET_ROW_FPR of reference rows above it (flag: score > limit)."""
    return float(np.quantile(reference_scores, 1 - config.TARGET_ROW_FPR, method="higher"))


def _lower_limit(reference_scores: np.ndarray) -> float:
    """A reference score with at most TARGET_ROW_FPR of reference rows below it (flag: score < limit)."""
    return float(np.quantile(reference_scores, config.TARGET_ROW_FPR, method="lower"))


def _summary_alert(det: Detector, flags: np.ndarray, message: str, **fields) -> Alert:
    """The one alert a row-level detector raises per batch: row count plus the first example rows."""
    rows = np.flatnonzero(flags)
    return det.alert(message, rows=rows[:MAX_EXAMPLE_ROWS].tolist(), n_rows=len(rows), **fields)


def _review_positions(review_rows: np.ndarray | None, n_rows: int) -> np.ndarray:
    """Batch positions to review: every row when the harness passes None, else the valid distinct ones given."""
    if review_rows is None:
        return np.arange(n_rows)
    positions = np.unique(np.asarray(review_rows, dtype=int))
    return positions[(positions >= 0) & (positions < n_rows)]


def _llm_row_review(
    det: Detector, batch: pd.DataFrame, review_rows, columns: list[str], task: str, check_id: str, required: list[str]
) -> DetectionResult:
    """Per-row LLM review (DESIGN 'LLM detectors'): send the `columns` that arrived of the rows to review, each
    with its batch position as `id`, and map the model's verdicts back. A row is flagged iff the model said
    flagged=true. Only rows with a readable verdict are reviewed; a row the model gave none for (unreadable
    reply, skipped id) is an abstention, listed in `abstained` - never a clean row. Without the
    `required` columns (the values the check is about) the model could judge nothing: no row is reviewed."""
    n_rows = len(batch)
    if any(c not in batch.columns for c in required):  # e.g. latitude renamed: no call, no tokens spent
        return _nothing_reviewed(det, n_rows)
    positions = _review_positions(review_rows, n_rows)
    present = [c for c in columns if c in batch.columns]
    abstained: dict[int, str] = {}
    try:
        verdicts, usage, _ = llm.review_rows(task, _llm_records(batch, present, positions), tag=f"{det.problem}/{det.name}",
                                                  abstained=abstained)
    except llm.LLMUnavailable as exc:
        return _nothing_reviewed(det, n_rows, error=f"llm_unavailable: {exc}")
    answered = [int(p) for p in positions if int(p) in verdicts]
    reviewed, flags = np.zeros(n_rows, dtype=bool), np.zeros(n_rows, dtype=bool)
    reviewed[answered] = True
    flagged = {p: verdicts[p] for p in answered if verdicts[p].get("flagged") is True}
    flags[list(flagged)] = True
    if not flagged:  # includes "no verdict at all": every asked row abstained, none is called clean
        return det.result(False, row_flags=flags, reviewed=reviewed, abstained=abstained, usage=usage)
    reasons = {p: str(v.get("reason") or "flagged by the model without a reason") for p, v in flagged.items()}
    evidence = {p: {key: v.get(key) for key in ("observed", "expected", "confidence")} for p, v in flagged.items()}

    first = min(flagged)
    unanswered = len(positions) - len(answered)
    confidences = [e["confidence"] for e in evidence.values() if isinstance(e["confidence"], (int, float))]
    alert = _summary_alert(
        det,
        flags,
        f"LLM review flagged {len(flagged)} of {len(answered)} reviewed rows"
        + (f" ({unanswered} rows got no verdict)" if unanswered else "")
        + f"; e.g. row {first}: {reasons[first]}",
        check_id=check_id,
        columns=present,
        observed=evidence[first]["observed"],
        expected=evidence[first]["expected"],
        score=float(np.mean(confidences)) if confidences else None,
    )
    return det.result(
        True, alerts=[alert], row_flags=flags, row_reasons=reasons, row_evidence=evidence, reviewed=reviewed,
        abstained=abstained, usage=usage,
    )


def _llm_records(batch: pd.DataFrame, columns: list[str], positions: np.ndarray) -> list[dict]:
    """One record per row to review: its batch position as `id` plus the given columns."""
    sample = pd.DataFrame({c: _column(batch, c).iloc[positions].to_numpy() for c in columns})
    return [{"id": int(p), **values} for p, values in zip(positions, sample.to_dict("records"))]


def _nothing_reviewed(det: Detector, n_rows: int, **fields) -> DetectionResult:
    """A result that reviews and flags no row (the check could not run); `fields` adds error / usage."""
    return det.result(False, row_flags=np.zeros(n_rows, dtype=bool), reviewed=np.zeros(n_rows, dtype=bool), **fields)


DETECTORS = [
    RuleNycBbox,
    StatCoordinateOutlier,
    LlmCoordinateReview,
    RuleZipPrefixBorough,
    StatZipBoroughFrequency,
    LlmZipBoroughReview,
]
