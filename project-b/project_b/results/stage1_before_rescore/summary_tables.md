# Project B - detector comparison

Computed from `eval_records.jsonl` by `python -m project_b.evaluate`. Seed 2023, 5,000-row batches (20 corrupted per problem, 20 clean), row injection rate 2%, LLM model gemini-3.5-flash-lite. Rates show the Wilson 95 % interval and (k/n). On row-level problems the LLM reads only a sample of each batch, so its batch FAR (any alert in a batch) would not compare with detectors that read every row and is left blank.

## a - Renamed or dropped column (batch-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_schema_columns | ok | 100.0% [83.9-100.0] (20/20) | - | 0.0% [0.0-16.1] (0/20) | - | 100.0% [83.9-100.0] (20/20) | 27,701,141 | $5.65e-07 | 5.0 | - |
| stat_column_profile_match | ok | 100.0% [83.9-100.0] (20/20) | - | 0.0% [0.0-16.1] (0/20) | - | 100.0% [83.9-100.0] (20/20) | 49,806 | $0.000304 | 5.0 | - |
| llm_schema_review | ok | 75.0% [40.9-92.9] (6/8) | - | 0.0% [0.0-32.4] (0/8) | - | 100.0% [61.0-100.0] (6/6) | 2,828 (free tier 2,500,000 rows/day) | $0.113 | 4.0 | 100.0% [20.7-100.0] (1/1) |

Same inputs as the LLM (a):

| detector | detection on the LLM's corrupted batches | FAR on its clean batches |
|---|---|---|
| rule_schema_columns | 100.0% [67.6-100.0] (8/8) | 0.0% [0.0-32.4] (0/8) |
| stat_column_profile_match | 100.0% [67.6-100.0] (8/8) | 0.0% [0.0-32.4] (0/8) |
| llm_schema_review | 75.0% [40.9-92.9] (6/8) | 0.0% [0.0-32.4] (0/8) |

## b - Column type change (batch-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_dtype_contract | ok | 100.0% [83.9-100.0] (20/20) | - | 0.0% [0.0-16.1] (0/20) | - | 100.0% [83.9-100.0] (20/20) | 1,753,097 | $7.45e-06 | 5.0 | - |
| stat_value_type_profile | ok | 100.0% [83.9-100.0] (20/20) | - | 0.0% [0.0-16.1] (0/20) | - | 100.0% [83.9-100.0] (20/20) | 233,573 | $5.95e-05 | 5.0 | - |
| llm_type_review | ok | 100.0% [67.6-100.0] (8/8) | - | 0.0% [0.0-32.4] (0/8) | - | 100.0% [67.6-100.0] (8/8) | 2,731 (free tier 2,500,000 rows/day) | $0.119 | 4.0 | 100.0% [20.7-100.0] (1/1) |

Same inputs as the LLM (b):

| detector | detection on the LLM's corrupted batches | FAR on its clean batches |
|---|---|---|
| rule_dtype_contract | 100.0% [67.6-100.0] (8/8) | 0.0% [0.0-32.4] (0/8) |
| stat_value_type_profile | 100.0% [67.6-100.0] (8/8) | 0.0% [0.0-32.4] (0/8) |
| llm_type_review | 100.0% [67.6-100.0] (8/8) | 0.0% [0.0-32.4] (0/8) |

## c - Date format switch partway through a batch (batch-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_datetime_format | ok | 100.0% [83.9-100.0] (20/20) | - | 0.0% [0.0-16.1] (0/20) | - | 100.0% [83.9-100.0] (20/20) | 141,836 | $0.000136 | 5.0 | - |
| stat_datetime_shape_shift | ok | 100.0% [83.9-100.0] (20/20) | - | 0.0% [0.0-16.1] (0/20) | - | 100.0% [83.9-100.0] (20/20) | 91,148 | $0.000176 | 5.0 | - |
| llm_datetime_review | ok | 100.0% [67.6-100.0] (8/8) | - | 0.0% [0.0-32.4] (0/8) | - | 100.0% [67.6-100.0] (8/8) | 1,889 (free tier 2,500,000 rows/day) | $0.441 | 3.8 (grounded 100%) | 100.0% [20.7-100.0] (1/1) |

Same inputs as the LLM (c):

