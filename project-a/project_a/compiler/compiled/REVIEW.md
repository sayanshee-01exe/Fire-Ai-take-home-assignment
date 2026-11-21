# Review of the LLM-compiled engine (raw_v1 -> reviewed.py)

Model `gemini-3.5-flash-lite`, temperature 0.2, three compiles (run_index 0, 1, 2), prompt sha256 `27200f8c…`.
Evidence allowed for this review: failures on the dev split (52 cases) and the official 30, and reading the
code against `rules/RULES_FY2024_25.md`. No held-out case or held-out expected value was used.

## The three raw compiles

| Compile | Code lines | Strict allow-list | Dev 52, helpers allowed (diagnostic) | Official 30, helpers allowed (diagnostic) |
|---|---:|---|---|---|
| raw_v1 | 226 | rejected: 1 top-level helper + 4 nested helpers, 13 violations | 52/52 | 30/30 |
| raw_v2 | 292 | rejected: nested helpers, 5 violations | 45/52 tax exact | 25/30 tax exact |
| raw_v3 | 288 | rejected: nested helpers, 18 violations | 52/52 | 30/30 |

Under the strict allow-list every query to a raw compile is an error result (0/52, 0/30): all three ignored the
"exactly one function" restriction stated in the prompt. Every violation was a helper-function definition or a
call to one; nothing else (no imports, no attribute access). The diagnostic column runs the same code with only
that restriction relaxed (`--diagnostic-allow-helpers`), to separate "broke the format" from "got the tax wrong".

raw_v2's logic errors (seen on dev and official): old-regime surcharge wrong in every surcharge case (for example
surcharge-03 old tax 10,27,000 against 14,43,000), and a tie between regimes reported as "old" (the rules say a
tie goes to "new"). raw_v1 and raw_v3 agree with each other and with the expected values on all 82 inputs.

raw_v1 was chosen for review: it is tied with raw_v3 on accuracy and is the shortest.

## Defects found in raw_v1 and the edits made

| # | Defect | Where (raw_v1 line) | Found by | Edit in reviewed.py |
|---|---|---|---|---|
| R1 | Helper functions: top-level `round_nearest_10` and nested `calc_slab_old`, `calc_slab_new`, `get_surcharge_rate`, `compute_surcharge_and_mr`. Breaks the one-function allow-list, so the artifact cannot run. | 9, 92, 118, 133, 148 | allow-list | Inlined. Rounding written out at each of its 4 uses; the per-regime helpers became one pass of `for regime in ["old", "new"]`, and the slab helper a loop over `[income, threshold]`. Helper bodies kept as written. |
| R2 | HRA limits truncated: `basic * pct // 100` and `basic * 10 // 100` drop the paise of 40% / 50% / 10% of basic, so the exemption can be up to Rs 0.90 off and total income can cross a C-288A rounding boundary. The rules ask for the exact amounts. | 34, 35 | reading | HRA, standard deduction and the old-regime subtotal kept in hundredths of a rupee; paise dropped once, on the total, then rounded. Example (constructed, metro, gross 12,00,000, basic 3,33,311, HRA 2,50,000, rent 4,00,000): raw_v1 total income 9,83,350, exact rule 9,83,340. |
| R3 | Surcharge, marginal relief and cess each floored with `// 100`, so the final figure can be up to about Rs 2 low before C-288B rounding, flipping a round-to-10. The rules round only the final tax. | 150, 176, 194, 217 | reading | Tax plus surcharge kept x100, the marginal-relief cap compared x100, cess applied exactly (x104, so x10000), paise dropped once, then rounded. Example (constructed, tax after rebate 13,00,022, surcharge 10%): raw_v1 14,87,220, exact rule 14,87,230. |
| R4 | Invented constant: the new-regime marginal relief is guarded by `new_total_income <= 727770`, a number that is not in the rules (the zone actually ends near 7,22,222). Harmless for FY 2024-25 because relief cannot bind above that point, but it is the stale-literal trap of FAILURES F-000 and would silently break a rules change. | 206 | reading | Guard removed: relief applies whenever total income exceeds 7,00,000, exactly as C-87A-NEW-MR says. |

Checked against the rules and found correct, so not edited: age bands for the old slabs (60, 80), both slab
tables, the 80C / 80CCD(1B) / 24(b) caps, the 80D caps (self 25k or 50k at 60+, parents 25k or 50k if a parent
is a senior, capped separately), the standard deduction limited to salary after exempt HRA, the old-regime 87A
rebate (no marginal relief), the surcharge rates and thresholds (37% old above 5 crore, 25% cap new), the rate of
the band below each threshold for marginal relief, cess on tax plus surcharge, and the tie rule.

R2 and R3 change no answer on the dev or official cases (their amounts are round), which is why only reading
found them. The two examples above are amounts I constructed to confirm the reading, computed from the rules'
arithmetic; no oracle or test case was consulted for them.

## Result

| Artifact | Strict allow-list | Dev 52 tax exact | Official 30 tax exact |
|---|---|---|---|
| raw_v1 | rejected | 0/52 (52/52 diagnostic) | 0/30 (30/30 diagnostic) |
| raw_v2 | rejected | 0/52 (45/52 diagnostic) | 0/30 (25/30 diagnostic) |
| raw_v3 | rejected | 0/52 (52/52 diagnostic) | 0/30 (30/30 diagnostic) |
| reviewed | passed | 52/52 | 30/30 |

`reviewed.diff` is large (225 changed lines) because R1 forced the helpers to be inlined; the logic changes
(R2 to R4) are about 20 lines. A rules change means recompiling and repeating this review (see README).
