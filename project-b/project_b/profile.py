"""Task 1: profile the raw 2023 NYC 311 data - which quality problems exist already?

    python -m project_b.profile

Writes project_b/results/profile.json (every number) and project_b/results/profiling_report.md
(tables rendered from that JSON only; RESULTS_B.md includes its summary table).

Why it is built this way
* One record shape for every check: rows affected, % of all rows, the most affected
  agencies (with the share of each agency's rows) and up to five concrete examples
  (unique_key + the offending values, spread over the year). One renderer turns every
  record into markdown, so a new check is one `report.add(...)` call.
* The issues the sanitiser removes are reported from `clean.issue_flags` and its helpers,
  so this report and the clean pool can never disagree.
* Free-text checks run on DISTINCT values (~800 resolution texts for 3.2 M rows) and are
  mapped back to rows with `isin`.
* The year is loaded once with every column (~2 GB of data; measured peak 3.3 GiB resident,
  4.5 GiB committed, ~2 min): the sanitiser's exact-duplicate check needs all columns, and
  dates are parsed once and shared by every check.
"""
from __future__ import annotations

import difflib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from project_b import config
from project_b.core import jsonable
from project_b.data import clean
from project_b.data.load import load_raw, to_feed

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
N_EXAMPLES = 5  # examples kept per finding in the JSON, spread over the year
MD_EXAMPLES = 3  # ... of which the markdown shows this many
MD_ITEMS = 5  # entries shown per detail dict / list in the markdown
EXAMPLE_MAX_CHARS = 90  # long free text is cut in examples
TOP_AGENCIES = 3  # most affected agencies listed per finding

TEXT_COLUMNS = [c for c, dtype in config.FEED_DTYPES.items() if dtype == "str" and c not in config.DATETIME_COLUMNS]
NUMERIC_COLUMNS = [c for c, dtype in config.FEED_DTYPES.items() if dtype != "str"]

# A placeholder is a value made only of these tokens, zeros or punctuation once stripped and
# upper-cased ('N/A', 'na na', '00000', '-', whitespace-only), or one saying UNSPECIFIED anywhere.
PLACEHOLDER_REGEX = r"(?:(?:N/?A|NONE|NULL|UNKNOWN|UNSPECIFIED|TBD|0+|[-.?]+)\s*)*"
UNSPECIFIED_REGEX = r"\bUNSPECIFIED\b"

MIDNIGHT = "T00:00:00.000"
LONG_OPEN_DAYS = 365
BULK_CLOSE_MIN_ROWS = 500  # this many rows closed in the same (non-midnight) second = a batch job
LATE_ENTRY_DAYS = 7  # entered at least this long after its created_date = back-filled (see _entry_time)
LOCAL_TIMEZONE = "America/New_York"  # the dates are wall-clock times here, without a UTC offset

NYC_ZIP_PREFIXES = ["100", "101", "102", "103", "104", "110", "111", "112", "113", "114", "116"]
NYC_NON_USPS_ZIPS = ["00083"]  # Central Park in the city's ZIP code map
STATE_PLANE_MAX_GAP_M = 250  # x/y vs lat/lon gap beyond the affine-fit error (~100 m at the edges)
M_PER_DEG_LAT, M_PER_DEG_LON = 111_320, 84_390  # metres per degree at NYC's latitude
BOROUGH_AS_CITY = ["QUEENS", "MANHATTAN"]  # USPS city names there are neighbourhoods / 'NEW YORK'
CITY_TYPO_MAX_ROWS = 5  # only rare spellings are typo candidates ...
CITY_TYPO_MIN_SIMILARITY = 0.88  # ... of a more common name, with a difflib ratio >= this

CASE_VARIANT_COLUMNS = ["complaint_type", "descriptor"]  # spelled two ways within one agency
CROSS_AGENCY_CASE_COLUMNS = ["complaint_type", "descriptor", "location_type"]  # ... or across agencies
VARIANT_DAY_SHARE = 0.05  # report the days on which more of an agency's rows are spelling variants
NEAR_DUP_TOP_TYPES = 10
NAME_CUE_REGEX = (
    r"\b(?:Mr|Ms|Mrs|Dr)\.? [A-Z][a-z]+"
    r"|\b(?:tenant|caller|complainant|resident|spoke with|contact) [A-Z][a-z]+ [A-Z][a-z]+"
)
MOJIBAKE_REGEX = r"[\x80-\x9f]|[ÂÃ]"  # C1 control characters or a stray 'Â'/'Ã': UTF-8 bytes read as Latin-1
# Typographic characters that correctly delivered copies of the same templates write in ASCII.
ASCII_PUNCTUATION = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\xa0": " "})
FIELD_LENGTH_LIMITS = [255, 500]  # a text of exactly this length ending mid-sentence was cut by storage
TEST_RECORD_REGEX = r"(?i)ztest|integration test|\bfake\b|\bdummy\b"

SECTIONS = {
    "volume": "Volume, keys and exact duplicates",
    "completeness": "Missing values and placeholders",
    "dates": "Dates",
    "status": "Status vs closed date",
    "location": "Location",
    "zip": "ZIP code and borough",
    "city": "City",
    "categories": "Agencies, complaint types and descriptors",
    "duplicates": "Near-duplicates",
    "text": "Free text",
}
# Findings that would distort a dashboard, shown first (largest first); all are in the sections too.
SUMMARY = [
    "near_duplicate_candidate", "placeholder_any", "address_extra_spaces", "mojibake", "updated_before_created", "closed_equals_created",
    "city_is_borough_name", "closed_after_a_year", "latlon_missing", "still_open_at_download",
    "closed_without_closed_date", "entered_late", "complaint_type_case_variants", "near_duplicate_respelled", "open_with_closed_date",
    "closed_before_created", "same_content_new_key", "resolution_contradicts_status", "pii_like_free_text", "test_records",
]


# --------------------------------------------------------------------------
# Shared inputs and the collector
# --------------------------------------------------------------------------
@dataclass
class Data:
    """Everything the checks share, computed once."""

    feed: pd.DataFrame  # the full year in feed dtypes
    flags: pd.DataFrame  # clean.issue_flags(feed): the sanitiser's verdicts
    dates: dict[str, pd.Series]  # parsed date columns (NaT where missing or unparseable)
    text_counts: dict[str, pd.Series]  # value_counts per text column: the distinct values
    not_numeric: dict[str, int]  # per numeric column: raw values that are not numbers
    manifest: dict  # data/raw/manifest.json ({} for a sample profiled without the download)
    download_time: pd.Timestamp  # local wall-clock time, like the dates
    entry_time: pd.Series  # lower bound on when each row was entered (see _entry_time)


