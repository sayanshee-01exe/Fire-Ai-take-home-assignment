# Salaried-individual income-tax rules: FY 2024-25 (AY 2025-26)

Single source of rule text for all three implementations (hand-coded, LLM-at-query-time, LLM-compiled).
Each rule has a clause ID. Every rupee in an answer must trace back to one of these IDs.
Scope: resident individual, salary income only. No capital gains, business or other heads.

Sources (official): Income Tax Department tax-rates page
(incometaxindia.gov.in/Documents/Left Menu/TAX RATES_Indvl.htm), HRA FAQ page on incometaxindia.gov.in,
Finance Act 2024 (Finance (No. 2) Act 2024 for the new-regime standard deduction), Income-tax Act 1961.

## Deductions from salary

- **C-STD-OLD** (s.16(ia)): standard deduction Rs 50,000, old regime. Limited to salary received.
- **C-STD-NEW** (s.16(ia) as amended by Finance (No. 2) Act 2024): standard deduction Rs 75,000, new regime.
- **C-HRA** (s.10(13A), Rule 2A): old regime only. Exempt HRA = least of
  (a) HRA actually received, (b) 50% of salary if rented home is in Delhi, Mumbai, Kolkata or Chennai,
  else 40% of salary, (c) rent paid minus 10% of salary. Floor at 0.
  "Salary" = basic + dearness allowance (+ commission on fixed % of turnover).
  FY 2024-25: only the four cities above are metro. (Eight cities apply from FY 2026-27, not here.)
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

**C-SLAB-OLD** (age on 31 March 2025 decides the table):

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

**C-SLAB-NEW** (s.115BAC(1A), all ages): up to 3,00,000 nil; 3,00,001 to 7,00,000 5%;
7,00,001 to 10,00,000 10%; 10,00,001 to 12,00,000 15%; 12,00,001 to 15,00,000 20%; above 15,00,000 30%.

## Rebate s.87A (resident individuals)

- **C-87A-OLD**: total income up to Rs 5,00,000: rebate = lower of tax and Rs 12,500. No marginal relief.
- **C-87A-NEW**: total income up to Rs 7,00,000: rebate = lower of tax and Rs 25,000.
- **C-87A-NEW-MR** (marginal relief): if total income is above Rs 7,00,000, tax cannot exceed
  (total income minus Rs 7,00,000). Applied before surcharge and cess.

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
