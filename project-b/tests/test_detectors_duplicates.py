"""Tests for project_b/detectors/duplicates.py: exact duplicates (e) and near-duplicates (f).

Corruptions are built by hand below (blue team: project_b/inject.py is not used), following the
problem descriptions of DESIGN.md section 3. batch_feed is real, UNsanitised data, so measured
rates are printed (run `pytest -s`) and asserted loosely. Every test talks to a FakeClient: no
test can reach the real Gemini API, even when a key is configured in .env.
"""
from __future__ import annotations

import re
import time

import numpy as np
import pandas as pd
import pytest

from project_b import config, llm
from project_b.data import clean
from project_b.data.load import to_feed
from project_b.detectors import duplicates as dup
from tests.fakes import FakeClient, rows_in_prompt

FMT = config.SOURCE_DATETIME_FORMAT
EDITS = ["time_only", "street", "avenue", "double_space", "typo", "house_number_typo", "descriptor_case"]
DUPLICATE_ISSUES = ["dup_unique_key", "exact_duplicate_row", "near_duplicate_candidate"]


def report(label: str, **numbers) -> None:
    """Print a measured rate so `pytest -s` shows it (asserts stay loose on real data)."""
    values = ", ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}" for k, v in numbers.items())
    print(f"\n[duplicates] {label}: {values}")


# ----------------------------------------------------------------------------------------------
# hand-made corruptions
# ----------------------------------------------------------------------------------------------
def with_exact_copies(batch: pd.DataFrame, k: int, rng: np.random.Generator) -> tuple[pd.DataFrame, np.ndarray]:
    """k random rows copied verbatim to random LATER positions; labels mark the copies."""
    sources = rng.choice(len(batch) - 1, size=k, replace=False)
    slots = rng.uniform(sources + 0.5, len(batch))  # somewhere after the original
    order = np.argsort(np.concatenate([np.arange(len(batch)), slots]), kind="stable")
    rows = np.concatenate([np.arange(len(batch)), sources])[order]
    return batch.iloc[rows].reset_index(drop=True), order >= len(batch)


def _typo(text: str, rng: np.random.Generator) -> str:
    """Replace one letter by another, as the injector's typo does (a near-duplicate keeps its
    building number; a changed number is house_number_typo, tested on its own)."""
    i = rng.choice([j for j, ch in enumerate(text) if ch.isalpha()])
    return text[:i] + rng.choice([c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c != text[i].upper()]) + text[i + 1 :]


def _house_number_typo(text: str, rng: np.random.Generator) -> str:
    """Replace one digit of the leading house number ("139-10 97 AVENUE" -> "139-16 97 AVENUE")."""
    number = re.match(r"\d[\d-]*", text).group(0)
    i = rng.choice([j for j, ch in enumerate(number) if ch.isdigit()])
    return text[:i] + rng.choice([c for c in "0123456789" if c != text[i]]) + text[i + 1 :]


def _eligible(batch: pd.DataFrame, edit: str) -> np.ndarray:
    address, descriptor = batch["incident_address"], batch["descriptor"]
    ok = address.notna()
    if edit == "street":
        ok &= address.str.contains(r"\bSTREET\b", regex=True, na=False)
    if edit == "avenue":
        ok &= address.str.contains(r"\bAVENUE\b", regex=True, na=False)
    if edit == "descriptor_case":
        ok &= descriptor.notna() & (descriptor.str.lower() != descriptor)
    if edit == "typo":
        ok &= address.str.contains(r"[A-Za-z]", regex=True, na=False)
    if edit == "house_number_typo":
        ok &= address.str.match(r"\d[\d-]* ", na=False)
    return ok.to_numpy()


def _edit(copies: pd.DataFrame, edit: str, rng: np.random.Generator) -> pd.DataFrame:
    address = copies["incident_address"]
    if edit == "street":
        return copies.assign(incident_address=address.str.replace(r"\bSTREET\b", "ST", n=1, regex=True))
    if edit == "avenue":
        return copies.assign(incident_address=address.str.replace(r"\bAVENUE\b", "AVE", n=1, regex=True))
    if edit == "double_space":
        return copies.assign(incident_address=address.str.replace(" ", "  ", n=1, regex=False))
    if edit == "typo":
        return copies.assign(incident_address=[_typo(a, rng) for a in address])
    if edit == "house_number_typo":
        return copies.assign(incident_address=[_house_number_typo(a, rng) for a in address])
    if edit == "descriptor_case":
        return copies.assign(descriptor=copies["descriptor"].str.lower())
    return copies  # time_only


def _later(dates: pd.Series, seconds) -> pd.Series:
    return (pd.to_datetime(dates, format=FMT) + pd.to_timedelta(seconds, unit="s")).dt.strftime("%Y-%m-%dT%H:%M:%S.000")


