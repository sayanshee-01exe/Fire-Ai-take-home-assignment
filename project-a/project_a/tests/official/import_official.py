"""Imports the filled official-calculator sheet and compares it with the engine and the oracle.

Run:  python3 -m project_a.tests.official.import_official [filled.csv]               # FY 2024-25
      python3 -m project_a.tests.official.import_official --fy 2025-26 [filled.csv]   # FY 2025-26

FY 2024-25 writes project_a/tests/cases/official_30.jsonl and OFFICIAL_CHECK_REPORT.md; FY 2025-26 writes
official_26.jsonl and OFFICIAL_CHECK_REPORT_FY2025_26.md (both reports at the repository root). Each row
is compared with the engine and the oracle for the row's own year. Only reports: it never changes the
engine, oracle, params or golden cases.
"""
import csv
import json
import re
import sys
from pathlib import Path

from project_a.core.schema import TaxInput
from project_a.engines.hand_coded import HandCodedEngine
from project_a.tests.oracle import oracle
from project_a.tests.official import make_sheet

HERE = Path(__file__).resolve().parent
DEFAULT_CSV = HERE / "official_checks.csv"
DEFAULT_JSONL = HERE.parent / "cases" / "official_30.jsonl"
DEFAULT_REPORT = HERE.parents[2] / "OFFICIAL_CHECK_REPORT.md"
FIELDS = ("old_total_income", "old_tax", "new_total_income", "new_tax")
ENGINE = HandCodedEngine()


def _sheet_fy2025_26():
    from project_a.tests.official import make_sheet_fy2025_26 as m26
    return {"csv": m26.CSV, "jsonl": HERE.parent / "cases" / "official_26.jsonl",
            "report": HERE.parents[2] / "OFFICIAL_CHECK_REPORT_FY2025_26.md",
            "title": "# Official-calculator check report: FY 2025-26 (AY 2026-27)",
            "golden": m26.load_golden, "rows": lambda: m26.build_rows()[0]}


SHEETS = {  # per year: the sheet, where results go, and how to rebuild the reference input rows
    "2024-25": lambda: {"csv": DEFAULT_CSV, "jsonl": DEFAULT_JSONL, "report": DEFAULT_REPORT,
                        "title": "# Official-calculator check report",
                        "golden": make_sheet.load_golden, "rows": make_sheet.sheet_rows},
    "2025-26": _sheet_fy2025_26,
}


def parse_amount(text):
    """'Rs 12,34,567' / '₹ 1,000' / ' 500 ' -> int; None if not a whole number."""
    s = re.sub(r"[,\s₹]", "", text or "")
    s = re.sub(r"^(?i:rs\.?|inr)", "", s)
    return int(s) if re.fullmatch(r"\d+", s) else None


def read_filled(csv_path, fy="2024-25"):
    """Returns (cases, errors). Every row must be complete; inputs must match the generated sheet."""
    cfg = SHEETS[fy]()
    golden = {c["id"]: c for c in cfg["golden"]()}
    sheet = {r["check_id"]: r for r in cfg["rows"]()}
    cases, errors = [], []
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for n, row in enumerate(rows, start=2):                # line 1 is the header
        label = "line %d (%s)" % (n, row.get("check_id") or "no check_id")
        ref = sheet.get(row.get("check_id"))
        if ref is None or ref["case_id"] != row.get("case_id"):
            errors.append("%s: check_id/case_id not in the generated sheet" % label)
            continue
        changed = [k for k in make_sheet.INPUT_COLUMNS if str(ref[k]) != (row.get(k) or "").strip()]
        if changed:
            errors.append("%s: input columns differ from the generated sheet: %s"
                          % (label, ", ".join(changed)))
            continue
        expected, bad = {}, []
        for field in FIELDS:
            v = parse_amount(row.get("official_" + field))
            if v is None:
                bad.append("official_%s=%r" % (field, row.get("official_" + field)))
            expected[field] = v
        if bad:
            errors.append("%s: missing or not a whole number: %s" % (label, ", ".join(bad)))
            continue
        expected["best"] = "new" if expected["new_tax"] <= expected["old_tax"] else "old"
        g = golden[row["case_id"]]
        cases.append({
            "id": g["id"], "check_id": row["check_id"], "category": g["category"],
            "source": "official", "input": g["input"], "city_hint": row.get("city_hint", ""),
            "expected": expected, "notes": (row.get("notes") or "").strip(),
        })
    if not errors and len(cases) != len(sheet):
        errors.append("expected %d rows, found %d" % (len(sheet), len(cases)))
    return cases, errors


