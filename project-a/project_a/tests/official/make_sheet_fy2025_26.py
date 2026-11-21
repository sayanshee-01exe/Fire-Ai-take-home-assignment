"""FY 2025-26 official-calculator sheet (12 rows, AY 2026-27, Income Tax Act 1961).

    python3 -m project_a.tests.official.make_sheet_fy2025_26 [--force]

Same columns and procedure as the FY 2024-25 sheet (Advanced calculator; record "Balance Tax Payable",
rounded to the nearest 10). Rows are chosen from cases/golden_fy2025_26.jsonl by input, deterministically.
The FY 2024-25 sheet and make_sheet.py are not touched. Refuses to overwrite a filled sheet unless --force.
"""
import csv
import json
import sys
from pathlib import Path

from project_a.tests.official import make_sheet as ms

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parent / "cases" / "golden_fy2025_26.jsonl"
CSV = HERE / "official_checks_fy2025_26.csv"
MD = HERE / "official_checks_fy2025_26.md"

# (category, gross salary, age, why). Gross = total income + 75,000 in the new regime.
ROWS = [
    ("rebate_12l", 1_274_990, 30, "new TI 11,99,990: just below the 12L rebate limit (tax 0)"),
    ("rebate_12l", 1_275_000, 30, "new TI 12,00,000: exactly at the limit (tax 0)"),
    ("rebate_12l", 1_275_010, 30, "new TI 12,00,010: marginal relief, tax = the Rs 10 excess"),
    ("rebate_12l", 1_285_000, 30, "new TI 12,10,000: inside the marginal-relief zone"),
    ("rebate_12l", 1_345_580, 30, "new TI 12,70,580: last point where marginal relief binds"),
    ("rebate_12l", 1_345_590, 30, "new TI 12,70,590: first point after the relief zone"),
    ("new_slab_boundary", 1_675_010, 30, "new TI 16,00,010: 15% -> 20% slab"),
    ("new_slab_boundary", 2_075_010, 30, "new TI 20,00,010: 20% -> 25% slab"),
    ("new_slab_boundary", 2_475_010, 30, "new TI 24,00,010: 25% -> 30% slab"),
    ("old_regime", 550_010, 30, "old regime unchanged: TI 5,00,010, the 5L cliff (no relief)"),
    ("surcharge", 5_075_010, 30, "new TI 50,00,010: just above the 50L surcharge threshold"),
    ("realistic", 1_500_000, 32, "realistic profile with HRA, 80C and 80D (old-regime deductions)"),
]


def select():
    cases = [json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()]
    out = []
    for cat, gross, age, why in ROWS:
        match = [c for c in cases if c["category"] == cat and c["input"]["gross_salary"] == gross
                 and c["input"]["age"] == age]
        if not match:
            raise SystemExit("no FY 2025-26 case for %s gross %d age %d" % (cat, gross, age))
        best = max(match, key=lambda c: len(c["input"]))          # the realistic row with deductions
        out.append((best, why))
    return out


def adapt(text):
    return (text.replace("AY 2025-26", "AY 2026-27").replace("FY 2024-25", "FY 2025-26")
            .replace("`make_sheet.py`", "`make_sheet_fy2025_26.py`")
            .replace("official_checks.csv", "official_checks_fy2025_26.csv"))


def load_golden():
    return [json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()]


def build_rows():
    """The sheet's input rows (no file written); also used by import_official to verify a filled sheet."""
    picked = select()
    cities = {"metro": 0, "non": 0}
    rows = []
    for i, (case, why) in enumerate(picked, 1):
        r = ms.sheet_row(i, case, cities)
        r["check_id"] = "OC26-%02d" % i
        rows.append(r)
    return rows, picked


def main(force=False):
    filled = ms.filled_cells(CSV)
    if filled and not force:
        raise SystemExit("refusing to overwrite %s: official_* values filled in %d row(s). Pass --force."
                         % (CSV, len(filled)))
    rows, picked = build_rows()
    with open(CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ms.COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({**r, **{k: "" for k in ms.OFFICIAL_COLUMNS + ["notes"]}})
    ms.write_md(rows, MD)
    md = adapt(MD.read_text(encoding="utf-8"))
    md = md.replace("# Official-calculator checks: inputs to type",
                    "# Official-calculator checks FY 2025-26 (AY 2026-27): inputs to type\n\n"
                    "Income Tax Act 1961 as amended by the Finance Act 2025. The new-regime 87A marginal relief "
                    "just above 12,00,000 is still to be confirmed: rows OC26-03 to OC26-06 test it.\n\n"
                    "Why each row: " + "; ".join("%s %s" % (r["check_id"], why) for r, (_, why) in zip(rows, picked)) + ".")
    MD.write_text(md, encoding="utf-8")
    for r, (case, why) in zip(rows, picked):
        print("%s %-26s %s" % (r["check_id"], case["id"], why))
    return rows


if __name__ == "__main__":
    main(force="--force" in sys.argv[1:])
