"""Tests for project_b/detectors/semantic.py (problems j and k).

Corruptions are built by hand here, from DESIGN.md section 3 only (blue team: the injector is
not read). Rates measured on the real fixtures are printed (run pytest with -s) and asserted
loosely: the fixtures are unsanitised and the reference is a 15,000-row sample, so a few
real pre-existing issues and sampling gaps show up as "false alarms".
"""
from __future__ import annotations

import json
import re
import time

import numpy as np
import pandas as pd
import pytest
from sklearn.naive_bayes import MultinomialNB

from project_b import config, llm, metrics
from project_b.core import timed_detect
from project_b.detectors import semantic
from project_b.detectors.semantic import (
    LlmComplaintDescriptorReview,
    LlmResolutionStatusReview,
    RuleComplaintDescriptorAllowlist,
    RuleResolutionStatusKeywords,
    StatDescriptorClassifier,
    StatResolutionStatusClassifier,
)
from tests.fakes import FakeClient, rows_in_prompt

N_INJECTED = 100  # rows corrupted per hand-made injection (2 % of the 5,000-row batch)
RULE_STAT = [RuleComplaintDescriptorAllowlist, StatDescriptorClassifier, RuleResolutionStatusKeywords, StatResolutionStatusClassifier]


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fitted(reference_feed) -> dict:
    """Every rule / stat detector fitted once on the reference fixture."""
    detectors = {}
    for cls in RULE_STAT:
        t0 = time.perf_counter()
        detectors[cls.name] = cls().fit(reference_feed)
        print(f"\nfit {cls.name} on {len(reference_feed):,} rows: {time.perf_counter() - t0:.2f} s")
    return detectors


@pytest.fixture()
def fake_llm():
    """Install a FakeClient (no network) for one test and always restore the real one."""

    def install(responder=None, unavailable: bool = False) -> FakeClient:
        client = FakeClient(responder, unavailable=unavailable)
        llm.set_client(client)
        return client

    yield install
    llm.set_client(None)


# --------------------------------------------------------------------------
# hand-made corruptions
# --------------------------------------------------------------------------
def swap_descriptors(batch: pd.DataFrame, reference: pd.DataFrame, rng, variant: str, n: int = N_INJECTED):
    """Problem j: give n rows a descriptor that never co-occurs with their complaint type
    (ignoring case) in the reference or the batch, borrowed from a complaint type of another
    agency ('cross_agency') or of the same agency ('same_agency')."""
    known = pd.concat([f[["agency", "complaint_type", "descriptor"]] for f in (reference, batch)]).dropna().drop_duplicates()
    seen = set(zip(known["complaint_type"].str.lower(), known["descriptor"].str.lower()))
    out, labels = batch.copy(), np.zeros(len(batch), dtype=bool)
    candidates = np.flatnonzero((batch["descriptor"].notna() & batch["complaint_type"].notna()).to_numpy())
    for pos in rng.choice(candidates, size=n, replace=False):
        agency, ctype = batch.at[pos, "agency"], batch.at[pos, "complaint_type"]
        same_agency = known["agency"].eq(agency)
        donors = known[same_agency & known["complaint_type"].ne(ctype)] if variant == "same_agency" else known[~same_agency]
        pool = sorted({d for d in donors["descriptor"] if (ctype.lower(), d.lower()) not in seen})
        if pool:
            out.at[pos, "descriptor"] = pool[rng.integers(len(pool))]
            labels[pos] = True
    return out, labels


def _text_stats(reference: pd.DataFrame) -> pd.DataFrame:
    """Per resolution text: rows with a Closed / open status, share of open ones, main agency."""
    scoped = reference[reference["status"].isin(config.CLOSED_STATUSES | config.OPEN_STATUSES)]
    scoped = scoped.assign(is_open=scoped["status"].isin(config.OPEN_STATUSES))
    return scoped.groupby("resolution_description").agg(
        n=("is_open", "size"), open_share=("is_open", "mean"), agency=("agency", lambda s: s.mode().iloc[0])
    )


