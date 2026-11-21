# Project B design spec

This is the contract every module of `project_b/` is built against. If code and
this file disagree, one of them is a bug.

```
raw parquet ─► to_feed ─► clean.sanitize ─► clean pool ─┬─► reference (Jan–Aug) ─► detector.fit()
(strings as     (typed)    (drops rows with              └─► evaluation (Sep–Dec) ─► batches of 5,000
 delivered)                 pre-existing issues)                     │
                                                                     ├─► inject.inject(batch, problem) ─► Injected(df, labels)
                                                                     └─► clean batches (no injection)
                                     detector.detect(batch) ─► DetectionResult ─► metrics ─► results/*.json ─► RESULTS_B.md
```

## 1. Data

* Source: NYC Open Data `erm2-nwe9`, rows with `created_date` in 2023: **3,224,723 rows**,
  25 columns (`config.COLUMNS`). Downloaded by `project_b/data/download.py` into
  `data/raw/nyc311_2023.parquet` (every value a string exactly as delivered; empty → null;
  sorted by `created_date`, `unique_key`).
* **Feed contract** (`config.FEED_DTYPES`): `unique_key` int64; `latitude`, `longitude`,
  `x_coordinate_state_plane`, `y_coordinate_state_plane` float64; everything else string
  (pandas 3 `str` dtype). Date columns (`config.DATETIME_COLUMNS`) are ISO strings
  `YYYY-MM-DDTHH:MM:SS.fff` (`config.SOURCE_DATETIME_FORMAT` / `SOURCE_DATETIME_REGEX`).
  `data.load.to_feed(raw)` produces it.
* **Split**: reference = `created_date < 2023-09-01` (Jan–Aug, used ONLY for `fit`, building
  lookups, training models, calibrating thresholds); evaluation = Sep–Dec (batches).
  Never fit or tune on evaluation data.
* **Batch** = 5,000 contiguous rows in arrival (`created_date`) order, positional index 0..n-1.

### pandas 3 notes (installed: pandas 3.0.1, numpy 1.26.4, scikit-learn 1.4.2, scipy 1.17)
* Default string dtype is `str` (pyarrow-backed), **not** `object`. Use
  `pd.api.types.is_string_dtype / is_float_dtype / is_integer_dtype / is_object_dtype`.
  A column holding a mix of floats and strings has dtype `object`.
* Copy-on-Write is always on: chained assignment (`df[c][mask] = x`) silently does nothing.
  Use `df.loc[mask, c] = x`.
* Missing values in `str` columns are `NaN`; test with `.isna()`.

## 2. Pre-existing issues and the clean pool — `project_b/data/clean.py`

```python
ISSUE_DEFINITIONS: dict[str, str]                       # issue id -> one-line definition
def issue_flags(feed: pd.DataFrame) -> pd.DataFrame     # bool frame, same index, one column per issue
def sanitize(feed: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]   # (clean rows, removed count per issue)
```

`feed` is the full year (frequency-based definitions use full-year counts). Principle:
**a row is clean for problem p if it would not count as an instance of p under the
injector's ground-truth definition** – otherwise a detector that correctly flags a real,
pre-existing issue would be charged with a false alarm. Required issue ids:

| id | definition |
|---|---|
| `dup_unique_key` | `unique_key` already seen earlier (all but first occurrence) |
| `exact_duplicate_row` | identical to an earlier row in every column (all but first) |
| `bad_datetime_format` | a non-null date column value does not match the source ISO format / does not parse |
| `closed_before_created` | parsed `closed_date` < parsed `created_date` |
| `latlon_outside_nyc` | non-null lat/lon outside the `config` NYC box (includes already-swapped pairs) |
| `latlon_half_missing` | exactly one of lat / lon is null |
| `zip_invalid_format` | non-null `incident_zip` not matching `^\d{5}$` |
| `zip_borough_rare_pair` | valid ZIP, borough in `config.BOROUGHS`, and the (zip, borough) pair is < 2 % of that ZIP's rows (full year) |
| `complaint_descriptor_rare_pair` | (complaint_type, descriptor) pair occurs < 5 times in the full year |
| `resolution_status_conflict` | status in CLOSED∪OPEN groups, resolution text seen ≥ 20 times, and ≥ 95 % of that text's rows are in the *other* status group; or the text occurs < 5 times |
| `pii_like_free_text` | a free-text column contains an e-mail or phone-like pattern and that exact text occurs < 20 times in the full year (frequent template texts with public office numbers are kept) |
| `near_duplicate_candidate` | same agency, complaint_type, descriptor, borough and normalised incident_address as an earlier row, created ≤ 300 s later (all but first); complaint_type and descriptor are compared ignoring case (HPD re-entered Sep/Oct 2023 requests in Title Case) |

