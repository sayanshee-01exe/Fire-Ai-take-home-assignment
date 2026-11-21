"""Smoke tests for project_b/evaluate.py.

The real injector and detectors are built by other people, so the harness is exercised with
small fakes that behave like them: a fake injector (problems a, d, e, g), fake detectors of
every family (one that crashes, one whose fit fails, one whose LLM is unreachable) and the
scripted FakeClient - never the real Gemini API. Batches are 250 rows, so a run takes seconds;
it runs once per module and the tests below each check one part of its output.
"""
from __future__ import annotations

import datetime
import json
import pickle
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from project_b import config, evaluate, llm
from project_b.core import Detector, Usage
from project_b.data.load import to_feed
from tests.fakes import FakeClient, rows_in_prompt

BUDGET = evaluate.Budget(
    n_corrupted=3, n_clean=3, llm_n_batches=2, llm_rows_injected=4, llm_rows_clean=4,
    llm_windows_per_batch=1, llm_repeats=2, sweep_n_batches=2, batch_size=250,
)
TIMING_FIELDS = ("wall_s",)  # the only record field allowed to differ between two identical runs


# --------------------------------------------------------------------------
# Fake injector (same shape as inject.Injected / inject.inject / inject.as_clean)
# --------------------------------------------------------------------------
@dataclass
class FakeInjected:
    df: pd.DataFrame
    row_labels: np.ndarray
    batch_label: bool
    problem: str
    params: dict
    row_variant: np.ndarray | None = None


def fake_inject(batch, problem, rng, ctx, rate=0.02, fraction=0.6):
    assert ctx == {"fake": "context"}  # the pickled context reaches the injector
    df = batch.reset_index(drop=True)
    labels = np.zeros(len(df), dtype=bool)
    k = max(1, round(rate * len(df)))
    if problem == "a":
        df = df.rename(columns={"incident_zip": "zip_code"})
        return FakeInjected(df, labels, True, "a", {"variant": "rename", "columns": ["incident_zip"], "new_name": "zip_code"})
    if problem == "d":
        rows = rng.choice(np.flatnonzero(df["latitude"].notna().to_numpy()), size=k, replace=False)
        df.loc[rows, ["latitude", "longitude"]] = df.loc[rows, ["longitude", "latitude"]].to_numpy()
        labels[rows] = True
        return FakeInjected(df, labels, True, "d", {"n_injected": k})
    if problem == "e":  # copies of late rows appended at the end, so originals fall inside a window
        sources = rng.choice(np.arange(len(df) - 20, len(df)), size=min(k, 20), replace=False)
        copy_of = {len(df) + i: int(source) for i, source in enumerate(sources)}  # like inject.py: copy -> original
        df = pd.concat([df, df.iloc[sources]], ignore_index=True)
        labels = np.r_[labels, np.ones(len(sources), dtype=bool)]
        return FakeInjected(df, labels, True, "e", {"n_injected": len(sources), "copy_of": copy_of})
    agency = df["agency"].value_counts().index[0]  # problem g
    eligible = np.flatnonzero((df["agency"].eq(agency) & df["incident_zip"].notna()).to_numpy())
    rows = rng.choice(eligible, size=round(fraction * len(eligible)), replace=False)
    df.loc[rows, "incident_zip"] = np.nan
    labels[rows] = True
    return FakeInjected(df, labels, True, "g", {"agency": agency, "columns": ["incident_zip"], "fraction": fraction})


def fake_as_clean(batch):
    df = batch.reset_index(drop=True)
    return FakeInjected(df, np.zeros(len(df), dtype=bool), False, "clean", {})


FAKE_INJECTOR = SimpleNamespace(inject=fake_inject, as_clean=fake_as_clean)


# --------------------------------------------------------------------------
# Fake detectors
# --------------------------------------------------------------------------
class RuleColumns(Detector):
    problem, family, name = "a", "rule", "rule_fake_columns"

    def detect(self, batch, review_rows=None):
        missing = [c for c in config.COLUMNS if c not in batch.columns]
        extra = [c for c in batch.columns if c not in config.COLUMNS]
        alerts = [self.alert(f"{c} missing", check_id="T-a1", columns=[c, *extra], observed=extra, expected=c) for c in missing]
        return self.result(bool(alerts), alerts=alerts)


class LlmUnreachable(Detector):
    problem, family, name = "a", "llm", "llm_fake_schema"

    def detect(self, batch, review_rows=None):
        return self.result(False, error="llm_unavailable: no cached answer and no key")


class RuleLatitude(Detector):
    problem, family, name = "d", "rule", "rule_fake_latitude"

    def detect(self, batch, review_rows=None):
        flags = (batch["latitude"] < 0).to_numpy()
        rows = np.flatnonzero(flags)
        alerts = [self.alert(f"{len(rows)} negative latitudes", check_id="T-d1", columns=["latitude"], rows=rows[:20].tolist(),
                             n_rows=len(rows), observed=float(batch["latitude"].min()), expected=">= 40.47")] if len(rows) else []
        return self.result(bool(len(rows)), alerts=alerts, row_flags=flags)


class StatCrash(Detector):
    problem, family, name = "d", "stat", "stat_fake_crash"

    def detect(self, batch, review_rows=None):
        raise RuntimeError("boom")


def llm_row_result(det: Detector, batch: pd.DataFrame, positions: np.ndarray, verdicts: dict, usage: Usage):
    """What a per-row LLM detector returns (DESIGN section 4 conventions)."""
    flags, reviewed = np.zeros(len(batch), dtype=bool), np.zeros(len(batch), dtype=bool)
    reviewed[positions] = True
    evidence = {}
    for pos, verdict in verdicts.items():
        if verdict.get("flagged"):
            flags[pos] = True
            evidence[pos] = {"observed": verdict.get("observed"), "expected": verdict.get("expected"), "confidence": 0.9}
    rows = np.flatnonzero(flags)
    alerts = [det.alert(f"{len(rows)} rows flagged", columns=["latitude"], rows=rows[:20].tolist(), observed="see rows")] if len(rows) else []
    result = det.result(bool(len(rows)), alerts=alerts, row_flags=flags, reviewed=reviewed, row_evidence=evidence)
    result.usage.add(usage)
    return result


class LlmCoordinates(Detector):
    problem, family, name = "d", "llm", "llm_fake_coordinates"

    def detect(self, batch, review_rows=None):
        positions = np.asarray(review_rows)
        records = [{"id": int(i), "latitude": batch["latitude"].iat[i], "longitude": batch["longitude"].iat[i]} for i in positions]
        try:
            verdicts, usage, _ = llm.review_rows("TASK=coordinates", records, tag="d/test")
        except llm.LLMUnavailable as exc:
            return self.result(False, error=f"llm_unavailable: {exc}")
        return llm_row_result(self, batch, positions, verdicts, usage)


class RuleDuplicates(Detector):
    problem, family, name = "e", "rule", "rule_fake_duplicates"

    def detect(self, batch, review_rows=None):
        flags = batch.duplicated(keep="first").to_numpy()
        return self.result(bool(flags.any()), row_flags=flags)


class LlmWindows(Detector):
    problem, family, name = "e", "llm", "llm_fake_windows"

    def detect(self, batch, review_rows=None):
        positions = np.asarray(review_rows)
        verdicts, total = {}, Usage()
        for run in np.split(positions, np.flatnonzero(np.diff(positions) != 1) + 1):  # contiguous runs
            records = [{"id": int(i), "key": f"{batch['unique_key'].iat[i]}|{batch['created_date'].iat[i]}"} for i in run]
            found, usage, _ = llm.review_rows("TASK=windows", records, tag="e/test", rows_per_call=len(records))
            verdicts.update(found)
            total.add(usage)
        return llm_row_result(self, batch, positions, verdicts, total)


