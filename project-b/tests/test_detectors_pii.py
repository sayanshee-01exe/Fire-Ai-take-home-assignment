"""Tests for problem l - personal data in free-text fields (project_b/detectors/pii.py).

Blue-team rule: corruptions are built by hand here, never with project_b.inject. Personal
data is appended to resolution_description of 2 % of the rows of the real October batch, in
the five variants of DESIGN.md section 3: phone, e-mail, name after a cue word, bare name,
and name + phone/e-mail.

Measured rates are printed (`python -m pytest tests/test_detectors_pii.py -q -s`). Asserts on
real data are deliberately loose; the reported numbers come from the evaluation harness.
"""
from __future__ import annotations

import json
import re
import time

import numpy as np
import pandas as pd
import pytest

from project_b import config, llm
from project_b.core import Detector
from project_b.detectors.pii import (
    DETECTORS, HOLDOUT_SHARE, LlmPiiReview, RulePiiRegex, StatPiiTemplateNovelty, _mask, _pii_matches, _tokens,
)
from tests.fakes import FakeClient, rows_in_prompt

VARIANTS = ["phone", "email", "name_with_cue", "name_bare", "mixed"]
NAMES = ["Lucia Moreno", "Darnell Hughes", "Priya Raman", "Tomasz Nowak", "Keisha Brown", "Hiroshi Tanaka", "Olga Petrova", "Samuel Okafor"]
PHONES = ["(917) 555-0142", "347-555-0188", "718.555.0123", "646 555 0199", "+1 212 555 0170", "9295550111"]
EMAILS = ["lucia.moreno@gmail.com", "dhughes77@yahoo.com", "p.raman@outlook.com", "tnowak@example.com"]
CORRUPTIONS = [
    "dropped_text_column", "renamed_text_column", "no_free_text_at_all", "text_column_as_numbers",
    "mixed_types_in_text", "all_null_text", "numbers_as_strings", "reformatted_dates",
    "duplicate_rows_inserted", "non_positional_index", "empty_batch",
    "duplicated_header", "list_values_in_text", "one_row_batch",
]


# --------------------------------------------------------------------------------------
# hand-made corruptions
# --------------------------------------------------------------------------------------
def pii_sentence(variant: str, rng: np.random.Generator) -> str:
    name, phone, email = rng.choice(NAMES), rng.choice(PHONES), rng.choice(EMAILS)
    options = {
        "phone": [f"Tenant can be reached at {phone}.", f"Callback number {phone}."],
        "email": [f"Follow-up sent to {email}.", f"Photos received from {email}."],
        "name_with_cue": [f"Spoke with tenant {name}.", f"Caller {name} confirmed the repair."],
        "name_bare": [f"Per {name}, condition corrected.", f"{name} reported the condition again."],
        "mixed": [f"Spoke with {name} at {phone}.", f"Contact {name}, {email}."],
    }[variant]
    return str(rng.choice(options))


def inject_pii(batch: pd.DataFrame, variant: str, rate: float = config.ROW_INJECTION_RATE, seed: int = 0) -> tuple[pd.DataFrame, np.ndarray]:
    """Append a personal-data sentence to resolution_description of `rate` of the rows."""
    rng = np.random.default_rng(seed)
    out = batch.copy()
    rows = np.sort(rng.choice(len(out), size=max(1, round(rate * len(out))), replace=False))
    col = out.columns.get_loc("resolution_description")
    for pos in rows:
        old = out.iat[pos, col]
        out.iat[pos, col] = f"{old if isinstance(old, str) else ''} {pii_sentence(variant, rng)}".strip()
    labels = np.zeros(len(out), dtype=bool)
    labels[rows] = True
    return out, labels


