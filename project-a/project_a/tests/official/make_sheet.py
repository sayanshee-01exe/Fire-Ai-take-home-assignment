"""Picks 30 golden cases for checking by hand on the official income-tax calculator.

Run:  python3 -m project_a.tests.official.make_sheet [--force]

Refuses to overwrite official_checks.csv once any official_* cell is filled, unless --force.

Writes official_checks.csv (inputs + empty columns to fill), official_checks.md (the same
inputs in Indian digit grouping) and CALCULATOR_NOTES.md (a template). No randomness, and
no expected value from the engine or oracle is written anywhere.
"""
import csv
import json
import sys
from pathlib import Path

from project_a.core.params import load_params
from project_a.core.schema import TaxInput
from project_a.engines.hand_coded import HandCodedEngine

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parent / "cases" / "golden.jsonl"
ENGINE = HandCodedEngine()

MULTI_FACT = {"realistic": 9, "regime_comparison": 3, "hra": 3, "deduction_caps": 2}
BOUNDARY = {"rebate_87a": 4, "surcharge": 5, "slab_boundary": 2, "age_boundary": 2}
SURCHARGE_THRESHOLDS = (5_000_000, 10_000_000, 20_000_000, 50_000_000)
REBATE_OLD_LIMIT, REBATE_NEW_LIMIT = 500_000, 700_000

INPUT_COLUMNS = ["check_id", "case_id", "age", "gross_salary", "hra_exemption", "sec_80c",
                 "sec_80ccd1b", "sec_80d_self", "sec_80d_parents", "parents_senior",
                 "home_loan_interest", "n_features", "city_hint",
                 "age_category", "adv_net_salary_new", "adv_net_salary_old", "adv_80d_total",
                 "relief_regimes"]
OFFICIAL_COLUMNS = ["official_old_total_income", "official_old_tax",
                    "official_new_total_income", "official_new_tax"]
COLUMNS = INPUT_COLUMNS + OFFICIAL_COLUMNS + ["notes"]

METRO_CITIES = ("Delhi", "Mumbai", "Kolkata", "Chennai")
NON_METRO_CITIES = ("Bengaluru", "Hyderabad", "Pune", "Ahmedabad")


def load_golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def run_engine(case):
    return ENGINE.calculate(TaxInput.from_dict(case["input"]))


def hra_exemption(case):
    """The HRA exemption the engine derives (old regime), exact (may be fractional)."""
    for step in run_engine(case).old.trace:
        if step.step == "HRA exempt":
            return step.amount
    return 0


def has_hra_or_rent(inp):
    return inp.get("hra_received", 0) > 0 or inp.get("rent_paid", 0) > 0


def n_features(case):
    inp = case["input"]
    hra = hra_exemption(case) > 0 or inp.get("rent_paid", 0) > 0
    others = ("sec_80c", "sec_80ccd1b", "sec_80d_self", "sec_80d_parents", "home_loan_interest")
    return int(hra) + sum(1 for k in others if inp.get(k, 0) > 0)


