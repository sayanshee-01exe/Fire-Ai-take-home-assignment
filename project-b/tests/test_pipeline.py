"""Tests for the hybrid pipeline (project_b/pipeline.py): plan selection, LLM confirmation,
mixed-corruption batches and one small end-to-end evaluation. Never calls Gemini (FakeClient)."""
from __future__ import annotations

import json
import pickle

import numpy as np
import pandas as pd
import pytest

from project_b import evaluate, llm, pipeline
from project_b.core import Detector
from project_b.data.load import to_feed
from project_b import inject as injector
from tests.fakes import FakeClient, rows_in_prompt


# --------------------------------------------------------------------------
# Plan selection
# --------------------------------------------------------------------------
def entry(problem, family, detection, false_alarm, precision=None, status="ok", n_corrupted=20):
    level = "row" if problem not in "abcg" else "batch"
    e = {"problem": problem, "family": family, "detector": f"{family}_{problem}", "status": status,
         "detection": {"value": detection}, "precision_at_rate": precision, "n_batches": {"corrupted": n_corrupted, "clean": 20}}
    e["row_fpr" if level == "row" else "batch_far"] = {"value": false_alarm}
    return e


def summary_with(*entries, same_rows=None):
    return {"detectors": list(entries), "same_rows": same_rows or {}}


def test_without_a_benchmark_the_plan_is_the_provisional_default():
    plan = pipeline.plan_from_summary(None)
    assert plan == {**pipeline.DEFAULT_PLAN, **pipeline.PLAN_OVERRIDES}
    assert all(step.reason.startswith("provisional") for step in pipeline.DEFAULT_PLAN.values())


def test_rule_wins_a_tie_and_a_failing_rule_loses():
    tie = pipeline._choose_step("d", pipeline.DEFAULT_PLAN["d"], summary_with(entry("d", "rule", 0.99, 0.0), entry("d", "stat", 1.0, 0.0)))
    assert tie.detectors == ("rule_d",) and tie.reason.startswith("measured")
    noisy_rule = summary_with(entry("i", "rule", 1.0, 0.01), entry("i", "stat", 0.9, 0.0005))
    assert pipeline._choose_step("i", pipeline.DEFAULT_PLAN["i"], noisy_rule).detectors == ("stat_i",)  # 1 % FPR > 0.2 % limit


def test_too_little_evidence_keeps_the_provisional_step():
    """A --quick run measures 3 corrupted batches per problem: not enough to change the plan."""
    quick = summary_with(entry("i", "rule", 1.0, 0.0, n_corrupted=3), entry("i", "stat", 0.9, 0.0, n_corrupted=3))
    step = pipeline._choose_step("i", pipeline.DEFAULT_PLAN["i"], quick)
    assert step.detectors == pipeline.DEFAULT_PLAN["i"].detectors and step.reason.startswith("provisional")
    assert f">= {pipeline.MIN_EVIDENCE_BATCHES} corrupted batches" in step.reason
    llm_few = entry("j", "llm", 0.97, 0.0, n_corrupted=1)  # LLM measured on one batch: no confirmation decision either way
    step = pipeline._choose_step("j", pipeline.DEFAULT_PLAN["j"], summary_with(entry("j", "rule", 1.0, 0.001, precision=0.8), llm_few))
    assert step.confirm is None and "not measured on enough batches" in step.reason


def test_complementary_detectors_both_run_when_both_pass():
    s = summary_with(entry("f", "rule", 0.8, 0.0), entry("f", "stat", 0.95, 0.0005))
    assert pipeline._choose_step("f", pipeline.DEFAULT_PLAN["f"], s).detectors == ("rule_f", "stat_f")
    s = summary_with(entry("d", "rule", 0.8, 0.0), entry("d", "stat", 0.95, 0.0005))  # d is not complementary
    assert pipeline._choose_step("d", pipeline.DEFAULT_PLAN["d"], s).detectors == ("stat_d",)


