# NL query set (50 queries)

Plain-language queries for the parser and the LLM engines. Not yet frozen: review `NL_QUERIES.md` first.

## Files
- `queries.jsonl`: the set, one query per line. This is the source of truth.
- `NL_QUERIES.md`: review copy, regenerated with `python3 -m project_a.tests.nl.render_md`.
- `MANIFEST.json`: sha256 of `queries.jsonl` and `frozen`. `python3 -m project_a.tests.nl.freeze` sets
  `frozen: true`; after that `test_nl_set.py` fails if the file changes.

## Composition
| Behaviour | Count | Meaning |
|---|---:|---|
| compute | 30 | one per official check OC-01..OC-30; `gold_input` is that case's input exactly |
| ask | 10 | a missing or ambiguous fact changes the result; `clarification_topics` says which |
| unsupported | 7 | needs something the schema cannot represent; `unsupported_features` says what |
| law-change | 3 | asks about FY 2025-26 / the new budget (used in Task 5) |

Every compute query states the financial year and the age. The schema has no default year, and age
changes old-regime slabs and 80D caps, so a query without either belongs under "ask".

Languages: `en` (English), `hinglish` (Hindi grammar in Roman script with English terms), `mixed`
(switches between English and Hindi clauses in one query, including Devanagari in NL-28).

## Split rule (deterministic)
Within each behaviour, sort ids by `sha256(id)` (hex) and mark the first k as `dev`, the rest `heldout`,
with k = compute 9, ask 3, unsupported 2, law-change 1 (15 dev, 35 heldout). `test_nl_set.py` recomputes
the rule and checks that dev covers all four behaviours and all three languages.