class RuleNulls(Detector):
    problem, family, name = "g", "rule", "rule_fake_nulls"

    def detect(self, batch, review_rows=None):
        rates = batch["incident_zip"].isna().groupby(batch["agency"]).agg(["mean", "size"])
        spikes = rates[(rates["size"] >= 20) & (rates["mean"] > 0.3)]
        alerts = [self.alert(f"{agency}: zip null rate {row['mean']:.0%}", check_id="T-g1", columns=["incident_zip"],
                             segment={"agency": agency}, observed=row["mean"], expected="<= 30 %") for agency, row in spikes.iterrows()]
        return self.result(bool(alerts), alerts=alerts)


class StatFitFails(Detector):
    problem, family, name = "g", "stat", "stat_fake_fit_fails"

    def fit(self, reference):
        raise ValueError("cannot fit")

    def detect(self, batch, review_rows=None):
        raise AssertionError("a detector whose fit failed must not run")


def fake_detectors() -> list[Detector]:
    classes = (RuleColumns, LlmUnreachable, RuleLatitude, StatCrash, LlmCoordinates, RuleDuplicates, LlmWindows, RuleNulls, StatFitFails)
    return [cls() for cls in classes]


def responder(prompt, schema):
    """FakeClient answers: flag negative latitudes (d) and repeats within a window (e)."""
    rows = rows_in_prompt(prompt)
    if "TASK=coordinates" in prompt:
        return {"rows": [{"id": r["id"], "flagged": r["latitude"] is not None and r["latitude"] < 0, "reason": "latitude below zero",
                          "observed": str(r["latitude"]), "expected": "40.5 to 40.9"} for r in rows]}
    seen, verdicts = set(), []
    for r in rows:
        verdicts.append({"id": r["id"], "flagged": r["key"] in seen, "observed": r["key"]})
        seen.add(r["key"])
    return {"rows": verdicts}


# --------------------------------------------------------------------------
# One run of the harness, shared by the tests below
# --------------------------------------------------------------------------
def write_derived_dir(folder, reference_feed, october_feed):
    """A derived-data folder like ensure_derived() builds: the reference sample (Jan-Aug) plus
    one day of October, which run() cuts into 20 batches of 250 rows."""
    pool = pd.concat([reference_feed, october_feed], ignore_index=True)
    pool.to_parquet(folder / evaluate.POOL_FILE, index=False)
    pool.to_parquet(folder / evaluate.FEED_FILE, index=False)
    (folder / evaluate.COUNTS_FILE).write_text(json.dumps({"rows_in": len(pool), "rows_kept": len(pool), "flagged_per_issue": {}}))
    with open(folder / evaluate.CONTEXT_FILE, "wb") as fh:
        pickle.dump({"fake": "context"}, fh)
    return folder


def run_harness(derived, out_dir) -> tuple[dict, list[dict]]:
    summary = evaluate.run(derived, fake_detectors(), BUDGET, out_dir, injector=FAKE_INJECTOR)
    lines = (out_dir / evaluate.RECORDS_FILE).read_text(encoding="utf-8").splitlines()
    return summary, [json.loads(line) for line in lines]


@pytest.fixture(scope="module")
def fake_llm():
    client = FakeClient(responder)
    llm.set_client(client)
    yield client
    llm.set_client(None)


@pytest.fixture(scope="module")
def smoke(tmp_path_factory, reference_feed, batch_raw, fake_llm):
    derived = write_derived_dir(tmp_path_factory.mktemp("derived"), reference_feed, to_feed(batch_raw))
    out = tmp_path_factory.mktemp("results")
    summary, records = run_harness(derived, out)
    detectors = {e["detector"]: e for e in summary["detectors"]}
    return SimpleNamespace(derived=derived, out=out, summary=summary, records=records, detectors=detectors)


# --------------------------------------------------------------------------
# End-to-end checks
# --------------------------------------------------------------------------
def test_outputs_and_records(smoke, fake_llm):
    assert json.loads((smoke.out / evaluate.SUMMARY_FILE).read_text(encoding="utf-8")) == smoke.summary
    assert {r["kind"] for r in smoke.records} == {"corrupted", "clean", "sweep", "determinism", "raw_background"}
    # one schema for every record; per-row LLM records add the abstention fields (rule / stat records keep their shape)
    base = set(smoke.records[0])
    for r in smoke.records:
        per_row_llm = r["family"] == "llm" and r["problem"] in "de"
        assert set(r) == (base | set(evaluate.ABSTENTION_KEYS) if per_row_llm else base)
    assert "stat_fake_fit_fails" not in {r["detector"] for r in smoke.records}
    assert fake_llm.default_repeat == 0  # reset after the determinism repeats


def test_row_level_scores(smoke):
    rule = smoke.detectors["rule_fake_latitude"]
    assert rule["status"] == "ok" and rule["detection"]["value"] == 1.0 and rule["detection"]["n"] == 15
    assert rule["row_fpr"]["value"] == 0.0 and rule["precision_at_rate"] == 1.0 and rule["clarity"]["mean"] == 5
    assert rule["rows_per_s"] > 0 and rule["cost_per_million_rows"]["usd"] > 0
    # no errors: every clean batch and every clean row is in its false-alarm rates
    assert (rule["n_corrupted_errors"], rule["n_clean_errors"]) == (0, 0)
    assert (rule["batch_far"]["n"], rule["row_fpr"]["n"]) == (BUDGET.n_clean, BUDGET.n_clean * BUDGET.batch_size)
    crash = smoke.detectors["stat_fake_crash"]
    assert crash["status"] == "failed" and "boom" in crash["note"] and crash["n_errors"] == 6
    assert crash["detection"]["value"] == 0.0  # a crash flags nothing: every injected row is a miss
    # ... but on a clean batch a crash is no verdict, not a correct "no alarm": no false-alarm rate, never 0 %
    assert (crash["n_corrupted_errors"], crash["n_clean_errors"]) == (3, 3)
    assert crash["row_fpr"]["value"] is None and crash["batch_far"]["value"] is None


def test_llm_sample_scores_and_costs(smoke):
    model = smoke.detectors["llm_fake_coordinates"]
    assert model["status"] == "ok" and model["detection"]["value"] == 1.0 and model["detection"]["n"] == 4
    assert model["row_fpr"]["n"] == 4 and model["grounding"]["value"] == 1.0
    assert model["determinism"]["value"] == 1.0 and model["determinism"]["n"] == 4
    # it read ~4 rows per batch: "any alert in the batch" would not compare with a 250-row read
    assert "batch_far" not in model and "batch_detection" not in model
    assert "batch_far" in smoke.detectors["rule_fake_latitude"]
    assert model["cost_per_million_rows"]["usd"] is None and "fake-model" in model["cost_per_million_rows"]["note"]
    # 2 corrupted batches x (2 injected + 2 other rows) + 2 clean batches x 2 rows, one call per batch
    assert model["llm_throughput"]["rows_per_call"] == pytest.approx(12 / 4)