def corrupt(batch: pd.DataFrame, kind: str) -> pd.DataFrame:
    """Hand-made versions of the corruptions the other problems inject."""
    if kind == "dropped_text_column":
        return batch.drop(columns=["resolution_description"])
    if kind == "renamed_text_column":
        return batch.rename(columns={"descriptor": "Descriptor"})
    if kind == "no_free_text_at_all":
        return batch.drop(columns=config.FREE_TEXT_COLUMNS)
    if kind == "text_column_as_numbers":
        return batch.assign(resolution_description=np.arange(len(batch), dtype=float))
    if kind == "mixed_types_in_text":
        return batch.assign(descriptor_2=np.resize(np.array(["x", 1, 2.5, None], dtype=object), len(batch)))
    if kind == "all_null_text":
        return batch.assign(resolution_description=None)
    if kind == "numbers_as_strings":
        return batch.assign(latitude=batch["latitude"].astype(str), unique_key=batch["unique_key"].astype(str))
    if kind == "reformatted_dates":
        created = pd.to_datetime(batch["created_date"], format=config.SOURCE_DATETIME_FORMAT)
        return batch.assign(created_date=created.dt.strftime("%m/%d/%Y %I:%M:%S %p"))
    if kind == "duplicate_rows_inserted":
        return pd.concat([batch, batch.iloc[:100]], ignore_index=True)
    if kind == "non_positional_index":
        return batch.set_index(batch.index + 1000)
    if kind == "empty_batch":
        return batch.iloc[:0]
    if kind == "duplicated_header":  # a rename onto an existing name: batch["descriptor"] is a DataFrame
        return batch.rename(columns={"descriptor_2": "descriptor"})
    if kind == "list_values_in_text":  # a JSON feed delivering an array; str() of it holds a name
        return batch.assign(descriptor_2=pd.Series([["Spoke with", "Lucia Moreno"]] * len(batch), index=batch.index, dtype=object))
    if kind == "one_row_batch":
        return batch.iloc[7:8]
    raise ValueError(kind)


def rates(flags: np.ndarray, labels: np.ndarray) -> tuple[float, float]:
    """(recall on injected rows, false-positive rate on the untouched rows)."""
    return float(flags[labels].mean()), float(flags[~labels].mean())


def pii_responder(prompt: str, schema: dict | None) -> dict:
    """A perfect fake reviewer: flags exactly the rows holding our synthetic personal data."""
    verdicts = []
    for rec in rows_in_prompt(prompt):
        text = " ".join(str(rec[c]) for c in config.FREE_TEXT_COLUMNS if rec.get(c) is not None)
        found = [s for s in NAMES + PHONES + EMAILS if s in text]
        verdicts.append({
            "id": rec["id"], "flagged": bool(found), "reason": f"contains {found[0]}" if found else "",
            "observed": found[0] if found else "", "expected": "no personal data", "confidence": 0.9,
        })
    return {"rows": verdicts}


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def stat_small(reference_feed) -> StatPiiTemplateNovelty:
    """Fitted on the 15k-row fixture as is. Rare templates are sampled once, so 1.5 % of its
    held-out rows carry a novel word and the calibrated bar ends up at three novel words: an
    unrealistic regime, kept to show the calibration reacting to template drift."""
    return StatPiiTemplateNovelty().fit(reference_feed)


@pytest.fixture(scope="module")
def stat_large(reference_feed) -> StatPiiTemplateNovelty:
    """Fitted on the fixture repeated 10x, so every template is in both the vocabulary part and
    the held-out part and the threshold comes out 0 (any never-seen token flags). That is the
    regime of the real reference: measured once on the 2023 part files, ~0.005 % of the Jul-Aug
    held-out rows hold a token unseen in Jan-Jun (< TARGET_ROW_FPR), giving threshold 0 and a
    clean-row FPR of 0.02-0.08 % per Sep-Dec month."""
    return StatPiiTemplateNovelty().fit(pd.concat([reference_feed] * 10, ignore_index=True))


@pytest.fixture()
def fake_llm():
    """Install a FakeClient (scripted answers, no network); always restore the real client."""
    def install(responder=None, unavailable: bool = False) -> FakeClient:
        client = FakeClient(responder, unavailable=unavailable)
        llm.set_client(client)
        return client

    yield install
    llm.set_client(None)


# --------------------------------------------------------------------------------------
# contract
# --------------------------------------------------------------------------------------
def test_detector_classes_follow_the_contract():
    assert [cls.family for cls in DETECTORS] == ["rule", "stat", "llm"]
    for cls in DETECTORS:
        assert issubclass(cls, Detector) and cls.problem == "l"
        assert cls.name.startswith(f"{cls.family}_") and cls.description


