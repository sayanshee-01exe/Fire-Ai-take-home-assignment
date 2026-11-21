"""Does the test suite actually catch bugs?  Run:  python -m project_a.tests.mutation_check

Copies the repository to a temp folder, injects ONE bug at a time (the failures from PLAN.md
section 9) and runs the tests. A mutant that "survives" is either a gap in the tests or an
equivalent mutant (a change that cannot alter any output). Survivors are listed with a reason.
Not collected by pytest (the file name does not start with test_).
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = "project_a/engines/hand_coded.py"

# name -> (file, old text, new text, equivalent_reason or None)
MUTANTS = {
    "M1  drop new-regime 87A marginal relief (ENG-1)":
        (ENGINE, '    if rule["marginal_relief"]:', "    if False:", None),
    "M2  surcharge '>=' instead of '>' (ENG-3)":
        (ENGINE, "if income > th]", "if income >= th]",
         "equivalent: with marginal relief, tax+surcharge at exactly the threshold is capped to the same value"),
    "M3  drop surcharge marginal relief (ENG-4)":
        (ENGINE, "    if uncapped_total > cap:", "    if False:", None),
    "M4  HRA 40%/50% taken on gross, not basic (ENG-5)":
        (ENGINE, "        basic * pct / 100,", "        gross * pct / 100,", None),
    "M5  every city treated as metro (ENG-5)":
        (ENGINE, 'pct = hra_cfg["metro_pct" if inp.metro else "non_metro_pct"]',
         'pct = hra_cfg["metro_pct"]', None),
    "M6  rounding: 5 no longer rounds up (ENG-2)":
        ("project_a/core/rounding.py", "    if 2 * r >= unit:", "    if 2 * r > unit:", None),
    "M7  87A rebate limit '<' instead of '<='":
        (ENGINE, '    if ti <= rule["limit"]:', '    if ti < rule["limit"]:', None),
    "M8  senior band starts at 61, not 60 (ENG-7)":
        (ENGINE, 'if age >= bands["senior_from"]:', 'if age > bands["senior_from"]:', None),
    "M9  80D parents cap ignores senior parents":
        (ENGINE, 'lim["sec_80d"]["parents_senior" if inp.parents_senior else "parents"]',
         'lim["sec_80d"]["parents"]', None),
    "M10 new-regime standard deduction 50,000 (ENG-6)":
        ("project_a/rules/params_fy2024_25.json", '"new": 75000}', '"new": 50000}', None),
    "M11 cess on tax only, not on tax + surcharge":
        (ENGINE, "cess = (tax + sur) * Fraction", "cess = (tax) * Fraction", None),
    "M12 home-loan interest cap removed":
        (ENGINE, 'home_loan = min(Fraction(inp.home_loan_interest), Fraction(lim["sec_24b"]))',
         "home_loan = Fraction(inp.home_loan_interest)", None),
    "M13 super-senior band starts at 81, not 80":
        (ENGINE, 'if age >= bands["super_senior_from"]:', 'if age > bands["super_senior_from"]:', None),
    "M14 80D self senior cap starts at 61":
        (ENGINE, 'senior_self = inp.age >= p["age_bands"]["senior_from"]',
         'senior_self = inp.age > p["age_bands"]["senior_from"]', None),
    "M15 HRA exemption not floored at zero":
        (ENGINE, "    hra_exempt = max(Fraction(0), min(", "    hra_exempt = (min(", None),
}


def main():
    gaps, equivalents = [], []
    for name, (rel, old, new, equivalent) in MUTANTS.items():
        work = Path(tempfile.mkdtemp(prefix="mutant_"))
        try:
            shutil.copytree(ROOT, work / "repo", ignore=shutil.ignore_patterns(
                "__pycache__", ".pytest_cache", ".git", "_prototype"))
            target = work / "repo" / rel
            text = target.read_text(encoding="utf-8")
            if old not in text:
                print("PATTERN NOT FOUND ", name)
                gaps.append(name)
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
                                 cwd=work / "repo", capture_output=True, text=True)
            caught = run.returncode != 0
            first = next((l for l in run.stdout.splitlines() if l.startswith("FAILED")), "")
            status = "caught  " if caught else ("equivalent" if equivalent else "SURVIVED")
            print("%-10s %s  %s" % (status, name, first[7:80]))
            if not caught:
                (equivalents if equivalent else gaps).append(name)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print("\ncaught %d of %d; equivalent mutants: %d; test gaps: %d" % (
        len(MUTANTS) - len(gaps) - len(equivalents), len(MUTANTS), len(equivalents), len(gaps)))
    for name in gaps:
        print("  GAP:", name)
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
