"""Pre-existing quality issues in the raw 2023 feed, and the clean pool built from it.

Why: the evaluation injects synthetic corruptions into REAL batches and scores detectors
against the injected ground truth. The real data already contains instances of the same
problems (a ZIP in the wrong borough, a closed date before the created date, ...). A
detector that correctly flags such a row would be charged with a false alarm. So before
injecting, `sanitize` drops every row that would itself count as an instance of a problem
under the injector's ground-truth definition (DESIGN.md section 2).

Frequency-based definitions (rare pairs, template texts) count over the whole feed, so
`feed` must be the full, unsanitised year. Everything is vectorised; text checks run on
distinct values (resolution texts are agency templates: ~800 distinct for 3.2 M rows).
The profiler (project_b/profile.py) reports from the same functions, so the Task 1
numbers and the clean pool cannot disagree.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from project_b import config

# --------------------------------------------------------------------------
# Tunables (DESIGN.md section 2)
# --------------------------------------------------------------------------
RARE_ZIP_BOROUGH_SHARE = 0.02  # a (zip, borough) pair below this share of the ZIP's rows is suspect
RARE_PAIR_MIN_COUNT = 5  # (complaint_type, descriptor) pairs seen fewer times are suspect
STATUS_TEXT_MIN_COUNT = 20  # a resolution text needs this many rows before its status mix counts
STATUS_TEXT_SHARE = 0.95  # ... and conflicts with a status when this share of its rows has the other one
RARE_TEXT_MIN_COUNT = 5  # resolution texts seen fewer times cannot be judged at all
PII_TEMPLATE_MIN_COUNT = 20  # texts this frequent are agency templates (public office numbers)
NEAR_DUP_SECONDS = 300  # max created_date gap between a near-duplicate and its earlier twin
# Spelling noise two entries of the same address differ by (besides case and spacing).
ADDRESS_ABBREVIATIONS = {"STREET": "ST", "AVENUE": "AVE"}

ZIP_REGEX = r"\d{5}"
EMAIL_REGEX = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
PHONE_REGEX = r"(?:\+?1[\s.-]*)?\(?\b\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}\b"  # NANP, any common separator
CONTACT_REGEX = f"{EMAIL_REGEX}|{PHONE_REGEX}"

ISSUE_DEFINITIONS: dict[str, str] = {
    "dup_unique_key": "unique_key already seen earlier (all but the first occurrence)",
    "exact_duplicate_row": "identical to an earlier row in every column (all but the first)",
    "bad_datetime_format": "a non-null date column value does not match the source ISO format or does not parse",
    "closed_before_created": "parsed closed_date is earlier than parsed created_date",
    "latlon_outside_nyc": "non-null latitude or longitude outside the config NYC box (includes swapped pairs)",
    "latlon_half_missing": "exactly one of latitude / longitude is null",
    "zip_invalid_format": "non-null incident_zip that is not exactly 5 digits",
    "zip_borough_rare_pair": (
        f"5-digit ZIP and a borough in config.BOROUGHS whose pair is < {RARE_ZIP_BOROUGH_SHARE:.0%} "
        "of that ZIP's rows (full year)"
    ),
    "complaint_descriptor_rare_pair": (
        f"(complaint_type, descriptor) pair seen < {RARE_PAIR_MIN_COUNT} times in the full year "
        "(a null descriptor counts as a value)"
    ),
    "resolution_status_conflict": (
        f"status is open or closed and its resolution text is seen >= {STATUS_TEXT_MIN_COUNT} times with "
        f">= {STATUS_TEXT_SHARE:.0%} of its rows in the other status group, or seen < {RARE_TEXT_MIN_COUNT} times"
    ),
    "pii_like_free_text": (
        f"a free-text column holds an e-mail or phone-like number and that exact text occurs "
        f"< {PII_TEMPLATE_MIN_COUNT} times (frequent templates with public office numbers are kept)"
    ),
    "near_duplicate_candidate": (
        "same agency, complaint_type and descriptor (any case), borough and normalised incident_address "
        f"as an earlier row created <= {NEAR_DUP_SECONDS} s before (all but the first)"
    ),
}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def issue_flags(feed: pd.DataFrame) -> pd.DataFrame:
    """One boolean column per issue id (ISSUE_DEFINITIONS order), same index as `feed`. Values are
    judged in the contract's dtypes (see _in_contract_dtypes); a missing column raises ValueError."""
    feed = _in_contract_dtypes(feed)
    flags = {issue: np.asarray(_CHECKS[issue](feed), dtype=bool) for issue in ISSUE_DEFINITIONS}
    return pd.DataFrame(flags, index=feed.index)


