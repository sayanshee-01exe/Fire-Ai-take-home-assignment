# Fire AI take-home: Project A (income-tax engine, FY 2024-25 and FY 2025-26)

## How to run everything
Python 3.10+ and pytest (`python3 -m pip install -r requirements.txt`). No API key, no network and no `.env`:

    python3 reproduce.py

It replays every LLM call from `llm_cache/` (`LLM_MODE=replay`), then:
- runs the whole test suite;
- runs the three engines on both years' golden suites;
- runs the parser evaluations;
- runs the determinism, latency, cost and traceability measurements;
- regenerates `RESULTS_TABLES.md` into `reproduce_out/<timestamp>/` and prints PASS or FAIL for every table against
  the committed copy.

Expected runtime: under a minute (about 6 seconds measured). Write-ups: `RESULTS_A.md`, `DECISION_MATRIX.md`,
`AI_USAGE.md`.

Status: **hand-coded engine, oracle and test suites (FY 2024-25 and FY 2025-26), LLM-at-query-time engine, parser and
LLM-compiled engine built; FY 2025-26 law change applied to all three implementations and measured (Task 5
parts A and B; see `project_a/task5/TASK5_LOG.md`).**
Original plan and failure catalogue: `PLAN.md` (its `make` targets were never created; use the commands
in this README). What went wrong while building: `FAILURES.md`.

## Run
    python3 -m pytest -q                       # 1087 tests, about 4 seconds, no API key needed
    python3 -m project_a.tests.gen_cases       # regenerate the 208-case suite + 10 hand-worked cases
    python3 -m project_a.tests.mutation_check  # inject 15 bugs, check the tests catch them

Python 3.10+ is required; developed on 3.13.

## Demo UI
A local Streamlit page around the same parser pipeline and hand-coded engine as the CLI. It needs Streamlit,
which `reproduce.py` and the tests do not need:

    # streamlit (only for the UI)
    python3 -m streamlit run ui/app.py --server.address localhost

Replay mode (the default) answers from `llm_cache/` with no network; live mode calls Gemini with the key in
`.env` and records to `ui/live_cache/`. See [ui/README.md](ui/README.md).

## LLM engine, parser and evaluation
The Gemini key is read from `GEMINI_API_KEY` (set from `.env`, which is git-ignored). `LLM_MODE` is
`live` (call and record), `replay` (cache only, no network) or `auto` (replay if cached, else live; default).
Calls are recorded in `llm_cache/`, so `LLM_MODE=replay` reproduces every recorded run offline.

    python3 -m project_a.llm.client                                    # list models that support generateContent
    python3 -m project_a.eval.run --engine llm_query --split dev --limit 10 [--repeats R]   # LLM at query time
    python3 -m project_a.eval.run --engine hand_coded --split dev      # same harness, hand-coded engine
    python3 -m project_a.cli "Main 30 ka hoon, FY 2024-25 mein 65k per month salary" --interactive
    python3 -m project_a.eval.nl_eval --split dev [--prompt v1]       # parser on the NL query set

Both harnesses refuse `--split heldout` unless `--allow-heldout` is given. Results go to `project_a/eval/results/` (or to `$PROJECT_A_RESULTS_DIR` if set; `reproduce.py` uses that).

Each role has its own model (defaults in `project_a/llm/config.py`): parser `gemini-3.5-flash-lite`,
LLM-at-query-time engine `gemini-3.1-flash-lite`, compiler `gemini-3.5-flash-lite`. Override one role with
`GEMINI_MODEL_PARSER`, `GEMINI_MODEL_QUERY_ENGINE` or `GEMINI_MODEL_COMPILER`, or all with `GEMINI_MODEL`.
The model is part of the cache key, so recordings from one model are never replayed for another
(the first 10-case smoke run replays with `GEMINI_MODEL_QUERY_ENGINE=gemini-3.5-flash`).
Every live request is appended to `llm_cache/usage.jsonl` (time, model, HTTP status; no secrets), and each
run ends with "calls today on <model>" (quota day in Pacific time). A quota error (429) never crashes a
harness: completed items are written, the rest are marked "not run: quota", and the exit code is 3.

## LLM-compiled engine (implementation c)
    python3 -m project_a.compiler.compile --runs 3 --temperature 0.2      # 3 LLM calls, once (cached)
    python3 -m project_a.eval.run --engine llm_compiled --artifact reviewed --split dev       # no LLM call
    python3 -m project_a.eval.run --engine llm_compiled --artifact raw_v1 --split official \
        [--diagnostic-allow-helpers]                                     # diagnostic: permit helper functions

An LLM reads `rules/RULES_FY2024_25.md` once and writes `compute(inp)`; queries then run that code with no LLM
call (about 4 us inside the function, about 30 us per sandbox round trip, zero tokens). Artifacts, the manifest
and the review live in `project_a/compiler/compiled/` (see `REVIEW.md`).

