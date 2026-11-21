"""Tests for project_b/detectors/completeness.py: (g) null spike in one agency's records and
(h) closed date earlier than created date.

Corruptions are made by hand here (blue team: inject.py is deliberately not used), following the
problem descriptions in DESIGN.md section 3. Measured rates are printed (`pytest -s`) and asserted
with slack: the fixtures are one real batch and a 15,000-row reference sample.
"""
from __future__ import annotations

import inspect
import math
import re
import time

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from project_b import config, llm
from project_b.core import DetectionResult, timed_detect
from project_b.detectors import completeness as C
from tests.fakes import FakeClient, rows_in_prompt

# Columns the null-spike injector may hit (DESIGN.md section 3, problem g); lat/lon go together.
SPIKE_COLUMNS = {
    "incident_zip": ["incident_zip"],
    "incident_address": ["incident_address"],
    "latitude+longitude": ["latitude", "longitude"],
    "descriptor": ["descriptor"],
    "location_type": ["location_type"],
    "city": ["city"],
    "community_board": ["community_board"],
    "resolution_description": ["resolution_description"],
}
SOURCE_FORMAT_OUT = "%Y-%m-%dT%H:%M:%S.000"


# ------------------------------------------------------------------------------------------
# hand-made corruptions
# ------------------------------------------------------------------------------------------
def make_null_spike(batch: pd.DataFrame, agency: str, columns: list[str], fraction: float, rng) -> tuple[pd.DataFrame, np.ndarray]:
    """Null out `fraction` of `agency`'s non-null values in `columns`; returns (batch, nulled rows)."""
    out = batch.copy()
    eligible = np.flatnonzero((batch["agency"] == agency).to_numpy() & batch[columns[0]].notna().to_numpy())
    rows = rng.choice(eligible, size=max(1, round(fraction * len(eligible))), replace=False)
    for column in columns:
        out.loc[rows, column] = np.nan
    labels = np.zeros(len(batch), dtype=bool)
    labels[rows] = True
    return out, labels


def make_closed_before_created(batch: pd.DataFrame, variant: str, rate: float, rng) -> tuple[pd.DataFrame, np.ndarray]:
    """closed_date moved before created_date on `rate` of the rows: 'minutes' (1-59 min before),
    'days' (1-30 days before) or 'year_typo' (closed year written as 2022)."""
    out = batch.copy()
    created, closed = parse(batch["created_date"]), parse(batch["closed_date"])
    eligible = np.flatnonzero((closed >= created).to_numpy() & batch["closed_date"].str.startswith("2023").to_numpy())
    rows = rng.choice(eligible, size=round(rate * len(batch)), replace=False)
    if variant == "year_typo":
        new = "2022" + batch["closed_date"].iloc[rows].str[4:]
    else:
        unit = "min" if variant == "minutes" else "D"
        high = 60 if variant == "minutes" else 31
        new = (created.iloc[rows] - pd.to_timedelta(rng.integers(1, high, len(rows)), unit=unit)).dt.strftime(SOURCE_FORMAT_OUT)
    out.loc[rows, "closed_date"] = new.to_numpy()
    labels = np.zeros(len(batch), dtype=bool)
    labels[rows] = True
    return out, labels


def parse(dates: pd.Series) -> pd.Series:
    return pd.to_datetime(dates, format=config.SOURCE_DATETIME_FORMAT, errors="coerce")


def real_closed_before_created(frame: pd.DataFrame) -> np.ndarray:
    """Pre-existing instances of problem h in the real data (DOT street-light requests)."""
    return (parse(frame["closed_date"]) < parse(frame["created_date"])).to_numpy()


def spike_cases(reference: pd.DataFrame, batch: pd.DataFrame) -> list[tuple[str, str]]:
    """(agency, column group) pairs the injector could pick: agency with >= 100 batch rows and a
    reference null rate < 10 % for that agency and column."""
    rows = batch["agency"].value_counts()
    ref_null = reference.drop(columns="agency").isna().groupby(reference["agency"]).mean()
    return [
        (agency, group)
        for agency in rows[rows >= 100].index
        for group, columns in SPIKE_COLUMNS.items()
        if agency in ref_null.index and ref_null.loc[agency, columns[0]] < 0.10
    ]


def names_cell(alert, agency: str, columns: list[str]) -> bool:
    return alert.segment.get("agency") == agency and bool(set(alert.columns) & set(columns))


def names_spike(result: DetectionResult, agency: str, columns: list[str]) -> bool:
    return any(names_cell(a, agency, columns) for a in result.alerts)


def caught(det, batch: pd.DataFrame, agency: str, group: str, fraction: float, rng) -> bool:
    """Make one spike and say whether `det` raised an alert naming its agency and column."""
    corrupted, _ = make_null_spike(batch, agency, SPIKE_COLUMNS[group], fraction, rng)
    return names_spike(det.detect(corrupted), agency, SPIKE_COLUMNS[group])


# ------------------------------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def stat_g(reference_feed):
    return C.StatNullSpikeBinomial().fit(reference_feed)


@pytest.fixture(scope="module")
def reference_clean_h(reference_feed):
    """The reference as clean.sanitize hands it to fit(): without closed-before-created rows."""
    return reference_feed[~real_closed_before_created(reference_feed)].reset_index(drop=True)


