# Project A demo UI

A local page around the real pipeline: the parser (`project_a/parser/pipeline.py`) extracts the facts with
the LLM, code decides, and the hand-coded engine computes the tax. Nothing in the UI computes tax.

## Run

From the repo root:

    /opt/anaconda3/bin/python3 -m streamlit run ui/app.py --server.address localhost

Then open http://localhost:8501. It needs Streamlit (installed in that Python; 1.45.1 was used).
`--server.address localhost` keeps the page off your network.

## What the page shows

**Ask tab.** Type a question, choose the LLM mode and press **Run**. The panels follow the order of the CLI
(`python3 -m project_a.cli`):
1. **What the LLM extracted:** the JSON as the user wrote it (value, scale, period, quoted phrase).
2. **What code decided:** the normalised input in rupees, and the decision list (the pipeline's trace).
3. **Status:** compute, ask, unsupported or error.
   - **ask:** each clarifying question becomes an input box. **Submit answers** calls `pipeline.answer` with
     `{question id: typed text}`, as `--interactive` does: no LLM call, up to 5 rounds.
   - **compute:** a table with old and new regime (taxable income, tax), the "Better" line, the
     no-deductions line when the CLI would print it, and the badge "Tax computed by: hand-coded rules (no LLM)".

     Below the table:
     - **Tax breakup**, one expandable panel per regime, built only from the clause trace the hand-coded
       engine returns (the trace the traceability measurement checks). The UI keeps the engine's own result
       through `RecordingEngine`, the pipeline's `engine=` argument. It never calls the engine itself or
       recomputes anything. Each line shows its clause. The trace does not contain everything, so:
       - the three HRA amounts (HRA received, 40%/50% of basic, rent minus 10% of basic) and which one was
         the minimum are shown as "not in trace", because the trace records only the exemption;
       - the amount in each slab and the tax on it are "not in trace", because it records only the total
         slab tax;
       - the C-288A and C-288B roundings have no trace step. They are shown as "derived" lines, the gap
         between two trace steps, and the check requires each gap to be under Rs 10.
     - **Assumptions**, each point shown only when relevant: gross includes HRA; HRA uses the basic given; rent,
       HRA and basic apply for the full year; stated deductions are taken as eligible, with any cap the engine
       applied; no other deductions; and "These are close..." when the two taxes are within Rs 2,000.

**Sidebar.** Five example queries fill the text box:
- three demo queries: the HRA case (it asks, see below), 12 lakh with no deductions, and the CLI's own example;
- NL-14 (Hinglish) and NL-49 (FY 2025-26), read verbatim from the frozen NL set.

For the HRA case, answer "2.4 lakh per year" (HRA) and "Pune" (city), then "6 lakh per year" (basic). Result:
old Rs 98,280, new Rs 71,500.

**Measurements tab.** Shows `RESULTS_TABLES.md` as committed. Nothing is recomputed. To regenerate it, run
`python3 -m project_a.eval.compare` (or `python3 reproduce.py` to check it).

## LLM mode

- **replay** (default): answers from `llm_cache/`, with no network and no key. A query that was never recorded
  shows an error and suggests an example or live mode.
- **live:** calls Gemini (`gemini-3.5-flash-lite`, the parser's model) with `GEMINI_API_KEY` from `.env`. The
  reply is recorded in `ui/live_cache/` (git-ignored), never in `llm_cache/`, so the recordings that
  `reproduce.py` replays are never overwritten. This means a query run live in the UI can't be replayed later,
  and its calls are logged in `ui/live_cache/usage.jsonl`, not in `llm_cache/usage.jsonl`. If a live call fails
  (no key, network, quota), the page shows the error and suggests replay.

The three demo queries replay from four `llm_cache/` entries recorded on 2026-11-21 (07:06–07:33 UTC). They
exist in this working copy, but they are not committed yet. Until they are, a fresh clone can replay only the
two NL-set examples.

## Check

    /opt/anaconda3/bin/python3 ui/check_demo.py

This runs in replay with no network. It puts the three demo queries through `ui/steps.py`, through the
Streamlit app itself (headless, via `streamlit.testing`) and through the real CLI with the same answers, and
checks that the taxes agree:
- HRA case after answers: 98,280 / 71,500;
- 12 lakh: 163,800 / 71,500;
- CLI example: 60,840 / 5,200.

For each demo query, both regimes' breakup lines must add up exactly to the engine's final tax, and the app's
breakup panels must show the CLI's final numbers. The same sum check runs, without an LLM, on all 880 regime
results of the golden and official cases.

The Hinglish 18.5 lakh query (old 2,30,570 / new 2,31,400) is not in the NL set. It was recorded once by the CLI
(one `llm_cache/` entry, not committed yet). Run `ui/check_demo.py --hinglish-185 "<exact text>"`; it asks for the
age, and the check answers 30. Without the flag, the check prints SKIP for it.

It also checks:
- the two NL examples;
- the error shown for a query that was never recorded, and for a failed live call (simulated with a dummy key
  and a closed local port, so nothing is sent);
- that `llm_cache/usage.jsonl` is untouched;
- the sidebar buttons, the Measurements tab and the default mode.

## Files

- `app.py`: the Streamlit page.
- `steps.py`: a wrapper that returns the CLI's steps as data. It uses only `pipeline.parse`, `pipeline.answer`
  and `cli.assumption_note`.
- `check_demo.py`: the check above.
- `.gitignore`: ignores `live_cache/`.
