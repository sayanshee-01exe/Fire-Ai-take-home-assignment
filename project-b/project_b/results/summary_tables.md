# Project B - detector comparison

Computed from `eval_records.jsonl` by `python -m project_b.evaluate`. Seed 2023, 5,000-row batches (20 corrupted per problem, 20 clean), row injection rate 2%, LLM model gemini-3.5-flash-lite. Rates show the Wilson 95 % interval and (k/n). On row-level problems the LLM reads only a sample of each batch, so its batch FAR (any alert in a batch) would not compare with detectors that read every row and is left blank. LLM rates in the detector tables are conditional on a readable verdict: rows (or batches) the model gave no readable verdict for are abstentions, counted in the coverage table and in the overall operational rates, never as clean.

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
| llm_duplicate_review | partial: no readable verdict for 120 of 1,920 rows asked (abstained) | 100.0% [85.7-100.0] (23/23) | 0.00% [0.00-0.40] (0/960) | - | 1.00 | - | 5 (free tier 18,750 rows/day) | $109 | 3.9 (grounded 100%) | 100.0% [95.4-100.0] (80/80) |

Abstentions: `llm_duplicate_review` gave a readable verdict for 1,800 of 1,920 rows asked (coverage 94%); it abstained on 120 (unreadable_reply:MAX_TOKENS 120). Overall (an abstention counts as "no alert"): detection 88.5% [71.0-96.0] (23/26), row FPR 0.00% [0.00-0.40] (0/960), precision 1.00, $102 per 1M rows asked. Given a readable verdict (table above): detection 100.0% [85.7-100.0] (23/23), row FPR 0.00% [0.00-0.40] (0/960).

Window reach: a 40-row window shows an injected copy next to its original for 6.2% [4.8-8.1] (50/800) of copies. `llm_duplicate_review` (and the rule / stat rows it is compared on) is scored only on copies shown with their original, so this share caps what window review can catch in a full batch.

Same inputs as the LLM (e):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_exact_duplicate_rows | 100.0% [85.7-100.0] (23/23) | 0.00% [0.00-0.40] (0/960) | 1.00 |
| stat_bloom_duplicate | 100.0% [85.7-100.0] (23/23) | 0.00% [0.00-0.40] (0/960) | 1.00 |
| llm_duplicate_review | 100.0% [85.7-100.0] (23/23) | 0.00% [0.00-0.40] (0/960) | 1.00 |

## f - Near-duplicates (row-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_near_duplicate_key | ok | 85.3% [83.7-86.8] (1706/2000) | 0.00% [0.00-0.01] (1/100000) | 5.0% [0.9-23.6] (1/20) | 1.00 | - | 147,543 | $9.76e-05 | 5.0 | - |
| stat_near_duplicate_similarity | ok | 87.8% [86.2-89.1] (1755/2000) | 0.01% [0.00-0.01] (7/100000) | 30.0% [14.5-51.9] (6/20) | 0.98 | - | 69,944 | $0.000211 | 5.0 | - |
| llm_near_duplicate_review | partial: no readable verdict for 280 of 1,920 rows asked (abstained) | 92.3% [75.9-97.9] (24/26) | 1.41% [0.83-2.40] (13/920) | - | 0.54 | - | 8 (free tier 17,083 rows/day) | $160 | 3.9 (grounded 100%) | 98.3% [94.1-99.5] (118/120) |

Abstentions: `llm_near_duplicate_review` gave a readable verdict for 1,640 of 1,920 rows asked (coverage 85%); it abstained on 280 (unreadable_reply:MAX_TOKENS 240, unreadable_reply:RECITATION 40). Overall (an abstention counts as "no alert"): detection 66.7% [50.3-79.8] (24/36), row FPR 1.35% [0.79-2.30] (13/960), precision 0.53, $137 per 1M rows asked. Given a readable verdict (table above): detection 92.3% [75.9-97.9] (24/26), row FPR 1.41% [0.83-2.40] (13/920).

Window reach: a 40-row window shows an injected copy next to its original for 90.5% [88.3-92.3] (724/800) of copies. `llm_near_duplicate_review` (and the rule / stat rows it is compared on) is scored only on copies shown with their original, so this share caps what window review can catch in a full batch.

