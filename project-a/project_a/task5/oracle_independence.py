"""Task 5 oracle-independence check. No LLM calls.

    python3 -m project_a.task5.oracle_independence

Feeds the hand-coded engine deliberately wrong FY 2025-26 parameters (in memory; the file is not
touched) and counts FY 2025-26 cases where the engine then disagrees with the oracle. Every mutant must
be caught. If the oracle read the parameter file or shared the engine's code, a wrong parameter would
move both, and the mutant would go unnoticed (0 disagreements). Also checks the oracle's imports.
"""
import ast
import copy
from pathlib import Path

from project_a.core.params import load_params
from project_a.core.schema import TaxInput
from project_a.engines import hand_coded
from project_a.tests.gen_cases_fy2025_26 import build_cases
from project_a.tests.oracle import oracle

FIELDS = ("old_total_income", "old_tax", "new_total_income", "new_tax")


def mutants(p):
    def m(fn):
        q = copy.deepcopy(p)
        fn(q)
        return q
    return {
        "87A limit left at 7L (stale)": m(lambda q: q["rebate_87a"]["new"].update(limit=700000)),
        "87A max left at 25,000": m(lambda q: q["rebate_87a"]["new"].update(max=25000)),
        "87A marginal relief off": m(lambda q: q["rebate_87a"]["new"].update(marginal_relief=False)),
        "new slabs left at FY 2024-25": m(lambda q: q["slabs"].update(new=load_params("2024-25")["slabs"]["new"])),
        "25% band missing (24L->30% from 20L)": m(lambda q: q["slabs"].update(new=q["slabs"]["new"][:5] + [[None, 30]])),
        "new standard deduction 50,000": m(lambda q: q["standard_deduction"].update(new=50000)),
        "cess 3%": m(lambda q: q.update(cess_percent=3)),
    }


def main():
    tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("tests", "oracle.py").read_text())
    imports = sorted({(n.module or "") if isinstance(n, ast.ImportFrom) else a.name
                      for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                      for a in n.names})
    print("oracle imports:", imports, "(no project_a import)" if not any(i.startswith("project_a") for i in imports) else "!!")
    cases = build_cases()
    expected = {c["id"]: oracle(c["input"]) for c in cases}
    real = load_params("2025-26")
    original = hand_coded.load_params
    caught = 0
    try:
        for name, params in mutants(real).items():
            hand_coded.load_params = lambda fy, params=params: params if fy == "2025-26" else original(fy)
            engine = hand_coded.HandCodedEngine()
            diff = 0
            for c in cases:
                r = engine.calculate(TaxInput.from_dict(c["input"]))
                got = (r.old.total_income, r.old.tax, r.new.total_income, r.new.tax)
                diff += got != tuple(expected[c["id"]][k] for k in FIELDS)
            caught += diff > 0
            print("%-40s engine disagrees with the oracle on %3d of %d cases -> %s"
                  % (name, diff, len(cases), "caught" if diff else "NOT CAUGHT"))
    finally:
        hand_coded.load_params = original
    print("mutants caught: %d of %d" % (caught, len(mutants(real))))


if __name__ == "__main__":
    main()