def test_rule_and_stat_are_rescored_on_the_llms_rows(smoke):
    same = {row["detector"]: row for row in smoke.summary["same_rows"]["d"]}
    assert list(same) == ["rule_fake_latitude", "stat_fake_crash", "llm_fake_coordinates"]
    assert same["rule_fake_latitude"]["detection"]["n"] == same["llm_fake_coordinates"]["detection"]["n"] == 4
    assert same["stat_fake_crash"]["detection"]["value"] == 0.0
    # clean rows: the rule and the LLM judged the same 4; the crashed stat detector judged none (no rate, not 0 %)
    assert same["rule_fake_latitude"]["row_fpr"]["n"] == same["llm_fake_coordinates"]["row_fpr"]["n"] == 4
    assert same["stat_fake_crash"]["row_fpr"]["value"] is None


def test_window_problem_reviews_contiguous_runs(smoke):
    windows = smoke.detectors["llm_fake_windows"]
    assert windows["detection"]["value"] == 1.0 and windows["row_fpr"]["value"] == 0.0
    reviewed = [r["n_reviewed"] for r in smoke.records if r["detector"] == "llm_fake_windows" and r["kind"] == "corrupted"]
    assert reviewed == [config.LLM_BATCH_SAMPLE_ROWS] * BUDGET.llm_n_batches
    assert windows["window_reach"]["value"] == 1.0  # every fake copy sits < 40 rows after its original
    assert windows["window_reach"]["n"] == 5 * BUDGET.llm_n_batches


def test_batch_level_scores_and_statuses(smoke):
    columns = smoke.detectors["rule_fake_columns"]
    assert columns["detection"]["value"] == 1.0 and columns["batch_far"]["value"] == 0.0
    assert columns["localisation"]["value"] == 1.0 and columns["clarity"]["mean"] == 5
    assert smoke.detectors["rule_fake_nulls"]["localisation"]["value"] == 1.0
    unreachable = smoke.detectors["llm_fake_schema"]
    assert unreachable["status"] == "not run" and "detection" not in unreachable  # no invented numbers
    assert any(r["detector"] == "llm_fake_schema" and r["error"].startswith("llm_unavailable") for r in smoke.records)
    fit_failed = smoke.detectors["stat_fake_fit_fails"]
    assert fit_failed["status"] == "failed" and fit_failed["note"].startswith("fit: ValueError")


def test_sweeps_and_background(smoke):
    rates = {(r["detector"], r["rate"]): r for r in smoke.summary["rate_sweep"]}
    assert {rate for _, rate in rates} == set(config.RATE_SWEEP)
    assert rates[("rule_fake_latitude", 0.1)]["detection"]["value"] == 1.0
    spikes = {r["fraction"]: r for r in smoke.summary["null_spike_sweep"]}
    assert set(spikes) == set(evaluate.NULL_SPIKE_FRACTIONS)
    assert spikes[0.1]["detection"]["value"] == 0.0 and spikes[0.9]["detection"]["value"] == 1.0
    background = {r["detector"]: r for r in smoke.summary["background"]}
    assert set(background) == {"rule_fake_columns", "rule_fake_latitude", "stat_fake_crash", "rule_fake_duplicates", "rule_fake_nulls"}
    assert background["rule_fake_latitude"]["n_batches"] == BUDGET.n_clean
    assert background["stat_fake_crash"]["n_errors"] == BUDGET.n_clean
    assert background["rule_fake_latitude"]["flagged_per_1000_rows"] == 0.0
    assert background["rule_fake_columns"]["flagged_per_1000_rows"] is None  # judges batches, flags no rows


def test_markdown_tables(smoke):
    tables = (smoke.out / evaluate.TABLES_FILE).read_text(encoding="utf-8")
    for text in ("## d - Latitude and longitude swapped", "not run: llm_unavailable", "Same inputs as the LLM (d)",
                 "Window reach: a 40-row window", "## Injection-rate sweep", "## Null-spike intensity sweep", "## Background"):
        assert text in tables
    assert "`stat_fake_crash` errored on 3 of 3 corrupted batches (scored as misses) and 3 of 3 clean batches" in tables
    assert "`rule_fake_latitude` errored" not in tables  # no note for a detector without errors


def test_same_seed_gives_identical_records_except_timings(smoke, tmp_path):
    _, again = run_harness(smoke.derived, tmp_path)

    def untimed(records):
        return [{k: v for k, v in r.items() if k not in TIMING_FIELDS} for r in records]

    assert untimed(again) == untimed(smoke.records)


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------
def test_choose_batches_is_seeded_and_never_reuses_a_clean_batch():
    clean, corrupted = evaluate.choose_batches(40, BUDGET, "ad")
    assert len(clean) == BUDGET.n_clean and all(len(ids) == BUDGET.n_corrupted for ids in corrupted.values())
    assert not set(clean) & (set(corrupted["a"]) | set(corrupted["d"]))
    assert evaluate.choose_batches(40, BUDGET, "d") == (clean, {"d": corrupted["d"]})  # independent of other problems


def test_every_seed_key_gives_its_own_stream():
    # numpy pads a short seed with zeros: rng(p) IS rng(p, 0), the injection stream of batch 0
    def stream(*keys):
        return tuple(evaluate.rng(*keys).integers(0, 2**62, 4).tolist())

    problems, batches = range(len(evaluate.PROBLEM_IDS)), range(250)
    injection = {stream(p, b) for p in problems for b in batches}
    llm_sample = {stream(p, b, evaluate.LLM_SAMPLE_KEY) for p in problems for b in batches}
    choices = {stream(evaluate.BATCH_CHOICE_KEY, p) for p in problems}
    others = {stream(evaluate.CLEAN_SET_KEY), stream(evaluate.BACKGROUND_KEY)}
    assert len(injection) == len(llm_sample) == len(problems) * len(batches) and len(choices) == len(problems)
    assert not injection & llm_sample and not (choices | others) & (injection | llm_sample) and not choices & others


def test_spread():
    assert evaluate.spread(10, 3) == [4, 3, 3]
    assert evaluate.spread(2, 3) == [1, 1, 0]
    assert evaluate.spread(5, 0) == []


def test_stratified_rows_balances_injected_and_other_rows():
    labels = np.zeros(100, dtype=bool)
    labels[[5, 17, 60]] = True
    rows = evaluate.stratified_rows(labels, 4, np.random.default_rng(0))
    assert labels[rows].sum() == 3 and (~labels[rows]).sum() == 4  # only 3 injected rows exist
    assert np.all(np.diff(rows) > 0)
    assert len(evaluate.stratified_rows(np.zeros(100, dtype=bool), 4, np.random.default_rng(0))) == 4


def _runs(positions: np.ndarray) -> list[np.ndarray]:
    return np.split(positions, np.flatnonzero(np.diff(positions) != 1) + 1)


def test_windows_end_just_after_a_copy_and_never_touch():
    labels = np.zeros(300, dtype=bool)
    labels[[100, 104, 200]] = True
    rows = evaluate.window_rows(labels, 3, 40, np.random.default_rng(1))
    runs = _runs(rows)
    assert len(runs) == 2 and all(len(run) == 40 for run in runs)  # copies 100 and 104 share a window
    assert labels[rows].sum() == 3
    for run in runs:
        last_copy = run[np.flatnonzero(labels[run])[-1]]
        assert run[-1] - last_copy <= evaluate.WINDOW_ROWS_AFTER_TARGET


def test_windows_show_a_copy_together_with_its_original_when_one_can():
    # exact copies land anywhere later: copy 200's original is 190 rows back, beyond any window
    labels = np.zeros(400, dtype=bool)
    labels[[200, 300]] = True
    copy_of = {200: 10, 300: 280}
    for seed in range(20):
        rows = evaluate.window_rows(labels, 1, 40, np.random.default_rng(seed), copy_of)
        assert {280, 300} <= set(rows.tolist())  # the one window shows a pair the model can judge
    assert len(_runs(evaluate.window_rows(labels, 2, 40, np.random.default_rng(0), copy_of))) == 2  # the other copy still gets one


