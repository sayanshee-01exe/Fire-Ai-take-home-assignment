Pipeline measurements: project_b/results/pipeline_summary.json.

Assumptions: PEAK_TO_MEAN = 3.0; CUSTOMER_SIZE_ZIPF = 1.0; HEADROOM = 2.0; LLM_CONCURRENCY = 8; NOVELTY_DECAY = 1.0.

| quantity | value | source | how |
|---|---|---|---|
| rows per second, daily mean | 579 rows/s | projection | 50 M / 86,400 s |
| rows per second, peak hour | 1,736 rows/s | projection | mean x PEAK_TO_MEAN (3.0) |
| batches per day | 10,000 batches | projection | 50 M / 5,000-row batches |
| largest customer, rows per day | 9,312,950 rows | projection | Zipf s=1.0 over 120 customers |
| smallest customer, rows per day | 77,608 rows | projection | same; its baselines take longest to learn |
| largest customer, batches per hour at peak | 233 batches/h | projection | its rows/day / 24 x PEAK_TO_MEAN / batch size |
| local throughput, one laptop core | 22,883 rows/s | measured | pipeline_summary.json |
| cores needed at peak | 1.00 cores | projection | peak rows/s x HEADROOM (2.0) / measured rows/s per core |
| CPU-hours per day | 0.61 core-h | projection | 50 M / measured rows/s / 3,600 |
| compute cost per day | 0.03 USD | projection | CPU-hours x $0.05/vCPU-h (config) |
| LLM values escalated per 1M rows (clean + real batches) | 0.00 values | measured | pipeline_summary.json llm_by_kind |
| LLM values per day | 0.00 values | projection | rate x 50 M x NOVELTY_DECAY (1.0): upper bound without a cross-day cache |
| LLM requests per day | 0.00 requests | projection | values/day x measured requests per value |
| LLM input / output tokens per day | 0 / 0 tokens | projection | values/day x measured tokens per value |
| LLM cost per day | 0.00 USD | projection | tokens x list price for gemini-3.5-flash-lite (config, 2026-11-20, Gemini Developer API pricing page (Standard tier, text)) |
| LLM requests per day vs free-tier cap | 0.00 x the cap | projection | requests/day / 500 (free-tier requests/day, AI Studio rate-limit page) |
| LLM wait per batch at peak, largest customer | 0.00 s | projection | no LLM request in the measured plan |
