"""Tests for project_b/detectors/schema.py: renamed/dropped column (a), type change (b),
date format switch (c).

Corruptions are hand-made from the descriptions in DESIGN.md section 3 (blue team: the
injector is not used). Measured rates and timings are printed; run with -s to see them.
"""
from __future__ import annotations

import re
import time

import numpy as np
import pandas as pd
import pytest

from project_b import config, llm, metrics
from project_b.core import DetectionResult
from project_b.detectors import schema
from tests.fakes import FakeClient

REFORMATTED_DATES = ["created_date", "closed_date", "resolution_action_updated_date"]
NUMERIC_COLUMNS = ["unique_key", "latitude", "longitude", "x_coordinate_state_plane", "y_coordinate_state_plane"]
RENAMES = [  # (contract name, plausible alias); only some aliases look like the original name
    ("incident_zip", "zip_code"),
    ("incident_zip", "Incident Zip"),
    ("descriptor", "Descriptor"),
    ("borough", "boro"),
    ("latitude", "lat"),
    ("complaint_type", "problem"),
    ("unique_key", "id"),
    ("descriptor_2", "Additional Details"),
    ("closed_date", "close_date"),
    ("due_date", "due"),
]
FORMATS = {
    "us_12h": "%m/%d/%Y %I:%M:%S %p",
    "dmy_24h": "%d-%m-%Y %H:%M",
    "iso_space_no_ms": "%Y-%m-%d %H:%M:%S",
    "iso_day_month_swapped": "%Y-%d-%mT%H:%M:%S.000",
}
RULE_STAT = [cls for cls in schema.DETECTORS if cls.family != "llm"]


# --------------------------------------------------------------------------
# Hand-made corruptions
# --------------------------------------------------------------------------
def numbers_as_text(batch: pd.DataFrame, col: str, start: int = 0) -> pd.DataFrame:
    """Values from row `start` on delivered as strings (object dtype when only part changes)."""
    values = batch[col].to_numpy(dtype=object, copy=True)
    values[start:] = [v if pd.isna(v) else str(v) for v in values[start:]]
    out = batch.copy()
    out[col] = pd.Series(values, index=batch.index, dtype=object if start else "str")
    return out


def zip_as_number(batch: pd.DataFrame) -> pd.DataFrame:
    out = batch.copy()
    out["incident_zip"] = pd.to_numeric(batch["incident_zip"], errors="coerce")
    return out


def reformat_dates(batch: pd.DataFrame, start: int, fmt: str) -> tuple[pd.DataFrame, np.ndarray]:
    """Date columns from row `start` on re-written in `fmt`; returns (batch, rows whose value changed)."""
    out = batch.copy()
    changed = np.zeros(len(batch), bool)
    for col in REFORMATTED_DATES:
        when = pd.to_datetime(batch[col], format=config.SOURCE_DATETIME_FORMAT)
        new = batch[col].copy()
        new.iloc[start:] = when.iloc[start:].dt.strftime(fmt)
        changed |= (new.fillna("") != batch[col].fillna("")).to_numpy()
        out[col] = new
    return out, changed


def move_dates(batch: pd.DataFrame, days: int) -> pd.DataFrame:
    """Shift every date back by `days`, keeping the source format (a clean batch on another day)."""
    out = batch.copy()
    for col in config.DATETIME_COLUMNS:
        when = pd.to_datetime(batch[col], format=config.SOURCE_DATETIME_FORMAT) - pd.Timedelta(days=days)
        out[col] = when.dt.strftime("%Y-%m-%dT%H:%M:%S.000")
    return out


def recall(result: DetectionResult, labels: np.ndarray) -> float:
    return float((result.row_flags & labels).sum() / labels.sum())


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fitted(reference_feed) -> dict:
    """Rule and stat detectors, fitted once on the reference fixture."""
    return {cls.name: cls().fit(reference_feed) for cls in RULE_STAT}


@pytest.fixture()
def fake_llm():
    """Install a FakeClient; always restore the real client afterwards."""

    def install(responder=None, unavailable: bool = False) -> FakeClient:
        client = FakeClient(responder, unavailable=unavailable)
        llm.set_client(client)
        return client

    yield install
    llm.set_client(None)