class Report:
    """Collects findings (one record per check) and tables while the checks run."""

    def __init__(self, feed: pd.DataFrame):
        self.feed = feed
        self.findings: list[dict] = []
        self.tables: list[dict] = []
        self._agency_rows = feed["agency"].value_counts()

    def add(self, section: str, fid: str, title: str, mask: pd.Series, columns: list[str], *, note: str = "",
            details: dict | None = None, derived: dict[str, pd.Series] | None = None,
            examples: list[dict] | None = None) -> None:
        """One finding: `mask` (bool, aligned with the feed) marks the affected rows; the examples
        show `columns` of those rows plus any `derived` per-row values (e.g. a computed gap)."""
        if examples is None:
            examples = [_example(row, columns, derived or {}) for _, row in pick_rows(self.feed, mask).iterrows()]
        rows = int(mask.sum())
        self.findings.append(
            {"id": fid, "section": section, "title": title, "rows": rows, "pct": _pct(rows, len(self.feed)),
             "top_agencies": self._top_agencies(mask), "examples": examples, "note": note, "details": details or {}}
        )

    def table(self, section: str, tid: str, title: str, records: list[dict]) -> None:
        self.tables.append({"id": tid, "section": section, "title": title, "rows": records})

    def _top_agencies(self, mask: pd.Series) -> list[dict]:
        hits = self.feed.loc[mask, "agency"].value_counts().head(TOP_AGENCIES)
        return [{"agency": a, "rows": int(n), "pct_of_agency": _pct(n, self._agency_rows[a])} for a, n in hits.items()]


def pick_rows(feed: pd.DataFrame, mask: pd.Series, n: int = N_EXAMPLES) -> pd.DataFrame:
    """Up to n flagged rows spread evenly over the year (rows are in arrival order)."""
    positions = np.flatnonzero(mask.to_numpy())
    if len(positions) > n:
        positions = positions[np.linspace(0, len(positions) - 1, n).round().astype(int)]
    return feed.iloc[positions]


def _example(row: pd.Series, columns: list[str], derived: dict[str, pd.Series]) -> dict:
    example = {c: _shorten(row[c]) for c in ["unique_key", *columns]}
    return example | {name: _shorten(values[row.name]) for name, values in derived.items()}


def _shorten(value):
    if isinstance(value, str) and len(value) > EXAMPLE_MAX_CHARS:
        return value[:EXAMPLE_MAX_CHARS] + "..."
    if isinstance(value, float):
        return round(value, 6)  # ~0.1 m for coordinates
    return value


def _pct(part: float, whole: float) -> float:
    return round(100 * part / whole, 4) if whole else 0.0


def _top(values: pd.Series, n: int = N_EXAMPLES) -> dict:
    """The n most frequent values with their row counts."""
    return values.value_counts().head(n).to_dict()


def _parse(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, format=config.SOURCE_DATETIME_FORMAT, errors="coerce")


def _distinct_matches(counts: pd.Series, regex: str) -> pd.Index:
    """Distinct values (the index of a value_counts) that contain `regex`."""
    return counts.index[counts.index.str.contains(regex)]


def _search(regex: str, text: str) -> re.Match | None:
    """re.search that agrees with pandas: pandas matches `str` columns with RE2, whose \\b, \\d and \\w
    are ASCII-only, so Python's re must be too ('Ñ212-639-9675' holds a phone number for both)."""
    return re.search(regex, text, flags=re.ASCII)


def _differs(a: pd.Series, b: pd.Series) -> pd.Series:
    """a != b, except that two missing values are equal (pandas says NaN != NaN)."""
    return (a != b) & ~(a.isna() & b.isna())


def _rows_matching(data: Data, regex: str, columns: list[str]) -> pd.Series:
    """Rows where any of `columns` contains `regex`; the regex runs on distinct values only."""
    mask = pd.Series(False, index=data.feed.index)
    for col in columns:
        mask |= data.feed[col].isin(_distinct_matches(data.text_counts[col], regex))
    return mask


def _fragment_examples(feed: pd.DataFrame, mask: pd.Series, columns: list[str], regex: str,
                       escape: bool = False, width: int = 35) -> list[dict]:
    """Examples showing only the text around the first match in the first matching column;
    `escape` spells out non-ASCII characters (when the garbled bytes are the point)."""
    examples = []
    for _, row in pick_rows(feed, mask).iterrows():
        col = next(c for c in columns if isinstance(row[c], str) and _search(regex, row[c]))
        match = _search(regex, row[col])
        fragment = row[col][max(0, match.start() - width) : match.end() + width]
        examples.append({"unique_key": row["unique_key"], "column": col, "fragment": ascii(fragment)[1:-1] if escape else fragment})
    return examples


# --------------------------------------------------------------------------
# 1. Volume, keys and exact duplicates
# --------------------------------------------------------------------------
def profile_volume(report: Report, data: Data) -> None:
    feed, flags = data.feed, data.flags
    per_day = feed["created_date"].str[:10].value_counts()
    report.table("volume", "rows_per_month",
                 f"Rows per created month vs the download manifest (median day: {per_day.median():,.0f} rows)",
                 _rows_per_month(per_day, data.manifest))
    report.add("volume", "dup_unique_key", "unique_key seen before", flags["dup_unique_key"], [])
    report.add("volume", "exact_duplicate_row", "Row identical to an earlier row in every column", flags["exact_duplicate_row"], [])
    report.add("volume", "same_content_new_key", "Identical to an earlier row in every column except unique_key",
               _same_content_new_key(feed), ["created_date", "agency", "complaint_type", "incident_address"],
               note="double submission: an exact duplicate under a new key")

    late = _entered_late(data)
    report.add("volume", "entered_late", f"Entered at least {LATE_ENTRY_DAYS} days after its created_date (back-filled)",
               late, ["created_date", "agency", "complaint_type"], derived={"entered_not_before": data.entry_time},
               details={"entry_days": _top(data.entry_time[late].dt.strftime("%Y-%m-%d")),
                        "created_months": _top(feed.loc[late, "created_date"].str[:7])},
               note="late-arriving rows: past days' counts change after a dashboard was built")


def _rows_per_month(per_day: pd.Series, manifest: dict) -> list[dict]:
    """Rows and busiest day per created month, next to the manifest's expected rows (None without one)."""
    month = per_day.index.str[:7]
    per_month, busiest = per_day.groupby(month).sum(), per_day.groupby(month).idxmax()
    expected = {f"{config.YEAR_START[:4]}-{entry['month']:02d}": entry["expected"] for entry in manifest.get("months", [])}
    records = []
    for key in sorted(set(per_month.index) | set(expected)):  # a month missing on either side still shows
        rows = int(per_month.get(key, 0))
        records.append({"month": key, "rows": rows, "manifest_rows": expected.get(key),
                        "difference": rows - expected[key] if key in expected else None,
                        "busiest_day": busiest.get(key), "busiest_day_rows": int(per_day.get(busiest.get(key), 0))})
    return records


def _same_content_new_key(feed: pd.DataFrame) -> pd.Series:
    content = [c for c in feed.columns if c != "unique_key"]
    # Identical content implies identical created_date, type and address: compare every column only there.
    candidates = feed.duplicated(["created_date", "complaint_type", "incident_address"], keep=False)
    same = pd.Series(False, index=feed.index)
    same[candidates] = feed.loc[candidates, content].duplicated().to_numpy()
    return same


def _entry_time(feed: pd.DataFrame, created: pd.Series) -> pd.Series:
    """A lower bound on when each row was entered: the newest created_date among rows with a
    smaller (or equal) unique_key. Keys are issued in entry order, so all those rows already
    existed; a row whose bound is far after its own created_date was back-filled later."""
    in_key_order = created.iloc[np.argsort(feed["unique_key"].to_numpy(), kind="stable")]
    return in_key_order.cummax().reindex(feed.index)