Same inputs as the LLM (f):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_near_duplicate_key | 92.3% [75.9-97.9] (24/26) | 0.00% [0.00-0.42] (0/920) | 1.00 |
| stat_near_duplicate_similarity | 92.3% [75.9-97.9] (24/26) | 0.00% [0.00-0.42] (0/920) | 1.00 |
| llm_near_duplicate_review | 92.3% [75.9-97.9] (24/26) | 1.41% [0.83-2.40] (13/920) | 0.54 |

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
| llm_date_order_review | partial: no readable verdict for 25 of 300 rows asked (abstained) | 100.0% [95.8-100.0] (87/87) | 0.00% [0.00-3.70] (0/100) | - | 1.00 | - | 5 (free tier 6,875 rows/day) | $213 | 4.0 (grounded 100%) | 100.0% [20.7-100.0] (1/1) |

Abstentions: `llm_date_order_review` gave a readable verdict for 275 of 300 rows asked (coverage 92%); it abstained on 25 (unreadable_reply:MAX_TOKENS 25). Overall (an abstention counts as "no alert"): detection 87.0% [79.0-92.2] (87/100), row FPR 0.00% [0.00-3.70] (0/100), precision 1.00, $195 per 1M rows asked. Given a readable verdict (table above): detection 100.0% [95.8-100.0] (87/87), row FPR 0.00% [0.00-3.70] (0/100).

Same inputs as the LLM (h):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_closed_after_created | 100.0% [95.8-100.0] (87/87) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| stat_duration_outlier | 100.0% [95.8-100.0] (87/87) | 1.00% [0.18-5.45] (1/100) | 0.64 |
| llm_date_order_review | 100.0% [95.8-100.0] (87/87) | 0.00% [0.00-3.70] (0/100) | 1.00 |

## i - ZIP code inconsistent with borough (row-level)

| detector | status | detection [95% CI] | row FPR | batch FAR | precision @ injection rate | localisation | rows/s | $ per 1M rows | clarity 0-5 | determinism |
|---|---|---|---|---|---|---|---|---|---|---|
| rule_zip_prefix_borough | ok | 100.0% [99.8-100.0] (2000/2000) | 0.06% [0.05-0.08] (63/100000) | 90.0% [69.9-97.2] (18/20) | 0.98 | - | 1,256,582 | $1.41e-05 | 5.0 | - |
| stat_zip_borough_frequency | ok | 100.0% [99.8-100.0] (2000/2000) | 0.06% [0.05-0.08] (59/100000) | 85.0% [64.0-94.8] (17/20) | 0.98 | - | 723,736 | $2.37e-05 | 5.0 | - |
| llm_zip_borough_review | partial: no readable verdict for 74 of 300 rows asked (abstained) | 100.0% [94.3-100.0] (63/63) | 0.00% [0.00-3.70] (0/100) | - | 1.00 | - | 4 (free tier 5,650 rows/day) | $272 | 3.8 (grounded 100%) | 100.0% [87.1-100.0] (26/26) |

Abstentions: `llm_zip_borough_review` gave a readable verdict for 226 of 300 rows asked (coverage 75%); it abstained on 74 (unreadable_reply:MAX_TOKENS 74). Overall (an abstention counts as "no alert"): detection 63.0% [53.2-71.8] (63/100), row FPR 0.00% [0.00-3.70] (0/100), precision 1.00, $205 per 1M rows asked. Given a readable verdict (table above): detection 100.0% [94.3-100.0] (63/63), row FPR 0.00% [0.00-3.70] (0/100).

