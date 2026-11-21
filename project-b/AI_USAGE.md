# AI usage

## Tools and what they did

| tool | used for |
|---|---|
| Claude Code (Claude Opus 5.5), desktop app | Planning and the design spec (`project_b/DESIGN.md`); writing all code and tests through parallel "builder" agents. Each component then got an independent "reviewer" agent that read it skeptically and fixed defects. An audit followed, with read-only "verifier" agents re-checking every open concern against data and agent transcripts. |
| Gemini (`gemini-3.5-flash-lite`, free tier) | The LLM detectors under test: per-row and batch review of NYC 311 data. Every answer is cached in `project_b/llm_cache/` for replay. |
| Google AI Studio | Creating the Gemini API key. |

How the work was organised against the AI's typical failure modes:
* **Independent injector and detectors.** The corruption injector and the detectors were written
  by different agents against the same spec, neither reading the other's code. That way the
  detectors cannot exploit artefacts of how corruptions were made.
* **Builder claims treated as unverified.** Every builder's claims were re-checked by a separate
  reviewer agent and then by a verifier agent.
* **No numbers by hand.** Every result number is produced by code from `project_b/results/`, and
  the reports are generated, never typed.

## Where the AI was wrong, and how it was caught

1. **All 44 columns at first.** The AI first took all 44 columns of the NYC 311 dataset. I figured
   out that 19 of them were redundant or not needed by any of the 12 checks, and removed them.
   Examples: the cross-street and intersection fields, the bridge, highway and taxi fields, and
   `location`, which only repeats latitude and longitude. The project now uses 25 columns
   (`COLUMNS` in `project_b/config.py`).
2. **The Gemini rate limits.** The AI could not work out which limits applied to my Gemini
   free-tier key. The code assumed 20 requests a day, which would have stretched the LLM benchmark
   over about 17 days; another estimate used 1,500 a day. I checked the rate-limit page in Google
   AI Studio for my project. `gemini-3.5-flash-lite` allows 15 requests a minute, 250,000 tokens a
   minute and 500 requests a day, and I fixed the settings to match:
   - the project paces itself at 10 requests a minute, below the limit;
   - the daily limit is a setting (`GEMINI_FREE_TIER_RPD` in `project_b/config.py`);
   - a run that hits the quota stops cleanly and resumes from the cache.
3. **A generic scale design.** The AI's first answer for the scale and design part (Task 6) was
   very generic. I specified what it had to cover: throughput, the processing architecture, LLM
   cost and customer-specific schemas, with measured values kept separate from projections.
   Section 6 of RESULTS_B.md is now built that way: every figure is labelled measured, assumption
   or projection.
4. **`.gitignore` hid source code (my main example).** The assistant wrote `data/` in `.gitignore`
   to exclude the raw download. That pattern also matches `project_b/data/` (the loader and
   sanitiser) and `tests/data/` (the test fixtures), so a clone of the repository could not have
   run. A readiness verifier caught it by asking what a fresh clone would contain. The fix,
   `/data/`, was checked in a throwaway git repository that mirrors the project.
5. **A false "all tests pass".** A builder agent reported "292 passed" for the injector tests, but
   5 tests failed. The independent reviewer re-ran the suite rather than trusting the report.
6. **Tuning on evaluation data.** Some constants were chosen while September-December numbers were
   on screen: the null-rate limits, the near-duplicate weights, the null-spike constants and one
   model choice. A verifier found this by reading the builders' transcripts. Each was re-derived on
   January-August data with a selection rule written down first. All stayed the same or were
   re-justified, and the disclosure is in RESULTS_B.md.
7. **Corrupted test data from my own export.** While building the fixture of hand-read text
   decisions, CSV quoting altered 4 of 444 texts. The new test caught it: the injector's
   fingerprint of the candidates no longer matched. The fixture was rebuilt from the raw data.
8. **An unfair ground truth.** Some injected "contradictions" used generic descriptors ('Other',
   'N/A') or status-neutral texts ('Please call 311 for further information'). These would have
   charged the LLM with misses for being right. A verifier measured the effect, and the texts were
   screened by meaning (see DESIGN.md section 3a).
9. **Unreadable LLM replies scored as clean.** The first scoring counted the rows of the 17
   unreadable Gemini replies as reviewed and not flagged: 16 replies looped until the output
   limit, and 1 was blocked by the recitation filter. That understated the LLM's detection and
   flattered its false-alarm rate. It also produced a fake 50 % determinism score. It was caught
   while verifying the benchmark run, by counting the replies in the cache that held no readable
   verdict. Those rows were re-scored as abstentions from the cache, with no new API call, and
   coverage is now reported.
10. **A stale paragraph in a generated report.** An AI edit to the report generator left a
    duplicated function, and Python used the older copy. RESULTS_B.md kept recommending alert
    thresholds after they had been built. No test caught it; reading the regenerated report did.
