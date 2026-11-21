"""Problems j and k: two fields of a row that must agree in meaning.

j  Complaint Type inconsistent with Descriptor          (row level)
k  Resolution Description contradicting Status          (row level)

Why these designs
-----------------
* 311 vocabularies are small and templated (~1,000 descriptors, ~800 distinct resolution
  texts a year). Every rule / stat detector evaluates each DISTINCT value once and maps the
  verdict back to its rows, so a batch costs a few hundred evaluations and fit() on 2.1 M
  rows is mostly one groupby (no subsampling needed).
* j rule: a frozen allow-list of (complaint_type, descriptor) pairs seen >= 2 times in the
  reference period - the declarative lookup table a data team would review and version.
* j stat: multinomial Naive Bayes on character n-grams of the descriptor predicts the
  complaint type. Character n-grams also score descriptors the reference never saw (new
  codes, spelling variants). Model and weights were chosen on reference data only: fit on
  Jan-Jul, scored on August - rows flagged, and recall on 9,873 hand-made mismatches (an
  August row given a descriptor never seen with its type in Jan-Aug) - by a rule fixed
  beforehand: the highest recall among candidates flagging <= 1.5 x TARGET_ROW_FPR of August
  rows. All six candidates (NB / logistic regression x raw / sqrt / log weights) qualified.
  NB with log weights won: recall 99.5 %, 0.12 % of August rows flagged (target 0.1 %; 280
  of its 341 alarms are one legitimate DPR pair, 'Maintenance or Facility' / 'Rodent
  Sighting'). NB with raw counts: 99.2 %, 0.05 %. Logistic regression: 98.0-98.7 %,
  0.07-0.11 %, and 7-53 s to fit instead of under a second.
* k rule: hand-written phrase lists for "the work is finished" and "this request is still
  being processed", written after reading the reference texts once. In 311, Closed often
  means "routed": many closing texts promise future steps ("DOT will inspect ... within
  180 days"), so the lists target what the text says about THIS request, not any future
  tense.
* k stat: TF-IDF word n-grams + logistic regression predict the status group (closed /
  open) from the text. It learns which templates each status really uses, including the
  counter-intuitive ones a phrase list cannot know.
* Both statistical models train on distinct (value, label) pairs weighted by their
  reference rows and flag P(observed label | value) below a threshold calibrated to
  config.TARGET_ROW_FPR on the reference rows. The weights follow each problem's definition:
  - j asks "does this pair occur at all?", so NB uses log(1 + rows): every legitimate pair
    counts, popularity only mildly. With raw counts a descriptor's main complaint type absorbs
    the probability and legitimate minority pairs score like mismatches - 'Car/Truck Horn'
    filed under 'Noise - Commercial' (434 of its 5,634 Jan-Jul rows; the rest are 'Noise -
    Vehicle') gets P = 3e-8 - so the threshold calibrated on Jan-Jul falls to 2e-6 (5e-3 with
    log weights) and more mismatches get through (81 vs 49 of the 9,873 above).
  - k asks "is this status rare for this text?" (the problem is defined by status shares),
    so logistic regression uses the raw counts - the same as training on every row - and
    P(group | text) estimates that share. Fit on Jan-Jul and scored on August, raw and log
    weights both caught all 10,000 hand-made contradictions (Closed rows given a text that is
    >= 95 % open in Jan-Aug, and the reverse), but raw counts flagged a quarter as many August
    rows (25 vs 97 of 279,933: 0.009 % vs 0.035 %).
* LLM detectors review rows one record each, with only the fields the check needs.

Scope: categories are compared ignoring case and spacing (HPD files the same category as
'HEAT/HOT WATER' and 'Heat/Hot Water'). A missing (null or blank) descriptor or resolution
text, and a status outside config.CLOSED_STATUSES / OPEN_STATUSES (e.g. 'Unspecified'),
cannot contradict anything and is never flagged. A batch without the needed columns is left
unflagged: a missing column is problem a's alarm, these checks cannot judge it. An LLM
detector that got no answer at all (LLMUnavailable, or no verdict for any row) returns an
error result that reviewed no row, as the other LLM detectors of the project do.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.naive_bayes import MultinomialNB

from project_b import config, llm
from project_b.core import DetectionResult, Detector

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
ALLOWLIST_MIN_COUNT = 2  # j rule: reference rows a pair needs to be on the allow-list
DESCRIPTOR_NGRAM_RANGE = (2, 4)  # j stat: character n-grams inside word boundaries
DESCRIPTOR_NB_ALPHA = 0.01  # j stat: NB smoothing; small keeps each descriptor's types sharp
RESOLUTION_NGRAM_RANGE = (1, 2)  # k stat: word unigrams and bigrams
RESOLUTION_LR_C = 100.0  # k stat: weak regularisation - texts are templates, so the model
#                          should learn each one's status share, not shrink it to the 99.3 % Closed prior (Jan-Jul)
RESOLUTION_LR_TOL = 1e-8  # k stat: sklearn minimises the MEAN weighted loss, where a 20-row template
#                           weighs ~1e-5; the default tol (1e-4) stops before such templates are fitted
RESOLUTION_LR_MAX_ITER = 5000  # k stat: solver budget for that tolerance (~300 iterations on 150k rows)
MAX_STATUS_THRESHOLD = 0.5  # k stat: a status the model itself prefers is never "unlikely", even when
#                             the reference has fewer unusual rows than the FPR budget allows
MAX_EXAMPLE_ROWS = 20  # example row positions listed in an aggregated alert
MAX_EXAMPLE_VALUES = 5  # most frequent offending values shown in an alert's `observed`

# k rule. Regular expressions matched case-insensitively anywhere in the text. A Closed row
# is flagged when its text has PENDING language and no FINISHED language; an open-status row
# when it has FINISHED language and no PENDING language. Texts with both or neither are fine.
FINISHED_PHRASES = [
    r"(?<!will be )\bclosed\b",  # the request was closed ("will be closed" is only a warning)
    r"\btook action\b|\baction was taken\b",
    r"\b(?:was|were|has been|have been|had been) (?:previously )?(?:issued|corrected|resolved|completed"
    r"|removed|repaired|restored|addressed|found|observed|fixed|prepared|placed|confirmed|performed)\b",
    r"\b(?:repaired|corrected|resolved|completed|removed|collected|cleaned|restored|replaced|addressed"
    r"|shut|unlocked|locked) (?:the|this|a|an|it|or)\b",
    r"\bissued (?:a|an)\b|\bperformed (?:the )?work\b|\bprovided additional information\b",
    r"\bfound (?:no|violations?|that|it|there)\b",  # an inspection outcome
    r"\bno (?:evidence|violations?|condition|indication|odors?|work is|action)\b",
    r"\bno further (?:action|attempts|information|status|updates)\b|\bnot necessary\b|\bwere gone\b",
    r"\b(?:did not|didn't|could not|couldn't|unable to|not able to)"
    r" (?:find|locate|gain|access|observe|respond|take action|act|identify|accept)\b",
    r"\binsufficient\b|\bnot have (?:enough|sufficient)\b",
    r"\b(?:not|no|isn't|out of)\b[^.]*\bjurisdiction\b",  # negation and 'jurisdiction' in one sentence
    r"\b(?:referred|forwarded) (?:it|this complaint|the complaint|this request|the request)"
    r" to the (?:Department|New York|NYPD)\b",  # handed over to another agency
    r"\bduplicate\b|\balready exists\b",  # closed in favour of an earlier request
]
PENDING_PHRASES = [
    r"\bis reviewing\b|\bhas received your complaint\b|\breceived and processed your complaint\b",
    r"\bhas been submitted\b|\bcheck back later\b",
    r"\bfurther (?:investigation|review|research) (?:is|will)\b|\brequires further review\b"
    r"|\badditional research is required\b",
    r"\b(?:still|currently) in progress\b|\bresults are pending\b",
    r"\b(?:re)?assigned it to\b",
    r"\b(?:referred|sent) (?:it|this complaint|the complaint) to (?:the|our|their|its)"
    r" [\w ]*(?:unit|division|team|squad|garage|bureau)\b",  # routed to the agency's own unit
    r"\bexpects? to address\b|\bwork order has been filed\b",
    r"\bwill visit\b|\bwill refer your\b|\battempt will be made\b|\bschedule an appointment\b",
    r"\bsent you a complaint form\b|\bneed to speak with you\b",
]
FINISHED_RE = re.compile("|".join(FINISHED_PHRASES), re.IGNORECASE)
PENDING_RE = re.compile("|".join(PENDING_PHRASES), re.IGNORECASE)

# Status -> group for problem k, compared ignoring case and surrounding spaces.
STATUS_GROUP = {s.lower(): "closed" for s in config.CLOSED_STATUSES} | {s.lower(): "open" for s in config.OPEN_STATUSES}

# The LLM prompts describe the kinds of category and text in general words. They never quote real
# complaint types, descriptors or resolution templates: a quoted template is a ready-made answer for
# every row that carries it (the phrase lists of the k rule may know the templates, the prompts may not).
J_TASK = """\
Check each NYC 311 service request for one problem: the Descriptor is inconsistent with the Complaint Type.
The descriptor is a sub-category of the complaint type, e.g. the kind of noise for a noise complaint, the
kind of violation for a parking complaint, the part of the building affected for a heat complaint, the
animal seen for a pest complaint.
Flag a row only when the descriptor clearly names a different kind of problem that belongs to another
complaint type, possibly of another agency (e.g. a road defect under a noise complaint, a parking problem
under a pest complaint).
Do NOT flag: generic placeholder descriptors (other, not applicable, see the details); a descriptor that is
a plausible detail of the complaint type even if unusual or worded differently; differences only in
capitalisation, spelling or abbreviation; a missing descriptor."""

K_TASK = """\
Check each NYC 311 service request for one problem: the Resolution Description contradicts the Status.
Status 'Closed' means the request is finished; 'Open', 'In Progress', 'Assigned', 'Pending' and 'Started'
mean the agency is still working on it.
Flag a row only when:
- the status is Closed but the text says this request is still being processed and reports no outcome
  (e.g. a text that only says the complaint was received or is under review, that the agency will get in
  touch, or that work is scheduled but not done yet); or