def _in_time_order(batch: pd.DataFrame, new_rows: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """batch plus new_rows, sorted by created_date (stable); the mask marks the new rows."""
    combined = pd.concat([batch, new_rows], ignore_index=True).sort_values("created_date", kind="stable")
    return combined.reset_index(drop=True), combined.index.to_numpy() >= len(batch)


def with_near_copies(batch: pd.DataFrame, k: int, rng: np.random.Generator, edit: str = "time_only") -> tuple[pd.DataFrame, np.ndarray]:
    """k rows re-entered as new requests: new unique_key, created/closed 1-300 s later, an optional
    small text edit, inserted right after the original in time order; labels mark the copies."""
    sources = rng.choice(np.flatnonzero(_eligible(batch, edit)), size=k, replace=False)
    copies = batch.iloc[sources].copy()
    copies["unique_key"] = batch["unique_key"].max() + 1 + np.arange(k)
    shift = rng.integers(1, 301, size=k)
    for col in ("created_date", "closed_date"):
        copies[col] = _later(copies[col], shift)
    return _in_time_order(batch, _edit(copies, edit, rng))


def with_reentry(batch: pd.DataFrame, source: int, **changes) -> tuple[pd.DataFrame, int]:
    """batch plus row `source` entered again 120 s later under a new unique_key, with `changes`
    applied; returns the new batch and the position of the re-entry."""
    row = batch.iloc[[source]].assign(unique_key=batch["unique_key"].max() + 1, **changes)
    row["created_date"] = _later(row["created_date"], 120)
    combined, is_new = _in_time_order(batch, row)
    return combined, int(np.flatnonzero(is_new)[0])


def sanitised(feed: pd.DataFrame) -> pd.DataFrame:
    """The rows clean.sanitize keeps for problems e and f, as in the evaluation's clean pool (its
    other issues are frequency-based and need the full year, so they are not applied here)."""
    issues = clean.issue_flags(feed)[DUPLICATE_ISSUES].any(axis=1).to_numpy()
    return feed[~issues].reset_index(drop=True)


def recall(flags: np.ndarray, labels: np.ndarray) -> float:
    return float(flags[labels].mean())


# ----------------------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def fake_llm():
    """Every test talks to a FakeClient - an unavailable one unless the test installs another
    with `fake_llm(responder)` - and the real client is restored afterwards."""

    def install(responder=None, unavailable: bool = False) -> FakeClient:
        client = FakeClient(responder, unavailable=unavailable)
        llm.set_client(client)
        return client

    install(unavailable=True)
    yield install
    llm.set_client(None)


@pytest.fixture(scope="module")
def clean_batch(batch_raw) -> pd.DataFrame:
    """batch_feed without its pre-existing duplicates and near-duplicates (~4 % of its rows)."""
    return sanitised(to_feed(batch_raw))


@pytest.fixture(scope="module")
def dense_stat(clean_batch) -> dup.StatNearDuplicateSimilarity:
    """Calibrated on the first half of the clean batch. The reference fixture is a 1-in-140 random
    sample (median 14 min between rows), so the 600 s candidate pairs that set the threshold on the
    real, contiguous reference barely exist in it; the batch fixture is the only contiguous data in
    tests/data. Tests using this detector evaluate on the second half only."""
    return dup.StatNearDuplicateSimilarity().fit(clean_batch.iloc[: len(clean_batch) // 2])


@pytest.fixture(scope="module")
def second_half(clean_batch) -> pd.DataFrame:
    return clean_batch.iloc[len(clean_batch) // 2 :].reset_index(drop=True)


@pytest.fixture(scope="module")
def big_reference(reference_raw) -> pd.DataFrame:
    """150k rows (the reference fixture 10x) stand in for the 2.1 M-row reference in timing tests."""
    return pd.concat([to_feed(reference_raw)] * 10, ignore_index=True)


# ----------------------------------------------------------------------------------------------
# e - exact duplicates: rule
# ----------------------------------------------------------------------------------------------
def test_rule_exact_flags_later_copy_only(batch_feed):
    corrupted, labels = with_exact_copies(batch_feed, 100, np.random.default_rng(1))
    res = dup.RuleExactDuplicateRows().detect(corrupted)
    assert np.array_equal(res.row_flags, labels)  # every copy, never the original
    alert = res.alerts[0]
    assert (alert.check_id, alert.n_rows) == ("R-e1", 100) and len(alert.rows) == dup.EXAMPLE_ROWS
    assert alert.observed and alert.expected and alert.columns == list(corrupted.columns)
    first = int(np.flatnonzero(labels)[0])
    original = int(res.row_reasons[first].split("row ")[1].split(":")[0])
    assert original < first and corrupted.iloc[original].equals(corrupted.iloc[first])


def test_rule_exact_reports_reused_unique_key_separately(batch_feed):
    reused = batch_feed.copy()
    reused.loc[4000, "unique_key"] = reused.loc[10, "unique_key"]
    res = dup.RuleExactDuplicateRows().detect(reused)
    assert np.flatnonzero(res.row_flags).tolist() == [4000]
    assert [a.check_id for a in res.alerts] == ["R-e2"]
    assert "already used by row 10" in res.row_reasons[4000] and "created_date" in res.row_reasons[4000]


def test_rule_exact_quiet_on_untouched_and_near_copies(batch_feed):
    assert dup.RuleExactDuplicateRows().detect(batch_feed).row_flags.sum() == 0
    near, _ = with_near_copies(batch_feed, 100, np.random.default_rng(2))
    assert dup.RuleExactDuplicateRows().detect(near).row_flags.sum() == 0  # new unique_key + time: problem f


def test_rule_exact_compares_unhashable_values_by_text(batch_feed):
    """Regression: a tuple holding a list passes isinstance(x, Hashable) but cannot be hashed, and
    the rule raised TypeError on it."""
    rows = batch_feed.assign(descriptor_2=[(1, [i % 7]) for i in range(len(batch_feed))])
    corrupted, labels = with_exact_copies(rows, 20, np.random.default_rng(8))
    assert np.array_equal(dup.RuleExactDuplicateRows().detect(corrupted).row_flags, labels)


@pytest.mark.parametrize("cls", [dup.RuleExactDuplicateRows, dup.StatBloomDuplicate])
def test_exact_detectors_flag_nothing_in_a_batch_without_columns(cls, batch_feed):
    """Regression: with no columns every row was 'identical' to row 0, so 4,999 of 5,000 rows were
    reported as exact copies - a finding invented from a batch that holds no values at all."""
    res = cls().detect(batch_feed.iloc[:, :0])
    assert res.row_flags.shape == (len(batch_feed),) and not res.row_flags.any() and not res.alerts


# ----------------------------------------------------------------------------------------------
# e - exact duplicates: Bloom filter
# ----------------------------------------------------------------------------------------------
def test_bloom_filter_sizing_and_no_false_negatives():
    bloom = dup.BloomFilter(5_000, 1e-4)
    assert (bloom.n_bits, bloom.n_hashes, bloom.packed_bits.nbytes) == (95_851, 13, 11_982)
    keys = [f"row {i}" for i in range(5_000)]
    first = bloom.add_all(keys)
    assert bloom.add_all(keys).all()  # kept across calls: this is how it dedups across batches
    report("Bloom in-stream false positives on 5,000 distinct keys", flagged=int(first.sum()))
    with pytest.raises(ValueError):
        dup.BloomFilter(5_000, 1e-6)  # would need 20 hashes; one blake2b digest gives 16


def test_bloom_false_positive_rate_is_measurable():
    flagged = sum(dup.BloomFilter(5_000, 1e-4).add_all([f"{b}-{i}" for i in range(5_000)]).sum() for b in range(20))
    report("Bloom false-positive rate, 20 x 5,000 distinct keys", rate=flagged / 100_000)
    assert flagged / 100_000 <= 1e-4  # design 1e-4 is the rate when FULL; in-stream it is ~1e-5


def test_bloom_detects_exact_copies(reference_feed, batch_feed):
    det = dup.StatBloomDuplicate().fit(reference_feed)
    assert det.threshold_ == dup.BLOOM_FALSE_POSITIVE_RATE and det.reference_fpr_ <= config.TARGET_ROW_FPR
    corrupted, labels = with_exact_copies(batch_feed, 100, np.random.default_rng(3))
    res = det.detect(corrupted)
    assert res.row_flags[labels].all()  # no false negatives by construction
    clean_flags = det.detect(batch_feed).row_flags
    report("stat_bloom_duplicate", recall=recall(res.row_flags, labels), fp_on_injected=int(res.row_flags[~labels].sum()),
           fp_untouched=int(clean_flags.sum()), reference_fpr=det.reference_fpr_)
    assert clean_flags.sum() <= 1 and res.alerts[0].check_id == "S-e1"


# ----------------------------------------------------------------------------------------------
# f - near-duplicates: rule
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("edit", EDITS)
def test_rule_near_duplicate_recall_by_edit(clean_batch, edit):
    corrupted, labels = with_near_copies(clean_batch, 100, np.random.default_rng(4), edit)
    res = dup.RuleNearDuplicateKey().detect(corrupted)
    report(f"rule_near_duplicate_key recall, {edit}", recall=recall(res.row_flags, labels))
    if edit in ("typo", "house_number_typo"):
        assert recall(res.row_flags, labels) < 0.1  # a typo changes the key: the rule's blind spot
    else:
        assert recall(res.row_flags, labels) == 1.0  # case, spaces and STREET/AVENUE are normalised


def test_rule_near_duplicate_false_alarms_and_explanation(batch_feed, clean_batch):
    untouched = dup.RuleNearDuplicateKey().detect(batch_feed)
    clean_rows = dup.RuleNearDuplicateKey().detect(clean_batch)
    report("rule_near_duplicate_key flag rate", untouched=float(untouched.row_flags.mean()),
           sanitised=float(clean_rows.row_flags.mean()))
    assert untouched.row_flags.mean() < 0.06  # real near-duplicates (~4 %), what the sanitiser removes
    assert clean_rows.row_flags.mean() < 0.005
    alert = untouched.alerts[0]
    assert alert.check_id == "R-f1" and alert.observed and alert.expected and alert.rows
    assert all("created" in reason and "unique_key" in reason for reason in untouched.row_reasons.values())


# ----------------------------------------------------------------------------------------------
# f - near-duplicates: statistical similarity
# ----------------------------------------------------------------------------------------------
def test_stat_similarity_fit_on_reference_fixture(reference_feed, clean_batch):
    det = dup.StatNearDuplicateSimilarity()
    t0 = time.perf_counter()
    det.fit(reference_feed)
    fit_s = time.perf_counter() - t0
    assert 0 < det.threshold_ <= 1 and det.reference_fpr_ <= config.TARGET_ROW_FPR
    report("stat_near_duplicate_similarity fitted on the SPARSE reference fixture", threshold=det.threshold_,
           fit_s=fit_s, flag_rate_on_clean_batch=float(det.detect(clean_batch).row_flags.mean()))


def test_stat_similarity_calibration(dense_stat, second_half, batch_feed):
    flags = dense_stat.detect(second_half).row_flags
    untouched = dense_stat.detect(batch_feed).row_flags
    real = dup.RuleNearDuplicateKey().detect(batch_feed).row_flags
    report("stat_near_duplicate_similarity (dense calibration)", threshold=dense_stat.threshold_,
           reference_fpr=dense_stat.reference_fpr_, false_alarm_rate=float(flags.mean()),
           untouched_flag_rate=float(untouched.mean()), untouched_also_flagged_by_rule=float(real[untouched].mean()))
    assert dense_stat.reference_fpr_ <= config.TARGET_ROW_FPR
    assert flags.mean() < 0.01


@pytest.mark.parametrize("edit", EDITS)
def test_stat_similarity_recall_by_edit(dense_stat, second_half, edit):
    corrupted, labels = with_near_copies(second_half, 50, np.random.default_rng(5), edit)
    stat = dense_stat.detect(corrupted)
    rule = dup.RuleNearDuplicateKey().detect(corrupted)
    report(f"stat_near_duplicate_similarity recall, {edit}", stat=recall(stat.row_flags, labels), rule=recall(rule.row_flags, labels))
    if edit == "typo":  # the copies the exact key misses
        assert recall(stat.row_flags, labels) >= 0.8 > 0.1 > recall(rule.row_flags, labels)
    elif edit == "house_number_typo":
        # A different building number is a different address (DIFFERENT_BUILDING_IS_NOT_A_COPY): the
        # price of keeping distinct reports along one block-geocoded street apart. Documented trade-off.
        assert recall(stat.row_flags, labels) == 0.0 and recall(rule.row_flags, labels) < 0.1
        return
    else:
        assert recall(stat.row_flags, labels) >= 0.95
    assert stat.row_scores.shape == (len(corrupted),)
    assert all("threshold" in stat.row_reasons[p] for p in np.flatnonzero(stat.row_flags))
    assert stat.alerts[0].check_id == "S-f1" and stat.alerts[0].observed and stat.alerts[0].expected


def test_stat_similarity_treats_another_house_number_as_another_building(dense_stat, second_half, monkeypatch):
    """Same street and descriptor, 120 s later, house number changed. At the same point this is
    either a re-entry with a mistyped number or the next building on a street geocoded per block,
    and the features cannot tell which; DIFFERENT_BUILDING_IS_NOT_A_COPY keeps them apart (it was
    flagged before). ~40 m away it is the next building either way and was never flagged."""
    address = second_half["incident_address"]
    candidate = address.str.match(r"[1-8]\d* \w", na=False) & (address.str.len() >= 12) & second_half["latitude"].notna()
    source = int(np.flatnonzero(candidate)[0])
    text = address[source]
    typo = str(int(text[0]) + 1) + text[1:]  # first digit of the house number mistyped
    copy_batch, copy = with_reentry(second_half, source, incident_address=typo)
    next_batch, neighbour = with_reentry(second_half, source, incident_address=typo,
                                         latitude=second_half["latitude"][source] + 0.00036)  # ~40 m north
    copy_score = dense_stat.detect(copy_batch).row_scores[copy]
    neighbour_score = dense_stat.detect(next_batch).row_scores[neighbour]
    report("house-number typo vs next building", address=text, copy=float(copy_score),
           neighbour=float(neighbour_score), threshold=dense_stat.threshold_)
    assert copy_score == neighbour_score == 0.0
    monkeypatch.setattr(dup, "DIFFERENT_BUILDING_IS_NOT_A_COPY", False)  # the behaviour before the change
    assert dense_stat.detect(next_batch).row_scores[neighbour] <= dense_stat.threshold_ < dense_stat.detect(copy_batch).row_scores[copy]


def test_stat_typo_recall_rests_on_unchanged_coordinates(dense_stat, second_half):
    """Documents a limitation (see the module docstring): the injected copies keep the original's
    coordinates, and that is what lifts a typo copy over the threshold. Moved 10 m, as a re-entry
    geocoded from a mistyped house number would be, most of them are missed."""
    corrupted, labels = with_near_copies(second_half, 50, np.random.default_rng(5), "typo")
    moved = corrupted.copy()
    moved.loc[labels, "latitude"] += 10 / dup.M_PER_DEG_LAT
    kept, shifted = (recall(dense_stat.detect(b).row_flags, labels) for b in (corrupted, moved))
    report("stat typo recall, copy at the same point vs 10 m away", same_point=kept, moved_10m=shifted)
    assert shifted < 0.5 < kept


def test_near_duplicate_detectors_ignore_exact_copies(dense_stat, second_half):
    corrupted, labels = with_exact_copies(second_half, 50, np.random.default_rng(6))
    for det in (dup.RuleNearDuplicateKey(), dense_stat):
        assert not det.detect(corrupted).row_flags[labels].any(), det.name  # same unique_key: problem e


def test_stat_similarity_scores_equal_patterns_equally(second_half):
    """Regression: 'same building, other descriptor' summed to 0.7999999999999999 and 'same address
    and descriptor, other location type, no coordinates' to 0.8, so a threshold calibrated on the
    first pattern passed the second by rounding noise alone."""
    source = second_half[second_half["incident_address"].notna() & second_half["latitude"].notna()].iloc[[0]]
    base = source.assign(unique_key=10**9)

    def entered(seconds_later: int, **changes) -> pd.DataFrame:
        created = _later(base["created_date"], seconds_later)
        return base.assign(unique_key=base["unique_key"] + seconds_later, created_date=created, **changes)

    tiny = pd.concat([
        entered(0), entered(60, descriptor="Another Descriptor"),  # pattern A, rows 0-1
        entered(2000, latitude=np.nan, longitude=np.nan),  # pattern B, rows 2-3 (> 600 s after A)
        entered(2060, latitude=np.nan, longitude=np.nan, location_type="Another Location Type"),
    ], ignore_index=True)
    det = dup.StatNearDuplicateSimilarity()
    det.threshold_ = 0.8
    res = det.detect(tiny)
    assert res.row_scores[1] == res.row_scores[3] == 0.8 and not res.row_flags.any()


@pytest.mark.parametrize("change", ["dropped", "renamed"])
def test_missing_descriptor_column_is_not_agreement(dense_stat, second_half, change):
    """Regression: without a descriptor column every pair counted as 'same descriptor', and both f
    detectors flagged 3.6 % of the sanitised half batch (different complaints at one address)."""
    if change == "dropped":
        broken = second_half.drop(columns="descriptor")
    else:
        broken = second_half.rename(columns={"descriptor": "Descriptor"})
    rule, stat = dup.RuleNearDuplicateKey().detect(broken), dense_stat.detect(broken)
    assert rule.error is None and not rule.row_flags.any() and not rule.alerts
    assert not stat.row_flags.any()
    assert (stat.row_scores <= dense_stat.detect(second_half).row_scores).all()  # unknown never adds similarity


def test_alerts_name_columns_missing_from_the_batch(dense_stat, second_half):
    """Regression: a detector that worked around a missing column did not say so in its alert."""
    corrupted, labels = with_near_copies(second_half, 50, np.random.default_rng(9))
    stat = dense_stat.detect(corrupted.drop(columns="location_type"))
    assert recall(stat.row_flags, labels) >= 0.9  # missed: copies without coordinates, which drop to 0.8
    assert "not in the batch: location_type" in stat.alerts[0].message
    rule = dup.RuleNearDuplicateKey().detect(corrupted.drop(columns="incident_address"))  # located by coordinates
    assert "not in the batch: incident_address" in rule.alerts[0].message


def test_near_duplicates_found_when_dates_arrive_parsed(dense_stat, second_half):
    """Regression: a created_date column of datetime64 was turned into text that the source format
    does not match, so both f detectors silently compared nothing."""
    corrupted, labels = with_near_copies(second_half, 50, np.random.default_rng(10))
    typed = corrupted.assign(created_date=pd.to_datetime(corrupted["created_date"], format=FMT))
    for det in (dup.RuleNearDuplicateKey(), dense_stat):
        assert recall(det.detect(typed).row_flags, labels) == 1.0, det.name


# ----------------------------------------------------------------------------------------------
# LLM window review
# ----------------------------------------------------------------------------------------------
def answer_every_row(prompt: str, schema) -> dict:
    """Fake model: a verdict for every row, none flagged."""
    return {"rows": [{"id": r["id"], "flagged": False} for r in rows_in_prompt(prompt)]}


def repeats_earlier_record(prompt: str, schema) -> dict:
    """Fake model: flag a record whose fields (except id) equal an earlier record of the prompt."""
    first_id, verdicts = {}, []
    for rec in rows_in_prompt(prompt):
        fields = tuple(sorted((k, str(v)) for k, v in rec.items() if k != "id"))
        verdicts.append({
            "id": rec["id"], "flagged": fields in first_id, "reason": "repeats an earlier row", "confidence": 0.9,
            "observed": str(rec.get("unique_key")), "expected": f"first delivered as id {first_id.get(fields)}",
        })
        first_id.setdefault(fields, rec["id"])
    return {"rows": verdicts}


@pytest.mark.parametrize("cls", [dup.LLMDuplicateReview, dup.LLMNearDuplicateReview])
def test_llm_prompt_holds_only_needed_columns_and_windows(cls, batch_feed, fake_llm):
    client = fake_llm(answer_every_row)
    windows = np.r_[10:20, 100:150]  # two windows; the second is longer than one prompt
    res = cls().detect(batch_feed, review_rows=np.r_[windows, -1, 10**6])  # positions outside the batch are dropped
    assert res.error is None and np.array_equal(np.flatnonzero(res.reviewed), windows)
    assert [len(rows_in_prompt(c["prompt"])) for c in client.calls] == [10, config.LLM_BATCH_SAMPLE_ROWS, 10]
    for call in client.calls:
        assert call["tag"] == f"{cls.problem}/{cls.name}"
        records = rows_in_prompt(call["prompt"])
        assert all(set(r) == {"id", *cls.columns} for r in records)
        assert [r["id"] for r in records] == sorted(r["id"] for r in records)  # arrival order
        not_needed = batch_feed.loc[[r["id"] for r in records], "resolution_description"].dropna()
        assert not any(text in call["prompt"] for text in not_needed if len(text) > 20)
    assert res.usage.llm_calls == 3 and res.usage.input_tokens > 0
    assert not res.batch_flag and not res.row_flags.any()


def test_llm_reviews_every_row_without_review_rows(batch_feed, fake_llm):
    client = fake_llm(answer_every_row)
    res = dup.LLMDuplicateReview().detect(batch_feed.iloc[:100])
    assert res.reviewed.all() and [len(rows_in_prompt(c["prompt"])) for c in client.calls] == [40, 40, 20]


def test_llm_duplicate_verdicts_map_to_batch_positions(batch_feed, fake_llm):
    corrupted, labels = with_exact_copies(batch_feed.iloc[:35], 5, np.random.default_rng(7))  # 40 rows = one prompt
    fake_llm(repeats_earlier_record)
    res = dup.LLMDuplicateReview().detect(corrupted, review_rows=np.arange(len(corrupted)))
    assert np.array_equal(res.row_flags, labels)
    p = int(np.flatnonzero(labels)[0])
    evidence = res.row_evidence[p]
    assert res.row_reasons[p] == "repeats an earlier row" and evidence["confidence"] == 0.9
    assert evidence["observed"] == str(corrupted["unique_key"][p])
    original = int(evidence["expected"].rsplit(" ", 1)[1])  # ids are batch positions
    assert original < p and corrupted["unique_key"][original] == corrupted["unique_key"][p]
    alert = res.alerts[0]
    assert (alert.check_id, alert.deterministic, alert.n_rows, alert.score) == ("L-e1", False, labels.sum(), 0.9)


def test_llm_near_duplicate_ignores_ids_outside_the_window(batch_feed, fake_llm):
    def flags_everything_and_more(prompt, schema):
        ids = [r["id"] for r in rows_in_prompt(prompt)]
        return {"rows": [{"id": i, "flagged": True, "reason": "x"} for i in [*ids[1:], 4999, -3]]}

    fake_llm(flags_everything_and_more)
    res = dup.LLMNearDuplicateReview().detect(batch_feed, review_rows=np.arange(200, 210))
    assert np.flatnonzero(res.row_flags).tolist() == list(range(201, 210))  # 4999 and -3 were not shown
    assert res.row_evidence[201] == {"observed": None, "expected": None, "confidence": None}
    assert res.alerts[0].check_id == "L-f1"


@pytest.mark.parametrize("cls", [dup.LLMDuplicateReview, dup.LLMNearDuplicateReview])
def test_llm_unavailable_is_an_error_not_a_verdict(cls, batch_feed, fake_llm):
    fake_llm(unavailable=True)
    res = cls().detect(batch_feed, review_rows=np.arange(30))
    assert res.error.startswith("llm_unavailable:") and not res.batch_flag and res.row_flags is None
    assert not res.reviewed.any()


def test_llm_reply_without_verdicts_abstains_on_every_row(batch_feed, fake_llm):
    fake_llm(lambda prompt, schema: {"rows": []})
    res = dup.LLMNearDuplicateReview().detect(batch_feed, review_rows=np.arange(30))
    assert res.error is None and not res.reviewed.any() and not res.row_flags.any() and res.usage.llm_calls == 1
    assert res.abstained == dict.fromkeys(range(30), llm.ABSTAIN_NO_VERDICT)


def test_llm_unreadable_reply_abstains_with_its_finish_reason(batch_feed, fake_llm):
    """Every row of a run whose reply cannot be read abstains, and the cause names Gemini's
    finishReason, e.g. a reply cut off at the output-token limit."""
    llm.set_client(FakeClient(lambda prompt, schema: "the reason went on and on and", finish_reason="MAX_TOKENS"))
    res = dup.LLMDuplicateReview().detect(batch_feed, review_rows=np.arange(30))
    assert res.error is None and not res.reviewed.any() and not res.batch_flag
    assert res.abstained == dict.fromkeys(range(30), "unreadable_reply:MAX_TOKENS")


def test_llm_window_review_scores_the_readable_run_and_abstains_on_the_other(batch_feed, fake_llm):
    """Two runs (two prompts): the first reply is unreadable, the second readable."""
    fake_llm(lambda prompt, schema: None if rows_in_prompt(prompt)[0]["id"] == 0
             else {"rows": [{"id": r["id"], "flagged": False} for r in rows_in_prompt(prompt)]})
    res = dup.LLMDuplicateReview().detect(batch_feed, review_rows=np.r_[np.arange(10), np.arange(100, 110)])
    assert np.flatnonzero(res.reviewed).tolist() == list(range(100, 110))
    assert sorted(res.abstained) == list(range(10)) and not res.row_flags.any()


# ----------------------------------------------------------------------------------------------
# robustness: detect() never raises on a corrupted batch
# ----------------------------------------------------------------------------------------------
def _reformat_dates(b):
    out = b.copy()
    late = out.index >= len(out) // 2
    out.loc[late, "created_date"] = pd.to_datetime(out.loc[late, "created_date"], format=FMT).dt.strftime("%m/%d/%Y %I:%M:%S %p")
    return out


def _strings_in_numeric(b):
    out = b.astype({"latitude": object, "unique_key": object})
    out.loc[:9, "latitude"] = "n/a"
    out.loc[:, "unique_key"] = out["unique_key"].astype(str)
    return out


CORRUPTIONS = {
    "missing unique_key": lambda b: b.drop(columns="unique_key"),
    "missing created_date": lambda b: b.drop(columns="created_date"),
    "renamed columns": lambda b: b.rename(columns={"incident_address": "address", "descriptor": "Descriptor", "agency": "Agency"}),
    "strings in numeric columns": _strings_in_numeric,
    "float unique_key with gaps": lambda b: b.assign(unique_key=b["unique_key"].astype(float).where(b.index % 7 > 0)),
    "dates as datetime64": lambda b: b.assign(created_date=pd.to_datetime(b["created_date"], format=FMT)),
    "unhashable values": lambda b: b.assign(descriptor_2=[["a", i % 3] for i in range(len(b))]),
    "tuples holding lists": lambda b: b.assign(descriptor_2=[("a", [i % 3]) for i in range(len(b))]),
    "reformatted dates": _reformat_dates,
    "inserted duplicate rows": lambda b: pd.concat([b, b.iloc[:50]]),  # duplicate index labels too
    "all-null columns": lambda b: b.assign(created_date=np.nan, incident_address=None, latitude=np.nan),
    "duplicate column names": lambda b: b.set_axis([*b.columns[:-1], "agency"], axis=1),
    "empty batch": lambda b: b.iloc[:0],
    "no columns": lambda b: b.iloc[:, :0],
}


@pytest.mark.parametrize("corruption", CORRUPTIONS)
def test_detectors_never_raise(corruption, batch_feed, fake_llm):
    corrupted = CORRUPTIONS[corruption](batch_feed)
    fake_llm(repeats_earlier_record)
    for cls in dup.DETECTORS:
        det = cls()
        if cls is dup.StatNearDuplicateSimilarity:
            det.threshold_ = 0.8  # fitting is not under test here
        res = det.detect(corrupted, review_rows=np.arange(min(30, len(corrupted))))
        assert res.error is None, (cls.name, res.error)
        assert res.row_flags.shape == (len(corrupted),), cls.name


def test_inserted_duplicates_with_duplicate_index_are_positional(batch_feed):
    corrupted = pd.concat([batch_feed, batch_feed.iloc[[7]]])  # index label 7 appears twice
    res = dup.RuleExactDuplicateRows().detect(corrupted)
    assert np.flatnonzero(res.row_flags).tolist() == [len(batch_feed)]


# ----------------------------------------------------------------------------------------------
# timing: fit() on the reference fixture, detect() on batch_feed
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("cls", [dup.RuleExactDuplicateRows, dup.StatBloomDuplicate, dup.RuleNearDuplicateKey, dup.StatNearDuplicateSimilarity])
def test_timing(cls, reference_feed, big_reference, batch_feed):
    det = cls()
    t0 = time.perf_counter()
    det.fit(reference_feed)
    fit_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    det.fit(big_reference)
    fit_big_s = time.perf_counter() - t0
    detect_s = min(_timed(det, batch_feed) for _ in range(3))
    report(f"{cls.name} timing", fit_reference_s=fit_s, fit_150k_s=fit_big_s, detect_5000_s=detect_s)
    assert fit_big_s < 60 and detect_s < (3 if cls.problem == "f" else 1)


def _timed(det, batch) -> float:
    t0 = time.perf_counter()
    det.detect(batch)
    return time.perf_counter() - t0


# ----------------------------------------------------------------------------------------------
# Regression: two real patterns from the clean-batch alert review (DESIGN.md section 4, f)
# ----------------------------------------------------------------------------------------------
def _reports(rows: list[dict]) -> pd.DataFrame:
    """311 rows with the columns the f detectors read; every value a string, like the feed."""
    base = {"agency": "NYPD", "location_type": "Street/Sidewalk", "status": "Closed"}
    return pd.DataFrame([{**base, **r} for r in rows])


def _hydrant_burst() -> pd.DataFrame:
    """Nine phoned-in 'Blocked Hydrant' reports along one Queens block, 60-100 s apart, every one at
    its own house number but all geocoded to the same block-level point (as on 2023-11-04)."""
    numbers = ["139-10", "139-12", "139-14", "139-18", "139-01", "139-03", "139-07", "139-09", "139-15"]
    start = pd.Timestamp("2023-11-04T02:52:33")
    return _reports([{
        "unique_key": str(59326196 + i), "complaint_type": "Illegal Parking", "descriptor": "Blocked Hydrant",
        "borough": "QUEENS", "incident_address": f"{n} 97 AVENUE", "latitude": "40.69594164335303",
        "longitude": "-73.80980714934346",
        "created_date": (start + pd.Timedelta(seconds=75 * i)).strftime("%Y-%m-%dT%H:%M:%S.000"),
    } for i, n in enumerate(numbers)])


def _parkway_pair() -> pd.DataFrame:
    """One blocked driveway reported twice, ONLINE then by PHONE 231 s later, the address once
    spelled out (PARKWAY) and once truncated (PKW): the same request."""
    common = {"complaint_type": "Blocked Driveway", "descriptor": "No Access", "borough": "BROOKLYN",
              "latitude": "40.64402713641938", "longitude": "-73.98921494398556"}
    return _reports([
        {**common, "unique_key": "59329766", "incident_address": "3708 FORT HAMILTON PARKWAY", "created_date": "2023-11-04T08:38:09.000"},
        {**common, "unique_key": "59328673", "incident_address": "3708 FORT HAMILTON PKW", "created_date": "2023-11-04T08:42:00.000"},
    ])


def _stat(threshold: float = 0.80) -> dup.StatNearDuplicateSimilarity:
    det = dup.StatNearDuplicateSimilarity()
    det.threshold_ = threshold  # the threshold fit() lands on with the 2023 reference period
    return det


def test_nine_distinct_reports_on_one_block_are_not_merged():
    burst = _hydrant_burst()
    assert not dup.RuleNearDuplicateKey().detect(burst).row_flags.any()  # different addresses: different keys
    scores, _ = dup.StatNearDuplicateSimilarity._score_rows(burst)
    assert scores.max() == 0.0  # every pair is two different buildings
    assert not _stat().detect(burst).row_flags.any()


def test_without_the_building_check_the_burst_would_look_like_copies(monkeypatch):
    """Documents why the check exists: the other terms alone (same point, same descriptor, within
    300 s, similar address text) score 0.82, just above the 0.80 threshold."""
    monkeypatch.setattr(dup, "DIFFERENT_BUILDING_IS_NOT_A_COPY", False)
    flags = _stat().detect(_hydrant_burst()).row_flags
    assert flags.sum() >= 5 and not flags[0]  # most of the burst would be merged into near-copies


def test_parkway_and_its_truncation_pkw_are_one_request():
    pair = _parkway_pair()
    assert dup._location(pair).tolist() == ["3708 FORT HAMILTON PKWY"] * 2
    assert dup.RuleNearDuplicateKey().detect(pair).row_flags.tolist() == [False, True]  # the later entry
    stat = _stat().detect(pair)
    assert stat.row_flags.tolist() == [False, True] and "house number same" in stat.row_reasons[1]


def test_a_letter_typo_in_the_house_number_is_still_the_same_building():
    """The injector's typo edits letters, which can include a house number's suffix (105A -> 105B):
    such a copy must stay comparable."""
    pair = _reports([
        {"unique_key": "1", "complaint_type": "Noise - Residential", "descriptor": "Loud Music/Party", "borough": "BRONX",
         "incident_address": "105A HULL STREET", "latitude": "40.85", "longitude": "-73.88", "created_date": "2023-03-01T10:00:00.000"},
        {"unique_key": "2", "complaint_type": "Noise - Residential", "descriptor": "Loud Music/Party", "borough": "BRONX",
         "incident_address": "105B HULL STREET", "latitude": "40.85", "longitude": "-73.88", "created_date": "2023-03-01T10:02:00.000"},
    ])
    assert _stat().detect(pair).row_flags.tolist() == [False, True]
