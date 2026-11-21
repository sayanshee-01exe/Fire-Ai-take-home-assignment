"""Tests for project_b/detectors/location.py: problems d (lat/lon swapped) and i (ZIP vs borough).

Blue team: corruptions are hand-made here from DESIGN.md section 3, never taken from inject.py.
Measured rates and timings are printed: python -m pytest tests/test_detectors_location.py -s
"""
from __future__ import annotations

import math
import time
import warnings

import numpy as np
import pandas as pd
import pytest

from project_b import config, llm
from project_b.detectors import location
from project_b.detectors.location import (
    LlmCoordinateReview,
    LlmZipBoroughReview,
    RuleNycBbox,
    RuleZipPrefixBorough,
    StatCoordinateOutlier,
    StatZipBoroughFrequency,
)
from tests.fakes import FakeClient, rows_in_prompt

STATE_PLANE = ["x_coordinate_state_plane", "y_coordinate_state_plane"]
LOCATION_COLUMNS = ["latitude", "longitude", *STATE_PLANE, "incident_zip", "borough", "city", "incident_address"]
# A borough that shares no ZIP with the key borough (not adjacent), and one ZIP used only by each borough.
FAR_BOROUGH = {
    "MANHATTAN": "STATEN ISLAND",
    "BRONX": "STATEN ISLAND",
    "BROOKLYN": "BRONX",
    "QUEENS": "STATEN ISLAND",
    "STATEN ISLAND": "BRONX",
}
ZIP_OF = {"MANHATTAN": "10025", "BRONX": "10467", "BROOKLYN": "11226", "QUEENS": "11368", "STATEN ISLAND": "10314"}
BOROUGH_OF = {zip_code: borough for borough, zip_code in ZIP_OF.items()}


# ------------------------------------------------------------------------------------------
# hand-made corruptions
# ------------------------------------------------------------------------------------------
def _pick(eligible: np.ndarray, n_rows: int, seed: int) -> np.ndarray:
    """config.ROW_INJECTION_RATE of the batch, drawn from the eligible positions."""
    size = min(len(eligible), max(1, round(config.ROW_INJECTION_RATE * n_rows)))
    return np.random.default_rng(seed).choice(eligible, size=size, replace=False)


def _labels(n_rows: int, rows: np.ndarray) -> np.ndarray:
    labels = np.zeros(n_rows, dtype=bool)
    labels[rows] = True
    return labels


def swap_lat_lon(batch: pd.DataFrame, seed: int = 0) -> tuple[pd.DataFrame, np.ndarray]:
    df = batch.copy()
    rows = _pick(np.flatnonzero(df["latitude"].notna() & df["longitude"].notna()), len(df), seed)
    df.loc[rows, ["latitude", "longitude"]] = df.loc[rows, ["longitude", "latitude"]].to_numpy()
    return df, _labels(len(df), rows)


def mismatch_zip_borough(batch: pd.DataFrame, variant: str, seed: int = 0) -> tuple[pd.DataFrame, np.ndarray]:
    """'borough': the borough becomes one that shares no ZIP with it; 'zip': the ZIP becomes a ZIP of such a borough."""
    df = batch.copy()
    eligible = df["incident_zip"].str.fullmatch(r"\d{5}") & df["borough"].isin(config.BOROUGHS)
    rows = _pick(np.flatnonzero(eligible), len(df), seed)
    far = df.loc[rows, "borough"].map(FAR_BOROUGH)
    if variant == "borough":
        df.loc[rows, "borough"] = far.to_numpy()
    else:
        df.loc[rows, "incident_zip"] = far.map(ZIP_OF).to_numpy()
    return df, _labels(len(df), rows)


def inserted_order(n_rows: int) -> np.ndarray:
    """Positions of a batch whose rows 100-199 arrive again halfway through. `batch.iloc[order]` keeps the old
    index labels, so labels repeat and stop being positions."""
    half = n_rows // 2
    return np.r_[0:half, 100 : min(200, n_rows), half:n_rows]


