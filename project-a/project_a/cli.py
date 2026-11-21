"""Plain-language tax query from the command line.

    python3 -m project_a.cli "Main 30 ka hoon, FY 2024-25 mein 65k per month salary" [--interactive]

Prints what the LLM extracted, asks the clarifying questions (with --interactive), then prints the
old- and new-regime tax from the hand-coded engine. LLM calls follow LLM_MODE (default auto).
"""
import argparse
import json
from dataclasses import asdict

from project_a.parser.pipeline import answer, parse
from project_a.tests.official.make_sheet import indian

# Inputs that lower old-regime tax; if the user stated none, the regime comparison rests on an assumption.
OLD_REGIME_RELIEFS = ("sec_80c", "sec_80ccd1b", "sec_80d_self", "sec_80d_parents", "home_loan_interest",
                      "hra_received", "rent_paid")
NO_DEDUCTIONS_NOTE = ("Assumed no deductions other than the standard deduction; the old regime may be better if "
                      "you have 80C, HRA, 80D or home-loan interest.")


def assumption_note(normalised_input):
    """The comparison note when the user stated no deduction, HRA or rent; None otherwise."""
    return None if any(k in normalised_input for k in OLD_REGIME_RELIEFS) else NO_DEDUCTIONS_NOTE


def show(pr):
    if pr.extraction is not None:
        print("Extracted (as the user wrote it):")
        print(json.dumps(asdict(pr.extraction), indent=1, ensure_ascii=False))
    print("Code decisions:")
    for line in pr.trace:
        print("  -", line)
    print("Status:", pr.status)
    if pr.status == "compute":
        r = pr.result
        print("\nFY %s, input: %s" % (pr.normalised_input["fy"], pr.normalised_input))
        print("  Old regime: taxable income Rs %s, tax Rs %s" % (indian(r["old_total_income"]), indian(r["old_tax"])))
        print("  New regime: taxable income Rs %s, tax Rs %s" % (indian(r["new_total_income"]), indian(r["new_tax"])))
        print("  Better: %s regime (saves Rs %s). %s" % (r["best"], indian(r["saving"]), r["reason"]))
        note = assumption_note(pr.normalised_input)
        if note:
            print("  " + note)
    elif pr.status == "ask":
        for q in pr.questions:
            print("  ? %s  [%s]" % (q["question"], q["id"]))
    elif pr.status == "unsupported":
        print("  Not supported: %s. No tax computed." % ", ".join(pr.unsupported_features))
    elif pr.status == "parse_error":
        print("  The model's reply could not be parsed. Raw text:\n", pr.raw_output)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("query")
    ap.add_argument("--interactive", action="store_true", help="ask the clarifying questions")
    ap.add_argument("--prompt", help="prompt version, e.g. v1 (default: the latest)")
    args = ap.parse_args(argv)
    from project_a.llm.client import GeminiClient, QuotaExhausted
    client = GeminiClient(role="parser")
    try:
        pr = parse(args.query, client=client, version=args.prompt)
    except QuotaExhausted as e:
        print("The model's daily quota is exhausted, so the query was not parsed.\n ", str(e)[:400])
        print(client.calls_today_line())
        raise SystemExit(3)
    show(pr)
    rounds = 0
    while args.interactive and pr.status == "ask" and rounds < 5:
        rounds += 1
        print()
        answers = {q["id"]: input(q["question"] + " > ") for q in pr.questions}
        pr = answer(pr, answers)
        show(pr)
    print(client.calls_today_line())
    return pr


if __name__ == "__main__":
    main()