def engine_view(inp):
    r = ENGINE.calculate(TaxInput.from_dict(inp))
    return {"old_total_income": r.old.total_income, "old_tax": r.old.tax,
            "new_total_income": r.new.total_income, "new_tax": r.new.tax}


def compare(cases):
    """One mismatch per (case, field) where the engine or the oracle differs from the calculator."""
    out = []
    for c in cases:
        eng, ora = engine_view(c["input"]), oracle(c["input"])
        for field in FIELDS:
            off = c["expected"][field]
            if eng[field] != off or ora[field] != off:
                out.append({"check_id": c["check_id"], "case_id": c["id"], "field": field,
                            "official": off, "engine": eng[field], "oracle": ora[field]})
    return out


def write_report(cases, mismatches, path, title="# Official-calculator check report"):
    lines = [title, "",
             "Generated by `python3 -m project_a.tests.official.import_official`. Report only: nothing was changed.",
             "Calculator details: see `project_a/tests/official/CALCULATOR_NOTES.md`.", "",
             "- Cases compared: %d" % len(cases),
             "- Fields compared: %d (old/new total income, old/new tax)" % (4 * len(cases)),
             "- Mismatching fields: %d (engine %d, oracle %d)" % (
                 len(mismatches),
                 sum(m["engine"] != m["official"] for m in mismatches),
                 sum(m["oracle"] != m["official"] for m in mismatches)), ""]
    if mismatches:
        lines += ["| Check | Case | Field | Official | Engine | Engine - official | Oracle | Oracle - official |",
                  "|---|---|---|---:|---:|---:|---:|---:|"]
        for m in mismatches:
            lines.append("| %s | %s | %s | %d | %d | %+d | %d | %+d |" % (
                m["check_id"], m["case_id"], m["field"], m["official"],
                m["engine"], m["engine"] - m["official"], m["oracle"], m["oracle"] - m["official"]))
    else:
        lines.append("No mismatches: the engine and the oracle agree with the calculator on every field.")
    notes = [c for c in cases if c["notes"]]
    if notes:
        lines += ["", "## Notes recorded in the sheet", ""]
        lines += ["- %s (%s): %s" % (c["check_id"], c["id"], c["notes"]) for c in notes]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(csv_path=None, jsonl_path=None, report_path=None, fy="2024-25"):
    """Returns the mismatch list. Raises ValueError (naming every bad row) and writes nothing."""
    cfg = SHEETS[fy]()
    csv_path, jsonl_path = csv_path or cfg["csv"], jsonl_path or cfg["jsonl"]
    report_path = report_path or cfg["report"]
    cases, errors = read_filled(csv_path, fy)
    if errors:
        raise ValueError("rejected %s:\n  %s" % (csv_path, "\n  ".join(errors)))
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, sort_keys=True) + "\n")
    mismatches = compare(cases)
    write_report(cases, mismatches, Path(report_path), cfg["title"])
    return mismatches


def main(argv):
    args = list(argv[1:])
    fy = "2024-25"
    if "--fy" in args:
        i = args.index("--fy")
        fy = args[i + 1]
        del args[i:i + 2]
    if fy not in SHEETS:
        print("unknown --fy %s (known: %s)" % (fy, ", ".join(SHEETS)))
        return 2
    cfg = SHEETS[fy]()
    csv_path = Path(args[0]) if args else cfg["csv"]
    try:
        mismatches = run(csv_path, fy=fy)
    except ValueError as e:
        print(e)
        return 1
    print("wrote %s and %s: %d mismatching field(s)" % (cfg["jsonl"], cfg["report"], len(mismatches)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
