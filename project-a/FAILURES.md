# FAILURES.md - what went wrong while building, and how it was caught

Newest last. Each entry: what failed, how it was found, the fix, and what stops it recurring.

## F-000 Prototype hard-coded the 7,00,000 rebate limit
- **What:** the first prototype (`_prototype/hand_coded.py`) put the new-regime 87A limit as a literal inside the marginal-relief branch.
- **Why it matters:** for FY 2025-26 the limit is 12,00,000. A stale literal silently gives a wrong answer
  (FY 2025-26 gross 11,75,000 should be Rs 0; the prototype with the old literal gives Rs 52,000). This is failure LAW-1 in PLAN.md.
- **Fix:** every number lives in `rules/params_fy*.json`; the engine reads them and nothing else. Unknown FY raises `UnknownRuleVersion`.
- **Guard:** the law-change task (Task 5) is run by adding `params_fy2025_26.json` only, and timed.

## F-001 PLAN.md claimed "no cliffs anywhere"; that is false for the old regime
- **What:** PLAN section 6.2 said raising income by x never raises tax by more than 1.04x. In the old regime the 87A rebate (Rs 12,500, up to Rs 5,00,000)
  has no marginal relief, so tax goes from 0 to Rs 13,000 between gross 5,50,000 and 5,50,010.
- **How found:** while writing the property tests (checked the window around 5L by hand before running).
- **Fix:** PLAN.md section 6.2 corrected; property windows skip that point; `test_old_regime_has_a_real_cliff_at_5_lakh` documents it.
- **Open:** confirm on the official calculator (risk R1).

## F-002 Prototype smoke test had placeholder expected values
- **What:** the first smoke test contained numbers I had written without computing (148,200; 154,700; `8_320*0+0`).
- **How found:** hand-working each case before running anything.
- **Fix:** replaced with hand-derived values (59,800; 60,840; 0). The hand-worked cases H01-H10 are now a permanent suite that checks both the oracle and the engine.
- **Lesson (for AI_USAGE.md):** an AI-written expected value is not evidence; only a value derived independently is.

## F-003 Mutation check found a hole: no case sat on the age boundaries
- **What:** with the senior band changed from `age >= 60` to `age > 60`, all 484 tests still passed.
- **How found:** `python -m project_a.tests.mutation_check` injects 15 single bugs and runs the suite.
- **Fix:** added an `age_boundary` category (ages 59/60/79/80 and the 80D self cap at 59/60). Suite is now 208 cases (52 dev, 156 heldout).
- **Result:** 14 of 15 mutants caught; the other is equivalent (F-004).

## F-004 Surcharge `>` vs `>=` at a threshold is not observable once marginal relief exists
- **What:** mutant M2 (`income >= threshold` instead of `>`) survives, and it should: at exactly 50,00,000 the relief cap makes the
  total equal to the tax with no surcharge, the same as the correct rule.
- **Why it matters:** the Rs 13,61,360 vs Rs 12,37,600 failure in PLAN.md comes from `>=` **without** relief, not from `>=` itself.
  The test that protects against it is the marginal-relief one (mutant M3, caught).
- **Status:** documented as an equivalent mutant in `mutation_check.py`.

## Known limits of the current test evidence (not failures yet)
- Oracle and engine were written by the same author (R6); the hand-worked cases and the official-calculator checks are the independent evidence.
- Zero official-calculator checks are done so far. The assignment requires at least 30. Until then every golden case has `source: "oracle"`.
- 80C / 80CCD(1B) / 24(b) / 80D limits are not yet confirmed on an official page (R3).