class LlmSeesOnePair(Detector):
    """Window review that flags row 120, the copy whose original (105) is in its window."""

    problem, family, name = "e", "llm", "llm_sees_one_pair"

    def detect(self, batch, review_rows=None):
        flags = np.zeros(len(batch), dtype=bool)
        flags[120] = True
        return self.result(True, row_flags=flags, reviewed=np.isin(np.arange(len(batch)), review_rows))


def test_window_copies_whose_original_is_outside_the_window_are_not_scored(batch_feed):
    labels = np.zeros(len(batch_feed), dtype=bool)
    labels[[120, 130]] = True  # copy 130 repeats row 20, far outside the window 100-139
    truth = FakeInjected(batch_feed, labels, True, "e", {"copy_of": {120: 105, 130: 20}})
    review = np.arange(100, 140)
    assert np.flatnonzero(evaluate.unjudgeable_copies("e", review, truth, len(batch_feed))).tolist() == [130]
    assert not evaluate.unjudgeable_copies("d", review, truth, len(batch_feed)).any()  # only window problems
    det = LlmSeesOnePair()
    record = evaluate.make_record(det, evaluate.run_detector(det, batch_feed, review), "corrupted", 0, batch_feed, truth, review)
    assert (record["n_reviewed"], record["injected_reviewed"], record["tp"], record["fn"]) == (40, 2, 1, 0)


def test_window_reach_counts_copies_one_window_can_show_with_their_original():
    records = [{"params": {"copy_of": {"50": 45, "900": 10}}}, {"params": {"copy_of": {"39": 0, "40": 0}}}, {"params": {}}]
    reach = evaluate.window_reach(records)  # gaps 5, 890, 39, 40: only < 40 rows fits in one 40-row window
    assert (reach["k"], reach["n"]) == (2, 4)


def test_random_windows_in_a_clean_batch_and_tiny_batches():
    rows = evaluate.window_rows(np.zeros(500, dtype=bool), 3, 40, np.random.default_rng(2))
    assert [len(run) for run in _runs(rows)] == [40, 40, 40]
    assert evaluate.window_rows(np.ones(10, dtype=bool), 2, 40, np.random.default_rng(3)).tolist() == list(range(10))


def test_llm_review_rows_by_problem_level():
    labels = np.zeros(300, dtype=bool)
    labels[150] = True
    rng = np.random.default_rng(0)
    assert evaluate.llm_review_rows("a", labels, 5, 1, rng) is None  # whole-batch verdict
    assert len(evaluate.llm_review_rows("d", labels, 5, 1, rng)) == 6  # 1 injected + 5 others
    assert len(evaluate.llm_review_rows("f", labels, 5, 1, rng)) == config.LLM_BATCH_SAMPLE_ROWS


class WrongLength(Detector):
    problem, family, name = "d", "rule", "rule_wrong_length"

    def detect(self, batch, review_rows=None):
        return self.result(True, row_flags=np.ones(len(batch) - 1, dtype=bool))


def test_malformed_row_flags_become_an_error_not_a_crash(batch_feed):
    result = evaluate.run_detector(WrongLength(), batch_feed)
    assert result.row_flags is None and result.error.startswith("malformed row_flags")
    truth = fake_as_clean(batch_feed)
    record = evaluate.make_record(WrongLength(), result, "clean", 0, truth.df, truth)
    assert (record["fp"], record["tn"]) == (0, len(batch_feed))


class ReturnsNothing(Detector):
    problem, family, name = "d", "rule", "rule_returns_nothing"

    def detect(self, batch, review_rows=None):
        return None  # e.g. a branch that forgot its return


class NullableFlags(Detector):
    """Flags as a nullable boolean array: NA where latitude is missing."""

    problem, family, name = "d", "rule", "rule_nullable_flags"

    def detect(self, batch, review_rows=None):
        return self.result(True, row_flags=(batch["latitude"].astype("Float64") > 40.7).array)


class NanFlags(Detector):
    """Float flags with NaN where latitude is missing: cast to bool, NaN would read as True."""

    problem, family, name = "d", "rule", "rule_nan_flags"

    def detect(self, batch, review_rows=None):
        return self.result(True, row_flags=np.where(batch["latitude"].isna(), np.nan, 0.0))


@pytest.mark.parametrize("det", [ReturnsNothing(), NullableFlags(), NanFlags()], ids=lambda det: det.name)
def test_unusable_detector_output_is_an_error_not_a_crash_or_a_guess(det, batch_feed):
    assert batch_feed["latitude"].isna().any()  # the NA / NaN cases need missing coordinates
    result = evaluate.run_detector(det, batch_feed)
    assert result.error and result.row_flags is None
    truth = fake_as_clean(batch_feed)
    record = evaluate.make_record(det, result, "clean", 0, truth.df, truth)
    assert (record["fp"], record["tn"], record["n_flagged"]) == (0, len(batch_feed), None)  # scored as "flagged nothing"


def test_row_mask_takes_booleans_in_any_container_and_nothing_else():
    flags = [True, False, True]
    for values in (np.array(flags), flags, pd.Series(flags), pd.array(flags, dtype="boolean"), np.array(flags, dtype=object), np.array([1, 0, 1])):
        assert evaluate.row_mask(values, 3).tolist() == flags
    for values in (np.array([1.0, np.nan, 1.0]), pd.array([True, None, True], dtype="boolean"), np.array([0.9, 0.1, 0.7]),
                   [True, False], [[True], [False], [True]], ["yes", "no", "yes"]):
        assert evaluate.row_mask(values, 3) is None


class LlmNullSpikeVerdict(Detector):
    """A batch verdict like the real null-spike reviewer's: the model names an agency and a column,
    and the detector itself flags that agency's rows - the model quoted no row."""

    problem, family, name = "g", "llm", "llm_null_spike_verdict"

    def detect(self, batch, review_rows=None):
        agency = batch["agency"].iat[0]
        alert = self.alert(f"{agency} lost ZIPs", check_id="L-g1", columns=["incident_zip"], segment={"agency": agency},
                           observed="9 of 12 sample rows", expected="filled", deterministic=False)
        return self.result(True, alerts=[alert], row_flags=batch["agency"].eq(agency).to_numpy())


class LlmDateVerdict(Detector):
    """A batch verdict that quotes the two rows it names (like the real date-format reviewer)."""

    problem, family, name = "c", "llm", "llm_date_verdict"

    def detect(self, batch, review_rows=None):
        flags = np.zeros(len(batch), dtype=bool)
        flags[[10, 20]] = True
        evidence = {10: {"observed": batch["created_date"].iat[10]}, 20: {"observed": "13/45/2023 25:00"}}  # row 20: invented
        return self.result(True, row_flags=flags, row_evidence=evidence)


def test_grounding_checks_only_the_rows_a_model_quoted(batch_feed):
    truth = fake_as_clean(batch_feed)
    spike = LlmNullSpikeVerdict()
    record = evaluate.make_record(spike, evaluate.run_detector(spike, batch_feed), "clean", 0, batch_feed, truth)
    assert record["n_flagged"] > 0 and record["grounding"] == {"grounded": 0, "flagged": 0}  # nothing quoted: not "0 % grounded"
    fit = {"problem": "g", "family": "llm", "description": "", "fit_s": 0.0, "fit_error": None}
    assert evaluate.detector_summary(spike.name, fit, [record], None)["grounding"]["value"] is None
    dates = LlmDateVerdict()
    record = evaluate.make_record(dates, evaluate.run_detector(dates, batch_feed), "clean", 0, batch_feed, truth)
    assert record["grounding"] == {"grounded": 1, "flagged": 2}


