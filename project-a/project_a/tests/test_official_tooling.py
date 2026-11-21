"""The official-calculator tooling: sheet selection, import/compare, and the official cases."""
import csv
import json
from collections import Counter
from pathlib import Path

import pytest

from project_a.tests.official import import_official, make_sheet
from project_a.tests.oracle import oracle

CASES_DIR = Path(__file__).resolve().parent / "cases"
QUOTAS = {**make_sheet.MULTI_FACT, **make_sheet.BOUNDARY}


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def sheet(tmp_path_factory):
    out = tmp_path_factory.mktemp("sheet")
    make_sheet.main(out)
    return out


def test_sheet_has_30_unique_rows_with_category_quotas(sheet):
    rows = _read_csv(sheet / "official_checks.csv")
    assert len(rows) == 30
    assert len({r["case_id"] for r in rows}) == 30
    assert [r["check_id"] for r in rows] == ["OC-%02d" % i for i in range(1, 31)]
    golden = {c["id"]: c["category"] for c in make_sheet.load_golden()}
    assert Counter(golden[r["case_id"]] for r in rows) == QUOTAS
    assert (sheet / "official_checks.md").exists() and (sheet / "CALCULATOR_NOTES.md").exists()


def test_sheet_columns_have_features_and_no_expected_values(sheet):
    rows = _read_csv(sheet / "official_checks.csv")
    assert list(rows[0]) == make_sheet.COLUMNS
    assert all(r["n_features"].isdigit() for r in rows)
    golden = {c["id"]: c for c in make_sheet.load_golden()}
    for r in rows:
        inp = golden[r["case_id"]]["input"]
        has_hra = inp.get("hra_received", 0) > 0 or inp.get("rent_paid", 0) > 0
        cities = make_sheet.METRO_CITIES if inp.get("metro") else make_sheet.NON_METRO_CITIES
        assert (r["city_hint"] in cities) if has_hra else r["city_hint"] == ""
        assert all(r[k] == "" for k in make_sheet.OFFICIAL_COLUMNS + ["notes"])
    assert "enter_new_income" not in rows[0] and "enter_old_income" not in rows[0]
    for r in rows:                                     # what the Advanced calculator takes
        inp = golden[r["case_id"]]["input"]
        age = inp["age"]
        assert r["age_category"] == ("80_plus" if age >= 80 else "60_79" if age >= 60 else "below_60")
        assert int(r["adv_net_salary_new"]) == inp["gross_salary"]
        assert int(r["adv_net_salary_old"]) == inp["gross_salary"] - int(r["hra_exemption"])
        assert int(r["adv_80d_total"]) == int(r["sec_80d_self"]) + int(r["sec_80d_parents"])
        assert r["relief_regimes"] in ("", "old", "new", "old+new")
    relief = {r["case_id"]: r["relief_regimes"] for r in rows}
    assert relief["rebate_87a-13"] == "new" and relief["rebate_87a-17"] == ""   # 7L zone and beyond
    md = (sheet / "official_checks.md").read_text()
    assert md.index("New-regime run") < md.index("Old-regime run") < md.index("| OC-01 |")
    assert "Basic tab" not in md and "Run A" not in md
    assert "Advanced Calculator" in (sheet / "CALCULATOR_NOTES.md").read_text()
    header = " ".join(rows[0]).lower()
    assert "expected" not in header and "engine" not in header and "oracle" not in header


def _fill_from_oracle(sheet, dest, alter=None):
    rows = _read_csv(sheet / "official_checks.csv")
    golden = {c["id"]: c for c in make_sheet.load_golden()}
    for r in rows:
        o = oracle(golden[r["case_id"]]["input"])
        for f in import_official.FIELDS:
            r["official_" + f] = "₹ " + make_sheet.indian(o[f])     # as typed off the screen
    if alter:
        rows[alter[0]]["official_" + alter[1]] = str(
            import_official.parse_amount(rows[alter[0]]["official_" + alter[1]]) + 10)
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=make_sheet.COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return dest


def test_import_of_oracle_values_reports_zero_mismatches(sheet, tmp_path):
    filled = _fill_from_oracle(sheet, tmp_path / "filled.csv")
    jsonl, report = tmp_path / "official_30.jsonl", tmp_path / "report.md"
    assert import_official.run(filled, jsonl, report) == []
    cases = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert len(cases) == 30 and all(c["source"] == "official" for c in cases)
    assert all(set(c["expected"]) == set(import_official.FIELDS) | {"best"} for c in cases)
    assert "No mismatches" in report.read_text()


def test_import_with_one_altered_value_reports_exactly_one(sheet, tmp_path):
    filled = _fill_from_oracle(sheet, tmp_path / "filled.csv", alter=(4, "old_tax"))
    report = tmp_path / "report.md"
    mismatches = import_official.run(filled, tmp_path / "o.jsonl", report)
    assert len(mismatches) == 1
    m = mismatches[0]
    assert (m["check_id"], m["field"]) == ("OC-05", "old_tax")
    assert m["engine"] - m["official"] == -10 and m["oracle"] - m["official"] == -10
    assert "OC-05" in report.read_text()


def test_import_rejects_incomplete_or_non_numeric_rows(sheet, tmp_path):
    rows = _read_csv(_fill_from_oracle(sheet, tmp_path / "filled.csv"))
    rows[2]["official_new_tax"] = ""
    rows[7]["official_old_tax"] = "12,345.50"
    bad = tmp_path / "bad.csv"
    with open(bad, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=make_sheet.COLUMNS)
        w.writeheader()
        w.writerows(rows)
    with pytest.raises(ValueError) as e:
        import_official.run(bad, tmp_path / "o.jsonl", tmp_path / "r.md")
    assert "OC-03" in str(e.value) and "OC-08" in str(e.value)
    assert not (tmp_path / "o.jsonl").exists()


def test_make_sheet_refuses_to_overwrite_a_filled_sheet_unless_forced(sheet, tmp_path):
    filled = _fill_from_oracle(sheet, tmp_path / "official_checks.csv")
    before = filled.read_bytes()
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        make_sheet.main(tmp_path)
    assert filled.read_bytes() == before
    make_sheet.main(tmp_path, force=True)
    assert all(r[k] == "" for r in _read_csv(filled) for k in make_sheet.OFFICIAL_COLUMNS)


def test_procedure_says_record_the_rounded_balance_tax(sheet):
    for name in ("official_checks.md", "CALCULATOR_NOTES.md"):
        text = (sheet / name).read_text()
        assert "Balance Tax Payable / Refundable as `official_new_tax`" in text
        assert "Balance Tax Payable / Refundable as `official_old_tax`" in text
        assert "Total Tax on Income as" not in text


def test_exceptions_file_entries_have_reasons():
    exceptions = json.loads((CASES_DIR / "official_exceptions.json").read_text())
    assert isinstance(exceptions, list)
    assert all(e.get("case_id") and e.get("reason", "").strip() for e in exceptions)


def test_engine_matches_official_cases():
    path = CASES_DIR / "official_30.jsonl"
    if not path.exists():
        pytest.skip("official_30.jsonl not created yet (fill the sheet, run import_official)")
    excepted = {e["case_id"] for e in json.loads((CASES_DIR / "official_exceptions.json").read_text())}
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    failures = []
    for c in cases:
        if c["id"] in excepted:
            continue
        got = import_official.engine_view(c["input"])
        want = {k: c["expected"][k] for k in import_official.FIELDS}
        if got != want:
            failures.append((c["check_id"], c["id"], want, got))
    assert not failures, failures