def give_closed_rows_open_text(batch: pd.DataFrame, reference: pd.DataFrame, rng, n: int = N_INJECTED):
    """Problem k, variant 1: n Closed rows get a text that is >= 95 % open-status in the
    reference (>= 3 rows), from the row's own agency when possible."""
    stats = _text_stats(reference)
    open_texts = stats[(stats["open_share"] >= 0.95) & (stats["n"] >= 3)]
    out, labels = batch.copy(), np.zeros(len(batch), dtype=bool)
    for pos in rng.choice(np.flatnonzero(batch["status"].eq("Closed").to_numpy()), size=n, replace=False):
        same_agency = open_texts.index[open_texts["agency"].eq(batch.at[pos, "agency"])]
        pool = same_agency if len(same_agency) else open_texts.index
        out.at[pos, "resolution_description"] = pool[rng.integers(len(pool))]
        labels[pos] = True
    return out, labels


def give_closed_text_open_status(batch: pd.DataFrame, reference: pd.DataFrame, rng, n: int = N_INJECTED):
    """Problem k, variant 2: n Closed rows whose text is >= 95 % Closed in the reference
    (>= 20 rows) get an open status and lose closed_date, so only the text is evidence."""
    stats = _text_stats(reference)
    closed_texts = stats.index[(stats["open_share"] <= 0.05) & (stats["n"] >= 20)]
    eligible = np.flatnonzero((batch["status"].eq("Closed") & batch["resolution_description"].isin(closed_texts)).to_numpy())
    rows = rng.choice(eligible, size=n, replace=False)
    out = batch.copy()
    out.loc[rows, "status"] = rng.choice(["Open", "In Progress", "Assigned"], size=n)
    out.loc[rows, "closed_date"] = np.nan
    labels = np.zeros(len(batch), dtype=bool)
    labels[rows] = True
    return out, labels


INJECTIONS = {
    "j/cross_agency": lambda b, r, rng: swap_descriptors(b, r, rng, "cross_agency"),
    "j/same_agency": lambda b, r, rng: swap_descriptors(b, r, rng, "same_agency"),
    "k/closed_with_open_text": give_closed_rows_open_text,
    "k/open_with_closed_text": give_closed_text_open_status,
}


def _mixed(values: list, index: pd.Index) -> pd.Series:
    """An object column cycling through values of different Python types."""
    return pd.Series(np.resize(np.array(values, dtype=object), len(index)), index=index, dtype=object)


CORRUPTIONS = {
    "empty_batch": lambda b: b.iloc[:0],
    "missing_columns": lambda b: b.drop(columns=["descriptor", "status"]),
    "missing_context_columns": lambda b: b.drop(columns=["agency", "closed_date"]),
    "renamed_columns": lambda b: b.rename(columns={"complaint_type": "problem", "resolution_description": "Resolution Description"}),
    "numbers_in_text_columns": lambda b: b.assign(descriptor=np.arange(len(b)), status=1.0, complaint_type=7),
    "mixed_python_types": lambda b: b.assign(
        complaint_type=_mixed([3, None, "Noise - Residential", 2.5, b"x"], b.index),
        resolution_description=_mixed([{"a": 1}, None, "The complaint has been closed.", 7, np.nan], b.index),
        status=_mixed(["Closed", 1, None, "Open", True], b.index),
    ),
    "strings_in_numeric_columns": lambda b: b.assign(latitude="40.7", unique_key="key"),
    "reformatted_dates": lambda b: b.assign(closed_date=pd.to_datetime(b["closed_date"]).dt.strftime("%m/%d/%Y %I:%M:%S %p")),
    "inserted_duplicate_rows": lambda b: pd.concat([b, b.iloc[:100]], ignore_index=True),
    "all_null_columns": lambda b: b.assign(descriptor=np.nan, complaint_type=np.nan, resolution_description=np.nan, status=np.nan),
    "non_default_index": lambda b: b.set_index(np.arange(len(b)) * 3 + 7),
    "duplicated_column_names": lambda b: pd.concat([b, b[["descriptor", "status"]]], axis=1),
    "categorical_dtype": lambda b: b.astype({"descriptor": "category", "status": "category"}),
}


# --------------------------------------------------------------------------
# module contract
# --------------------------------------------------------------------------
def test_one_detector_per_problem_and_family():
    assert sorted((cls.problem, cls.family) for cls in semantic.DETECTORS) == [
        ("j", "llm"), ("j", "rule"), ("j", "stat"), ("k", "llm"), ("k", "rule"), ("k", "stat"),
    ]
    for cls in semantic.DETECTORS:
        assert cls.name.startswith(f"{cls.family}_") and cls.description