Same inputs as the LLM (i):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_zip_prefix_borough | 100.0% [94.3-100.0] (63/63) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| stat_zip_borough_frequency | 100.0% [94.3-100.0] (63/63) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| llm_zip_borough_review | 100.0% [94.3-100.0] (63/63) | 0.00% [0.00-3.70] (0/100) | 1.00 |

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
| rule_pii_regex | ok | 80.3% [78.6-82.0] (1607/2000) | 0.00% [0.00-0.00] (0/100000) | 0.0% [0.0-16.1] (0/20) | 1.00 | - | 386,730 | $3.75e-05 | 5.0 | - |
| stat_pii_template_novelty | ok | 100.0% [99.8-100.0] (2000/2000) | 0.04% [0.03-0.06] (42/100000) | 75.0% [53.1-88.8] (15/20) | 0.98 | - | 351,834 | $3.93e-05 | 5.0 | - |
| llm_pii_review | partial: no readable verdict for 50 of 300 rows asked (abstained) | 93.3% [85.3-97.1] (70/75) | 0.00% [0.00-3.70] (0/100) | - | 1.00 | - | 4 (free tier 6,250 rows/day) | $233 | 4.0 (grounded 96%) | 100.0% [87.1-100.0] (26/26) |

Abstentions: `llm_pii_review` gave a readable verdict for 250 of 300 rows asked (coverage 83%); it abstained on 50 (unreadable_reply:MAX_TOKENS 50). Overall (an abstention counts as "no alert"): detection 70.0% [60.4-78.1] (70/100), row FPR 0.00% [0.00-3.70] (0/100), precision 1.00, $194 per 1M rows asked. Given a readable verdict (table above): detection 93.3% [85.3-97.1] (70/75), row FPR 0.00% [0.00-3.70] (0/100).

Same inputs as the LLM (l):

| detector | detection on the LLM's injected rows | FPR on its clean-batch rows | precision @ injection rate |
|---|---|---|---|
| rule_pii_regex | 82.7% [72.6-89.6] (62/75) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| stat_pii_template_novelty | 100.0% [95.1-100.0] (75/75) | 0.00% [0.00-3.70] (0/100) | 1.00 |
| llm_pii_review | 93.3% [85.3-97.1] (70/75) | 0.00% [0.00-3.70] (0/100) | 1.00 |

## LLM coverage and abstention

Asked = rows sent for per-row review (or batches judged, for whole-batch checks) in the main corrupted and clean batches. An abstention is a row or batch the model gave no readable verdict for. Overall rates count an abstention as "no alert" (a miss on an injected row, no false alarm on a clean one); "given a verdict" rates leave abstentions out. False alarms = row FPR (row-level) or batch FAR.

| problem | LLM detector | asked | readable verdict | coverage | abstention rate | causes | detection: overall / given a verdict | false alarms: overall / given a verdict | determinism (items compared) |
|---|---|---|---|---|---|---|---|---|---|
| a | llm_schema_review | 16 batches | 16 | 100% | 0.00% [0.00-19.36] (0/16) | none | 75% / 75% | 0.0% [0.0-32.4] (0/8) / 0.0% [0.0-32.4] (0/8) | 100% (1/1) |
| b | llm_type_review | 16 batches | 16 | 100% | 0.00% [0.00-19.36] (0/16) | none | 100% / 100% | 0.0% [0.0-32.4] (0/8) / 0.0% [0.0-32.4] (0/8) | 100% (1/1) |
| c | llm_datetime_review | 16 batches | 16 | 100% | 0.00% [0.00-19.36] (0/16) | none | 100% / 100% | 0.0% [0.0-32.4] (0/8) / 0.0% [0.0-32.4] (0/8) | 100% (1/1) |
| d | llm_coordinate_review | 300 rows | 300 | 100% | 0.00% [0.00-1.26] (0/300) | none | 100% / 100% | 0.00% [0.00-3.70] (0/100) / 0.00% [0.00-3.70] (0/100) | 100% (26/26) |
| e | llm_duplicate_review | 1,920 rows | 1,800 | 94% | 6.25% [5.25-7.42] (120/1920) | unreadable_reply:MAX_TOKENS 120 | 88% / 100% | 0.00% [0.00-0.40] (0/960) / 0.00% [0.00-0.40] (0/960) | 100% (80/80) |
| f | llm_near_duplicate_review | 1,920 rows | 1,640 | 85% | 14.58% [13.08-16.23] (280/1920) | unreadable_reply:MAX_TOKENS 240, unreadable_reply:RECITATION 40 | 67% / 92% | 1.35% [0.79-2.30] (13/960) / 1.41% [0.83-2.40] (13/920) | 98% (118/120) |
| g | llm_null_spike_review | 16 batches | 16 | 100% | 0.00% [0.00-19.36] (0/16) | none | 0% / 0% | 0.0% [0.0-32.4] (0/8) / 0.0% [0.0-32.4] (0/8) | 100% (1/1) |
| h | llm_date_order_review | 300 rows | 275 | 92% | 8.33% [5.71-12.01] (25/300) | unreadable_reply:MAX_TOKENS 25 | 87% / 100% | 0.00% [0.00-3.70] (0/100) / 0.00% [0.00-3.70] (0/100) | 100% (1/1) |
| i | llm_zip_borough_review | 300 rows | 226 | 75% | 24.67% [20.13-29.84] (74/300) | unreadable_reply:MAX_TOKENS 74 | 63% / 100% | 0.00% [0.00-3.70] (0/100) / 0.00% [0.00-3.70] (0/100) | 100% (26/26) |
| j | llm_complaint_descriptor_review | 300 rows | 300 | 100% | 0.00% [0.00-1.26] (0/300) | none | 74% / 74% | 1.00% [0.18-5.45] (1/100) / 1.00% [0.18-5.45] (1/100) | 92% (24/26) |
| k | llm_resolution_status_review | 300 rows | 300 | 100% | 0.00% [0.00-1.26] (0/300) | none | 73% / 73% | 1.00% [0.18-5.45] (1/100) / 1.00% [0.18-5.45] (1/100) | 100% (26/26) |
| l | llm_pii_review | 300 rows | 250 | 83% | 16.67% [12.88-21.30] (50/300) | unreadable_reply:MAX_TOKENS 50 | 70% / 93% | 0.00% [0.00-3.70] (0/100) / 0.00% [0.00-3.70] (0/100) | 100% (26/26) |