**Design note: a rules change.** The compiled code is derived from one rules file and records that file's
sha256. For a new year: write the new rules file, recompile (new prompt sha, new cache key), validate, and
evaluate against the new year's oracle cases and official checks. The review must then be redone on the new
compile: every constant (slabs, limits, rebate limits, surcharge bands), the rebate and marginal-relief logic,
rounding, and anything the old review fixed by hand (R1 to R4 in REVIEW.md). A reviewed artifact is never
carried over to a new rules file: each artifact serves only the year it was compiled for (the `fy` in its
manifest: `compiler/compiled/` for FY 2024-25, `compiler/compiled/fy2025_26/` for FY 2025-26), and the engine
refuses any other year with `UnknownRuleVersion`.

**What the safety layers do and do not guarantee.** The AST allow-list (`compiler/safety.py`) guarantees the
code is one function made of assignments, if/else, bounded-form for loops, arithmetic, comparisons, literals,
subscripts and nine pure builtins: no import, attribute access, dunder names, eval/exec/open, lambdas,
comprehensions, while, try/except, globals or classes. The sandbox (`compiler/sandbox.py`) runs it in a separate
`python -I` process with an empty environment, a temporary directory, a builtins table holding only those nine
functions, a CPU-time limit, a wall-clock timeout and a memory limit, and turns any failure into an error result.
On macOS the OS refuses memory rlimits, so memory is enforced by a watchdog that kills the worker when its
virtual size grows past the limit. Neither layer says anything about correctness: valid, sandboxed code can still
compute the wrong tax. Correctness comes only from the evaluation and the human review.

## FY 2025-26 (Task 5 part A: law change)
    python3 -m project_a.tests.gen_cases_fy2025_26           # 173-case FY 2025-26 suite + 7 hand-worked cases (from the oracle)
    python3 -m pytest -q project_a/tests/test_fy2025_26.py   # 377 tests: golden, paper cases, properties, sheet shape
    python3 -m project_a.task5.oracle_independence           # wrong FY 2025-26 params must be caught by the oracle
    python3 -m project_a.task5.baseline_step0 --classify     # re-classify the step-0 silent-failure baseline
    python3 -m project_a.tests.official.make_sheet_fy2025_26 # 12-row official sheet for AY 2026-27 (refuses if filled)
    python3 -m project_a.tests.official.import_official --fy 2025-26   # -> cases/official_26.jsonl + OFFICIAL_CHECK_REPORT_FY2025_26.md

What changed (see `rules/RULES_FY2025_26.md`, section "Changes from FY 2024-25"): new-regime slabs (0-4L nil ...
above 24L 30%) and the new-regime 87A rebate (limit 12,00,000, maximum 60,000, marginal relief just above 12L,
**confirmed on the official calculator on the 12 FY 2025-26 rows, OC26-03 to OC26-06 in the marginal-relief zone,
see `OFFICIAL_CHECK_REPORT_FY2025_26.md`**). Everything else is unchanged. The hand-coded engine needed
**no code change**: adding `params_fy2025_26.json` was enough. The oracle got its own FY 2025-26 constants,
written from the rules text. The FY 2024-25 suite, the official 30 and every earlier test pass unchanged.

Silent-failure baseline, taken before the change (`task5/baseline_step0.json`; full timings in
`task5/TASK5_LOG.md`): on the FY 2025-26 inputs the hand-coded engine and the compiled engine fail loudly
(UnknownRuleVersion). The FY 2024-25 compiled code, run directly without the engine's year guard, answers
every case with no error and is silently wrong on 136 of 173, because generated code does not look at `fy`.
The compiled engine therefore refuses any year it was not compiled for.

FY 2025-26 official-calculator checks (Advanced calculator, AY 2026-27, "Balance Tax Payable"): **12 of 12 rows match
the engine and the oracle on all four fields**. That includes four rows inside the 12-lakh marginal-relief zone
(total income 12,00,010 / 12,10,000 / 12,70,580, and 12,70,590 just past it), so the official calculator
applies the new-regime marginal relief as implemented. `test_fy2025_26.py` keeps the engine and the oracle
pinned to these rows.

## FY 2025-26 in the LLM implementations (Task 5 part B)
    python3 -m project_a.task5.query_stale                       # query-time engine with the FORGOTTEN rules update (dev)
    python3 -m project_a.eval.run --engine llm_query --suite fy2025_26 --split all --allow-heldout   # after the update
    python3 -m project_a.task5.analyse_query <results.json>      # right / silent / error, anchoring on the old law
    python3 -m project_a.compiler.compile --fy 2025-26 --runs 3  # -> compiler/compiled/fy2025_26/
    python3 -m project_a.eval.run --engine llm_compiled --suite fy2025_26 --artifact reviewed --split dev

| Implementation | Change needed | Tax exact after the update: dev / heldout | Silent failures |
|---|---|---|---|
| hand-coded | params + rules file; 0 code lines | 44/44 / 129/129 | 0 |
| LLM at query time (gemini-3.1-flash-lite) | 1 line (rules table) | 7/44 / 23/129 | 143/173 wrong with no error |
| LLM-compiled (gemini-3.5-flash-lite, reviewed) | recompile (3 calls) + 2 min review | 44/44 / 129/129 | 0 measured; 2 latent (REVIEW.md D1, D2) |