def _entered_late(data: Data) -> pd.Series:
    """Rows entered at least LATE_ENTRY_DAYS after their created_date (back-filled)."""
    return data.entry_time - data.dates["created_date"] > pd.Timedelta(days=LATE_ENTRY_DAYS)


# --------------------------------------------------------------------------
# 2. Missing values and placeholders
# --------------------------------------------------------------------------
def profile_completeness(report: Report, data: Data) -> None:
    feed = data.feed
    records, placeholders = [], {}
    for col in feed.columns:
        nulls = int(feed[col].isna().sum())
        record = {"column": col, "null_rows": nulls, "null_pct": _pct(nulls, len(feed))}
        if col in data.not_numeric:
            record["not_numeric_rows"] = data.not_numeric[col]
        if col in data.text_counts:
            placeholders[col] = _placeholders(data.text_counts[col])
            n = int(placeholders[col].sum())
            record |= {"placeholder_rows": n, "placeholder_pct": _pct(n, len(feed)),
                       "placeholder_values": placeholders[col].head(4).to_dict()}
        records.append(record)
    report.table("completeness", "nulls_and_placeholders",
                 "Nulls and placeholder values per column (numeric columns: raw values that are not numbers)", records)

    any_placeholder = pd.Series(False, index=feed.index)
    for col, values in placeholders.items():
        any_placeholder |= feed[col].isin(values.index)

    def placeholder_cells(row: pd.Series) -> dict:
        return {"unique_key": row["unique_key"], **{c: row[c] for c, v in placeholders.items() if row[c] in v.index}}

    report.add("completeness", "placeholder_any", "Row holds a placeholder ('N/A', 'Unspecified', 'UNKNOWN', ...) in some column",
               any_placeholder, [], note="missing values a null-rate check cannot see",
               examples=[placeholder_cells(row) for _, row in pick_rows(feed, any_placeholder).iterrows()])


def _placeholders(counts: pd.Series) -> pd.Series:
    """The placeholder values among a column's distinct values (value -> rows)."""
    upper = counts.index.str.strip().str.upper()
    return counts[upper.str.fullmatch(PLACEHOLDER_REGEX) | upper.str.contains(UNSPECIFIED_REGEX)]


# --------------------------------------------------------------------------
# 3. Dates
# --------------------------------------------------------------------------
def profile_dates(report: Report, data: Data) -> None:
    feed, flags = data.feed, data.flags
    created, closed = data.dates["created_date"], data.dates["closed_date"]
    updated, due = data.dates["resolution_action_updated_date"], data.dates["due_date"]
    columns = ["created_date", "closed_date", "status", "complaint_type"]

    report.add("dates", "bad_datetime_format", "Date value not in the source ISO format, or not a real date",
               flags["bad_datetime_format"], config.DATETIME_COLUMNS, details={"offending_shapes": _offending_shapes(data)},
               note="source format: YYYY-MM-DDTHH:MM:SS.000")
    before = flags["closed_before_created"]
    report.add("dates", "closed_before_created", "closed_date earlier than created_date", before, columns,
               details={"status": _top(feed.loc[before, "status"]), "complaint_type": _top(feed.loc[before, "complaint_type"]),
                        "median_gap_hours": round((created - closed)[before].dt.total_seconds().median() / 3600, 1)},
               note="mostly DOT street lights still 'Pending': closed_date is not a closing date there")
    report.add("dates", "closed_before_2023", "closed_date before 2023 (every row was created in 2023)",
               closed < pd.Timestamp(config.YEAR_START), columns)
    report.add("dates", "closed_after_download", f"closed_date after the download ({data.download_time:%Y-%m-%d})",
               closed > data.download_time, columns)
    instant = closed == created
    report.add("dates", "closed_equals_created", "closed_date equal to created_date to the second", instant, columns,
               details={"complaint_type": _top(feed.loc[instant, "complaint_type"])},
               note="0-minute resolution times (auto-closed or back-filled requests)")
    report.add("dates", "closed_after_a_year", f"Closed more than {LONG_OPEN_DAYS} days after creation",
               closed - created > pd.Timedelta(days=LONG_OPEN_DAYS), columns,
               note="long tail that dominates average resolution times")
    report.add("dates", "created_at_midnight", "created_date exactly 00:00:00", feed["created_date"].str.endswith(MIDNIGHT), columns,
               note="date-only precision for some agencies (table above), not midnight traffic")
    hour = created.dt.floor("h")
    unclear_hours = _dst_unclear_hours()
    report.add("dates", "created_in_dst_unclear_hour", "created_date in a local hour that is skipped or repeated by a DST change",
               hour.isin(unclear_hours), ["created_date", "agency"],
               details={"rows_per_hour": {f"{h:%Y-%m-%d %H}:00": int((hour == h).sum()) for h in unclear_hours},
                        "median_rows_per_hour": hour.value_counts().median()},
               note="local time without a UTC offset: the skipped hour is empty, the repeated one holds two hours of requests")

    early = updated < created
    report.add("dates", "updated_before_created", "resolution_action_updated_date before created_date", early,
               ["created_date", "resolution_action_updated_date", "status"],
               details={"stored_as_midnight_of_the_created_day": int((early & (updated == created.dt.normalize())).sum())},
               note="mostly a same-day update stored at date precision (00:00)")
    much_earlier = updated < created - pd.Timedelta(days=1)
    report.add("dates", "updated_day_before_created", "resolution_action_updated_date more than a day before created_date",
               much_earlier, ["created_date", "resolution_action_updated_date", "status"],
               details={"updated_year": _top(feed.loc[much_earlier, "resolution_action_updated_date"].str[:4])})
    report.add("dates", "due_more_than_a_year_out", "due_date more than a year after created_date",
               due - created > pd.Timedelta(days=365), ["created_date", "due_date", "closed_date"],
               details={"due_date_rows_by_agency": _top(feed.loc[due.notna(), "agency"]),
                        "due_before_created": int((due < created).sum()), "most_shared_due_dates": _top(feed["due_date"], 3)},
               note="only DSNY fills due_date; hundreds of rows share one bulk-assigned due timestamp")

    closings = feed["closed_date"].value_counts()
    bulk = closings[(closings >= BULK_CLOSE_MIN_ROWS) & ~closings.index.str.endswith(MIDNIGHT)]
    report.add("dates", "bulk_closed_same_second", f">= {BULK_CLOSE_MIN_ROWS} rows closed in the same second (not midnight)",
               feed["closed_date"].isin(bulk.index), columns, details={"timestamps": bulk.to_dict()},
               note="administrative batch closures: the resolution time is the job's, not the work's")
    report.table("dates", "timestamp_precision",
                 "Timestamp precision by agency, % of non-null values (uniform times: midnight 0.001 %, whole minute 1.7 %)",
                 _timestamp_precision(feed))


