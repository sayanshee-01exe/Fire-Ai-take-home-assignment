# Rules vs LLMs: take-home submission

Two projects, each in its own self-contained folder with its own README, results, decision matrix and AI usage note.

| | Project A | Project B |
|---|---|---|
| Problem | Indian salaried income-tax engine (FY 2024-25, then the FY 2025-26 law change) | Data-quality checks on NYC 311 service requests (12 kinds of data problem) |
| What is compared | Hand-coded rules vs an LLM at query time vs LLM-compiled code, plus a plain-language parser | A declarative rule, a statistical detector and an LLM review for each problem, then a hybrid pipeline |
| Folder | [`project-a/`](project-a/) | [`project-b/`](project-b/) |
| Results | [`RESULTS_A.md`](project-a/RESULTS_A.md) | [`RESULTS_B.md`](project-b/RESULTS_B.md) |
| Decision matrix | [`DECISION_MATRIX.md`](project-a/DECISION_MATRIX.md) | [`DECISION_MATRIX.md`](project-b/DECISION_MATRIX.md) |
| AI usage note | [`AI_USAGE.md`](project-a/AI_USAGE.md) | [`AI_USAGE.md`](project-b/AI_USAGE.md) |

## Reproduce

Python 3.10 or newer for Project A, 3.11 or newer for Project B. Every LLM answer is replayed from a committed cache, so **no API key and no network access are needed** to reproduce the reported numbers.

**Project A: one command, about 7 seconds.**

    cd project-a
    python3 -m pip install -r requirements.txt
    python3 reproduce.py

It runs the test suite and 12 measurement steps, and prints `REPRODUCE: PASS` when every table matches the committed one (`project-a/RESULTS_TABLES.md`).

**Project B.** Install and run as described in [`project-b/README.md`](project-b/README.md). It needs the pinned dataset snapshot (`dataset_manifest.json` gives the endpoint, row count and SHA-256); that file is about 150 MB and is not stored in git.

## Project A in brief

The design principle is **the LLM handles language and code handles tax**.

- A hand-coded engine computes the tax with exact fractions and one parameter file per financial year. It is rupee-exact on every development, held-out and official-calculator case in both years, deterministic, and takes about 33 µs a call at no cost.
- Asking an LLM to do the arithmetic at query time was tax-exact on only 16.0% of FY 2024-25 held-out cases, and silently wrong on 143 of 173 FY 2025-26 answers.
- LLM-compiled code needed human-style review before it was safe; none of the three raw outputs shipped as generated.
- The plain-language parser uses the LLM only to report what the user wrote. Code decides whether to compute, ask a clarifying question or refuse as out of scope.
- A Streamlit demo UI (`project-a/ui/`) runs the real pipeline and shows the extraction, the code's decisions and the tax breakup.

Full tables, the Task 5 change log (FY 2025-26: time taken, what broke, silent failures) and the final design for 100,000 users are in [`RESULTS_A.md`](project-a/RESULTS_A.md). Items described as "proposed, not implemented" there were not built.

## Project B in brief

For each of 12 kinds of data problem, the project builds a rule, a statistical detector and an LLM review, and measures detection, false alarms, cost, throughput and explanation clarity. The winners are combined into a hybrid pipeline, with a scale design. Details and numbers are in [`RESULTS_B.md`](project-b/RESULTS_B.md).

## Repository layout

    README.md          this file
    project-a/         income-tax engine, parser, compiler, tests, demo UI
      project_a/       the code
      llm_cache/       recorded LLM calls replayed by reproduce.py
      reproduce.py     one command that re-creates every reported number
      RESULTS_A.md  DECISION_MATRIX.md  AI_USAGE.md  README.md
    project-b/         data-quality detectors, injector, pipeline, tests
      project_b/       the code
      RESULTS_B.md  DECISION_MATRIX.md  AI_USAGE.md  README.md  dataset_manifest.json

## Notes

- Work done alone; AI tools were used and are described honestly in each `AI_USAGE.md`.
- API keys are never committed. To make live calls instead of replaying the cache, put `GEMINI_API_KEY=...` in a git-ignored `.env` inside the project folder (Project B ships a `.env.example`).