# --------------------------------------------------------------------------------------
# rule_pii_regex
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, check_id", [
    ("Reach me at (917) 555-0142 please", "R-l2"),
    ("call 917-555-0142", "R-l2"),
    ("call 917.555.0142", "R-l2"),
    ("call 917 555 0142", "R-l2"),
    ("call +1 917 555 0142", "R-l2"),
    ("call 9175550142", "R-l2"),
    ("call 555-0142", "R-l2"),
    ("mail lucia.moreno@gmail.com.", "R-l1"),
    ("Spoke with tenant Lucia Moreno.", "R-l3"),
    ("Caller: Darnell Hughes reported it", "R-l3"),
    ("Ms. Raman was not home", "R-l3"),
    ("spoke with José Ramírez", "R-l3"),
])
def test_rule_patterns_match_personal_data(text, check_id):
    assert check_id in [check for check, _ in _pii_matches(text)]


@pytest.mark.parametrize("text", [
    "please call 311 (or 212-639-9675 if calling from a non-New York City area code)",
    "If you are outside of New York City, please call (212) NEW-YORK (212-639-9675).",
    "please call (212) 676-1600 or email infobfscs@health.nyc.gov to request an inspection report.",
    "You should contact the New York State Office of the Attorney General. Call (800) 771-7755.",
    "please contact the borough office with your complaint number at 718 827 - 1955 (BROOKLYN).",
    "Tenant Refused Install Request",
    "contact HPD at the Tenant Information Messaging Service at 212-863-4951.",
    "Section 19-152 of the Administrative Code; within 120 days; call 911 in an emergency",
])
def test_rule_ignores_public_template_text(text):
    assert _pii_matches(text) == []


@pytest.mark.parametrize("variant", VARIANTS)
def test_rule_detects_injected_personal_data(batch_feed, variant):
    bad, labels = inject_pii(batch_feed, variant)
    recall, fpr = rates(RulePiiRegex().detect(bad).row_flags, labels)
    print(f"\n[l] rule_pii_regex {variant}: recall {recall:.3f}, FPR on untouched rows {fpr:.5f}")
    if variant == "name_bare":
        assert recall < 0.2  # by design: no cue word for the rule to anchor on
    else:
        assert recall >= 0.95
    assert fpr < 0.002


def test_rule_is_quiet_on_the_untouched_batch(batch_feed):
    res = RulePiiRegex().detect(batch_feed)
    print(f"\n[l] rule_pii_regex untouched October batch: {res.row_flags.sum()} of {len(batch_feed)} rows flagged")
    assert res.row_flags.mean() < 0.001


def test_rule_alert_and_reasons_explain_every_flagged_row(batch_feed):
    bad, labels = inject_pii(batch_feed, "mixed")
    res = RulePiiRegex().detect(bad)
    assert res.batch_flag and len(res.alerts) == 1
    alert = res.alerts[0]
    assert set(alert.check_id.split(",")) == {"R-l1", "R-l2", "R-l3"}
    assert alert.columns == ["resolution_description"]
    assert alert.n_rows == res.row_flags.sum() and len(alert.rows) <= 20
    assert alert.observed and alert.expected and alert.deterministic
    assert set(res.row_reasons) == set(np.flatnonzero(res.row_flags))
    for reason in res.row_reasons.values():
        assert any(s in reason for s in NAMES + PHONES + EMAILS)


# --------------------------------------------------------------------------------------
# stat_pii_template_novelty
# --------------------------------------------------------------------------------------
def test_stat_threshold_is_calibrated_on_held_out_reference_rows(stat_small, stat_large):
    for label, det in (("1x fixture", stat_small), ("10x fixture", stat_large)):
        print(f"\n[l] stat {label}: threshold {det.threshold_:.2f}, held-out FPR {det.holdout_fpr_:.5f}, weights {det.weights_.round(2).to_dict()}")
        assert det.holdout_fpr_ <= config.TARGET_ROW_FPR
    # In a small reference rare templates look novel, so the calibrated bar is higher ...
    assert stat_small.threshold_ > stat_large.threshold_
    # ... while with every template recurring (as at full scale) no held-out row is novel and
    # any never-seen token flags: the stat recall tests below run in this regime.
    assert stat_large.threshold_ == 0.0
    # Shapes that template drift rarely produces weigh more than a new word.
    weights = stat_small.weights_
    assert weights["@ token"] > weights["capitalised word"] and weights["digit run"] > weights["capitalised word"]