@pytest.fixture(scope="module")
def stat_h(reference_clean_h):
    return C.StatDurationOutlier().fit(reference_clean_h)


@pytest.fixture
def fake_llm():
    """Install a FakeClient (no network) for one test and always restore the real client."""

    def install(responder=None, unavailable: bool = False) -> FakeClient:
        client = FakeClient(responder, unavailable=unavailable)
        llm.set_client(client)
        return client

    yield install
    llm.set_client(None)


def answer_every_row(prompt, schema):
    """Fake model that reviews every row it is shown and flags nothing."""
    return {"rows": [{"id": r["id"], "flagged": False} for r in rows_in_prompt(prompt)], "issue_found": False, "reason": "fake"}


# ------------------------------------------------------------------------------------------
# module contract
# ------------------------------------------------------------------------------------------
def test_module_lists_six_detectors_with_metadata():
    assert [(d.problem, d.family) for d in C.DETECTORS] == [(p, f) for p in "gh" for f in ("rule", "stat", "llm")]
    for det in C.DETECTORS:
        assert det.name.startswith(f"{det.family}_") and det.description


def test_rule_has_one_limit_for_every_checked_column():
    assert sorted(C.MAX_NULL_RATE) == sorted(C.NULL_CHECK_COLUMNS)
    assert all(0 < limit <= 1 for limit in C.MAX_NULL_RATE.values())
    assert {"unique_key", "created_date", "agency"}.isdisjoint(C.NULL_CHECK_COLUMNS)


# ------------------------------------------------------------------------------------------
# provenance of the hand-set and tuned numbers: reference period (Jan-Aug) only
# ------------------------------------------------------------------------------------------
# Highest NORMAL per-agency null rate of each enforced rule column in the Jan-Aug 2023 reference batches
# (contiguous 5,000-row batches of the raw file, agencies with >= MIN_AGENCY_ROWS rows). For the coordinates
# it is DOT's second-highest batch: the highest, a DOT burst on 2023-04-30 (53.3 %), is a real spike.
JAN_AUG_HIGHEST_NORMAL_NULL_RATE = {
    "incident_zip": 0.569,  # DHS
    "city": 0.569,  # DHS
    "incident_address": 0.715,  # DOT
    "street_name": 0.715,  # DOT
    "latitude": 0.2875,  # DOT
    "longitude": 0.2875,
    "x_coordinate_state_plane": 0.2875,
    "y_coordinate_state_plane": 0.2875,
}
ALWAYS_FILLED = ["agency_name", "complaint_type", "status", "community_board", "borough", "open_data_channel_type", "park_borough"]


def comment_of(name: str) -> str:
    """The comment lines right above `name = ...` in completeness.py, plus that line (and its inline comment)."""
    lines = inspect.getsource(C).splitlines()
    end = next(i for i, line in enumerate(lines) if line.startswith(f"{name} = "))
    start = end
    while start > 0 and lines[start - 1].startswith("#"):
        start -= 1
    return "\n".join(lines[start : end + 1])


def test_rule_limits_sit_just_above_the_jan_aug_reference_rates():
    """The enforced limits follow from the reference period alone: each sits just above (by at most 5 points)
    the highest normal Jan-Aug rate; the always-filled columns (never null in Jan-Aug) keep the 5 % floor."""
    for column, rate in JAN_AUG_HIGHEST_NORMAL_NULL_RATE.items():
        assert rate < C.MAX_NULL_RATE[column] <= rate + 0.05, column
    assert all(C.MAX_NULL_RATE[column] == 0.05 for column in ALWAYS_FILLED)
    assert {c for c, limit in C.MAX_NULL_RATE.items() if limit < 1.0} == set(JAN_AUG_HIGHEST_NORMAL_NULL_RATE) | set(ALWAYS_FILLED)


def test_tuned_numbers_cite_reference_period_evidence_only():
    """Regression: RANGE_QUANTILE and MIN_RHO were picked while Sep-Dec (evaluation) false-alarm and recall
    figures were on screen, the rule limits were said to be read off "the 2023" batch rates (Sep-Dec included)
    and the docstring quoted a Sep-Dec false-positive rate of stat_duration_outlier. Evaluation-period numbers
    may not choose or justify anything: all three are now justified by Jan-Aug evidence (RANGE_QUANTILE and
    MIN_RHO re-checked by fitting on Jan-Jun and scoring Jul-Aug: every grid setting tied, so they stay) and the
    module quotes no evaluation-period figure."""
    assert not re.search(r"Sep-Dec|September|October|November|December", inspect.getsource(C))
    for name in ("MAX_NULL_RATE", "RANGE_QUANTILE", "MIN_RHO"):
        assert "Jan-Aug" in comment_of(name), name
    assert (C.RANGE_QUANTILE, C.MIN_RHO) == (0.99, 1e-5)  # what the Jan-Aug check kept


# ------------------------------------------------------------------------------------------
# g - null spike: rule and stat
# ------------------------------------------------------------------------------------------
def test_g_rule_and_stat_are_quiet_on_the_untouched_batch(batch_feed, stat_g):
    for det in (C.RuleNullRateThresholds(), stat_g):
        result = det.detect(batch_feed)
        assert not result.batch_flag, [a.message for a in result.alerts]
        assert result.row_flags is not None and not result.row_flags.any()


