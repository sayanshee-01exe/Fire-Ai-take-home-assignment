"""Renders queries.jsonl as NL_QUERIES.md for human review (one vertical block per query).

Run:  python3 -m project_a.tests.nl.render_md
"""
import json
from pathlib import Path

from project_a.tests.official.make_sheet import indian

HERE = Path(__file__).resolve().parent
LANG = {"en": "English", "hinglish": "Hinglish", "mixed": "Mixed"}
LABELS = [  # schema field -> plain words
    ("fy", "Financial year"), ("age", "Age"), ("gross_salary", "Gross salary (annual)"),
    ("basic_salary", "Basic salary (annual)"), ("hra_received", "HRA received (annual)"),
    ("rent_paid", "Rent paid (annual)"), ("metro", "Metro city"),
    ("sec_80c", "80C"), ("sec_80ccd1b", "80CCD(1B) own NPS"), ("sec_80d_self", "80D self and family"),
    ("sec_80d_parents", "80D parents"), ("parents_senior", "A parent is 60 or older"),
    ("home_loan_interest", "Home-loan interest (self-occupied)"),
]


def _value(field, v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if field in ("fy", "age"):
        return str(v)
    return "₹" + indian(v)


def render(rows):
    out = ["# NL query set: review copy", "",
           "Generated from `queries.jsonl` by `render_md.py`; edit the JSONL, not this file.", ""]
    for r in rows:
        out += ["---", "", "### %s" % r["id"], "",
                "- **Language:** %s" % LANG[r["language"]],
                "- **Split:** %s" % r["split"], "",
                "> %s" % r["text"], "",
                "- **Expected behaviour:** %s%s" % (
                    r["expected_behavior"], " (source %s)" % r["source_case"] if r["source_case"] else "")]
        if r["gold_input"]:
            out.append("- **Gold input:**")
            out += ["    - %s: %s" % (label, _value(f, r["gold_input"][f]))
                    for f, label in LABELS if f in r["gold_input"]]
        if r["clarification_topics"]:
            out.append("- **Should ask about:**")
            out += ["    - %s" % t for t in r["clarification_topics"]]
        if r["unsupported_features"]:
            out.append("- **Unsupported:**")
            out += ["    - %s" % t for t in r["unsupported_features"]]
        out += ["- **Tests:** %s" % r["notes"], ""]
    return "\n".join(out)


def main():
    rows = [json.loads(l) for l in (HERE / "queries.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    (HERE / "NL_QUERIES.md").write_text(render(rows), encoding="utf-8")


if __name__ == "__main__":
    main()