def test_stat_threshold_absorbs_template_drift_but_not_contact_data():
    """Calibration when drift is common (the regime the fixtures above do not reach): 2 % of the
    held-out rows carry one new template word, so the bar sits at one novel capitalised word.
    A second new word stays under it; a phone number, an e-mail or a full name crosses it."""
    template = "The Department inspected the property and found no violations."
    texts = [template] * 4000
    holdout_start = int(len(texts) * (1 - HOLDOUT_SHARE))
    for pos in range(holdout_start, len(texts), 50):  # 20 of the 1,000 held-out rows
        texts[pos] = f"{template} Courtyard inspected."
    det = StatPiiTemplateNovelty().fit(pd.DataFrame({"resolution_description": texts}))
    word_weight = det.weights_["capitalised word"]
    assert det.threshold_ == pytest.approx(word_weight)
    assert det.weights_["digit run"] > word_weight and det.weights_["@ token"] > word_weight
    assert det.holdout_fpr_ <= config.TARGET_ROW_FPR

    batch = pd.DataFrame({"resolution_description": [
        template,
        f"{template} Basement inspected.",  # template drift: one new word
        f"{template} call 917-555-0142.",
        f"{template} sent to lucia.moreno@gmail.com",
        f"{template} per Lucia Moreno",
        f"{template} per Lucia",  # a lone first name is indistinguishable from drift
    ]})
    assert det.detect(batch).row_flags.tolist() == [False, False, True, True, True, False]


@pytest.mark.parametrize("variant", VARIANTS)
def test_stat_detects_injected_personal_data(stat_large, stat_small, batch_feed, variant):
    bad, labels = inject_pii(batch_feed, variant)
    res = stat_large.detect(bad)
    recall, fpr = rates(res.row_flags, labels)
    small_recall, small_fpr = rates(stat_small.detect(bad).row_flags, labels)
    print(f"\n[l] stat {variant}: recall {recall:.3f}, FPR {fpr:.4f} (fit on 1x fixture: recall {small_recall:.3f}, FPR {small_fpr:.4f})")
    assert recall >= 0.9
    assert res.row_scores[labels].mean() > res.row_scores[~labels].mean()


def test_stat_false_alarms_on_the_untouched_batch(stat_large, stat_small, batch_feed):
    large, small = stat_large.detect(batch_feed), stat_small.detect(batch_feed)
    print(f"\n[l] stat untouched October batch: FPR {large.row_flags.mean():.4f} (10x fixture), {small.row_flags.mean():.4f} (1x fixture)")
    # October words missing from the 15k-row fixture (rare descriptors) are the false alarms.
    assert large.row_flags.mean() < 0.03


def test_stat_alert_and_reasons_name_the_novel_tokens(stat_large, batch_feed):
    bad, labels = inject_pii(batch_feed, "email")
    res = stat_large.detect(bad)
    alert = res.alerts[0]
    assert alert.check_id == "S-l1" and "resolution_description" in alert.columns
    assert alert.observed and alert.expected and alert.score > stat_large.threshold_
    assert set(res.row_reasons) == set(np.flatnonzero(res.row_flags))
    for pos in np.flatnonzero(labels & res.row_flags):
        assert any(email in res.row_reasons[pos] for email in EMAILS)


# --------------------------------------------------------------------------------------
# llm_pii_review (FakeClient, never the real API)
# --------------------------------------------------------------------------------------
def test_llm_sends_only_free_text_columns_with_batch_positions_as_ids(fake_llm, batch_feed):
    client = fake_llm(pii_responder)
    res = LlmPiiReview().detect(batch_feed, review_rows=np.array([3, 10, 4999, 7, 10]))
    records = [rec for call in client.calls for rec in rows_in_prompt(call["prompt"])]
    assert [rec["id"] for rec in records] == [3, 7, 10, 4999]
    assert all(set(rec) == {"id", *config.FREE_TEXT_COLUMNS} for rec in records)
    expected = batch_feed["resolution_description"].iloc[7]
    assert records[1]["resolution_description"] == (expected if isinstance(expected, str) else None)
    prompt = client.calls[0]["prompt"]
    assert "incident_address" not in prompt and "NOT personal data" in prompt and "311" in prompt
    assert {call["tag"] for call in client.calls} == {"l/llm_pii_review"}
    assert np.array_equal(np.flatnonzero(res.reviewed), [3, 7, 10, 4999])