# --------------------------------------------------------------------------
# detection on hand-made corruptions, false alarms on the untouched batch
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "detector_name, injection, min_recall",
    [
        ("rule_complaint_descriptor_allowlist", "j/cross_agency", 0.99),
        ("rule_complaint_descriptor_allowlist", "j/same_agency", 0.99),
        ("stat_descriptor_classifier", "j/cross_agency", 0.85),
        ("stat_descriptor_classifier", "j/same_agency", 0.85),
        ("rule_resolution_status_keywords", "k/closed_with_open_text", 0.95),
        ("rule_resolution_status_keywords", "k/open_with_closed_text", 0.80),
        ("stat_resolution_status_classifier", "k/closed_with_open_text", 0.85),
        ("stat_resolution_status_classifier", "k/open_with_closed_text", 0.85),
    ],
)
def test_detects_hand_made_corruptions(fitted, reference_feed, batch_feed, detector_name, injection, min_recall):
    corrupted, labels = INJECTIONS[injection](batch_feed, reference_feed, np.random.default_rng(config.SEED))
    assert labels.sum() >= 0.9 * N_INJECTED
    res = timed_detect(fitted[detector_name], corrupted)
    assert res.error is None
    recall = res.row_flags[labels].mean()
    other_flags = res.row_flags[~labels].mean()
    print(f"\n{detector_name} on {injection}: recall {recall:.3f} ({labels.sum()} injected), flagged non-injected rows {other_flags:.4f}")
    assert recall >= min_recall
    assert res.batch_flag


@pytest.mark.parametrize(
    "detector_name, max_rate",
    [
        # allow-list built from a 15,000-row sample: legitimate pairs seen < 2 times are common here
        ("rule_complaint_descriptor_allowlist", 0.05),
        ("stat_descriptor_classifier", 0.02),
        ("rule_resolution_status_keywords", 0.01),
        ("stat_resolution_status_classifier", 0.01),
    ],
)
def test_few_false_alarms_on_untouched_batch(fitted, batch_feed, detector_name, max_rate):
    res = timed_detect(fitted[detector_name], batch_feed)
    rate = res.row_flags.mean()
    print(f"\n{detector_name} on the untouched batch: {res.row_flags.sum()} rows flagged ({rate:.4f})")
    assert res.error is None
    assert rate <= max_rate


def test_stat_thresholds_are_calibrated_on_reference(fitted):
    for name in ("stat_descriptor_classifier", "stat_resolution_status_classifier"):
        det = fitted[name]
        print(f"\n{name}: threshold {det.threshold_:.4g}, reference FPR {det.reference_fpr_:.5f}")
        assert 0.0 < det.threshold_ < 1.0
        assert det.reference_fpr_ <= config.TARGET_ROW_FPR


def test_stat_j_choice_rests_on_reference_data_only(fitted, reference_feed):
    """Regression (BUG 5): stat j's model and weights were justified by false alarms on the October
    test batch, i.e. evaluation-period data ("twice the false alarms in our runs"). The choice was
    redone on reference data only (fit on Jan-Jul, scored on August, rule fixed beforehand), the
    docstring cites only that evidence, and the code is what it says won: NB, pairs weighted log(1 + rows)."""
    doc = " ".join(semantic.__doc__.split())
    assert not any(claim in doc for claim in ("in our runs", "twice the false alarms", "a third of the false alarms"))
    assert "chosen on reference data only: fit on Jan-Jul, scored on August" in doc  # j
    assert "Fit on Jan-Jul and scored on August" in doc  # k

    model = fitted["stat_descriptor_classifier"].model_
    per_type = np.log1p(semantic._type_descriptor_counts(reference_feed)).groupby(level="complaint_type").sum()
    assert isinstance(model, MultinomialNB)
    np.testing.assert_allclose(model.class_count_, per_type.loc[model.classes_].to_numpy())  # NB sums the sample weights


