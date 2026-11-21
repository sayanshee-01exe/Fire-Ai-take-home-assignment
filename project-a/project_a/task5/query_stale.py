"""Task 5 (b1): the query-time engine with a FORGOTTEN rules update. Live LLM calls (cached).

    python3 -m project_a.task5.query_stale

1. The unchanged engine on a FY 2025-26 input: its rules table has no FY 2025-26 entry, so it raises
   UnknownRuleVersion before any LLM call (a loud failure).
2. The forgotten-update simulation: FY 2025-26 is mapped, in memory only, to RULES_FY2024_25.md (the
   engine file is not edited). The 44 FY 2025-26 dev cases are run through the normal harness.
"""
from project_a.core.errors import UnknownRuleVersion
from project_a.core.schema import TaxInput
from project_a.engines import llm_query
from project_a.eval import run as eval_run
from project_a.task5.analyse_query import report


def main():
    probe = TaxInput.from_dict({"fy": "2025-26", "age": 30, "gross_salary": 1_300_000})
    if "2025-26" not in llm_query.RULES_FILES:
        try:
            llm_query.LLMQueryEngine(client=object()).calculate(probe)
            print("unchanged engine: answered (unexpected)")
        except UnknownRuleVersion as e:
            print("unchanged engine on FY 2025-26: LOUD failure, no LLM call:", e)
    else:
        print("note: the engine already maps FY 2025-26 to", llm_query.RULES_FILES["2025-26"])
    saved = dict(llm_query.RULES_FILES)
    llm_query.RULES_FILES["2025-26"] = "RULES_FY2024_25.md"            # the forgotten update
    try:
        s = eval_run.run("llm_query", "dev", suite="fy2025_26")
    finally:
        llm_query.RULES_FILES.clear()
        llm_query.RULES_FILES.update(saved)
    eval_run.print_summary(s)
    text, _, _ = report(s["results_file"], by_category=True, examples=5)
    print(text)
    if s.get("incomplete"):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