def test_g_stat_detects_injected_spikes_and_names_agency_and_column(reference_feed, batch_feed, stat_g):
    rng = np.random.default_rng(11)
    cases = spike_cases(reference_feed, batch_feed)
    found, localised, row_recall, wrong_alerts = [], [], [], 0
    for agency, group in cases:
        columns = SPIKE_COLUMNS[group]
        corrupted, labels = make_null_spike(batch_feed, agency, columns, rng.uniform(0.3, 0.9), rng)
        result = stat_g.detect(corrupted)
        found.append(result.batch_flag)
        localised.append(names_spike(result, agency, columns))
        row_recall.append((result.row_flags & labels).sum() / labels.sum())
        wrong_alerts += sum(not names_cell(a, agency, columns) for a in result.alerts)
    print(f"\n[g stat] {len(cases)} spikes, fraction U(0.3, 0.9): detected {np.mean(found):.2f}, right agency+column "
          f"{np.mean(localised):.2f}, nulled-row recall {np.mean(row_recall):.2f}, alerts on other cells {wrong_alerts}")
    assert len(cases) >= 40
    assert np.mean(localised) >= 0.95
    assert np.mean(row_recall) >= 0.95
    assert wrong_alerts <= 2


def test_g_stat_weak_spikes(reference_feed, batch_feed, stat_g):
    """The intensity sweep's weak end (0.5 and 0.9 lie inside the U(0.3, 0.9) test above)."""
    rng = np.random.default_rng(12)
    cases = spike_cases(reference_feed, batch_feed)
    rates = {fraction: round(float(np.mean([caught(stat_g, batch_feed, a, g, fraction, rng) for a, g in cases])), 2) for fraction in (0.1, 0.25)}
    print(f"\n[g stat] detection by nulled fraction: {rates}")
    assert rates[0.1] >= 0.6 and rates[0.25] >= 0.9


def test_g_rule_catches_spikes_in_enforced_columns_and_is_blind_elsewhere(reference_feed, batch_feed):
    rule, rng = C.RuleNullRateThresholds(), np.random.default_rng(13)
    cases = spike_cases(reference_feed, batch_feed)
    enforced = [(a, g) for a, g in cases if C.MAX_NULL_RATE[SPIKE_COLUMNS[g][0]] < 1.0]
    unenforced = [case for case in cases if case not in enforced]
    # A 90 % spike is above every enforced limit (the loosest is 75 %): the rule must name it.
    strong = [caught(rule, batch_feed, a, g, 0.9, rng) for a, g in enforced]
    # Unenforced columns (limit 1.0) never alert: the documented blind spot of one global limit.
    blind = [rule.detect(make_null_spike(batch_feed, a, SPIKE_COLUMNS[g], 0.9, rng)[0]).batch_flag for a, g in unenforced]
    typical = [caught(rule, batch_feed, a, g, rng.uniform(0.3, 0.9), rng) for a, g in cases]
    print(f"\n[g rule] 90 % spikes in enforced columns: {np.mean(strong):.2f} of {len(strong)}; unenforced columns "
          f"flagged: {sum(blind)} of {len(blind)}; all candidate columns, fraction U(0.3, 0.9): {np.mean(typical):.2f}")
    assert all(strong) and not any(blind)


def test_g_alert_fields_and_row_flags_are_the_spiking_agencys_nulls(batch_feed, stat_g):
    corrupted, _ = make_null_spike(batch_feed, "NYPD", ["incident_zip"], 0.8, np.random.default_rng(14))
    expected_rows = (corrupted["agency"] == "NYPD").to_numpy() & corrupted["incident_zip"].isna().to_numpy()
    for det, check_id in ((C.RuleNullRateThresholds(), "R-g1"), (stat_g, "S-g1")):
        result = det.detect(corrupted)
        (alert,) = result.alerts
        assert result.batch_flag and alert.check_id == check_id and alert.deterministic
        assert alert.columns == ["incident_zip"] and alert.segment == {"agency": "NYPD"}
        assert alert.observed.startswith("8") and alert.expected and alert.score is not None
        assert alert.n_rows == expected_rows.sum() and len(alert.rows) == C.MAX_EXAMPLE_ROWS
        assert np.array_equal(result.row_flags, expected_rows) and set(result.row_reasons) == set(np.flatnonzero(expected_rows))
        print(f"\n[g {det.family}] {alert.message}")


def test_g_batch_with_many_spikes_explains_every_row_and_stays_fast(batch_feed, stat_g):
    """Every checked column blank for every agency (say, a feed outage): one alert per (agency, column),
    each flagged row's reason lists every alert covering it. Regression: the row flags re-read the
    agency column once per alert and grew each reason string one alert at a time (~0.6 s here)."""
    blank = batch_feed.assign(**{column: np.nan for column in C.NULL_CHECK_COLUMNS})
    nypd = int(np.flatnonzero((blank["agency"] == "NYPD").to_numpy())[0])
    for det in (C.RuleNullRateThresholds(), stat_g):
        result = timed_detect(det, blank)
        nypd_alerts = [a for a in result.alerts if a.segment == {"agency": "NYPD"}]
        print(f"\n[g {det.family}] blank batch: {len(result.alerts)} alerts, {result.row_flags.sum()} rows flagged, "
              f"{result.usage.wall_s * 1000:.0f} ms")
        assert result.error is None and len(result.alerts) > 100 and len(nypd_alerts) >= 15
        assert result.row_reasons[nypd] == "; ".join(a.message for a in nypd_alerts)
        assert result.usage.wall_s < 0.5