def _offending_shapes(data: Data) -> dict:
    """Per date column, the most common shapes of offending values, e.g. '99/99/9999 99:99 AA'."""
    shapes = {}
    for col in config.DATETIME_COLUMNS:
        values = data.feed.loc[data.flags["bad_datetime_format"], col].dropna()
        offending = values[~values.str.fullmatch(config.SOURCE_DATETIME_REGEX) | _parse(values).isna()]
        shapes[col] = _top(offending.str.replace(r"\d", "9", regex=True).str.replace(r"[A-Za-z]", "A", regex=True))
    return shapes


def _dst_unclear_hours() -> pd.DatetimeIndex:
    """Wall-clock hours of the year that do not exist (clocks jump forward) or happen twice
    (clocks fall back) in local time; checked on the 8,760 hours, not on 3.2 M rows."""
    hours = pd.date_range(config.YEAR_START, config.YEAR_END, freq="h", inclusive="left")
    return hours[hours.tz_localize(LOCAL_TIMEZONE, ambiguous="NaT", nonexistent="NaT").isna()]


def _timestamp_precision(feed: pd.DataFrame) -> list[dict]:
    def share_ending(col: str, suffix: str) -> pd.Series:
        """1.0 / 0.0 per row (NaN where null), so a group mean is the share of non-null values."""
        return feed[col].str.endswith(suffix).where(feed[col].notna()).astype(float) * 100

    precision = pd.DataFrame(
        {
            "agency": feed["agency"],
            "created_midnight_pct": share_ending("created_date", MIDNIGHT),
            "created_whole_minute_pct": share_ending("created_date", ":00.000"),
            "closed_midnight_pct": share_ending("closed_date", MIDNIGHT),
            "updated_midnight_pct": share_ending("resolution_action_updated_date", MIDNIGHT),
        }
    ).groupby("agency").mean().round(2)
    precision.insert(0, "rows", feed["agency"].value_counts())
    return precision.sort_values("rows", ascending=False).reset_index().to_dict("records")


# --------------------------------------------------------------------------
# 4. Status vs closed date
# --------------------------------------------------------------------------
def profile_status(report: Report, data: Data) -> None:
    feed = data.feed
    status, has_closed = feed["status"], feed["closed_date"].notna()
    is_closed, is_open = status.isin(config.CLOSED_STATUSES), status.isin(config.OPEN_STATUSES)
    columns = ["status", "closed_date", "complaint_type"]
    report.add("status", "closed_without_closed_date", "status Closed but no closed_date", is_closed & ~has_closed, columns,
               note="closed requests missing from resolution-time charts")
    report.add("status", "open_with_closed_date", "Open status (Open, In Progress, Pending, ...) but closed_date set",
               is_open & has_closed, columns, note="open or closed depending on which field a dashboard reads")
    report.add("status", "status_unspecified", "status 'Unspecified' (in neither status group)", status == "Unspecified", columns)
    age_years = (data.download_time - data.dates["created_date"]).dt.days / 365.25
    report.add("status", "still_open_at_download", "Still in an open status at download", is_open,
               ["created_date", "status", "complaint_type"],
               details={"min_age_years": round(age_years[is_open].min(), 1), "status": _top(status[is_open])},
               note="never closed: inflates open-backlog counts")
    report.add("status", "closed_without_resolution", "status Closed but no resolution_description",
               is_closed & feed["resolution_description"].isna(), columns)
    crosstab = pd.crosstab(status, has_closed).rename(columns={True: "with_closed_date", False: "without_closed_date"})
    report.table("status", "status_vs_closed_date", "Status vs presence of closed_date", crosstab.reset_index().to_dict("records"))


# --------------------------------------------------------------------------
# 5. Location
# --------------------------------------------------------------------------
def profile_location(report: Report, data: Data) -> None:
    feed, flags = data.feed, data.flags
    lat, x, y = feed["latitude"], feed["x_coordinate_state_plane"], feed["y_coordinate_state_plane"]
    coords = ["latitude", "longitude", "borough", "incident_zip"]
    state_plane = ["x_coordinate_state_plane", "y_coordinate_state_plane"]
    report.add("location", "latlon_missing", "latitude and longitude both missing", lat.isna() & feed["longitude"].isna(),
               ["incident_address", "borough"], note="rows that vanish from every map")
    report.add("location", "latlon_outside_nyc", "lat/lon outside the NYC box (incl. swapped)", flags["latlon_outside_nyc"], coords)
    report.add("location", "latlon_half_missing", "Exactly one of latitude / longitude missing", flags["latlon_half_missing"], coords)
    report.add("location", "coordinates_but_borough_unspecified", "Coordinates present but borough 'Unspecified'",
               lat.notna() & (feed["borough"] == config.UNSPECIFIED_BOROUGH), coords)
    report.add("location", "state_plane_presence_mismatch", "x/y state-plane presence differs from lat/lon presence",
               (x.notna() != lat.notna()) | (y.notna() != lat.notna()), [*state_plane, "latitude"],
               note="x or y alone, without lat/lon: a half-finished geocode")
    gap_m = _state_plane_gap_m(feed)
    report.add("location", "state_plane_disagrees", f"x/y and lat/lon more than {STATE_PLANE_MAX_GAP_M} m apart (affine fit)",
               gap_m > STATE_PLANE_MAX_GAP_M, [*state_plane, "latitude", "longitude"],
               details={"gap_m_quantiles": gap_m.quantile([0.5, 0.99, 0.999, 1.0]).round(1).to_dict()},
               note="x/y is an independent cross-check of lat/lon")
    boards = data.text_counts["community_board"].index  # '08 BRONX': the borough follows the board number
    board_borough = feed["community_board"].map(dict(zip(boards, boards.str.split(" ", n=1).str[1])))
    report.add("location", "borough_vs_community_board", "borough differs from the borough named in community_board",
               feed["borough"].isin(config.BOROUGHS) & board_borough.isin(config.BOROUGHS) & (feed["borough"] != board_borough),
               ["borough", "community_board", "incident_zip"], note="mostly boundary cases (Marble Hill: Manhattan, Bronx CB 8)")
    report.add("location", "park_borough_differs", "park_borough differs from borough", _differs(feed["park_borough"], feed["borough"]),
               ["borough", "park_borough"], note="if it never differs, park_borough is a redundant copy")
    report.add("location", "address_extra_spaces", "incident_address padded with runs of spaces ('EAST   38 STREET')",
               feed["incident_address"].str.contains(r"\S\s{2,}\S"), ["incident_address"],
               note="addresses must be normalised before grouping or de-duplicating")


def _state_plane_gap_m(feed: pd.DataFrame) -> pd.Series:
    """Metres between each row's lat/lon and an affine map of its x/y fitted on all rows
    (NaN where a coordinate is missing). Over NYC's extent the projection is affine to
    ~100 m, so larger gaps would be data errors."""
    x = feed["x_coordinate_state_plane"] - feed["x_coordinate_state_plane"].mean()  # centred: a well-conditioned fit
    y = feed["y_coordinate_state_plane"] - feed["y_coordinate_state_plane"].mean()
    lat, lon = feed["latitude"], feed["longitude"]
    complete = x.notna() & y.notna() & lat.notna() & lon.notna()
    design = np.c_[x[complete], y[complete], np.ones(complete.sum())]
    target = np.c_[lat[complete], lon[complete]]
    # Least squares via the 3x3 normal equations: on 3 M rows np.linalg.lstsq returned a latitude
    # fit off by ~2.6 km RMS on this laptop (an OpenBLAS issue); the normal equations fit to metres.
    try:
        (lat_x, lon_x), (lat_y, lon_y), (lat_0, lon_0) = np.linalg.solve(design.T @ design, design.T @ target)
    except np.linalg.LinAlgError:  # fewer than three geocoded rows off one line (a tiny sample): no map
        return pd.Series(np.nan, index=feed.index)
    lat_gap_m = (lat - (lat_x * x + lat_y * y + lat_0)) * M_PER_DEG_LAT
    lon_gap_m = (lon - (lon_x * x + lon_y * y + lon_0)) * M_PER_DEG_LON
    return np.hypot(lat_gap_m, lon_gap_m)