def test_llm_confirms_only_when_it_measurably_cuts_false_alarms():
    cheap = entry("j", "rule", 1.0, 0.001, precision=0.80)
    llm_entry = entry("j", "llm", 0.97, 0.0)
    good = {"j": [{"detector": "rule_j", "detection": {"value": 1.0}, "row_fpr": {"value": 0.01}},
                  {"detector": "llm_j", "detection": {"value": 0.99}, "row_fpr": {"value": 0.0}}]}
    step = pipeline._choose_step("j", pipeline.DEFAULT_PLAN["j"], summary_with(cheap, llm_entry, same_rows=good))
    assert step.confirm == "llm_j" and step.confirm_flags_of == ("rule_j",)
    worse = {"j": [good["j"][0], {"detector": "llm_j", "detection": {"value": 0.80}, "row_fpr": {"value": 0.0}}]}  # misses too much
    assert pipeline._choose_step("j", pipeline.DEFAULT_PLAN["j"], summary_with(cheap, llm_entry, same_rows=worse)).confirm is None
    precise = entry("j", "rule", 1.0, 0.0, precision=0.99)  # nothing to fix
    assert pipeline._choose_step("j", pipeline.DEFAULT_PLAN["j"], summary_with(precise, llm_entry, same_rows=good)).confirm is None


# --------------------------------------------------------------------------
# LLM confirmation
# --------------------------------------------------------------------------
class FlagEveryOtherRow(Detector):
    problem, family, name = "j", "rule", "rule_fake_j"

    def detect(self, batch, review_rows=None):
        flags = np.zeros(len(batch), dtype=bool)
        flags[::2] = True
        return self.result(bool(flags.any()), row_flags=flags, row_reasons={int(p): "x" for p in np.flatnonzero(flags)},
                           alerts=[self.alert("rows", check_id="T")])


class FakeLlmJ(Detector):
    """Confirms a row only when its descriptor is 'BAD'; counts which rows it was asked about."""
    problem, family, name = "j", "llm", "llm_fake_j"
    asked: list = []

    def detect(self, batch, review_rows=None):
        rows = np.asarray(review_rows)
        FakeLlmJ.asked.append(rows.tolist())
        reviewed = np.isin(np.arange(len(batch)), rows)
        flags = reviewed & batch["descriptor"].eq("BAD").to_numpy()
        return self.result(bool(flags.any()), row_flags=flags, reviewed=reviewed)


def fake_pipeline(monkeypatch, llm_cls=FakeLlmJ, cap=100):
    monkeypatch.setattr(pipeline, "detector_classes", lambda: [FlagEveryOtherRow, llm_cls])
    step = pipeline.Step("j", ("rule_fake_j",), confirm=llm_cls.name, confirm_flags_of=("rule_fake_j",), reason="test")
    return pipeline.HybridPipeline({"j": step}, max_confirm_items=cap)


def tiny_batch(descriptors):
    n = len(descriptors)
    return pd.DataFrame({"agency": ["NYPD"] * n, "complaint_type": ["Noise"] * n, "descriptor": descriptors})


def test_llm_vetoes_unconfirmed_flags_and_asks_each_distinct_value_once(monkeypatch):
    FakeLlmJ.asked = []
    pipe = fake_pipeline(monkeypatch)
    batch = tiny_batch(["BAD", "x", "BAD", "x", "ok", "x", "ok", "x"])  # flagged rows 0, 2, 4, 6: values BAD, BAD, ok, ok
    out = pipe.run(batch).outcomes["j"]
    assert np.flatnonzero(out.row_flags).tolist() == [0, 2]  # 'ok' vetoed, 'BAD' confirmed and broadcast to row 2
    assert FakeLlmJ.asked == [[0, 4]] and out.llm_asked == 2 and out.vetoed_rows == 2
    again = pipe.run(batch).outcomes["j"]  # the same values in the next batch: answered from this run's verdicts
    assert FakeLlmJ.asked == [[0, 4]] and again.llm_reused == 2 and np.flatnonzero(again.row_flags).tolist() == [0, 2]


