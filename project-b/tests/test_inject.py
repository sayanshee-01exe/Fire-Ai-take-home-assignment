"""Tests for project_b/inject.py (DESIGN.md section 3).

Contract checks run for EVERY problem and variant: labels align with the returned frame,
counts follow the rate, the input batch is untouched, rows outside the labels are untouched,
the feed dtypes survive (except where the dtype is the corruption) and the same seed gives
the same output. Problem-specific tests then check that the corruption is really there.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pickle
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import project_b.inject as injector
from project_b import config
from project_b.core import PROBLEMS
from project_b.data.load import to_feed
from project_b.inject import (
    COLUMN_ALIASES,
    GENERIC_DESCRIPTORS,
    LAST_NAMES,
    MAX_KEY_OFFSET,
    MAX_REFERENCE_NULL_RATE,
    MAX_TIME_SHIFT_S,
    MIN_AGENCY_ROWS,
    MIN_TEXT_ROWS,
    NULL_FRACTION_RANGE,
    NULL_SPIKE_CHOICES,
    NUMERIC_COLUMNS,
    REOPENED_STATUSES,
    TYPO_SHIFT_M,
    VARIANTS,
    Injected,
    InjectionContext,
    as_clean,
    inject,
)

SEED = 7
CASES = [(problem, variant) for problem, variants in VARIANTS.items() for variant in variants]
ROW_LEVEL = sorted(p for p, meta in PROBLEMS.items() if meta["level"] == "row")
INSERTS_ROWS = {"e", "f"}
COORDINATES = ["latitude", "longitude", "x_coordinate_state_plane", "y_coordinate_state_plane"]

PHONE = re.compile(r"(?:\+1 )?\(?\b(?:212|718|347|646|917|929)\)?[ .-]?555[ .-]?01\d\d\b")
EMAIL = re.compile(r"\b[a-z._]+\d*@example\.(?:com|org|net)\b")
LAST_NAME = re.compile(r"\b(?:%s)\b" % "|".join(LAST_NAMES))
NAME_CUE = re.compile(r"\b(?:tenant|caller|complainant|resident|spoke with|contact|Mr\.|Ms\.|Mrs\.|Dr\.)", re.I)


def run(batch, problem, ctx, variant=None, seed=SEED, **params) -> Injected:
    return inject(batch, problem, np.random.default_rng(seed), ctx, variant=variant, **params)


def parse(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, format=config.SOURCE_DATETIME_FORMAT, errors="coerce")


def expected_k(n_rows: int, rate: float = config.ROW_INJECTION_RATE) -> int:
    return max(1, round(rate * n_rows))


def cells_differ(new: pd.DataFrame, old: pd.DataFrame) -> pd.DataFrame:
    """Cell-wise "value changed", counting null -> null as unchanged (NaN != NaN in pandas)."""
    return new.ne(old) & ~(new.isna() & old.isna())


@pytest.fixture(scope="module")
def batch(batch_raw) -> pd.DataFrame:
    """Pristine module-wide batch; tests that change a batch use the batch_feed fixture."""
    return to_feed(batch_raw)


@pytest.fixture(scope="module")
def ctx(reference_feed, batch) -> InjectionContext:
    """As in the harness, the context sees the whole year: here reference + this batch."""
    return InjectionContext.build(pd.concat([reference_feed, batch], ignore_index=True))


@dataclass
class Case:
    problem: str
    variant: str
    passed: pd.DataFrame  # the frame handed to inject()
    original: pd.DataFrame  # an untouched copy of it
    result: Injected


@pytest.fixture(scope="module", params=CASES, ids=[f"{p}-{v}" for p, v in CASES])
def case(request, batch, ctx) -> Case:
    problem, variant = request.param
    passed = batch.copy(deep=True)
    return Case(problem, variant, passed, batch, run(passed, problem, ctx, variant))


# --------------------------------------------------------------------------
# Contract checks, every problem x variant
# --------------------------------------------------------------------------
def test_labels_align_with_returned_frame(case):
    out = case.result
    assert out.problem == case.problem
    assert out.df.index.equals(pd.RangeIndex(len(out.df)))
    assert out.row_labels.dtype == bool and len(out.row_labels) == len(out.df)
    if case.problem in ROW_LEVEL:
        assert len(out.row_variant) == len(out.df)
        assert set(out.row_variant[out.row_labels]) == {case.variant}
        assert all(v is None for v in out.row_variant[~out.row_labels])
    else:
        assert out.row_variant is None


def test_count_matches_rate(case):
    out, n = case.result, len(case.original)
    assert out.batch_label
    if case.problem in ROW_LEVEL:
        k = expected_k(n)
        assert out.params["n_requested"] == out.params["n_injected"] == out.row_labels.sum() == k
        assert out.params["variants"] == {case.variant: k}
        assert len(out.df) == n + (k if case.problem in INSERTS_ROWS else 0)
    elif case.problem in ("c", "g"):  # batch-level, but the affected rows are labelled
        assert out.params["n_injected"] == out.row_labels.sum() > 0
    else:
        assert len(out.df) == n and not out.row_labels.any()


def test_input_batch_not_mutated(case):
    pd.testing.assert_frame_equal(case.passed, case.original)


def test_labels_mark_exactly_what_changed(case):
    out, original = case.result, case.original
    if case.problem in ("a", "b"):  # one whole column is corrupted; every other column is identical
        others = [c for c in original.columns if c not in out.params["columns"]]
        pd.testing.assert_frame_equal(out.df[others], original[others])
    elif case.problem in INSERTS_ROWS:  # removing the labelled copies gives back the batch
        pd.testing.assert_frame_equal(out.df[~out.row_labels].reset_index(drop=True), original)
    else:  # labelled rows changed, every other row is untouched
        np.testing.assert_array_equal(out.row_labels, cells_differ(out.df, original).any(axis=1))


def test_feed_dtypes_kept_except_the_corrupted_one(case):
    out = case.result
    retyped = set(out.params["columns"]) if case.problem == "b" else set()
    contract_name = {out.params["new_name"]: out.params["old_name"]} if case.variant == "rename" else {}
    for column in out.df.columns:
        name = contract_name.get(column, column)
        if name not in retyped:
            assert str(out.df[column].dtype) == config.FEED_DTYPES[name], column


def test_same_seed_same_output(case, ctx):
    again = run(case.original, case.problem, ctx, case.variant)
    pd.testing.assert_frame_equal(again.df, case.result.df)
    np.testing.assert_array_equal(again.row_labels, case.result.row_labels)
    assert again.params == case.result.params
    if case.problem in ROW_LEVEL:
        np.testing.assert_array_equal(again.row_variant, case.result.row_variant)


# --------------------------------------------------------------------------
# Shared behaviour
# --------------------------------------------------------------------------
@pytest.mark.parametrize("problem", ROW_LEVEL)
def test_other_seed_other_rows(batch, ctx, problem):
    assert not np.array_equal(run(batch, problem, ctx, seed=1).row_labels, run(batch, problem, ctx, seed=2).row_labels)


@pytest.mark.parametrize("problem", [p for p in ROW_LEVEL if len(VARIANTS[p]) > 1])
def test_row_variants_are_mixed_by_default(batch, ctx, problem):
    out = run(batch, problem, ctx)
    assert set(out.params["variants"]) == set(VARIANTS[problem])
    assert sum(out.params["variants"].values()) == out.row_labels.sum()


@pytest.mark.parametrize("problem", ["a", "b", "c"])
def test_batch_variants_are_drawn_uniformly_by_default(batch, ctx, problem):
    seen = {run(batch, problem, ctx, seed=seed).params["variant"] for seed in range(20)}
    assert seen == set(VARIANTS[problem])


@pytest.mark.parametrize("rate", [0.001, 0.10])
@pytest.mark.parametrize("problem", ROW_LEVEL)
def test_row_count_follows_any_rate(batch, ctx, problem, rate):
    out = inject(batch, problem, np.random.default_rng(SEED), ctx, rate=rate)
    assert out.params["n_injected"] == out.row_labels.sum() == expected_k(len(batch), rate)


@pytest.mark.parametrize("problem", sorted(VARIANTS))
def test_result_does_not_depend_on_the_input_index(batch, ctx, problem):
    shifted = batch.set_axis(batch.index + 10_000)
    plain, moved = run(batch, problem, ctx), run(shifted, problem, ctx)
    pd.testing.assert_frame_equal(plain.df, moved.df)
    np.testing.assert_array_equal(plain.row_labels, moved.row_labels)


@pytest.mark.parametrize("n_rows", [0, 10])
@pytest.mark.parametrize("problem", sorted(VARIANTS))
def test_tiny_batches_never_crash(batch, ctx, problem, n_rows):
    out = run(batch.head(n_rows), problem, ctx)
    assert len(out.row_labels) == len(out.df)
    if problem in ROW_LEVEL:
        assert out.params["n_injected"] == out.row_labels.sum() <= min(n_rows, out.params["n_requested"])


@pytest.mark.parametrize("problem", sorted(VARIANTS))
def test_empty_context_never_crashes(batch, problem):
    out = run(batch, problem, InjectionContext.build(batch.head(0)))
    assert len(out.row_labels) == len(out.df)
    if problem in ("g", "j", "k"):  # nothing to draw from: report "nothing injected" honestly
        assert not out.batch_label and out.params["n_injected"] == 0


READ_COLUMNS = ["unique_key", "created_date", "closed_date", "agency", "complaint_type", "descriptor", "incident_zip",
                "incident_address", "borough", "status", "resolution_description", *COORDINATES]


@pytest.mark.parametrize("problem", sorted(VARIANTS))
def test_a_column_another_corruption_took_never_breaks_an_injector(batch, ctx, problem):
    """DESIGN section 7 mixes corruptions: a column an injector reads may be gone (a) or hold
    another dtype (b). The injector then injects what it still can, without raising, and never
    writes the lost column back."""
    small = batch.head(500)
    for column in READ_COLUMNS:
        retyped = small[column].astype("str") if config.FEED_DTYPES[column] != "str" else pd.to_numeric(small[column], errors="coerce")
        for broken in (small.drop(columns=column), small.assign(**{column: retyped})):
            out = run(broken, problem, ctx)
            assert out.df.index.equals(pd.RangeIndex(len(out.df))) and len(out.row_labels) == len(out.df)
            if problem not in ("a", "b"):  # only the schema problems change columns or dtypes
                assert list(out.df.columns) == list(broken.columns), column
                if column in broken:  # a retyped column keeps the dtype it arrived in
                    assert out.df[column].dtype == broken[column].dtype, column


@pytest.mark.parametrize("first", sorted(VARIANTS))
def test_injecting_on_top_of_another_problem_never_crashes(batch, ctx, first):
    """Every injector on a batch another injector already corrupted (re-formatted dates,
    inserted rows, nulls, ...): labels still line up with the returned frame."""
    broken = run(batch.head(1_000), first, ctx).df
    for problem in sorted(VARIANTS):
        out = run(broken, problem, ctx, seed=1)
        assert out.df.index.equals(pd.RangeIndex(len(out.df))) and len(out.row_labels) == len(out.df)
        if problem in ROW_LEVEL:
            assert out.params["n_injected"] == out.row_labels.sum()


def test_an_unusable_column_is_reported_not_raised(batch, ctx):
    out = run(batch.assign(latitude=batch["latitude"].astype("str")), "d", ctx, "swap")
    assert not out.batch_label and out.params["unusable_columns"] == ["latitude"]
    with pytest.raises(ValueError):  # an unknown variant is still an error
        run(batch.drop(columns="latitude"), "d", ctx, "teleport")


def test_too_few_eligible_rows_corrupts_as_many_as_possible(batch_feed, ctx):
    batch_feed.loc[3:, ["latitude", "longitude"]] = np.nan  # only rows 0-2 keep coordinates
    out = run(batch_feed, "d", ctx, "swap")
    assert out.params["n_requested"] == expected_k(len(batch_feed))
    assert out.params["n_injected"] == out.row_labels.sum() == 3


def test_no_eligible_rows_injects_nothing(batch_feed, ctx):
    batch_feed["resolution_description"] = pd.Series(np.nan, index=batch_feed.index, dtype="str")
    out = run(batch_feed, "l", ctx)
    assert not out.batch_label and out.params["n_injected"] == 0 and not out.row_labels.any()


def test_unknown_problem_variant_or_fraction_is_rejected(batch, ctx):
    with pytest.raises(ValueError):
        run(batch, "z", ctx)
    with pytest.raises(ValueError):
        run(batch, "a", ctx, "teleport")
    with pytest.raises(ValueError):
        run(batch, "d", ctx, "teleport")
    with pytest.raises(ValueError):
        run(batch, "g", ctx, fraction=1.5)


def test_as_clean_labels_nothing(batch):
    out = as_clean(batch.set_axis(batch.index + 10_000))
    assert not out.batch_label and not out.row_labels.any() and len(out.row_labels) == len(batch)
    pd.testing.assert_frame_equal(out.df, batch)


def test_context_holds_lookups_not_the_frame(reference_feed, ctx):
    assert len(pickle.dumps(ctx)) < len(pickle.dumps(reference_feed)) / 10


def test_null_rates_come_from_the_reference_period_only(reference_feed, batch_feed):
    batch_feed["city"] = pd.Series(np.nan, index=batch_feed.index, dtype="str")  # October rows lose every city
    with_october = InjectionContext.build(pd.concat([reference_feed, batch_feed], ignore_index=True))
    reference_only = InjectionContext.build(reference_feed)
    pd.testing.assert_frame_equal(with_october.reference_null_rates, reference_only.reference_null_rates)


# --------------------------------------------------------------------------
# a - renamed or dropped column
# --------------------------------------------------------------------------
def test_a_aliases_never_collide_with_contract_names():
    assert set(COLUMN_ALIASES) == set(config.COLUMNS)
    aliases = [alias for names in COLUMN_ALIASES.values() for alias in names]
    assert len(aliases) == len(set(aliases)) and not set(aliases) & set(config.COLUMNS)


def test_a_rename_keeps_position_and_values(batch, ctx):
    for seed in range(20):  # different columns and alias styles
        out = run(batch, "a", ctx, "rename", seed=seed)
        old, new = out.params["old_name"], out.params["new_name"]
        assert out.params["columns"] == [old] and new not in config.COLUMNS
        assert list(out.df.columns) == [new if c == old else c for c in config.COLUMNS]
        pd.testing.assert_series_equal(out.df[new], batch[old], check_names=False)


def test_a_drop_removes_exactly_one_column(batch, ctx):
    out = run(batch, "a", ctx, "drop")
    dropped = out.params["dropped_column"]
    assert out.params["columns"] == [dropped]
    assert list(out.df.columns) == [c for c in config.COLUMNS if c != dropped]


# --------------------------------------------------------------------------
# b - column type change
# --------------------------------------------------------------------------
def test_b_num_as_str_delivers_the_same_numbers_as_text(batch, ctx):
    for seed in range(10):  # covers several numeric columns
        out = run(batch, "b", ctx, "num_as_str", seed=seed)
        (column,) = out.params["columns"]
        assert column in NUMERIC_COLUMNS and pd.api.types.is_string_dtype(out.df[column])
        np.testing.assert_allclose(pd.to_numeric(out.df[column]), batch[column])


def test_b_partial_switches_from_numbers_to_text(batch, ctx):
    out = run(batch, "b", ctx, "num_as_str_partial")
    (column,), switch = out.params["columns"], out.params["switch_row"]
    values = out.df[column]
    assert pd.api.types.is_object_dtype(values)
    assert round(0.2 * len(batch)) <= switch <= round(0.8 * len(batch))
    assert not any(isinstance(v, str) for v in values[:switch].dropna())
    assert all(isinstance(v, str) for v in values[switch:].dropna())
    np.testing.assert_allclose(pd.to_numeric(values), batch[column])


def test_b_zip_as_number(batch, ctx):
    out = run(batch, "b", ctx, "zip_as_number")
    assert out.params["columns"] == ["incident_zip"] and out.df["incident_zip"].dtype == "float64"
    np.testing.assert_allclose(out.df["incident_zip"], pd.to_numeric(batch["incident_zip"], errors="coerce"))


# --------------------------------------------------------------------------
# c - date format switch
# --------------------------------------------------------------------------
@pytest.mark.parametrize("variant", VARIANTS["c"])
def test_c_dates_after_the_switch_leave_the_source_format(batch, ctx, variant):
    out = run(batch, "c", ctx, variant)
    switch, columns = out.params["switch_row"], out.params["columns"]
    assert round(0.2 * len(batch)) <= switch <= round(0.8 * len(batch))
    assert not out.row_labels[:switch].any() and out.row_labels[switch:].mean() > 0.95
    for column in columns:
        new, old = out.df[column].iloc[switch:], batch[column].iloc[switch:]
        assert new.isna().equals(old.isna())  # nulls stay null
        new, old = new.dropna(), old.dropna()
        source_shaped = new.str.fullmatch(config.SOURCE_DATETIME_REGEX)
        if variant != "iso_day_month_swapped":
            assert not source_shaped.any()
            continue
        # same shape as the source: unparseable when day > 12, silently wrong when day <= 12
        assert source_shaped.all()
        day, month = old.str[8:10].astype(int), old.str[5:7].astype(int)
        assert parse(new)[day > 12].isna().all()
        looks_valid = (day <= 12) & (day != month)
        assert (parse(new)[looks_valid] != parse(old)[looks_valid]).all()


# --------------------------------------------------------------------------
# d - latitude / longitude swapped
# --------------------------------------------------------------------------
def test_d_swapped_coordinates_land_outside_nyc(batch, ctx):
    out = run(batch, "d", ctx, "swap")
    rows, original = out.df[out.row_labels], batch[out.row_labels]
    np.testing.assert_array_equal(rows["latitude"], original["longitude"])
    np.testing.assert_array_equal(rows["longitude"], original["latitude"])
    inside = rows["latitude"].between(config.NYC_LAT_MIN, config.NYC_LAT_MAX) & rows["longitude"].between(config.NYC_LON_MIN, config.NYC_LON_MAX)
    assert not inside.any()


# --------------------------------------------------------------------------
# e - exact duplicates
# --------------------------------------------------------------------------
def test_e_each_copy_is_an_exact_later_duplicate(batch, ctx):
    out = run(batch, "e", ctx, "exact_copy")
    # the batch has no duplicates of its own, so the copies are exactly the later duplicates
    np.testing.assert_array_equal(out.df.duplicated(keep="first").to_numpy(), out.row_labels)
    for copy, original in out.params["copy_of"].items():
        assert original < copy and out.row_labels[copy]
        pd.testing.assert_series_equal(out.df.iloc[copy], out.df.iloc[original], check_names=False)


# --------------------------------------------------------------------------
# f - near-duplicates
# --------------------------------------------------------------------------
@pytest.mark.parametrize("variant", VARIANTS["f"])
def test_f_copies_are_near_duplicates_in_time_order(batch, ctx, variant):
    out = run(batch, "f", ctx, variant)
    df = out.df
    assert df["unique_key"].is_unique
    assert df["created_date"].is_monotonic_increasing  # the batch arrives sorted and stays sorted
    edited, text_columns = 0, ["incident_address", "descriptor"]
    for copy, original in out.params["copy_of"].items():
        new, old = df.iloc[copy], df.iloc[original]
        assert original < copy and out.row_labels[copy] and not out.row_labels[original]
        assert 1 <= new["unique_key"] - old["unique_key"] <= MAX_KEY_OFFSET
        shift = pd.Timestamp(new["created_date"]) - pd.Timestamp(old["created_date"])
        assert pd.Timedelta(seconds=1) <= shift <= pd.Timedelta(seconds=MAX_TIME_SHIFT_S)
        if pd.notna(old["closed_date"]):
            assert pd.Timestamp(new["closed_date"]) - pd.Timestamp(old["closed_date"]) == shift
        moved = COORDINATES if copy in out.params["coord_shift_m"] else []  # a mistyped address only (next test)
        untouched = [c for c in df.columns if c not in ["unique_key", "created_date", "closed_date", *text_columns, *moved]]
        assert new[untouched].equals(old[untouched])
        changed = [c for c in text_columns if new[c] != old[c] and not (pd.isna(new[c]) and pd.isna(old[c]))]
        edited += bool(changed)
        if variant == "time_only":
            assert not changed
        elif changed == ["descriptor"]:
            assert new["descriptor"].lower() == old["descriptor"].lower()  # case change only
        else:
            assert changed == ["incident_address"] and abs(len(new["incident_address"]) - len(old["incident_address"])) <= 4
    assert edited == (out.row_labels.sum() if variant == "time_and_text" else 0)
    assert sum(out.params["text_edits"].values()) == edited


def metres_apart(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance on a sphere of the Earth's mean radius."""
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((phi2 - phi1) / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6_371_008.8 * np.arcsin(np.sqrt(a))


def test_f_a_mistyped_address_is_geocoded_a_few_metres_away(batch_feed, ctx):
    """Regression: a copy whose address got the typo kept the original's exact coordinates, an
    artefact a detector could key on (a real re-entry with a mistyped address is geocoded on its
    own). It now lies 5-15 m away and its state-plane x/y moved by the same step; copies with
    another edit or none, and rows without coordinates, keep theirs exactly."""
    no_coordinates = batch_feed[COORDINATES].isna().any(axis=1) | (batch_feed.index % 3 == 0)
    batch_feed.loc[no_coordinates, COORDINATES] = np.nan  # each row has all four coordinates or none
    out = run(batch_feed, "f", ctx, "time_and_text", rate=0.2)
    df, shift_m = out.df, out.params["coord_shift_m"]
    # Feet per degree here, fitted on the batch's own rows (not the injector's constants): a copy's
    # x/y must move the way its latitude/longitude moved.
    known = batch_feed[~no_coordinates]
    design = np.column_stack([np.ones(len(known)), known["latitude"], known["longitude"]])
    feet_per_degree = np.linalg.lstsq(design, known[["x_coordinate_state_plane", "y_coordinate_state_plane"]].to_numpy(), rcond=None)[0][1:]
    moved = []
    for copy, original in out.params["copy_of"].items():
        new, old = df.iloc[copy], df.iloc[original]
        address, old_address = new["incident_address"], old["incident_address"]
        typo = isinstance(old_address, str) and address != old_address and len(address) == len(old_address)  # the one address edit that keeps the length
        if not (typo and old[COORDINATES].notna().all()):
            assert copy not in shift_m and new[COORDINATES].equals(old[COORDINATES]), copy
            continue
        moved.append(copy)
        metres = metres_apart(old["latitude"], old["longitude"], new["latitude"], new["longitude"])
        # 5-15 m as drawn, give or take what rounding x/y to whole feet changed (<= 0.22 m)
        assert TYPO_SHIFT_M[0] - 0.3 <= metres <= TYPO_SHIFT_M[1] + 0.3 and abs(metres - shift_m[copy]) < 0.1
        step = (new[COORDINATES] - old[COORDINATES]).to_numpy(dtype=float)  # latitude, longitude, x, y
        assert np.hypot(*(step[2:] - step[:2] @ feet_per_degree)) / 3.28083 < 0.5  # metres
        assert abs(np.hypot(*step[2:]) / 3.28083 - shift_m[copy]) < 0.001  # reports the step actually made (after rounding)
        assert all(float(v).is_integer() for v in new[COORDINATES[2:]])  # whole feet, like every source x/y
    assert len(moved) >= 20 and sorted(shift_m) == sorted(moved)
    assert run(batch_feed, "f", ctx, "time_only", rate=0.2).params["coord_shift_m"] == {}
    again = run(batch_feed, "f", ctx, "time_and_text", rate=0.2)  # same seed, same move
    pd.testing.assert_frame_equal(again.df, df)
    assert again.params == out.params


def state_plane_inverse(rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    return injector._state_plane_to_latlon(rows["x_coordinate_state_plane"].to_numpy(), rows["y_coordinate_state_plane"].to_numpy())


def test_f_state_plane_inverse_reproduces_real_rows(reference_feed, batch):
    """The EPSG:2263 inverse the injector moves copies with is the relation the source obeys: every
    real row's latitude/longitude lie within 1 cm of the inverse of its whole-foot x/y, at a
    near-constant offset (4.4 mm)."""
    real = pd.concat([reference_feed, batch]).dropna(subset=COORDINATES)
    lat, lon = state_plane_inverse(real)
    off = metres_apart(real["latitude"].to_numpy(), real["longitude"].to_numpy(), lat, lon)
    assert len(real) > 19_000 and off.max() < 0.01 and np.ptp(off) < 0.001


def test_f_a_moved_copy_keeps_the_latlon_xy_relation_of_real_rows(batch, ctx):
    """Regression: a moved copy's latitude/longitude were stepped in degrees and its x/y in whole
    feet separately, so the pair missed the exact relation every real row obeys by 1-22 cm, a tell
    any consistency check could key on. Now latitude/longitude follow the new x/y: a cubic
    (x, y) -> (latitude, longitude) fitted on the real rows (no injector code) leaves every copy
    inside the real rows' residual range, and latitude/longitude equal the inverse of the copy's
    x/y plus its original's own residual within 1 mm."""
    out = run(batch, "f", ctx, "time_and_text", rate=0.2)
    copies = sorted(out.params["coord_shift_m"])
    new, old = out.df.loc[copies], out.df.loc[[out.params["copy_of"][c] for c in copies]]
    assert len(copies) >= 100 and new[COORDINATES].notna().all().all()

    real = batch.dropna(subset=COORDINATES)
    centre = real[COORDINATES[2:]].mean()

    def cubic(rows: pd.DataFrame) -> np.ndarray:  # monomials u^i v^j, i + j <= 3, of centred x/y in 100,000 ft
        u, v = ((rows[COORDINATES[2:]] - centre) / 1e5).to_numpy().T
        return np.column_stack([u**i * v**j for i in range(4) for j in range(4 - i)])

    fit = np.linalg.lstsq(cubic(real), real[COORDINATES[:2]].to_numpy(), rcond=None)[0]
    real_residual = real[COORDINATES[:2]].to_numpy() - cubic(real) @ fit  # real rows: within about 0.1 mm
    copy_residual = new[COORDINATES[:2]].to_numpy() - cubic(new) @ fit
    slack = 1e-11  # degrees (about 1 micrometre): the cubic's own error drifts about 0.3 micrometre over 15 m
    assert (copy_residual >= real_residual.min(axis=0) - slack).all() and (copy_residual <= real_residual.max(axis=0) + slack).all()

    (lat_new, lon_new), (lat_old, lon_old) = state_plane_inverse(new), state_plane_inverse(old)
    expected_lat = lat_new + (old["latitude"].to_numpy() - lat_old)
    expected_lon = lon_new + (old["longitude"].to_numpy() - lon_old)
    assert metres_apart(new["latitude"].to_numpy(), new["longitude"].to_numpy(), expected_lat, expected_lon).max() < 0.001


def test_f_survives_dense_unique_keys(batch_feed, ctx):
    batch_feed["unique_key"] = np.arange(len(batch_feed), dtype="int64")  # most keys have no free successor
    out = run(batch_feed, "f", ctx, "time_only")
    assert out.df["unique_key"].is_unique
    assert 0 < out.params["n_injected"] == out.row_labels.sum() < out.params["n_requested"]


def test_f_skips_an_original_whose_next_report_differs_only_in_case(batch_feed, ctx):
    """Regression: a copy lands up to 300 s after its original, so an original whose next report
    of the same request follows within 600 s is never copied (the copy would make that report an
    unlabelled near-duplicate). HPD's Title Case back-fill repeats requests with the complaint
    type in another case, and data/clean.py treats that as the same request, so must the guard."""
    batch = batch_feed.head(300)
    original = int(np.flatnonzero((batch["agency"] == "HPD").to_numpy())[0])
    key = batch.at[original, "unique_key"]

    def copied_originals(frame) -> set:
        out = run(frame, "f", ctx, "time_only", rate=1.0)  # every eligible row gets a copy
        return set(out.df.loc[list(out.params["copy_of"].values()), "unique_key"])

    assert key in copied_originals(batch)  # eligible on its own
    later = batch.iloc[[original]].assign(
        unique_key=batch["unique_key"].max() + 1,
        complaint_type=batch.at[original, "complaint_type"].title(),  # 'HEAT/HOT WATER' -> 'Heat/Hot Water'
        created_date=(parse(batch.iloc[[original]]["created_date"]) + pd.Timedelta(seconds=450)).dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
    )
    with_later = pd.concat([batch, later]).sort_values("created_date", kind="stable").reset_index(drop=True)
    assert key not in copied_originals(with_later)


# --------------------------------------------------------------------------
# g - null spike in one agency
# --------------------------------------------------------------------------
@pytest.mark.parametrize("fraction", [0.1, 0.25, 0.5, 0.9])
def test_g_null_spike_honours_the_fraction(batch, ctx, fraction):
    out = run(batch, "g", ctx, "null_spike", fraction=fraction)
    p = out.params
    of_agency = batch["agency"] == p["agency"]
    eligible = of_agency & batch[p["columns"]].notna().any(axis=1)
    assert of_agency.sum() >= MIN_AGENCY_ROWS and p["reference_null_rate"] < MAX_REFERENCE_NULL_RATE
    assert p["fraction"] == fraction and p["n_eligible"] == eligible.sum()
    assert p["n_injected"] == out.row_labels.sum() == round(fraction * eligible.sum())
    assert of_agency[out.row_labels].all()
    new_nulls = out.df.isna() & ~batch.isna()
    assert set(new_nulls.columns[new_nulls.any()]) == set(p["columns"])
    assert out.df.loc[out.row_labels, p["columns"]].isna().all().all()


def test_g_default_choices_follow_the_spec(batch, ctx):
    for seed in range(10):
        p = run(batch, "g", ctx, seed=seed).params
        assert NULL_FRACTION_RANGE[0] <= p["fraction"] <= NULL_FRACTION_RANGE[1]
        assert tuple(p["columns"]) in NULL_SPIKE_CHOICES  # latitude + longitude only together


def test_g_reports_nothing_when_no_agency_qualifies(batch, ctx):
    out = run(batch.head(50), "g", ctx)
    assert not out.batch_label and not out.row_labels.any() and out.params["n_injected"] == 0


# --------------------------------------------------------------------------
# h - closed before created
# --------------------------------------------------------------------------
@pytest.mark.parametrize("variant", VARIANTS["h"])
def test_h_closed_date_is_before_created_date(batch, ctx, variant):
    out = run(batch, "h", ctx, variant)
    rows = out.df[out.row_labels]
    created, closed = parse(rows["created_date"]), parse(rows["closed_date"])
    assert rows["closed_date"].str.fullmatch(config.SOURCE_DATETIME_REGEX).all()  # written in the source format
    assert (closed < created).all()
    lag = created - closed
    if variant == "minutes":
        assert lag.between(pd.Timedelta(minutes=1), pd.Timedelta(minutes=59)).all()
    elif variant == "days":
        assert lag.between(pd.Timedelta(days=1), pd.Timedelta(days=30)).all()
    else:
        assert (closed.dt.year == 2022).all()


def test_h_year_typo_handles_29_february(batch_feed, ctx):
    batch_feed["closed_date"] = pd.Series("2024-02-29T10:00:00.000", index=batch_feed.index, dtype="str")
    out = run(batch_feed, "h", ctx, "year_typo")
    assert (out.df.loc[out.row_labels, "closed_date"] == "2022-02-28T10:00:00.000").all()


# --------------------------------------------------------------------------
# i - ZIP inconsistent with borough
# --------------------------------------------------------------------------
@pytest.mark.parametrize("variant", VARIANTS["i"])
def test_i_injected_pairs_never_occur_in_the_year(batch, ctx, variant):
    out = run(batch, "i", ctx, variant)
    rows, original = out.df[out.row_labels], batch[out.row_labels]
    counts = ctx.zip_borough_counts
    for zip_code, borough in zip(rows["incident_zip"], rows["borough"]):
        assert zip_code not in counts.index or counts.at[zip_code, borough] == 0
    replaced, kept = ("incident_zip", "borough") if variant == "zip_replaced" else ("borough", "incident_zip")
    assert (rows[replaced] != original[replaced]).all() and (rows[kept] == original[kept]).all()
    assert rows["borough"].isin(config.BOROUGHS).all() and rows["incident_zip"].str.fullmatch(r"\d{5}").all()
    if variant == "zip_replaced":  # the new ZIP clearly belongs to another borough
        new = counts.loc[rows["incident_zip"]]
        assert (new.max(axis=1) >= 0.95 * new.sum(axis=1)).all()


# --------------------------------------------------------------------------
# j - complaint type inconsistent with descriptor
# --------------------------------------------------------------------------
@pytest.mark.parametrize("variant", VARIANTS["j"])
def test_j_injected_pairs_never_occur_in_the_year(batch, ctx, variant):
    out = run(batch, "j", ctx, variant)
    rows = out.df[out.row_labels]
    assert (rows["descriptor"] != batch.loc[out.row_labels, "descriptor"]).all()
    for agency, complaint_type, descriptor in rows[["agency", "complaint_type", "descriptor"]].itertuples(index=False):
        assert not ctx.type_uses.at[complaint_type, descriptor]
        assert ctx.agency_uses.at[agency, descriptor] == (variant == "same_agency")


def spelling(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def test_j_never_swaps_in_another_spelling_of_a_descriptor_the_type_uses(reference_feed, batch_feed):
    """Regression: HPD back-filled its Sep/Oct 2023 requests in Title Case ('Heat/Hot Water' /
    'Entire Building') next to the upper-case originals. 'Heat/Hot Water' + 'ENTIRE BUILDING' is
    the most common HPD pair in another spelling, not a mismatch, yet a case-sensitive lookup
    called it "never seen" and drew it as the replacement (HPD's commonest descriptors)."""
    heat = np.flatnonzero((batch_feed["complaint_type"] == "HEAT/HOT WATER").to_numpy())[::2]
    batch_feed.loc[heat, ["complaint_type", "descriptor"]] = batch_feed.loc[heat, ["complaint_type", "descriptor"]].apply(lambda c: c.str.title())
    year = pd.concat([reference_feed, batch_feed], ignore_index=True)
    seen = {(spelling(c), spelling(d)) for c, d in year[["complaint_type", "descriptor"]].dropna().itertuples(index=False)}
    out = run(batch_feed, "j", InjectionContext.build(year), rate=0.2)
    rows = out.df[out.row_labels]
    assert (rows["complaint_type"] == "Heat/Hot Water").sum() > 20 and (rows["complaint_type"] == "HEAT/HOT WATER").sum() > 20
    for complaint_type, descriptor in rows[["complaint_type", "descriptor"]].itertuples(index=False):
        assert (spelling(complaint_type), spelling(descriptor)) not in seen, (complaint_type, descriptor)


def test_j_never_injects_a_catch_all_descriptor(batch, ctx):
    """Regression: catch-alls such as 'Other' and 'N/A' (among the commonest descriptors) were drawn
    as replacements. They contradict no complaint type, so a detector that judges meaning was
    charged a miss for being right. Neither variant may draw one, whatever the seed."""
    catch_alls = {spelling(d) for d in GENERIC_DESCRIPTORS}
    for variant in VARIANTS["j"]:
        drawn = []
        for seed in range(10):
            out = run(batch, "j", ctx, variant, seed=seed, rate=0.1)
            drawn += out.df.loc[out.row_labels, "descriptor"].tolist()
        assert len(drawn) >= 2_000, variant  # enough draws to have met a catch-all before the fix
        assert not [d for d in drawn if spelling(d) in catch_alls], variant


def test_j_pool_drops_exactly_the_catch_all_descriptors(reference_feed, batch, ctx):
    """Catch-alls leave the replacement pool in any spelling ('Other (Explain Below)' is listed as
    'Other - Explain Below'); a descriptor that merely starts with 'Other' names a subject and stays."""
    catch_alls = {spelling(d) for d in GENERIC_DESCRIPTORS}
    year = set(pd.concat([reference_feed, batch])["descriptor"].dropna())
    pool = set(ctx.descriptor_counts.index)
    assert pool == {d for d in year if spelling(d) not in catch_alls}
    assert "Other (Explain Below)" in year - pool
    assert {"Other Animal", "Other Store (Non-Food)"} <= pool


# --------------------------------------------------------------------------
# k - resolution description contradicting status
# --------------------------------------------------------------------------
@pytest.mark.parametrize("variant", VARIANTS["k"])
def test_k_text_contradicts_status(batch, ctx, variant):
    out = run(batch, "k", ctx, variant)
    rows = out.df[out.row_labels]
    texts = ctx.resolution_texts.loc[rows["resolution_description"]]
    assert (texts["n"] >= MIN_TEXT_ROWS).all()
    assert (batch.loc[out.row_labels, "status"] == "Closed").all()
    if variant == "closed_with_open_text":
        assert (rows["status"] == "Closed").all() and (texts["open_share"] >= 0.95).all()
        # Regression: always the row agency's own text (another agency's, e.g. a Parks tree work
        # order on an NYPD row, was used whenever the row's agency had none)
        assert (texts["agency"].to_numpy() == rows["agency"].to_numpy()).all()
        assert out.params["text_agency"] == dict(zip(np.flatnonzero(out.row_labels).tolist(), rows["agency"]))
    else:  # only the text still says the work is finished
        assert rows["status"].isin(REOPENED_STATUSES).all() and rows["closed_date"].isna().all()
        assert (texts["closed_share"] >= 0.95).all() and out.params["text_agency"] == {}


def test_k_open_texts_are_the_agency_s_own_and_evenly_spread(batch, ctx):
    """Regression: texts were drawn in proportion to their full-year frequency, so one template
    dominated the insertions, and rows of an agency without open texts got another agency's. Here
    one agency has three open texts, one of them 4,000 times more frequent than the others: only
    that agency's rows qualify, and no text makes up half of the insertions."""
    agency = batch.loc[batch["status"] == "Closed", "agency"].mode()[0]
    texts = pd.DataFrame(
        {"n": [100_000, 25, 25], "open_share": 1.0, "closed_share": 0.0, "agency": agency, "insertable_open": True, "insertable_closed": False},
        index=["Inspection scheduled.", "Crew assigned; work pending.", "Request under review."],
    )
    three_texts = dataclasses.replace(ctx, resolution_texts=texts)
    drawn = []
    for seed in range(5):
        out = run(batch, "k", three_texts, "closed_with_open_text", seed=seed)
        rows = out.df[out.row_labels]
        assert len(rows) == expected_k(len(batch)) and (rows["agency"] == agency).all()
        drawn += rows["resolution_description"].tolist()
    shares = pd.Series(drawn).value_counts(normalize=True)
    assert set(shares.index) == set(texts.index) and shares.max() < 0.5, shares.to_dict()


JAN_AUG, OCTOBER = "2023-03-15T12:00:00.000", "2023-10-20T12:00:00.000"


def text_rows(template: pd.DataFrame, text: str, n: int, status: str = "Open", created: str = JAN_AUG) -> pd.DataFrame:
    """n copies of the one-row frame `template` carrying resolution text `text` with `status`."""
    return template.loc[template.index.repeat(n)].assign(resolution_description=text, status=status, created_date=created)


def open_texts_put_in(batch: pd.DataFrame, ctx: InjectionContext, seeds) -> list[str]:
    drawn = []
    for seed in seeds:
        out = run(batch, "k", ctx, "closed_with_open_text", seed=seed, rate=0.1)
        drawn += out.df.loc[out.row_labels, "resolution_description"].tolist()
    return drawn


def is_neutral(text: str) -> bool:
    return any(part in re.sub(r"\s+", " ", text.lower()) for part in injector.NEUTRAL_OPEN_TEXTS)


def test_k_never_puts_in_a_status_neutral_open_text(reference_feed, batch, ctx):
    """Regression: any text >= 95 % open was used, so texts that say nothing about status ('Please
    call 311 for further information', 'has received and processed your complaint') were put on
    Closed rows, and a detector that judges meaning was charged a miss for being right. Here the
    row agency gets every excluded text as a frequent Jan-Aug open text, next to one that does say
    "not finished": only that one is drawn, whatever the seed (and none on the fixture year)."""
    agency = batch.loc[batch["status"] == "Closed", "agency"].mode()[0]
    template = batch[batch["agency"] == agency].head(1)
    pending = "An inspector will visit the location to investigate the condition."
    neutral = [part.capitalize() + "." for part in injector.NEUTRAL_OPEN_TEXTS]
    assert len(neutral) >= 6 and all(map(is_neutral, neutral)) and not is_neutral(pending)
    year = pd.concat([reference_feed, batch, *[text_rows(template, text, 25) for text in [*neutral, pending]]], ignore_index=True)
    drawn = open_texts_put_in(batch, InjectionContext.build(year), range(5))
    assert pending in drawn and not [text for text in drawn if is_neutral(text)]
    assert not [text for text in open_texts_put_in(batch, ctx, range(20)) if is_neutral(text)]


def test_k_puts_in_only_texts_open_on_jan_aug_rows_alone(reference_feed, batch, ctx):
    """Regression: "open" was decided on the full year, so a text first seen after August, or one
    open only thanks to Sep-Dec rows, was put in although it could not have been screened without
    evaluation data. Now every inserted text has >= 20 Jan-Aug rows, >= 95 % of them open."""
    agency = batch.loc[batch["status"] == "Closed", "agency"].mode()[0]
    template = batch[batch["agency"] == agency].head(1)
    late = "A specialist will contact you to arrange the inspection."  # October only
    mixed = "Your complaint is under review by the inspection unit."  # 90 % open in Jan-Aug, 99 % in the year
    kept = "An inspector will visit the location to investigate the condition."
    extra = [
        text_rows(template, late, 30, created=OCTOBER),
        text_rows(template, mixed, 18), text_rows(template, mixed, 2, status="Closed"), text_rows(template, mixed, 200, created=OCTOBER),
        text_rows(template, kept, 25),
    ]
    year_ctx = InjectionContext.build(pd.concat([reference_feed, batch, *extra], ignore_index=True))
    assert (year_ctx.resolution_texts.loc[[late, mixed, kept], "open_share"] >= 0.95).all()  # all three "open" on the full year
    drawn = open_texts_put_in(batch, year_ctx, range(5))
    assert kept in drawn and late not in drawn and mixed not in drawn
    assert open_texts_put_in(batch, year_ctx, [3]) == open_texts_put_in(batch, year_ctx, [3])  # same seed, same texts
    # the fixture year: each inserted text qualifies on its Jan-Aug rows (reference_feed) alone
    in_reference = reference_feed.groupby("resolution_description")["status"].agg(n="size", open_share=lambda s: s.isin(config.OPEN_STATUSES).mean())
    drawn = in_reference.reindex(sorted(set(open_texts_put_in(batch, ctx, range(20)))))  # a text without Jan-Aug rows: NaN, fails
    assert len(drawn) and (drawn["n"] >= MIN_TEXT_ROWS).all() and (drawn["open_share"] >= 0.95).all(), drawn


def reopened_texts(batch: pd.DataFrame, ctx: InjectionContext, seeds, rate: float = 0.1) -> list[str]:
    """The texts of the rows open_with_closed_text reopened, over these seeds."""
    drawn = []
    for seed in seeds:
        out = run(batch, "k", ctx, "open_with_closed_text", seed=seed, rate=rate)
        drawn += out.df.loc[out.row_labels, "resolution_description"].tolist()
    return drawn


def is_neutral_closed(text: str) -> bool:
    words = re.sub(r"\s+", " ", text.lower())  # once per text, not once per key (there are 166)
    return any(part in words for part in injector.NEUTRAL_CLOSED_TEXTS)


def test_k_never_reopens_a_row_whose_closed_text_does_not_say_finished(reference_feed, batch, ctx):
    """Regression: any Closed row with a >= 95 % closed text could be reopened, so texts that do not say
    the work is finished (NYPD 'reviewed your complaint and provided additional information below', a
    status pointer, a referral, a duplicate, a visit without access) became the only evidence of a
    contradiction they do not state, and a detector that judges meaning was charged a miss for being
    right. Here Closed rows carry every excluded text, each also a frequent Jan-Aug closed text, next to
    rows with one that does say "finished": only those are reopened (and no excluded text is, on the
    fixture year, whatever the seed)."""
    template = batch[batch["status"] == "Closed"].head(1)
    finished = "The agency inspected the condition and repaired it."
    neutral = [part.capitalize() + "." for part in injector.NEUTRAL_CLOSED_TEXTS]
    assert len(neutral) >= 150 and all(map(is_neutral_closed, neutral)) and not is_neutral_closed(finished)
    closed_rows = pd.concat([text_rows(template, text, 2, status="Closed", created=OCTOBER) for text in [*neutral, finished]], ignore_index=True)
    jan_aug = [text_rows(template, text, 25, status="Closed") for text in [*neutral, finished]]
    year_ctx = InjectionContext.build(pd.concat([reference_feed, closed_rows, *jan_aug], ignore_index=True))
    assert set(reopened_texts(closed_rows, year_ctx, [SEED], rate=1.0)) == {finished}  # every eligible row is reopened
    assert not [text for text in reopened_texts(batch, ctx, range(20)) if is_neutral_closed(text)]


def test_k_reopens_only_rows_whose_text_is_closed_on_jan_aug_rows_alone(reference_feed, batch, ctx):
    """Regression: "closed" was decided on the full year, so a row was reopened on the strength of a
    text first seen after August, or closed only thanks to Sep-Dec rows, which could not have been
    screened without evaluation data. Now every reopened row's text has >= 20 Jan-Aug rows, >= 95 % of
    them Closed."""
    template = batch[batch["status"] == "Closed"].head(1)
    late = "The inspector corrected the condition on site."  # October only
    mixed = "The agency repaired the reported defect."  # 90 % closed in Jan-Aug, 99 % in the year
    kept = "The agency completed the requested repair."
    assert not any(map(is_neutral_closed, [late, mixed, kept]))
    closed_rows = pd.concat([text_rows(template, text, 30, status="Closed", created=OCTOBER) for text in [late, mixed, kept]], ignore_index=True)
    extra = [
        text_rows(template, mixed, 18, status="Closed"), text_rows(template, mixed, 2), text_rows(template, mixed, 200, status="Closed", created=OCTOBER),
        text_rows(template, kept, 25, status="Closed"),
    ]
    year_ctx = InjectionContext.build(pd.concat([reference_feed, batch, closed_rows, *extra], ignore_index=True))
    assert (year_ctx.resolution_texts.loc[[late, mixed, kept], "closed_share"] >= 0.95).all()  # all three "closed" on the full year
    assert set(reopened_texts(closed_rows, year_ctx, [SEED], rate=1.0)) == {kept}  # every eligible row is reopened
    # the fixture year: each reopened row's text qualifies on its Jan-Aug rows (reference_feed) alone
    in_reference = reference_feed.groupby("resolution_description")["status"].agg(n="size", closed_share=lambda s: s.isin(config.CLOSED_STATUSES).mean())
    drawn = in_reference.reindex(sorted(set(reopened_texts(batch, ctx, range(20)))))  # a text without Jan-Aug rows: NaN, fails
    assert len(drawn) and (drawn["n"] >= MIN_TEXT_ROWS).all() and (drawn["closed_share"] >= 0.95).all(), drawn


def candidates_read(texts: list[str]) -> tuple[int, str]:
    """What READ_*_CANDIDATES holds for these candidate texts, worked out here rather than taken from the
    injector: how many, and the first 16 hex characters of the SHA-256 of their sorted normalised forms
    (lower case, runs of spaces collapsed), one per line."""
    normalised = sorted(re.sub(r"\s+", " ", text.lower()) for text in texts)
    return len(texts), hashlib.sha256("\n".join(normalised).encode("utf-8")).hexdigest()[:16]


def test_build_warns_when_a_screen_meets_unread_candidates(batch, monkeypatch):
    """Regression: NEUTRAL_OPEN_TEXTS and NEUTRAL_CLOSED_TEXTS are hand judgments about the Jan-Aug
    candidates of one data snapshot; after a re-download a candidate nobody read would have been used
    unscreened, silently. build() now compares the candidates with the count and fingerprint of those
    read (READ_*_CANDIDATES) and, when they differ, warns how many are new or missing; it never raises."""
    template = batch.head(1)
    pending, done, other = "An inspector will visit the location.", "The  condition was CORRECTED.", "The agency repaired the defect."
    tiny = pd.concat([text_rows(template, pending, 20), text_rows(template, done, 20, status="Closed")], ignore_index=True)
    n_open, n_closed = injector.READ_OPEN_CANDIDATES[0], injector.READ_CLOSED_CANDIDATES[0]
    with pytest.warns(UserWarning) as caught:
        InjectionContext.build(tiny)  # one candidate per screen, not the ones read
    messages = " | ".join(str(w.message) for w in caught)
    assert f"1 Jan-Aug open-text candidates, {n_open} were read (at least {n_open - 1} missing" in messages
    assert f"1 Jan-Aug closed-text candidates, {n_closed} were read (at least {n_closed - 1} missing" in messages
    monkeypatch.setattr(injector, "READ_OPEN_CANDIDATES", candidates_read([pending]))
    monkeypatch.setattr(injector, "READ_CLOSED_CANDIDATES", candidates_read([done]))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        InjectionContext.build(tiny)  # exactly the candidates that were read: silent
    assert not [w for w in caught if "text candidates" in str(w.message)]
    with pytest.warns(UserWarning, match=r"2 Jan-Aug closed-text candidates, 1 were read \(at least 1 new"):
        InjectionContext.build(pd.concat([tiny, text_rows(template, other, 20, status="Closed")], ignore_index=True))
    swapped = pd.concat([text_rows(template, pending, 20), text_rows(template, other, 20, status="Closed")], ignore_index=True)
    with pytest.warns(UserWarning, match=r"1 Jan-Aug closed-text candidates, 1 were read \(at least one new and one missing"):
        InjectionContext.build(swapped)  # same count, another text: only the fingerprint tells


def test_k_screens_reproduce_the_hand_decisions_on_the_real_jan_aug_texts(batch):
    """Both screens, checked against the REAL Jan-Aug candidate texts and the decisions a person
    recorded while reading them (tests/data/k_screen_decisions.json: 29 open texts, 415 closed
    texts). Unlike the tests above, the expected answers do not come from NEUTRAL_*_TEXTS, so a typo
    in a key (which would let a status-neutral text through) or a key that also hits a kept text
    (which would drop a legitimate one) fails here. Each text gets 20 Jan-Aug rows of its own status
    group, so it is a candidate exactly as in the real data - and the build must stay silent,
    which proves the file holds exactly the candidates the screens were read on."""
    entries = json.loads((Path(__file__).parent / "data" / "k_screen_decisions.json").read_text(encoding="utf-8"))["entries"]
    status = {"open": "In Progress", "closed": "Closed"}
    feed = pd.concat([text_rows(batch.head(1), e["text"], 20, status=status[e["kind"]]) for e in entries], ignore_index=True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        texts = injector._resolution_texts(feed)
    assert not [w for w in caught if "text candidates" in str(w.message)]  # the fixture is the read set
    for kind in ("open", "closed"):
        decided = {e["text"]: e["decision"] == "keep" for e in entries if e["kind"] == kind}
        screened = texts.loc[list(decided), f"insertable_{kind}"].to_dict()
        wrong = [text[:80] for text, keep in decided.items() if screened[text] != keep]
        assert not wrong, f"{kind} screen disagrees with the recorded decision for: {wrong}"
    counts = {kind: sum(e["decision"] == "keep" for e in entries if e["kind"] == kind) for kind in status}
    assert counts == {"open": 23, "closed": 239}


# --------------------------------------------------------------------------
# l - personal data in free text
# --------------------------------------------------------------------------
@pytest.mark.parametrize("variant", VARIANTS["l"])
def test_l_personal_data_is_present(batch, ctx, variant):
    out = run(batch, "l", ctx, variant)
    assert sorted(out.params["inserted"]) == np.flatnonzero(out.row_labels).tolist()
    assert sorted(out.params["placements"]) == sorted(out.params["inserted"])
    assert set(out.params["placements"].values()) == {"append", "replace"}  # both placements in use
    for pos, sentence in out.params["inserted"].items():
        text, original = out.df.at[pos, "resolution_description"], batch.at[pos, "resolution_description"]
        if out.params["placements"][pos] == "append":  # the whole template is kept, the sentence follows it
            assert text == f"{original.rstrip()} {sentence}"
        else:  # the sentence takes the place of exactly one sentence of the template; the rest is kept
            before, after = text.split(sentence)
            assert original.startswith(before) and original.endswith(after) and len(before) + len(after) < len(original)
            replaced = original[len(before) : len(original) - len(after)]
            assert replaced.strip() and not re.search(r"[.!?]\s+\S", replaced.strip())  # one sentence
            assert before == "" or re.search(r"[.!?]\s+$", before)  # it started where a sentence starts
        found = {kind for kind, pattern in [("phone", PHONE), ("email", EMAIL), ("name", LAST_NAME)] if pattern.search(sentence)}
        if variant in ("phone", "email"):
            assert found == {variant}
        elif variant == "mixed":
            assert "name" in found and found & {"phone", "email"}
        else:
            assert found == {"name"} and bool(NAME_CUE.search(sentence)) == (variant == "name_with_cue")


def test_l_phone_numbers_come_in_several_formats(batch, ctx):
    out = run(batch, "l", ctx, "phone")
    shapes = {re.sub(r"\d", "9", PHONE.search(s).group()) for s in out.params["inserted"].values()}
    assert len(shapes) >= 4
