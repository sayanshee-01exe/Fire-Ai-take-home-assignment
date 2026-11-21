"""Unit tests for project_b/metrics.py: each DESIGN section 6 definition on hand-checked numbers."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from project_b import config, metrics
from project_b.core import Alert


# --------------------------------------------------------------------------
# Wilson intervals and proportions
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "k, n, low, high",
    [(5, 10, 0.2366, 0.7634), (0, 10, 0.0, 0.2775), (0, 20, 0.0, 0.1611), (20, 20, 0.8389, 1.0), (93, 100, 0.8625, 0.9657)],
)
def test_wilson_ci_matches_textbook_values(k, n, low, high):
    lo, hi = metrics.wilson_ci(k, n)
    assert lo == pytest.approx(low, abs=1e-4)
    assert hi == pytest.approx(high, abs=1e-4)


def test_nothing_measured_is_none_not_zero():
    assert metrics.wilson_ci(0, 0) is None
    assert metrics.proportion(0, 0) == {"value": None, "ci": None, "k": 0, "n": 0}


def test_proportion_carries_k_n_and_interval():
    p = metrics.proportion(3, 4)
    assert p["value"] == 0.75 and (p["k"], p["n"]) == (3, 4)
    assert p["ci"][0] < 0.75 < p["ci"][1]


# --------------------------------------------------------------------------
# Row-level and batch-level metrics
# --------------------------------------------------------------------------
def test_confusion_counts_only_reviewed_rows():
    flags = np.array([1, 1, 0, 0, 1, 0], dtype=bool)
    labels = np.array([1, 0, 1, 0, 1, 1], dtype=bool)
    reviewed = np.array([1, 1, 1, 1, 0, 0], dtype=bool)  # the last two were never looked at
    assert metrics.confusion(flags, labels, reviewed) == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}


def test_confusion_of_a_crashed_detector_misses_everything():
    labels = np.array([1, 0, 1], dtype=bool)
    assert metrics.confusion(None, labels, np.ones(3, dtype=bool)) == {"tp": 0, "fp": 0, "fn": 2, "tn": 1}


def test_sum_counts_pools_records():
    pooled = metrics.sum_counts([{"tp": 1, "fp": 2, "fn": 3, "tn": 4, "other": 9}, {"tp": 1, "fp": 0, "fn": 0, "tn": 5}])
    assert pooled == {"tp": 2, "fp": 2, "fn": 3, "tn": 9}


def test_precision_at_rate_equals_plain_precision_on_a_full_batch():
    # 5,000 rows, 100 injected (2 %): 90 caught, 49 false alarms
    tp, fn, fp, tn = 90, 10, 49, 4851
    precision = metrics.precision_at_rate(tp / (tp + fn), fp / (fp + tn), rate=100 / 5000)
    assert precision == pytest.approx(tp / (tp + fp))


def test_precision_at_rate_reweights_an_enriched_sample():
    # LLM sample: 100 injected + 100 clean rows. Raw sample precision 90/91 would flatter it;
    # at a 2 % corruption rate the same recall / FPR give far lower precision.
    precision = metrics.precision_at_rate(recall=0.9, fpr=0.01, rate=0.02)
    assert precision == pytest.approx(0.018 / (0.018 + 0.0098))
    assert metrics.precision_at_rate(None, 0.01, 0.02) is None
    assert metrics.precision_at_rate(0.0, 0.0, 0.02) is None  # flagged nothing: undefined


def test_row_metrics():
    corrupted = {"tp": 90, "fp": 49, "fn": 10, "tn": 4851}
    clean = {"tp": 0, "fp": 5, "fn": 0, "tn": 4995}
    m = metrics.row_metrics(corrupted, clean, rate=0.02)
    assert m["detection"]["value"] == 0.9 and m["detection"]["n"] == 100
    assert m["row_fpr"]["value"] == 0.001 and m["row_fpr"]["n"] == 5000
    assert m["precision_at_rate"] == pytest.approx(0.018 / (0.018 + 0.98 * 0.01))


def test_batch_metrics():
    m = metrics.batch_metrics([True, True, False, True], [False, False, True, False, False])
    assert (m["batch_detection"]["k"], m["batch_detection"]["n"]) == (3, 4)
    assert m["batch_far"]["value"] == 0.2


# --------------------------------------------------------------------------
# Localisation
# --------------------------------------------------------------------------
RENAMED_COLUMNS = [("zip_code" if c == "incident_zip" else c) for c in config.COLUMNS]
RENAME_PARAMS = {"variant": "rename", "columns": ["incident_zip"], "old_name": "incident_zip", "new_name": "zip_code"}


@pytest.mark.parametrize(
    "named, ok",
    [(["incident_zip"], True), (["zip_code"], True), (["Incident Zip"], True), (["borough"], False), ([], False)],
)
def test_localisation_accepts_the_old_or_new_name_of_a_renamed_column(named, ok):
    alerts = [{"columns": named, "segment": {}}]
    assert metrics.localisation_ok(alerts, RENAME_PARAMS, RENAMED_COLUMNS) is ok


def test_localisation_of_a_null_spike_needs_the_agency_as_a_whole_word():
    params = {"agency": "DEP", "columns": ["latitude", "longitude"]}

    def ok(columns, segment):
        return metrics.localisation_ok([{"columns": columns, "segment": segment}], params, config.COLUMNS)

    assert ok(["latitude"], {"agency": "DEP"})
    assert ok(["longitude"], {"text": "rows of DEP lost coordinates"})  # LLM-style free-text segment
    assert not ok(["latitude"], {"agency": "Department of Transportation"})  # 'DEP' inside a word
    assert not ok(["incident_zip"], {"agency": "DEP"})  # right agency, wrong column
    assert not ok(["latitude"], {"agency": "NYPD"})


def test_localisation_is_true_if_any_alert_is_right():
    alerts = [{"columns": ["borough"]}, {"columns": ["zip_code"]}]
    assert metrics.localisation_ok(alerts, RENAME_PARAMS, RENAMED_COLUMNS)


def test_localisation_reads_a_segment_or_column_of_the_wrong_type_instead_of_crashing():
    params = {"agency": "DEP", "columns": ["incident_zip"]}
    assert metrics.localisation_ok([{"columns": "incident_zip", "segment": "DEP"}], params, config.COLUMNS)
    assert not metrics.localisation_ok([{"columns": None, "segment": None}], params, config.COLUMNS)
    assert not metrics.localisation_ok([{"columns": ["incident_zip"]}], {"columns": None}, config.COLUMNS)


# --------------------------------------------------------------------------
# Explanation clarity and grounding
# --------------------------------------------------------------------------
def _alert(**kw) -> Alert:
    base = {"problem": "h", "family": "rule", "detector": "rule_x", "message": "closed before created"}
    return Alert(**{**base, **kw})


def test_clarity_full_marks_for_a_complete_rule_alert():
    alert = _alert(check_id="R-h1", columns=["closed_date"], rows=[3, 7], observed="2023-01-01", expected=">= created_date")
    assert metrics.clarity_score(alert) == 5
    assert metrics.clarity_score(alert.to_dict()) == 5  # records hold alerts as dicts


def test_clarity_llm_alert_loses_the_reproducibility_point():
    alert = _alert(family="llm", columns=["closed_date"], rows=[3], observed="x", expected="y", deterministic=False)
    assert metrics.clarity_score(alert) == 4


def test_clarity_bare_alert_only_names_the_problem_and_is_reproducible():
    assert metrics.clarity_score(_alert()) == 2


def test_clarity_localisation_needs_rows_or_segment_except_for_schema_problems():
    assert metrics.clarity_score(_alert(columns=["closed_date"])) == 2  # no rows / segment
    assert metrics.clarity_score(_alert(columns=["latitude"], segment={"agency": "DEP"})) == 3
    assert metrics.clarity_score(_alert(problem="a", columns=["incident_zip"])) == 3  # the column is the location


def test_clarity_zero_counts_as_an_observed_value_but_empty_does_not():
    assert metrics.clarity_score(_alert(observed=0, expected=0.05)) == 4
    assert metrics.clarity_score(_alert(observed="  ", expected=[])) == 2


def test_clarity_an_empty_segment_or_evidence_inside_a_container_shows_nothing():
    # an LLM verdict that named no agency and quoted nothing: {"text": ""} and {12: None} are empty
    vague = _alert(family="llm", columns=["incident_zip"], segment={"text": ""}, observed={12: None}, expected="filled", deterministic=False)
    assert metrics.clarity_score(vague) == 2  # names the problem, shows the expected value
    precise = _alert(columns=["incident_zip"], segment={"agency": "DEP"}, observed={12: "null"}, expected=[0, None])
    assert metrics.clarity_score(precise) == 5  # 0 inside a list is still a value


@pytest.mark.parametrize(
    "observed, values, grounded",
    [
        ("40.7128", ["40.71280001", "BROOKLYN"], True),  # part of a value
        ("latitude=-73.95, longitude=40.71", ["-73.95", "40.71"], True),  # labelled quote of values
        ("MARIA  gonzalez", ["Spoke with tenant Maria Gonzalez."], True),  # case and spacing ignored
        ("John Smith", ["Spoke with tenant Maria Gonzalez."], False),  # invented evidence
        ("ab", ["ab cd"], False),  # too short to be evidence
        (None, ["anything"], False),
    ],
)
def test_is_grounded(observed, values, grounded):
    assert metrics.is_grounded(observed, values) is grounded


def test_grounding_counts_over_flagged_rows():
    batch = pd.DataFrame({"resolution_description": ["Call (212) 555-0142.", "Closed.", "Spoke with Ana Silva."], "latitude": [40.7, np.nan, 40.8]})
    evidence = {0: {"observed": "(212) 555-0142"}, 2: {"observed": "Ana Lopez"}}  # row 1 has no evidence at all
    assert metrics.grounding_counts(batch, np.array([0, 1, 2]), evidence) == {"grounded": 1, "flagged": 3}
    assert metrics.grounding_counts(batch, np.array([0]), {0: "(212) 555-0142"}) == {"grounded": 0, "flagged": 1}  # not a dict


# --------------------------------------------------------------------------
# Cost, throughput, determinism
# --------------------------------------------------------------------------
def test_compute_cost_per_million_rows():
    cost = metrics.cost_per_million_rows(5000, cpu_seconds=0.5)
    assert cost["usd"] == pytest.approx(0.5 / 5000 * 1e6 / 3600 * config.COMPUTE_USD_PER_VCPU_HOUR)
    assert cost["llm_usd"] == 0.0 and cost["note"] is None


def test_llm_cost_per_million_rows(monkeypatch):
    monkeypatch.setattr(config, "LLM_PRICES_PER_MTOK", {"m": {"input": 0.10, "output": 0.40}})
    # 100 rows reviewed with 10,000 input and 2,000 output tokens: 100 + 20 tokens per row
    cost = metrics.cost_per_million_rows(100, input_tokens=10_000, output_tokens=2_000, model="m")
    assert cost["usd"] == pytest.approx(100 * 0.10 + 20 * 0.40)  # USD per 1M rows


def test_unknown_llm_price_gives_no_number_and_a_note():
    cost = metrics.cost_per_million_rows(100, input_tokens=10, model="not-a-model")
    assert cost["usd"] is None and "not-a-model" in cost["note"]
    assert metrics.cost_per_million_rows(0)["usd"] is None


def test_rows_per_second_is_the_median_over_batches():
    assert metrics.rows_per_second([5000, 5000, 5000], [0.5, 1.0, 0.25]) == 10_000
    assert metrics.rows_per_second([5000], [0.0]) is None


def test_llm_throughput_and_free_tier_ceiling():
    t = metrics.llm_throughput(rows=100, calls=4, call_latencies_s=[1.0, 2.0, 3.0])
    requests_per_day = min(config.GEMINI_RPM * 24 * 60, metrics.FREE_TIER_REQUESTS_PER_DAY)
    assert (t["rows_per_call"], t["median_latency_s"], t["rows_per_s"]) == (25, 2.0, 12.5)
    assert t["free_tier_rows_per_day"] == 25 * requests_per_day
    assert metrics.llm_throughput(0, 0, [])["rows_per_s"] is None


def test_determinism_agreement():
    assert metrics.determinism_agreement([{1, 2}, {1, 2}, {1, 2}], 10)["value"] == 1.0
    assert metrics.determinism_agreement([{1, 2}, {1, 3}, {1}], 10)["k"] == 8  # rows 2 and 3 flip
    assert metrics.determinism_agreement([{0}, {0}, set()], 1)["value"] == 0.0  # batch verdict flipped


def test_determinism_agreement_never_crashes_on_inconsistent_input():
    # every flagged row is an item: 3 flipped rows out of a claimed 2 means at least 3 items
    assert metrics.determinism_agreement([{1, 2, 3}, set()], 2) == metrics.proportion(0, 3)
    assert metrics.determinism_agreement([], 5)["value"] is None