class LlmFailsSilently(Detector):
    """No parseable verdict, reported as "nothing reviewed" (what several real LLM detectors do)."""

    problem, family, name = "d", "llm", "llm_fails_silently"

    def detect(self, batch, review_rows=None):
        nothing = np.zeros(len(batch), dtype=bool)
        return self.result(False, row_flags=nothing, reviewed=nothing, error="llm_no_verdicts: unparseable reply")


class RuleNarrowsItself(Detector):
    """A rule that claims to have read only its first 10 rows."""

    problem, family, name = "d", "rule", "rule_narrows_itself"

    def detect(self, batch, review_rows=None):
        reviewed = np.zeros(len(batch), dtype=bool)
        reviewed[:10] = True
        return self.result(False, row_flags=np.zeros(len(batch), dtype=bool), reviewed=reviewed)


def test_failures_and_self_narrowing_cannot_shrink_the_denominator(batch_feed):
    labels = np.zeros(len(batch_feed), dtype=bool)
    labels[[3, 40, 4000]] = True
    truth = FakeInjected(batch_feed, labels, True, "d", {})
    review = np.array([3, 10, 40, 77])
    failed = LlmFailsSilently()
    record = evaluate.make_record(failed, evaluate.run_detector(failed, batch_feed, review), "corrupted", 0, batch_feed, truth, review)
    assert (record["n_reviewed"], record["fn"], record["tn"]) == (4, 2, 2)  # misses on the rows it was asked about
    rule = RuleNarrowsItself()
    record = evaluate.make_record(rule, evaluate.run_detector(rule, batch_feed), "corrupted", 0, batch_feed, truth)
    assert (record["n_reviewed"], record["fn"]) == (len(batch_feed), 3)  # rule / stat are scored on every row


class Flaky(Detector):
    """Flags the rows it is told to, or fails the way real detectors do: an error result that flags
    nothing (e.g. an LLM reply it could not parse) or, for the error "crash", an exception."""

    def __init__(self, problem: str, family: str, rows=(), error: str | None = None):
        self.problem, self.family, self.name = problem, family, f"{family}_flaky"
        self.rows, self.error = list(rows), error

    def detect(self, batch, review_rows=None):
        if self.error == "crash":
            raise RuntimeError("boom")
        flags = np.isin(np.arange(len(batch)), self.rows)
        return self.result(bool(flags.any()), row_flags=flags, error=self.error)


def flaky_summary(problem: str, family: str, verdicts: list[tuple], batch: pd.DataFrame, review=None) -> dict:
    """detector_summary of Flaky over one batch per (kind, verdict): a verdict is the rows it flags or
    the error it fails with. Corrupted batches have rows 3 and 40 injected."""
    labels = np.isin(np.arange(len(batch)), [3, 40])
    records = []
    for batch_id, (kind, verdict) in enumerate(verdicts):
        truth = FakeInjected(batch, labels, True, problem, {}) if kind == "corrupted" else fake_as_clean(batch)
        det = Flaky(problem, family, error=verdict) if isinstance(verdict, str) else Flaky(problem, family, rows=verdict)
        records.append(evaluate.make_record(det, evaluate.run_detector(det, batch, review), kind, batch_id, batch, truth, review))
    fit = {"problem": problem, "family": family, "description": "", "fit_s": 0.0, "fit_error": None}
    return evaluate.detector_summary(f"{family}_flaky", fit, records, None)


ANSWERED = [("corrupted", [3, 40]), ("clean", [7]), ("clean", [])]  # both injected rows caught; one false alarm
CLEAN_ERRORS = [("clean", "llm_no_verdicts: the model returned no verdict"), ("clean", "crash")]


@pytest.mark.parametrize("family, review", [("stat", None), ("llm", np.arange(50))], ids=["stat", "llm"])
def test_clean_batches_a_detector_errored_on_are_left_out_of_its_false_alarm_rates(family, review, batch_feed):
    rows = len(batch_feed) if review is None else len(review)  # rows judged per batch
    entry = flaky_summary("d", family, [*ANSWERED, *CLEAN_ERRORS, ("corrupted", "crash")], batch_feed, review)
    assert (entry["row_fpr"]["k"], entry["row_fpr"]["n"]) == (1, 2 * rows)  # the 2 clean batches it judged, not all 4
    if family != "llm":  # an LLM reading a sample of each batch reports no batch FAR
        assert (entry["batch_far"]["k"], entry["batch_far"]["n"]) == (1, 2)  # not 1 of 4
    assert (entry["detection"]["k"], entry["detection"]["n"]) == (2, 4)  # an error on a corrupted batch stays a miss
    assert entry["n_batches"] == {"corrupted": 2, "clean": 4}
    assert (entry["n_corrupted_errors"], entry["n_clean_errors"]) == (1, 2)
    without = flaky_summary("d", family, ANSWERED, batch_feed, review)  # the same verdicts, no errors
    assert (without["n_corrupted_errors"], without["n_clean_errors"]) == (0, 0)
    assert all(without[key] == entry[key] for key in ("row_fpr", "batch_far") if key in without)


@pytest.mark.parametrize("problem, family, review", [("d", "stat", None), ("d", "llm", np.arange(50)), ("a", "llm", None)],
                         ids=["row-stat", "row-llm", "batch-llm"])
def test_a_detector_that_errored_on_every_clean_batch_has_no_false_alarm_rate(problem, family, review, batch_feed):
    entry = flaky_summary(problem, family, [("corrupted", [3, 40]), *CLEAN_ERRORS], batch_feed, review)
    rates = [entry[key] for key in ("batch_far", "row_fpr") if key in entry]
    assert rates and all(rate["value"] is None and rate["n"] == 0 for rate in rates)  # not measured: never "0 %"
    assert (entry["n_clean_errors"], entry["n_batches"]["clean"]) == (2, 2)


class AlertsAnyway(Flaky):
    """Raises an alert but returns a NaN row flag (a detector bug): run_detector records an error,
    yet the alert went out like any other."""

    def detect(self, batch, review_rows=None):
        flags = np.zeros(len(batch))
        flags[0] = np.nan
        return self.result(True, alerts=[self.alert("row 0 looks wrong", columns=["latitude"], rows=[0])], row_flags=flags)


def test_an_alert_raised_in_spite_of_an_error_still_counts_as_a_false_alarm(batch_feed):
    clean = fake_as_clean(batch_feed)
    runs = [AlertsAnyway("d", "rule"), Flaky("d", "rule"), Flaky("d", "rule", error="crash")]
    records = [evaluate.make_record(det, evaluate.run_detector(det, batch_feed), "clean", b, batch_feed, clean) for b, det in enumerate(runs)]
    assert records[0]["error"].startswith("malformed row_flags") and records[0]["batch_flag"]
    fit = {"problem": "d", "family": "rule", "description": "", "fit_s": 0.0, "fit_error": None}
    entry = evaluate.detector_summary("rule_flaky", fit, records, None)
    assert (entry["batch_far"]["k"], entry["batch_far"]["n"]) == (1, 2)  # the alert counts; the crash (no verdict) does not
    assert (entry["row_fpr"]["k"], entry["row_fpr"]["n"]) == (0, len(batch_feed))  # row verdicts: only the run that gave them
    assert entry["n_clean_errors"] == 2
    # same inputs as the LLM, batch level: the rule's alert on the clean batch the LLM judged is a false alarm too
    same = [{"problem": "a", "family": family, "detector": f"{family}_x", "kind": "clean", "batch_id": 1, "batch_label": False,
             "batch_flag": family == "rule", "error": "malformed row_flags" if family == "rule" else None} for family in ("rule", "llm")]
    rows = {row["detector"]: row for row in evaluate.same_rows_summary("a", same)}
    assert (rows["rule_x"]["batch_far"]["k"], rows["rule_x"]["batch_far"]["n"]) == (1, 1)