# --------------------------------------------------------------------------
# output contract of the row-level rule / stat detectors
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "detector_name, injection, check_id",
    [
        ("rule_complaint_descriptor_allowlist", "j/cross_agency", "R-j1"),
        ("stat_descriptor_classifier", "j/cross_agency", "S-j1"),
        ("rule_resolution_status_keywords", "k/closed_with_open_text", "R-k1"),
        ("stat_resolution_status_classifier", "k/open_with_closed_text", "S-k1"),
    ],
)
def test_one_aggregated_alert_and_a_reason_per_flagged_row(fitted, reference_feed, batch_feed, detector_name, injection, check_id):
    corrupted, _ = INJECTIONS[injection](batch_feed, reference_feed, np.random.default_rng(1))
    res = fitted[detector_name].detect(corrupted)
    flagged = set(np.flatnonzero(res.row_flags).tolist())
    assert res.row_flags.dtype == bool and len(res.row_flags) == len(corrupted)
    assert set(res.row_reasons) == flagged and all(res.row_reasons.values())

    assert res.batch_flag and len(res.alerts) == 1
    alert = res.alerts[0]
    assert alert.check_id == check_id and alert.columns
    assert alert.n_rows == len(flagged)
    assert 0 < len(alert.rows) <= semantic.MAX_EXAMPLE_ROWS and set(alert.rows) <= flagged
    assert alert.observed and alert.expected
    json.dumps(alert.to_dict())  # serialisable for results/*.json
    print(f"\n{alert.message}\n  observed: {alert.observed}\n  expected: {alert.expected}\n  e.g. {res.row_reasons[alert.rows[0]]}")


# --------------------------------------------------------------------------
# problem definitions at the edges
# --------------------------------------------------------------------------
def test_rule_j_allowlist_counts_pairs_ignoring_case_and_spacing():
    reference = pd.DataFrame(
        {
            "complaint_type": ["Noise - Residential", "Noise - Residential", "Noise - Residential", "HEAT/HOT WATER", "HEAT/HOT WATER"],
            "descriptor": ["Loud Music/Party", "loud music/party", "Banging/Pounding", "ENTIRE BUILDING", "Entire  Building"],
        },
        dtype="str",
    )
    det = RuleComplaintDescriptorAllowlist().fit(reference)
    batch = pd.DataFrame(
        {
            "complaint_type": ["NOISE - RESIDENTIAL", "Noise - Residential", "Heat/Hot Water", "Noise - Residential", None, "Noise - Residential"],
            "descriptor": ["Loud Music/Party", "Banging/Pounding", "entire building", "ENTIRE BUILDING", "Loud Music/Party", None],
        },
        dtype="str",
    )
    # seen twice -> allowed; seen once -> flagged; never seen together -> flagged; missing -> not checked
    assert det.detect(batch).row_flags.tolist() == [False, True, False, True, False, False]


def test_stat_k_reason_prints_a_readable_probability(fitted, reference_feed, batch_feed):
    """Regression: P was printed with three decimals, so a typical injected row (P ~ 1e-5) read
    'P(open | text) = 0.000 < 0.071' - a reason that shows no evidence."""
    corrupted, labels = give_closed_text_open_status(batch_feed, reference_feed, np.random.default_rng(5))
    res = fitted["stat_resolution_status_classifier"].detect(corrupted)
    reasons = [res.row_reasons[pos] for pos in np.flatnonzero(res.row_flags & labels)]
    assert reasons and not any("= 0.000 " in reason for reason in reasons)


def test_stat_j_flags_unknown_complaint_type_with_its_own_reason(fitted, batch_feed):
    batch = batch_feed.copy()
    batch.loc[0, ["complaint_type", "descriptor"]] = ["Totally New Type", "Loud Music/Party"]
    res = fitted["stat_descriptor_classifier"].detect(batch)
    assert res.row_flags[0] and "never occurred in the reference" in res.row_reasons[0]


def test_j_never_flags_a_missing_descriptor_or_type(fitted, reference_feed, batch_feed):
    corrupted, labels = swap_descriptors(batch_feed, reference_feed, np.random.default_rng(3), "cross_agency")
    corrupted.loc[labels, "descriptor"] = np.nan
    corrupted.loc[np.flatnonzero(labels)[:10], "complaint_type"] = np.nan
    for name in ("rule_complaint_descriptor_allowlist", "stat_descriptor_classifier"):
        assert not fitted[name].detect(corrupted).row_flags[labels].any()