| detector | detection on the LLM's corrupted batches | FAR on its clean batches |
|---|---|---|
| rule_datetime_format | 100.0% [67.6-100.0] (8/8) | 0.0% [0.0-32.4] (0/8) |
| stat_datetime_shape_shift | 100.0% [67.6-100.0] (8/8) | 0.0% [0.0-32.4] (0/8) |
| llm_datetime_review | 100.0% [67.6-100.0] (8/8) | 0.0% [0.0-32.4] (0/8) |

## d - Latitude and longitude swapped (row-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_nyc_bbox | ok | 100.0% [99.8-100.0] (2000/2000) | 0.00% [0.00-0.00] (0/100000) | 0.0% [0.0-16.1] (0/20) | 1.00 | - | 5,585,090 | $2.74e-06 | 5.0 | - |
| stat_coordinate_outlier | ok | 100.0% [99.8-100.0] (2000/2000) | 0.07% [0.05-0.09] (68/100000) | 90.0% [69.9-97.2] (18/20) | 0.96 | - | 1,334,116 | $1.04e-05 | 5.0 | - |
| llm_coordinate_review | ok | 100.0% [96.3-100.0] (100/100) | 0.00% [0.00-3.70] (0/100) | - | 1.00 | - | 5 (free tier 7,500 rows/day) | $195 | 3.0 (grounded 100%) | 100.0% [87.1-100.0] (26/26) |