# --------------------------------------------------------------------------
# Registry, calibration and the untouched batch
# --------------------------------------------------------------------------
def test_module_lists_one_detector_per_problem_and_family():
    assert len(schema.DETECTORS) == 9
    assert {(cls.problem, cls.family) for cls in schema.DETECTORS} == {(p, f) for p in "abc" for f in ("rule", "stat", "llm")}
    for cls in schema.DETECTORS:
        assert cls.name.startswith(f"{cls.family}_") and cls.description


def test_stat_thresholds_are_calibrated_and_stored(fitted):
    """The reference fixture's 'batches' are 5,000 rows sampled over ~3 months each, not ~7 hours,
    so its calibrated profile similarity is stricter than on real reference batches (0.97 vs 0.94):
    on the fixture PROFILE_SIMILARITY_CAP is the working threshold."""
    profile = fitted["stat_column_profile_match"]
    types, dates = fitted["stat_value_type_profile"], fitted["stat_datetime_shape_shift"]
    print(f"thresholds: profile similarity {profile.threshold_:.3f} (calibrated {profile.calibrated_threshold_:.3f}), "
          f"type-mix shift {types.threshold_:.4f}, flagged date share {dates.threshold_:.4f}")
    assert profile.threshold_ == min(profile.calibrated_threshold_, schema.PROFILE_SIMILARITY_CAP) > 0
    assert types.threshold_ >= schema.TYPE_SHIFT_MIN
    assert dates.threshold_ == 0.0  # the reference fixture has no odd date at all


def statuses_with_nulls(share: float) -> pd.DataFrame:
    """One reference batch for the column profile: a 'status' column whose null rate is `share`."""
    values = np.full(config.BATCH_SIZE, "Closed", dtype=object)
    values[: round(share * config.BATCH_SIZE)] = None
    return pd.DataFrame({"status": pd.Series(values, dtype="str")})


def zips_with_text(share: float) -> pd.DataFrame:
    """One reference batch for the type mix: 'N/A' text in `share` of the ZIPs."""
    values = np.full(config.BATCH_SIZE, "10001", dtype=object)
    values[: round(share * config.BATCH_SIZE)] = "N/A"
    return pd.DataFrame({"incident_zip": pd.Series(values, dtype="str")})


