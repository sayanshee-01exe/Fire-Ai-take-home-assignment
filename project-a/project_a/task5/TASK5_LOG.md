# Task 5 log: adding FY 2025-26

Each entry: UTC time, local time, what was done, files and lines changed, what broke, and whether any failure was silent (a wrong answer with no error).

### 2026-11-20 19:54:49 UTC / 2026-11-21 01:24:49 IST local - START preparation
Reading the 2024-25 params, rules, oracle, case generator and tests that mention 2025-26, to plan the change. No files changed yet.

### 2026-11-20 19:55:50 UTC / 2026-11-21 01:25:50 IST local - END preparation
Read params_fy2024_25.json, oracle.py, gen_cases.py, test_properties.py, test_validation_and_rounding.py. Findings: the engine already selects params by FY via load_params(fy) and raises UnknownRuleVersion for a missing file; the oracle hard-codes FY 2024-25 constants and asserts fy == '2024-25'; the compiled engine refuses any FY other than 2024-25 but the compiled code itself never reads fy; the existing unknown-FY test uses 2030-31, so adding a 2025-26 params file breaks no test. Files changed: none.

### 2026-11-20 19:55:50 UTC / 2026-11-21 01:25:50 IST local - START step 0 (silent-failure baseline)
Plan: write the FY 2025-26 case-input generator (inputs only, no expected values yet), then run the UNCHANGED hand-coded engine and the UNCHANGED compiled artifacts (raw_v1, raw_v3, reviewed) on those inputs with fy=2025-26, record every output, and classify later against the new oracle. No LLM calls.