def _numbers_partly_strings(df: pd.DataFrame) -> pd.DataFrame:
    """Second half of the coordinate values arrive as strings (object dtype)."""
    out = df.copy()
    for column in ["latitude", "longitude"]:
        values = out[column].astype(object)
        values.iloc[len(out) // 2 :] = values.iloc[len(out) // 2 :].astype(str)
        out[column] = values
    return out


RENAMES = {"latitude": "lat", "longitude": "Longitude", "incident_zip": "zip_code", "borough": "boro", "x_coordinate_state_plane": "X Coordinate"}
GARBAGE = np.array(["N/A", "", "40,7", "abc"], dtype=object)
CORRUPTIONS = {
    "location_columns_dropped": lambda df: df.drop(columns=LOCATION_COLUMNS),
    "location_columns_renamed": lambda df: df.rename(columns=RENAMES),
    "numbers_as_strings": lambda df: df.astype({c: "str" for c in ["latitude", "longitude", *STATE_PLANE]}),
    "numbers_partly_strings": _numbers_partly_strings,
    "garbage_in_numeric_columns": lambda df: df.assign(latitude=np.resize(GARBAGE, len(df))),
    "zip_as_number": lambda df: df.assign(incident_zip=pd.to_numeric(df["incident_zip"], errors="coerce")),
    "dates_reformatted": lambda df: df.assign(
        created_date=pd.to_datetime(df["created_date"]).dt.strftime("%m/%d/%Y %I:%M:%S %p")
    ),
    "duplicate_rows_inserted": lambda df: pd.concat([df, df.iloc[::10]], ignore_index=True),
    "rows_inserted_index_not_reset": lambda df: df.iloc[inserted_order(len(df))],
    "one_row_batch": lambda df: df.iloc[:1],
    "empty_batch": lambda df: df.iloc[:0],
    "all_location_values_missing": lambda df: df.assign(**{c: np.nan for c in LOCATION_COLUMNS}),
    "duplicated_column_name": lambda df: df.rename(columns={"park_borough": "borough"}),
}


def rates(result, labels: np.ndarray) -> tuple[float, float]:
    """(recall on the corrupted rows, false-positive rate on the other rows)."""
    return result.row_flags[labels].mean(), result.row_flags[~labels].mean()


# ------------------------------------------------------------------------------------------
# fixtures and fake LLM answers
# ------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fitted(reference_feed) -> dict:
    """Rule and stat detectors, fitted once on the reference fixture, by name."""
    classes = (RuleNycBbox, StatCoordinateOutlier, RuleZipPrefixBorough, StatZipBoroughFrequency)
    return {cls.name: cls().fit(reference_feed) for cls in classes}


@pytest.fixture()
def fake_llm():
    """Installs a FakeClient for one test; the real client is always restored afterwards."""

    def install(responder=None, unavailable: bool = False) -> FakeClient:
        client = FakeClient(responder, unavailable=unavailable)
        llm.set_client(client)
        return client

    yield install
    llm.set_client(None)


def _verdict(record: dict, flagged: bool) -> dict:
    """A verdict whose texts name the row id, so a test can tell where each one landed."""
    row = record["id"]
    texts = {key: f"{key} {row}" for key in ("reason", "observed", "expected")}
    return {"id": row, "flagged": flagged, **texts, "confidence": 0.9}


def answer_all_clean(prompt, schema):
    return {"rows": [_verdict(r, False) for r in rows_in_prompt(prompt)]}


def coordinate_responder(prompt, schema):
    """Fake model: a negative latitude means swapped. Answers in reverse order, so mapping must use ids."""
    rows = reversed(rows_in_prompt(prompt))
    return {"rows": [_verdict(r, isinstance(r["latitude"], float) and r["latitude"] < 0) for r in rows]}


def zip_borough_responder(prompt, schema):
    """Fake model: knows the borough of the ZIP_OF ZIPs only. Answers in reverse order."""
    rows = reversed(rows_in_prompt(prompt))
    return {"rows": [_verdict(r, BOROUGH_OF.get(r["incident_zip"], r["borough"]) != r["borough"]) for r in rows]}


# ------------------------------------------------------------------------------------------
# module contract
# ------------------------------------------------------------------------------------------
def test_module_lists_one_detector_per_problem_and_family():
    expected = [("d", "rule"), ("d", "stat"), ("d", "llm"), ("i", "rule"), ("i", "stat"), ("i", "llm")]
    assert [(cls.problem, cls.family) for cls in location.DETECTORS] == expected
    for cls in location.DETECTORS:
        assert cls.name.startswith(f"{cls.family}_") and cls.description


@pytest.mark.parametrize("name", ["rule_nyc_bbox", "stat_coordinate_outlier", "rule_zip_prefix_borough", "stat_zip_borough_frequency"])
def test_few_false_alarms_on_the_untouched_batch(name, fitted, batch_feed):
    result = fitted[name].detect(batch_feed)
    print(f"\n{name} on the untouched batch: {result.row_flags.sum()} rows flagged ({result.row_flags.mean():.2%})")
    for position in list(result.row_reasons)[:3]:
        print(f"  row {position}: {result.row_reasons[position]}")
    assert result.row_flags.mean() < 0.005
    assert result.batch_flag == bool(result.alerts) and len(result.alerts) <= 1


@pytest.mark.parametrize("cls", [RuleNycBbox, StatCoordinateOutlier, RuleZipPrefixBorough, StatZipBoroughFrequency], ids=lambda c: c.name)
def test_fit_and_detect_are_fast(cls, reference_feed, batch_feed):
    start = time.perf_counter()
    detector = cls().fit(reference_feed)
    fit_s = time.perf_counter() - start
    start = time.perf_counter()
    detector.detect(batch_feed)
    detect_s = time.perf_counter() - start
    print(f"\n{cls.name}: fit {fit_s * 1e3:.1f} ms on {len(reference_feed):,} rows, detect {detect_s * 1e3:.1f} ms on {len(batch_feed):,} rows")
    assert detect_s < 1.0  # DESIGN target for rule/stat detect() on a 5,000-row batch


@pytest.mark.parametrize("cls", [StatCoordinateOutlier, StatZipBoroughFrequency], ids=lambda c: c.name)
def test_fit_on_ten_times_the_reference_projects_under_a_minute_at_full_size(cls, reference_feed):
    big = pd.concat([reference_feed] * 10, ignore_index=True)
    start = time.perf_counter()
    cls().fit(big)
    projected_s = (time.perf_counter() - start) * 2_100_000 / len(big)
    print(f"\n{cls.name}: fit on {len(big):,} rows projects to {projected_s:.1f} s on 2.1 M rows")
    assert projected_s < 60


@pytest.mark.parametrize("name", ["rule_zip_prefix_borough", "stat_zip_borough_frequency"])
def test_a_batch_flagged_almost_everywhere_stays_fast(name, fitted, batch_feed):
    """Regression: the per-row reasons read ZIP and borough with pandas `.iloc` (~50 us a value), so a batch whose
    rows were nearly all flagged took ~0.6 s of the 1 s detect() budget; numpy arrays make it ~0.01 s."""
    corrupted = batch_feed.assign(borough=batch_feed["borough"].map(FAR_BOROUGH).fillna(batch_feed["borough"]))
    start = time.perf_counter()
    result = fitted[name].detect(corrupted)
    detect_s = time.perf_counter() - start
    print(f"\n{name}: {result.row_flags.sum():,} of {len(corrupted):,} rows flagged in {detect_s * 1e3:.1f} ms")
    assert result.row_flags.mean() > 0.9 and len(result.row_reasons) == result.row_flags.sum()
    assert detect_s < 0.25


# ------------------------------------------------------------------------------------------
# d - latitude / longitude swapped
# ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["rule_nyc_bbox", "stat_coordinate_outlier"])
def test_swapped_rows_are_all_flagged_and_explained(name, fitted, batch_feed):
    corrupted, labels = swap_lat_lon(batch_feed)
    result = fitted[name].detect(corrupted)
    recall, fpr = rates(result, labels)
    print(f"\n{name}: recall {recall:.3f} on {labels.sum()} swapped rows, FPR {fpr:.4f} on the others")
    assert recall == 1.0 and fpr < 0.005
    assert all("swap" in result.row_reasons[p] for p in np.flatnonzero(labels))
    [alert] = result.alerts
    assert alert.check_id in ("R-d1", "S-d1") and alert.n_rows == result.row_flags.sum()
    assert len(alert.rows) == location.MAX_EXAMPLE_ROWS and alert.observed and alert.expected and alert.score > 0


def test_rule_bbox_says_outside_rather_than_swapped_when_reversing_does_not_help(batch_feed):
    df = batch_feed.head(3).copy()
    df.loc[0, ["latitude", "longitude"]] = [42.65, -73.75]  # Albany: outside NYC, not a swap
    df.loc[1, ["latitude", "longitude"]] = [-73.95, np.nan]  # half missing: nothing to check
    df.loc[2, ["latitude", "longitude"]] = [np.nan, np.nan]
    result = RuleNycBbox().detect(df)
    assert result.row_flags.tolist() == [True, False, False]
    assert "outside NYC" in result.row_reasons[0] and "swapped" not in result.row_reasons[0]


def test_stat_coordinate_thresholds_hold_the_target_fpr_on_reference(fitted, reference_feed):
    detector = fitted["stat_coordinate_outlier"]
    print(f"\nstat_coordinate_outlier thresholds: {detector.threshold_}")
    assert 0 < detector.threshold_["xy_km"] < 1  # x/y explain lat/lon to well under a kilometre
    assert detector.detect(reference_feed).row_flags.mean() <= config.TARGET_ROW_FPR
    assert detector.detect(reference_feed.drop(columns=STATE_PLANE)).row_flags.mean() <= config.TARGET_ROW_FPR


def test_stat_coordinate_falls_back_to_robust_z_without_state_plane(fitted, batch_feed):
    corrupted, labels = swap_lat_lon(batch_feed.drop(columns=STATE_PLANE))
    result = fitted["stat_coordinate_outlier"].detect(corrupted)
    recall, fpr = rates(result, labels)
    print(f"\nstat_coordinate_outlier without x/y: recall {recall:.3f}, FPR {fpr:.4f}")
    assert recall == 1.0 and fpr < 0.005
    assert all("robust SDs" in result.row_reasons[p] for p in np.flatnonzero(labels))


def test_stat_coordinate_uses_state_plane_as_an_independent_witness(fitted, batch_feed):
    """A 5 km shift that stays inside the city is invisible to the box but not to x/y."""
    df = batch_feed.copy()
    row = int(np.flatnonzero(df["latitude"].between(40.6, 40.8) & df[STATE_PLANE].notna().all(axis=1))[0])
    df.loc[row, "latitude"] += 0.05
    assert fitted["stat_coordinate_outlier"].detect(df).row_flags[row]
    assert not fitted["rule_nyc_bbox"].detect(df).row_flags[row]


def test_coordinates_delivered_as_strings_are_still_checked(fitted, batch_feed):
    corrupted, labels = swap_lat_lon(batch_feed)
    as_text = CORRUPTIONS["numbers_as_strings"](corrupted)
    for name in ("rule_nyc_bbox", "stat_coordinate_outlier"):
        assert fitted[name].detect(as_text).row_flags[labels].all()


def test_rows_without_coordinates_are_never_flagged(fitted, batch_feed):
    corrupted, _ = swap_lat_lon(batch_feed)
    corrupted["longitude"] = np.nan  # x/y are still there, but there is no pair to judge
    for name in ("rule_nyc_bbox", "stat_coordinate_outlier"):
        assert not fitted[name].detect(corrupted).row_flags.any()


def test_an_infinite_coordinate_is_outside_the_city(fitted, batch_feed):
    """Regression: the box rule skipped non-finite values, so 'inf' delivered as text (coerced to inf) passed it."""
    df = batch_feed.head(3).astype({"latitude": "str"})
    df.loc[0, "latitude"] = "inf"
    result = fitted["rule_nyc_bbox"].detect(df)
    assert result.row_flags.tolist() == [True, False, False] and "outside NYC" in result.row_reasons[0]
    assert fitted["stat_coordinate_outlier"].detect(df).row_flags.tolist() == [True, False, False]


@pytest.mark.parametrize("x, y", [(np.inf, np.inf), (-np.inf, np.inf), (np.inf, np.nan)])
def test_infinite_state_plane_values_count_as_missing(x, y, fitted, batch_feed):
    """Regression: an infinite x and y made the x/y map compute inf - inf inside detect() (a RuntimeWarning, so an
    exception under -W error). Unusable x/y must behave exactly like absent x/y: the robust z-score decides."""
    detector = fitted["stat_coordinate_outlier"]
    corrupted, labels = swap_lat_lon(batch_feed)
    without_xy = detector.detect(corrupted.drop(columns=STATE_PLANE))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = detector.detect(corrupted.assign(x_coordinate_state_plane=x, y_coordinate_state_plane=y))
    assert np.array_equal(result.row_flags, without_xy.row_flags) and result.row_flags[labels].all()
    assert np.array_equal(result.row_scores, without_xy.row_scores)
    assert result.row_reasons == without_xy.row_reasons and result.alerts[0].columns == ["latitude", "longitude"]


@pytest.mark.parametrize("name", ["rule_nyc_bbox", "stat_coordinate_outlier", "rule_zip_prefix_borough", "stat_zip_borough_frequency"])
def test_flags_follow_batch_positions_when_the_index_is_not(name, fitted, batch_feed):
    """Rows inserted mid-batch without an index reset must not shift flags or reasons onto other rows."""
    detector = fitted[name]
    corrupted, _ = swap_lat_lon(batch_feed) if detector.problem == "d" else mismatch_zip_borough(batch_feed, "borough")
    order = inserted_order(len(corrupted))
    expected = detector.detect(corrupted).row_flags[order]
    result = detector.detect(corrupted.iloc[order])
    assert expected.sum() > 0 and np.array_equal(result.row_flags, expected)
    assert set(result.row_reasons) == set(np.flatnonzero(expected).tolist())


# ------------------------------------------------------------------------------------------
# i - ZIP code inconsistent with borough
# ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["borough", "zip"])
@pytest.mark.parametrize("name", ["rule_zip_prefix_borough", "stat_zip_borough_frequency"])
def test_zip_borough_mismatches_are_flagged(name, variant, fitted, batch_feed):
    corrupted, labels = mismatch_zip_borough(batch_feed, variant)
    result = fitted[name].detect(corrupted)
    recall, fpr = rates(result, labels)
    print(f"\n{name} ({variant} replaced): recall {recall:.3f} on {labels.sum()} rows, FPR {fpr:.4f} on the others")
    # the stat detector does not judge ZIPs with fewer than MIN_ZIP_ROWS reference rows
    assert recall >= (1.0 if name.startswith("rule") else 0.9) and fpr < 0.005
    [alert] = result.alerts
    assert alert.check_id in ("R-i1", "S-i1") and alert.columns == ["incident_zip", "borough"]
    assert alert.n_rows == result.row_flags.sum() and alert.observed and alert.expected


@pytest.mark.parametrize("name", ["rule_zip_prefix_borough", "stat_zip_borough_frequency"])
def test_zip_delivered_as_numbers_is_still_checked(name, fitted, batch_feed):
    """Regression: a float ZIP column reads '11201.0' as text, which made every row uncheckable (recall 0)."""
    corrupted, labels = mismatch_zip_borough(batch_feed, "zip")
    as_text = fitted[name].detect(corrupted)
    as_numbers = fitted[name].detect(CORRUPTIONS["zip_as_number"](corrupted))
    recall, fpr = rates(as_numbers, labels)
    assert recall >= 0.9 and fpr < 0.005
    assert np.array_equal(as_numbers.row_flags[labels], as_text.row_flags[labels])
    first = int(np.flatnonzero(as_numbers.row_flags)[0])
    assert f"ZIP {corrupted['incident_zip'].iloc[first]} " in as_numbers.row_reasons[first]  # the 5-digit ZIP, not 10314.0


def test_rule_zip_prefix_flags_a_zip_outside_new_york_city(batch_feed):
    df = batch_feed.head(2).copy()
    df["incident_zip"], df["borough"] = ["07030", "11201"], ["MANHATTAN", "BROOKLYN"]
    result = RuleZipPrefixBorough().detect(df)
    assert result.row_flags.tolist() == [True, False]
    assert "not a New York City ZIP" in result.row_reasons[0]


@pytest.mark.parametrize("name", ["rule_zip_prefix_borough", "stat_zip_borough_frequency"])
def test_invalid_zips_and_unspecified_boroughs_are_skipped(name, fitted, batch_feed):
    df = batch_feed.head(5).copy()
    df["incident_zip"] = ["1120", "ABCDE", "10467", None, "10467"]
    df["borough"] = ["BRONX", "BRONX", "Unspecified", "BROOKLYN", None]
    assert not fitted[name].detect(df).row_flags.any()


def test_stat_zip_borough_threshold_holds_the_target_fpr_on_reference(fitted, reference_feed):
    detector = fitted["stat_zip_borough_frequency"]
    print(f"\nstat_zip_borough_frequency threshold (relative likelihood): {detector.threshold_}")
    assert 0 < detector.threshold_ <= 1
    assert detector.detect(reference_feed).row_flags.mean() <= config.TARGET_ROW_FPR


def test_stat_zip_borough_only_judges_zips_with_enough_reference_rows(fitted, batch_feed):
    df = batch_feed.head(2).copy()
    df["incident_zip"], df["borough"] = ["99999", "10467"], ["BRONX", "STATEN ISLAND"]
    result = fitted["stat_zip_borough_frequency"].detect(df)
    assert result.row_flags.tolist() == [False, True]
    assert "usually BRONX" in result.row_reasons[1]


# ------------------------------------------------------------------------------------------
# LLM detectors (FakeClient: no network)
# ------------------------------------------------------------------------------------------
LLM_CASES = {  # problem -> (detector, hand-made corruption, fake model, the only columns the prompt may hold)
    "d": (
        LlmCoordinateReview,
        swap_lat_lon,
        coordinate_responder,
        {"borough", "incident_zip", "latitude", "longitude"},
    ),
    "i": (
        LlmZipBoroughReview,
        lambda batch: mismatch_zip_borough(batch, "zip"),
        zip_borough_responder,
        {"incident_zip", "borough", "city", "incident_address"},
    ),
}


@pytest.mark.parametrize("problem", LLM_CASES)
def test_llm_prompt_holds_only_the_needed_columns(problem, fake_llm, batch_feed):
    cls, _, _, needed = LLM_CASES[problem]
    fake = fake_llm(answer_all_clean)
    cls().detect(batch_feed, review_rows=np.arange(30))
    records = [r for call in fake.calls for r in rows_in_prompt(call["prompt"])]
    assert [r["id"] for r in records] == list(range(30))
    assert all(set(r) == {"id"} | needed for r in records)


@pytest.mark.parametrize("problem", LLM_CASES)
def test_llm_verdicts_map_back_to_reviewed_positions(problem, fake_llm, batch_feed):
    cls, corrupt, responder, _ = LLM_CASES[problem]
    corrupted, labels = corrupt(batch_feed)
    review_rows = np.concatenate([np.flatnonzero(labels)[:40], np.arange(0, len(corrupted), 125)])
    fake = fake_llm(responder)
    result = cls().detect(corrupted, review_rows=review_rows)

    reviewed = np.unique(review_rows)
    expected = reviewed[labels[reviewed]]
    assert result.error is None
    assert np.array_equal(np.flatnonzero(result.reviewed), reviewed)
    assert np.array_equal(np.flatnonzero(result.row_flags), expected)
    assert result.row_reasons == {p: f"reason {p}" for p in expected}
    assert result.row_evidence == {p: {"observed": f"observed {p}", "expected": f"expected {p}", "confidence": 0.9} for p in expected}
    assert result.usage.llm_calls == len(fake.calls) == math.ceil(len(reviewed) / config.LLM_ROWS_PER_CALL)
    assert {call["tag"] for call in fake.calls} == {f"{problem}/{cls.name}"}
    [alert] = result.alerts
    assert (alert.check_id, alert.n_rows, alert.deterministic, alert.score) == (f"L-{problem}1", len(expected), False, 0.9)


def test_llm_ids_are_batch_positions_even_when_the_index_is_not(fake_llm, batch_feed):
    corrupted, labels = swap_lat_lon(batch_feed)
    order = inserted_order(len(corrupted))
    batch, labels = corrupted.iloc[order], labels[order]
    review_rows = np.r_[np.flatnonzero(labels)[:20], 2490:2520]  # swapped rows plus a run across the inserted copies
    fake_llm(coordinate_responder)
    result = LlmCoordinateReview().detect(batch, review_rows=review_rows)
    assert np.array_equal(np.flatnonzero(result.reviewed), np.unique(review_rows))
    assert np.array_equal(np.flatnonzero(result.row_flags), np.unique(review_rows[labels[review_rows]]))


@pytest.mark.parametrize(
    "cls, column",
    [(LlmCoordinateReview, "latitude"), (LlmCoordinateReview, "longitude"), (LlmZipBoroughReview, "incident_zip"), (LlmZipBoroughReview, "borough")],
)
def test_llm_reviews_nothing_without_the_columns_its_check_is_about(cls, column, fake_llm, batch_feed):
    """Regression: with latitude renamed, the model was still asked about swaps without the latitude, spending
    tokens and counting every row as reviewed and clean."""
    fake = fake_llm(answer_all_clean)
    result = cls().detect(batch_feed.rename(columns={column: f"{column}_renamed"}), review_rows=np.arange(30))
    assert not fake.calls and result.error is None
    assert not result.reviewed.any() and not result.row_flags.any() and not result.batch_flag


def test_llm_reviews_every_row_when_no_review_rows_are_given(fake_llm, batch_feed):
    fake = fake_llm(answer_all_clean)
    result = LlmZipBoroughReview().detect(batch_feed.head(60))
    assert result.reviewed.all() and len(fake.calls) == 3 and not result.batch_flag


@pytest.mark.parametrize("cls", [LlmCoordinateReview, LlmZipBoroughReview], ids=lambda c: c.name)
def test_llm_unavailable_gives_an_error_result(cls, fake_llm, batch_feed):
    fake_llm(unavailable=True)
    result = cls().detect(batch_feed, review_rows=np.arange(10))
    assert result.error.startswith("llm_unavailable: ")
    assert not result.batch_flag and not result.row_flags.any() and not result.reviewed.any()


def test_llm_reply_without_verdicts_abstains_on_every_row_never_a_clean_batch(fake_llm, batch_feed):
    client = fake_llm(lambda prompt, schema: {"unexpected": "shape"})
    result = LlmCoordinateReview().detect(batch_feed, review_rows=np.arange(10))
    assert result.error is None and not result.batch_flag and not result.row_flags.any()
    assert not result.reviewed.any()  # no row reads as reviewed, so none reads as clean
    assert result.abstained == dict.fromkeys(range(10), llm.unreadable_cause(""))  # the fake gives no finish reason
    assert result.usage.llm_calls == len(client.calls) == 1  # its tokens still count


def test_llm_ignores_unknown_ids_and_skipped_rows_abstain(fake_llm, batch_feed):
    def responder(prompt, schema):
        first = rows_in_prompt(prompt)[0]
        return {"rows": [_verdict(first, True), {"id": 4999, "flagged": True}]}  # skips 4 rows, invents id 4999

    fake_llm(responder)
    result = LlmCoordinateReview().detect(batch_feed, review_rows=np.arange(5))
    assert np.flatnonzero(result.row_flags).tolist() == [0]
    assert np.flatnonzero(result.reviewed).tolist() == [0]  # only the row with a verdict is reviewed
    assert result.abstained == dict.fromkeys([1, 2, 3, 4], llm.ABSTAIN_NO_VERDICT)  # made-up id 4999 is ignored
    assert "4 rows got no verdict" in result.alerts[0].message


# ------------------------------------------------------------------------------------------
# robustness: detect() never raises on a corrupted batch
# ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("corruption", CORRUPTIONS)
@pytest.mark.parametrize("cls", location.DETECTORS, ids=lambda c: c.name)
def test_detect_never_raises_on_a_corrupted_batch(cls, corruption, fitted, fake_llm, batch_feed):
    fake_llm(answer_all_clean)
    detector = fitted.get(cls.name) or cls()
    batch = CORRUPTIONS[corruption](batch_feed)
    result = detector.detect(batch, review_rows=np.arange(min(len(batch), 30)))
    assert result.error is None
    assert result.row_flags.dtype == bool and len(result.row_flags) == len(batch)
    assert result.batch_flag == bool(result.alerts)