def sanitize(feed: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Rows without any pre-existing issue (positional index), and the number of rows
    flagged per issue. A row with several issues counts once per issue, so the total
    removed is len(feed) - len(clean), not the sum of the counts."""
    flags = issue_flags(feed)
    removed = {issue: int(n) for issue, n in flags.sum().items()}
    clean = feed.loc[~flags.any(axis=1).to_numpy()].reset_index(drop=True)
    return clean, removed


def status_mix_by_text(feed: pd.DataFrame) -> pd.DataFrame:
    """Per distinct resolution text: rows, and the share of those rows whose status is in
    the closed / open group (rows with another status, e.g. 'Unspecified', count in `rows`
    only). `status_text_conflicts` decides on this table."""
    status = feed["status"]
    per_row = pd.DataFrame(
        {
            "text": feed["resolution_description"],
            "closed": status.isin(config.CLOSED_STATUSES),
            "open": status.isin(config.OPEN_STATUSES),
        }
    )
    return per_row.groupby("text").agg(rows=("closed", "size"), closed_share=("closed", "mean"), open_share=("open", "mean"))


def status_text_conflicts(feed: pd.DataFrame) -> pd.DataFrame:
    """The two halves of `resolution_status_conflict`, for rows whose status is open or closed:
    `contradicts` - the text is seen >= STATUS_TEXT_MIN_COUNT times and >= STATUS_TEXT_SHARE of its
    rows are in the other status group; `rare_text` - the text is seen < RARE_TEXT_MIN_COUNT times,
    too rare to tell which status it goes with."""
    mix = status_mix_by_text(feed)
    supported = mix["rows"] >= STATUS_TEXT_MIN_COUNT
    open_texts = mix.index[supported & (mix["open_share"] >= STATUS_TEXT_SHARE)]
    closed_texts = mix.index[supported & (mix["closed_share"] >= STATUS_TEXT_SHARE)]
    rare_texts = mix.index[mix["rows"] < RARE_TEXT_MIN_COUNT]

    text, status = feed["resolution_description"], feed["status"]
    is_closed, is_open = status.isin(config.CLOSED_STATUSES), status.isin(config.OPEN_STATUSES)
    return pd.DataFrame(
        {
            "contradicts": (is_closed & text.isin(open_texts)) | (is_open & text.isin(closed_texts)),
            "rare_text": (is_closed | is_open) & text.isin(rare_texts),
        },
        index=feed.index,
    )


def previous_report(feed: pd.DataFrame) -> pd.DataFrame:
    """For every row, the latest earlier report of the same request: its position in `feed`
    (`position`, -1 if none) and the seconds since it (`gap_s`, NaN if none).

    A request is (agency, complaint_type, descriptor, borough, normalised incident_address).
    Case is ignored because one request does arrive in two spellings: HPD back-filled its
    Sep/Oct 2023 requests in Title Case next to the upper-case originals. A missing value is
    part of the key: two address-less reports of the same kind seconds apart are
    indistinguishable, so they stay candidates."""
    request = pd.DataFrame(
        {
            "agency": feed["agency"],
            "complaint_type": feed["complaint_type"].str.upper(),
            "descriptor": feed["descriptor"].str.upper(),
            "borough": feed["borough"],
            "address": _normalise_address(feed["incident_address"]),
        }
    )
    timeline = pd.DataFrame(
        {
            "request": request.groupby(list(request.columns), dropna=False, sort=False).ngroup().to_numpy(),
            "created": _parse_dates(feed["created_date"]).to_numpy(),
            "position": np.arange(len(feed)),
        }
    )
    # A multi-column sort is a stable lexsort: rows created in the same second keep their arrival order.
    by_request = timeline.sort_values(["request", "created"]).groupby("request")
    gap_s = by_request["created"].diff().dt.total_seconds()
    # A row whose created_date does not parse sorts last in its request and has no earlier report.
    position = by_request["position"].shift(fill_value=-1).where(gap_s.notna(), -1)
    return pd.DataFrame({"position": position, "gap_s": gap_s}).sort_index().set_axis(feed.index)


def _in_contract_dtypes(feed: pd.DataFrame) -> pd.DataFrame:
    """`feed` with config.FEED_DTYPES, converted the way data.load.to_feed converts raw strings:
    a number that does not parse becomes NaN, anything in a text column becomes its text (so a
    re-typed date fails the format check and a numeric ZIP the 5-digit check). A missing column
    raises instead of being skipped: the sanitiser cannot vouch for rows it cannot check."""
    missing = [col for col in config.COLUMNS if col not in feed.columns]
    if missing:
        raise ValueError(f"feed lacks contract columns {missing}: sanitise the full feed from data.load.to_feed")
    retyped = {}
    for col, dtype in config.FEED_DTYPES.items():
        if dtype == "str" and feed[col].dtype != "str":
            retyped[col] = feed[col].astype("str")
        elif dtype != "str" and not pd.api.types.is_numeric_dtype(feed[col]):
            retyped[col] = pd.to_numeric(feed[col], errors="coerce")
    return feed.assign(**retyped)


# --------------------------------------------------------------------------
# One function per issue: feed -> bool Series aligned with feed
# --------------------------------------------------------------------------
def _parse_dates(values: pd.Series) -> pd.Series:
    """Source-format timestamps; anything else becomes NaT."""
    return pd.to_datetime(values, format=config.SOURCE_DATETIME_FORMAT, errors="coerce")


def _dup_unique_key(feed: pd.DataFrame) -> pd.Series:
    return feed["unique_key"].duplicated(keep="first")


def _exact_duplicate_row(feed: pd.DataFrame) -> pd.Series:
    # Identical rows necessarily share their unique_key, so the expensive all-column
    # comparison only runs on rows whose key repeats (none in the real 2023 data).
    shares_key = feed["unique_key"].duplicated(keep=False)
    flags = pd.Series(False, index=feed.index)
    flags[shares_key] = feed[shares_key].duplicated(keep="first").to_numpy()
    return flags


def _bad_datetime_format(feed: pd.DataFrame) -> pd.Series:
    bad = pd.Series(False, index=feed.index)
    for col in config.DATETIME_COLUMNS:
        values = feed[col]
        well_formed = values.str.fullmatch(config.SOURCE_DATETIME_REGEX) & _parse_dates(values).notna()
        bad |= values.notna() & ~well_formed
    return bad


def _closed_before_created(feed: pd.DataFrame) -> pd.Series:
    return _parse_dates(feed["closed_date"]) < _parse_dates(feed["created_date"])


def _latlon_outside_nyc(feed: pd.DataFrame) -> pd.Series:
    lat, lon = feed["latitude"], feed["longitude"]
    lat_outside = lat.notna() & ~lat.between(config.NYC_LAT_MIN, config.NYC_LAT_MAX)
    lon_outside = lon.notna() & ~lon.between(config.NYC_LON_MIN, config.NYC_LON_MAX)
    return lat_outside | lon_outside


def _latlon_half_missing(feed: pd.DataFrame) -> pd.Series:
    return feed["latitude"].isna() != feed["longitude"].isna()


def _zip_invalid_format(feed: pd.DataFrame) -> pd.Series:
    zips = feed["incident_zip"]
    return zips.notna() & ~zips.str.fullmatch(ZIP_REGEX)


def _zip_borough_rare_pair(feed: pd.DataFrame) -> pd.Series:
    pair_rows = feed.groupby(["incident_zip", "borough"])["borough"].transform("size")
    zip_rows = feed.groupby("incident_zip")["incident_zip"].transform("size")  # every row of the ZIP
    judged = feed["incident_zip"].str.fullmatch(ZIP_REGEX) & feed["borough"].isin(config.BOROUGHS)
    return judged & (pair_rows / zip_rows < RARE_ZIP_BOROUGH_SHARE)


def _complaint_descriptor_rare_pair(feed: pd.DataFrame) -> pd.Series:
    pairs = feed.groupby(["complaint_type", "descriptor"], dropna=False)["complaint_type"]
    return pairs.transform("size") < RARE_PAIR_MIN_COUNT


def _resolution_status_conflict(feed: pd.DataFrame) -> pd.Series:
    return status_text_conflicts(feed).any(axis=1)


def _pii_like_free_text(feed: pd.DataFrame) -> pd.Series:
    flags = pd.Series(False, index=feed.index)
    for col in config.FREE_TEXT_COLUMNS:
        counts = feed[col].value_counts()
        has_contact = counts.index.str.contains(CONTACT_REGEX)
        flags |= feed[col].isin(counts.index[has_contact & (counts < PII_TEMPLATE_MIN_COUNT).to_numpy()])
    return flags


def _normalise_address(address: pd.Series) -> pd.Series:
    """Upper case, single spaces, common abbreviations: '420 East   38 Street' -> '420 EAST 38 ST'."""
    out = address.str.upper().str.replace(r"\s+", " ", regex=True).str.strip()
    for word, abbreviation in ADDRESS_ABBREVIATIONS.items():
        out = out.str.replace(rf"\b{word}\b", abbreviation, regex=True)
    return out


def _near_duplicate_candidate(feed: pd.DataFrame) -> pd.Series:
    return previous_report(feed)["gap_s"] <= NEAR_DUP_SECONDS


_CHECKS = {
    "dup_unique_key": _dup_unique_key,
    "exact_duplicate_row": _exact_duplicate_row,
    "bad_datetime_format": _bad_datetime_format,
    "closed_before_created": _closed_before_created,
    "latlon_outside_nyc": _latlon_outside_nyc,
    "latlon_half_missing": _latlon_half_missing,
    "zip_invalid_format": _zip_invalid_format,
    "zip_borough_rare_pair": _zip_borough_rare_pair,
    "complaint_descriptor_rare_pair": _complaint_descriptor_rare_pair,
    "resolution_status_conflict": _resolution_status_conflict,
    "pii_like_free_text": _pii_like_free_text,
    "near_duplicate_candidate": _near_duplicate_candidate,
}