# --------------------------------------------------------------------------
# 6. ZIP code and borough
# --------------------------------------------------------------------------
def profile_zip(report: Report, data: Data) -> None:
    feed, flags = data.feed, data.flags
    zips, columns = feed["incident_zip"], ["incident_zip", "city", "borough"]
    report.add("zip", "zip_invalid_format", "incident_zip not 5 digits", flags["zip_invalid_format"], columns)
    outside = zips.str.fullmatch(clean.ZIP_REGEX) & ~zips.str[:3].isin(NYC_ZIP_PREFIXES) & ~zips.isin(NYC_NON_USPS_ZIPS)
    report.add("zip", "zip_outside_nyc", "5-digit ZIP with a non-NYC prefix", outside, columns, details={"zips": _top(zips[outside])},
               note="'12345' is a dummy (always MANHATTAN); DCWP complaints carry out-of-state business ZIPs")
    rare = flags["zip_borough_rare_pair"]
    pairs = feed.loc[rare, ["incident_zip", "borough"]].value_counts()
    report.add("zip", "zip_borough_rare_pair", f"(ZIP, borough) pair < {clean.RARE_ZIP_BOROUGH_SHARE:.0%} of the ZIP's rows",
               rare, [*columns, "incident_address"], details={"pairs": {f"{z} {b}": n for (z, b), n in pairs.items()}},
               note="ZIPs that straddle a borough line (Ridgewood/Bushwick, Rikers Island)")
    unspecified = feed["borough"] == config.UNSPECIFIED_BOROUGH
    share_by_agency = (100 * unspecified.groupby(feed["agency"]).mean()).round(2).sort_values(ascending=False)
    report.add("zip", "borough_unspecified", "borough 'Unspecified'", unspecified, columns,
               details={"pct_of_agency_rows": share_by_agency[share_by_agency > 0].to_dict()})


# --------------------------------------------------------------------------
# 7. City
# --------------------------------------------------------------------------
def profile_city(report: Report, data: Data) -> None:
    city, columns = data.feed["city"], ["city", "borough", "incident_zip"]
    variants, groups = _spelling_variants(city)
    report.add("city", "city_spelling_variants", "city spelled differently (case, punctuation) from its most common spelling",
               variants, columns, details={"groups": groups})
    typos = _likely_typos(data.text_counts["city"])
    report.add("city", "city_likely_typo", f"Rare city name with difflib similarity >= {CITY_TYPO_MIN_SIMILARITY} to a common one",
               _name_key(city).isin(list(typos)), columns, details={"typo_of": typos})
    report.add("city", "city_is_borough_name", "city holds 'QUEENS' or 'MANHATTAN' instead of the postal city name",
               city.str.upper().isin(BOROUGH_AS_CITY), columns, note="city granularity depends on which agency filed the row")


def _name_key(values: pd.Series | pd.Index) -> pd.Series | pd.Index:
    """Upper-case letters and digits only: 'New Hyde park' and 'NEW HYDE PARK' share a key."""
    return values.str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)


def _spelling_variants(values: pd.Series) -> tuple[pd.Series, list[dict]]:
    """Rows whose value is not the most frequent spelling of its name (see _name_key), and the
    variant groups {spelling: rows}, largest first. Prefix values with a scope ('HPD | ...')
    to compare spellings only within that scope."""
    counts = values.value_counts()
    table = pd.DataFrame({"spelling": counts.index, "rows": counts.to_numpy(), "name": _name_key(counts.index)})
    table = table[table.groupby("name")["spelling"].transform("size") > 1]
    most_common = table.groupby("name")["spelling"].transform("first")  # counts are sorted, largest first
    groups = [dict(zip(g["spelling"], g["rows"])) for _, g in table.groupby("name", sort=False)]
    return values.isin(table.loc[table["spelling"] != most_common, "spelling"]), groups


def _likely_typos(counts: pd.Series) -> dict[str, str]:
    """{rare name key: the more common name key it is most similar to}."""
    names = counts.groupby(_name_key(counts.index)).sum().sort_values(ascending=False)
    typos = {}
    for name, rows in names[names <= CITY_TYPO_MAX_ROWS].items():
        match = difflib.get_close_matches(name, names[names > rows].index.tolist(), n=1, cutoff=CITY_TYPO_MIN_SIMILARITY)
        if match:
            typos[name] = match[0]
    return typos


# --------------------------------------------------------------------------
# 8. Agencies, complaint types and descriptors
# --------------------------------------------------------------------------
def profile_categories(report: Report, data: Data) -> None:
    feed, flags = data.feed, data.flags
    report.table("categories", "complaint_type_case_style", "complaint_type case style by agency (distinct types)",
                 _case_style_by_agency(feed))
    report.table("categories", "case_differs_across_agencies", "Same name, different case in different agencies (rows)",
                 _cross_agency_case(feed))
    late = _entered_late(data)
    for col in CASE_VARIANT_COLUMNS:
        variants, groups = _spelling_variants(feed["agency"] + " | " + feed[col])
        late_pct = _pct((variants & late).sum(), variants.sum())
        report.add("categories", f"{col}_case_variants", f"{col} spelled differently (case) from the agency's usual spelling",
                   variants, ["agency", col, "created_date"],
                   details={"groups": groups, "entered_late_pct": late_pct, "days_over_5pct_of_agency_rows": _variant_days(feed, variants)},
                   note=f"one category, two dashboard rows; {late_pct:.0f} % of these rows were back-filled")

    rare = flags["complaint_descriptor_rare_pair"]
    report.add("categories", "complaint_descriptor_rare_pair",
               f"(complaint_type, descriptor) pair seen < {clean.RARE_PAIR_MIN_COUNT} times",
               rare, ["agency", "complaint_type", "descriptor"],
               details={"pairs": len(feed.loc[rare, ["complaint_type", "descriptor"]].drop_duplicates())})
    names_per_agency = feed.groupby("agency")["agency_name"].transform("nunique")
    agencies_per_name = feed.groupby("agency_name")["agency"].transform("nunique")
    report.add("categories", "agency_name_inconsistent", "agency with more than one agency_name, or vice versa",
               (names_per_agency > 1) | (agencies_per_name > 1), ["agency", "agency_name"],
               details={"pairs": len(feed[["agency", "agency_name"]].drop_duplicates())}, note="a strict 1:1 code-name mapping")
    report.add("categories", "test_records", "Test or fake records in production data", _rows_matching(data, TEST_RECORD_REGEX, TEXT_COLUMNS),
               ["complaint_type", "descriptor", "city", "resolution_description"])


