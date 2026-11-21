"""Problem l - personal data (names, phone numbers, e-mails) inside free-text fields.

The free-text columns (config.FREE_TEXT_COLUMNS) are almost entirely agency templates:
3.2 M rows of 2023 carry only 794 distinct resolution texts, and the templates themselves
print PUBLIC office numbers and mailboxes ("call 311 (or 212-639-9675 ...)",
infobfscs@health.nyc.gov). "Contains a phone number" is therefore not the same as
"contains personal data", and each family deals with that in its own way:

* rule_pii_regex - the patterns a privacy reviewer writes first (e-mail, NANP phone,
  "Mr./Ms. Name", "tenant|caller|... Name Surname") minus a hand-written allow-list of the
  public numbers printed in the templates. Precise and explainable and learns nothing, but
  blind to a name without a cue word ("Per Anita Desai, ...") and the list needs upkeep.
* stat_pii_template_novelty - because the text is templated, personal data shows up as
  tokens the reference never contained. Needs no list of names or numbers, but a word from a
  new legitimate template looks just as novel; its calibrated threshold has to absorb that.
* llm_pii_review - reads the text, so it can tell a person from an organisation, at a
  token cost per row.

Every detector looks at each distinct text of a column once (a batch of 5,000 rows has a
few hundred) and maps the result back to row positions.

Three narrow fixes came from reviewing clean-batch alerts, each measured on Jan-Aug only
(CUE_INSIDE_LABEL_IS_NOT_A_CUE, NOVELTY_SKIPS_PUBLIC_NUMBERS, one PUBLIC_PHONE_NUMBERS entry;
`python -m project_b.audits.pii_false_positives`). Ignoring capitalisation in Title Case category
labels was rejected: it would hide a name written into a label. New category labels therefore
still look novel until the vocabulary is refreshed (DESIGN.md section 4, l).

Alerts are forwarded and stored (the harness writes them to results/eval_records.jsonl, also
for real, unsanitised batches), so an alert never repeats the personal data it found: its
`observed` shows each value masked ('L**** M*****'). The full value stays in row_reasons /
row_evidence, next to the row it came from.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

from project_b import config, llm
from project_b.core import Alert, DetectionResult, Detector

# --------------------------------------------------------------------------------------
# rule_pii_regex
# --------------------------------------------------------------------------------------
# Public office numbers printed in the 311 templates, read once from the Jan-Aug 2023
# (reference) resolution texts and frozen here, like the allow-list of problem j: every number
# printed in >= 10 reference rows, plus all 5 HPD borough offices. Not re-learned by fit().
# Toll-free numbers always belong to organisations, so they need no entry.
PUBLIC_PHONE_NUMBERS = frozenset({
    "2126399675",  # 311 from outside NYC, "(212) NEW-YORK"
    "2126761600", "6466326101", "6466326104", "6466326023", "7187866004",  # Health Dept
    "2124874110",  # Consumer and Worker Protection
    "2122911220",  # Sanitation
    "7183915617",  # Taxi and Limousine Commission
    "7188271955", "7185796777", "7182860823", "2122347485", "7188162340", "2128634951",  # HPD
    "7188768441",  # Staten Island Ferry
    "7189352200",  # Department of Education
    "5184869786", "5184576195", "5183883300", "2126434760",  # New York State agencies
    "2125336200",  # Better Business Bureau
    # Added by hand, like the HPD offices: the U.S. DOT Office of Aviation Consumer Protection, printed in
    # DCWP's "not within its jurisdiction" referral template - 8 Jan-Aug 2023 rows, below the >= 10 cut.
    "2023662220",
})
TOLL_FREE_AREA_CODES = frozenset({"800", "833", "844", "855", "866", "877", "888"})
# PUBLIC_PHONE_NUMBERS and TOLL_FREE_AREA_CODES are the project's two explicit lists of numbers that
# are not personal data. The rule (R-l2) and, with NOVELTY_SKIPS_PUBLIC_NUMBERS, the novelty
# statistic both use them, so a public number is public to every detector.
PUBLIC_EMAIL_DOMAIN = ".gov"  # agency mailboxes, e.g. infobfscs@health.nyc.gov
NAME_CUES = r"tenant|caller|complainant|resident|spoke\s+with|contact"
TITLES = r"Mr|Ms|Mrs|Dr"
CHECKS = {"R-l1": "e-mail", "R-l2": "phone number", "R-l3": "name after a cue"}

NANP_3 = r"[2-9](?!11)\d{2}"  # area code / exchange: starts with 2-9, never N11 (311, 911)
PHONE_RE = re.compile(
    rf"(?<!\d)(?:\+?1[\s.-]*)?(?:\({NANP_3}\)|{NANP_3})[\s.-]*{NANP_3}[\s.-]*\d{{4}}(?!\d)"  # (212) 555-0147, 212.555.0147, +1 212 555 0147
    rf"|(?<!\d){NANP_3}[.-]\d{{4}}(?!\d)"  # local number without area code: 555-0147
)
# The look-behind lets a match start only where a run of address characters starts. Without
# it the engine re-scans a long run from every character (quadratic: 3.5 s on one 32k-char cell).
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
NAME_WORD = r"[A-ZÀ-Þ][A-Za-zÀ-ÿ'-]*[a-zß-ÿ]"  # Maria, O'Brien, McDonald, José
TITLE_NAME_RE = re.compile(rf"\b(?:{TITLES})\.?\s+{NAME_WORD}")
# Two capitalised words after a cue word. A third capitalised word means a Title Case
# label such as "Tenant Refused Install Request", not a person.
CUE_NAME_RE = re.compile(rf"\b(?i:{NAME_CUES})[\s:,-]+{NAME_WORD}\s+{NAME_WORD}\b(?!\s+[A-ZÀ-Þ])")
# A cue word that directly follows a capitalised word sits inside a Title Case label, not in a
# sentence: "Food Contact Equipment Dirty" (a DOHMH category, 13 Jan-Aug 2023 rows) is no "contact
# <Name Surname>". A cue at the start of a sentence or after a lower-case word still counts.
CUE_INSIDE_LABEL_IS_NOT_A_CUE = True
_LABEL_WORD_BEFORE_RE = re.compile(rf"{NAME_WORD} $")

# --------------------------------------------------------------------------------------
# stat_pii_template_novelty
# --------------------------------------------------------------------------------------
# A token is template vocabulary once this many reference cells contain it. 1 = only never-seen
# tokens are novel; 3 (rare ones too) lost ~30 % of bare names on held-out Jul-Aug rows.
MIN_TOKEN_ROWS = 1
MIN_PHONE_DIGITS = 7  # a digit run this long is phone-shaped (a local number has 7 digits)
# A digit run that the rule's explicit lists call public (PUBLIC_PHONE_NUMBERS, TOLL_FREE_AREA_CODES)
# is not personal-data-shaped: toll-free and agency numbers in new templates are not novel personal
# data (in Jul-Aug 2023, 9 clean rows were flagged for one toll-free number).
NOVELTY_SKIPS_PUBLIC_NUMBERS = True
HOLDOUT_SHARE = 0.25  # most recent share of the reference used to set weights + threshold
SHAPES = ("capitalised word", "digit run", "@ token")  # token shapes personal data takes
WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")  # letters of any alphabet: Desai, O'Brien
DIGIT_RUN_RE = re.compile(r"\d[\d\s().-]*\d")  # digits with phone separators: (212) 555-0147
AT_TOKEN_RE = re.compile(r"(?<!\S)\S+@\S+")  # starts only at a token start: linear time, like EMAIL_RE

# --------------------------------------------------------------------------------------
# llm_pii_review
# --------------------------------------------------------------------------------------
LLM_TASK = """\
Problem: personal data in the free-text fields of NYC 311 service requests (descriptor,
descriptor_2, resolution_description). Most texts are standard agency templates.
Personal data = information that identifies a PRIVATE individual:
- a person's name (first and/or last name, e.g. "Jane Doe", "Mr. Doe"),
- a phone number or e-mail address of a person.
NOT personal data: names of agencies, departments, programs, businesses, streets or places;
roles and job titles without a name ("the tenant", "an inspector"); 311, 911 and the public
office numbers, mailboxes and websites printed in agency messages, e.g. (212) NEW-YORK
(212-639-9675), (212) 676-1600, infobfscs@health.nyc.gov, nyc.gov/311.
Set flagged=true only for rows that contain personal data; put the personal data exactly as
written in "observed" and "no personal data" in "expected".
"""


# --------------------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------------------
def _free_text_columns(frame: pd.DataFrame) -> list[tuple[str, pd.Series]]:
    """(name, values) of every free-text column, taken by position: a duplicated header (a
    corrupted feed) gives one entry per copy, where frame[name] would return a DataFrame."""
    return [(name, frame.iloc[:, i]) for i, name in enumerate(frame.columns) if name in config.FREE_TEXT_COLUMNS]


def _column_texts(values: pd.Series) -> pd.Series:
    """Non-null values as str, indexed by row position (works for any dtype, lists included)."""
    values = values.reset_index(drop=True)
    return values[values.notna()].astype(str)


def _mask(value) -> str | None:
    """A personal-data value as an alert shows it: the first character of every word or number,
    the rest starred ('Lucia Moreno' -> 'L**** M*****', '(917) 555-0142' -> '(9**) 5**-0***')."""
    return None if value is None else re.sub(r"(?<=\w)\w", "*", str(value))


# --------------------------------------------------------------------------------------
# rule
# --------------------------------------------------------------------------------------
def _is_public_number(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone)[-10:]  # drops a leading +1
    return digits in PUBLIC_PHONE_NUMBERS or (len(digits) == 10 and digits[:3] in TOLL_FREE_AREA_CODES)


def _pii_matches(text: str) -> list[tuple[str, str]]:
    """(check_id, matched text) for every personal-data pattern in one text."""
    found = [("R-l1", m.group()) for m in EMAIL_RE.finditer(text) if not m.group(1).lower().endswith(PUBLIC_EMAIL_DOMAIN)]
    found += [("R-l2", m.group()) for m in PHONE_RE.finditer(text) if not _is_public_number(m.group())]
    found += [("R-l3", m.group()) for m in TITLE_NAME_RE.finditer(text)]
    found += [("R-l3", m.group()) for m in CUE_NAME_RE.finditer(text)
              if not (CUE_INSIDE_LABEL_IS_NOT_A_CUE and _LABEL_WORD_BEFORE_RE.search(text[: m.start()]))]
    return found


class RulePiiRegex(Detector):
    problem = "l"
    family = "rule"
    name = "rule_pii_regex"
    description = (
        "E-mail (R-l1), NANP phone (R-l2) and cue + name (R-l3) regexes over the free-text fields, "
        "minus a hand-written allow-list of public office numbers and .gov mailboxes."
    )

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        hits: dict[int, list[tuple[str, str, str]]] = defaultdict(list)  # position -> (check, column, match)
        for col, values in _free_text_columns(batch):
            texts = _column_texts(values)
            found_by_text = {t: _pii_matches(t) for t in texts.unique()}
            texts_with_hits = {t for t, found in found_by_text.items() if found}
            for pos, text in texts[texts.isin(texts_with_hits)].items():
                hits[pos] += [(check, col, match) for check, match in found_by_text[text]]

        flags = np.zeros(len(batch), dtype=bool)
        flags[list(hits)] = True
        if not hits:
            return self.result(False, row_flags=flags)
        reasons = {
            pos: "; ".join(f"{check} {CHECKS[check]} '{match}' in {col}" for check, col, match in found)
            for pos, found in hits.items()
        }
        return self.result(True, alerts=[self._alert(hits)], row_flags=flags, row_reasons=reasons)

    def _alert(self, hits: dict[int, list[tuple[str, str, str]]]) -> Alert:
        rows = sorted(hits)
        rows_per_check: Counter = Counter()
        for found in hits.values():
            rows_per_check.update({check for check, _, _ in found})
        summary = ", ".join(f"{check} {CHECKS[check]}: {n}" for check, n in sorted(rows_per_check.items()))
        return self.alert(
            f"{len(rows)} rows contain personal data in free text ({summary})",
            check_id=",".join(sorted(rows_per_check)),
            columns=sorted({col for found in hits.values() for _, col, _ in found}),
            rows=rows[:20],
            n_rows=len(rows),
            observed={pos: [_mask(match) for _, _, match in hits[pos]] for pos in rows[:5]},
            expected="no e-mail, phone number or cue + name (allowed: public office numbers, .gov mailboxes)",
        )


# --------------------------------------------------------------------------------------
# statistical
# --------------------------------------------------------------------------------------
def _tokens(text: str) -> list[tuple[str, str | None]]:
    """(token, shape) pairs. Words are lower-cased and digit runs reduced to their digits,
    so '(212) 639-9675' and '212-639-9675' are the same token. Shape is None for words that
    cannot carry personal data (lower-case words, acronyms)."""
    tokens = []
    for word in WORD_RE.findall(text):
        capitalised = word[0].isupper() and not word.isupper()
        tokens.append((word.lower(), "capitalised word" if capitalised else None))
    for run in DIGIT_RUN_RE.findall(text):
        digits = re.sub(r"\D", "", run)
        if len(digits) >= MIN_PHONE_DIGITS:
            public = NOVELTY_SKIPS_PUBLIC_NUMBERS and _is_public_number(digits)
            tokens.append((digits, None if public else "digit run"))
    for token in AT_TOKEN_RE.findall(text):
        tokens.append((token.lower().strip(".,;:!?()<>[]\"'"), "@ token"))
    return tokens


def _vocabulary(frame: pd.DataFrame) -> Counter:
    """Token -> number of free-text cells that contain it (a row counts once per column)."""
    vocab: Counter = Counter()
    for _, values in _free_text_columns(frame):
        for text, n_rows in values.value_counts().items():
            for token in {tok for tok, _ in _tokens(str(text))}:
                vocab[token] += n_rows
    return vocab


def _novel_tokens(text: str, vocab: Counter) -> list[tuple[str, str]]:
    """(token, shape) of the personal-data-shaped tokens of `text` that are not vocabulary."""
    return [(tok, shape) for tok, shape in dict.fromkeys(_tokens(text)) if shape and vocab[tok] < MIN_TOKEN_ROWS]


def _shape_counts(tokens: list[tuple[str, str]]) -> list[int]:
    return [sum(token_shape == shape for _, token_shape in tokens) for shape in SHAPES]


def _novel_counts(frame: pd.DataFrame, vocab: Counter) -> tuple[np.ndarray, dict[str, list[tuple[str, str]]]]:
    """Per row: number of novel tokens of each shape (columns = SHAPES), summed over the
    free-text columns. Also returns the novel tokens of every distinct text."""
    counts = np.zeros((len(frame), len(SHAPES)))
    novel_by_text: dict[str, list[tuple[str, str]]] = {}
    for _, values in _free_text_columns(frame):
        texts = _column_texts(values)
        codes, uniques = pd.factorize(texts)
        novel = [_novel_tokens(text, vocab) for text in uniques]
        per_text = np.array([_shape_counts(toks) for toks in novel]).reshape(-1, len(SHAPES))  # also when empty
        counts[texts.index.to_numpy()] += per_text[codes]
        novel_by_text.update(zip(uniques, novel))
    return counts, novel_by_text


def _novel_cells(batch: pd.DataFrame, positions: np.ndarray, novel_by_text: dict[str, list[tuple[str, str]]]) -> dict[int, dict[str, list[str]]]:
    """Row position -> {column: novel tokens of that cell}, for the given rows. Reuses the
    texts _novel_counts scored (same str conversion), so any cell value works, lists included."""
    cells: dict[int, dict[str, list[str]]] = {int(pos): {} for pos in positions}
    for col, values in _free_text_columns(batch):
        texts = _column_texts(values)
        for pos, text in texts[texts.index.isin(positions)].items():
            tokens = [tok for tok, _ in novel_by_text[text]]
            if tokens:
                cells[pos].setdefault(col, []).extend(tokens)
    return cells


class StatPiiTemplateNovelty(Detector):
    problem = "l"
    family = "stat"
    name = "stat_pii_template_novelty"
    description = (
        "Scores capitalised words, long digit runs and @-tokens never seen in the (templated) reference "
        "free text, weighted by how rarely template drift produces each shape; threshold calibrated to the target FPR."
    )

    def fit(self, reference: pd.DataFrame) -> "StatPiiTemplateNovelty":
        """Calibrate on the most recent HOLDOUT_SHARE of the reference (it arrives in time
        order: the harness keeps created_date order) scored against a vocabulary of the older
        part - the situation of a new batch, whose new templates are the false alarms to budget
        for. Works on distinct texts (a few thousand), so the full 2.1 M-row reference needs no
        subsampling.

        On the full 2023 reference only ~0.005 % of held-out rows hold any novel token, fewer
        than TARGET_ROW_FPR, so the threshold comes out 0: one never-seen personal-data-shaped
        token flags a row, and the weights only rank rows (row_scores). The weights decide
        flags when template drift is common, e.g. after a new form goes live."""
        cut = int(len(reference) * (1 - HOLDOUT_SHARE))
        holdout = reference.iloc[cut:]
        counts, _ = _novel_counts(holdout, _vocabulary(reference.iloc[:cut]))
        # Weight of a shape = surprisal of meeting a novel token of that shape in a held-out
        # row (+1/+2 smoothing). New template words are common, new phone numbers rare and
        # new e-mails absent, so one novel e-mail outweighs one novel word.
        rate = ((counts > 0).sum(axis=0) + 1) / (len(holdout) + 2)
        self.weights_ = pd.Series(-np.log10(rate), index=SHAPES)
        scores = counts @ self.weights_.to_numpy()
        self.threshold_ = float(np.quantile(scores, 1 - config.TARGET_ROW_FPR, method="higher"))
        self.holdout_fpr_ = float(np.mean(scores > self.threshold_))
        self.vocab_ = _vocabulary(reference)  # a larger vocabulary can only lower scores
        return self

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        counts, novel_by_text = _novel_counts(batch, self.vocab_)
        scores = counts @ self.weights_.to_numpy()
        flags = scores > self.threshold_
        if not flags.any():
            return self.result(False, row_flags=flags, row_scores=scores)
        flagged = np.flatnonzero(flags)
        novel = _novel_cells(batch, flagged, novel_by_text)
        reasons = {
            pos: "S-l1 tokens never seen in reference free text: "
            + "; ".join(f"{col} {', '.join(tokens)}" for col, tokens in cells.items())
            + f" (novelty {scores[pos]:.1f} > {self.threshold_:.1f})"
            for pos, cells in novel.items()
        }
        alert = self.alert(
            f"{len(flagged)} rows contain tokens never seen in reference free text",
            check_id="S-l1",
            columns=sorted({col for cells in novel.values() for col in cells}),
            rows=flagged[:20].tolist(),
            n_rows=len(flagged),
            observed={pos: {col: [_mask(tok) for tok in tokens] for col, tokens in novel[pos].items()} for pos in flagged[:5].tolist()},
            expected=f"novelty <= {self.threshold_:.1f}, the {1 - config.TARGET_ROW_FPR:.1%} quantile on held-out reference rows",
            score=float(scores[flagged].max()),
        )
        return self.result(True, alerts=[alert], row_flags=flags, row_scores=scores, row_reasons=reasons)


# --------------------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------------------
def _positions_to_review(n_rows: int, review_rows: np.ndarray | None) -> np.ndarray:
    """Sorted, de-duplicated, in-range positions (every row when review_rows is None)."""
    if review_rows is None:
        return np.arange(n_rows)
    positions = np.unique(np.asarray(review_rows, dtype=int))
    return positions[(positions >= 0) & (positions < n_rows)]


class LlmPiiReview(Detector):
    problem = "l"
    family = "llm"
    name = "llm_pii_review"
    description = (
        "Gemini reads only the free-text fields of each reviewed row and flags names, phone numbers "
        "or e-mails of private individuals."
    )

    def detect(self, batch: pd.DataFrame, review_rows: np.ndarray | None = None) -> DetectionResult:
        nothing = np.zeros(len(batch), dtype=bool)
        positions = _positions_to_review(len(batch), review_rows)
        cols = [c for c in config.FREE_TEXT_COLUMNS if c in batch.columns]
        if not cols:  # no free-text field arrived: nothing the model could read
            return self.result(False, row_flags=nothing, reviewed=nothing)

        unique_columns = batch.loc[:, ~batch.columns.duplicated()]  # a record has one value per name: first copy
        rows = unique_columns[cols].iloc[positions].to_dict("records")
        records = [{"id": int(pos), **row} for pos, row in zip(positions, rows)]
        abstained: dict[int, str] = {}
        try:
            verdicts, usage, _ = llm.review_rows(LLM_TASK, records, tag=f"{self.problem}/{self.name}", abstained=abstained)
        except llm.LLMUnavailable as exc:
            return self.result(False, row_flags=nothing, reviewed=nothing, error=f"llm_unavailable: {exc}")
        answered = [pos for pos in positions.tolist() if pos in verdicts]

        # Only rows with a readable verdict are reviewed; the rest abstained (never read as "clean").
        reviewed, flags = nothing.copy(), nothing.copy()
        reviewed[answered] = True
        reasons, evidence = {}, {}
        for pos in answered:
            verdict = verdicts[pos]
            if verdict.get("flagged") is True:
                flags[pos] = True
                reasons[pos] = str(verdict.get("reason") or "personal data in free text")
                evidence[pos] = {key: verdict.get(key) for key in ("observed", "expected", "confidence")}
        alerts = [self._alert(evidence, len(answered), len(positions) - len(answered), cols)] if evidence else []
        return self.result(
            bool(alerts), alerts=alerts, row_flags=flags, row_reasons=reasons, row_evidence=evidence,
            reviewed=reviewed, abstained=abstained, usage=usage,
        )

    def _alert(self, evidence: dict[int, dict], n_reviewed: int, n_unanswered: int, cols: list[str]) -> Alert:
        rows = sorted(evidence)
        confidences = [e["confidence"] for e in evidence.values() if isinstance(e["confidence"], (int, float))]
        return self.alert(
            f"LLM review found personal data in {len(rows)} of {n_reviewed} reviewed rows"
            + (f" ({n_unanswered} rows got no verdict)" if n_unanswered else ""),
            check_id="L-l1",
            columns=cols,
            rows=rows[:20],
            n_rows=len(rows),
            observed={pos: _mask(evidence[pos]["observed"]) for pos in rows[:5]},
            expected="no name, phone number or e-mail of a private individual",
            score=float(np.mean(confidences)) if confidences else None,
        )


DETECTORS = [RulePiiRegex, StatPiiTemplateNovelty, LlmPiiReview]