def test_g_stat_tolerates_volatile_cells_but_not_new_nulls():
    """The reason for a beta-binomial: a column whose null rate swings between batches in the
    reference (here city: 0 % or 60 %) is not a spike at 70 %, a never-null column at 70 % is."""
    rng = np.random.default_rng(15)
    size = config.BATCH_SIZE
    city_null = np.concatenate([rng.random(size) < (0.6 if b % 2 else 0.0) for b in range(10)])
    reference = pd.DataFrame({"agency": "X", "city": np.where(city_null, None, "BRONX"), "incident_zip": "10451"}).astype("str")
    detector = C.StatNullSpikeBinomial().fit(reference)
    batch = pd.DataFrame({
        "agency": "X",
        "city": np.where(rng.random(size) < 0.7, None, "BRONX"),
        "incident_zip": np.where(rng.random(size) < 0.7, None, "10451"),
    }).astype("str")
    result = detector.detect(batch)
    print(f"\n[g stat] volatile-cell check: alerts {[a.columns for a in result.alerts]}, rho city {detector.baseline_.loc[('X', 'city'), 'rho']:.2f}")
    assert [a.columns for a in result.alerts] == [["incident_zip"]]
    rule_alerts = C.RuleNullRateThresholds().detect(batch).alerts  # one global limit (60 %) for both columns
    assert sorted(a.columns[0] for a in rule_alerts) == ["city", "incident_zip"]


def test_g_calibration_keeps_reference_alerts_under_target(stat_g):
    assert 0 < stat_g.threshold_ <= C.MAX_P_VALUE and stat_g.threshold_ == pytest.approx(10**stat_g.log10_threshold_)
    assert stat_g.reference_batch_far_ <= config.TARGET_BATCH_FAR
    # The cut-off (on log10 p) lets at most TARGET_BATCH_FAR of the batches through (alert: below it).
    batch_log10_p = np.array([-12.0] * 3 + [-8.0] * 5 + [0.0] * 92)
    cutoff = C._calibrated_cutoff(batch_log10_p)
    assert np.mean(batch_log10_p < cutoff) <= config.TARGET_BATCH_FAR and cutoff == -8.0
    assert C._calibrated_cutoff(np.zeros(100)) == np.log10(C.MAX_P_VALUE)
    # Regression: as plain floats, p-values of 1e-700 and 1e-500 are all 0.0, so the cut-off became 0.0
    # and no batch could ever alert again. On log10 they stay ordered and the strongest batches still alert.
    batch_log10_p = np.array([-700.0] * 3 + [-500.0] * 3 + [0.0] * 94)
    cutoff = C._calibrated_cutoff(batch_log10_p)
    assert cutoff == -500.0 and np.mean(batch_log10_p < cutoff) == 0.03


def test_g_stat_p_values_stay_exact_in_the_far_tail():
    """Regression: scipy's betabinom.sf is 1 - cdf. For 1,000 rows against a 1 % reference rate it gave
    p = 1.8e-10 for every null rate from 4 % to 100 % (rounding noise; the true values run from 1e-12
    down to 1e-1800), so alerts quoted wrong p-values and a cut-off calibrated below that noise would
    have missed every strong spike. The tail is now summed exactly in log space."""
    baseline = pd.DataFrame({"base_rate": [0.01], "rho": [C.MIN_RHO], "upper": [0.02]},
                            index=pd.MultiIndex.from_tuples([("X", "incident_zip")], names=C.CELL))
    nulls = [30, 40, 60, 70, 100, 300, 900, 1000]
    cells = pd.DataFrame({"batch": range(len(nulls)), "agency": "X", "n": 1000, "column": "incident_zip", "k": nulls})
    log10_p = C._spike_candidates(cells, baseline).set_index("k").log10_p_adj  # one cell per batch: no Bonferroni factor
    assert log10_p.is_monotonic_decreasing and log10_p.is_unique  # a stronger spike is always more significant
    strength = 1 / C.MIN_RHO - 1  # where scipy's 1 - cdf is still accurate, the two agree
    assert log10_p[30] == pytest.approx(np.log10(stats.betabinom.sf(29, 1000, 0.01 * strength, 0.99 * strength)), abs=1e-3)
    assert log10_p[40] < -12 and log10_p[900] < -1000  # scipy: -9.75 for both
    assert C._p_value_text(log10_p[30]) == "2.5e-07" and C._p_value_text(log10_p[900]).startswith("1e-15")