- the status is an open one but the text reports a final outcome (e.g. a text that says the agency fixed
  the problem, inspected and did or did not issue violations, found nothing to act on at the address, or
  closed the complaint).
Do NOT flag: neutral or informational texts (where to look up the status, a pointer to other notes, a
placeholder); Closed requests referred to another agency or closed as duplicates; closing texts that
mention possible next steps (advice to report the problem again if it returns, follow-up visits that may
come later); status 'Unspecified'; a missing text. closed_date_present is context only: never flag a row
because of it alone."""


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def _text_column(batch: pd.DataFrame, name: str) -> pd.Series | None:
    """Column `name` as strings indexed 0..n-1, or None if absent. Whatever dtype a corrupted
    batch delivers (numbers, mixed objects) is compared as text. Missing values stay NaN and a
    blank or whitespace-only string becomes NaN too: it says nothing, so it is missing as well."""
    if name not in batch.columns:
        return None
    column = batch.loc[:, batch.columns == name].iloc[:, 0]  # the first one if the name is duplicated
    text = column.astype("str").reset_index(drop=True)
    return text.mask(text.str.strip().eq(""))


def _normalise(values: pd.Series | pd.Index) -> pd.Series | pd.Index:
    """Case and spacing do not change what a category means."""
    return values.str.lower().str.replace(r"\s+", " ", regex=True).str.strip()


def _status_group(status: pd.Series) -> pd.Series:
    """'closed' / 'open' per row; NaN for statuses in neither group (e.g. 'Unspecified')."""
    return status.str.strip().str.lower().map(STATUS_GROUP)


def _not_checked(det: Detector, n_rows: int, **fields) -> DetectionResult:
    """A result that flags no row because the check could not run: a needed column is missing,
    or the LLM gave no answer. An LLM detector also reports that it reviewed no row (its metrics
    count only the rows it really reviewed); rule / stat detectors cover every row, so they keep
    reviewed=None and the rows they could not check count as not flagged. `fields` adds error / usage."""
    nothing = np.zeros(n_rows, dtype=bool)
    reviewed = nothing.copy() if det.family == "llm" else None
    return det.result(False, row_flags=nothing, reviewed=reviewed, **fields)


def _top_values(values: pd.Series) -> dict[str, int]:
    """The most frequent offending values with their row counts (an alert's `observed`)."""
    return values.value_counts().head(MAX_EXAMPLE_VALUES).to_dict()


def _example_rows(flags: np.ndarray) -> list[int]:
    return np.flatnonzero(flags)[:MAX_EXAMPLE_ROWS].tolist()


def _type_descriptor_counts(reference: pd.DataFrame) -> pd.Series:
    """Reference rows per normalised (complaint_type, descriptor) pair; rows missing either
    are skipped. Counting raw pairs first keeps the string work on ~1,500 distinct pairs."""
    raw = reference.groupby(["complaint_type", "descriptor"]).size()
    types = _normalise(raw.index.get_level_values("complaint_type"))
    descriptors = _normalise(raw.index.get_level_values("descriptor"))
    return raw.groupby([types, descriptors]).sum().rename_axis(["complaint_type", "descriptor"])


def _text_group_counts(reference: pd.DataFrame) -> pd.Series:
    """Reference rows per (resolution_description, status group); missing texts and
    statuses outside both groups are skipped."""
    group = _status_group(reference["status"]).rename("group")
    return reference.groupby([reference["resolution_description"], group]).size()


def _label_probability(vectorizer, classifier, texts, labels) -> tuple[np.ndarray, np.ndarray]:
    """P(label | text) for each (text, label) pair and the model's most likely label.
    A label the model never saw in training gets probability 0."""
    proba = classifier.predict_proba(vectorizer.transform(texts))
    column = pd.Index(classifier.classes_).get_indexer(labels)  # -1 = unknown label
    p = np.where(column >= 0, proba[np.arange(len(column)), column], 0.0)
    return p, classifier.classes_[proba.argmax(axis=1)]


def _fit_label_model(vectorizer, classifier, texts, labels, weights: np.ndarray) -> np.ndarray:
    """Fit classifier(vectorizer(text)) -> label on distinct (text, label) pairs with the given
    sample weights; return each pair's in-sample P(label | text) for calibration."""
    classifier.fit(vectorizer.fit_transform(texts), labels, sample_weight=weights)
    return _label_probability(vectorizer, classifier, texts, labels)[0]


def _calibrated_threshold(p: np.ndarray, rows: np.ndarray) -> float:
    """Largest threshold t such that the reference rows with P < t are at most
    TARGET_ROW_FPR of all reference rows (`p` per distinct pair, `rows` its row count)."""
    order = np.argsort(p, kind="stable")
    share_at_or_below = np.cumsum(rows[order]) / rows.sum()
    first_over_budget = np.searchsorted(share_at_or_below, config.TARGET_ROW_FPR, side="right")
    return float(p[order][min(first_over_budget, len(p) - 1)])


def _score_rows(vectorizer, classifier, texts: pd.Series, labels: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """_label_probability for every row, computed once per distinct (text, label) pair."""
    codes, pairs = pd.MultiIndex.from_arrays([texts, labels]).factorize()
    p, best = _label_probability(vectorizer, classifier, pairs.get_level_values(0), pairs.get_level_values(1))
    return p[codes], best[codes]


def _first_match(pattern: re.Pattern, texts: pd.Series) -> pd.Series:
    """The first phrase of `pattern` found in each text (NaN if none), searched once per distinct text."""
    distinct = texts.dropna().unique()
    found = {text: match.group(0) for text in distinct if (match := pattern.search(text))}
    return texts.map(found)


def _llm_review(det: Detector, fields: pd.DataFrame, review_rows, task: str, columns: list[str]) -> DetectionResult:
    """Per-row LLM review of `fields` (one row per batch position) for the requested rows.
    Each record's id is its batch position, so verdicts map straight back to rows. Only rows
    with a readable verdict are reviewed: a row the model gave no (parseable) verdict for is an
    abstention, listed in `abstained` with its cause - silence is never read as "clean", and a
    reply without a verdict for ANY row reviews nothing (its tokens still count)."""
    n_rows = len(fields)
    positions = np.arange(n_rows) if review_rows is None else np.unique(np.asarray(review_rows, dtype=int))
    positions = positions[(positions >= 0) & (positions < n_rows)].tolist()
    records = [{"id": pos, **record} for pos, record in zip(positions, fields.iloc[positions].to_dict("records"))]
    abstained: dict[int, str] = {}
    try:
        verdicts, usage, _ = llm.review_rows(task, records, tag=f"{det.problem}/{det.name}", abstained=abstained)
    except llm.LLMUnavailable as exc:
        return _not_checked(det, n_rows, error=f"llm_unavailable: {exc}")
    answered = [pos for pos in positions if pos in verdicts]  # ids the model made up are ignored

    flags, reviewed = np.zeros(n_rows, dtype=bool), np.zeros(n_rows, dtype=bool)
    reviewed[answered] = True
    reasons, evidence = {}, {}
    for pos in answered:
        verdict = verdicts[pos]
        if verdict.get("flagged") is True:
            flags[pos] = True
            reasons[pos] = str(verdict.get("reason") or "flagged by the LLM")
            evidence[pos] = {key: verdict.get(key) for key in ("observed", "expected", "confidence")}

    alerts = []
    if flags.any():
        flagged = np.flatnonzero(flags).tolist()
        unanswered = len(positions) - len(answered)
        confidences = [e["confidence"] for e in evidence.values() if isinstance(e["confidence"], (int, float))]
        alerts.append(det.alert(
            f"LLM review flagged {len(flagged)} of {len(answered)} reviewed rows"
            + (f" ({unanswered} rows got no verdict)" if unanswered else ""),
            check_id=f"L-{det.problem}1",
            columns=columns,
            rows=flagged[:MAX_EXAMPLE_ROWS],
            n_rows=len(flagged),
            observed=_model_evidence(evidence, flagged, "observed"),
            expected=_model_evidence(evidence, flagged, "expected"),
            score=float(np.mean(confidences)) if confidences else None,
        ))
    return det.result(
        bool(alerts), alerts=alerts, row_flags=flags, row_reasons=reasons, row_evidence=evidence, reviewed=reviewed,
        abstained=abstained, usage=usage,
    )


def _model_evidence(evidence: dict[int, dict], flagged: list[int], key: str) -> dict[int, object] | None:
    """The model's own `key` ('observed' / 'expected') for the first flagged rows that have one,
    by row position; None when the model gave none, so an alert never shows evidence it lacks."""
    rows = [pos for pos in flagged if _filled_in(evidence[pos][key])][:MAX_EXAMPLE_VALUES]
    return {pos: evidence[pos][key] for pos in rows} or None


def _filled_in(value) -> bool:
    """Did the model fill in this field? None and blank strings are not an answer."""
    return value is not None and not (isinstance(value, str) and not value.strip())


# --------------------------------------------------------------------------
# j - Complaint Type inconsistent with Descriptor
# --------------------------------------------------------------------------
class RuleComplaintDescriptorAllowlist(Detector):
    problem = "j"
    family = "rule"
    name = "rule_complaint_descriptor_allowlist"
    description = "Flags (complaint_type, descriptor) pairs missing from a frozen allow-list of pairs seen at least twice in the reference period."

    def fit(self, reference: pd.DataFrame) -> "RuleComplaintDescriptorAllowlist":
        counts = _type_descriptor_counts(reference)
        allowed = counts[counts >= ALLOWLIST_MIN_COUNT]
        self.allowed_ = allowed.index
        # The three most common descriptors per type, quoted as "expected" in explanations.
        top = allowed.sort_values(ascending=False).groupby(level="complaint_type").head(3)
        self.examples_ = top.reset_index().groupby("complaint_type")["descriptor"].agg(", ".join).to_dict()
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        raw_types, raw_descriptors = _text_column(batch, "complaint_type"), _text_column(batch, "descriptor")
        if raw_types is None or raw_descriptors is None:
            return _not_checked(self, len(batch))
        types, descriptors = _normalise(raw_types), _normalise(raw_descriptors)
        checked = (types.notna() & descriptors.notna()).to_numpy()
        allowed = pd.MultiIndex.from_arrays([types, descriptors]).isin(self.allowed_)
        flags = checked & ~allowed
        if not flags.any():
            return self.result(False, row_flags=flags)

        reasons = {
            pos: f"('{raw_types[pos]}', '{raw_descriptors[pos]}') is not an allowed pair; "
            f"allowed descriptors for this type include: {self.examples_.get(types[pos], 'none (type not on the list)')}"
            for pos in np.flatnonzero(flags).tolist()
        }
        alert = self.alert(
            f"{flags.sum()} row(s) have a (complaint_type, descriptor) pair that is not on the allow-list",
            check_id="R-j1",
            columns=["complaint_type", "descriptor"],
            rows=_example_rows(flags),
            n_rows=int(flags.sum()),
            observed=_top_values(raw_types[flags] + " / " + raw_descriptors[flags]),
            expected=f"one of {len(self.allowed_)} pairs seen >= {ALLOWLIST_MIN_COUNT} times in the reference period",
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_reasons=reasons)


class StatDescriptorClassifier(Detector):
    problem = "j"
    family = "stat"
    name = "stat_descriptor_classifier"
    description = (
        "Naive Bayes on descriptor character n-grams predicts the complaint type; flags rows whose "
        "P(observed type | descriptor) is below a threshold calibrated to the target reference FPR."
    )

    def fit(self, reference: pd.DataFrame) -> "StatDescriptorClassifier":
        counts = _type_descriptor_counts(reference)
        self.vectorizer_ = TfidfVectorizer(analyzer="char_wb", ngram_range=DESCRIPTOR_NGRAM_RANGE, sublinear_tf=True)
        self.model_ = MultinomialNB(alpha=DESCRIPTOR_NB_ALPHA)
        rows = counts.to_numpy()
        p = _fit_label_model(
            self.vectorizer_, self.model_,
            counts.index.get_level_values("descriptor"), counts.index.get_level_values("complaint_type"),
            weights=np.log1p(rows),  # "does the pair occur at all?" (module docstring)
        )
        self.threshold_ = _calibrated_threshold(p, rows)
        self.reference_fpr_ = float(rows[p < self.threshold_].sum() / rows.sum())
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        raw_types, raw_descriptors = _text_column(batch, "complaint_type"), _text_column(batch, "descriptor")
        if raw_types is None or raw_descriptors is None:
            return _not_checked(self, len(batch))
        types, descriptors = _normalise(raw_types), _normalise(raw_descriptors)
        checked = (types.notna() & descriptors.notna()).to_numpy()
        p = np.ones(len(batch))
        likely_type = np.full(len(batch), None, dtype=object)
        if checked.any():
            p[checked], likely_type[checked] = _score_rows(self.vectorizer_, self.model_, descriptors[checked], types[checked])
        flags = checked & (p < self.threshold_)
        scores = np.where(checked, 1.0 - p, 0.0)
        if not flags.any():
            return self.result(False, row_flags=flags, row_scores=scores)

        known_type = types.isin(self.model_.classes_).to_numpy()
        reasons = {
            pos: (
                f"descriptor '{raw_descriptors[pos]}' is unlikely for complaint type '{raw_types[pos]}': "
                f"P(type | descriptor) = {p[pos]:.2g} < {self.threshold_:.2g}; it reads like '{likely_type[pos]}'"
                if known_type[pos]
                else f"complaint type '{raw_types[pos]}' never occurred in the reference, so no descriptor fits it"
            )
            for pos in np.flatnonzero(flags).tolist()
        }
        alert = self.alert(
            f"{flags.sum()} row(s) have a descriptor that is unlikely for their complaint type",
            check_id="S-j1",
            columns=["complaint_type", "descriptor"],
            rows=_example_rows(flags),
            n_rows=int(flags.sum()),
            observed=_top_values(raw_types[flags] + " / " + raw_descriptors[flags]),
            expected=f"P(complaint_type | descriptor) >= {self.threshold_:.2g} (calibrated to {config.TARGET_ROW_FPR:.1%} of reference rows)",
            score=float(p[flags].min()),
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_scores=scores, row_reasons=reasons)


class LlmComplaintDescriptorReview(Detector):
    problem = "j"
    family = "llm"
    name = "llm_complaint_descriptor_review"
    description = "The LLM reads agency, complaint_type and descriptor of each reviewed row and flags descriptors that do not fit the complaint type."

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        fields = {name: _text_column(batch, name) for name in ("agency", "complaint_type", "descriptor")}
        if fields["complaint_type"] is None or fields["descriptor"] is None:
            return _not_checked(self, len(batch))  # no call, no tokens spent
        frame = pd.DataFrame(fields, index=pd.RangeIndex(len(batch)))  # a missing agency is sent as null
        return _llm_review(self, frame, review_rows, J_TASK, columns=["complaint_type", "descriptor"])


# --------------------------------------------------------------------------
# k - Resolution Description contradicting Status
# --------------------------------------------------------------------------
class RuleResolutionStatusKeywords(Detector):
    problem = "k"
    family = "rule"
    name = "rule_resolution_status_keywords"
    description = (
        "Hand-written phrase lists: flags Closed rows whose text only says the request is still being "
        "processed, and open-status rows whose text only reports a finished outcome."
    )

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        status, text = _text_column(batch, "status"), _text_column(batch, "resolution_description")
        if status is None or text is None:
            return _not_checked(self, len(batch))
        group = _status_group(status)
        finished, pending = _first_match(FINISHED_RE, text), _first_match(PENDING_RE, text)
        closed_but_pending = (group.eq("closed") & pending.notna() & finished.isna()).to_numpy()
        open_but_finished = (group.eq("open") & finished.notna() & pending.isna()).to_numpy()
        flags = closed_but_pending | open_but_finished
        if not flags.any():
            return self.result(False, row_flags=flags)

        reasons = {
            pos: (
                f"status '{status[pos]}' but the text only describes pending work ('{pending[pos]}'), no outcome"
                if closed_but_pending[pos]
                else f"status '{status[pos]}' but the text reports a finished outcome ('{finished[pos]}') and nothing pending"
            )
            for pos in np.flatnonzero(flags).tolist()
        }
        evidence = pending.where(closed_but_pending, finished)
        alert = self.alert(
            f"{flags.sum()} row(s) have a resolution text that contradicts their status "
            f"({closed_but_pending.sum()} Closed with pending-only text, {open_but_finished.sum()} open with finished-only text)",
            check_id="R-k1",
            columns=["status", "resolution_description"],
            rows=_example_rows(flags),
            n_rows=int(flags.sum()),
            observed=_top_values(status[flags] + ": '" + evidence[flags] + "'"),
            expected="Closed rows describe an outcome; open-status rows do not report a final outcome",
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_reasons=reasons)


class StatResolutionStatusClassifier(Detector):
    problem = "k"
    family = "stat"
    name = "stat_resolution_status_classifier"
    description = (
        "TF-IDF + logistic regression predicts the status group (closed / open) from the resolution text; flags "
        "rows whose P(observed group | text) is below a threshold calibrated to the target reference FPR."
    )

    def fit(self, reference: pd.DataFrame) -> "StatResolutionStatusClassifier":
        counts = _text_group_counts(reference)
        self.vectorizer_ = TfidfVectorizer(ngram_range=RESOLUTION_NGRAM_RANGE, sublinear_tf=True)
        self.model_ = LogisticRegression(C=RESOLUTION_LR_C, tol=RESOLUTION_LR_TOL, max_iter=RESOLUTION_LR_MAX_ITER)
        rows = counts.to_numpy()
        p = _fit_label_model(
            self.vectorizer_, self.model_,
            counts.index.get_level_values("resolution_description"), counts.index.get_level_values("group"),
            weights=rows,  # "how rare is this status for this text?" (module docstring)
        )
        self.threshold_ = min(_calibrated_threshold(p, rows), MAX_STATUS_THRESHOLD)
        self.reference_fpr_ = float(rows[p < self.threshold_].sum() / rows.sum())
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        status, text = _text_column(batch, "status"), _text_column(batch, "resolution_description")
        if status is None or text is None:
            return _not_checked(self, len(batch))
        group = _status_group(status)
        checked = (group.notna() & text.notna()).to_numpy()
        p = np.ones(len(batch))
        likely_group = np.full(len(batch), None, dtype=object)
        if checked.any():
            p[checked], likely_group[checked] = _score_rows(self.vectorizer_, self.model_, text[checked], group[checked])
        flags = checked & (p < self.threshold_)
        scores = np.where(checked, 1.0 - p, 0.0)
        if not flags.any():
            return self.result(False, row_flags=flags, row_scores=scores)

        reasons = {
            pos: f"status '{status[pos]}' is unlikely for this resolution text: P({group[pos]} | text) = {p[pos]:.2g} "
            f"< {self.threshold_:.2g}; the text is typical of {likely_group[pos]} requests"
            for pos in np.flatnonzero(flags).tolist()
        }
        alert = self.alert(
            f"{flags.sum()} row(s) have a status that is unlikely for their resolution text",
            check_id="S-k1",
            columns=["status", "resolution_description"],
            rows=_example_rows(flags),
            n_rows=int(flags.sum()),
            observed=_top_values(status[flags] + ": '" + text[flags].str.slice(0, 80) + "'"),
            expected=f"P(status group | text) >= {self.threshold_:.2g} (calibrated to {config.TARGET_ROW_FPR:.1%} of reference rows)",
            score=float(p[flags].min()),
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_scores=scores, row_reasons=reasons)


class LlmResolutionStatusReview(Detector):
    problem = "k"
    family = "llm"
    name = "llm_resolution_status_review"
    description = "The LLM reads status, resolution_description and whether closed_date is set, and flags texts that contradict the status."

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        status, text = _text_column(batch, "status"), _text_column(batch, "resolution_description")
        if status is None or text is None:
            return _not_checked(self, len(batch))  # no call, no tokens spent
        closed_date = _text_column(batch, "closed_date")
        frame = pd.DataFrame(
            {
                "status": status,
                "resolution_description": text,
                "closed_date_present": None if closed_date is None else closed_date.notna(),  # null = unknown
            },
            index=pd.RangeIndex(len(batch)),
        )
        return _llm_review(self, frame, review_rows, K_TASK, columns=["status", "resolution_description"])


DETECTORS = [
    RuleComplaintDescriptorAllowlist,
    StatDescriptorClassifier,
    LlmComplaintDescriptorReview,
    RuleResolutionStatusKeywords,
    StatResolutionStatusClassifier,
    LlmResolutionStatusReview,
]
