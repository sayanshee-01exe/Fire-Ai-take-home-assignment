"""Tests for project_b.profile: the trickier helpers on hand-built data, and one end-to-end
run on the real reference sample (the CLI runs the same code on the full year)."""
from __future__ import annotations

import json

import pandas as pd

from project_b import profile
from project_b.data import clean
from project_b.data.load import to_feed


def test_placeholders_are_whole_values_made_of_filler():
    counts = pd.Series({"N/A": 5, "na na": 1, "00000": 2, " - ": 1, "Unspecified QUEENS": 3, "NANUET": 4, "10001": 9})
    assert sorted(profile._placeholders(counts).index) == [" - ", "00000", "N/A", "Unspecified QUEENS", "na na"]


def test_spelling_variants_keep_the_most_common_spelling():
    city = pd.Series(["BROOKLYN"] * 3 + ["Brooklyn", "brooklyn", "BRONX"])
    variants, groups = profile._spelling_variants(city)
    assert variants.tolist() == [False] * 3 + [True, True, False]
    assert groups == [{"BROOKLYN": 3, "Brooklyn": 1, "brooklyn": 1}]


def test_entry_time_is_the_newest_created_date_among_smaller_keys():
    # Key 3 was issued after key 2 (created 2023-11-16), so it was entered then, not on 2023-09-29.
    feed = pd.DataFrame({"unique_key": [1, 2, 3, 4]})
    created = pd.to_datetime(pd.Series(["2023-09-28", "2023-11-16", "2023-09-29", "2023-11-17"]))
    entered = profile._entry_time(feed, created)
    assert entered.dt.strftime("%m-%d").tolist() == ["09-28", "11-16", "11-16", "11-17"]


def test_near_duplicate_pairs_name_the_earlier_twin():
    row = {"agency": "HPD", "descriptor": "MOLD", "borough": "BRONX", "incident_address": "1 MAIN STREET"}
    feed = pd.DataFrame(
        [
            {**row, "unique_key": 20, "complaint_type": "Unsanitary Condition", "created_date": "2023-10-01T10:00:00.000"},
            {**row, "unique_key": 10, "complaint_type": "UNSANITARY CONDITION", "created_date": "2023-10-01T10:01:30.000"},
        ]
    )
    near = clean.previous_report(feed)["gap_s"] <= clean.NEAR_DUP_SECONDS
    pairs = profile._near_duplicate_pairs(feed, near)
    assert pairs.to_dict("records") == [{"unique_key": 10, "complaint_type": "UNSANITARY CONDITION", "twin_key": 20,
                                         "twin_complaint_type": "Unsanitary Condition", "gap_s": 90.0}]


def test_build_profile_on_the_reference_sample(reference_raw):
    result = profile.build_profile(reference_raw)
    json.dumps(result)  # every number is JSON-serialisable

    findings = {f["id"]: f for f in result["findings"]}
    assert set(result["summary"]) <= set(findings)
    for f in findings.values():
        assert f["section"] in profile.SECTIONS
        assert len(f["examples"]) == min(f["rows"], profile.N_EXAMPLES)

    # The sanitiser section reports exactly what clean.sanitize removes.
    clean_rows, removed = clean.sanitize(to_feed(reference_raw))
    s = result["sanitizer"]
    assert s["removed_by_issue"] == removed and s["clean_rows"] == len(clean_rows)
    assert s["reference_clean_rows"] + s["evaluation_clean_rows"] == s["clean_rows"]

    markdown = profile.render_markdown(result)
    assert "### Top findings" in markdown
    assert all(heading in markdown for heading in profile.SECTIONS.values())