def _case_style_by_agency(feed: pd.DataFrame) -> list[dict]:
    types = feed.groupby(["agency", "complaint_type"]).size().rename("rows").reset_index()
    types["upper_case"] = types["complaint_type"] == types["complaint_type"].str.upper()
    style = types.groupby("agency").agg(types=("complaint_type", "size"), upper_case_types=("upper_case", "sum"))
    style["upper_case_rows"] = types[types["upper_case"]].groupby("agency")["rows"].sum()
    return style.fillna(0).astype(int).reset_index().to_dict("records")


def _variant_days(feed: pd.DataFrame, variants: pd.Series) -> dict:
    """Created days on which more than VARIANT_DAY_SHARE of the affected agencies' rows are variants."""
    in_agency = feed["agency"].isin(feed.loc[variants, "agency"].unique())
    daily = variants[in_agency].groupby(feed.loc[in_agency, "created_date"].str[:10]).mean()
    return daily[daily > VARIANT_DAY_SHARE].round(3).to_dict()


def _cross_agency_case(feed: pd.DataFrame) -> list[dict]:
    """Names that several agencies spell in different case, e.g. DOB 'Elevator' vs HPD 'ELEVATOR'."""
    records = []
    for col in CROSS_AGENCY_CASE_COLUMNS:
        values = feed.groupby(["agency", col]).size().rename("rows").reset_index().rename(columns={col: "value"})
        values["name"] = _name_key(values["value"])
        by_name = values.groupby("name")
        shared = values[(by_name["agency"].transform("nunique") > 1) & (by_name["value"].transform("nunique") > 1)]
        for _, spellings in shared.sort_values("rows", ascending=False).groupby("name", sort=False):
            records.append({"column": col, "spellings": {f"{s.agency} {s.value}": s.rows for s in spellings.itertuples()}})
    return records


# --------------------------------------------------------------------------
# 9. Near-duplicates
# --------------------------------------------------------------------------
def profile_duplicates(report: Report, data: Data) -> None:
    feed, near = data.feed, data.flags["near_duplicate_candidate"]
    pairs, max_gap = _near_duplicate_pairs(feed, near), clean.NEAR_DUP_SECONDS
    gap_bins = pd.cut(pairs["gap_s"], [-1, 0, 60, max_gap], labels=["0 s", "1-60 s", f"61-{max_gap} s"])
    report.add("duplicates", "near_duplicate_candidate",
               f"Same agency/type/descriptor/borough/address as a row <= {max_gap} s earlier",
               near, ["created_date", "complaint_type", "descriptor", "incident_address"],
               derived={"earlier_twin": pairs["twin_key"], "gap_s": pairs["gap_s"]},
               details={"gap": gap_bins.value_counts(sort=False).to_dict(),
                        "without_address": int((near & feed["incident_address"].isna()).sum())},
               note="inflates counts; many are legitimate multi-caller reports (same party, encampment, helicopter)")

    respelled_pairs = pairs[_differs(pairs["complaint_type"], pairs["twin_complaint_type"])]
    respelled = pd.Series(feed.index.isin(respelled_pairs.index), index=feed.index)
    twin_entered_later = respelled_pairs["twin_key"] > respelled_pairs["unique_key"]  # keys are issued in entry order
    report.add("duplicates", "near_duplicate_respelled", "Near-duplicate whose earlier twin spells complaint_type in another case",
               respelled, ["created_date", "complaint_type"],
               derived={"earlier_twin": pairs["twin_key"], "twin_complaint_type": pairs["twin_complaint_type"], "gap_s": pairs["gap_s"]},
               details={"created_days": _top(feed.loc[respelled, "created_date"].str[:10]),
                        "twin_entered_later_pct": _pct(twin_entered_later.sum(), len(respelled_pairs))},
               note="one request counted twice: HPD re-entered Sep/Oct requests in Title Case (back-filled, larger key)")

    by_type = pd.DataFrame({"candidate_rows": near.groupby(feed["complaint_type"]).sum(), "rows": feed["complaint_type"].value_counts()})
    by_type["candidate_pct"] = (100 * by_type["candidate_rows"] / by_type["rows"]).round(2)
    report.table("duplicates", "near_duplicates_by_complaint_type",
                 f"Near-duplicate candidates by complaint type (top {NEAR_DUP_TOP_TYPES})",
                 by_type.sort_values("candidate_rows", ascending=False).head(NEAR_DUP_TOP_TYPES).reset_index().to_dict("records"))


def _near_duplicate_pairs(feed: pd.DataFrame, near: pd.Series) -> pd.DataFrame:
    """One row per near-duplicate candidate (same index): its unique_key and complaint_type next
    to those of its earlier twin (clean.previous_report), and the seconds between the two."""
    previous = clean.previous_report(feed).loc[near]
    twin = previous["position"].to_numpy()  # every candidate has an earlier twin
    return feed.loc[near, ["unique_key", "complaint_type"]].assign(
        twin_key=feed["unique_key"].to_numpy()[twin], twin_complaint_type=feed["complaint_type"].to_numpy()[twin],
        gap_s=previous["gap_s"])


# --------------------------------------------------------------------------
# 10. Free text
# --------------------------------------------------------------------------
def profile_text(report: Report, data: Data) -> None:
    feed = data.feed
    text, status = feed["resolution_description"], feed["status"]
    conflicts = clean.status_text_conflicts(feed)
    contradicts, rare = conflicts["contradicts"], conflicts["rare_text"]
    report.add("text", "resolution_contradicts_status",
               f"Resolution text >= {clean.STATUS_TEXT_SHARE:.0%} associated with the other status group",
               contradicts, ["status", "resolution_description"],
               details={"closed_with_open_text": int((contradicts & status.isin(config.CLOSED_STATUSES)).sum()),
                        "open_with_closed_text": int((contradicts & status.isin(config.OPEN_STATUSES)).sum()),
                        "distinct_texts": int(text[contradicts].nunique())},
               note="status and resolution text tell different stories")
    report.add("text", "resolution_text_rare", f"Resolution text seen < {clean.RARE_TEXT_MIN_COUNT} times (too rare to judge)",
               rare, ["status", "resolution_description"], details={"distinct_texts": int(text[rare].nunique())})
    _profile_contact_details(report, data)
    _profile_encoding(report, data)


