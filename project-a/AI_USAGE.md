# AI usage

This file records how AI was used in Project A, what the author did, how AI output was checked, and where AI help
limits the claims in [RESULTS_A.md](RESULTS_A.md).

## Tools

- **Claude Code (Anthropic), an AI coding assistant**, was used across several sessions to write code, tests, rules
  text, documents and logs, and to run the harnesses.
- **Google Gemini, at runtime and in measurement**, through the project's own client (`project_a/llm/client.py`,
  API key in a header, record/replay cache). It has three roles:
  - parser: gemini-3.5-flash-lite;
  - query-time engine: gemini-3.1-flash-lite;
  - compiler: gemini-3.5-flash-lite.

## What the AI assistant did

- Wrote almost all of the code:
  - the schema, the hand-coded engine and the params files;
  - the LLM query-time engine, the compiler, the safety checker and the sandbox;
  - the parser's prompt, normalisation and decision code;
  - the harnesses, determinism, traceability and the comparison script.
- Wrote the rules texts (`RULES_FY2024_25.md`, `RULES_FY2025_26.md`) from the sources named in the task. It did not
  re-fetch them in the session that added FY 2025-26 (TASK5_LOG step 1).
- Wrote the independent oracle, the golden-case generators, the hand-worked cases and the tests (1,087 at the end).
- Drafted the 50 NL queries and the topic map to the author's specification. NL-49 was changed on the author's
  instruction before the set was frozen.
- Reviewed and edited the compiled code: `REVIEW.md` for both years. The FY 2025-26 review took 116 s; the FY 2024-25
  review time was not recorded.
- Ran every measurement except the three terminal runs listed below, and wrote the logs, README, RESULTS_TABLES.md
  (via the script) and these documents.

## What the author did

- Set each task, its constraints and its acceptance criteria, including the per-role models used from Task 5 part B
  on (query engine gemini-3.1-flash-lite, compiler gemini-3.5-flash-lite).
- Proposed the decisions in the final design, which the written design follows.
- Entered every official-calculator row by hand on the Income Tax Department's Advanced calculator (30 rows for
  FY 2024-25, 12 for FY 2025-26) and recorded the figures in the sheets. The assistant built the sheets and the
  importer, but never typed a value into them.
- Ran three measurements from a terminal with the same harness and cache:
  - `llm_query_dev_n52_r1_auto_20261120T190205Z.json`;
  - `llm_query_heldout_n156_r1_auto_20261120T193052Z.json`;
  - `nl_v1_heldout_auto_20261120T195740Z.json`.
- Supplied the paid-tier prices from https://ai.google.dev/gemini-api/docs/pricing, checked 2026-11-21. The assistant
  did not fetch that page.

## Gemini calls

The call counts come from `llm_cache/usage.jsonl`, which starts at 2026-11-20 18:40 UTC:

| Model | HTTP 200 | Errors | Used for |
|---|---|---|---|
| gemini-3.1-flash-lite | 495 | 6 transport errors, 1 × 503, 1 × 429 (daily quota) | query-time engine (both years, determinism) |
| gemini-3.5-flash-lite | 148 | 2 × 503 | parser (dev, heldout, determinism) and compiles |
| gemma-4-31b-it, gemma-4-26b-a4b-it, gemini-2.5-flash-lite | 1 | 3 (404 or 503) | one-request availability probes |
| gemini-3-flash-preview | 2 | 6 transport errors | not called by project code; no results file corresponds to these |

Earlier use of gemini-3.5-flash happened before the usage log started; it hit that model's free-tier limit of 20
requests a day (noted in `llm/config.py`), and the exact count was not recorded. Every call used the free tier,
at $0.

## How AI output was checked

- **Against the official calculator**, entered by the author: 120/120 fields (FY 2024-25) and 48/48 (FY 2025-26)
  agree with both the engine and the oracle.
- **Against an oracle kept structurally separate:** it imports only `decimal` and `fractions`, uses its own
  differential form, and caught 7/7 wrong FY 2025-26 parameter sets.
- **Mutation checks:** 14 of 15 mutants were caught; the 15th is equivalent.
- **Splits:** heldout cases were kept out of prompt iteration and out of the compiled-code reviews, and were run
  once for the LLM implementations. The hand-coded engine is different: pytest checks it on every golden case,
  heldout included.