def test_recorder_writes_values_json_cannot_hold_as_text(tmp_path):
    recorder = evaluate.Recorder(tmp_path / "records.jsonl")
    recorder.add({"observed": datetime.timedelta(days=-3), "n": np.int64(2)})  # e.g. a closed-created gap in an alert
    on_disk = json.loads((tmp_path / "records.jsonl").read_text(encoding="utf-8"))
    assert on_disk == recorder.records[0] == {"observed": "-3 days, 0:00:00", "n": 2}


def test_same_rows_leaves_out_corrupted_batches_where_nothing_was_injected():
    def record(kind, batch_id, injected, tp, fp, fn, tn):
        counts = {"tp": tp, "fp": fp, "fn": fn, "tn": tn}
        return {"problem": "d", "family": "llm", "detector": "llm_x", "kind": kind, "batch_id": batch_id, "batch_label": injected,
                "error": None, **counts, "same_rows": {"rule_x": counts}}

    records = [record("corrupted", 0, True, 1, 0, 1, 2), record("corrupted", 1, False, 0, 3, 0, 1), record("clean", 2, False, 0, 0, 0, 4)]
    rows = {row["detector"]: row for row in evaluate.same_rows_summary("d", records)}
    assert rows["llm_x"]["precision_at_rate"] == rows["rule_x"]["precision_at_rate"] == 1.0  # the 3 fp are not counted


@pytest.mark.parametrize("problem, rate, judged", [("d", "row_fpr", 4), ("a", "batch_far", 1)], ids=["row-level", "batch-level"])
def test_same_rows_leaves_out_the_clean_batches_each_detector_errored_on(problem, rate, judged):
    # Two clean batches. rule_x crashed on batch 2, llm_x got an unparseable reply on batch 1; each raised one
    # false alarm on the batch it did judge (row-level: on the LLM's 4 rows; batch-level: on the batch).
    nothing, one_alarm = {"tp": 0, "fp": 0, "fn": 0, "tn": 4}, {"tp": 0, "fp": 1, "fn": 0, "tn": 3}

    def record(family, batch_id, counts, error=None, same_rows=None):
        return {"problem": problem, "family": family, "detector": f"{family}_x", "kind": "clean", "batch_id": batch_id,
                "batch_label": False, "batch_flag": counts["fp"] > 0, "error": error, **counts, "same_rows": same_rows}

    records = [record("rule", 1, one_alarm), record("rule", 2, nothing, error="RuntimeError: boom"),
               record("llm", 1, nothing, error="llm_bad_reply: no JSON verdict", same_rows={"rule_x": one_alarm}),
               record("llm", 2, one_alarm, same_rows={"rule_x": nothing})]
    rows = {row["detector"]: row for row in evaluate.same_rows_summary(problem, records)}
    for name in ("rule_x", "llm_x"):
        assert (rows[name][rate]["k"], rows[name][rate]["n"]) == (1, judged)  # the batch it judged only, not 1 of 2 x judged


def test_background_record_has_no_ground_truth(batch_feed):
    det = RuleLatitude()
    record = evaluate.make_record(det, evaluate.run_detector(det, batch_feed), "raw_background", 7, batch_feed)
    assert record["tp"] is None and record["params"] is None and record["batch_label"] is None
    assert record["n_flagged"] == 0 and record["n_reviewed"] == len(batch_feed)


def test_read_period_splits_on_reference_end_and_keeps_order(tmp_path, reference_feed, batch_feed):
    path = tmp_path / "pool.parquet"
    pd.concat([reference_feed, batch_feed], ignore_index=True).to_parquet(path, index=False)
    assert len(evaluate.read_period(path, reference=True)) == len(reference_feed)
    evaluation = evaluate.read_period(path, reference=False)
    assert evaluation["unique_key"].tolist() == batch_feed["unique_key"].tolist()


def test_derived_data_is_rebuilt_only_when_raw_data_or_code_change(tmp_path, monkeypatch):
    """The key holds the raw file's own (verified) SHA-256 and a hash of the deriving code."""
    from project_b.data import snapshot

    code = tmp_path / "clean.py"
    code.write_text("x = 1")
    raw_sha = {"value": "aaa"}  # the raw snapshot's checksum, as snapshot.sha256 would compute it
    monkeypatch.setattr(snapshot, "verify", lambda path=None, manifest=None: {"verified": True})
    monkeypatch.setattr(snapshot, "sha256", lambda path: raw_sha["value"])
    monkeypatch.setattr(evaluate, "DERIVING_CODE", [code])
    builds = []

    def fake_build(folder):
        builds.append(folder)
        for name in evaluate.DERIVED_FILES:
            (folder / name).write_text("x")

    monkeypatch.setattr(evaluate, "build_derived", fake_build)
    derived = tmp_path / "derived"
    derived.mkdir()

    evaluate.ensure_derived(derived)
    evaluate.ensure_derived(derived)
    assert len(builds) == 1  # the second call used the cache
    raw_sha["value"] = "bbb"  # another raw file (only possible when run with PROJECT_B_ALLOW_NEW_SNAPSHOT=1)
    evaluate.ensure_derived(derived)
    code.write_text("x = 2")
    evaluate.ensure_derived(derived)
    assert len(builds) == 3
    (derived / evaluate.POOL_FILE).unlink()  # a derived file went missing: rebuild rather than crash later
    evaluate.ensure_derived(derived)
    assert len(builds) == 4

    def interrupted_build(folder):  # dies half way, after rewriting some files
        (folder / evaluate.CONTEXT_FILE).write_text("half a pickle")
        raise KeyboardInterrupt

    (derived / evaluate.POOL_FILE).unlink()
    monkeypatch.setattr(evaluate, "build_derived", interrupted_build)
    with pytest.raises(KeyboardInterrupt):
        evaluate.ensure_derived(derived)
    (derived / evaluate.POOL_FILE).write_text("x")  # every file is back, but the build never finished
    monkeypatch.setattr(evaluate, "build_derived", fake_build)
    evaluate.ensure_derived(derived)
    assert len(builds) == 5


def test_derived_data_is_rebuilt_when_the_build_step_changes(tmp_path, monkeypatch):
    # build_derived lives in this module, outside DERIVING_CODE: editing it must still rebuild.
    # The raw snapshot is simulated (a fresh clone has no data/), as in the test above.
    from project_b.data import snapshot

    code = tmp_path / "clean.py"
    code.write_text("x = 1")
    monkeypatch.setattr(snapshot, "verify", lambda path=None, manifest=None: {"verified": True})
    monkeypatch.setattr(snapshot, "sha256", lambda path: "aaa")
    monkeypatch.setattr(evaluate, "DERIVING_CODE", [code])
    builds = []

    def build_v1(folder):
        builds.append(1)
        for name in evaluate.DERIVED_FILES:
            (folder / name).write_text("v1")

    def build_v2(folder):
        builds.append(2)
        for name in evaluate.DERIVED_FILES:
            (folder / name).write_text("v2")

    derived = tmp_path / "derived"
    derived.mkdir()
    monkeypatch.setattr(evaluate, "build_derived", build_v1)
    evaluate.ensure_derived(derived)
    evaluate.ensure_derived(derived)
    monkeypatch.setattr(evaluate, "build_derived", build_v2)
    evaluate.ensure_derived(derived)
    assert builds == [1, 2]