def _profile_contact_details(report: Report, data: Data) -> None:
    """E-mails / phone numbers in templates (public office numbers, kept) vs in rare texts
    (cannot be told apart from personal data, removed), and name cues."""
    feed, columns, regex = data.feed, config.FREE_TEXT_COLUMNS, clean.CONTACT_REGEX
    in_template, template_texts, rare_texts, rare_matches = pd.Series(False, index=feed.index), {}, {}, Counter()
    for col in columns:
        counts = data.text_counts[col]
        templates = _distinct_matches(counts[counts >= clean.PII_TEMPLATE_MIN_COUNT], regex)
        template_texts[col] = len(templates)
        in_template |= feed[col].isin(templates)
        rare = _distinct_matches(counts[counts < clean.PII_TEMPLATE_MIN_COUNT], regex)
        rare_texts[col] = len(rare)
        for text in rare:
            rare_matches.update({match: counts[text] for match in re.findall(regex, text, flags=re.ASCII)})  # as _search
    report.add("text", "contact_in_template_text", f"E-mail/phone in a template text (>= {clean.PII_TEMPLATE_MIN_COUNT} rows)",
               in_template, [], details={"distinct_texts": template_texts}, note="public office numbers: kept by the sanitiser",
               examples=_fragment_examples(feed, in_template, columns, regex))
    pii = data.flags["pii_like_free_text"]
    report.add("text", "pii_like_free_text", f"E-mail/phone in a rare text (< {clean.PII_TEMPLATE_MIN_COUNT} rows)", pii, [],
               details={"distinct_texts": rare_texts, "matched_rows": dict(rare_matches.most_common(N_EXAMPLES))},
               note="agency referrals to office numbers (IRS, FTC, NYS agencies); a pattern cannot tell them from personal data",
               examples=_fragment_examples(feed, pii, columns, regex))
    names = _rows_matching(data, NAME_CUE_REGEX, columns)
    report.add("text", "name_cue_in_free_text", "Name-cue pattern ('Mr. X', 'tenant First Last', 'contact First Last')", names, [],
               note="matches name organisations ('contact National Grid'), not people",
               examples=_fragment_examples(feed, names, columns, NAME_CUE_REGEX))


def _profile_encoding(report: Report, data: Data) -> None:
    """Mojibake, and texts cut at a storage limit."""
    feed = data.feed
    mojibake = _rows_matching(data, MOJIBAKE_REGEX, TEXT_COLUMNS)
    garbled = {c: _distinct_matches(data.text_counts[c], MOJIBAKE_REGEX) for c in TEXT_COLUMNS}
    repaired = [(c, _repair_mojibake(t)) for c, texts in garbled.items() for t in texts]
    also_correct = [data.text_counts[c][t] for c, t in repaired if t in data.text_counts[c].index]
    report.add("text", "mojibake", "Encoding damage (mojibake): UTF-8 read as Latin-1, e.g. '\\xe2\\x80\\x99' for an apostrophe",
               mojibake, [], details={"distinct_texts": {c: len(t) for c, t in garbled.items() if len(t)},
                                      "texts_also_delivered_correctly": len(also_correct), "rows_of_correct_copies": int(sum(also_correct))},
               note="garbled text on screen; one template can arrive both ways and count as two texts",
               examples=_fragment_examples(feed, mojibake, TEXT_COLUMNS, MOJIBAKE_REGEX, escape=True))

    counts = data.text_counts["resolution_description"]
    cut = counts.index[counts.index.str.len().isin(FIELD_LENGTH_LIMITS) & ~counts.index.str.contains(r"[.!?)\"']$")]
    is_cut = feed["resolution_description"].isin(cut)
    limits = " or ".join(map(str, FIELD_LENGTH_LIMITS))
    report.add("text", "truncated_text", f"Resolution text cut mid-sentence at a field-length limit ({limits} chars)",
               is_cut, [], details={"distinct_texts": len(cut)},
               examples=[{"unique_key": row["unique_key"], "length": len(row["resolution_description"]),
                          "ending": row["resolution_description"][-40:]} for _, row in pick_rows(feed, is_cut).iterrows()])


def _repair_mojibake(text: str) -> str:
    """Undo a UTF-8-read-as-Latin-1 decoding; typographic characters are written in ASCII,
    as the correctly delivered copies of the same templates do."""
    try:
        return text.encode("latin-1").decode("utf-8", errors="ignore").translate(ASCII_PUNCTUATION)
    except UnicodeEncodeError:  # holds characters beyond Latin-1: not this kind of damage
        return text


# --------------------------------------------------------------------------
# 11. What the sanitiser removes
# --------------------------------------------------------------------------
def profile_sanitizer(data: Data) -> dict:
    """What clean.sanitize removes. Counted from the flags rather than by calling sanitize(),
    which would materialise a second ~2 GB copy of the year (tests check the two agree)."""
    removed = data.flags.any(axis=1)
    in_reference = data.feed["created_date"] < config.REFERENCE_END
    return {
        "definitions": clean.ISSUE_DEFINITIONS,
        "removed_by_issue": {issue: int(n) for issue, n in data.flags.sum().items()},
        "rows_removed": int(removed.sum()),
        "rows_removed_pct": _pct(removed.sum(), len(removed)),
        "clean_rows": int((~removed).sum()),
        "reference_clean_rows": int((~removed & in_reference).sum()),
        "evaluation_clean_rows": int((~removed & ~in_reference).sum()),
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
SECTION_PROFILERS = [profile_volume, profile_completeness, profile_dates, profile_status, profile_location,
                     profile_zip, profile_city, profile_categories, profile_duplicates, profile_text]


def load_data(raw: pd.DataFrame | None = None) -> Data:
    """The full raw year (or the given raw frame, e.g. a test sample) and everything shared.
    data/ is not committed: without its manifest (tests profile a committed sample) the month
    table has no expected rows and the time of this run stands in for the download time."""
    # the committed description of the snapshot (same download facts as data/raw/manifest.json, which is not committed)
    manifest = json.loads(config.DATASET_MANIFEST.read_text(encoding="utf-8")) if raw is None and config.DATASET_MANIFEST.exists() else {}
    raw = load_raw() if raw is None else raw
    feed = to_feed(raw)
    not_numeric = {c: int((raw[c].notna() & feed[c].isna()).sum()) for c in NUMERIC_COLUMNS}
    del raw  # the feed shares every text column with it
    dates = {c: _parse(feed[c]) for c in config.DATETIME_COLUMNS}
    downloaded = pd.Timestamp(manifest.get("downloaded_at_utc") or datetime.now(timezone.utc))
    download_time = downloaded.tz_convert(LOCAL_TIMEZONE).tz_localize(None)
    return Data(feed=feed, flags=clean.issue_flags(feed), dates=dates,
                text_counts={c: feed[c].value_counts() for c in TEXT_COLUMNS}, not_numeric=not_numeric,
                manifest=manifest, download_time=download_time, entry_time=_entry_time(feed, dates["created_date"]))


def build_profile(raw: pd.DataFrame | None = None) -> dict:
    started = time.perf_counter()
    data = load_data(raw)
    seconds = {"load_and_flag": round(time.perf_counter() - started, 1)}
    report = Report(data.feed)
    for profile_section in SECTION_PROFILERS:
        t0 = time.perf_counter()
        profile_section(report, data)
        seconds[profile_section.__name__] = round(time.perf_counter() - t0, 1)

    feed = data.feed
    meta = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": config.RAW_FILE.relative_to(config.ROOT).as_posix(),
        "downloaded_at_utc": data.manifest.get("downloaded_at_utc"),
        "rows": len(feed),
        "columns": feed.shape[1],
        "rows_match_manifest": len(feed) == data.manifest.get("total_rows"),
        "created_min": feed["created_date"].min(),
        "created_max": feed["created_date"].max(),
        "runtime_s": round(time.perf_counter() - started, 1),
        "seconds_by_step": seconds,
    }
    profile = {"meta": meta, "summary": SUMMARY, "findings": report.findings, "tables": report.tables,
               "sanitizer": profile_sanitizer(data)}
    return jsonable(profile)