Determinism repeats (fresh calls, not in the rates above): `llm_duplicate_review` abstained on 40 of 240 rows asked.

Determinism compares only the rows (or the batch verdict) with a readable verdict in every repeat: an abstention in any repeat, including the main answer that repeat 0 replays, removes that row from the comparison.

## Injection-rate sweep (rule + stat)

Cell = detection / precision over 5 batches per rate.

| problem | detector | 0.1% | 0.5% | 2.0% | 10.0% |
|---|---|---|---|---|---|
| d | rule_nyc_bbox | 100% / 100% | 100% / 100% | 100% / 100% | 100% / 100% |
| d | stat_coordinate_outlier | 100% / 58% | 100% / 87% | 100% / 97% | 100% / 99% |
| e | rule_exact_duplicate_rows | 100% / 100% | 100% / 100% | 100% / 100% | 100% / 100% |
| e | stat_bloom_duplicate | 100% / 100% | 100% / 99% | 100% / 100% | 100% / 100% |
| f | rule_near_duplicate_key | 84% / 100% | 88% / 100% | 82% / 100% | 86% / 100% |
| f | stat_near_duplicate_similarity | 84% / 95% | 90% / 99% | 85% / 100% | 89% / 100% |
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
| f | rule_near_duplicate_key | 100.0% [83.9-100.0] (20/20) | 1.0 | 59.75 | 0/20 | 198 rows repeat an earlier request within 300 s under a new unique_key<br>306 rows repeat an earlier request within 300 s under a new unique_key<br>292 rows repeat an earlier request within 300 s under a new unique_key |
| f | stat_near_duplicate_similarity | 100.0% [83.9-100.0] (20/20) | 1.0 | 60.10 | 0/20 | 198 rows are near-copies of an earlier row (similarity above the reference threshold)<br>307 rows are near-copies of an earlier row (similarity above the reference threshold)<br>293 rows are near-copies of an earlier row (similarity above the reference threshold) |
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
| l | rule_pii_regex | 30.0% [14.5-51.9] (6/20) | 0.3 | 0.06 | 0/20 | 1 rows contain personal data in free text (R-l2 phone number: 1) |
| l | stat_pii_template_novelty | 90.0% [69.9-97.2] (18/20) | 0.9 | 0.67 | 0/20 | 2 rows contain tokens never seen in reference free text<br>1 rows contain tokens never seen in reference free text<br>6 rows contain tokens never seen in reference free text |