### 2026-11-20 19:57:02 UTC / 2026-11-21 01:27:02 IST local - END step 0 (recorded; classified after step 3)
Files added: project_a/tests/gen_cases_fy2025_26.py (     138 lines; inputs for 174 FY 2025-26 cases, 9 categories; 44 dev / 130 heldout), project_a/task5/baseline_step0.py (     107 lines), project_a/task5/__init__.py (0 lines). Raw outcomes saved in project_a/task5/baseline_step0.json. No existing file changed; no LLM call.
Recorded, before any FY 2025-26 change (174 cases each):
- hand_coded: LOUD 174/174, UnknownRuleVersion 'no rule parameters for FY 2025-26'.
- compiled_engine:raw_v1 / raw_v3 / reviewed: LOUD 174/174 each, UnknownRuleVersion 'compiled artifacts exist only for FY 2024-25' (the engine's FY guard).
- compiled_code:raw_v1:strict and raw_v3:strict: LOUD 174/174 (rejected by the allow-list, not because of the year).
- compiled_code:raw_v1:diagnostic, raw_v3:diagnostic and reviewed:strict: ANSWERED 174/174 with no error. The generated code never reads inp['fy'], so it silently applies FY 2024-25 rules to a 2025-26 input. Whether each answer is wrong is classified against the new oracle after step 3.

### 2026-11-20 19:57:23 UTC / 2026-11-21 01:27:23 IST local - START step 1 (params and rules text)
Writing project_a/rules/params_fy2025_26.json (same keys as 2024-25) and project_a/rules/RULES_FY2025_26.md (same clause IDs) with a 'changes from FY 2024-25' section.

### 2026-11-20 19:57:51 UTC / 2026-11-21 01:27:51 IST local - END step 1
Files added: project_a/rules/params_fy2025_26.json (      50 lines; differs from 2024-25 in fy, rule_version, slabs.new, rebate_87a.new, plus new keys assessment_year, changes_from_fy2024_25, verification text) and project_a/rules/RULES_FY2025_26.md (      97 lines; 39 diff lines against the 2024-25 text: title, sources, a 'Changes from FY 2024-25' table, C-SLAB-NEW, C-87A-NEW, C-87A-NEW-MR marked 'to be confirmed on the official calculator', age date 31 March 2026). load_params('2025-26') loads and passes the loader's key and FY checks. The new rules text passes the compiler's no-code check. The sources are the ones cited in the task; I did not re-fetch them in this session. Nothing broke.

### 2026-11-20 19:58:01 UTC / 2026-11-21 01:28:01 IST local - START step 2 (hand-coded engine)
Check whether the unchanged engine handles FY 2025-26 from the new params file alone, using the seven hand-worked cases W01-W07 (derived on paper, in gen_cases_fy2025_26.py).

### 2026-11-20 19:58:09 UTC / 2026-11-21 01:28:09 IST local - END step 2
NO CODE CHANGE to the hand-coded engine (project_a/engines/hand_coded.py sha256 ae63b083..., unchanged). Its FY 2025-26 behaviour comes entirely from params_fy2025_26.json via load_params(fy): slabs are a variable-length list, 87A uses limit/max/marginal_relief from the file, and the surcharge-relief comment ('thresholds are 50 lakh and above, where no rebate applies') still holds with a 12-lakh rebate limit. Against the seven paper-worked cases W01-W07 (12L edge, inside and just past the marginal-relief zone, 30% top slab, 8L, a senior), the unchanged engine matched 7/7 with rule_version fy2025_26.v1. Law-change lines for the engine: 0. Nothing broke.

### 2026-11-20 19:58:21 UTC / 2026-11-21 01:28:21 IST local - START step 3 (oracle)
Extend project_a/tests/oracle.py for FY 2025-26 from RULES_FY2025_26.md only (new-regime rate steps and the 87A limit/max written inline in the oracle's own differential form); FY 2024-25 constants and formulas untouched.

### 2026-11-20 19:59:19 UTC / 2026-11-21 01:29:19 IST local - END step 3 (oracle) + step 0 classification
Changed project_a/tests/oracle.py: 12 lines added, 6 changed (docstring; NEW_STEPS_FY26 and a per-year NEW_REGIME table of (steps, 87A limit, 87A max); the year assert now accepts the table's years; the new-regime block reads steps/limit/max from the table instead of the 7,00,000 / 25,000 literals). FY 2024-25 constants and every formula unchanged; all 436 FY 2024-25 engine-case tests still pass. The oracle matches the 7 paper cases W01-W07.
Independence check (added project_a/task5/oracle_independence.py,       68 lines): the oracle imports only decimal and fractions. Seven wrong FY 2025-26 parameter sets fed to the engine in memory were all caught by the oracle: stale 7L limit 50/174 cases, 25,000 max 38/174, marginal relief off 5/174 (the thinnest), old slabs 113/174, missing 25% band 46/174, 50,000 standard deduction 170/174, cess 3% 140/174.
Step 0 classified against the new oracle (174 cases per layer):
- hand_coded: LOUD 174, silent 0.
- compiled_engine (raw_v1, raw_v3, reviewed): LOUD 174 each (FY guard), silent 0.
- compiled_code strict raw_v1, raw_v3: LOUD 174 each (allow-list), silent 0.
- compiled_code raw_v1 diagnostic, raw_v3 diagnostic, reviewed strict: 174 answers each, 37 right (both years give the same answer) and 137 SILENTLY WRONG each. Example fy26-rebate_12l-01 (gross 11,99,999): new tax 71,500, should be 0. The generated code ignores inp['fy'], so without the engine's guard a stale artifact fails silently.

### 2026-11-20 19:59:45 UTC / 2026-11-21 01:29:45 IST local - CORRECTION to the step 3 line count
The previous entry gave the oracle change as '12 lines added, 6 changed'. That was an estimate written before I measured it. Measured exactly (diff of oracle.py against the FY 2024-25 version reconstructed from this session's copy): 7 lines removed, 16 lines added. In detail: docstring 1 line replaced by 4; 5 new lines for NEW_STEPS_FY26 and the NEW_REGIME table; the year assert 1 line replaced by 2; the new-regime block 5 lines rewritten to read steps/limit/max from the table.

### 2026-11-20 19:59:59 UTC / 2026-11-21 01:29:59 IST local - START step 4 (golden suite, hand-worked cases, property tests)
Generate cases/golden_fy2025_26.jsonl from the oracle and cases/hand_worked_fy2025_26.jsonl from the paper values; add tests/test_fy2025_26.py.

### 2026-11-20 20:01:16 UTC / 2026-11-21 01:31:16 IST local - END step 4
Generated project_a/tests/cases/golden_fy2025_26.jsonl: 173 cases, 9 categories (rebate_12l 17, new_slab_boundary 15, old_regime 27, surcharge 28, hra 16, deduction_caps 24, age_boundary 8, realistic 30, degenerate 8); 44 dev, 129 heldout by the sha256 hash rule. Generated project_a/tests/cases/hand_worked_fy2025_26.jsonl: 7 cases derived on paper in gen_cases_fy2025_26.py. Added project_a/tests/test_fy2025_26.py (     136 lines, 376 tests): suite shape, the rebate points (gross 11,99,999 / 12,00,000 / 12,00,001 / 12,10,000 / 12,75,000 / 12,75,001; total income 12,70,580, the last relief point, and 12,70,590, the first after it), the hash split, oracle and engine vs paper, engine vs golden, no stale expected values, old regime identical to FY 2024-25, monotone and no-cliff windows, the 5L old-regime cliff, and marginal relief never above the extra income.
What broke: (1) My first build had a duplicate input: gross 12,75,000 appeared both in the 'as gross' list and as total income 12,00,000. Found by a duplicate-input check, removed, so 174 -> 173. The step 0 baseline was recorded on the 174-case draft. On the final 173, each loud layer is 173 loud, and each answering layer is 37 right and 136 silently wrong (the removed row was silently wrong too: 83,200 against 0). (2) Six duplicate inputs remain, inherited by design from the FY 2024-25 suite: the old 5L rebate rows repeat the old slab-boundary rows at 5L. Kept for parity. No silent failure: all FY 2025-26 tests passed on the first run.

### 2026-11-20 20:01:30 UTC / 2026-11-21 01:31:30 IST local - START step 5 (regression)
Full pytest; FY 2024-25 golden (208), official 30, every earlier test; FY 2024-25 mutation check.

### 2026-11-20 20:02:46 UTC / 2026-11-21 01:32:46 IST local - END step 5
Full pytest: 1056 passed (680 before Task 5 + 376 new FY 2025-26 tests). The FY 2024-25 golden suite (208 cases: test_engine_cases 436 tests), the official 30 (test_official_tooling) and the suite-shape tests pass unchanged (450 passed in that subset). Mutation check (FY 2024-25): 14 of 15 caught, 1 known equivalent mutant (M2), 0 gaps, the same as before Task 5. Unchanged by sha256 or mtime: hand_coded.py (ae63b083...), params_fy2024_25.json, RULES_FY2024_25.md, golden.jsonl, official_30.jsonl, official_checks.csv, the NL set and topic map, the parser files, and the FY 2024-25 compiled artifacts (raw_v1-3, reviewed.py f9f9e950...). Nothing broke; no silent failure.

### 2026-11-20 20:03:07 UTC / 2026-11-21 01:33:07 IST local - START step 6 (official sheet FY 2025-26)
New script project_a/tests/official/make_sheet_fy2025_26.py writes official_checks_fy2025_26.csv/.md (12 rows, same columns as the FY 2024-25 sheet, Advanced calculator, AY 2026-27). make_sheet.py itself is not run and not changed.

### 2026-11-20 20:03:43 UTC / 2026-11-21 01:33:43 IST local - END step 6
Added project_a/tests/official/make_sheet_fy2025_26.py (      88 lines; reuses make_sheet's row builder and Markdown writer without changing them, and has the same refuse-if-filled guard). Wrote official_checks_fy2025_26.csv (12 rows OC26-01..12, same 23 columns, official_* columns and notes empty) and official_checks_fy2025_26.md (procedure for AY 2026-27, Income Tax Act 1961, record 'Balance Tax Payable'; marginal relief flagged as to be confirmed). Rows: new-regime total income 11,99,990 / 12,00,000 / 12,00,010 / 12,10,000 / 12,70,580 (last relief point) / 12,70,590 (first after); new slabs just above 16L, 20L and 24L; old regime TI 5,00,010 (unchanged cliff); surcharge TI 50,00,010; one realistic HRA+80C+80D profile. The FY 2024-25 official_checks.csv is unchanged (sha1 8cd350b6...) and make_sheet.py was not run. Added one shape test to test_fy2025_26.py (now 377 tests). Not done: import_official.py still reads only the FY 2024-25 sheet; importing the FY 2025-26 sheet needs a parameterised importer once it is filled. Nothing broke.

### 2026-11-20 20:03:50 UTC / 2026-11-21 01:33:50 IST local - START step 7 (README)
Add a FY 2025-26 section (commands, what changed, the baseline result), update the test count and the layout.

### 2026-11-20 20:04:24 UTC / 2026-11-21 01:34:24 IST local - END step 7 and summary
README.md: title and status line updated (the old status said the LLM engines were 'not started', which was stale); test count 680 -> 1057; new section 'FY 2025-26 (Task 5 part A: law change)' with commands, what changed, the no-code-change result and the silent-failure baseline; layout lists the per-year rule files, the three engines and task5/. Final full pytest: 1057 passed. Live LLM requests during Task 5: 0 (llm_cache/usage.jsonl, last request before 19:54 UTC). Key scan: 0 files contain the key prefix.
SUMMARY. Law change cost: engine code 0 lines; new data and text files params_fy2025_26.json + RULES_FY2025_26.md; oracle 7 lines removed / 16 added; new tests and generators only. Loud failures before the change: hand-coded engine and compiled engine, 173/173 each (UnknownRuleVersion). Silent failures: the FY 2024-25 compiled code run without the engine's FY guard, 136/173 wrong with no error (raw_v1 and raw_v3 diagnostic, reviewed strict). Remaining uncertainty: the new-regime 87A marginal relief at 12L is from the cited sources and not yet seen on the official calculator; the FY 2025-26 official sheet is unfilled; deduction limits carried over unverified (risk R3).

### 2026-11-20 20:04:52 UTC / 2026-11-21 01:34:52 IST local - CORRECTION to the summary (LLM calls)
The summary entry said 'Live LLM requests during Task 5: 0'. I wrote that before reading the check's output, and it is wrong as a statement about the account. llm_cache/usage.jsonl shows 23 live requests to gemini-3.5-flash-lite (all HTTP 200) between 19:54:56 and 19:57:40 UTC, during steps 0-1. They were not made by any Task 5 step: none of my scripts creates an LLM client. They match a results file I did not create, project_a/eval/results/nl_v1_heldout_auto_20261120T195740Z.json (an NL-parser run on the HELDOUT split, written 19:57 UTC). Another file I did not create, llm_query_heldout_n156_r1_auto_20261120T193052Z.json (19:30 UTC, before Task 5), is an LLM-at-query-time heldout run. I have not opened either file. Correct statement: Task 5 part A itself made 0 LLM calls; 23 calls were made on this account by another process during it.

### 2026-11-20 20:09:33 UTC / 2026-11-21 01:39:33 IST local - START step 8 (import the FY 2025-26 official sheet)
Sheet official_checks_fy2025_26.csv reported filled by the user (12 rows, Balance Tax Payable). Plan: give import_official.py a per-year sheet setting so the FY 2025-26 sheet is checked against the FY 2025-26 engine and oracle; FY 2024-25 import unchanged; add a test; update README.

### 2026-11-20 20:11:41 UTC / 2026-11-21 01:41:41 IST local - END step 8
import_official.py: added a per-year SHEETS setting and a --fy flag (default 2024-25). Six regions rewritten: 29 original lines replaced by 62 (net +33), counted per region from the edit; no compare or parse logic changed. make_sheet_fy2025_26.py: load_golden() and build_rows() split out of main() so the importer can rebuild the reference rows without writing files (+10 lines). test_fy2025_26.py: 2 tests added (engine and oracle vs official_26.jsonl; one altered value gives exactly one mismatch), and the step-6 sheet test changed from 'official columns empty' to 'all empty or all whole numbers'. That test failed once because the sheet is now filled; it was my own outdated assertion, not a code fault. README: status, test count, the import command and the result.
FY 2024-25 import: re-run into scratch files, byte-identical to the existing official_30.jsonl and OFFICIAL_CHECK_REPORT.md, 0 mismatches. FY 2025-26 import: wrote project_a/tests/cases/official_26.jsonl and OFFICIAL_CHECK_REPORT_FY2025_26.md. 12 of 12 rows match the engine and the oracle on all four fields (0 of 48 fields differ). That covers OC26-03..06 in and at the end of the 12L marginal-relief zone, so the calculator applies the new-regime marginal relief as implemented. Sheet unchanged (sha1 55823545...). Full pytest: 1059 passed. LLM calls in this step: 0 (checked in llm_cache/usage.jsonl). No silent failure found.

### 2026-11-20 20:11:58 UTC / 2026-11-21 01:41:58 IST local - CORRECTION to the step 8 entry (LLM calls)
The step 8 entry said 'LLM calls in this step: 0 (checked in usage.jsonl)'. That text was written into the command before the check printed its result, and the check said 1. I made the same mistake in the Task 5 summary. llm_cache/usage.jsonl has one request during step 8: 2026-11-20T20:11:33Z, model gemini-3-flash-preview, generateContent, status transport_error. No code of mine uses that model (the role defaults are gemini-3.5-flash-lite and gemini-3.1-flash-lite), so it came from another process using this account with a model override; no new results file appeared. Correct statement: step 8 itself made 0 LLM calls; 1 failed request was made by something else during it. From now on I read the check's output before writing the log text.

### 2026-11-20 20:39:46 UTC / 2026-11-21 02:09:46 IST local - START Task 5 part B
Apply FY 2025-26 to the two LLM implementations and measure. Models per role: query engine gemini-3.1-flash-lite, compiler gemini-3.5-flash-lite. Usage today before part B (llm_cache/usage.jsonl, quota day 2026-11-20 Pacific): gemini-3.1-flash-lite 201 requests (200 OK), gemini-3.5-flash-lite 57 (55 OK); almost all from the outside runs noted earlier. gemini-3-flash-preview has 8 requests today, the latest at 20:18:30 UTC; none were made by me.

### 2026-11-20 20:39:46 UTC / 2026-11-21 02:09:46 IST local - START step B0 (housekeeping: marginal relief confirmed)
Replace the 'to be confirmed on the official calculator' wording in RULES_FY2025_26.md (2 places) and params_fy2025_26.json (2 places); nothing else.

### 2026-11-20 20:40:04 UTC / 2026-11-21 02:10:04 IST local - END step B0
RULES_FY2025_26.md: 2 places reworded; diff 3 lines removed, 2 added (one sentence had wrapped over two lines). params_fy2025_26.json: 2 string values reworded (2 lines changed). No 'to be confirmed' wording remains in either file. Left alone as instructed ('nothing else changes there'), though now stale: params verification.slabs_new_and_87a_new still says 'not yet checked on the official calculator for AY 2026-27'. The official sheet's .md still says 'to be confirmed', and official sheets are out of scope. FY 2025-26 tests: 379 passed. Changing the rules text changes its sha256, so later query-engine and compile prompts use the new text. Nothing broke.

### 2026-11-20 20:40:14 UTC / 2026-11-21 02:10:14 IST local - START step B-prep (harness, compiled engine, compiler: no LLM)
Add a FY 2025-26 case set to project_a/eval/run.py; make the compiled engine take each artifact's FY from its manifest (the year guard); parameterise compile.py by rules file. The query-time engine is not changed yet (that is step b2).

### 2026-11-20 20:42:07 UTC / 2026-11-21 02:12:07 IST local - END step B-prep
No LLM calls. Exact line counts (difflib against copies taken before editing):
- project_a/eval/run.py: 17 removed, 28 added. Added a SUITES table (fy2024_25, fy2025_26), --suite, split 'all' (needs --allow-heldout like heldout), --compiled-dir, a 'split' field on every result row, and the suite in the file name and header. The FY 2024-25 default is unchanged.
- project_a/engines/llm_compiled.py: 2 removed, 3 added. The year guard now takes the artifact's FY from its manifest (default 2024-25) instead of the fixed COMPILED_FY.
- project_a/compiler/compile.py: 10 removed, 17 added. YEARS table, --fy, and the input schema's year and 31 March templated. The FY 2024-25 prompt sha256 is unchanged (27200f8c...), so its recorded compiles remain valid.
- New tests: project_a/tests/test_task5b.py, 7 tests. Full pytest: 1066 passed.
Observation: RULES_FY2025_26.md has its own 'Changes from FY 2024-25' table quoting the old 7L / 25,000 values. That is rules text, not code, so both LLM implementations will see the old numbers inside the new text; a possible anchoring risk to watch.

### 2026-11-20 20:42:40 UTC / 2026-11-21 02:12:40 IST local - START step b1 (query-time engine, stale baseline)
Write project_a/task5/analyse_query.py (classification and anchoring analysis) and project_a/task5/query_stale.py (the forgotten-update simulation: FY 2025-26 inputs with the FY 2024-25 rules text, mapped in memory without editing the engine). Run on the 44 FY 2025-26 dev cases: 44 live calls to gemini-3.1-flash-lite, cached.

### 2026-11-20 20:42:49 UTC / 2026-11-21 02:12:49 IST local - START step c1 (compile FY 2025-26, 3 runs)
python3 -m project_a.compiler.compile --fy 2025-26 --runs 3 --temperature 0.2. Model gemini-3.5-flash-lite (compiler role), retries off, run_index 0/1/2. The prompt holds RULES_FY2025_26.md plus the year-templated schemas and restrictions only: no FY 2024-25 code, no oracle or engine code. Output: project_a/compiler/compiled/fy2025_26/. Running alongside b1, which uses a different model.

### 2026-11-20 20:43:22 UTC / 2026-11-21 02:13:22 IST local - END step c1
3 live calls to gemini-3.5-flash-lite, all HTTP 200, no 429 (60 requests on that model today). Prompt sha256 92463708... (FY 2025-26 rules). Saved project_a/compiler/compiled/fy2025_26/raw_v1.py, raw_v2.py, raw_v3.py and manifest.json (fy 2025-26 for each).
Strict allow-list: raw_v1 REJECTED (12 violations, helper functions only), raw_v2 PASSED (289 code lines), raw_v3 REJECTED (13 violations, helper functions only). Tokens prompt/output per compile: 2944 / 3729, 4930, 3904; latency 7.4 s, 9.2 s, 7.5 s. Files changed: none outside fy2025_26/.

### 2026-11-20 20:43:31 UTC / 2026-11-21 02:13:31 IST local - START step c2 (validate and evaluate FY 2025-26 compiles on dev and official 12)
No LLM calls; no heldout.

### 2026-11-20 20:43:56 UTC / 2026-11-21 02:13:56 IST local - END step c2
No LLM calls. Tax exact (both regimes), FY 2025-26 dev 44 / official 12:
- raw_v1: strict REJECTED (0/44, 0/12, every query an error); diagnostic 41/44, 11/12. All failures are surcharge cases (for example surcharge-03 old tax 14,66,400 against 14,43,000), so the surcharge marginal relief is wrong. Taxable income 44/44.
- raw_v2: strict PASSED, 44/44 and 12/12 (diagnostic identical).
- raw_v3: strict REJECTED; diagnostic 16/44 and 1/12. Tax roughly doubled (for example old tax 3,74,400 against 1,87,200), while taxable income is 44/44.
Disagreements between the three (diagnostic): dev 31 of 44 inputs, official 12 of 12. Almost all are raw_v3; raw_v1 differs only on surcharge rows; raw_v2 agrees with the oracle and the calculator everywhere.
These raw_v1 and raw_v3 errors would be SILENT under the diagnostic relaxation (answers, no error). Under the strict allow-list both are LOUD (rejected) for an unrelated reason, the helper functions.

### 2026-11-20 20:44:03 UTC / 2026-11-21 02:14:03 IST local - START step c3 (human review of FY 2025-26 raw_v2) - review clock starts
Best raw compile: raw_v2 (the only one passing the strict allow-list; 44/44 dev, 12/12 official). Evidence allowed: dev and official-12 failures (none for raw_v2) and reading the code against RULES_FY2025_26.md. Heldout not looked at.

### 2026-11-20 20:45:59 UTC / 2026-11-21 02:15:59 IST local - END step c3 (review final) - review clock stops
Review took from 20:44:03 UTC (start entry) to this entry's timestamp, all human-review time (reading, editing, confirming), no LLM calls. Wrote project_a/compiler/compiled/fy2025_26/reviewed.py (strict allow-list PASSED, 284 code lines), reviewed.diff (code only, header excluded: 39 lines removed, 34 added), REVIEW.md, and a 'reviewed' manifest entry with fy 2025-26 (needed by the year guard).
Defects in raw_v2, all found by reading; none changes any dev or official answer:
D1 HRA truncated (wrong for any basic not divisible by 10; constructed case total income 9,83,350 vs 9,83,340).
D2 surcharge and cess truncated (constructed gross 60,00,186: new tax 15,53,040 vs 15,53,050, old 18,27,600 vs 18,27,610).
D3 invented constant 1276595 guarding the 87A relief (harmless this year, a law-change trap).
D4 trace mislabels.
D1 and D2 would be SILENT failures (a plausible answer Rs 10 off, no error) in cases no test covers. Confirmed by comparing raw_v2 with reviewed on inputs I constructed; no oracle, no heldout.
Reviewed: dev 44/44, official 12/12, strict. Heldout still not run or looked at.

### 2026-11-20 20:46:13 UTC / 2026-11-21 02:16:13 IST local - START step c4 (compiled: heldout, once, after the review)
raw_v1/v2/v3 (diagnostic) and reviewed (strict) on the 129 FY 2025-26 heldout cases, once, with --allow-heldout; also excluding heldout cases that overlap the 12 official rows. No LLM calls.

### 2026-11-20 20:46:35 UTC / 2026-11-21 02:16:35 IST local - END step c4
Heldout (129 FY 2025-26 cases), run once after the review. Tax exact / income exact, then excluding the 7 heldout cases that overlap the official 12 (n = 122):
- raw_v1 (diagnostic): 118/129, 129/129; 112/122.
- raw_v2 (diagnostic; it also passes strictly, so the strict result is the same): 129/129, 129/129; 122/122.
- raw_v3 (diagnostic): 47/129, 129/129; 46/122.
- reviewed (strict): 129/129, 129/129; 122/122.
No LLM calls. The raw_v1 and raw_v3 heldout errors are the same failure modes seen on dev (surcharge relief; doubled tax), and they would be silent without the allow-list.

### 2026-11-20 20:46:47 UTC / 2026-11-21 02:16:47 IST local - START+END step c5 (year guard)
Confirmed both ways with the real artifacts: FY 2024-25 reviewed on a FY 2025-26 input raises UnknownRuleVersion ('compiled for FY 2024-25, not FY 2025-26'), and FY 2025-26 reviewed on a FY 2024-25 input raises ('compiled for FY 2025-26, not FY 2024-25'). Both are loud. Kept as tested behaviour: test_task5b.py has 9 tests (2 added in this step). The guard is the engine's check of the artifact's manifest fy (changed in B-prep, 2 lines removed / 3 added). The generated code itself never checks fy, which is why step 0 found 136/173 silent failures without the guard.

### 2026-11-20 20:48:42 UTC / 2026-11-21 02:18:42 IST local - END step b1
Added project_a/task5/analyse_query.py and project_a/task5/query_stale.py; no engine change.
(1) The UNCHANGED query-time engine on a FY 2025-26 input fails LOUDLY before any LLM call (UnknownRuleVersion 'no rules text for FY 2025-26'): its RULES_FILES table has no FY 2025-26 entry.
(2) Forgotten-update simulation (FY 2025-26 mapped in memory to RULES_FY2024_25.md), 44 dev cases, 44 live calls to gemini-3.1-flash-lite, no 429 or 5xx (245 requests on that model today). Tax exact 7/44; within Rs 10 8/44; income exact 34/44; best regime 41/44; errors 0; SILENT wrong 37/44. Mean latency 3.51 s; tokens 100,017 prompt / 23,071 output; notional US$1.99 per 1,000.
Old-law consistency: of the 34 wrong new-regime taxes, 0 exactly equal the FY 2024-25 oracle's answer, but 31 of 34 are NEARER the old-law answer than the new one (for example gross 12,75,000: model 93,600, old law 83,200, new law 0). The model follows the stale rules but with arithmetic errors. 37/44 old-regime taxes are also wrong although the old regime is unchanged, so the model's arithmetic is unreliable whatever text it gets.
Measure added after seeing this result (stated so the change is visible): 'nearer old law' counts wrong new-regime taxes closer to the FY 2024-25 answer than to the FY 2025-26 answer, where the two differ.

### 2026-11-20 20:48:54 UTC / 2026-11-21 02:18:54 IST local - START step b2 (update the query-time engine)
Point LLMQueryEngine at RULES_FY2025_26.md, then run all 173 FY 2025-26 cases once (--split all --allow-heldout): 173 live calls to gemini-3.1-flash-lite.

### 2026-11-20 21:29:52 UTC / 2026-11-21 02:59:52 IST local - b2 BROKE: network failure mid-run (loud), harness fixed, re-run
Engine change for b2 (the law change itself): project_a/engines/llm_query.py 1 line changed (RULES_FILES gains '2025-26': 'RULES_FY2025_26.md'); nothing else needed, because the prompt wording has nothing year-specific.
WHAT BROKE: the 173-case run started 20:49:05 UTC; 51 calls succeeded, then 6 consecutive transport errors (urlopen Errno 60 'Operation timed out', the client's 1 + 5 retries) at about 21:07 UTC, and the harness crashed with LLMError. That is a LOUD failure: no wrong answers, but no results file. The harness only caught 429 (QuotaExhausted), not network errors. The 51 completed calls are in llm_cache, so nothing was lost.
FIX: project_a/eval/run.py now treats any LLMError other than CacheMiss like a quota error: it writes partial results, marks the rest 'not run: network error' and exits 3 (8 lines removed, 15 added). New test in test_task5b.py. Re-running the same command in auto mode: the 51 cached cases replay and only the remaining 122 are called.

### 2026-11-20 21:30:01 UTC / 2026-11-21 03:00:01 IST local - CORRECTION to the previous entry (line count)
The previous entry says the run.py network-error fix was '8 lines removed, 15 added'. The difflib measurement printed in the same command says 7 removed, 13 added; the logged numbers were typed before the measurement was read. Correct: 7 removed, 13 added. This is the third time in Task 5 I logged a number before checking it (see the earlier corrections), so from now on I measure in one command and log in the next.

### 2026-11-20 21:45:35 UTC / 2026-11-21 03:15:35 IST local - END step b2 (re-run complete)
Results file project_a/eval/results/llm_query_fy2025_26-all_n173_r1_auto_20261120T214417Z.json (173 cases, dev and heldout together; the one run with --allow-heldout). Re-run 21:29:52 to 21:44:17 UTC: 116 network calls, 57 cache hits (51 from the interrupted run, plus 6 duplicate inputs that send the same prompt), 0 retries, 0 429, 0 5xx. LLM requests for b2 overall: 173 (167 HTTP 200, 6 transport errors), gemini-3.1-flash-lite (418 requests on that model today).
Accuracy after the update (tax exact both regimes / within Rs 10 / income exact / best regime):
- ALL 30/173, 32, 121, 165.
- dev 7/44, 8, 32, 43.
- heldout 23/129, 24, 89, 122.
By category (tax exact): degenerate 8/8, old_regime 15/27, new_slab_boundary 3/15, age_boundary 2/8, rebate_12l 1/17, realistic 1/30, deduction_caps 0/24, hra 0/16, surcharge 0/28.
Errors 0; SILENT wrong 143/173 (dev 37, heldout 106). Latency mean 2.86 s, p95 4.01 s (dev 2.87 / 3.93, heldout 2.86 / 3.88). Tokens 483,243 prompt / 89,348 output (dev 122,897 / 22,016; heldout 360,346 / 67,332). Notional US$2.13 per 1,000 (dev 2.09, heldout 2.14; unverified prices).

### 2026-11-20 21:45:35 UTC / 2026-11-21 03:15:35 IST local - START+END step b3 (anchoring after the update)
No LLM calls. Of the 143 silent-wrong answers, 118 have a wrong new-regime tax; 115 of those are cases where the two laws give different new-regime taxes. Of the 115, 0 exactly equal the FY 2024-25 oracle, but 46 (40%) are NEARER the old-law answer than the new-law one. In the stale run (b1) it was 31 of 34 (91%). The update moved most answers away from the old law but did not make them right; 134 of 173 old-regime taxes are also wrong although the old regime is unchanged, so arithmetic error dominates. Five examples, nearest the old law relative to the gap between the laws (model / FY26 oracle / FY25 oracle, new-regime tax):
- realistic-30, age 63, gross 1 Cr, 80C 1L, 80D 50k: 30,49,800 / 29,25,780 / 30,51,620.
- hra-14, metro, gross 12L, basic 3,33,333, HRA 2L, rent 3L: 67,600 / 0 / 71,500.
- surcharge-16, gross 65.5L: 18,56,400 / 17,41,740 / 18,67,580.
- rebate_12l-01, gross 11,99,999: 65,000 / 0 / 71,500.
- old_regime-09, gross 10,50,010: 41,600 / 0 / 49,400.
In three of the five the model charges new-regime tax below 12L total income, where the new 87A rebate makes it 0: the dangerous kind of silent failure.

### 2026-11-20 21:46:33 UTC / 2026-11-21 03:16:33 IST local - FINAL SUMMARY (Task 5 parts A and B)
Full pytest: 1070 passed. LLM requests in part B (llm_cache/usage.jsonl, 20:39:46 to now): gemini-3.1-flash-lite 211 OK + 6 transport errors; gemini-3.5-flash-lite 3 OK; gemini-3.5-flash and gemini-3-flash-preview 0. No 429. Key scan: 0 of 726 files contain the key prefix.

| Implementation | Time to apply the change (wall clock; human review) | Files and lines changed | LLM calls; tokens | What broke | Silent failures: stale baseline / after update | Tax exact after update: dev / heldout |
|---|---|---|---|---|---|---|
| Hand-coded | params + rules written and engine verified 19:57:23-19:58:09 (46 s); oracle 19:58:21-19:59:19 (58 s); all of part A incl. suite, tests, baseline and sheet 19:54:49-20:04:52 (10 min). Review: none separate. | engine 0 lines; new params_fy2025_26.json + RULES_FY2025_26.md; oracle 7 removed / 16 added | 0; 0 | nothing in the engine; my own duplicate test input (fixed) | 0 (unchanged engine LOUD 173/173) / 0 | 44/44 / 129/129 (official 12/12) |
| LLM at query time | code change 1 line at about 20:49; run 20:49:05-21:44:17 (55 min, about 22 min of it a network outage, 21:07-21:29). Review: none. | llm_query.py 1 line; harness for the suite (run.py 17 removed / 28 added) and the network fix (7 removed / 13 added); analysis scripts | 217 requests (b1 44, b2 173 incl. 6 failed); tokens b1 100,017 / 23,071, b2 483,243 / 89,348 | network timeout crashed the first b2 run (loud, no results file); harness fixed | unchanged engine LOUD (no rules text); forgotten-update simulation 37/44 SILENT on dev (31 of 34 wrong new taxes nearer the old law) / 143/173 SILENT (dev 37, heldout 106; 46 of 115 still nearer the old law) | 7/44 / 23/129 |
| LLM-compiled | compiles 20:42:49-20:43:22; evaluation to 20:43:56; review 20:44:03-20:45:59 (1 min 56 s, done by the assistant, not a person); heldout 20:46:13-20:46:35. Prep edits for the compiler and year guard inside 20:40:14-20:42:07 (shared with the harness; roughly 1 min is compiler-specific, an estimate). | compile.py 10 removed / 17 added; llm_compiled.py 2 removed / 3 added (year guard); reviewed.py 39 code lines removed / 34 added against raw_v2 | 3; 8,832 prompt / 12,563 output | raw_v1 and raw_v3 rejected (helper functions); raw_v1 surcharge relief wrong; raw_v3 roughly doubles tax; raw_v2 latent truncation (D1, D2) and an invented constant (D3) | FY 2024-25 artifact via the engine LOUD 173/173; same code without the guard 136/173 SILENT / reviewed 0 measured (dev, official, heldout); 2 latent defect classes fixed in review (would be silent, Rs 10 off) | reviewed 44/44 / 129/129 (122/122 excluding the 7 heldout cases in the official 12); raw_v2 the same; raw_v1 41/44 and 118/129, raw_v3 16/44 and 47/129 (diagnostic) |

Estimates: the compiler-specific share of the prep time. Everything else is measured from this log or the results files. Earlier corrections in this log were appended, never rewritten.

### 2026-11-21 05:13:35 UTC / 2026-11-21 10:43:35 IST local - CLARIFICATION (who made the terminal runs; prices)
The entries above that say results files or calls came from 'another process' or 'outside runs' refer to runs made by the author from a terminal with the same harness and cache: llm_query_dev_n52_r1_auto_20261120T190205Z.json, llm_query_heldout_n156_r1_auto_20261120T193052Z.json and nl_v1_heldout_auto_20261120T195740Z.json, with their calls. The earlier entries are left as written. This note does not cover the gemini-3-flash-preview requests (8 in usage.jsonl, 20:05-20:18 UTC); no results file corresponds to them. Separately, the notional prices were corrected on 2026-11-21 (see eval/MEASUREMENT_LOG.md): the 'US$1.99' and 'US$2.13 per 1,000' figures above used the old flat price; at US$0.25 / 1.50 per 1M tokens the same token counts give the costs in RESULTS_TABLES.md.