Null spikes (g) are not sanitised (they are a batch property). The removed counts are
themselves Task 1 findings and are reported by the profiler.

## 3. Corruption injector — `project_b/inject.py`

```python
@dataclass
class Injected:
    df: pd.DataFrame              # corrupted batch, positional index 0..len-1
    row_labels: np.ndarray        # bool[len(df)]: rows that carry the corruption (ground truth)
    batch_label: bool             # True if anything was injected
    problem: str                  # 'a'..'l'
    params: dict                  # exactly what was done (variant, column, agency, switch row, ...)
    row_variant: np.ndarray | None = None   # object[len(df)]: variant per injected row, None elsewhere

class InjectionContext:           # built ONCE from the full-year, UNsanitised feed
    @classmethod
    def build(cls, full_feed: pd.DataFrame) -> "InjectionContext": ...

def inject(batch, problem, rng, ctx, rate=config.ROW_INJECTION_RATE, **params) -> Injected
def as_clean(batch) -> Injected   # labels all False, batch_label False
INJECTORS: dict[str, Callable]    # problem -> function(batch, rng, ctx, rate, **params) -> Injected
```

Rules for all injectors: never mutate the input batch; keep the feed dtypes except where the
corruption is the dtype; row-level count `k = max(1, round(rate * len(batch)))` capped by the
number of eligible rows; pick variants uniformly unless `variant=` is passed; everything is
driven by the passed `rng` (reproducible). Dates are written back in the source format.

| p | level | what the injector does (variants) | `row_labels` |
|---|---|---|---|
| a | batch | `rename`: one random column renamed to a plausible alias (`incident_zip→zip_code`, `descriptor→Descriptor`, `borough→boro`, `latitude→lat`, `complaint_type→problem`, display-name style `Incident Zip`, …); `drop`: one random column dropped | all False |
| b | batch | `num_as_str`: a numeric column (lat/lon/x/y/unique_key) delivered as strings; `num_as_str_partial`: only rows after a random point become strings (object dtype); `zip_as_number`: `incident_zip` delivered as numbers (float, because of nulls) | all False |
| c | batch | from a switch row in [20 %, 80 %] of the batch, `created_date` and `closed_date` (and `resolution_action_updated_date`) are re-formatted: `us_12h` `%m/%d/%Y %I:%M:%S %p`; `dmy_24h` `%d-%m-%Y %H:%M`; `iso_space_no_ms` `%Y-%m-%d %H:%M:%S`; `iso_day_month_swapped` `%Y-%d-%mT%H:%M:%S.000` (looks valid when day ≤ 12 – a silent failure) | rows ≥ switch row whose value changed |
| d | row | swap `latitude`/`longitude` on rows where both are non-null and inside the NYC box (so every swap lands outside it) | swapped rows |
| e | row | exact copies of k random rows inserted at random later positions | the later copy of each pair |
| f | row | copies of k rows with a new `unique_key`, `created_date` (+ `closed_date`) shifted by 1–300 s, and with probability ½ a small text edit to `incident_address` (`STREET→ST`, `AVENUE→AVE`, doubled space, one-character typo) or case change of `descriptor`; inserted right after the original in time order. A copy whose address got the one-character typo is moved 5–15 m (a mistyped address is geocoded separately): its x/y move in whole feet and its latitude/longitude are derived from the new x/y with the EPSG:2263 inverse projection, keeping the original row's residual, so it keeps the exact coordinate relation real rows have (`params['coord_shift_m']`). Variants `time_only`, `time_and_text` | the copies |
| g | batch | pick an agency with ≥ 100 rows in the batch (prob ∝ size) and a column whose reference null rate for that agency is < 10 % (candidates: `incident_zip`, `incident_address`, `latitude`+`longitude` together, `descriptor`, `location_type`, `city`, `community_board`, `resolution_description`); null out a fraction U(0.3, 0.9) of that agency's non-null values (override with `fraction=`) | nulled rows |
| h | row | `closed_date = created_date − Δ`: `minutes` Δ∈U(1,59) min; `days` Δ∈U(1,30) d; `year_typo` closed year set to 2022 | modified rows |
| i | row | `zip_replaced`: ZIP replaced by a ZIP whose full-year majority borough (≥ 95 %) differs and whose pair with the row's borough never occurs in 2023; `borough_replaced`: borough replaced by a different borough with zero full-year co-occurrence with the ZIP. Only rows with a 5-digit ZIP and a real borough | modified rows |
| j | row | descriptor replaced by one that never co-occurs with the row's complaint_type in 2023: `cross_agency` (taken from another agency's complaint types) or `same_agency` (another complaint type of the same agency – harder). Generic catch-all descriptors ('Other', 'N/A', 'Other (complaint details)', 'Other/Unknown', 'Other - Explain Below', 'General Complaint', ...) are never used: they contradict nothing | modified rows |
| k | row | `closed_with_open_text`: a Closed row gets one of its OWN agency's open texts, chosen uniformly; `open_with_closed_text`: a Closed row whose text is closed gets status `Open`/`In Progress`/`Assigned` and its `closed_date` nulled (so the only evidence is the text). A text qualifies only if it is ≥ 95 % in that status group with ≥ 20 rows over the year AND on January–August rows alone, and it passes the meaning screen (§3a) | modified rows |
| l | row | synthetic personal data put into `resolution_description` (appended sentence or replacing a clause): `phone` (fictional 555 numbers in several formats), `email`, `name_with_cue` ("Spoke with tenant Maria Gonzalez."), `name_bare` ("Per Anita Desai, condition corrected."), `mixed`. Names come from a small hard-coded synthetic list | modified rows |