def test_derived_data_depends_on_config():
    # the clean pool and the injection context read config (REFERENCE_END, NYC box, status groups)
    assert config.PKG_DIR / "config.py" in evaluate.DERIVING_CODE


def test_cli_arguments():
    args = evaluate.parse_args(["--problems", "ad", "--families", "rule,llm", "--quick", "--skip-sweep"])
    assert (args.problems, args.families, args.quick, args.skip_sweep, args.skip_background) == ("ad", ("rule", "llm"), True, True, False)
    for bad in (["--problems", "xyz"], ["--families", "rule,magic"]):
        with pytest.raises(SystemExit):
            evaluate.parse_args(bad)


def test_only_the_full_experiment_writes_to_the_committed_results():
    assert evaluate.parse_args([]).out_dir == config.RESULTS_DIR
    for reduced in (["--quick"], ["--problems", "ad"], ["--families", "rule,stat"], ["--skip-sweep"]):
        assert evaluate.parse_args(reduced).out_dir == config.RESULTS_DIR / "partial"
    assert evaluate.parse_args(["--quick", "--out-dir", "elsewhere"]).out_dir == evaluate.Path("elsewhere")


# --------------------------------------------------------------------------
# Abstentions: rows (or batches) the LLM gave no readable verdict for
# --------------------------------------------------------------------------
class LlmAbstains(Detector):
    """A per-row LLM detector following the project convention: it flags `rows`, gives a verdict for
    every asked row except `silent` (abstained, with `cause`), and marks only answered rows reviewed."""

    problem, family, name = "d", "llm", "llm_abstains"

    def __init__(self, rows=(), silent=(), cause="unreadable_reply:MAX_TOKENS"):
        self.rows, self.silent, self.cause = set(rows), set(silent), cause

    def detect(self, batch, review_rows=None):
        asked = [int(p) for p in review_rows]
        answered = [p for p in asked if p not in self.silent]
        reviewed = np.isin(np.arange(len(batch)), answered)
        flags = np.isin(np.arange(len(batch)), [p for p in answered if p in self.rows])
        result = self.result(bool(flags.any()), row_flags=flags, reviewed=reviewed,
                             abstained={p: self.cause for p in asked if p in self.silent})
        result.usage.add(Usage(llm_calls=1, input_tokens=1000, output_tokens=100, llm_latency_s=1.0))
        return result


def abstain_record(batch, kind, rows=(), silent=(), batch_id=0, repeat=None):
    labels = np.isin(np.arange(len(batch)), [3, 40])  # injected rows of a corrupted batch
    truth = FakeInjected(batch, labels, True, "d", {}) if kind == "corrupted" else fake_as_clean(batch)
    det = LlmAbstains(rows, silent)
    review = np.array([3, 10, 40, 77])
    extra = {"repeat": repeat} if repeat is not None else {}
    return evaluate.make_record(det, evaluate.run_detector(det, batch, review), kind, batch_id, batch, truth, review, **extra)


def test_abstained_rows_are_neither_reviewed_nor_clean(batch_feed):
    rec = abstain_record(batch_feed, "corrupted", rows=[3], silent=[40, 77])
    assert (rec["n_asked"], rec["n_reviewed"], rec["n_abstained"]) == (4, 2, 2)
    # conditional counts cover only rows with a verdict: row 40 (injected) abstained, so it is no miss here
    assert (rec["tp"], rec["fn"], rec["fp"], rec["tn"]) == (1, 0, 0, 1)
    assert (rec["abstained_injected"], rec["abstained_non_injected"]) == (1, 1)
    assert rec["abstain_causes"] == {"unreadable_reply:MAX_TOKENS": 2} and rec["abstained_positions"] == [40, 77]


def test_an_llm_error_is_still_a_miss_not_an_abstention(batch_feed):
    labels = np.isin(np.arange(len(batch_feed)), [3, 40])
    truth = FakeInjected(batch_feed, labels, True, "d", {})
    failed = LlmFailsSilently()
    review = np.array([3, 10, 40, 77])
    rec = evaluate.make_record(failed, evaluate.run_detector(failed, batch_feed, review), "corrupted", 0, batch_feed, truth, review)
    assert rec["n_abstained"] == 0 and (rec["n_reviewed"], rec["fn"]) == (4, 2)


def test_summary_reports_coverage_and_both_views_of_the_rates(batch_feed):
    records = [
        abstain_record(batch_feed, "corrupted", rows=[3], silent=[40], batch_id=0),  # catches 3, abstains on injected 40
        abstain_record(batch_feed, "corrupted", rows=[3, 40], batch_id=1),  # catches both
        abstain_record(batch_feed, "clean", rows=[10], silent=[77], batch_id=2),  # one false alarm, one abstention
        abstain_record(batch_feed, "clean", silent=[3, 10, 40, 77], batch_id=3),  # every row abstained: no verdict at all
    ]
    fit = {"problem": "d", "family": "llm", "description": "", "fit_s": 0.0, "fit_error": None}
    e = evaluate.detector_summary("llm_abstains", fit, records, "gemini-3.5-flash-lite")
    a = e["abstention"]
    assert (a["asked"], a["with_verdict"], a["unit"]) == (16, 10, "rows")
    assert a["coverage"]["value"] == pytest.approx(10 / 16) and a["abstention_rate"]["value"] == pytest.approx(6 / 16)
    assert a["causes"] == {"unreadable_reply:MAX_TOKENS": 6} and (a["abstained_injected"], a["abstained_non_injected"]) == (1, 5)
    # given a readable verdict: 3 of 3 injected rows with a verdict caught; 1 false alarm in 3 clean rows with one
    assert (e["detection"]["k"], e["detection"]["n"]) == (3, 3) and (e["row_fpr"]["k"], e["row_fpr"]["n"]) == (1, 3)
    # overall (abstention = no alert): 3 of 4 injected rows caught; 1 false alarm in all 8 clean-batch rows asked
    op = e["operational"]
    assert (op["detection"]["k"], op["detection"]["n"]) == (3, 4) and (op["row_fpr"]["k"], op["row_fpr"]["n"]) == (1, 8)
    # an all-abstained batch is no error: it is in the rates' batch counts and its tokens are paid for
    assert e["status"] == "partial" and "6 of 16 rows asked" in e["note"] and e["n_clean_errors"] == 0
    per_verdict, per_asked = e["cost_per_million_rows"]["usd"], op["cost_per_million_rows"]["usd"]
    assert per_verdict == pytest.approx(per_asked * 16 / 10)  # same tokens, over 10 verdicts vs 16 rows asked


def test_determinism_compares_only_rows_every_repeat_answered(batch_feed):
    repeats = [abstain_record(batch_feed, "corrupted", rows=[3, 77], batch_id=0, repeat=0),
               abstain_record(batch_feed, "corrupted", rows=[3], silent=[77], batch_id=0, repeat=1)]
    for r in repeats:
        r["kind"] = "determinism"
    agreement = evaluate.determinism("row", repeats)
    assert (agreement["k"], agreement["n"]) == (3, 3)  # row 77 has no verdict in repeat 1: not a disagreement


