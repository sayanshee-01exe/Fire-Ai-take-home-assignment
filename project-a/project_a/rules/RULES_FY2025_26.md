# Salaried-individual income-tax rules: FY 2025-26 (AY 2026-27)

Single source of rule text for all three implementations (hand-coded, LLM-at-query-time, LLM-compiled).
Numbers here must match params_fy2025_26.json (the hand-coded engine reads that file).
Each rule has a clause ID. Every rupee in an answer must trace back to one of these IDs.
Scope: resident individual, salary income only. No capital gains, business or other heads.
Income Tax Act 1961 as amended by the Finance Act 2025.

Sources (official): Union Budget 2025-26 speech; PIB press release 2098406; Finance Act 2025 highlights on
incometaxindia.gov.in. Unchanged rules: as in RULES_FY2024_25.md and its sources.

## Changes from FY 2024-25

| Clause | FY 2024-25 | FY 2025-26 | Source |
|---|---|---|---|
| C-SLAB-NEW | 0-3L nil, 3-7L 5%, 7-10L 10%, 10-12L 15%, 12-15L 20%, above 15L 30% | 0-4L nil, 4-8L 5%, 8-12L 10%, 12-16L 15%, 16-20L 20%, 20-24L 25%, above 24L 30% | Budget 2025-26 speech; PIB 2098406; Finance Act 2025 highlights |
| C-87A-NEW | total income up to 7,00,000: rebate up to 25,000 | total income up to 12,00,000: rebate up to 60,000 | same |
| C-87A-NEW-MR | above 7,00,000: tax cannot exceed income above 7,00,000 | above 12,00,000: tax cannot exceed income above 12,00,000. **Confirmed on the official calculator, rows OC26-03 to OC26-06 (12 of 12 official rows match).** | same |

Everything else is unchanged: standard deductions (50,000 old, 75,000 new), the old-regime slabs and the
old-regime 87A rebate, HRA, deduction limits, surcharge rates and the new-regime 25% cap, cess and rounding.

## Deductions from salary

- **C-STD-OLD** (s.16(ia)): standard deduction Rs 50,000, old regime. Limited to salary left after exempt HRA.
- **C-STD-NEW** (s.16(ia)): standard deduction Rs 75,000, new regime.
- **C-HRA** (s.10(13A), Rule 2A): old regime only. Exempt HRA = least of
  (a) HRA actually received, (b) 50% of salary if rented home is in Delhi, Mumbai, Kolkata or Chennai,
  else 40% of salary, (c) rent paid minus 10% of salary. Floor at 0.
  "Salary" = basic + dearness allowance (+ commission on fixed % of turnover).
  FY 2025-26: only the four cities above are metro. (Eight cities apply from FY 2026-27, not here.)
- **C-24B** (s.24(b)): old regime only. Interest on loan for a self-occupied house, deductible up to Rs 2,00,000.
  Not allowed in the new regime.

## Chapter VI-A deductions (old regime only)

- **C-80C** (s.80C): up to Rs 1,50,000.
- **C-80CCD1B** (s.80CCD(1B)): additional NPS up to Rs 50,000, over and above 80C.
- **C-80D-SELF** (s.80D): premium for self, spouse and children: up to Rs 25,000;
  Rs 50,000 if the taxpayer is 60 or older.
- **C-80D-PARENTS** (s.80D): premium for parents: up to Rs 25,000; Rs 50,000 if a parent is 60 or older.
  The two 80D blocks are capped separately and then added. Preventive health check-up (up to Rs 5,000)
  sits inside these limits.

## Rounding

- **C-288A**: total income is rounded to the nearest Rs 10 (last digit 5 or more rounds up).
- **C-288B**: final tax payable is rounded to the nearest Rs 10 the same way.

## Slabs on total income

**C-SLAB-OLD** (age on 31 March 2026 decides the table; unchanged from FY 2024-25):

| Age | Slab | Rate |
|---|---|---|
| below 60 | up to 2,50,000 | nil |
| | 2,50,000 to 5,00,000 | 5% |
| | 5,00,000 to 10,00,000 | 20% |
| | above 10,00,000 | 30% |
| 60 to under 80 | up to 3,00,000 | nil |
| | 3,00,000 to 5,00,000 | 5% |
| | 5,00,000 to 10,00,000 | 20% |
| | above 10,00,000 | 30% |
| 80 and above | up to 5,00,000 | nil |
| | 5,00,000 to 10,00,000 | 20% |
| | above 10,00,000 | 30% |

**C-SLAB-NEW** (s.115BAC(1A) as amended by the Finance Act 2025, all ages): up to 4,00,000 nil;
4,00,001 to 8,00,000 5%; 8,00,001 to 12,00,000 10%; 12,00,001 to 16,00,000 15%; 16,00,001 to 20,00,000 20%;
20,00,001 to 24,00,000 25%; above 24,00,000 30%.

## Rebate s.87A (resident individuals)

- **C-87A-OLD**: total income up to Rs 5,00,000: rebate = lower of tax and Rs 12,500. No marginal relief.
- **C-87A-NEW**: total income up to Rs 12,00,000: rebate = lower of tax and Rs 60,000.
- **C-87A-NEW-MR** (marginal relief): if total income is above Rs 12,00,000, tax cannot exceed
  (total income minus Rs 12,00,000). Applied before surcharge and cess. *Confirmed on the official calculator, rows OC26-03 to OC26-06 (12 of 12 official rows match), AY 2026-27.*

## Surcharge (on income tax after rebate)

- **C-SUR**: total income above 50 lakh: 10%; above 1 crore: 15%; above 2 crore: 25%;
  above 5 crore: 37% under the old regime, capped at 25% under the new regime.
- **C-SUR-MR** (marginal relief): tax plus surcharge cannot exceed (tax plus surcharge payable at the
  threshold income) plus (total income minus threshold). Thresholds: 50 lakh, 1 crore, 2 crore, 5 crore.
  Tax at the threshold uses the surcharge rate of the band below it.

## Cess

- **C-CESS**: health and education cess 4% on (income tax + surcharge).

## Order of computation

1. Gross salary minus exempt HRA (old only), minus standard deduction; minus s.24(b) interest (old only).
2. Minus Chapter VI-A deductions (old only). Floor at 0. Round per C-288A. = Total income.
3. Slab tax, then 87A rebate (and its marginal relief), then surcharge with its marginal relief, then cess.
4. Round per C-288B. Compare regimes: choose the lower final tax (tie goes to the new regime).
