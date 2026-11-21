"""Unit tests for project_b.data.clean: every issue definition on small hand-built feeds,
plus a smoke test of sanitize on the real reference sample. The last section holds regression
tests for the profiler helpers that report the sanitiser's issues (project_b/profile.py).

Frequency-based issues need context, so each feed starts with `n_base` identical-looking
clean rows (one hour apart, unique keys 1..n_base) and appends the rows under test.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from project_b import config, profile
from project_b.data.clean import (
    CONTACT_REGEX,
    ISSUE_DEFINITIONS,
    issue_flags,
    previous_report,
    sanitize,
    status_mix_by_text,
    status_text_conflicts,
)
from project_b.data.load import to_feed

N_BASE = 30
START = datetime(2023, 3, 1)
BASE_TEXT = "The Police Department responded to the complaint and took action to fix the condition."
BASE_ROW = {
    "agency": "NYPD",
    "agency_name": "New York City Police Department",
    "complaint_type": "Noise - Residential",
    "descriptor": "Loud Music/Party",
    "descriptor_2": None,
    "location_type": "Residential Building/House",
    "incident_zip": "11201",
    "incident_address": "100 MAIN STREET",
    "street_name": "MAIN STREET",
    "city": "BROOKLYN",
    "status": "Closed",
    "due_date": None,
    "resolution_description": BASE_TEXT,
    "resolution_action_updated_date": None,
    "community_board": "02 BROOKLYN",
    "borough": "BROOKLYN",
    "x_coordinate_state_plane": "987000",
    "y_coordinate_state_plane": "193000",
    "open_data_channel_type": "PHONE",
    "park_borough": "BROOKLYN",
    "latitude": "40.6930",
    "longitude": "-73.9900",
}


def ts(hours: float, seconds: int = 0) -> str:
    """Source-format timestamp `hours` (+ `seconds`) after START."""
    return (START + timedelta(hours=hours, seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000")


def make_feed(extra: list[dict] = (), n_base: int = N_BASE) -> pd.DataFrame:
    """`n_base` clean rows one hour apart, then one row per dict in `extra`: a base row
    created in June (far from the base rows) with the given fields overridden."""
    rows = [{**BASE_ROW, "unique_key": str(i + 1), "created_date": ts(i), "closed_date": ts(i + 2)} for i in range(n_base)]
    for j, overrides in enumerate(extra):
        june = 24 * 92 + j  # hours after START: 1 June 2023, one hour apart
        rows.append({**BASE_ROW, "unique_key": str(1000 + j), "created_date": ts(june), "closed_date": ts(june + 2), **overrides})
    return to_feed(pd.DataFrame(rows, columns=config.COLUMNS).astype("str"))


def flagged(feed: pd.DataFrame, issue: str) -> list[int]:
    """Positions of the rows flagged for `issue`."""
    return [int(pos) for pos in np.flatnonzero(issue_flags(feed)[issue].to_numpy())]


def extra_positions(*js: int, n_base: int = N_BASE) -> list[int]:
    return [n_base + j for j in js]


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------
def test_issue_ids_match_design_and_flags_shape():
    assert list(ISSUE_DEFINITIONS) == [
        "dup_unique_key",
        "exact_duplicate_row",
        "bad_datetime_format",
        "closed_before_created",
        "latlon_outside_nyc",
        "latlon_half_missing",
        "zip_invalid_format",
        "zip_borough_rare_pair",
        "complaint_descriptor_rare_pair",
        "resolution_status_conflict",
        "pii_like_free_text",
        "near_duplicate_candidate",
    ]
    feed = make_feed()
    flags = issue_flags(feed)
    assert list(flags.columns) == list(ISSUE_DEFINITIONS)
    assert flags.index.equals(feed.index)
    assert all(dtype == bool for dtype in flags.dtypes)


def test_base_rows_have_no_issues():
    assert not issue_flags(make_feed()).to_numpy().any()


def test_flags_follow_the_feed_index_not_positions():
    feed = make_feed([{"latitude": None}])
    relabelled = feed.set_index(feed.index * 10 + 7)
    flags = issue_flags(relabelled)
    assert flags.index.equals(relabelled.index)
    assert flags.loc[N_BASE * 10 + 7, "latlon_half_missing"]
    assert (flags.to_numpy() == issue_flags(feed).to_numpy()).all()


def test_issue_flags_judges_values_in_the_contract_dtypes():
    # Regression: numbers delivered as strings raised TypeError, a numeric ZIP / an all-null
    # float text column / a parsed date column raised AttributeError (.str on non-strings).
    feed = make_feed([{"latitude": "41.5000"}, {"incident_zip": "N/A"}])
    expected = issue_flags(feed)
    as_text = feed.assign(latitude=feed["latitude"].astype("str"), unique_key=feed["unique_key"].astype("str"))
    pd.testing.assert_frame_equal(issue_flags(as_text), expected)  # same numbers, same verdicts
    retyped = feed.assign(descriptor_2=np.nan, incident_zip=pd.to_numeric(feed["incident_zip"], errors="coerce"),
                          created_date=pd.to_datetime(feed["created_date"]))
    flags = issue_flags(retyped)
    assert flags["zip_invalid_format"].sum() == len(feed) - 1  # '11201.0' is no 5-digit ZIP; 'N/A' became null
    assert flags["bad_datetime_format"].all()  # '2023-03-01 00:00:00' is not the source format
    assert issue_flags(feed.iloc[:0]).shape == (0, len(ISSUE_DEFINITIONS))


def test_issue_flags_names_a_missing_contract_column():
    # A sanitiser that skipped the checks it cannot run would hand dirty rows to the pool.
    with pytest.raises(ValueError, match="descriptor"):
        issue_flags(make_feed().drop(columns=["descriptor"]))


# --------------------------------------------------------------------------
# One test per issue definition
# --------------------------------------------------------------------------
def test_dup_unique_key_flags_all_but_the_first_occurrence():
    feed = make_feed([{"unique_key": "5"}, {"unique_key": "5"}])
    assert flagged(feed, "dup_unique_key") == extra_positions(0, 1)
    assert flagged(feed, "exact_duplicate_row") == []  # created dates differ


def test_exact_duplicate_row_needs_every_column_equal():
    feed = make_feed()
    copy = feed.iloc[[3]]
    same_key_other_values = feed.iloc[[4]].assign(status="Open")
    feed = pd.concat([feed, copy, same_key_other_values], ignore_index=True)
    assert flagged(feed, "exact_duplicate_row") == [N_BASE]
    assert flagged(feed, "dup_unique_key") == [N_BASE, N_BASE + 1]


def test_bad_datetime_format_checks_every_date_column():
    feed = make_feed(
        [
            {"created_date": "03/01/2023 10:00:00 AM"},  # US 12-hour format
            {"closed_date": "2023-02-30T00:00:00.000"},  # ISO shape, impossible date
            {"due_date": "2023-06-05T00:00:00"},  # milliseconds missing
            {"resolution_action_updated_date": "2023-06-01 10:00:00.000"},  # space separator
            {"due_date": None, "closed_date": None},  # nulls are fine
        ]
    )
    assert flagged(feed, "bad_datetime_format") == extra_positions(0, 1, 2, 3)


def test_closed_before_created():
    created = ts(2000)
    feed = make_feed(
        [
            {"created_date": created, "closed_date": ts(2000, seconds=-60)},
            {"created_date": created, "closed_date": created},  # instant closure is allowed
            {"created_date": created, "closed_date": None},
            {"created_date": created, "closed_date": "2022-12-30T09:00:00.000"},
            {"created_date": "not a date", "closed_date": "2022-12-30T09:00:00.000"},  # unparseable: not judged
        ]
    )
    assert flagged(feed, "closed_before_created") == extra_positions(0, 3)


def test_latlon_outside_nyc_includes_swapped_pairs():
    feed = make_feed(
        [
            {"latitude": "-73.9900", "longitude": "40.6930"},  # swapped
            {"latitude": "41.5000"},  # north of the box
            {"longitude": "-75.0000"},  # west of the box
            {"latitude": None, "longitude": None},  # missing is not outside
            {"latitude": str(config.NYC_LAT_MIN), "longitude": str(config.NYC_LON_MAX)},  # on the edge
        ]
    )
    assert flagged(feed, "latlon_outside_nyc") == extra_positions(0, 1, 2)


def test_latlon_half_missing():
    feed = make_feed([{"latitude": None}, {"longitude": None}, {"latitude": None, "longitude": None}])
    assert flagged(feed, "latlon_half_missing") == extra_positions(0, 1)


def test_zip_invalid_format():
    feed = make_feed([{"incident_zip": z} for z in ["N/A", "1120", "11201-1234", " 11201", None, "00083"]])
    assert flagged(feed, "zip_invalid_format") == extra_positions(0, 1, 2, 3)


def test_zip_borough_rare_pair_is_a_share_of_all_the_zips_rows():
    # 49 BROOKLYN rows + 1 QUEENS row: the pair is 1/50 = 2 % of ZIP 11201 -> not rare.
    queens = {"borough": "QUEENS"}
    assert flagged(make_feed([queens], n_base=49), "zip_borough_rare_pair") == []
    # One more row of the ZIP, even with an 'Unspecified' borough, makes it 1/51 < 2 % -> rare.
    # The 'Unspecified' row and a row with an invalid ZIP are themselves never judged.
    feed = make_feed([queens, {"borough": "Unspecified"}, {"borough": "QUEENS", "incident_zip": "N/A"}], n_base=49)
    assert flagged(feed, "zip_borough_rare_pair") == extra_positions(0, n_base=49)


def test_zip_borough_rare_pair_keeps_balanced_boundary_zips():
    extra = [{"incident_zip": "11385", "borough": b} for b in ["BROOKLYN", "QUEENS"] * 10]
    assert flagged(make_feed(extra), "zip_borough_rare_pair") == []


def test_complaint_descriptor_rare_pair_counts_the_full_feed():
    rare = [{"descriptor": "Loud Talking"}] * 4
    frequent_enough = [{"descriptor": "Banging/Pounding"}] * 5
    feed = make_feed(rare + frequent_enough + [{"descriptor": None}])
    assert flagged(feed, "complaint_descriptor_rare_pair") == extra_positions(0, 1, 2, 3, 9)


def test_status_mix_by_text_counts_every_status_in_the_denominator():
    feed = make_feed([{"status": "Open"}, {"status": "Unspecified"}], n_base=8)
    mix = status_mix_by_text(feed).loc[BASE_TEXT]
    assert (mix["rows"], mix["closed_share"], mix["open_share"]) == (10, 0.8, 0.1)


def test_resolution_status_conflict():
    open_text = "The Department of Transportation referred this complaint to the Inspections Unit for further action."
    mixed_text = "The Department of Sanitation found an open service request already exists for the same location."
    rare_text = "The complaint was resolved in an unusual way."
    extra = (
        [{"status": "In Progress", "resolution_description": open_text}] * 20  # 0-19: open template
        + [{"status": "Closed", "resolution_description": open_text}]  # 20: closed with a 95 %-open text
        + [{"status": "Open", "resolution_description": BASE_TEXT}]  # 21: open with a 97 %-closed text
        + [{"status": "Closed", "resolution_description": rare_text}]  # 22: text seen twice: rare
        + [{"status": "Unspecified", "resolution_description": rare_text}]  # 23: status in no group
        + [{"status": "Open", "resolution_description": None}]  # 24: no text, nothing to contradict
        + [{"status": s, "resolution_description": mixed_text} for s in ["Closed", "Open"] * 10]  # 25-44: mixed
    )
    feed = make_feed(extra)
    assert flagged(feed, "resolution_status_conflict") == extra_positions(20, 21, 22)
    halves = status_text_conflicts(feed)
    assert np.flatnonzero(halves["contradicts"]).tolist() == extra_positions(20, 21)
    assert np.flatnonzero(halves["rare_text"]).tolist() == extra_positions(22)


def test_pii_like_free_text_keeps_frequent_templates():
    template = "If you are outside of New York City, please call (212) NEW-YORK (212-639-9675)."
    feed = make_feed(
        [{"resolution_description": template}] * 20  # 0-19: frequent template with a public number
        + [
            {"resolution_description": "Spoke with the tenant, call back at 212.555.0147."},  # 20
            {"descriptor_2": "Contact jane.doe@example.com"},  # 21
            {"descriptor": "Reported by +1 (718) 555 0199"},  # 22
            {"resolution_description": "Please call 311 for further information."},  # 23: 311 is not a phone number
        ]
    )
    assert flagged(feed, "pii_like_free_text") == extra_positions(20, 21, 22)


def test_near_duplicate_candidate():
    def twin_of(i: int, seconds: int, **overrides) -> dict:
        return {"created_date": ts(i, seconds), "closed_date": ts(i + 2), **overrides}

    feed = make_feed(
        [
            twin_of(0, 120, incident_address="100  Main St", descriptor="LOUD MUSIC/PARTY"),  # 0: spelling noise only
            twin_of(1, 400),  # 1: too late
            twin_of(2, 60, borough="QUEENS"),  # 2: other borough
            twin_of(3, 0),  # 3: same second as base row 3, arrives after it
            {"incident_address": None, "created_date": ts(3000)},  # 4: no address ...
            {"incident_address": None, "created_date": ts(3000, 10)},  # 5: ... same kind, 10 s later
            twin_of(4, 90, complaint_type="NOISE - RESIDENTIAL"),  # 6: the type in another case
            twin_of(5, 30, complaint_type="Noise - Street/Sidewalk"),  # 7: another type
        ]
    )
    assert flagged(feed, "near_duplicate_candidate") == extra_positions(0, 3, 5, 6)


def test_previous_report_is_the_latest_earlier_twin_in_time():
    # Base row at 0 s, then twins arriving out of time order: +90 s, then +30 s.
    feed = make_feed([{"created_date": ts(0, 90)}, {"created_date": ts(0, 30)}], n_base=1)
    previous = previous_report(feed)
    assert previous["position"].tolist() == [-1, 2, 0]
    assert np.isnan(previous.loc[0, "gap_s"]) and previous.loc[1, "gap_s"] == 60 and previous.loc[2, "gap_s"] == 30


def test_previous_report_row_without_a_parsed_created_date_has_none():
    # Regression: the unparseable row sorts last in its request and was given the row before it
    # as "previous report" (position 1) while its gap was NaN.
    feed = make_feed([{"created_date": ts(0, 60)}, {"created_date": "not a date"}], n_base=1)
    previous = previous_report(feed)
    assert previous["position"].tolist() == [-1, 0, -1]
    assert previous.loc[1, "gap_s"] == 60 and np.isnan(previous.loc[2, "gap_s"])


# --------------------------------------------------------------------------
# sanitize
# --------------------------------------------------------------------------
def test_sanitize_drops_flagged_rows_and_counts_per_issue():
    feed = make_feed([{"latitude": None}, {"incident_zip": "N/A", "latitude": None}, {"unique_key": "7"}])
    before = feed.copy()
    clean, removed = sanitize(feed)
    pd.testing.assert_frame_equal(feed, before)  # input untouched
    assert removed["latlon_half_missing"] == 2 and removed["zip_invalid_format"] == 1 and removed["dup_unique_key"] == 1
    assert sum(removed.values()) == 4  # the second extra row has two issues ...
    assert len(clean) == len(feed) - 3  # ... but is removed once
    assert clean.index.equals(pd.RangeIndex(len(clean)))
    assert (clean.dtypes == feed.dtypes).all()


def test_sanitize_smoke_on_reference_feed(reference_feed):
    clean, removed = sanitize(reference_feed)
    assert list(removed) == list(ISSUE_DEFINITIONS)
    assert 0 < len(clean) < len(reference_feed)
    assert len(reference_feed) - len(clean) <= sum(removed.values())
    assert list(clean.columns) == list(reference_feed.columns)
    # Row-level issues cannot reappear in a subset (frequency-based ones can, by design).
    row_level = ["dup_unique_key", "exact_duplicate_row", "bad_datetime_format", "closed_before_created",
                 "latlon_outside_nyc", "latlon_half_missing", "zip_invalid_format"]
    assert not issue_flags(clean)[row_level].to_numpy().any()


# --------------------------------------------------------------------------
# Profiler helpers (project_b/profile.py) - regression tests
# --------------------------------------------------------------------------
def test_profile_contact_examples_agree_with_pandas_on_non_ascii_text():
    # Regression: pandas matches `str` columns with RE2 (ASCII \b), Python's re did not, so the
    # example extractor found no match in a row pandas had flagged and raised StopIteration.
    feed = pd.DataFrame({"unique_key": [1], "descriptor": [None], "descriptor_2": [None],
                         "resolution_description": ["Call Ñ212-639-9675 for the status."]})
    assert feed["resolution_description"].str.contains(CONTACT_REGEX).all()
    [example] = profile._fragment_examples(feed, pd.Series([True]), config.FREE_TEXT_COLUMNS, CONTACT_REGEX)
    assert example["column"] == "resolution_description" and "212-639-9675" in example["fragment"]


def test_profile_state_plane_fit_needs_geocoded_rows_off_one_point(reference_feed):
    # Regression: rows that all sit at one point (or a sample with < 3 geocoded rows) made the
    # normal equations singular and the whole profile raised LinAlgError.
    assert profile._state_plane_gap_m(make_feed(n_base=2)).isna().all()
    assert profile._state_plane_gap_m(reference_feed).median() < 50  # the real fit still works (metres)


def test_profile_differs_treats_two_missing_values_as_equal():
    # Regression: `a != b` is True for NaN vs NaN, which would count two missing boroughs as "differs".
    a, b = pd.Series(["QUEENS", None, "BRONX", None]), pd.Series(["QUEENS", None, "BROOKLYN", "BRONX"])
    assert profile._differs(a, b).tolist() == [False, False, True, True]


def test_profile_runs_without_the_download_manifest(monkeypatch, tmp_path, reference_raw):
    # Regression: data/ is not committed, yet the profile of a committed sample required
    # data/raw/manifest.json (FileNotFoundError); an empty sample also crashed the renderer.
    monkeypatch.setattr(config, "RAW_MANIFEST", tmp_path / "missing.json")
    for n_rows in (0, 1, 2_000):
        result = profile.build_profile(reference_raw.iloc[:n_rows].reset_index(drop=True))
        assert result["meta"]["downloaded_at_utc"] is None
        assert "(no download manifest)" in profile.render_markdown(result)
    months = next(t for t in result["tables"] if t["id"] == "rows_per_month")["rows"]
    assert sum(m["rows"] for m in months) == 2_000 and all(m["manifest_rows"] is None for m in months)