# ------------------------------------------------------------------------------------------
# g - LLM review
# ------------------------------------------------------------------------------------------
def test_g_llm_sends_a_stratified_sample_of_the_relevant_columns(batch_feed, fake_llm):
    client = fake_llm()
    result = C.LlmNullSpikeReview().detect(batch_feed, review_rows=np.arange(10))  # review_rows is ignored
    (call,) = client.calls
    assert call["tag"] == "g/llm_null_spike_review" and result.error is None and not result.batch_flag
    records = rows_in_prompt(call["prompt"])
    agencies = batch_feed["agency"].value_counts()
    judged = agencies[agencies >= C.MIN_AGENCY_ROWS]
    assert {k for r in records for k in r} == {"id", *C.NULL_LLM_COLUMNS}
    assert len(records) <= config.LLM_BATCH_SAMPLE_ROWS
    per_agency = pd.Series([r["agency"] for r in records]).value_counts()
    assert set(per_agency.index) == set(judged.index) and per_agency.max() <= C.LLM_ROWS_PER_AGENCY
    for r in records:  # ids are batch positions; nulls stay visible as null
        assert r["agency"] == batch_feed["agency"].iloc[r["id"]]
        assert (r["incident_zip"] is None) == pd.isna(batch_feed["incident_zip"].iloc[r["id"]])
    assert all(f'"{a}": {n}' in call["prompt"] for a, n in agencies.items())  # every agency's row count
    C.LlmNullSpikeReview().detect(batch_feed)
    assert client.calls[1]["prompt"] == call["prompt"]  # deterministic sample
    # Regression: the instructions gave "NYPD" as the example answer. The spiking agency is drawn in
    # proportion to its size, so a model echoing the example would be "right" for the wrong reason.
    assert not any(re.search(rf"\b{agency}\b", C.NULL_SPIKE_LLM_TASK) for agency in agencies.index)
    # Regression: with more qualifying "agencies" than sample rows (an agency column holding something
    # else), each got one row and the sample outgrew config.LLM_BATCH_SAMPLE_ROWS.
    C.LlmNullSpikeReview().detect(batch_feed.assign(agency=[f"A{i % 50}" for i in range(len(batch_feed))]))
    assert len(rows_in_prompt(client.calls[-1]["prompt"])) == config.LLM_BATCH_SAMPLE_ROWS


def test_g_llm_verdict_becomes_alert_and_row_flags(batch_feed, fake_llm):
    corrupted, _ = make_null_spike(batch_feed, "NYPD", ["incident_zip"], 0.9, np.random.default_rng(16))
    shown = {}

    def model(prompt, schema):
        """Stand-in for Gemini: names the spike, plus an unknown column and a row id it was never shown."""
        shown["nypd"] = [r for r in rows_in_prompt(prompt) if r["agency"] == "NYPD"]
        ids = [r["id"] for r in shown["nypd"] if r["incident_zip"] is None]
        return {
            "issue_found": True,
            "segment": "agency NYPD",
            "columns": ["incident_zip", "not_a_column"],
            "row_ids": ids + [999_999],
            "reason": "NYPD rows miss incident_zip",
            "observed": f"{len(ids)} of {len(shown['nypd'])} NYPD rows null",
            "expected": "filled",
            "confidence": 0.8,
        }

    fake_llm(model)
    result = C.LlmNullSpikeReview().detect(corrupted)
    (alert,) = result.alerts
    expected_rows = (corrupted["agency"] == "NYPD").to_numpy() & corrupted["incident_zip"].isna().to_numpy()
    # the sample makes the spike visible: most NYPD rows shown to the model miss their ZIP
    assert sum(r["incident_zip"] is None for r in shown["nypd"]) > len(shown["nypd"]) / 2
    assert result.batch_flag and alert.segment == {"agency": "NYPD"} and alert.columns == ["incident_zip"]
    assert alert.check_id == "L-g1" and alert.score == 0.8 and not alert.deterministic and alert.observed
    assert np.array_equal(result.row_flags, expected_rows) and alert.n_rows == expected_rows.sum()
    assert result.usage.llm_calls == 1 and result.usage.input_tokens > 0


def test_g_llm_unanswerable_cases(batch_feed, fake_llm):
    fake_llm(unavailable=True)
    result = C.LlmNullSpikeReview().detect(batch_feed)
    assert not result.batch_flag and result.error.startswith("llm_unavailable:")
    fake_llm(lambda prompt, schema: None)  # unparseable reply
    assert C.LlmNullSpikeReview().detect(batch_feed).error == "llm_unparseable_reply"
    fake_llm(lambda prompt, schema: {"issue_found": True, "segment": "the whole city", "columns": [], "reason": "vague"})
    vague = C.LlmNullSpikeReview().detect(batch_feed)
    assert vague.batch_flag and vague.alerts[0].segment == {"text": "the whole city"} and not vague.row_flags.any()