def dates_with_a_late_block(share: float) -> pd.DataFrame:
    """One reference batch for the date checks: the last `share` of the rows 10 days off the timeline."""
    n = config.BATCH_SIZE
    created = pd.date_range("2023-03-01", periods=n, freq="s")
    on_time = created.strftime("%Y-%m-%dT%H:%M:%S.000").to_numpy()
    late = (created + pd.Timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000").to_numpy()
    values = np.where(np.arange(n) < n - round(share * n), on_time, late)
    return pd.DataFrame({"created_date": pd.Series(values, dtype="str")})


@pytest.mark.parametrize("cls,make_batch", [
    (schema.StatColumnProfileMatch, statuses_with_nulls),
    (schema.StatValueTypeProfile, zips_with_text),
    (schema.StatDatetimeShapeShift, dates_with_a_late_block),
], ids=["profile_similarity", "type_mix_shift", "flagged_date_share"])
def test_at_most_target_far_of_reference_batches_alert(monkeypatch, cls, make_batch):
    """DESIGN.md section 4: batch-level thresholds let at most TARGET_BATCH_FAR of the reference
    batches alert. Batch i of 50 departs from the typical one by 0.2 * (i / 49) ** 2, so the largest
    departures are distinct: an interpolated quantile let 3 of the 50 (6 %) alert."""
    monkeypatch.setattr(schema, "PROFILE_SIMILARITY_CAP", 1.0)  # test the calibrated similarity itself
    n = schema.FIT_MAX_BATCHES
    batches = [make_batch(0.2 * (i / (n - 1)) ** 2) for i in range(n)]
    det = cls().fit(pd.concat(batches, ignore_index=True))
    alerted = sum(det.detect(b).batch_flag for b in batches)
    assert alerted == int(config.TARGET_BATCH_FAR * n)  # 2 of 50: as many as allowed, no more


def test_untouched_batch_gets_no_alert(fitted, batch_feed):
    for name, det in fitted.items():
        res = det.detect(batch_feed)
        print(f"untouched batch: {name} batch_flag={res.batch_flag} alerts={[a.message for a in res.alerts]}")
        assert res.error is None and not res.batch_flag, name
        if det.problem == "c":
            assert res.row_flags.shape == (len(batch_feed),) and not res.row_flags.any()


def test_measured_rates_on_hand_made_corruptions(fitted, batch_feed):
    """One line per detector: batches detected, and for c the row recall and the rows wrongly
    flagged before the switch. batch_feed is from 16 October (day > 12), so here even the rule
    sees every swapped date; the silent day <= 12 case is measured separately below."""
    cases = {
        "a": [batch_feed.rename(columns={old: new}) for old, new in RENAMES] + [batch_feed.drop(columns=[c]) for c in config.COLUMNS],
        "b": [numbers_as_text(batch_feed, c, s) for c in NUMERIC_COLUMNS for s in (0, 2500, 4900)] + [zip_as_number(batch_feed)],
    }
    date_cases = [(reformat_dates(batch_feed, s, fmt), s) for fmt in FORMATS.values() for s in (1000, 2500, 4000)]
    for name, det in fitted.items():
        if det.problem == "c":
            results = [(det.detect(batch), labels, start) for (batch, labels), start in date_cases]
            detected = np.mean([r.batch_flag for r, _, _ in results])
            row_recall = np.mean([recall(r, labels) for r, labels, _ in results])
            early = np.mean([r.row_flags[:start].mean() for r, _, start in results])
            print(f"{name:28s} batches {detected:.0%} of {len(results)}, row recall {row_recall:.3f}, rows flagged before switch {early:.4f}")
            assert detected == 1.0 and row_recall > 0.99 and early == 0.0
        else:
            detected = np.mean([det.detect(batch).batch_flag for batch in cases[det.problem]])
            print(f"{name:28s} batches {detected:.0%} of {len(cases[det.problem])}")
            assert detected == 1.0


# --------------------------------------------------------------------------
# a - renamed or dropped column
# --------------------------------------------------------------------------
@pytest.mark.parametrize("old,new", RENAMES)
def test_rename_is_flagged_and_stat_pairs_it_by_profile(fitted, batch_feed, old, new):
    batch = batch_feed.rename(columns={old: new})
    rule, stat = fitted["rule_schema_columns"].detect(batch), fitted["stat_column_profile_match"].detect(batch)
    assert rule.batch_flag and stat.batch_flag
    assert any(old in a.columns for a in rule.alerts)
    assert [(a.check_id, a.columns) for a in stat.alerts] == [("S-a1", [old, new])]  # paired by content, whatever the new name


def test_rule_pairs_only_lookalike_names(fitted, batch_feed):
    paired = {}
    for old, new in RENAMES:
        res = fitted["rule_schema_columns"].detect(batch_feed.rename(columns={old: new}))
        paired[new] = any(a.columns == [old, new] for a in res.alerts)
    print(f"rule name pairing: {sum(paired.values())}/{len(paired)} renames paired: {paired}")
    assert paired["Descriptor"] and paired["Incident Zip"] and paired["boro"]
    assert not paired["zip_code"] and not paired["problem"]  # blind spot: reported as missing + unexpected


@pytest.mark.parametrize("old,new", [("agency_name", "agency"), ("descriptor_2", "descriptor")])
def test_rename_onto_a_contract_name_is_paired_with_the_second_column(fitted, batch_feed, old, new):
    """The header then repeats a contract name: the second column of that name is the renamed one."""
    batch = batch_feed.rename(columns={old: new})
    for name in ("rule_schema_columns", "stat_column_profile_match"):
        res = fitted[name].detect(batch)
        assert [a.columns for a in res.alerts] == [[old, new]], name
    stat = fitted["stat_column_profile_match"]
    plain = stat.detect(batch_feed.rename(columns={old: "renamed"})).alerts[0]
    assert stat.detect(batch).alerts[0].score == plain.score  # scored on the second column's own content


@pytest.mark.parametrize("col", config.COLUMNS)
def test_drop_is_flagged_and_named(fitted, batch_feed, col):
    batch = batch_feed.drop(columns=[col])
    for name in ("rule_schema_columns", "stat_column_profile_match"):
        res = fitted[name].detect(batch)
        assert res.batch_flag and [a.columns for a in res.alerts] == [[col]], name


def test_rule_reports_every_unexpected_column_even_with_a_repeated_name(fitted, batch_feed):
    renamed = batch_feed.rename(columns={"incident_zip": "Incident Zip"})
    batch = pd.concat([renamed, renamed[["Incident Zip"]]], axis=1)  # the rename plus a second column of that name
    alerts = fitted["rule_schema_columns"].detect(batch).alerts
    assert [(a.columns, a.severity) for a in alerts] == [(["incident_zip", "Incident Zip"], "error"), (["Incident Zip"], "warning")]
    extra = fitted["rule_schema_columns"].detect(pd.concat([batch_feed, batch_feed[["agency"]]], axis=1)).alerts
    assert [a.columns for a in extra] == [["agency"]] and "repeats a contract column name" in extra[0].message


def test_whole_header_renamed_is_matched_column_by_column_within_a_second(fitted, batch_feed):
    """Every column under its display name ('Incident Zip'): 25 missing x 25 unexpected columns. The
    stat reads each candidate column once, not once per pair (that took 1.3 s). Sibling columns can
    still be paired with each other (closed_date / resolution_action_updated_date)."""
    batch = batch_feed.rename(columns={c: c.replace("_", " ").title() for c in batch_feed.columns})
    rule = fitted["rule_schema_columns"].detect(batch)
    assert all(schema._normalised(a.columns[1]) == a.columns[0] for a in rule.alerts) and len(rule.alerts) == 25
    stat = fitted["stat_column_profile_match"]
    stat.detect(batch)  # warm-up
    start = time.perf_counter()
    res = stat.detect(batch)
    elapsed = time.perf_counter() - start
    paired = sum(len(a.columns) == 2 and schema._normalised(a.columns[1]) == a.columns[0] for a in res.alerts)
    print(f"whole header renamed: stat detect {elapsed * 1000:.0f} ms, {paired} of 25 paired with their own column")
    assert sorted(a.columns[0] for a in res.alerts) == sorted(config.COLUMNS)  # each contract column reported once
    assert elapsed < 1.0


def test_extra_column_is_a_rule_warning_only(fitted, batch_feed):
    batch = batch_feed.assign(extra_note="x")
    rule = fitted["rule_schema_columns"].detect(batch)
    assert rule.batch_flag and rule.alerts[0].severity == "warning"
    assert not fitted["stat_column_profile_match"].detect(batch).batch_flag  # no contract profile is missing


def test_stat_flags_a_changed_profile_under_an_unchanged_name(fitted, batch_feed):
    stat = fitted["stat_column_profile_match"]
    res = stat.detect(batch_feed.assign(incident_zip=batch_feed["borough"]))  # header looks fine, content is wrong
    assert res.batch_flag and [(a.check_id, a.columns) for a in res.alerts] == [("S-a2", ["incident_zip"])]
    assert res.alerts[0].score < stat.threshold_
    assert not fitted["rule_schema_columns"].detect(batch_feed.assign(incident_zip=batch_feed["borough"])).batch_flag


# --------------------------------------------------------------------------
# b - column type change
# --------------------------------------------------------------------------
@pytest.mark.parametrize("col", NUMERIC_COLUMNS)
@pytest.mark.parametrize("start", [0, 1000, 4900], ids=["all_rows", "from_row_1000", "last_100_rows"])
def test_numbers_as_text_flagged_and_named(fitted, batch_feed, col, start):
    batch = numbers_as_text(batch_feed, col, start)
    for name in ("rule_dtype_contract", "stat_value_type_profile"):
        res = fitted[name].detect(batch)
        assert res.batch_flag and [a.columns for a in res.alerts] == [[col]], name
        assert res.alerts[0].rows[0] >= start  # localised to the rows that changed


def test_zip_as_number_flagged_with_typed_examples(fitted, batch_feed):
    batch = zip_as_number(batch_feed)
    rule = fitted["rule_dtype_contract"].detect(batch)
    assert rule.batch_flag and rule.alerts[0].columns == ["incident_zip"]
    assert rule.alerts[0].observed["dtype"] == "float64" and rule.alerts[0].observed["examples"][0].endswith("(float)")
    assert rule.alerts[0].expected == "str" and rule.alerts[0].score == 1.0
    assert fitted["stat_value_type_profile"].detect(batch).batch_flag


def test_partial_drift_rule_reports_the_string_values(fitted, batch_feed):
    res = fitted["rule_dtype_contract"].detect(numbers_as_text(batch_feed, "latitude", 3000))
    alert = res.alerts[0]
    changed = batch_feed["latitude"].iloc[3000:].notna().sum()
    assert alert.observed["dtype"] == "object" and alert.n_rows == changed
    assert alert.observed["examples"][0].endswith("(str)")
    assert alert.score == pytest.approx(changed / batch_feed["latitude"].notna().sum())


def test_bool_values_are_the_wrong_type_for_an_integer_column(fitted, batch_feed):
    """bool subclasses int, so an isinstance check alone counted no wrong value (n_rows 0, score 0)."""
    alert = fitted["rule_dtype_contract"].detect(batch_feed.assign(unique_key=batch_feed["unique_key"] % 2 == 0)).alerts[0]
    assert alert.columns == ["unique_key"] and alert.n_rows == len(batch_feed) and alert.score == 1.0


def test_value_level_type_change_only_stat_sees(fitted, batch_feed):
    batch = zip_as_number(batch_feed)
    batch["incident_zip"] = batch["incident_zip"].astype("str")  # '10463.0': still a str column
    assert not fitted["rule_dtype_contract"].detect(batch).batch_flag  # blind spot of a dtype contract
    stat = fitted["stat_value_type_profile"].detect(batch)
    assert stat.batch_flag and stat.alerts[0].observed == {"decimal_text": 1.0}


# --------------------------------------------------------------------------
# c - date format switch
# --------------------------------------------------------------------------
@pytest.mark.parametrize("variant", FORMATS)
@pytest.mark.parametrize("switch", [0.2, 0.5, 0.8])
def test_format_switch_flagged_with_rows_after_switch(fitted, batch_feed, variant, switch):
    start = int(switch * len(batch_feed))
    batch, labels = reformat_dates(batch_feed, start, FORMATS[variant])
    for name in ("rule_datetime_format", "stat_datetime_shape_shift"):
        res = fitted[name].detect(batch)
        assert res.batch_flag, name
        assert recall(res, labels) == 1.0 and not res.row_flags[:start].any(), name
        assert res.alerts[0].segment["first_position"] == start
        assert set(res.row_reasons) == set(np.flatnonzero(res.row_flags))


@pytest.mark.parametrize("variant", ["us_12h", "dmy_24h", "iso_space_no_ms"])
def test_rule_guesses_the_new_format(fitted, batch_feed, variant):
    batch, _ = reformat_dates(batch_feed, 2500, FORMATS[variant])
    alert = fitted["rule_datetime_format"].detect(batch).alerts[0]
    assert alert.check_id == "R-c1" and alert.segment["guessed_format"] == FORMATS[variant]


def test_silent_day_month_swap_needs_the_timeline(fitted, batch_feed):
    """On 5 October a swapped created_date ('2023-05-10...') is a valid ISO date: the rule
    only sees the closed/updated dates that happen to have day > 12."""
    batch, labels = reformat_dates(move_dates(batch_feed, 11), 2500, FORMATS["iso_day_month_swapped"])
    rule, stat = fitted["rule_datetime_format"].detect(batch), fitted["stat_datetime_shape_shift"].detect(batch)
    print(f"silent swap: rule row recall {recall(rule, labels):.3f}, stat row recall {recall(stat, labels):.3f}")
    assert recall(stat, labels) == 1.0 and not stat.row_flags[:2500].any()
    assert recall(rule, labels) < 0.5
    timeline = next(a for a in stat.alerts if a.check_id == "S-c2")
    assert timeline.segment["first_position"] == 2500 and timeline.observed["offset_days"] < -100
    assert "days off the batch timeline" in stat.row_reasons[2500]


def test_swap_that_keeps_created_date_is_caught_through_impossible_closed_dates(fitted, batch_feed):
    """On 10 October day == month, so the swap leaves created_date unchanged: only closed /
    updated dates on other days change, and those with day > 12 become impossible dates."""
    batch, labels = reformat_dates(move_dates(batch_feed, 6), 2500, FORMATS["iso_day_month_swapped"])
    rule, stat = fitted["rule_datetime_format"].detect(batch), fitted["stat_datetime_shape_shift"].detect(batch)
    print(f"swap on 10 Oct: {labels.sum()} rows changed; rule recall {recall(rule, labels):.3f}, stat recall {recall(stat, labels):.3f}")
    assert rule.batch_flag and stat.batch_flag
    assert np.array_equal(stat.row_flags, rule.row_flags)  # both see exactly the impossible dates
    alert = next(a for a in stat.alerts if a.columns == ["closed_date"])
    assert "not real dates" in alert.message and alert.segment["first_position"] >= 2500


def test_moved_clean_batch_is_not_flagged(fitted, batch_feed):
    batch = move_dates(batch_feed, 11)
    assert not fitted["rule_datetime_format"].detect(batch).batch_flag
    assert not fitted["stat_datetime_shape_shift"].detect(batch).batch_flag


def test_missing_date_columns_are_not_a_format_problem(fitted, batch_feed, fake_llm):
    batch = batch_feed.drop(columns=["created_date", "closed_date"])
    for name in ("rule_datetime_format", "stat_datetime_shape_shift"):
        res = fitted[name].detect(batch)
        assert res.error is None and not res.batch_flag, name
    client = fake_llm()
    assert not schema.LlmDatetimeReview().detect(batch).batch_flag and client.calls == []


# --------------------------------------------------------------------------
# Robustness: detect() never raises on a corrupted batch
# --------------------------------------------------------------------------
def object_column(value, n: int) -> np.ndarray:
    """An object column holding `value` (a list, an array, ...) in every one of n cells."""
    cells = np.empty(n, dtype=object)
    for i in range(n):
        cells[i] = value
    return cells


def corrupted_batches(batch: pd.DataFrame) -> dict[str, pd.DataFrame]:
    wrong_dtypes = zip_as_number(numbers_as_text(batch, "latitude"))
    wrong_dtypes["created_date"] = pd.to_datetime(batch["created_date"])
    wrong_dtypes["closed_date"] = np.nan
    wrong_dtypes["unique_key"] = batch["unique_key"].astype("str")
    garbage = batch.copy()
    garbage["created_date"] = "not a date"
    garbage.loc[::2, "closed_date"] = "2023-13-45T99:99:99.000"
    duplicated = pd.concat([batch, batch.iloc[[5, 10, 10]]])  # duplicated index labels on purpose
    two_descriptors = batch.rename(columns={"descriptor_2": "descriptor"})
    containers = batch.assign(  # cells pd.isna turns into arrays instead of one True / False
        created_date=object_column([2023, 10, 16], len(batch)),
        closed_date=object_column(np.array(["2023-10-16T08:00:16.000"]), len(batch)),
        latitude=object_column({"lat": 40.7}, len(batch)),
    )
    return {
        "empty": batch.iloc[:0],
        "single_row": batch.iloc[:1],
        "missing_columns": batch.drop(columns=["unique_key", "created_date", "latitude", "incident_zip"]),
        "renamed_columns": batch.rename(columns={"created_date": "Created Date", "longitude": "lon"}),
        "wrong_dtypes": wrong_dtypes,
        "strings_in_numeric": numbers_as_text(numbers_as_text(batch, "longitude", 2000), "x_coordinate_state_plane", 10),
        "unparseable_dates": garbage,
        "inserted_duplicates": duplicated,
        "duplicate_column_names": two_descriptors,
        "container_cells": containers,
        "all_null": pd.DataFrame({c: pd.Series([None] * 50, dtype=object) for c in config.COLUMNS}),
        "reversed_index": batch.iloc[::-1],
        "no_columns": pd.DataFrame(index=range(10)),
    }


@pytest.mark.parametrize("cls", schema.DETECTORS, ids=lambda c: c.name)
def test_detect_never_raises_on_corrupted_batches(fitted, batch_feed, fake_llm, cls):
    fake_llm(fixed_reply(issue_found=True, row_ids=[0, 1, "x", -3, 10**9], columns=["c"]))
    det = fitted.get(cls.name) or cls()
    for case, batch in corrupted_batches(batch_feed).items():
        res = det.detect(batch)
        assert isinstance(res, DetectionResult) and res.error is None, (case, res.error)
        assert res.row_flags is None or res.row_flags.shape == (len(batch),), case


def test_corruptions_are_reported_where_honest(fitted, batch_feed):
    cases = corrupted_batches(batch_feed)
    assert fitted["rule_schema_columns"].detect(cases["duplicate_column_names"]).alerts[0].columns == ["descriptor_2", "descriptor"]
    rule_b = fitted["rule_dtype_contract"].detect(cases["wrong_dtypes"])
    flagged = {a.columns[0] for a in rule_b.alerts}
    assert flagged == {"latitude", "incident_zip", "unique_key", "created_date"}  # all-NaN closed_date: no evidence
    rule_c = fitted["rule_datetime_format"].detect(cases["unparseable_dates"])
    assert rule_c.row_flags.all() and {a.columns[0] for a in rule_c.alerts} == {"created_date", "closed_date"}
    stat_c = fitted["stat_datetime_shape_shift"].detect(cases["unparseable_dates"])
    assert stat_c.row_flags.all() and {a.columns[0] for a in stat_c.alerts} == {"created_date", "closed_date"}


# --------------------------------------------------------------------------
# LLM detectors (FakeClient: no network)
# --------------------------------------------------------------------------
def fixed_reply(**fields):
    """A FakeClient responder that always returns the same batch verdict."""
    return lambda prompt, schema_: {"issue_found": False, "reason": "fine", **fields}


@pytest.mark.parametrize("cls", [schema.LlmSchemaReview, schema.LlmTypeReview], ids=lambda c: c.name)
def test_schema_llm_prompt_holds_contract_header_and_three_rows(batch_feed, fake_llm, cls):
    client = fake_llm(fixed_reply())
    res = cls().detect(numbers_as_text(batch_feed, "latitude"))
    prompt = client.calls[0]["prompt"]
    assert len(client.calls) == 1 and client.calls[0]["tag"] == f"{cls.problem}/{cls.name}"
    assert "Contract (column: dtype):\n  unique_key: int64" in prompt and "  latitude: str | '" in prompt
    shown_keys = [repr(batch_feed["unique_key"].iloc[p]) for p in (0, 2500, 4999)]
    assert all(k in prompt for k in shown_keys) and repr(batch_feed["unique_key"].iloc[1]) not in prompt
    long_text = batch_feed["resolution_description"].iloc[0]
    assert long_text not in prompt and long_text[:20] in prompt  # long values are cut
    assert res.reviewed is None and not res.batch_flag and res.usage.llm_calls == 1


def test_schema_llm_verdict_becomes_alert(batch_feed, fake_llm):
    fake_llm(fixed_reply(issue_found=True, columns=["incident_zip", "zip_code"], observed="zip_code", expected="incident_zip", confidence=0.9, reason="renamed"))
    res = schema.LlmSchemaReview().detect(batch_feed.rename(columns={"incident_zip": "zip_code"}))
    alert = res.alerts[0]
    assert res.batch_flag and alert.columns == ["incident_zip", "zip_code"] and alert.check_id == "L-a1"
    assert alert.score == 0.9 and alert.message == "renamed" and alert.deterministic is False


def sampled_positions(prompt: str) -> list[int]:
    return [int(m) for m in re.findall(r"^(\d+) \| ", prompt, flags=re.M)]


def test_datetime_llm_sees_only_dates_of_40_evenly_spaced_rows(batch_feed, fake_llm):
    client = fake_llm(fixed_reply())
    res = schema.LlmDatetimeReview().detect(batch_feed)
    prompt = client.calls[0]["prompt"]
    positions = sampled_positions(prompt)
    assert len(positions) == config.LLM_BATCH_SAMPLE_ROWS and positions[0] == 0 and positions[-1] == len(batch_feed) - 1
    assert np.flatnonzero(res.reviewed).tolist() == positions
    other_columns = [c for c in config.COLUMNS if c not in ("created_date", "closed_date")]
    assert not any(c in prompt for c in other_columns)
    assert batch_feed["incident_address"].iloc[0] not in prompt and str(batch_feed["unique_key"].iloc[0]) not in prompt
    assert batch_feed["created_date"].iloc[positions[1]] in prompt
    assert not res.batch_flag and not res.row_flags.any() and res.usage.llm_calls == 1


def test_datetime_llm_shows_list_cells_as_text(batch_feed, fake_llm):
    """pd.isna of a list cell is an array; testing its truth value raised ValueError."""
    client = fake_llm(fixed_reply())
    res = schema.LlmDatetimeReview().detect(batch_feed.assign(created_date=object_column([2023, 10, 16], len(batch_feed))))
    assert res.error is None and "\n0 | [2023, 10, 16] | " in client.calls[0]["prompt"]


def test_datetime_llm_verdict_maps_to_reviewed_positions(batch_feed, fake_llm):
    batch, _ = reformat_dates(batch_feed, 2500, FORMATS["us_12h"])

    def responder(prompt, schema_):
        late = [p for p in sampled_positions(prompt) if p >= 2500]
        return {"issue_found": True, "row_ids": late + [1, -1, 10**6, "x"], "segment": f"from position {late[0]}",
                "reason": "format changes", "observed": batch["created_date"].iloc[late[0]], "expected": "ISO", "confidence": 0.8}

    fake_llm(responder)
    res = schema.LlmDatetimeReview().detect(batch)
    flagged = np.flatnonzero(res.row_flags).tolist()
    assert res.batch_flag and flagged and min(flagged) >= 2500
    assert set(flagged) == {p for p in np.flatnonzero(res.reviewed) if p >= 2500}  # junk ids ignored
    assert set(res.row_reasons) == set(res.row_evidence) == set(flagged)
    assert res.row_evidence[flagged[0]] == {"observed": batch["created_date"].iloc[flagged[0]], "expected": "ISO", "confidence": 0.8}
    assert res.alerts[0].rows == flagged[: schema.MAX_EXAMPLE_ROWS] and res.alerts[0].segment == {"model": f"from position {flagged[0]}"}


def test_datetime_llm_evidence_listing_every_affected_value_is_grounded_per_row(batch_feed, fake_llm):
    """The prompt asks for all affected values in `observed`: then the harness's grounding check
    finds each flagged row's own value in the evidence; a quote of one value grounds one row."""
    batch, _ = reformat_dates(batch_feed, 2500, FORMATS["dmy_24h"])

    def answer(listed: int):
        def responder(prompt, schema_):
            late = [p for p in sampled_positions(prompt) if p >= 2500]
            observed = "; ".join(batch["created_date"].iloc[late[:listed]])
            return {"issue_found": True, "row_ids": late, "reason": "day-first dates", "observed": observed}
        return responder

    for listed in (config.LLM_BATCH_SAMPLE_ROWS, 1):  # every affected value, then only the first
        fake_llm(answer(listed))
        res = schema.LlmDatetimeReview().detect(batch)
        flagged = np.flatnonzero(res.row_flags)
        counts = metrics.grounding_counts(batch, flagged, res.row_evidence)
        assert counts == {"grounded": min(listed, len(flagged)), "flagged": len(flagged)}


@pytest.mark.parametrize("cls", [c for c in schema.DETECTORS if c.family == "llm"], ids=lambda c: c.name)
def test_llm_unavailable_and_bad_reply_are_errors(batch_feed, fake_llm, cls):
    fake_llm(unavailable=True)
    res = cls().detect(batch_feed)
    assert not res.batch_flag and res.error.startswith("llm_unavailable:")
    fake_llm(lambda prompt, schema_: ["not", "a", "verdict"])
    res = cls().detect(batch_feed)
    assert not res.batch_flag and res.error.startswith("llm_bad_reply") and res.usage.llm_calls == 1


# --------------------------------------------------------------------------
# Speed
# --------------------------------------------------------------------------
def test_fit_uses_at_most_fit_max_batches_spread_over_the_reference():
    """What keeps fit() fast on the 2.1 M-row reference: its cost stops growing at FIT_MAX_BATCHES."""
    reference = pd.DataFrame({"row": np.arange(config.BATCH_SIZE * 150)})
    batches = schema._reference_batches(reference)
    assert len(batches) == schema.FIT_MAX_BATCHES and all(len(b) == config.BATCH_SIZE for b in batches)
    assert batches[0]["row"].iloc[0] == 0 and batches[-1]["row"].iloc[-1] == len(reference) - 1


def test_fit_and_detect_times(reference_feed, batch_feed):
    """fit() on the fixture and on the fixture x10 (30 batches); the full reference uses
    FIT_MAX_BATCHES batches, so its fit time is extrapolated from the x10 run."""
    reference_10x = pd.concat([reference_feed] * 10, ignore_index=True)
    scale_to_full = schema.FIT_MAX_BATCHES / (len(reference_10x) // config.BATCH_SIZE)
    for cls in RULE_STAT:
        start = time.perf_counter()
        det = cls().fit(reference_feed)
        fit_s = time.perf_counter() - start
        start = time.perf_counter()
        cls().fit(reference_10x)
        fit_10x_s = time.perf_counter() - start
        det.detect(batch_feed)  # warm-up (regex compilation)
        start = time.perf_counter()
        det.detect(batch_feed)
        detect_s = time.perf_counter() - start
        print(f"{cls.name:28s} fit {fit_s:5.2f}s  fit(x10) {fit_10x_s:5.2f}s  est. full reference {fit_10x_s * scale_to_full:5.1f}s  "
              f"detect(5,000 rows) {detect_s * 1000:6.1f} ms")
        assert detect_s < 1.0 and fit_10x_s * scale_to_full < 60