Detector authors must NOT read `inject.py` (and the injector author must not read the
detectors): the two sides are built independently against this table, like a red team and
a blue team, so detectors cannot overfit injector artefacts.

### 3a. Ground-truth screens for problem k (resolution text vs status)

A text that is statistically "open" (or "closed") does not always SAY so: "Please call 311 for
further information" is >= 95 % open, yet contradicts nothing on a Closed row. Such labels would
charge a meaning-based detector (the LLM) with misses for being right. So:

* Candidates: every resolution text with >= 20 January-August rows, >= 95 % of them in one status
  group (29 open, 415 closed in this snapshot). January-August only, so screening never needs
  evaluation data.
* Criterion, written before any text was read: keep an open text only if the text itself says the
  request is not finished (under review, scheduled, assigned, the agency will visit / contact, a
  work order with a future date, waiting for a form); keep a closed text only if it says the
  agency's handling is finished (action taken, inspection with an outcome, complaint closed, no
  further action). Exclude instructions, status pointers, acknowledgements, referrals, duplicates
  of still-open requests, failed / no-access visits, future steps and anything ambiguous.
* Decisions: open 23 kept / 6 excluded; closed 239 kept / 176 excluded; 30 of the kept closed texts
  were judged borderline by the second reader. One agent read every candidate and an independent
  reviewer re-read all 444 and agreed on every decision (it had already seen the exclusion list, so
  not fully blind). Stored as `NEUTRAL_OPEN_TEXTS` / `NEUTRAL_CLOSED_TEXTS` in `inject.py` and, with
  the texts, in `tests/data/k_screen_decisions.json`; a test checks the screens reproduce every decision.
* Guard: `READ_OPEN_CANDIDATES` / `READ_CLOSED_CANDIDATES` hold the count and a fingerprint of the
  candidates that were read; `InjectionContext.build` warns when a data snapshot has others.
* The screens only restrict which rows may carry an injected contradiction; no real record is
  removed or changed. Every batch still receives its full 2 % of contradictions.

## 4. Detectors — `project_b/detectors/*.py`

Every problem gets exactly three detectors, one per family, subclasses of `core.Detector`.
Module layout and class names:

| module | problems |
|---|---|
| `schema.py` | a, b, c |
| `location.py` | d, i |
| `duplicates.py` | e, f |
| `completeness.py` | g, h |
| `semantic.py` | j, k |
| `pii.py` | l |

Each module ends with `DETECTORS = [ClassA, ClassB, ...]` (classes, not instances);
`project_b/detectors/__init__.py` collects them. Names: `rule_*`, `stat_*`, `llm_*`.

### Conventions (all families)
* `detect()` must never raise on a corrupted batch (missing/renamed columns, wrong dtypes,
  unparseable values): treat what it cannot check as "not flagged" for that check, or
  flag it if that is the honest reading, and say so in the alert. `timed_detect` records
  any exception as an error result, but an error is a failure of the detector.
* Row-level detectors return `row_flags` (bool[len(batch)]) and, for flagged rows,
  `row_reasons[pos]`; plus ONE aggregated `Alert` per batch (count, ≤ 20 example rows,
  columns, observed, expected, check_id). Batch-level detectors return one `Alert` per
  finding (e.g. per mismatched column, per (agency, column) spike).
* `batch_flag` = True iff at least one alert was raised.
* Explanations are scored (see §6): fill `check_id`, `columns`, `rows`/`segment`,
  `observed`, `expected`, `score` whenever you have them.
* Thresholds of statistical detectors are set in `fit()` from the reference data:
  row-level ones to a reference false-positive rate of `config.TARGET_ROW_FPR` (0.1 %),
  batch-level ones so that ≤ `config.TARGET_BATCH_FAR` of reference batches alert.
  Rules may not learn thresholds from data (except §j's frozen allow-list and the hand-checked
  list of public office numbers in `rule_pii_regex`); that is the point of the comparison.
  Nothing may be chosen or justified with evaluation-period data (created ≥ 2023-09-01). Put tunables as UPPER_CASE constants at the top of the module.
* Performance target on this laptop: rule/stat `detect()` on 5,000 rows < 1 s
  (near-duplicates < 3 s); `fit()` < 60 s (subsample the reference if needed).
* `fit()` receives the sanitised reference feed (~2.1 M rows).

### LLM detectors
* Use `project_b.llm.review_rows(task, records, tag=f"{problem}/{name}")` for per-row review
  and `project_b.llm.review_batch(task, payload, tag=...)` for batch review. Send only the
  columns the check needs (cost), with an integer `id` per row (use the batch position).
* Per-row detectors review only `review_rows` (int positions passed by the harness; if None
  review every row). Flag a row iff the model said `flagged: true`. Copy the model's `reason`
  to `row_reasons` and `{"observed","expected","confidence"}` to `row_evidence`.
* **Abstain, never guess.** Mark in `reviewed` only the rows that got a readable verdict. A row
  the model was asked about but gave none for is an abstention: pass
  `abstained={}` to `review_rows`, which fills `{id: cause}`. The cause is
  `unreadable_reply:<finishReason>` for every row of a reply without a parseable verdict list
  (e.g. `MAX_TOKENS`, `RECITATION`), or `no_verdict_in_reply` for a row a readable reply skipped.
  Return it as `DetectionResult.abstained`. A reply without a verdict for any row is not an
  error: every row abstains, nothing is flagged, its tokens still count.
* Window detectors (e, f): `review_rows` is a set of contiguous positions; split it into runs
  of consecutive positions, show each run (≤ `config.LLM_BATCH_SAMPLE_ROWS` rows) in order and
  ask which rows duplicate an earlier row in the same run.
* Batch-sample detectors (a, b, c, g): ignore `review_rows`; build a deterministic sample
  (`np.random.default_rng(config.SEED)` or evenly spaced positions) and call `review_batch`.
* Catch `project_b.llm.LLMUnavailable` and return a result with
  `error="llm_unavailable: ..."` (never invent an answer). Add `reply` usage to `result.usage`.
* `Alert.deterministic=False` for LLM alerts (the base class does this).

### Per-problem detector designs

**a — renamed or dropped column (batch)**
* `rule_schema_columns`: expected = `config.COLUMNS`; alert on missing and unexpected
  columns; pair a missing with an unexpected column as a "likely rename" when their
  normalised names are similar (difflib ≥ 0.6) – check_id `R-a1`.
* `stat_column_profile_match`: fit a profile per column (dtype kind, null rate, distinct
  ratio, mean string length, share numeric-looking, top values) from reference batches;
  for a missing column, find the unexpected column whose profile matches best and report
  the similarity ("`zip_code` has the profile of `incident_zip`, sim 0.97"); flag missing
  profiles even if names look fine.
* `llm_schema_review`: the model sees the contract (column names + dtypes), the batch's
  actual header + dtypes and 3 sample rows.

**b — column type change (batch)**
* `rule_dtype_contract`: compare each present column's dtype kind with `config.FEED_DTYPES`;
  report observed dtype, expected dtype, 3 example values with their Python types – `R-b1`.
* `stat_value_type_profile`: fit per column the share of non-null values by Python type /
  numeric-parseability from reference batches; flag columns whose type mix moves beyond the
  reference range (detects partial drift and value-level type changes).
* `llm_type_review`: contract dtypes vs actual dtypes plus sample values shown with `repr`.

**c — date format switch partway through (batch, rows after the switch labelled)**
* `rule_datetime_format`: every non-null value in each date column must match
  `SOURCE_DATETIME_REGEX` and parse with `SOURCE_DATETIME_FORMAT`; flag non-conforming rows,
  report first offending position, count, an example, a guessed format – `R-c1`.
* `stat_datetime_shape_shift`: fit the distribution of "shape signatures" (digit→9,
  letter→A, punctuation kept) per date column; flag rows with a shape that is rare/unseen in
  reference, AND rows whose parsed `created_date` is incoherent with the batch's arrival-order
  timeline (deviation from a rolling median > 2 days) – this catches day/month swaps that keep
  the ISO shape. Batch flag from a calibrated share / change point.
* `llm_datetime_review`: 40 evenly spaced rows in arrival order (position, created_date,
  closed_date); ask whether the format is consistent and where it changes.

**d — latitude/longitude swapped (row)**
* `rule_nyc_bbox`: flag non-null coordinates outside the NYC box; reason says "swapped"
  when (lon, lat) would be inside – `R-d1`.
* `stat_coordinate_outlier`: robust (median/MAD) model of lat/lon learned from reference
  (optionally per borough, or consistency with the state-plane x/y via a fitted linear map);
  threshold calibrated to the target FPR.
* `llm_coordinate_review`: per row `borough, incident_zip, latitude, longitude`.

**e — exact duplicates (row)**
* `rule_exact_duplicate_rows`: `duplicated(keep="first")` over all columns (convert
  unhashable values to strings), plus repeated `unique_key` reported separately – `R-e1`, `R-e2`.
* `stat_bloom_duplicate`: streaming, memory-bounded dedup with a Bloom filter (numpy bit
  array, k hashes from blake2b of the canonical row string) sized for a target false-positive
  probability of 1e-4; flags rows whose fingerprint "might have been seen". This is the
  at-scale way to dedup across batches and has a measurable false-positive rate.
* `llm_duplicate_review`: window review (see LLM conventions).

**f — near-duplicates (row)**
* `rule_near_duplicate_key`: blocking key (agency, complaint_type, descriptor, normalised
  address, borough); flag a row if an earlier row with the same key is ≤ 300 s older and has
  a different `unique_key` – `R-f1`. (Rows without an address use rounded lat/lon.)
* `stat_near_duplicate_similarity`: candidates = rows with the same complaint_type and
  borough within a 600 s window; similarity from address character-3-gram Jaccard,
  descriptor / location_type equality, coordinate distance and time gap; threshold calibrated
  on reference batches. Catches text-edited copies the exact key misses.
* `llm_near_duplicate_review`: window review; "same real-world request entered twice
  (timestamp or text slightly different)" vs different requests.

*f, added after the clean-alert review - one building, one request.*
Found while reviewing the clean Sep-Dec batches that still alerted. Decided and measured on
January-August only.
* **Spellings.** `STREET_ABBREVIATIONS` also maps the truncated forms PKW -> PKWY and AV -> AVE.
  They are the only truncations of the mapped street types in the Jan-Aug clean pool (the last
  word of 15 and 8 addresses). Before, "3708 FORT HAMILTON PKW" and "... PARKWAY" were two
  locations to R-f1.
* **Building numbers.** `DIFFERENT_BUILDING_IS_NOT_A_COPY`: two addresses whose house numbers
  (digits and hyphens) differ are different buildings, so never the same request. Consecutive
  reports along one block that share a block-level geocode had scored 0.82 on the other terms.
  - The injector's typo edits only letters, so no injected copy is affected.
  - A real re-entry with a mistyped building number is no longer caught. At the same point it
    cannot be told from the next building on a block-geocoded street; at another point it was
    already missed.
* **Measured on Jan-Aug** (fit on Jan-Jun, 108 clean Jul-Aug batches; `python -m project_b.audits.near_duplicate_change`):
  - flagged clean rows 260 -> 48; clean batches with any f flag 96 -> 31 of 108;
  - injected near-duplicates (10,800 at 2 %) caught: rule 86.7 %, statistic 88.9 %, both unchanged;
  - similarity threshold unchanged at 0.80;
  - no clean Jul-Aug pair was affected by the new spellings.
* **Unchanged:** the sanitiser (clean.py) and the injector, so the dataset, the clean pool and
  every ground-truth label stay as they were.

**g — null spike in one agency's records (batch)**
* `rule_null_rate_thresholds`: hand-set maximum null rate per column (one global number per
  column, written in the module, NOT learned per agency), checked per agency with ≥ 50 rows
  in the batch – `R-g1`.
* `stat_null_spike_binomial`: per (agency, column) reference null rate and its batch-to-batch
  spread; flag when the batch rate exceeds the reference range and a binomial / beta-binomial
  test gives p < 1e-6 after Bonferroni over cells; cells with < 30 rows skipped. Alert names
  agency, column, observed vs baseline rate, p-value.
* `llm_null_spike_review`: a stratified sample (≤ 6 rows per agency, relevant columns, nulls
  visible) plus per-agency row counts; "does one agency show a spike of missing values?".

**h — closed date earlier than created date (row)**
* `rule_closed_after_created`: parse both; flag `closed < created` – `R-h1`.
* `stat_duration_outlier`: per complaint_type distribution of closed − created learned from
  reference; flag the extreme lower tail, calibrated to the target FPR.
* `llm_date_order_review`: per row `created_date, closed_date, status`.

**i — ZIP inconsistent with borough (row)**
* `rule_zip_prefix_borough`: hand-coded USPS prefixes (100–102 MANHATTAN, 103 STATEN
  ISLAND, 104 BRONX, 112 BROOKLYN, 110/111/113/114/116 QUEENS); skip invalid ZIPs and
  `Unspecified` borough – `R-i1`. Not learned from data: its boundary-ZIP false alarms are a
  finding, not something to tune away.
* `stat_zip_borough_frequency`: P(borough | zip) from reference counts with smoothing;
  flag below a calibrated threshold when the ZIP has enough support.
* `llm_zip_borough_review`: per row `incident_zip, borough, city, incident_address`.

**j — Complaint Type inconsistent with Descriptor (row)**
* `rule_complaint_descriptor_allowlist`: frozen allow-list of (complaint_type, descriptor)
  pairs seen ≥ 2 times in the reference period (a declarative lookup table that would be
  reviewed and versioned in production); flag pairs not in it – `R-j1`.
* `stat_descriptor_classifier`: a model that generalises to unseen descriptors, e.g.
  char n-gram TF-IDF + multinomial NB / logistic regression predicting complaint_type from
  descriptor; flag when P(observed type | descriptor) is below a calibrated threshold.
* `llm_complaint_descriptor_review`: per row `agency, complaint_type, descriptor`.

**k — Resolution Description contradicting Status (row)**
* `rule_resolution_status_keywords`: hand-written phrase lists for "work finished" vs
  "still open / scheduled / under review" language; flag Closed rows with only open language
  and open-status rows with only finished language – `R-k1`.
* `stat_resolution_status_classifier`: TF-IDF + logistic regression predicting status group
  (closed vs open) from the resolution text, trained on reference; flag when P(observed group)
  is below a calibrated threshold.
* `llm_resolution_status_review`: per row `status, resolution_description, closed_date present?`.

**l — personal data in free-text fields (row)**
* `rule_pii_regex`: e-mail, NANP phone (several formats; ignore 311/911), and name-cue
  patterns (`Mr./Ms./Mrs./Dr. Name`, `tenant|caller|complainant|resident|spoke with|contact Name Surname`)
  over `config.FREE_TEXT_COLUMNS` – `R-l1..3`.
* `stat_pii_template_novelty`: 311 free text is templated, so personal data shows up as
  tokens never seen in the reference vocabulary (capitalised word runs, long digit runs, `@`);
  score novel tokens per row; threshold calibrated to the target FPR.
* `llm_pii_review`: per row free-text fields; personal data = a private person's name, phone,
  e-mail; agency names, job titles and public office numbers are not personal data.

*l, added after the clean-alert review - three narrow fixes, one rejected.*
Every PII flag on the clean Sep-Dec batches that still alerted was a false positive. Each fix
below was decided and measured on January-August only (fit on Jan-Jun, 108 clean Jul-Aug batches,
injected PII at 2 %; `python -m project_b.audits.pii_false_positives`). PII alerts stay immediate.
* **C1** (`CUE_INSIDE_LABEL_IS_NOT_A_CUE`): a cue word that directly follows a capitalised word
  sits inside a Title Case label ("Food Contact Equipment Dirty", a DOHMH category, 13 Jan-Aug
  rows). It is not "contact <Name Surname>". A cue at the start of a sentence, after a full stop
  or after a lower-case word still counts.
* **C3** (`NOVELTY_SKIPS_PUBLIC_NUMBERS`): the novelty statistic gives no personal-data shape to a
  digit run that the explicit lists `PUBLIC_PHONE_NUMBERS` / `TOLL_FREE_AREA_CODES` call public.
  These are the lists the rule already uses. One toll-free number flagged 9 clean Jul-Aug rows.
* **P1**: (202) 366-2220 joins `PUBLIC_PHONE_NUMBERS`, by hand like the HPD offices. It is the U.S.
  DOT aviation consumer line in DCWP's referral template: 8 Jan-Aug rows, below the >= 10 cut.
* **Measured** (before -> after):
  - clean Jul-Aug rows flagged 21 -> 3; clean batches with a PII flag 20 -> 3 of 108;
  - injected PII caught unchanged for every kind (rule 79.7 %, novelty 98.9 %, either 99.4 %);
  - novelty threshold of the production fit unchanged at 0.
* **Rejected, C2'**: ignoring capitalisation in Title Case values of `descriptor` / `descriptor_2`.
  It would remove the remaining false positives, which come from new 311 category labels
  ("Age Restrictions"; in Sep-Dec "Allowed in Smoke Free Area", "Waste Disposal"). But a bare
  personal name inside such a value would then go unflagged. The injector never writes there, so
  the benchmark cannot measure that risk, and immediate PII alerts were not to be weakened.
  New category labels therefore still raise PII alerts until the reference vocabulary is
  refreshed. In production, that refresh would run on a rolling window of accepted batches.

## 5. Evaluation harness — `project_b/evaluate.py`, `project_b/metrics.py`

1. Load the full-year feed; `InjectionContext.build(full_feed)` (unsanitised);
   `clean.sanitize(full_feed)` → clean pool; split → `reference_clean`, `eval_clean`.
   Cache derived frames under `data/derived/`.
2. Fit every detector once on `reference_clean` (record fit seconds).
3. Batches: `make_batches(eval_clean)`. Per problem choose `config.N_CORRUPTED_BATCHES`
   batch indices; one shared set of `config.N_CLEAN_BATCHES` clean batches. All randomness
   from `np.random.default_rng([config.SEED, problem_idx, batch_idx])`.
4. Rule + stat: run each problem's detectors on its corrupted batches and on the shared clean
   batches (`timed_detect`). One record per (detector, batch) to `results/eval_records.jsonl`.
5. LLM (budgeted by `config.LLM_*`): the first `LLM_N_BATCHES` corrupted batches of each
   problem (the same batches rule/stat saw) plus clean batches. Per-row problems: review a
   stratified sample (injected + non-injected rows; clean-batch rows for false alarms).
   Window problems (e, f): windows around injected copies + random windows in clean batches.
   Batch-level problems: whole-batch verdicts. Rule/stat metrics are ALSO computed on exactly
   the rows the LLM reviewed, so the three families are compared on identical rows.
   Determinism: `LLM_REPEATS` repeated runs (`get_client().default_repeat = i`) on one batch.
6. Rate sweep (rule + stat, row-level problems, `config.RATE_SWEEP`, 5 batches each) and a
   null-spike intensity sweep (fraction 0.1 / 0.25 / 0.5 / 0.9).
7. Background on real data: rule + stat detectors on `N_CLEAN_BATCHES` UNsanitised evaluation
   batches → alerts per batch, flagged rows per 1,000, examples (real issues found).
8. `results/summary.json` + `results/summary_tables.md`.

## 6. Metrics (definitions used everywhere)

* **Detection rate** – row-level problems: recall = flagged ∩ injected / injected rows (over
  reviewed rows). Batch-level problems: corrupted batches with `batch_flag` / corrupted
  batches. Wilson 95 % interval reported.
* **LLM coverage and abstention** – coverage = rows (or batches) with a readable verdict / rows
  (or batches) asked, in the main corrupted and clean batches; abstention rate = 1 − coverage,
  with counts per cause. An LLM's detection, false-alarm and precision figures are
  **conditional on a readable verdict**. The **overall operational** figures (`operational` in
  `summary.json`) count every abstention as "no alert": a miss on an injected row, no false alarm
  on a clean one. Operational cost is per row asked. A detector error (other than an unreadable
  reply) is not an abstention: it stays a miss on every row it was asked to judge.
* **False-alarm rate on clean batches** – row-level: flagged rows / rows in clean batches
  (row FPR) and share of clean batches with any alert (batch FAR). Batch-level: share of
  clean batches with `batch_flag`. A clean batch on which the detector errored is left out
  of its false-alarm rates (an error is no verdict), unless it raised an alert anyway; the
  number of errored batches is reported. An errored corrupted batch counts as a miss.
  `llm_unavailable` (no key / no cached answer / quota / access refused) means "not run".
* **Precision at the injection rate** (row-level), computed on corrupted batches.
* **Localisation** (batch-level): the alert names the right column / agency.
* **Cost per million rows** – compute: single-core seconds per row × 1e6 / 3600 ×
  `COMPUTE_USD_PER_VCPU_HOUR`; LLM: measured tokens × `LLM_PRICES_PER_MTOK` per reviewed row
  (per-row review), per batch / 5,000 (batch review), per window row (window review).
* **Throughput on a laptop** – rule/stat: rows / `detect()` wall seconds (median over
  batches). LLM: rows per call / measured API latency, plus the free-tier ceiling (rows/day).
* **Explanation clarity (0–5)** – one point each: names the check (`check_id` or problem),
  localises (columns + rows/segment), shows the observed value, shows the expected
  value/baseline, reproducible (deterministic). LLM explanations are also checked for
  **grounding**: share of flagged rows whose `observed` text actually appears in the row.
* **Determinism** – share of identical verdicts across `LLM_REPEATS` repeated calls, over the
  rows with a readable verdict in every repeat.

## 7. Hybrid pipeline — `project_b/pipeline.py` (Task 5)
* One `Step` per problem: rule / statistical detectors run on every row, their flags OR-ed; an
  optional LLM detector CONFIRMS the flags of named detectors (it can only remove false alarms).
  The LLM judges each distinct value once per run (`LLM_DEDUP_ON`), at most
  `config.PIPELINE_MAX_CONFIRM_ITEMS` per batch; a value it cannot judge keeps its cheap flag.
* A schema or date-format break (a, b, c) quarantines the batch.
* The plan: `plan_from_summary(results/summary.json)` by the rule printed in DECISION_MATRIX.md
  (false-alarm limits, tie goes to the rule, union for c / f / l, LLM confirmation only when it
  measurably cuts false alarms without losing detection); `DEFAULT_PLAN` (every reason marked
  "provisional") until the benchmark exists; `PLAN_OVERRIDES` for a person's decision.
* Evaluation (`evaluate_pipeline`): the benchmark's own single-problem batches, clean batches,
  mixed batches (three types from c-l, each on its own rows; e / f last, with earlier labels
  following their rows into the copies; a / b excluded because a schema break quarantines the
  batch and could erase other evidence) and real unsanitised batches. Outputs
  `results/pipeline_records.jsonl`, `pipeline_summary.json`, `pipeline_tables.md`.

### 7a. Batch alerting layer (`AlertPolicy`)
Written down before any threshold was computed. It was added because the first pipeline
evaluation (Sep-Dec) raised an alert on every clean batch. That result motivated the layer; it
chose nothing in it.
* **Two layers.** The row layer is unchanged: the same detectors, the same row flags, the same
  row false-alarm rates; flagged rows are always kept on the rows. The alerting layer only decides
  whether a check *raises a batch alert*.
* **Critical checks alert immediately, at any count:**
  - a, b, c: schema, type and date-format breaks; they also quarantine the batch;
  - l: personal data in free text (one exposed record is already an incident);
  - g: its detector is already calibrated per batch.
* **Every other row-level check (d, e, f, h, i, j, k)** alerts only when the batch has at least
  `min_rows[p]` flagged rows. An alert a detector raises without row flags (a structural finding)
  still passes at once.
* **Calibration, January-August only.**
  - Fit the plan's rule / statistical detectors on the clean pool created before
    `ALERT_CALIBRATION_SPLIT` (2023-07-01, i.e. Jan-Jun).
  - Run each step on every 5,000-row clean batch from 2023-07-01 to `config.REFERENCE_END`
    (Jul-Aug: held out from that fit, still inside the reference period).
  - Count the flagged rows per batch and check. LLM confirmation is off during calibration (no
    Gemini call). Confirmation could only remove flags, so the clean-batch target still holds
    with it.
  - `min_rows[p]` = the smallest k >= 1 such that at most `int(config.TARGET_BATCH_FAR x n)`
    calibration batches have >= k flagged rows. This is the same 5 % rule and formula the
    batch-level statistical detectors already use, so no new tunable is introduced.
* **Evaluation.** The evaluation period (Sep-Dec) is read only after the thresholds are fixed,
  and only to measure them. Every pipeline record holds both the raw alert ("before": every flag
  alerts) and the layered alert ("after"), so the comparison comes from one run.
* **Cost of a threshold in detection.** Row recall cannot change. A batch alert can: a batch with
  fewer injected rows than `min_rows` alerts only if false flags make up the difference. An
  alert sweep injects each thresholded problem at `config.RATE_SWEEP` (0.1-10 %) into the
  benchmark's first `ALERT_SWEEP_BATCHES` corrupted batches and records the batch alert before
  and after.

## 8. Scale, reports, single command
* Raw snapshot: `dataset_manifest.json` (committed) describes it, and `project_b/data/snapshot.py`
  verifies it (SHA-256 and size) in `load.load_raw`, `evaluate.cache_key` and the download step. A
  missing or different file stops the run. `--download-fresh` writes the live data to a separate
  file, which replaces the snapshot only if it matches. `PROJECT_B_ALLOW_NEW_SNAPSHOT=1` is the
  explicit opt-in to run on other data.

* `project_b/scale.py` (Task 6): projections to 50 M rows/day x 120 customers; every figure is
  labelled measured / assumption / projection, and "pending" when its measurement is missing.
* `project_b/report.py`: fills `project_b/templates/*.template.md` from the result files only and
  writes RESULTS_B.md and DECISION_MATRIX.md; missing inputs say "not measured yet".
* `python -m project_b`: download -> profile -> evaluate -> pipeline -> scale -> report; LLM mode
  `replay` without a key (cache only, never a call), `auto` with one.