The query-time engine with the stale FY 2024-25 rules text gave 7/44 on dev, and 31 of its 34 wrong new-regime
taxes were nearer the old law. After the update, 46 of 115 still were: the model partly answers from memory.
The compiled engine refuses an artifact compiled for another year (`UnknownRuleVersion`), because generated
code never checks `fy`: without that guard, the FY 2024-25 artifacts were silently wrong on 136 of 173
FY 2025-26 cases. Full numbers, timings and corrections are in `project_a/task5/TASK5_LOG.md`.

## Measurements and comparison tables (Task 4)
    python3 -m project_a.eval.determinism offline     # hand-coded + reviewed compiled: every golden case x10, no LLM
    python3 -m project_a.eval.determinism query       # query-time engine: 20 FY 2024-25 cases x run_index 0..9 (~200 calls)
    python3 -m project_a.eval.determinism parser      # parser: 10 dev queries x run_index 0..9 (~100 calls)
    python3 -m project_a.eval.measure_offline         # every no-LLM harness configuration, both years, all splits
    python3 -m project_a.eval.traceability            # does every rupee trace to a clause? levels (a), (b), (c)
    python3 -m project_a.eval.compare                 # -> RESULTS_TABLES.md, from recorded files only (no LLM)

`RESULTS_TABLES.md` is generated: every number in it is computed by `compare.py` from files in the repo, and
the files it used are listed at its end. What was run and when: `project_a/eval/MEASUREMENT_LOG.md`.

## Layout
    project_a/core/      schema, params loader, statutory rounding, errors, Engine interface
    project_a/rules/     params_fy<YYYY_YY>.json (every number) + RULES_FY<YYYY_YY>.md (rule text with clause IDs), FY 2024-25 and 2025-26
    project_a/engines/   hand_coded.py, llm_query.py (LLM at query time), llm_compiled.py (LLM-compiled code)
    project_a/task5/     FY 2025-26 law change: TASK5_LOG.md, silent-failure baseline, oracle-independence check
    project_a/tests/     oracle.py (independent second implementation), gen_cases.py, cases/, tests, mutation_check.py
    _prototype/          first throwaway prototype (flawed on purpose to keep, see FAILURES.md); not tested

## Three independent checks on the numbers
1. 10 hand-worked cases, derived on paper (`cases/hand_worked.jsonl`).
2. An independent oracle that shares no code or constants with the engine.
3. Official income-tax calculator checks: **done**. 30 FY 2024-25 rows (120/120 fields match) and 12 FY 2025-26
   rows (48/48 fields match). See "Official-calculator checks" below, `OFFICIAL_CHECK_REPORT.md` and
   `OFFICIAL_CHECK_REPORT_FY2025_26.md`.

## Official-calculator checks
    python3 -m project_a.tests.official.make_sheet       # pick 30 cases -> tests/official/official_checks.{csv,md}, CALCULATOR_NOTES.md
                                                         # refuses once any official_* cell is filled; --force discards them
    # type each row from official_checks.md into the calculator; fill the 4 official_* columns in the CSV
    python3 -m project_a.tests.official.import_official  # -> tests/cases/official_30.jsonl + OFFICIAL_CHECK_REPORT.md
    python3 -m pytest -q project_a/tests/test_official_tooling.py   # engine vs official_30.jsonl (skips until it exists)

The import refuses the whole sheet if any row is incomplete, non-numeric, or has edited inputs. It only reports
mismatches; known, triaged ones go in `tests/cases/official_exceptions.json` as `{"case_id": ..., "reason": ...}`.

### What the official calculator does and does not verify
- **Basic calculator: not used.** It takes taxable income, not salary, and omits marginal relief (the portal
  says so). Entering 7,05,000 as new-regime taxable income gives tax Rs 21,320 (no 87A marginal relief).
- **Advanced calculator (Act 1961, AY 2025-26): used for all 30 rows.** It takes salary and deductions, applies
  the standard deduction automatically and models marginal relief. It verifies slabs, 87A rebate, marginal
  relief, surcharge, cess, the standard deduction and the deduction amounts we enter.
- **Not verified by it:** the HRA formula (we enter the exemption by subtracting it from net salary,
  `adv_net_salary_old`) and the split of 80D between self and parents (one combined field, `adv_80d_total`).
  Those stay covered by the oracle and the hand-worked cases.
- **Which tax figure to record:** "Balance Tax Payable / Refundable", which is rounded to the nearest 10
  (s.288B) like our engine's tax. "Total Tax on Income" is the unrounded figure. Recording it first gave
  1 to 3 rupee differences in 6 fields (OC-19, 21, 25, 28). Re-recorded from Balance Tax Payable, all
  120 fields match.
- **First Advanced check (OC-20, rebate_87a-13, new regime):** gross 7,80,000 gives taxable income 7,05,000,
  tax before rebate 20,500, rebate 15,500, net 5,000, cess 200, Total Tax on Income 5,200 (already a multiple
  of 10, so Balance Tax Payable is also 5,200). This matches the hand-coded engine.