def evenly(items, k):
    """k items spread evenly by position, first and last included."""
    if k >= len(items):
        return list(items)
    if k == 1:
        return [items[len(items) // 2]]
    return [items[round(i * (len(items) - 1) / (k - 1))] for i in range(k)]


def pick_multi_fact(cases, k):
    """Prefer n_features 2 or 3; fill any shortfall evenly from the rest."""
    preferred = [c for c in cases if n_features(c) in (2, 3)]
    if len(preferred) >= k:
        return [(c, "evenly spaced among n_features 2-3") for c in evenly(preferred, k)]
    rest = [c for c in cases if c not in preferred]
    return ([(c, "n_features 2-3") for c in preferred]
            + [(c, "fill: no more n_features 2-3 cases") for c in evenly(rest, k - len(preferred))])


def pick_surcharge(cases, k):
    picked = []
    totals = {c["id"]: run_engine(c) for c in cases}
    for th in SURCHARGE_THRESHOLDS:
        best = None
        for c in cases:
            r = totals[c["id"]]
            for regime, ti in (("old", r.old.total_income), ("new", r.new.total_income)):
                if ti > th and (best is None or ti - th < best[0]):
                    best = (ti - th, c, regime)
        if best and best[1] not in [p[0] for p in picked]:
            picked.append((best[1], "just above %s (%s TI %+d over it)" % (indian(th), best[2], best[0])))
    for c in cases:                                    # one well inside a relief zone
        if len(picked) >= k:
            break
        r = totals[c["id"]]
        for regime in (r.old, r.new):
            th = max([t for t in SURCHARGE_THRESHOLDS if regime.total_income > t], default=None)
            relief = any(s.step == "surcharge marginal relief" for s in regime.trace)
            if (relief and regime.total_income - th >= 1000
                    and c not in [p[0] for p in picked]):
                picked.append((c, "inside marginal-relief zone (%s TI %s)"
                               % (regime.regime, indian(regime.total_income))))
                break
    return picked[:k]


def pick_rebate(cases, k):
    res = {c["id"]: run_engine(c) for c in cases}
    old_at = [c for c in cases if res[c["id"]].old.total_income == REBATE_OLD_LIMIT]
    old_above = sorted((c for c in cases if res[c["id"]].old.total_income > REBATE_OLD_LIMIT
                        and res[c["id"]].old.total_income < REBATE_NEW_LIMIT),
                       key=lambda c: res[c["id"]].old.total_income)
    new = [c for c in cases if res[c["id"]].new.total_income > REBATE_NEW_LIMIT]
    in_zone = [c for c in new
               if any(s.step == "87A marginal relief" for s in res[c["id"]].new.trace)]
    beyond = [c for c in new if c not in in_zone]
    picked = []
    if old_at:
        picked.append((old_at[0], "old regime TI exactly 5,00,000"))
    if old_above:
        picked.append((old_above[0], "old regime TI just above 5,00,000"))
    if in_zone:
        c = in_zone[len(in_zone) // 2]
        picked.append((c, "new regime inside 7L marginal-relief zone (TI %s)"
                       % indian(res[c["id"]].new.total_income)))
    if beyond:
        c = beyond[0]
        picked.append((c, "new regime just beyond the relief zone (TI %s)"
                       % indian(res[c["id"]].new.total_income)))
    return picked[:k]


def select():
    """Returns [(case, reason)] in sheet order."""
    by_cat = {}
    for c in load_golden():
        by_cat.setdefault(c["category"], []).append(c)
    out = []
    for cat, k in MULTI_FACT.items():
        out += pick_multi_fact(by_cat[cat], k)
    out += pick_rebate(by_cat["rebate_87a"], BOUNDARY["rebate_87a"])
    out += pick_surcharge(by_cat["surcharge"], BOUNDARY["surcharge"])
    for cat in ("slab_boundary", "age_boundary"):
        out += [(c, "evenly spaced by position") for c in evenly(by_cat[cat], BOUNDARY[cat])]
    for cat, k in {**MULTI_FACT, **BOUNDARY}.items():
        got = sum(1 for c, _ in out if c["category"] == cat)
        if got != k:
            raise RuntimeError("%s: wanted %d cases, selection gave %d" % (cat, k, got))
    return out


def sheet_row(i, case, cities):
    """The input columns for one case. cities: iterator state {"metro": n, "non": n}."""
    inp = case["input"]
    result = run_engine(case)
    hra = hra_exemption(case)
    if hra != int(hra):                                # the calculator takes whole rupees
        raise ValueError("%s: HRA exemption %s is not whole rupees" % (case["id"], hra))
    city = ""
    if has_hra_or_rent(inp):
        if inp.get("metro"):
            city = METRO_CITIES[cities["metro"] % len(METRO_CITIES)]
            cities["metro"] += 1
        else:
            city = NON_METRO_CITIES[cities["non"] % len(NON_METRO_CITIES)]
            cities["non"] += 1
    return {
        "check_id": "OC-%02d" % i, "case_id": case["id"], "age": inp["age"],
        "gross_salary": inp["gross_salary"], "hra_exemption": int(hra),
        "sec_80c": inp.get("sec_80c", 0), "sec_80ccd1b": inp.get("sec_80ccd1b", 0),
        "sec_80d_self": inp.get("sec_80d_self", 0), "sec_80d_parents": inp.get("sec_80d_parents", 0),
        "parents_senior": "yes" if inp.get("parents_senior") else "no",
        "home_loan_interest": inp.get("home_loan_interest", 0),
        "n_features": n_features(case), "city_hint": city,
        # What the Advanced calculator takes: net salary (HRA already removed), one 80D field.
        "age_category": age_category(inp),
        "adv_net_salary_new": inp["gross_salary"],
        "adv_net_salary_old": inp["gross_salary"] - int(hra),
        "adv_80d_total": inp.get("sec_80d_self", 0) + inp.get("sec_80d_parents", 0),
        "relief_regimes": relief_regimes(result),
    }


def relief_regimes(result):
    """Informational: regimes where the engine applies marginal relief (87A or surcharge)."""
    return "+".join(r.regime for r in (result.old, result.new)
                    if any("marginal relief" in s.step for s in r.trace))


def age_category(inp):
    bands = load_params(inp["fy"])["age_bands"]
    if inp["age"] >= bands["super_senior_from"]:
        return "80_plus"
    return "60_79" if inp["age"] >= bands["senior_from"] else "below_60"


def sheet_rows():
    cities = {"metro": 0, "non": 0}
    return [sheet_row(i, c, cities) for i, (c, _) in enumerate(select(), 1)]


def indian(n):
    """1234567 -> '12,34,567'."""
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


PROCEDURE = """\
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
"""


def write_md(rows, path):
    heads = ["Check", "Case", "Age category", "Net salary (new)", "Net salary (old)", "80C",
             "80CCD(1B)", "80D", "Home-loan int.", "Relief in",
             "Age", "Gross salary", "HRA exempt", "80D self", "80D parents", "Parents 60+",
             "n_feat", "City"]
    text_cols = ("Check", "Case", "Age category", "Relief in", "Parents 60+", "City")
    lines = [
        "# Official-calculator checks: inputs to type",
        "",
        PROCEDURE,
        "FY 2024-25 (AY 2025-26). The columns up to \"Relief in\" are what you type (\"Relief in\" is",
        "informational: the regimes where our engine applies marginal relief). The rest are the original",
        "inputs, for reference. Generated by `make_sheet.py`; do not edit by hand.",
        "Record the calculator's results in `official_checks.csv`.",
        "",
        "| " + " | ".join(heads) + " |",
        "|" + "|".join("---" if h in text_cols else "---:" for h in heads) + "|",
    ]
    for r in rows:
        cells = [r["check_id"], r["case_id"], r["age_category"]]
        cells += [indian(r[k]) for k in ("adv_net_salary_new", "adv_net_salary_old", "sec_80c",
                                          "sec_80ccd1b", "adv_80d_total", "home_loan_interest")]
        cells += [r["relief_regimes"], str(r["age"])]
        cells += [indian(r[k]) for k in ("gross_salary", "hra_exemption", "sec_80d_self",
                                          "sec_80d_parents")]
        cells += [r["parents_senior"], str(r["n_features"]), r["city_hint"]]
        lines.append("| " + " | ".join(cells) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


NOTES_TEMPLATE = """# Calculator notes (fill in while checking)

""" + PROCEDURE + """
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
"""


def filled_cells(csv_path):
    """check_ids of rows in an existing sheet that have any official_* cell filled."""
    if not csv_path.exists():
        return []
    with open(csv_path, newline="", encoding="utf-8") as f:
        return [r.get("check_id", "?") for r in csv.DictReader(f)
                if any((r.get(k) or "").strip() for k in OFFICIAL_COLUMNS)]


def main(out_dir=HERE, force=False):
    out_dir = Path(out_dir)
    filled = filled_cells(out_dir / "official_checks.csv")
    if filled and not force:
        raise SystemExit("refusing to overwrite %s: official_* values already filled in %d row(s) (%s). "
                         "Pass --force to discard them." % (out_dir / "official_checks.csv",
                                                           len(filled), ", ".join(filled[:5])))
    picked = select()
    rows = sheet_rows()
    with open(out_dir / "official_checks.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({**r, **{k: "" for k in OFFICIAL_COLUMNS + ["notes"]}})
    write_md(rows, out_dir / "official_checks.md")
    notes = out_dir / "CALCULATOR_NOTES.md"
    if not notes.exists():                             # never overwrite what the user filled in
        notes.write_text(NOTES_TEMPLATE, encoding="utf-8")

    for r, (case, why) in zip(rows, picked):
        print("%s  %-22s n=%d  %s" % (r["check_id"], case["id"], r["n_features"], why))
    n23 = sum(1 for r in rows if r["n_features"] in (2, 3))
    print("\n%d of %d rows have n_features 2 or 3 (target at least 15)%s"
          % (n23, len(rows), "" if n23 >= 15 else ": TARGET NOT MET under these quotas"))
    print("wrote", out_dir / "official_checks.csv", "and official_checks.md")
    return rows


if __name__ == "__main__":
    main(force="--force" in sys.argv[1:])
