# Calculator notes (fill in while checking)

## Procedure (Advanced Calculator tab)

Setup for every row: **Advanced Calculator** tab; Act 1961; Type of ITR "Other than Updated";
AY 2025-26; Individual; Resident; leave both dates at their defaults; click the age button matching
`age_category`.

1. **New-regime run:** toggle New Tax Regime. Salaries panel, line (i) Net Salary Income =
   `adv_net_salary_new`. Enter nothing else. Record Taxable Income as `official_new_total_income`
   and Balance Tax Payable / Refundable as `official_new_tax`.
2. **Old-regime run:** toggle Old Tax Regime. Line (i) = `adv_net_salary_old`. Deductions panel:
   80C = `sec_80c`, 80CCD(1B) = `sec_80ccd1b`, 80D = `adv_80d_total`. House Property panel,
   self-occupied "Interest on Borrowed Capital" = `home_loan_interest`. Record Taxable Income as
   `official_old_total_income` and Balance Tax Payable / Refundable as `official_old_tax`.

Record "Balance Tax Payable / Refundable": it is rounded to the nearest 10 (s.288B), as our engine
reports tax. Do not record "Total Tax on Income": it is the unrounded figure and can differ by up to
Rs 5. Never record the interest (234A/B/C) or late-fee (234F) lines; they depend on the dates.

## Notes

Fill this in once, before typing the 30 rows into the calculator, and update it if anything changes.

- Calculator URL:
- Date checked:
- Assessment-year option selected (should be AY 2025-26 for FY 2024-25):
- Taxpayer category / residential status selected:
- Fields the form asked for (list them):
- Is the standard deduction (50,000 old / 75,000 new) applied automatically, or did you enter it?
- Is the HRA exemption entered as an amount, or does the form compute it from basic / rent / city?
- Is tax rounded to the nearest 10 (section 288B)? Is total income rounded to the nearest 10 (288A)?
- Does the result include 4% cess? Surcharge with marginal relief?
- Does the old-regime 87A rebate show at total income 5,00,000? Does the new-regime 7L marginal relief show?
- Inputs that did not map to our fields (and what you did instead):
- Anything else odd:
