# Project B - hybrid pipeline

Plan source: **measured**. Computed from `pipeline_records.jsonl` by `python -m project_b.pipeline`. 10 single-problem batches per problem (the detector benchmark's own), 10 clean, 20 mixed (3 corruption types each), 10 real unsanitised batches of 5,000 rows. LLM model gemini-3.5-flash-lite.

## Plan

| problem | detectors (flags OR-ed) | LLM confirms | why |
|---|---|---|---|
| a | rule_schema_columns | - | measured: rule_schema_columns detection 100.00%, false alarms 0.00%; stat_column_profile_match detection 100.00%, false alarms 0.00% -> rule_schema_columns; LLM confirmation does not apply (batch-level or window check) |
| b | rule_dtype_contract | - | measured: rule_dtype_contract detection 100.00%, false alarms 0.00%; stat_value_type_profile detection 100.00%, false alarms 0.00% -> rule_dtype_contract; LLM confirmation does not apply (batch-level or window check) |
| c | rule_datetime_format + stat_datetime_shape_shift | - | measured: rule_datetime_format detection 100.00%, false alarms 0.00%; stat_datetime_shape_shift detection 100.00%, false alarms 0.00% -> rule_datetime_format + stat_datetime_shape_shift; LLM confirmation does not apply (batch-level or window check) |
| d | rule_nyc_bbox | - | measured: rule_nyc_bbox detection 100.00%, false alarms 0.00%; stat_coordinate_outlier detection 100.00%, false alarms 0.07% -> rule_nyc_bbox; no LLM confirmation (precision 1.00 >= 0.9 or not measured) |
| e | rule_exact_duplicate_rows | - | measured: rule_exact_duplicate_rows detection 100.00%, false alarms 0.00%; stat_bloom_duplicate detection 100.00%, false alarms 0.00% -> rule_exact_duplicate_rows; LLM confirmation does not apply (batch-level or window check) |
| f | rule_near_duplicate_key + stat_near_duplicate_similarity | - | measured: rule_near_duplicate_key detection 85.30%, false alarms 0.00%; stat_near_duplicate_similarity detection 87.75%, false alarms 0.01% -> rule_near_duplicate_key + stat_near_duplicate_similarity; LLM confirmation does not apply (batch-level or window check) |
| g | stat_null_spike_binomial | - | measured: rule_null_rate_thresholds detection 50.00%, false alarms 0.00%; stat_null_spike_binomial detection 100.00%, false alarms 0.00% -> stat_null_spike_binomial; LLM confirmation does not apply (batch-level or window check) |
| h | rule_closed_after_created | - | measured: rule_closed_after_created detection 100.00%, false alarms 0.00%; stat_duration_outlier detection 100.00%, false alarms 0.63% -> rule_closed_after_created; no LLM confirmation (precision 1.00 >= 0.9 or not measured) |
| i | rule_zip_prefix_borough | - | measured: rule_zip_prefix_borough detection 100.00%, false alarms 0.06%; stat_zip_borough_frequency detection 100.00%, false alarms 0.06% -> rule_zip_prefix_borough; no LLM confirmation (precision 0.98 >= 0.9 or not measured) |
| j | rule_complaint_descriptor_allowlist | - | measured: rule_complaint_descriptor_allowlist detection 100.00%, false alarms 0.04%; stat_descriptor_classifier detection 99.75%, false alarms 0.06% -> rule_complaint_descriptor_allowlist; no LLM confirmation: precision 0.87 < 0.9; on the LLM's rows: LLM FPR 1.00% vs 0.00%, detection 74.00% vs 100.00% |
| k | stat_resolution_status_classifier | - | measured: rule_resolution_status_keywords detection 97.60%, false alarms 0.03%; stat_resolution_status_classifier detection 100.00%, false alarms 0.06% -> stat_resolution_status_classifier; no LLM confirmation (precision 0.97 >= 0.9 or not measured) |
| l | rule_pii_regex + stat_pii_template_novelty | - | measured: rule_pii_regex detection 80.35%, false alarms 0.00%; stat_pii_template_novelty detection 100.00%, false alarms 0.04% -> rule_pii_regex + stat_pii_template_novelty; no LLM confirmation (precision 0.98 >= 0.9 or not measured) |

## Detection and false alarms per problem

Row columns come from the row layer, which the alerting layer never changes. Batch columns show the alert before the alerting layer -> after it ("before" = every flag alerts; a single value = unchanged).

| problem | alert from (rows) | row detection (single) | row detection (mixed) | row FPR (clean) | batch alert, injected (single) | batch alert, injected (mixed) | batch FAR (clean) | batch FAR (mixed, not injected) | LLM values asked / reused / unanswered | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| a - Renamed or dropped column | every flag (critical) | - | - | - | 10/10 | - | 0/10 | 0/20 | 0 / 0 / 0 | 0 |
| b - Column type change | every flag (critical) | - | - | - | 10/10 | - | 0/10 | 0/20 | 0 / 0 / 0 | 0 |
| c - Date format switch partway through a batch | every flag (critical) | - | - | - | 10/10 | 8/8 | 0/10 | 0/12 | 0 / 0 / 0 | 0 |
| d - Latitude and longitude swapped | 1 | 100.0% [99.6-100.0] (1000/1000) | 100.0% [99.4-100.0] (593/593) | 0.00% [0.00-0.01] (0/50000) | 10/10 | 7/7 | 0/10 | 0/13 | 0 / 0 / 0 | 0 |
| e - Exact duplicates | 1 | 100.0% [99.6-100.0] (1000/1000) | 100.0% [99.4-100.0] (600/600) | 0.00% [0.00-0.01] (0/50000) | 10/10 | 6/6 | 0/10 | 0/14 | 0 / 0 / 0 | 0 |
| f - Near-duplicates | 3 | 86.5% [84.2-88.5] (865/1000) | 91.6% [88.8-93.7] (458/500) | 0.01% [0.00-0.02] (3/50000) | 10/10 | 5/5 | 2/10 -> 0/10 | 7/15 -> 3/15 | 0 / 0 / 0 | 0 |
| g - Null spike in one agency's records | every flag (critical) | - | - | - | 10/10 | 3/3 | 0/10 | 6/17 | 0 / 0 / 0 | 0 |
| h - Closed date earlier than created date | 1 | 100.0% [99.6-100.0] (1000/1000) | 100.0% [99.2-100.0] (502/502) | 0.00% [0.00-0.01] (0/50000) | 10/10 | 5/5 | 0/10 | 1/15 | 0 / 0 / 0 | 0 |
| i - ZIP code inconsistent with borough | 13 | 100.0% [99.6-100.0] (1000/1000) | 100.0% [99.2-100.0] (497/497) | 0.06% [0.04-0.09] (30/50000) | 10/10 | 5/5 | 9/10 -> 0/10 | 11/15 -> 0/15 | 0 / 0 / 0 | 0 |
| j - Complaint Type inconsistent with Descriptor | 47 | 100.0% [99.6-100.0] (1000/1000) | 100.0% [99.4-100.0] (676/676) | 0.02% [0.01-0.04] (11/50000) | 10/10 | 8/8 -> 7/8 | 5/10 -> 0/10 | 8/12 -> 0/12 | 0 / 0 / 0 | 0 |
| k - Resolution Description contradicting Status | 7 | 100.0% [99.6-100.0] (1000/1000) | 100.0% [98.9-100.0] (334/334) | 0.05% [0.03-0.07] (25/50000) | 10/10 | 4/4 | 9/10 -> 0/10 | 16/16 -> 2/16 | 0 / 0 / 0 | 0 |
| l - Personal data inside free-text fields | every flag (critical) | 100.0% [99.6-100.0] (1000/1000) | 100.0% [99.5-100.0] (703/703) | 0.02% [0.01-0.04] (11/50000) | 10/10 | 9/9 | 5/10 | 6/11 | 0 / 0 / 0 | 0 |

## Alerting layer: thresholds calibrated on January-August

min_rows = the smallest k >= 1 such that at most int(0.05 x n) calibration batches have >= k flagged rows; critical checks alert on every flag. Detectors fitted on the clean pool, created before 2023-07-01 (1,428,087 rows); flags counted on the clean pool, created 2023-07-01 to before 2023-09-01 (544,825 rows, 5,000-row batches); LLM confirmation off (no Gemini call). Critical checks alerting on every flag: a, b, c, g, l.

| check | calibration batches | flagged rows per batch: median / 95th pct / max | batches alerting at 1 flag | allowed (5% of n) | threshold (min flagged rows) | batches alerting at the threshold |
|---|---|---|---|---|---|---|
| d - Latitude and longitude swapped | 108 | 0 / 0 / 0 | 0/108 | 5 | 1 | 0/108 |
| e - Exact duplicates | 108 | 0 / 0 / 0 | 0/108 | 5 | 1 | 0/108 |
| f - Near-duplicates | 108 | 0 / 2 / 5 | 31/108 | 5 | 3 | 3/108 |
| h - Closed date earlier than created date | 108 | 0 / 0 / 0 | 0/108 | 5 | 1 | 0/108 |
| i - ZIP code inconsistent with borough | 108 | 1 / 12 / 69 | 76/108 | 5 | 13 | 5/108 |
| j - Complaint Type inconsistent with Descriptor | 108 | 14 / 46 / 377 | 99/108 | 5 | 47 | 5/108 |
| k - Resolution Description contradicting Status | 108 | 2 / 6 / 14 | 97/108 | 5 | 7 | 5/108 |

## What the thresholds cost: injected batches that alert, by injection rate

Each thresholded problem injected at every rate into the benchmark's first corrupted batches. Cell = injected batches alerted before -> after the alerting layer (median injected rows per batch).

| check | threshold | 0.1% | 0.5% | 2.0% | 10.0% |
|---|---|---|---|---|---|
| d - Latitude and longitude swapped | 1 | 5/5 (5 rows) | 5/5 (25 rows) | 5/5 (100 rows) | 5/5 (500 rows) |
| e - Exact duplicates | 1 | 5/5 (5 rows) | 5/5 (25 rows) | 5/5 (100 rows) | 5/5 (500 rows) |
| f - Near-duplicates | 3 | 5/5 -> 4/5 (5 rows) | 5/5 (25 rows) | 5/5 (100 rows) | 5/5 (500 rows) |
| h - Closed date earlier than created date | 1 | 5/5 (5 rows) | 5/5 (25 rows) | 5/5 (100 rows) | 5/5 (500 rows) |
| i - ZIP code inconsistent with borough | 13 | 5/5 -> 0/5 (5 rows) | 5/5 (25 rows) | 5/5 (100 rows) | 5/5 (500 rows) |
| j - Complaint Type inconsistent with Descriptor | 47 | 5/5 -> 0/5 (5 rows) | 5/5 -> 0/5 (25 rows) | 5/5 (100 rows) | 5/5 (500 rows) |
| k - Resolution Description contradicting Status | 7 | 5/5 -> 3/5 (5 rows) | 5/5 (25 rows) | 5/5 (100 rows) | 5/5 (500 rows) |

## End to end

* Clean batches with any alert: before the alerting layer 100.0% [72.2-100.0] (10/10), after it 50.0% [23.7-76.3] (5/10); quarantined: 0.0% [0.0-27.8] (0/10); alerts per clean batch: 3.1 -> 0.5.
* Cost per 1M rows: $0.000607 (LLM: 0 calls, 0 input / 0 output tokens over 753,100 rows; 0 distinct values sent per 1M rows).
* Throughput on this laptop (one core): 22,883 rows/s of local work, 22,883 rows/s including the recorded LLM waits; median batch 0.21 s of local work.

## Real, unsanitised batches (nothing injected)

Quarantined: 0.0% [0.0-27.8] (0/10) of 10 batches. Batches with any alert: 10/10 (before -> after the alerting layer). Real batches carry real issues, so these alerts are not all false.

| problem | alerts / batch (before -> after) | flagged rows / 1,000 |
|---|---|---|
| a | 0.00 -> 0.00 | 0.00 |
| b | 0.00 -> 0.00 | 0.00 |
| c | 0.00 -> 0.00 | 0.00 |
| d | 0.00 -> 0.00 | 0.00 |
| e | 0.00 -> 0.00 | 0.00 |
| f | 2.00 -> 2.00 | 55.34 |
| g | 0.00 -> 0.00 | 0.00 |
| h | 0.40 -> 0.40 | 0.28 |
| i | 0.70 -> 0.00 | 0.50 |
| j | 0.90 -> 0.00 | 0.52 |
| k | 1.00 -> 0.30 | 1.40 |
| l | 1.30 -> 1.30 | 0.58 |