def test_coverage_table_shows_both_views(batch_feed):
    records = [abstain_record(batch_feed, "corrupted", rows=[3], silent=[40]), abstain_record(batch_feed, "clean", batch_id=1)]
    fit = {"problem": "d", "family": "llm", "description": "", "fit_s": 0.0, "fit_error": None}
    e = evaluate.detector_summary("llm_abstains", fit, records, None)
    text = "\n".join(evaluate.coverage_table([e]) + evaluate.abstention_lines(e))
    assert "LLM coverage and abstention" in text and "unreadable_reply:MAX_TOKENS 1" in text
    assert "50% / 100%" in text  # detection: overall / given a verdict


class LlmBatchVerdict(Detector):
    problem, family, name = "a", "llm", "llm_batch_verdict"

    def __init__(self, flag=False, error=None):
        self.flag, self.error = flag, error

    def detect(self, batch, review_rows=None):
        return self.result(self.flag, error=self.error)


def test_an_unreadable_batch_verdict_is_an_abstention(batch_feed):
    truth = FakeInjected(batch_feed, np.zeros(len(batch_feed), dtype=bool), True, "a", {"columns": ["incident_zip"]})
    runs = [("corrupted", LlmBatchVerdict(flag=True)), ("corrupted", LlmBatchVerdict(error="llm_bad_reply: no JSON verdict")),
            ("clean", LlmBatchVerdict()), ("clean", LlmBatchVerdict(error="llm_bad_reply: no JSON verdict"))]
    records = [evaluate.make_record(det, evaluate.run_detector(det, batch_feed), kind, i, batch_feed,
                                    truth if kind == "corrupted" else fake_as_clean(batch_feed)) for i, (kind, det) in enumerate(runs)]
    fit = {"problem": "a", "family": "llm", "description": "", "fit_s": 0.0, "fit_error": None}
    e = evaluate.detector_summary("llm_batch_verdict", fit, records, None)
    assert (e["abstention"]["asked"], e["abstention"]["with_verdict"], e["abstention"]["unit"]) == (4, 2, "batches")
    assert e["abstention"]["causes"] == {"llm_bad_reply": 2}
    assert (e["batch_detection"]["k"], e["batch_detection"]["n"]) == (1, 1)  # given a verdict
    assert (e["operational"]["batch_detection"]["k"], e["operational"]["batch_detection"]["n"]) == (1, 2)  # abstention = miss
    assert (e["batch_far"]["n"], e["operational"]["batch_far"]["n"]) == (1, 2)


# --------------------------------------------------------------------------
# --reuse-base-records: re-score the LLM, keep the rule / stat records exactly
# --------------------------------------------------------------------------
def test_reuse_keeps_the_earlier_rule_and_stat_records_with_their_timings(smoke, tmp_path):
    earlier = tmp_path / "earlier.jsonl"
    marked = [dict(r, wall_s=123.0) if r["family"] != "llm" else r for r in smoke.records]  # earlier timings, recognisable
    earlier.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in marked), encoding="utf-8")
    summary = evaluate.run(smoke.derived, fake_detectors(), BUDGET, tmp_path / "out", injector=FAKE_INJECTOR, reuse_base_records=earlier)
    records = [json.loads(line) for line in (tmp_path / "out" / evaluate.RECORDS_FILE).read_text(encoding="utf-8").splitlines()]
    assert [r for r in records if r["family"] != "llm"] == [r for r in marked if r["family"] != "llm"]
    assert all(r["wall_s"] != 123.0 for r in records if r["family"] == "llm")  # LLM records are this run's own
    rule = next(e for e in summary["detectors"] if e["detector"] == "rule_fake_latitude")
    assert rule["rows_per_s"] == pytest.approx(BUDGET.batch_size / 123.0)  # speed from the kept timings


def test_reuse_also_keeps_the_earlier_fit_times(smoke, tmp_path):
    earlier_dir = tmp_path / "earlier"
    earlier_dir.mkdir()
    (earlier_dir / evaluate.RECORDS_FILE).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in smoke.records), encoding="utf-8")
    old = json.loads(json.dumps(smoke.summary))
    for e in old["detectors"]:
        e["fit_s"] = 7.5
    (earlier_dir / evaluate.SUMMARY_FILE).write_text(json.dumps(old), encoding="utf-8")
    summary = evaluate.run(smoke.derived, fake_detectors(), BUDGET, tmp_path / "out", injector=FAKE_INJECTOR,
                           reuse_base_records=earlier_dir / evaluate.RECORDS_FILE)
    fit_s = {e["detector"]: e["fit_s"] for e in summary["detectors"]}
    assert fit_s["rule_fake_latitude"] == 7.5 and fit_s["llm_fake_coordinates"] != 7.5
    assert fit_s["stat_fake_fit_fails"] != 7.5  # a failed fit keeps its own (it never ran)


def test_reuse_stops_when_a_rule_or_stat_record_would_change(smoke, tmp_path):
    earlier = tmp_path / "earlier.jsonl"
    changed, done = [], False
    for r in smoke.records:
        if not done and r["family"] == "rule" and r["kind"] == "corrupted":
            r, done = dict(r, n_alerts=r["n_alerts"] + 1), True  # a rule verdict that would no longer match
        changed.append(r)
    earlier.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in changed), encoding="utf-8")
    with pytest.raises(evaluate.BaseRecordMismatch, match="differs from the earlier run"):
        evaluate.run(smoke.derived, fake_detectors(), BUDGET, tmp_path / "out", injector=FAKE_INJECTOR, reuse_base_records=earlier)
    extra = tmp_path / "extra.jsonl"
    extra.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in smoke.records + [smoke.records[0]]), encoding="utf-8")
    with pytest.raises(evaluate.BaseRecordMismatch, match="did not produce"):
        evaluate.run(smoke.derived, fake_detectors(), BUDGET, tmp_path / "out2", injector=FAKE_INJECTOR, reuse_base_records=extra)



def test_rescore_lets_named_detectors_change_and_keeps_every_other_record(smoke, tmp_path):
    earlier = tmp_path / "earlier.jsonl"
    changed, n_changed = [], 0
    for r in smoke.records:
        if r["detector"] == "rule_fake_latitude" and r["kind"] == "corrupted":
            r, n_changed = dict(r, n_alerts=r["n_alerts"] + 7), n_changed + 1  # as if its code had changed since
        elif r["family"] != "llm":
            r = dict(r, wall_s=99.0)
        changed.append(r)
    earlier.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in changed), encoding="utf-8")
    with pytest.raises(evaluate.BaseRecordMismatch):  # without naming it, the change stops the run
        evaluate.run(smoke.derived, fake_detectors(), BUDGET, tmp_path / "a", injector=FAKE_INJECTOR, reuse_base_records=earlier)
    evaluate.run(smoke.derived, fake_detectors(), BUDGET, tmp_path / "b", injector=FAKE_INJECTOR, reuse_base_records=earlier,
                 rescore=frozenset({"rule_fake_latitude"}))
    records = [json.loads(line) for line in (tmp_path / "b" / evaluate.RECORDS_FILE).read_text(encoding="utf-8").splitlines()]
    mine = [r for r in records if r["detector"] == "rule_fake_latitude"]
    assert n_changed and all(r["wall_s"] != 99.0 for r in mine)  # its own, freshly measured records
    assert all(r["wall_s"] == 99.0 for r in records if r["family"] != "llm" and r["detector"] != "rule_fake_latitude")



def test_derived_data_are_never_served_for_an_unverified_snapshot(tmp_path, monkeypatch):
    """Even a complete cache is not used when the raw snapshot fails verification."""
    from project_b.data import snapshot

    def refuse(path=None, manifest=None):
        raise snapshot.SnapshotError("missing")

    monkeypatch.setattr(snapshot, "verify", refuse)
    with pytest.raises(snapshot.SnapshotError):
        evaluate.cache_key()