@pytest.mark.parametrize(
    "status, text, flagged",
    [
        ("Closed", "The Department of Consumer and Worker Protection (DCWP) is reviewing your complaint.", True),
        ("Closed", "Your Service Request has been submitted to the Department of Buildings.  Please check back later for status.", True),
        ("Closed", "The Department of Transportation referred this complaint to the Inspections Unit for further action.", True),
        (" closed ", "The Department is reviewing your complaint.", True),  # status compared ignoring case and spaces
        ("Closed", "The Department reviewed this complaint and closed it.", False),
        ("Closed", "The complaint is a duplicate. The original complaint is being reviewed and will visit the site.", False),  # both kinds
        ("Closed", "The Department of Transportation will inspect the complaint location within 180 days.", False),  # routed, not pending
        ("In Progress", "The Police Department responded to the complaint and took action to fix the condition.", True),
        ("Pending", "No violations were issued. The complaint has been closed.", True),
        ("Open", "If you do not respond within 14 days, your complaint will be closed.", False),  # a warning, not a closure
        ("Assigned", "Service Request status for this request is available on the Department of Transportation's website.", False),
        ("Unspecified", "The complaint has been closed.", False),
        ("Unspecified", "The Department is reviewing your complaint.", False),
        ("Closed", None, False),
    ],
)
def test_rule_k_phrase_lists(status, text, flagged):
    batch = pd.DataFrame({"status": [status], "resolution_description": [text]}, dtype="str")
    assert RuleResolutionStatusKeywords().detect(batch).row_flags.tolist() == [flagged]


def test_blank_values_count_as_missing(fitted):
    """Regression: a blank or whitespace-only value says nothing, but it used to be checked as a
    value - rule j flagged ('Noise - Residential', ''), stat j flagged the blank complaint type as
    unknown, and stat k flagged open rows with an empty text as 'typical of closed requests'."""
    batch = pd.DataFrame(
        {
            "complaint_type": ["Noise - Residential", "Noise - Residential", "   ", "Noise - Residential"],
            "descriptor": ["", "   ", "Loud Music/Party", "\t"],
            "status": ["Open", "In Progress", "Closed", "Open"],
            "resolution_description": ["", "   ", "", " \n "],
        },
        dtype="str",
    )
    for name, det in fitted.items():
        res = det.detect(batch)
        assert not res.row_flags.any() and not res.alerts, name


def test_k_never_flags_unspecified_status_or_missing_text(fitted, reference_feed, batch_feed):
    corrupted, labels = give_closed_rows_open_text(batch_feed, reference_feed, np.random.default_rng(4))
    rows = np.flatnonzero(labels)
    corrupted.loc[rows[:40], "status"] = "Unspecified"
    corrupted.loc[rows[40:70], "status"] = np.nan
    corrupted.loc[rows[70:], "resolution_description"] = np.nan
    for name in ("rule_resolution_status_keywords", "stat_resolution_status_classifier"):
        assert not fitted[name].detect(corrupted).row_flags[labels].any()


# --------------------------------------------------------------------------
# robustness: detect() never raises on a corrupted batch
# --------------------------------------------------------------------------
@pytest.mark.parametrize("corruption", sorted(CORRUPTIONS))
@pytest.mark.parametrize("detector_name", [cls.name for cls in RULE_STAT])
def test_rule_and_stat_survive_corrupted_batches(fitted, batch_feed, detector_name, corruption):
    corrupted = CORRUPTIONS[corruption](batch_feed)
    res = fitted[detector_name].detect(corrupted)  # called directly: any exception fails the test
    assert res.error is None
    assert res.row_flags.dtype == bool and len(res.row_flags) == len(corrupted)
    if corruption in ("empty_batch", "missing_columns", "renamed_columns", "all_null_columns"):
        assert not res.batch_flag and not res.alerts


@pytest.mark.parametrize("corruption", sorted(CORRUPTIONS))
@pytest.mark.parametrize("cls", [LlmComplaintDescriptorReview, LlmResolutionStatusReview])
def test_llm_survives_corrupted_batches(fake_llm, batch_feed, cls, corruption):
    fake_llm(lambda prompt, schema: {"rows": [{"id": r["id"], "flagged": False} for r in rows_in_prompt(prompt)]})
    corrupted = CORRUPTIONS[corruption](batch_feed)
    res = cls().detect(corrupted, review_rows=np.arange(0, len(corrupted), 100))
    assert res.error is None and not res.batch_flag
    assert len(res.row_flags) == len(res.reviewed) == len(corrupted)


# --------------------------------------------------------------------------
# LLM detectors with a fake client
# --------------------------------------------------------------------------
def _responder(flag_ids: set[int], extra_verdicts: tuple = ()):
    """Fake model: flags the given ids among the rows it was shown, plus any extra verdicts."""

    def respond(prompt, schema):
        rows = [
            {"id": r["id"], "flagged": r["id"] in flag_ids, "reason": f"reason {r['id']}", "observed": "obs", "expected": "exp", "confidence": 0.8}
            for r in rows_in_prompt(prompt)
        ]
        return {"rows": rows + list(extra_verdicts)}

    return respond