Same inputs as the LLM (d):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_nyc_bbox | 100.0% [96.3-100.0] (100/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| stat_coordinate_outlier | 100.0% [96.3-100.0] (100/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| llm_coordinate_review | 100.0% [96.3-100.0] (100/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |

## e - Exact duplicates (row-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_exact_duplicate_rows | ok | 100.0% [99.8-100.0] (2000/2000) | 0.00% [0.00-0.00] (0/100000) | 0.0% [0.0-16.1] (0/20) | 1.00 | - | 192,385 | $7.31e-05 | 5.0 | - |
| stat_bloom_duplicate | ok | 100.0% [99.8-100.0] (2000/2000) | 0.00% [0.00-0.01] (1/100000) | 5.0% [0.9-23.6] (1/20) | 1.00 | - | 36,219 | $0.000382 | 5.0 | - |
| llm_duplicate_review | ok | 88.5% [71.0-96.0] (23/26) | 0.00% [0.00-0.40] (0/960) | - | 1.00 | - | 5 (free tier 20,000 rows/day) | $102 | 3.9 (grounded 100%) | 99.2% [95.4-99.9] (119/120) |

Window reach: a 40-row window shows an injected copy next to its original for 6.2% [4.8-8.1] (50/800) of copies. `llm_duplicate_review` (and the rule / stat rows it is compared on) is scored only on copies shown with their original, so this share caps what window review can catch in a full batch.

Same inputs as the LLM (e):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_exact_duplicate_rows | 100.0% [87.1-100.0] (26/26) | 0.00% [0.00-0.40] (0/960) | 1.00 |
| stat_bloom_duplicate | 100.0% [87.1-100.0] (26/26) | 0.00% [0.00-0.40] (0/960) | 1.00 |
| llm_duplicate_review | 88.5% [71.0-96.0] (23/26) | 0.00% [0.00-0.40] (0/960) | 1.00 |

## f - Near-duplicates (row-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_near_duplicate_key | ok | 85.3% [83.7-86.8] (1706/2000) | 0.00% [0.00-0.00] (0/100000) | 0.0% [0.0-16.1] (0/20) | 1.00 | - | 179,600 | $8.2e-05 | 5.0 | - |
| stat_near_duplicate_similarity | ok | 87.8% [86.2-89.1] (1755/2000) | 0.05% [0.04-0.06] (48/100000) | 85.0% [64.0-94.8] (17/20) | 0.95 | - | 93,560 | $0.000164 | 5.0 | - |
| llm_near_duplicate_review | ok | 66.7% [50.3-79.8] (24/36) | 1.35% [0.79-2.30] (13/960) | - | 0.53 | - | 9 (free tier 20,000 rows/day) | $137 | 3.9 (grounded 100%) | 98.3% [94.1-99.5] (118/120) |

Window reach: a 40-row window shows an injected copy next to its original for 90.5% [88.3-92.3] (724/800) of copies. `llm_near_duplicate_review` (and the rule / stat rows it is compared on) is scored only on copies shown with their original, so this share caps what window review can catch in a full batch.

Same inputs as the LLM (f):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_near_duplicate_key | 91.7% [78.2-97.1] (33/36) | 0.00% [0.00-0.40] (0/960) | 1.00 |
| stat_near_duplicate_similarity | 91.7% [78.2-97.1] (33/36) | 0.00% [0.00-0.40] (0/960) | 1.00 |
| llm_near_duplicate_review | 66.7% [50.3-79.8] (24/36) | 1.35% [0.79-2.30] (13/960) | 0.53 |

## g - Null spike in one agency's records (batch-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_null_rate_thresholds | ok | 50.0% [29.9-70.1] (10/20) | - | 0.0% [0.0-16.1] (0/20) | - | 100.0% [72.2-100.0] (10/10) | 528,969 | $3.12e-05 | 5.0 | - |
| stat_null_spike_binomial | ok | 100.0% [83.9-100.0] (20/20) | - | 0.0% [0.0-16.1] (0/20) | - | 100.0% [83.9-100.0] (20/20) | 399,591 | $4.22e-05 | 5.0 | - |
| llm_null_spike_review | ok | 0.0% [0.0-32.4] (0/8) | - | 0.0% [0.0-32.4] (0/8) | - | - | 2,700 (free tier 2,500,000 rows/day) | $0.382 | - | 100.0% [20.7-100.0] (1/1) |

Same inputs as the LLM (g):

| detector | detection on the LLM's corrupted batches | FAR on its clean batches |
|---|---|---|
| rule_null_rate_thresholds | 50.0% [21.5-78.5] (4/8) | 0.0% [0.0-32.4] (0/8) |
| stat_null_spike_binomial | 100.0% [67.6-100.0] (8/8) | 0.0% [0.0-32.4] (0/8) |
| llm_null_spike_review | 0.0% [0.0-32.4] (0/8) | 0.0% [0.0-32.4] (0/8) |

## h - Closed date earlier than created date (row-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_closed_after_created | ok | 100.0% [99.8-100.0] (2000/2000) | 0.00% [0.00-0.00] (0/100000) | 0.0% [0.0-16.1] (0/20) | 1.00 | - | 874,860 | $1.74e-05 | 5.0 | - |
| stat_duration_outlier | ok | 100.0% [99.8-100.0] (2000/2000) | 0.63% [0.59-0.68] (633/100000) | 100.0% [83.9-100.0] (20/20) | 0.74 | - | 669,854 | $2.25e-05 | 5.0 | - |
| llm_date_order_review | ok | 87.0% [79.0-92.2] (87/100) | 0.00% [0.00-3.70] (0/100) | - | 1.00 | - | 6 (free tier 7,500 rows/day) | $195 | 4.0 (grounded 100%) | 50.0% [32.1-67.9] (13/26) |

Same inputs as the LLM (h):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_closed_after_created | 100.0% [96.3-100.0] (100/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| stat_duration_outlier | 100.0% [96.3-100.0] (100/100) | 1.00% [0.18-5.45] (1/100) | 0.67 |
| llm_date_order_review | 87.0% [79.0-92.2] (87/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |

## i - ZIP code inconsistent with borough (row-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_zip_prefix_borough | ok | 100.0% [99.8-100.0] (2000/2000) | 0.06% [0.05-0.08] (63/100000) | 90.0% [69.9-97.2] (18/20) | 0.98 | - | 1,256,582 | $1.41e-05 | 5.0 | - |
| stat_zip_borough_frequency | ok | 100.0% [99.8-100.0] (2000/2000) | 0.06% [0.05-0.08] (59/100000) | 85.0% [64.0-94.8] (17/20) | 0.98 | - | 723,736 | $2.37e-05 | 5.0 | - |
| llm_zip_borough_review | partial: llm_no_verdicts: unparseable reply for ids 35..4933 | 63.0% [53.2-71.8] (63/100) | 0.00% [0.00-3.70] (0/100) | - | 1.00 | - | 5 (free tier 7,263 rows/day) | $184 | 3.8 (grounded 100%) | 100.0% [87.1-100.0] (26/26) |

Errors: `llm_zip_borough_review` errored on 1 of 8 corrupted batches (scored as misses) and 0 of 8 clean batches (left out of its false-alarm rates: an error is no verdict, not a correct "no alarm"). An alert raised in spite of an error still counts in the batch rates.

Same inputs as the LLM (i):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_zip_prefix_borough | 100.0% [96.3-100.0] (100/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| stat_zip_borough_frequency | 100.0% [96.3-100.0] (100/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| llm_zip_borough_review | 63.0% [53.2-71.8] (63/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |

## j - Complaint Type inconsistent with Descriptor (row-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_complaint_descriptor_allowlist | ok | 100.0% [99.8-100.0] (2000/2000) | 0.04% [0.03-0.06] (42/100000) | 75.0% [53.1-88.8] (15/20) | 0.87 | - | 86,909 | $0.000163 | 5.0 | - |
| stat_descriptor_classifier | ok | 99.8% [99.4-99.9] (1995/2000) | 0.06% [0.05-0.08] (63/100000) | 90.0% [69.9-97.2] (18/20) | 0.92 | - | 26,167 | $0.000553 | 5.0 | - |
| llm_complaint_descriptor_review | ok | 74.0% [64.6-81.6] (74/100) | 1.00% [0.18-5.45] (1/100) | - | 1.00 | - | 4 (free tier 7,500 rows/day) | $118 | 3.9 (grounded 100%) | 92.3% [75.9-97.9] (24/26) |

Same inputs as the LLM (j):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_complaint_descriptor_allowlist | 100.0% [96.3-100.0] (100/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| stat_descriptor_classifier | 100.0% [96.3-100.0] (100/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| llm_complaint_descriptor_review | 74.0% [64.6-81.6] (74/100) | 1.00% [0.18-5.45] (1/100) | 1.00 |

## k - Resolution Description contradicting Status (row-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_resolution_status_keywords | ok | 97.6% [96.8-98.2] (1952/2000) | 0.03% [0.02-0.05] (33/100000) | 70.0% [48.1-85.5] (14/20) | 0.98 | - | 32,080 | $0.000438 | 5.0 | - |
| stat_resolution_status_classifier | ok | 100.0% [99.8-100.0] (2000/2000) | 0.06% [0.05-0.08] (60/100000) | 95.0% [76.4-99.1] (19/20) | 0.97 | - | 48,533 | $0.000293 | 5.0 | - |
| llm_resolution_status_review | ok | 73.0% [63.6-80.7] (73/100) | 1.00% [0.18-5.45] (1/100) | - | 0.60 | - | 4 (free tier 7,500 rows/day) | $142 | 4.0 (grounded 100%) | 100.0% [87.1-100.0] (26/26) |

Same inputs as the LLM (k):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_resolution_status_keywords | 98.0% [93.0-99.4] (98/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| stat_resolution_status_classifier | 100.0% [96.3-100.0] (100/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| llm_resolution_status_review | 73.0% [63.6-80.7] (73/100) | 1.00% [0.18-5.45] (1/100) | 0.60 |

## l - Personal data inside free-text fields (row-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_pii_regex | ok | 80.3% [78.6-82.0] (1607/2000) | 0.00% [0.00-0.01] (4/100000) | 20.0% [8.1-41.6] (4/20) | 1.00 | - | 393,651 | $4.34e-05 | 5.0 | - |
| stat_pii_template_novelty | ok | 100.0% [99.8-100.0] (2000/2000) | 0.04% [0.03-0.06] (42/100000) | 75.0% [53.1-88.8] (15/20) | 0.98 | - | 345,911 | $4.22e-05 | 5.0 | - |
| llm_pii_review | ok | 70.0% [60.4-78.1] (70/100) | 0.00% [0.00-3.70] (0/100) | - | 1.00 | - | 4 (free tier 7,500 rows/day) | $194 | 4.0 (grounded 96%) | 100.0% [87.1-100.0] (26/26) |

Same inputs as the LLM (l):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_pii_regex | 81.0% [72.2-87.5] (81/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| stat_pii_template_novelty | 100.0% [96.3-100.0] (100/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| llm_pii_review | 70.0% [60.4-78.1] (70/100) | 0.00% [0.00-3.70] (0/100) | 1.00 |

## Injection-rate sweep (rule + stat)

Cell = detection / precision over 5 batches per rate.

| problem | detector | 0.1% | 0.5% | 2.0% | 10.0% |
|---|---|---|---|---|---|
| d | rule_nyc_bbox | 100% / 100% | 100% / 100% | 100% / 100% | 100% / 100% |
| d | stat_coordinate_outlier | 100% / 58% | 100% / 87% | 100% / 97% | 100% / 99% |
| e | rule_exact_duplicate_rows | 100% / 100% | 100% / 100% | 100% / 100% | 100% / 100% |
| e | stat_bloom_duplicate | 100% / 100% | 100% / 99% | 100% / 100% | 100% / 100% |
| f | rule_near_duplicate_key | 84% / 100% | 88% / 100% | 82% / 100% | 86% / 100% |
| f | stat_near_duplicate_similarity | 84% / 70% | 90% / 93% | 85% / 98% | 89% / 100% |
| h | rule_closed_after_created | 100% / 100% | 100% / 100% | 100% / 100% | 100% / 100% |
| h | stat_duration_outlier | 100% / 11% | 100% / 39% | 100% / 72% | 100% / 93% |
| i | rule_zip_prefix_borough | 100% / 71% | 100% / 93% | 100% / 98% | 100% / 100% |
| i | stat_zip_borough_frequency | 100% / 71% | 100% / 93% | 100% / 98% | 100% / 100% |
| j | rule_complaint_descriptor_allowlist | 100% / 78% | 100% / 95% | 100% / 99% | 100% / 100% |
| j | stat_descriptor_classifier | 100% / 54% | 99% / 86% | 100% / 96% | 100% / 99% |
| k | rule_resolution_status_keywords | 88% / 79% | 96% / 95% | 98% / 99% | 99% / 100% |
| k | stat_resolution_status_classifier | 100% / 61% | 100% / 89% | 100% / 97% | 100% / 99% |
| l | rule_pii_regex | 80% / 100% | 83% / 100% | 83% / 100% | 80% / 100% |
| l | stat_pii_template_novelty | 100% / 60% | 100% / 88% | 100% / 97% | 100% / 99% |

## Null-spike intensity sweep (g, rule + stat)

Cell = corrupted batches detected, and of those correctly localised.

| detector | 10% nulled | 25% nulled | 50% nulled | 90% nulled |
|---|---|---|---|---|
| rule_null_rate_thresholds | 1/5 detected, 1/1 localised | 1/5 detected, 1/1 localised | 3/5 detected, 3/3 localised | 5/5 detected, 5/5 localised |
| stat_null_spike_binomial | 5/5 detected, 5/5 localised | 5/5 detected, 5/5 localised | 5/5 detected, 5/5 localised | 5/5 detected, 5/5 localised |

## Background: rule + stat on real, unsanitised evaluation batches

Nothing was injected here: alerts are real pre-existing issues or false alarms.

| problem | detector | batches alerted | alerts / batch | flagged rows / 1,000 | errors | example alerts |
|---|---|---|---|---|---|---|
| a | rule_schema_columns | 0.0% [0.0-16.1] (0/20) | 0.0 | - | 0/20 | - |
| a | stat_column_profile_match | 0.0% [0.0-16.1] (0/20) | 0.0 | - | 0/20 | - |
| b | rule_dtype_contract | 0.0% [0.0-16.1] (0/20) | 0.0 | - | 0/20 | - |
| b | stat_value_type_profile | 0.0% [0.0-16.1] (0/20) | 0.0 | - | 0/20 | - |
| c | rule_datetime_format | 0.0% [0.0-16.1] (0/20) | 0.0 | 0.00 | 0/20 | - |
| c | stat_datetime_shape_shift | 0.0% [0.0-16.1] (0/20) | 0.0 | 0.00 | 0/20 | - |
| d | rule_nyc_bbox | 0.0% [0.0-16.1] (0/20) | 0.0 | 0.00 | 0/20 | - |
| d | stat_coordinate_outlier | 100.0% [83.9-100.0] (20/20) | 1.0 | 0.71 | 0/20 | 2 rows have coordinates far from where the reference model puts them (2 checked against state-plane x/y, 0 ...<br>1 rows have coordinates far from where the reference model puts them (1 checked against state-plane x/y, 0 ...<br>4 rows have coordinates far from where the reference model puts them (4 checked against state-plane x/y, 0 ... |
| e | rule_exact_duplicate_rows | 0.0% [0.0-16.1] (0/20) | 0.0 | 0.00 | 0/20 | - |
| e | stat_bloom_duplicate | 5.0% [0.9-23.6] (1/20) | 0.1 | 0.01 | 0/20 | 1 rows have a fingerprint seen earlier in the batch (Bloom filter) |
| f | rule_near_duplicate_key | 100.0% [83.9-100.0] (20/20) | 1.0 | 59.74 | 0/20 | 198 rows repeat an earlier request within 300 s under a new unique_key<br>306 rows repeat an earlier request within 300 s under a new unique_key<br>292 rows repeat an earlier request within 300 s under a new unique_key |
| f | stat_near_duplicate_similarity | 100.0% [83.9-100.0] (20/20) | 1.0 | 60.38 | 0/20 | 198 rows are near-copies of an earlier row (similarity above the reference threshold)<br>307 rows are near-copies of an earlier row (similarity above the reference threshold)<br>293 rows are near-copies of an earlier row (similarity above the reference threshold) |
| g | rule_null_rate_thresholds | 0.0% [0.0-16.1] (0/20) | 0.0 | 0.00 | 0/20 | - |
| g | stat_null_spike_binomial | 10.0% [2.8-30.1] (2/20) | 0.9 | 17.88 | 0/20 | DOB: closed_date is null in 8 of 144 rows (5.6%); reference 0.0%, normal batches up to 0.0%; beta-binomial ...<br>HPD: closed_date is null in 116 of 2041 rows (5.7%); reference 0.1%, normal batches up to 0.7%; beta-binomi...<br>DEP: resolution_description is null in 187 of 528 rows (35.4%); reference 0.7%, normal batches up to 2.3%; ... |
| h | rule_closed_after_created | 45.0% [25.8-65.8] (9/20) | 0.5 | 0.44 | 0/20 | 3 rows were closed before they were created (closed_date < created_date)<br>1 rows were closed before they were created (closed_date < created_date)<br>9 rows were closed before they were created (closed_date < created_date) |
| h | stat_duration_outlier | 100.0% [83.9-100.0] (20/20) | 1.0 | 8.13 | 0/20 | 33 rows were closed implausibly early for their complaint type (0 before they were created)<br>27 rows were closed implausibly early for their complaint type (3 before they were created)<br>30 rows were closed implausibly early for their complaint type (0 before they were created) |
| i | rule_zip_prefix_borough | 85.0% [64.0-94.8] (17/20) | 0.8 | 0.75 | 0/20 | 2 rows have a ZIP whose USPS prefix belongs to another borough (0 are not New York City ZIPs)<br>4 rows have a ZIP whose USPS prefix belongs to another borough (0 are not New York City ZIPs)<br>3 rows have a ZIP whose USPS prefix belongs to another borough (0 are not New York City ZIPs) |
| i | stat_zip_borough_frequency | 85.0% [64.0-94.8] (17/20) | 0.8 | 0.73 | 0/20 | 2 rows have a borough that is unusual for their ZIP (relative likelihood < 1); 0 rows not judged (ZIP unsee...<br>4 rows have a borough that is unusual for their ZIP (relative likelihood < 1); 2 rows not judged (ZIP unsee...<br>2 rows have a borough that is unusual for their ZIP (relative likelihood < 1); 5 rows not judged (ZIP unsee... |
| j | rule_complaint_descriptor_allowlist | 90.0% [69.9-97.2] (18/20) | 0.9 | 2.45 | 0/20 | 2 row(s) have a (complaint_type, descriptor) pair that is not on the allow-list<br>1 row(s) have a (complaint_type, descriptor) pair that is not on the allow-list<br>3 row(s) have a (complaint_type, descriptor) pair that is not on the allow-list |
| j | stat_descriptor_classifier | 90.0% [69.9-97.2] (18/20) | 0.9 | 1.37 | 0/20 | 2 row(s) have a descriptor that is unlikely for their complaint type<br>1 row(s) have a descriptor that is unlikely for their complaint type<br>4 row(s) have a descriptor that is unlikely for their complaint type |
| k | rule_resolution_status_keywords | 75.0% [53.1-88.8] (15/20) | 0.8 | 0.63 | 0/20 | 3 row(s) have a resolution text that contradicts their status (3 Closed with pending-only text, 0 open with...<br>3 row(s) have a resolution text that contradicts their status (2 Closed with pending-only text, 1 open with...<br>2 row(s) have a resolution text that contradicts their status (2 Closed with pending-only text, 0 open with... |
| k | stat_resolution_status_classifier | 95.0% [76.4-99.1] (19/20) | 0.9 | 1.38 | 0/20 | 4 row(s) have a status that is unlikely for their resolution text<br>15 row(s) have a status that is unlikely for their resolution text<br>1 row(s) have a status that is unlikely for their resolution text |
| l | rule_pii_regex | 35.0% [18.1-56.7] (7/20) | 0.3 | 0.07 | 0/20 | 1 rows contain personal data in free text (R-l2 phone number: 1) |
| l | stat_pii_template_novelty | 95.0% [76.4-99.1] (19/20) | 0.9 | 0.69 | 0/20 | 2 rows contain tokens never seen in reference free text<br>1 rows contain tokens never seen in reference free text<br>6 rows contain tokens never seen in reference free text |