def test_g_llm_reads_loosely_formatted_replies(batch_feed, fake_llm):
    """Regression: a right verdict naming the column as 'Incident Zip', or as a bare string instead of a
    list, lost its column (and so its localisation and row flags) although the localisation metric
    accepts that spelling; a bare integer in row_ids raised TypeError."""
    corrupted, _ = make_null_spike(batch_feed, "NYPD", ["incident_zip"], 0.9, np.random.default_rng(19))
    fake_llm(lambda prompt, schema: {"issue_found": True, "segment": "NYPD", "columns": "Incident Zip", "row_ids": 12,
                                     "reason": "NYPD rows miss incident_zip"})
    result = C.LlmNullSpikeReview().detect(corrupted)
    (alert,) = result.alerts
    expected_rows = (corrupted["agency"] == "NYPD").to_numpy() & corrupted["incident_zip"].isna().to_numpy()
    assert result.error is None and alert.columns == ["incident_zip"] and alert.segment == {"agency": "NYPD"}
    assert np.array_equal(result.row_flags, expected_rows)

    def odd_fields(prompt, schema):
        shown = rows_in_prompt(prompt)[0]["id"]
        return {"issue_found": True, "segment": "several agencies", "columns": ["LATITUDE", "longitude ", "Latitude", "lat/lon"],
                "row_ids": [True, 3.5, str(shown), shown, float(shown), 999_999], "reason": "x"}

    fake_llm(odd_fields)
    result = C.LlmNullSpikeReview().detect(corrupted)
    (alert,) = result.alerts
    shown = rows_in_prompt(llm.get_client().calls[0]["prompt"])[0]["id"]
    # names matched once each, unknown ones dropped; only real sample ids kept, once (no agency: no row flags)
    assert alert.columns == ["latitude", "longitude"] and alert.rows == [shown] and alert.n_rows == 1
    assert result.error is None and not result.row_flags.any()


# ------------------------------------------------------------------------------------------
# h - closed before created: rule and stat
# ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["minutes", "days", "year_typo"])
def test_h_rule_and_stat_find_injected_rows(variant, batch_feed, stat_h):
    corrupted, labels = make_closed_before_created(batch_feed, variant, config.ROW_INJECTION_RATE, np.random.default_rng(21))
    truth = labels | real_closed_before_created(batch_feed)
    for det in (C.RuleClosedAfterCreated(), stat_h):
        result = det.detect(corrupted)
        recall = (result.row_flags & labels).sum() / labels.sum()
        false_flags = (result.row_flags & ~truth).sum()
        print(f"\n[h {det.family}] {variant}: recall {recall:.3f}, false flags {false_flags} of {(~truth).sum()} rows")
        assert recall == 1.0 and false_flags <= 5
        assert len(result.alerts) == 1 and set(result.row_reasons) == set(np.flatnonzero(result.row_flags))


def test_h_untouched_batch_flags_only_the_real_preexisting_rows(batch_feed, stat_h):
    real = real_closed_before_created(batch_feed)
    rule = C.RuleClosedAfterCreated().detect(batch_feed)
    stat = stat_h.detect(batch_feed)
    print(f"\n[h] untouched batch: {real.sum()} real closed<created rows; rule flags {rule.row_flags.sum()}, "
          f"stat flags {stat.row_flags.sum()} ({(stat.row_flags & ~real).sum()} others)")
    assert np.array_equal(rule.row_flags, real)
    assert (stat.row_flags & real).sum() == real.sum() and (stat.row_flags & ~real).mean() <= 2 * config.TARGET_ROW_FPR


def test_h_rule_explains_each_row(batch_feed):
    corrupted, labels = make_closed_before_created(batch_feed, "minutes", 0.01, np.random.default_rng(22))
    result = C.RuleClosedAfterCreated().detect(corrupted)
    (alert,) = result.alerts
    assert alert.check_id == "R-h1" and alert.columns == ["created_date", "closed_date"] and alert.deterministic
    assert alert.n_rows == result.row_flags.sum() and len(alert.rows) == C.MAX_EXAMPLE_ROWS
    assert alert.observed.startswith("created ") and alert.expected == "closed_date >= created_date" and 0 < alert.score < 0.05
    p = int(np.flatnonzero(labels)[0])
    assert corrupted["closed_date"].iloc[p] in result.row_reasons[p] and " min before created_date " in result.row_reasons[p]


def test_h_dates_in_another_format_are_not_this_problems_business(batch_feed):
    reformatted = batch_feed.copy()
    later = reformatted.index >= 2_000  # a format switch partway through (problem c)
    for column in ("created_date", "closed_date"):
        reformatted.loc[later, column] = parse(batch_feed[column])[later].dt.strftime("%m/%d/%Y %I:%M:%S %p")
    result = C.RuleClosedAfterCreated().detect(reformatted)
    assert not result.row_flags[later].any()


def test_h_stat_calibration(reference_clean_h, stat_h):
    dated = parse(reference_clean_h["created_date"]).notna() & parse(reference_clean_h["closed_date"]).notna()
    type_rows = reference_clean_h["complaint_type"][dated].value_counts()
    assert set(stat_h.bounds_) == set(type_rows.index[type_rows >= C.MIN_TYPE_ROWS])
    assert stat_h.default_bound_ >= 0  # the sanitised reference has no negative durations
    assert stat_h.reference_fpr_ <= 2 * config.TARGET_ROW_FPR
    print(f"\n[h stat] {len(stat_h.bounds_)} types with own bound, default bound {stat_h.default_bound_:.0f} s, reference FPR {stat_h.reference_fpr_:.5f}")


# ------------------------------------------------------------------------------------------
# h - LLM review
# ------------------------------------------------------------------------------------------
def date_order_model(prompt, schema):
    """Stand-in for Gemini: flags rows whose ISO closed_date sorts before created_date."""
    verdicts = []
    for r in rows_in_prompt(prompt):
        flagged = bool(r["closed_date"]) and r["closed_date"] < r["created_date"]
        verdicts.append({"id": r["id"], "flagged": flagged, "reason": "closed before created" if flagged else "",
                         "observed": f"created {r['created_date']}, closed {r['closed_date']}", "expected": "closed >= created", "confidence": 0.9})
    return {"rows": verdicts}