@pytest.mark.parametrize(
    "cls, fields",
    [
        (LlmComplaintDescriptorReview, {"id", "agency", "complaint_type", "descriptor"}),
        (LlmResolutionStatusReview, {"id", "status", "resolution_description", "closed_date_present"}),
    ],
)
def test_llm_prompt_holds_only_the_needed_fields(fake_llm, batch_feed, cls, fields):
    client = fake_llm(_responder(set()))
    review = np.flatnonzero(batch_feed["incident_address"].notna().to_numpy())[[0, 7, 300]]
    cls().detect(batch_feed, review_rows=review)
    sent = [r for call in client.calls for r in rows_in_prompt(call["prompt"])]
    assert [r["id"] for r in sent] == review.tolist()  # ids are batch positions
    assert all(set(r) == fields for r in sent)
    prompt = client.calls[0]["prompt"]
    assert batch_feed.at[review[0], "incident_address"] not in prompt  # a column the check does not need
    assert {call["tag"] for call in client.calls} == {f"{cls.problem}/{cls.name}"}


def test_llm_k_sends_status_text_and_closed_date_presence(fake_llm, batch_feed):
    client = fake_llm(_responder(set()))
    with_date = int(np.flatnonzero(batch_feed["closed_date"].notna().to_numpy())[0])
    without_date = int(np.flatnonzero(batch_feed["closed_date"].isna().to_numpy())[0])
    LlmResolutionStatusReview().detect(batch_feed, review_rows=np.array([with_date, without_date]))
    sent = {r["id"]: r for r in rows_in_prompt(client.calls[0]["prompt"])}
    assert sent[with_date]["closed_date_present"] is True and sent[without_date]["closed_date_present"] is False
    assert sent[with_date]["status"] == batch_feed.at[with_date, "status"]
    assert sent[with_date]["resolution_description"] == batch_feed.at[with_date, "resolution_description"]


@pytest.mark.parametrize("cls", [LlmComplaintDescriptorReview, LlmResolutionStatusReview])
def test_llm_verdicts_map_back_to_reviewed_positions(fake_llm, batch_feed, cls):
    extra = ({"id": 6, "flagged": True, "reason": "not requested"}, {"id": 99_999, "flagged": True, "reason": "made up"})
    client = fake_llm(_responder({17, 4000}, extra))
    res = cls().detect(batch_feed, review_rows=np.array([4000, 5, 17, 17, -3, 6000]))  # unsorted, repeated, out of range
    assert np.flatnonzero(res.reviewed).tolist() == [5, 17, 4000]
    assert np.flatnonzero(res.row_flags).tolist() == [17, 4000]
    assert res.row_reasons == {17: "reason 17", 4000: "reason 4000"}
    assert res.row_evidence[17] == {"observed": "obs", "expected": "exp", "confidence": 0.8}
    alert = res.alerts[0]
    assert res.batch_flag and len(res.alerts) == 1 and alert.n_rows == 2 and alert.rows == [17, 4000]
    assert alert.deterministic is False and alert.check_id == f"L-{cls.problem}1" and alert.score == pytest.approx(0.8)
    assert res.usage.llm_calls == len(client.calls) == 1


def test_llm_flags_only_a_boolean_true_verdict(fake_llm, batch_feed):
    fake_llm(lambda prompt, schema: {"rows": [{"id": 1, "flagged": "true"}, {"id": 2, "flagged": 1}, {"id": 3, "flagged": True}]})
    res = LlmComplaintDescriptorReview().detect(batch_feed, review_rows=np.array([1, 2, 3]))
    assert np.flatnonzero(res.row_flags).tolist() == [3]
    assert res.row_reasons[3] == "flagged by the LLM"  # no reason given by the model


def test_llm_reviews_every_row_when_review_rows_is_none(fake_llm, batch_feed):
    client = fake_llm(_responder(set()))
    res = LlmResolutionStatusReview().detect(batch_feed.head(60))
    assert res.reviewed.all() and len(client.calls) == 3  # config.LLM_ROWS_PER_CALL = 25 rows per prompt