# --------------------------------------------------------------------------
# Markdown rendering (from the JSON only)
# --------------------------------------------------------------------------
def render_markdown(profile: dict) -> str:
    meta, findings = profile["meta"], {f["id"]: f for f in profile["findings"]}
    if meta["downloaded_at_utc"]:
        provenance = (f"(row count {'matches' if meta['rows_match_manifest'] else 'does NOT match'} the download manifest), "
                      f"downloaded {meta['downloaded_at_utc'][:10]}")
    else:
        provenance = "(no download manifest)"
    lines = [
        "## Task 1: data profile of NYC 311, 2023",
        "",
        f"{meta['rows']:,} rows x {meta['columns']} columns created {(meta['created_min'] or '-')[:10]} to "
        f"{(meta['created_max'] or '-')[:10]} {provenance}. "
        f"Generated by `python -m project_b.profile` in {meta['runtime_s']:.0f} s; every number is in "
        "`project_b/results/profile.json`. Percentages are of all rows; \"% of agency\" is the share of that agency's rows.",
        "",
        "### Top findings",
        "",
        "| Finding | Rows | % of rows | Most affected (% of agency) | Why it matters |",
        "|---|---:|---:|---|---|",
    ]
    for f in sorted((findings[fid] for fid in profile["summary"] if findings[fid]["rows"]), key=lambda f: f["rows"], reverse=True):
        lines.append(f"| {_md(f['title'])} | {f['rows']:,} | {_fmt_pct(f['pct'])} | {_fmt_agencies(f['top_agencies'][:1])} | {_md(f['note'])} |")
    for number, (section, heading) in enumerate(SECTIONS.items(), start=1):
        lines += ["", f"### {number}. {heading}", ""]
        lines += _md_section([t for t in profile["tables"] if t["section"] == section],
                             [f for f in profile["findings"] if f["section"] == section])
    lines += _md_sanitizer(profile["sanitizer"], len(SECTIONS) + 1)
    return "\n".join(lines) + "\n"


def _md_section(tables: list[dict], findings: list[dict]) -> list[str]:
    lines = []
    for table in tables:
        lines += [f"*{_md(table['title'])}*", "", *_md_table(table["rows"]), ""]
    found = [f for f in findings if f["rows"]]
    if found:
        lines += ["| Finding | Rows | % of rows | Most affected (% of agency) | Note |", "|---|---:|---:|---|---|"]
        lines += [f"| {_md(f['title'])} | {f['rows']:,} | {_fmt_pct(f['pct'])} | {_fmt_agencies(f['top_agencies'])} | {_md(f['note'])} |"
                  for f in found]
        lines += ["", "Examples (`unique_key` and the offending values) and details:", ""]
        lines += [f"- {_md(f['title'])}: " + "; ".join(_fmt_example(e) for e in f["examples"][:MD_EXAMPLES]) + _fmt_details(f["details"])
                  for f in found]
    for f in (f for f in findings if not f["rows"]):
        lines.append(f"- No rows: {_md(f['title'])}" + (f" ({_md(f['note'])})" if f["note"] else "") + _fmt_details(f["details"]))
    return lines


def _md_sanitizer(s: dict, number: int) -> list[str]:
    total = s["rows_removed"] + s["clean_rows"]
    lines = ["", f"### {number}. Rows the sanitiser removes before injection (`clean.sanitize`)", "",
             "| Issue id | Rows | % of rows | Definition |", "|---|---:|---:|---|"]
    lines += [f"| `{issue}` | {n:,} | {_fmt_pct(_pct(n, total))} | {_md(s['definitions'][issue])} |"
              for issue, n in s["removed_by_issue"].items()]
    lines += ["", f"Rows removed (any issue; a row can have several): {s['rows_removed']:,} ({_fmt_pct(s['rows_removed_pct'])} %). "
              f"Clean pool: {s['clean_rows']:,} rows = reference (created before {config.REFERENCE_END[:10]}) "
              f"{s['reference_clean_rows']:,} + evaluation {s['evaluation_clean_rows']:,}."]
    return lines


def _md_table(records: list[dict]) -> list[str]:
    columns = list(dict.fromkeys(c for r in records for c in r))  # union of keys, first-seen order
    numeric = {c: any(isinstance(r.get(c), (int, float)) and not isinstance(r.get(c), bool) for r in records) for c in columns}
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join("---:" if numeric[c] else "---" for c in columns) + "|"]
    lines += ["| " + " | ".join(_md(_fmt_value(r.get(c))) for c in columns) + " |" for r in records]
    return lines


def _fmt_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return ", ".join(f"{k} ({_fmt_value(v)})" for k, v in list(value.items())[:MD_ITEMS])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f"{value:,}" if isinstance(value, int) else f"{value:,.2f}"


def _fmt_details(details: dict) -> str:
    """' (details: key: a (1), b (2); ...)', empty parts dropped; spelling groups as 'A (n) vs a (m)'."""
    parts = []
    for key, value in details.items():
        if isinstance(value, dict) and value and all(isinstance(v, dict) for v in value.values()):
            value = "; ".join(f"{k}: {_fmt_value(v)}" for k, v in list(value.items())[:MD_ITEMS] if v)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            value = "; ".join(" vs ".join(f"{k} ({_fmt_value(v)})" for k, v in group.items()) for group in value[:MD_ITEMS])
        elif isinstance(value, list):
            value = ", ".join(map(str, value[:MD_ITEMS]))
        else:
            value = _fmt_value(value)
        if value:
            parts.append(f"{key.replace('_', ' ')}: {value}")
    return f" (details: {_md('; '.join(parts))})" if parts else ""


def _fmt_example(example: dict) -> str:
    rest = ", ".join(f"{k}={_md('null' if v is None else v)}" for k, v in example.items() if k != "unique_key")
    return f"`{example['unique_key']}` {rest}".strip()


def _fmt_agencies(groups: list[dict]) -> str:
    return ", ".join(f"{g['agency']} {g['rows']:,} ({_fmt_pct(g['pct_of_agency'])} %)" for g in groups)


def _fmt_pct(pct: float) -> str:
    if pct == 0:
        return "0"
    return "<0.01" if pct < 0.01 else f"{pct:.2f}"


def _md(text) -> str:
    """Markdown-table-safe text: no pipes or newlines, invisible control characters spelled out."""
    text = re.sub(r"[\x80-\x9f]", lambda m: f"\\x{ord(m.group()):02x}", str(text))
    return text.replace("|", "\\|").replace("\n", " ")


JSON_FILE, REPORT_FILE = "profile.json", "profiling_report.md"


def main(argv: list[str] | None = None) -> None:
    profile = build_profile()
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.RESULTS_DIR / JSON_FILE).write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    (config.RESULTS_DIR / REPORT_FILE).write_text(render_markdown(profile), encoding="utf-8")
    print(f"[profile] {profile['meta']['rows']:,} rows in {profile['meta']['runtime_s']} s -> {config.RESULTS_DIR}")


if __name__ == "__main__":
    main()