def test_llm_maps_verdicts_back_to_batch_positions(fake_llm, batch_feed):
    bad, labels = inject_pii(batch_feed, "mixed")
    injected = np.flatnonzero(labels)
    review = np.concatenate([injected, np.flatnonzero(~labels)[:60]])
    client = fake_llm(pii_responder)
    res = LlmPiiReview().detect(bad, review_rows=review)
    assert res.reviewed.sum() == len(review)
    assert np.array_equal(np.flatnonzero(res.row_flags), injected)
    for pos in injected:
        assert res.row_evidence[pos]["observed"] in bad["resolution_description"].iloc[pos]
        assert res.row_reasons[pos].startswith("contains")
    alert = res.alerts[0]
    assert res.batch_flag and alert.check_id == "L-l1" and not alert.deterministic and alert.n_rows == len(injected)
    assert res.usage.llm_calls == len(client.calls) == -(-len(review) // config.LLM_ROWS_PER_CALL)


def test_llm_reviews_every_row_when_no_sample_is_given(fake_llm, batch_feed):
    client = fake_llm(pii_responder)
    res = LlmPiiReview().detect(batch_feed.iloc[:30])
    assert res.reviewed.all() and not res.row_flags.any() and not res.batch_flag
    assert len(client.calls) == 2


def test_llm_silence_is_an_abstention_never_a_clean_verdict(fake_llm, batch_feed):
    fake_llm()  # default FakeClient answer: an empty list of verdicts
    res = LlmPiiReview().detect(batch_feed, review_rows=np.arange(10))
    assert res.error is None and not res.reviewed.any() and not res.batch_flag
    assert res.abstained == dict.fromkeys(range(10), llm.ABSTAIN_NO_VERDICT)


def test_llm_rows_without_a_verdict_abstain_and_are_not_reviewed(fake_llm, batch_feed):
    bad, labels = inject_pii(batch_feed, "email")
    first_injected = int(np.flatnonzero(labels)[0])

    def skip_one_row(prompt, schema):
        answer = pii_responder(prompt, schema)
        answer["rows"] = [v for v in answer["rows"] if v["id"] != first_injected]
        return answer

    fake_llm(skip_one_row)
    res = LlmPiiReview().detect(bad, review_rows=np.flatnonzero(labels))
    assert not res.reviewed[first_injected] and not res.row_flags[first_injected]
    assert res.abstained == {first_injected: llm.ABSTAIN_NO_VERDICT} and res.reviewed.sum() == labels.sum() - 1
    assert res.row_flags.sum() == labels.sum() - 1 and "1 rows got no verdict" in res.alerts[0].message


def test_llm_unavailable_returns_an_error_result(fake_llm, batch_feed):
    fake_llm(unavailable=True)
    res = LlmPiiReview().detect(batch_feed, review_rows=np.arange(10))
    assert res.error.startswith("llm_unavailable:")
    assert not res.batch_flag and not res.row_flags.any() and not res.reviewed.any()


# --------------------------------------------------------------------------------------
# robustness and speed
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", CORRUPTIONS)
def test_detectors_never_raise_on_corrupted_batches(kind, batch_feed, stat_large, fake_llm):
    fake_llm(pii_responder)
    bad = corrupt(batch_feed, kind)
    sample = np.arange(0, len(bad), 50)
    for det in (RulePiiRegex(), stat_large, LlmPiiReview()):
        res = det.detect(bad, review_rows=sample)  # called directly: any exception fails the test
        assert res.error is None
        assert res.row_flags.dtype == bool and res.row_flags.shape == (len(bad),)


def test_every_free_text_column_is_checked(batch_feed, stat_large):
    bad = batch_feed.copy()
    bad.loc[11, "descriptor_2"] = "Contact Olga Petrova at olga.petrova@gmail.com"
    bad.loc[12, "descriptor"] = "Callback (347) 555-0188"
    for det in (RulePiiRegex(), stat_large):
        res = det.detect(bad)
        assert res.row_flags[[11, 12]].all(), det.name
        assert "descriptor_2" in res.row_reasons[11] and "descriptor" in res.row_reasons[12]


def test_every_copy_of_a_duplicated_header_is_checked(batch_feed, stat_large):
    """Regression: batch["descriptor"] returned a DataFrame and both detectors raised."""
    bad = corrupt(batch_feed, "duplicated_header")
    second_copy = [i for i, name in enumerate(bad.columns) if name == "descriptor"][1]
    bad.iloc[9, second_copy] = "Callback (347) 555-0188"
    for det in (RulePiiRegex(), stat_large):
        res = det.detect(bad)
        assert res.row_flags[9], det.name
        assert "descriptor" in res.row_reasons[9] and "0188" in res.row_reasons[9]


def test_alerts_do_not_repeat_the_personal_data_they_found(batch_feed, stat_large, fake_llm):
    """Regression: alert.observed quoted names, phones and e-mails verbatim, and the harness
    writes alerts to results/eval_records.jsonl (also for real, unsanitised batches). Alerts
    show masked values; the full value stays in row_reasons / row_evidence."""
    assert _mask("Lucia Moreno") == "L**** M*****" and _mask("(917) 555-0142") == "(9**) 5**-0***"
    assert _mask(None) is None
    bad, labels = inject_pii(batch_feed, "mixed")
    injected = np.flatnonzero(labels)
    personal = [s.lower() for s in EMAILS + [part for name in NAMES for part in name.split()]]
    personal += [re.sub(r"\D", "", phone)[-10:] for phone in PHONES] + [phone[-8:] for phone in PHONES]
    fake_llm(pii_responder)
    for det in (RulePiiRegex(), stat_large, LlmPiiReview()):
        res = det.detect(bad, review_rows=injected)
        alerts = json.dumps([alert.to_dict() for alert in res.alerts], ensure_ascii=False).lower()
        assert res.alerts and res.alerts[0].observed, det.name
        assert [s for s in personal if s in alerts] == [], det.name
        details = " ".join([*res.row_reasons.values(), *(str(e.get("observed")) for e in res.row_evidence.values())])
        assert any(s in details.lower() for s in personal), det.name


def test_long_cells_are_scanned_in_linear_time(batch_feed, stat_large):
    """Regression: EMAIL_RE / AT_TOKEN_RE restarted at every character of a long token
    (quadratic: 3.5 s / 9 s for one 32k-character cell, minutes for 100k)."""
    blob = "x" * 100_000
    for text in (blob, f"{blob} lucia.moreno@gmail.com", f"a@{blob}"):
        t0 = time.perf_counter()
        _pii_matches(text)
        _tokens(text)
        assert time.perf_counter() - t0 < 1.0
    bad = batch_feed.copy()
    bad.loc[3, "resolution_description"] = f"{blob} lucia.moreno@gmail.com"
    for det in (RulePiiRegex(), stat_large):
        t0 = time.perf_counter()
        res = det.detect(bad)
        assert time.perf_counter() - t0 < 2.0, det.name
        assert res.row_flags[3], det.name


def test_personal_data_is_still_found_next_to_other_corruptions(batch_feed, stat_large):
    clean, labels = inject_pii(batch_feed, "mixed")
    for kind in ("reformatted_dates", "numbers_as_strings", "non_positional_index", "renamed_text_column"):
        bad = corrupt(clean, kind)
        for det in (RulePiiRegex(), stat_large):
            assert det.detect(bad).row_flags[labels].mean() >= 0.95, (kind, det.name)


def test_speed(reference_feed, batch_feed):
    big = pd.concat([reference_feed] * 10, ignore_index=True)
    t0 = time.perf_counter()
    stat = StatPiiTemplateNovelty().fit(big)
    fit_s = time.perf_counter() - t0
    fit_at_full_scale = fit_s * 2_100_000 / len(big)
    detect_s = {}
    for det in (RulePiiRegex(), stat):
        t0 = time.perf_counter()
        det.detect(batch_feed)
        detect_s[det.name] = time.perf_counter() - t0
    print(f"\n[l] stat fit on {len(big):,} rows: {fit_s:.2f}s (~{fit_at_full_scale:.0f}s at 2.1M rows); "
          + ", ".join(f"{name} detect on 5,000 rows: {s:.3f}s" for name, s in detect_s.items()))
    assert fit_at_full_scale < 60
    assert all(s < 1.0 for s in detect_s.values())


# ------------------------------------------------------------------------------------------
# Regression: three false-positive classes found in clean batches, fixed on Jan-Aug evidence
# (C1, C3, P1). Genuine personal data must still be found; C2' (Title Case labels) was not adopted.
# ------------------------------------------------------------------------------------------
from project_b.detectors import pii as pii_module  # noqa: E402


@pytest.mark.parametrize("text", [
    "Food Contact Equipment Dirty",  # a DOHMH category label: "Contact" inside a Title Case label
    "Lead Contact Surface Peeling",
])
def test_c1_a_cue_word_inside_a_title_case_label_is_not_a_cue(text):
    assert _pii_matches(text) == []


@pytest.mark.parametrize("text, expected", [
    ("Contact Olga Petrova at 917-555-0142 for access.", "Contact Olga Petrova"),  # cue starts the text
    ("Spoke with tenant Maria Lopez.", "tenant Maria Lopez"),  # after a lower-case word
    ("No access at 10:00. Complainant Maria Lopez was not home.", "Complainant Maria Lopez"),  # after a full stop
    ("Inspector met resident Kwame Mensah on site.", "resident Kwame Mensah"),
    ("Caller Wei Chen reported the condition again.", "Caller Wei Chen"),
])
def test_c1_genuine_cue_and_name_is_still_found(text, expected):
    assert ("R-l3", expected) in _pii_matches(text)


def test_c1_setting_restores_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(pii_module, "CUE_INSIDE_LABEL_IS_NOT_A_CUE", False)
    assert [check for check, _ in _pii_matches("Food Contact Equipment Dirty")] == ["R-l3"]


def test_p1_the_documented_dot_number_is_public_but_its_neighbours_are_not():
    referral = ("You should contact the U.S. Department of Transportation Office of Aviation Consumer "
                "Protection. Call (202) 366-2220 or visit transportation.gov.")
    assert _pii_matches(referral) == []
    assert [check for check, _ in _pii_matches(referral.replace("2220", "2221"))] == ["R-l2"]
    assert "2023662220" in pii_module.PUBLIC_PHONE_NUMBERS


def _with_resolution(batch: pd.DataFrame, text: str, row: int = 7) -> pd.DataFrame:
    out = batch.copy()
    out.loc[row, "resolution_description"] = text
    return out


@pytest.mark.parametrize("number", ["888-692-2368", "(800) 697-1220", "(202) 366-2220"])
def test_c3_novelty_ignores_numbers_the_explicit_lists_call_public(stat_large, batch_feed, number):
    """Toll-free (TOLL_FREE_AREA_CODES) and allow-listed (PUBLIC_PHONE_NUMBERS) numbers in a new text."""
    flags = stat_large.detect(_with_resolution(batch_feed, f"please call {number} for help")).row_flags
    assert not flags[7]


def test_c3_novelty_still_flags_a_private_number_in_a_new_text(stat_large, batch_feed):
    flags = stat_large.detect(_with_resolution(batch_feed, "please call 917-555-0142 for help")).row_flags
    assert flags[7]


def test_c3_setting_restores_the_old_behaviour(stat_large, batch_feed, monkeypatch):
    monkeypatch.setattr(pii_module, "NOVELTY_SKIPS_PUBLIC_NUMBERS", False)
    assert stat_large.detect(_with_resolution(batch_feed, "please call 888-692-2368 for help")).row_flags[7]


@pytest.mark.parametrize("text", [
    "Per Anita Desai, condition corrected.",  # a bare name: no cue, no number
    "Keys were left with Tyrone Okafor.",
])
def test_free_text_resolutions_are_still_checked_for_bare_names(stat_large, batch_feed, text):
    """C2' was not adopted: capitalised never-seen words in a resolution still flag the row."""
    res = stat_large.detect(_with_resolution(batch_feed, text))
    assert res.row_flags[7] and "resolution_description" in res.row_reasons[7]