@pytest.mark.parametrize("cls", [LlmComplaintDescriptorReview, LlmResolutionStatusReview])
def test_llm_unavailable_gives_an_error_result(fake_llm, batch_feed, cls):
    fake_llm(unavailable=True)
    res = cls().detect(batch_feed, review_rows=np.array([1, 2]))
    assert res.error.startswith("llm_unavailable:") and not res.batch_flag and not res.alerts
    # regression: reviewed was None, which core.DetectionResult reads as "every row was examined"
    assert len(res.reviewed) == len(res.row_flags) == len(batch_feed)
    assert not res.reviewed.any() and not res.row_flags.any()


@pytest.mark.parametrize("reply, cause", [
    (None, llm.unreadable_cause("")), ("not json", llm.unreadable_cause("")),
    ({"rows": []}, llm.ABSTAIN_NO_VERDICT), ({"rows": [{"id": 99_999, "flagged": True}]}, llm.ABSTAIN_NO_VERDICT),
])
@pytest.mark.parametrize("cls", [LlmComplaintDescriptorReview, LlmResolutionStatusReview])
def test_llm_reply_without_any_verdict_abstains_on_every_row(fake_llm, batch_feed, cls, reply, cause):
    """Regression: a reply with no verdict for any reviewed row (unparseable, empty, or only ids
    the model made up) was scored as three reviewed, clean rows. Silence is no verdict: every row
    abstains - none reviewed, none clean, no alert - and its tokens still count."""
    client = fake_llm(lambda prompt, schema: reply)
    res = cls().detect(batch_feed, review_rows=np.array([1, 2, 3]))
    assert res.error is None and not res.batch_flag and not res.alerts
    assert len(res.reviewed) == len(batch_feed) and not res.reviewed.any() and not res.row_flags.any()
    assert res.abstained == dict.fromkeys([1, 2, 3], cause)
    assert res.usage.llm_calls == len(client.calls) == 1


def test_llm_partial_reply_abstains_on_the_rows_without_a_verdict(fake_llm, batch_feed):
    """Rows of an unparseable prompt are abstentions - not reviewed, never clean - while the rows
    the model did answer are scored; the alert says how many got no verdict."""
    prompts = []

    def respond(prompt, schema):
        prompts.append(prompt)
        if len(prompts) == 1:
            return None  # the first prompt (rows 0-24) comes back unparseable
        return {"rows": [{"id": r["id"], "flagged": r["id"] == 40, "reason": "x"} for r in rows_in_prompt(prompt)]}

    fake_llm(respond)
    res = LlmResolutionStatusReview().detect(batch_feed.head(60))
    assert res.error is None and len(prompts) == 3
    assert np.flatnonzero(res.reviewed).tolist() == list(range(25, 60))
    assert res.abstained == dict.fromkeys(range(25), llm.unreadable_cause(""))
    assert np.flatnonzero(res.row_flags).tolist() == [40]
    assert "(25 rows got no verdict)" in res.alerts[0].message


def test_llm_alert_shows_only_evidence_the_model_gave(fake_llm, batch_feed):
    """Regression: observed / expected were lists such as [None, None] when the model gave no
    evidence, and the clarity metric counts any non-empty list as a value shown."""
    fake_llm(lambda prompt, schema: {"rows": [
        {"id": 1, "flagged": True},
        {"id": 2, "flagged": True, "observed": "  ", "expected": None},
        {"id": 3, "flagged": True, "observed": "Pothole", "expected": "a noise descriptor"},
    ]})
    alert = LlmComplaintDescriptorReview().detect(batch_feed, review_rows=np.array([1, 2, 3])).alerts[0]
    assert alert.observed == {3: "Pothole"} and alert.expected == {3: "a noise descriptor"}
    json.dumps(alert.to_dict())

    fake_llm(lambda prompt, schema: {"rows": [{"id": 1, "flagged": True, "reason": "no evidence given"}]})
    alert = LlmComplaintDescriptorReview().detect(batch_feed, review_rows=np.array([1])).alerts[0]
    assert alert.observed is None and alert.expected is None
    assert metrics.clarity_score(alert) == 2  # names the check + localises; no evidence; not deterministic