def test_values_the_llm_cannot_judge_stay_flagged(monkeypatch):
    class Unreachable(FakeLlmJ):
        name = "llm_fake_unreachable"

        def detect(self, batch, review_rows=None):
            return self.result(False, error="llm_unavailable: no key")

    out = fake_pipeline(monkeypatch, Unreachable).run(tiny_batch(["ok", "x", "ok", "x"])).outcomes["j"]
    assert np.flatnonzero(out.row_flags).tolist() == [0, 2] and out.llm_unanswered == 1 and out.errors
    FakeLlmJ.asked = []
    capped = fake_pipeline(monkeypatch, cap=1).run(tiny_batch(["ok", "x", "fine", "x"])).outcomes["j"]  # budget: one value
    assert capped.llm_asked == 1 and capped.llm_unanswered == 1 and capped.row_flags[2]  # the unasked value keeps its flag


def test_dedup_keys_survive_a_missing_column():
    batch = tiny_batch(["a", "a"]).drop(columns=["agency"])
    keys = pipeline._dedup_keys(batch, ("agency", "descriptor", "?complaint_type"))
    assert keys == [(None, "a", True), (None, "a", True)]


# --------------------------------------------------------------------------
# Mixed batches
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ctx(reference_feed):
    batch = to_feed(pd.read_parquet("tests/data/batch_raw.parquet"))
    return injector.InjectionContext.build(pd.concat([reference_feed, batch], ignore_index=True))


@pytest.mark.parametrize("problems", [["d", "h", "j"], ["c", "g", "k"], ["i", "l", "f"], ["d", "k", "e"]])
def test_mixed_labels_are_exact(batch_feed, ctx, problems):
    m = pipeline.compose_mixed(batch_feed, problems, injector, ctx, np.random.default_rng(3))
    inserting = [p for p in problems if p in pipeline.INSERTING_PROBLEMS]
    base = batch_feed.reset_index(drop=True)
    copies = {int(c): int(o) for c, o in (m.params[inserting[0]]["copy_of"] if inserting else {}).items()}
    old = pipeline._old_positions(len(m.df), copies)
    others = [p for p in problems if p not in inserting]
    for p in others:  # each non-inserting type: its own rows, all of which really changed
        assert m.labels[p].any()
        for q in others:
            if q != p:
                assert not (m.labels[p] & m.labels[q])[[i for i in range(len(m.df)) if i not in copies]].any()
    any_label = np.zeros(len(m.df), dtype=bool)
    for p in others:
        any_label |= m.labels[p]
    originals = [i for i in range(len(m.df)) if i not in copies]
    unchanged = [i for i in originals if not any_label[i] and not (inserting and m.labels[inserting[0]][i])]
    left = m.df.loc[unchanged].reset_index(drop=True)
    right = base.loc[old[unchanged]].reset_index(drop=True)
    pd.testing.assert_frame_equal(left, right, check_dtype=False)  # unlabelled rows are untouched
    for copy, original in copies.items():  # a copy carries its original's corruptions
        for p in others:
            assert m.labels[p][copy] == m.labels[p][original]


def test_old_positions_follow_inserted_copies():
    assert pipeline._old_positions(5, {2: 0, 4: 3}).tolist() == [0, 1, 0, 2, 2]


# --------------------------------------------------------------------------
# End to end, small: real detectors and injector, fake LLM, fixture data
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def derived(tmp_path_factory, reference_raw, ctx):
    d = tmp_path_factory.mktemp("derived")
    feed = to_feed(pd.concat([reference_raw, pd.read_parquet("tests/data/batch_raw.parquet")], ignore_index=True))
    feed.to_parquet(d / evaluate.FEED_FILE, index=False)
    feed.to_parquet(d / evaluate.POOL_FILE, index=False)
    with open(d / evaluate.CONTEXT_FILE, "wb") as fh:
        pickle.dump(ctx, fh)
    (d / evaluate.COUNTS_FILE).write_text("{}")
    return d


