# Review of the FY 2025-26 LLM-compiled engine (raw_v2 -> reviewed.py)

Model `gemini-3.5-flash-lite`, temperature 0.2, three compiles (run_index 0, 1, 2) from `RULES_FY2025_26.md`,
prompt sha256 `92463708…`. Evidence used: the FY 2025-26 dev split (44 cases), the 12 official rows, and reading
the code against the rules text. No held-out case, result or expected value was looked at before this review
was final. Review time: 20:44:03 to 20:45:59 UTC, 1 min 56 s (TASK5_LOG.md). The "human review" was done by the
assistant (Claude), not by a person; a person would take longer.

## The three raw compiles

| Compile | Code lines | Strict allow-list | Dev 44 tax exact | Official 12 tax exact | What goes wrong |
|---|---:|---|---|---|---|
| raw_v1 | 215 | rejected (helper functions) | 0 strict; 41 diagnostic | 0 strict; 11 diagnostic | surcharge marginal relief (for example surcharge-03 old tax 14,66,400, should be 14,43,000) |
| raw_v2 | 289 | **passed** | **44** | **12** | nothing visible on dev or official; see defects below |
| raw_v3 | 211 | rejected (helper functions) | 0 strict; 16 diagnostic | 0 strict; 1 diagnostic | tax roughly doubled (for example old tax 3,74,400, should be 1,87,200) |

"Diagnostic" relaxes only the one-function rule. Under it, raw_v1's and raw_v3's errors are silent (answers, no
error); under the strict allow-list they are loud (rejected), but for an unrelated reason (helper functions).

## Defects found in raw_v2 (all by reading; none changes a dev or official answer)

| # | Defect | raw_v2 line | Would be wrong for | Edit |
|---|---|---|---|---|
| D1 | HRA limits truncated with `// 100` (40%/50% of basic, 10% of basic), up to Rs 0.90 off, which can move total income across a C-288A rounding boundary. Same defect as R2 in the FY 2024-25 review. | 30, 31 | a basic salary that is not a multiple of 10. Constructed example: metro, gross 12,00,000, basic 3,33,311, HRA 2,50,000, rent 4,00,000. raw_v2 gives total income 9,83,350 and old tax 1,13,540; the exact rule gives 9,83,340 and 1,13,530. | HRA, standard deduction and the old-regime subtotal kept in hundredths of a rupee; paise dropped once, then rounded. |
| D2 | Surcharge, its marginal relief and cess each floored with `// 100`, so the final figure can be up to about Rs 2 low before C-288B rounding, flipping a round-to-10. The rules round only the final tax. Same as R3. | 162, 212, 221, 273, 290, 299 | any surcharge case beyond the relief zone where the dropped paise matter. Constructed example: gross 60,00,186 (new TI 59,25,190): new tax 15,53,040 in raw_v2, 15,53,050 by the rules (15,53,045.21 before rounding); old tax 18,27,600 against 18,27,610. | Tax plus surcharge kept x100, the relief cap compared x100, cess exact (x104), paise dropped once, then rounded. |
| D3 | Invented constant: the new-regime marginal relief only applies when `new_ti <= 1276595`, a number not in the rules (the zone actually ends near 12,70,588). Harmless this year because relief cannot bind above that point, but it is the stale-literal trap of FAILURES F-000 and R4, and it would silently break the next rules change. | 258 | no FY 2025-26 input; any future change to the 87A limit, maximum or slab rates | Removed: relief applies whenever total income exceeds 12,00,000, as C-87A-NEW-MR says. |
| D4 | Trace mislabels: `C-87A-OLD` / `C-87A-NEW` record the tax after the rebate instead of the rebate, `C-CESS` records the rounded final tax instead of the cess, and there is no `C-288B` step. The numbers are right; traceability is wrong. | 323, 325, 327, 329 | every answer's trace | Steps now record the rebate (plus any relief), the cess, and a separate C-288B final figure. |

Checked against RULES_FY2025_26.md and found correct (not edited): age bands (60, 80); both slab tables (new:
4/8/12/16/20/24 lakh at 5% to 30%); old-regime 87A (5 lakh, 12,500, no relief); new-regime 87A (12 lakh, 60,000)
and relief applied before surcharge; surcharge bands (10/15/25/37 old, 25% cap new) and thresholds; the band below
each threshold for the relief (0/10/15/25 old, 0/10/15 new); the threshold slab taxes (new 3,00,000 at 24 lakh;
old 1,12,500 / 1,10,000 / 1,00,000 by age); 80C, 80CCD(1B), 80D (self and parents, senior caps) and 24(b) caps;
standard deductions (50,000 limited to salary after HRA; 75,000 new); cess on tax plus surcharge; the tie rule.
Noted, not edited: the threshold slab taxes are hard-coded amounts (correct now, but they would need re-deriving if
the slabs change); `inp["fy"]` is read and never used (the engine's year guard covers it).

## Result before heldout

| Artifact | Strict allow-list | Dev 44 tax exact | Official 12 tax exact |
|---|---|---|---|
| raw_v1 | rejected | 0 (41 diagnostic) | 0 (11 diagnostic) |
| raw_v2 | passed | 44 | 12 |
| raw_v3 | rejected | 0 (16 diagnostic) | 0 (1 diagnostic) |
| reviewed | passed | 44 | 12 |

Diff: `reviewed.diff` (code lines only, header excluded: 39 removed, 34 added; D1 9 lines, D2 18, D3 1, D4 9 plus
header).