def test_llm_sends_null_for_a_blank_value(fake_llm, batch_feed):
    client = fake_llm(_responder(set()))
    batch = batch_feed.copy()
    batch.loc[0, ["descriptor", "resolution_description", "closed_date"]] = ["  ", "", " "]
    LlmComplaintDescriptorReview().detect(batch, review_rows=np.array([0]))
    LlmResolutionStatusReview().detect(batch, review_rows=np.array([0]))
    j_record, k_record = (rows_in_prompt(call["prompt"])[0] for call in client.calls)
    assert j_record["descriptor"] is None
    assert k_record["resolution_description"] is None and k_record["closed_date_present"] is False


def test_llm_sends_null_for_a_missing_context_column(fake_llm, batch_feed):
    client = fake_llm(_responder(set()))
    pos = int(np.flatnonzero(batch_feed["descriptor"].notna().to_numpy())[0])
    LlmComplaintDescriptorReview().detect(batch_feed.drop(columns=["agency"]), review_rows=np.array([pos]))
    LlmResolutionStatusReview().detect(batch_feed.drop(columns=["closed_date"]), review_rows=np.array([pos]))
    j_record, k_record = (rows_in_prompt(call["prompt"])[0] for call in client.calls)
    assert j_record["agency"] is None and j_record["descriptor"] == batch_feed.at[pos, "descriptor"]
    assert k_record["closed_date_present"] is None and k_record["status"] == batch_feed.at[pos, "status"]


@pytest.mark.parametrize("cls, column", [(LlmComplaintDescriptorReview, "descriptor"), (LlmResolutionStatusReview, "status")])
def test_llm_skips_a_batch_without_the_needed_column(fake_llm, batch_feed, cls, column):
    client = fake_llm(_responder(set()))
    res = cls().detect(batch_feed.drop(columns=[column]), review_rows=np.array([1, 2]))
    assert client.calls == [] and not res.reviewed.any() and not res.batch_flag


# --------------------------------------------------------------------------
# LLM prompts: no answers handed over
# --------------------------------------------------------------------------
# A real resolution template (NYC Parks tree work orders), one of the texts K_TASK used to quote.
PARKS_TREE_TEMPLATE = (
    "NYC Parks created a work order for the tree condition and expects to address the issue within 120 days "
    "from the date of the tree assessment, barring any weather-related requests."
)


def _word_runs(text: str, n: int = 5) -> set[tuple[str, ...]]:
    """Every run of n consecutive words of `text`, ignoring case and punctuation."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


@pytest.mark.parametrize("task", ["J_TASK", "K_TASK"])
def test_llm_prompts_copy_no_resolution_template(reference_feed, task):
    """Regression (BUG 7): K_TASK quoted real resolution templates word for word - among them the NYC
    Parks tree work-order text, a large share of the k contradictions - which hands the model the answer
    for every row carrying that text. The prompts describe the kinds of text instead: no run of five
    words from any reference resolution text, or from the Parks template, may appear in them."""
    prompt = _word_runs(getattr(semantic, task))
    templates = [PARKS_TREE_TEMPLATE, *reference_feed["resolution_description"].dropna().unique()]
    copied = sorted({" ".join(run) for text in templates for run in _word_runs(text) & prompt})
    assert not copied, f"{task} copies template text: {copied[:5]}"


def test_llm_j_prompt_quotes_no_real_category(reference_feed):
    """Regression (BUG 7): J_TASK quoted real complaint types and descriptors as examples, including the
    very pairs to flag; it describes the kinds of category in general words instead."""
    prompt = semantic.J_TASK.lower()
    values = pd.concat([reference_feed["complaint_type"], reference_feed["descriptor"]]).dropna().unique()
    quoted = [v for v in values if f"'{v.lower()}'" in prompt or f'"{v.lower()}"' in prompt]
    assert not quoted


# --------------------------------------------------------------------------
# speed
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cls", RULE_STAT)
def test_speed_fit_on_10x_reference_and_detect_on_a_batch(reference_feed, batch_feed, cls):
    big_reference = pd.concat([reference_feed] * 10, ignore_index=True)  # 150,000 rows
    t0 = time.perf_counter()
    det = cls().fit(big_reference)
    fit_s = time.perf_counter() - t0
    detect_s = np.median([timed_detect(det, batch_feed).usage.wall_s for _ in range(3)])
    print(f"\n{cls.name}: fit on {len(big_reference):,} rows {fit_s:.2f} s, detect on {len(batch_feed):,} rows {detect_s * 1000:.0f} ms")
    assert fit_s < 60
    assert detect_s < 1.0