- **Append-only logs:** errors were corrected in later entries rather than rewritten. TASK5_LOG and
  MEASUREMENT_LOG give UTC and local times.

## Where the AI was wrong and how I caught it

- **"No cliffs exist anywhere" (a planning claim) was false.** The old regime has a real cliff at total income
  5,00,000, because the 87A rebate has no marginal relief there. Tax goes from 0 at gross 5,50,000 (total income
  5,00,000) to Rs 13,000 at gross 5,50,010 (total income 5,00,010). It was caught by the boundary check at 5,00,010
  while the property tests were being written. PLAN.md was corrected, the no-cliff windows skip that point, and
  `test_old_regime_has_a_real_cliff_at_5_lakh` asserts it (FAILURES.md F-001).
- **The first calculator procedure assumed the Basic tab takes gross salary.** It takes taxable income and omits
  marginal relief. This was caught when the hand-computed Rs 5,200 for OC-20 did not match the tab's Rs 21,320 (for
  7,05,000 entered as new-regime taxable income). All rows were then done on the Advanced tab (README, "What the
  official calculator does and does not verify").
- **The calculator's "Total Tax on Income" was recorded first.** It is unrounded, so six values (OC-19, 21, 25, 28)
  differed from the engine by 1 to 3 rupees. Fixed by re-reading the rounded "Balance Tax Payable" line (s.288B),
  after which all 120 fields match.
- **The notional price for gemini-3.1-flash-lite was first copied from the 3.5 model** ($0.30 / $2.50 instead of
  $0.25 / $1.50 per 1M tokens). That overstated the query-engine cost at $2.07 per 1,000 instead of $1.42. It was
  caught by checking the official pricing page on 2026-11-21.
- **The LLM-compiled code had defects:**
  - every FY 2024-25 raw compile, and 2 of 3 FY 2025-26 compiles, broke the safety rules by defining helper
    functions;
  - one compile per year got the surcharge wrong (FY 2024-25 raw_v2: old-regime surcharge; FY 2025-26 raw_v1:
    surcharge marginal relief);
  - the best compile each year truncated the HRA limits and the surcharge/cess paise, and used an invented constant
    to guard the 87A relief (727770 in FY 2024-25 raw_v1; 1276595 in FY 2025-26 raw_v2).

  The safety rules were caught by the allow-list. The surcharge errors were caught by the dev and official cases.
  The truncation and the constant were caught only by reading the code, because they change no test answer
  (compiler/compiled/REVIEW.md, fy2025_26/REVIEW.md).
- **Placeholder expected values:** the first smoke test contained expected values the AI had written without
  computing them (148,200; 154,700). This was caught by working each case by hand, and the hand-worked cases became
  a permanent suite (FAILURES.md F-002).

Other mistakes by the AI assistant, and how they were caught:

- **Logged numbers before measuring them**, three times in Task 5: line counts and LLM call counts. Each was found
  by reading the measurement and corrected in a later entry. The rule since then: measure in one command, log in
  the next.
- **A bulk text replacement changed `engines/llm_query.py` by accident.** It was found and reverted before any
  measurement used it.
- **Its own bugs:**
  - a duplicate test input in the FY 2025-26 suite (found by a duplicate check);
  - a crash in the offline determinism run (wrong result key);
  - a traceability checker that was first too narrow for deduction steps.

  All were fixed and logged.
- **The harness first crashed on a network timeout**, a loud failure. It was changed to stop cleanly and keep
  finished results.
- **Attribution:** it first described the author's terminal runs as "an outside process". This was corrected on
  2026-11-21.

## Where AI help limits the claims

- **The engine, the oracle and the golden cases came from the same AI assistant.** Their independence is
  structural, not authorial. A misreading shared by the rules text, the engine and the oracle would be caught only
  by the official rows, which do not cover deduction caps, the HRA formula or the 80D split.
- **The NL queries and topic map were written with AI help**, so the heldout parser results are not fully blind.
- **The compiled-code review was done by the AI assistant, not by an independent person.**
- **The design decisions are the author's.** The measurements they cite were produced by AI-written code and checked
  as described above.