def test_h_llm_reviews_only_the_requested_rows_and_maps_verdicts_back(batch_feed, fake_llm):
    corrupted, labels = make_closed_before_created(batch_feed, "days", config.ROW_INJECTION_RATE, np.random.default_rng(31))
    real = real_closed_before_created(batch_feed)
    clean_rows = np.flatnonzero(~labels & ~real)[::97]
    review = np.concatenate([np.flatnonzero(labels)[:30], clean_rows])
    client = fake_llm(date_order_model)
    result = C.LlmDateOrderReview().detect(corrupted, review_rows=review)

    assert {c["tag"] for c in client.calls} == {"h/llm_date_order_review"}
    sent = [r for c in client.calls for r in rows_in_prompt(c["prompt"])]
    assert all(set(r) == {"id", "created_date", "closed_date", "status"} for r in sent)
    assert sorted(r["id"] for r in sent) == sorted(review.tolist())
    assert np.array_equal(np.flatnonzero(result.reviewed), np.sort(review))
    assert np.array_equal(np.flatnonzero(result.row_flags), np.sort(np.flatnonzero(labels)[:30]))
    p = int(np.flatnonzero(labels)[0])
    assert result.row_reasons[p] == "closed before created" and corrupted["closed_date"].iloc[p] in result.row_evidence[p]["observed"]
    assert result.alerts[0].check_id == "L-h1" and not result.alerts[0].deterministic and result.alerts[0].score == pytest.approx(0.9)
    assert result.usage.llm_calls == math.ceil(len(review) / config.LLM_ROWS_PER_CALL) == len(client.calls)


def test_h_llm_flags_exactly_what_the_model_says(batch_feed, fake_llm):
    fake_llm(lambda prompt, schema: {"rows": [{"id": r["id"], "flagged": r["id"] % 7 == 0, "reason": "x"} for r in rows_in_prompt(prompt)]
                                     + [{"id": 4_999_999, "flagged": True}]})  # an id that was never sent is ignored
    review = np.arange(100, 160)
    result = C.LlmDateOrderReview().detect(batch_feed, review_rows=review)
    assert np.array_equal(np.flatnonzero(result.row_flags), review[review % 7 == 0])


def test_h_llm_unanswerable_cases(batch_feed, fake_llm):
    fake_llm(unavailable=True)
    result = C.LlmDateOrderReview().detect(batch_feed, review_rows=np.arange(10))
    assert result.error.startswith("llm_unavailable:") and not result.row_flags.any() and not result.reviewed.any()
    fake_llm(lambda prompt, schema: {"rows": []})  # silence is not "all clean": every row abstains
    silent = C.LlmDateOrderReview().detect(batch_feed, review_rows=np.arange(10))
    assert silent.error is None and not silent.reviewed.any() and not silent.row_flags.any()
    assert silent.abstained == dict.fromkeys(range(10), llm.ABSTAIN_NO_VERDICT)
    skipped = fake_llm(lambda prompt, schema: {"rows": [{"id": r["id"], "flagged": True} for r in rows_in_prompt(prompt)[:5]]})
    partial = C.LlmDateOrderReview().detect(batch_feed, review_rows=np.arange(10))
    assert partial.row_flags.sum() == 5 and partial.reviewed.sum() == 5 and "5 rows got no verdict" in partial.alerts[0].message
    assert partial.abstained == dict.fromkeys(range(5, 10), llm.ABSTAIN_NO_VERDICT)
    assert len(skipped.calls) == 1
    no_dates = fake_llm()
    result = C.LlmDateOrderReview().detect(batch_feed.drop(columns="closed_date"), review_rows=np.arange(10))
    assert not no_dates.calls and result.error is None and not result.batch_flag and result.reviewed.sum() == 10


# ------------------------------------------------------------------------------------------
# robustness: detect() never raises on a corrupted batch
# ------------------------------------------------------------------------------------------
def _with_duplicate_rows(b):
    return pd.concat([b, b.iloc[100:300]], ignore_index=True)


def _reformat_dates(b):
    out = b.copy()
    for column in ("created_date", "closed_date"):
        out[column] = parse(b[column]).dt.strftime("%m/%d/%Y %I:%M:%S %p")
    return out


def _strings_in_numbers(b):
    out = b.copy()
    out["latitude"] = b["latitude"].astype(object)
    out.loc[::50, "latitude"] = "N/A"
    out["unique_key"] = b["unique_key"].astype(str)
    return out