def test_evaluate_pipeline_end_to_end(derived, tmp_path):
    def responder(prompt, schema):
        rows = rows_in_prompt(prompt)
        return {"rows": [{"id": r["id"], "flagged": True, "reason": "fake"} for r in rows]} if rows else {"issue_found": False, "reason": "fake"}

    llm.set_client(FakeClient(responder))
    try:
        budget = pipeline.PipelineBudget(n_single=1, n_clean=2, n_mixed=2, n_background=1, n_sweep=1)
        bench = evaluate.Budget(n_corrupted=2, n_clean=3, batch_size=500)
        summary = pipeline.evaluate_pipeline(derived, tmp_path, pipeline.plan_from_summary(None), "provisional", budget, bench)
    finally:
        llm.set_client(None)
    records = [json.loads(line) for line in (tmp_path / pipeline.RECORDS_FILE).read_text(encoding="utf-8").splitlines()]
    kinds = [r["kind"] for r in records]
    assert kinds.count("single") == 12 and kinds.count("clean") == 2 and kinds.count("mixed") == 2 and kinds.count("raw_background") == 1
    n_thresholded = sum(pipeline.thresholded(p) for p in "abcdefghijkl")
    assert kinds.count("alert_sweep") == n_thresholded * len(pipeline.config.RATE_SWEEP)  # 1 batch per (problem, rate)
    assert set(summary["problems"]) == set("abcdefghijkl") and summary["plan_source"] == "provisional"
    assert summary["cost"]["rows"] == sum(r["n_rows"] for r in records if r["kind"] in pipeline.MAIN_KINDS)
    # the alerting layer: calibrated before evaluation, written out, applied, and both verdicts recorded
    calibration = json.loads((tmp_path / pipeline.ALERT_FILE).read_text(encoding="utf-8"))
    assert set(calibration["checks"]) == {p for p in "abcdefghijkl" if pipeline.thresholded(p)}
    assert all(c["n_batches"] == 8 for c in calibration["checks"].values())  # 4,100 Jul-Aug fixture rows / 500
    assert summary["alerting"]["min_rows"]["l"] == 1 and summary["alerting"]["min_rows"]["a"] == 1  # critical
    for r in records:
        for p, e in r["problems"].items():
            assert e["batch_flag"] <= e["raw_batch_flag"]  # the layer can only hold an alert back, never add one
            assert e["alert_threshold"] == summary["alerting"]["min_rows"][p]
    assert summary["clean"]["any_alert_before"]["n"] == summary["clean"]["any_alert"]["n"] == 2
    assert (tmp_path / pipeline.TABLES_FILE).read_text(encoding="utf-8").startswith("# Project B - hybrid pipeline")
    for p, e in summary["problems"].items():
        assert e["single_detection"] is not None, p  # every problem was injected once



# --------------------------------------------------------------------------
# The batch alerting layer
# --------------------------------------------------------------------------
def test_min_rows_threshold_is_the_batch_level_detectors_rule():
    assert pipeline.min_rows_threshold([0] * 100) == 1  # never flags: every flag alerts
    assert pipeline.min_rows_threshold([0] * 95 + [1] * 5) == 1  # 5 of 100 alert at 1: within int(0.05 x 100)
    assert pipeline.min_rows_threshold([0] * 94 + [1] * 6) == 2
    assert pipeline.min_rows_threshold(list(range(20))) == 19  # int(0.05 x 20) = 1 batch may alert: only the 19
    assert pipeline.min_rows_threshold([3, 3, 3, 3]) == 4  # int(0.05 x 4) = 0: no calibration batch may alert


def test_critical_and_batch_level_checks_always_alert_on_the_first_flag():
    policy = pipeline.AlertPolicy({p: 50 for p in "abcdefghijkl"})
    assert {p for p in "abcdefghijkl" if policy.threshold(p) == 1} == set(pipeline.CRITICAL_PROBLEMS) == set("abcgl")
    assert policy.threshold("f") == 50 and pipeline.AlertPolicy().threshold("f") == 1


class FlagFirstRows(Detector):
    """Flags the first `n` rows of a batch (a row-level check for problem f)."""
    problem, family, name = "f", "rule", "rule_fake_f"
    n = 3

    def detect(self, batch, review_rows=None):
        flags = np.arange(len(batch)) < self.n
        return self.result(bool(flags.any()), row_flags=flags, alerts=[self.alert(f"{flags.sum()} rows", check_id="T")] if flags.any() else [])