CORRUPTIONS = {
    "drop_agency_and_dates": lambda b: b.drop(columns=["agency", "created_date", "closed_date"]),
    "rename_columns": lambda b: b.rename(columns={"agency": "Agency", "closed_date": "Closed Date", "complaint_type": "problem"}),
    "dates_as_datetime": lambda b: b.assign(created_date=parse(b["created_date"]), closed_date=parse(b["closed_date"])),
    "dates_reformatted": _reformat_dates,
    "numbers_as_strings": lambda b: b.astype({"latitude": str, "longitude": str, "incident_zip": object}),
    "strings_in_numeric": _strings_in_numbers,
    "agency_as_numbers": lambda b: b.assign(agency=np.arange(len(b)) % 7),
    "agency_all_null": lambda b: b.assign(agency=np.nan),
    "only_the_agency_column": lambda b: b[["agency"]],
    "dates_as_numbers": lambda b: b.assign(created_date=np.arange(len(b)), closed_date=np.arange(len(b)) - 1.0),
    "everything_null": lambda b: pd.DataFrame(np.nan, index=b.index, columns=b.columns, dtype=object),
    "duplicate_rows": _with_duplicate_rows,
    "duplicate_column_names": lambda b: pd.concat([b, b[["agency", "closed_date"]]], axis=1),
    "shuffled_index": lambda b: b.set_index(np.arange(len(b))[::-1] + 10_000),
    "single_row": lambda b: b.iloc[:1],
    "empty": lambda b: b.iloc[:0],
}


@pytest.mark.parametrize("corruption", list(CORRUPTIONS))
def test_detect_never_raises_on_corrupted_batches(corruption, batch_feed, stat_g, stat_h, fake_llm):
    fake_llm(answer_every_row)
    batch = CORRUPTIONS[corruption](batch_feed)
    for det in (C.RuleNullRateThresholds(), stat_g, C.LlmNullSpikeReview(), C.RuleClosedAfterCreated(), stat_h, C.LlmDateOrderReview()):
        result = timed_detect(det, batch, review_rows=np.arange(min(len(batch), 30)))
        assert result.error is None, f"{det.name} on {corruption}: {result.error}"
        if result.row_flags is not None:
            assert result.row_flags.shape == (len(batch),) and result.row_flags.dtype == bool


# ------------------------------------------------------------------------------------------
# speed
# ------------------------------------------------------------------------------------------
def test_fit_and_detect_timings(reference_feed, reference_clean_h, batch_feed, fake_llm):
    fake_llm(answer_every_row)
    lines = []
    for cls in C.DETECTORS:
        reference = reference_clean_h if cls.problem == "h" else reference_feed
        t0 = time.perf_counter()
        det = cls().fit(reference)
        fit_s = time.perf_counter() - t0
        detect_s = np.median([timed_detect(det, batch_feed, review_rows=np.arange(100)).usage.wall_s for _ in range(3)])
        lines.append(f"{cls.name}: fit {fit_s * 1000:.0f} ms ({len(reference)} rows), detect {detect_s * 1000:.0f} ms")
        if cls.family != "llm":
            assert detect_s < 1.0
    ten_x = pd.concat([reference_feed] * 10, ignore_index=True)
    for cls in (C.StatNullSpikeBinomial, C.StatDurationOutlier):
        t0 = time.perf_counter()
        cls().fit(ten_x)
        fit_s = time.perf_counter() - t0
        lines.append(f"{cls.name}: fit {fit_s:.2f} s on {len(ten_x)} rows -> ~{fit_s * 2.1e6 / len(ten_x):.0f} s at 2.1 M rows")
        assert fit_s * 2.1e6 / len(ten_x) < 60
    # The 10x fixture repeats the same 3 batches, so no reference cell is above its normal range and the
    # p-value step of the null-spike fit never runs. Time that step on a full reference period's worth of
    # (batch, agency, column) counts: 420 batches of 5,000 rows, rates that drift from batch to batch.
    cells = simulated_reference_cells(n_batches=420, rng=np.random.default_rng(41))
    t0 = time.perf_counter()
    candidates = C._spike_candidates(cells, C._null_baseline(cells))
    calibration_s = time.perf_counter() - t0
    lines.append(f"{C.StatNullSpikeBinomial.name}: baseline + p-values for {len(cells):,} reference cells "
                 f"({len(candidates):,} above their range) in {calibration_s:.2f} s")
    assert len(candidates) > 500 and calibration_s < 15
    print("\n" + "\n".join(lines))


def simulated_reference_cells(n_batches: int, rng) -> pd.DataFrame:
    """What _agency_null_counts returns for a reference period of n_batches batches: 14 agencies with
    NYC-like shares of the rows, null rates from 0 to 90 % per (agency, column), drifting by batch."""
    shares = np.array([0.44, 0.19, 0.1, 0.07, 0.06, 0.04, 0.035, 0.025, 0.02, 0.014, 0.011, 0.007, 0.0005, 0.0001])
    rows = rng.multinomial(config.BATCH_SIZE, shares / shares.sum(), size=n_batches)  # (batch, agency)
    rates = rng.choice([0.0, 0.001, 0.01, 0.05, 0.2, 0.5, 0.9], size=(len(shares), len(C.NULL_CHECK_COLUMNS)))
    n = np.repeat(rows[:, :, None], len(C.NULL_CHECK_COLUMNS), axis=2)  # (batch, agency, column)
    k = rng.binomial(n, np.clip(rates * rng.lognormal(0, 0.3, size=n.shape), 0, 1))
    batch, agency, column = np.indices(n.shape).reshape(3, -1)
    cells = pd.DataFrame({"batch": batch, "agency": np.array([f"A{i}" for i in range(len(shares))])[agency], "n": n.ravel(),
                          "column": np.array(C.NULL_CHECK_COLUMNS)[column], "k": k.ravel()})
    return cells[cells.n > 0].reset_index(drop=True)  # a groupby has no empty groups