class StructuralAlert(Detector):
    """Raises an alert with no row flags (e.g. a column it needs is missing)."""
    problem, family, name = "f", "stat", "stat_fake_structural"

    def detect(self, batch, review_rows=None):
        return self.result(True, alerts=[self.alert("cannot check: column missing", check_id="S")])


def one_step_pipeline(monkeypatch, problem, detectors, min_rows):
    monkeypatch.setattr(pipeline, "detector_classes", lambda: detectors)
    step = pipeline.Step(problem, tuple(d.name for d in detectors), reason="test")
    return pipeline.HybridPipeline({problem: step}, alerting=pipeline.AlertPolicy({problem: min_rows}))


def test_below_its_threshold_a_check_keeps_its_row_flags_but_raises_no_batch_alert(monkeypatch):
    batch = tiny_batch(["x"] * 10)
    FlagFirstRows.n = 3
    out = one_step_pipeline(monkeypatch, "f", [FlagFirstRows], min_rows=4).run(batch).outcomes["f"]
    assert np.flatnonzero(out.row_flags).tolist() == [0, 1, 2]  # the row layer is unchanged
    assert out.raw_batch_flag and not out.batch_flag and out.alerts == [] and out.n_alerts == 0 and out.n_alerts_raw == 1
    assert (out.n_flagged, out.alert_threshold) == (3, 4)
    FlagFirstRows.n = 4
    out = one_step_pipeline(monkeypatch, "f", [FlagFirstRows], min_rows=4).run(batch).outcomes["f"]
    assert out.batch_flag and out.n_alerts == 1  # at the threshold it alerts


def test_personal_data_alerts_on_a_single_flagged_row_whatever_the_threshold(monkeypatch):
    class FlagOneRowL(FlagFirstRows):
        problem, name, n = "l", "rule_fake_l", 1

    out = one_step_pipeline(monkeypatch, "l", [FlagOneRowL], min_rows=99).run(tiny_batch(["x"] * 10)).outcomes["l"]
    assert out.batch_flag and out.alert_threshold == 1


def test_an_alert_without_row_flags_passes_the_threshold_at_once(monkeypatch):
    FlagFirstRows.n = 1
    out = one_step_pipeline(monkeypatch, "f", [FlagFirstRows, StructuralAlert], min_rows=10).run(tiny_batch(["x"] * 10)).outcomes["f"]
    assert out.batch_flag and out.n_flagged == 1  # 1 flagged row is below 10, but the structural alert goes out


def test_calibration_reads_january_to_august_only_and_fits_on_january_to_june(derived, monkeypatch):
    periods, fitted_max = [], []
    real_read, real_fit = pipeline.evaluate.read_period, pipeline.HybridPipeline.fit

    def spy_read(path, reference):
        periods.append(reference)
        return real_read(path, reference)

    def spy_fit(self, reference):
        fitted_max.append(reference["created_date"].astype(str).max())
        return real_fit(self, reference)

    monkeypatch.setattr(pipeline.evaluate, "read_period", spy_read)
    monkeypatch.setattr(pipeline.HybridPipeline, "fit", spy_fit)
    plan = pipeline.plan_from_summary(None)
    policy, report = pipeline.calibrate_alert_policy(derived, plan, batch_size=500)
    assert periods == [True]  # the evaluation period is never read
    assert fitted_max and all(m < pipeline.ALERT_CALIBRATION_SPLIT for m in fitted_max)  # fitted on Jan-Jun only
    for p, check in report["checks"].items():
        assert policy.min_rows[p] == check["threshold"] == pipeline.min_rows_threshold(check["flagged_rows_per_batch"])
        assert check["batches_alerting_at_threshold"] <= check["allowed_to_alert"]
    assert set(report["checks"]) == {p for p in plan if pipeline.thresholded(p)} and "Gemini" in report["llm_confirmation"]
