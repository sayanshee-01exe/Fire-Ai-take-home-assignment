# Rules vs LLMs - Project B: data-quality checks on NYC 311

For each of 12 kinds of data problem, this project builds a declarative rule, a statistical
detector and an LLM review. It measures detection, false alarms, cost, throughput and explanation
clarity for each, then combines the winners into a hybrid pipeline.

* Results: [RESULTS_B.md](RESULTS_B.md) · decisions: [DECISION_MATRIX.md](DECISION_MATRIX.md) ·
  AI usage: [AI_USAGE.md](AI_USAGE.md) · design spec: [project_b/DESIGN.md](project_b/DESIGN.md)
* Both reports are generated from `project_b/results/`. A section whose experiment has not run
  says "not measured yet".

## Setup

Python 3.11 or newer (pandas 3 requires it; developed and tested on 3.11.9), Windows / macOS / Linux.
`requirements.txt` pins the exact versions every result was produced with.

```
python -m pip install -r requirements.txt
```

The LLM detectors use Gemini. Put a key in a `.env` file at the repository root (git-ignores it):

```
GEMINI_API_KEY=your-key
```

Without a key, every LLM answer comes from the committed cache in `project_b/llm_cache/` and no
API call is made. A prompt with no cached answer is reported as "not run", never guessed.

## Dataset snapshot

Every number comes from **one** download of NYC Open Data dataset `erm2-nwe9`, requests created in
2023. That source is live: 2023 rows are still being updated, so a new download gives slightly
different data. Reproducing the numbers needs that exact file, not the same query.

* [`dataset_manifest.json`](dataset_manifest.json) (committed) describes it: the API endpoint and
  query, the date filter, the download time, the file name, size, row count, schema and SHA-256.
* The file itself, `data/raw/nyc311_2023.parquet` (about 150 MB, 3,224,723 rows), is not in git
  (`/data/` is git-ignored).
* Every run checks the file against the manifest before reading it (`project_b/data/snapshot.py`).
  A missing or different file stops the run with an explanation. It is never replaced silently by a
  new download.

**Getting the exact snapshot.** It is distributed as a release asset of this repository, so it
stays out of the git history and needs no Git LFS. The repository owner publishes it once:

```
gh release create data-snapshot-2023 data/raw/nyc311_2023.parquet dataset_manifest.json --title "NYC 311 2023 snapshot"
```

A reviewer downloads it and runs as usual:

```
gh release download data-snapshot-2023 --pattern nyc311_2023.parquet --dir data/raw
python -m project_b.data.snapshot      # optional: prints "verified" or why not
python -m project_b
```

Git LFS would also work (un-ignore the file, `git lfs track "data/raw/*.parquet"`). But it puts
the file into every clone and counts against the account's LFS quota, so a release asset is the
simpler choice for one fixed file.

**Without the snapshot:**
* `python -m project_b --download-fresh` downloads the live dataset to
  `data/raw/nyc311_2023.fresh.parquet`. It is used only if it matches the manifest, which is
  unlikely after the source has been updated.
* `PROJECT_B_ALLOW_NEW_SNAPSHOT=1` runs on a different file on purpose, with a warning. The
  results will then differ from the reported ones and must not replace them.

## One command

```
python -m project_b
```

This runs, in order:

1. Verifies the 2023 snapshot against `dataset_manifest.json` (see "Dataset snapshot").
2. Profiles the raw data (Task 1).
3. Runs the detector benchmark: injected corruptions, 36 detectors (Tasks 2-4).
4. Evaluates the hybrid pipeline (Task 5).
5. Projects it to 50 M rows/day across 120 customers (Task 6).
6. Writes RESULTS_B.md and DECISION_MATRIX.md.

| option | effect |
|---|---|
| `--llm-mode replay` | LLM answers only from the cache (default without a key) |
| `--llm-mode auto` | cache first, the API only for new prompts (default with a key) |
| `--quick` | small budgets for development; results go to `project_b/results/partial/` |
| `--steps profile,report` | run only some steps |
| `--dry-run` | print what would run |
| `--download-fresh` | download the live dataset instead of verifying the snapshot (used only if it matches the manifest) |
| `--results-dir DIR` | write every result and both reports to DIR (e.g. to check that a replay reproduces the committed results) |

Each step also runs on its own: `python -m project_b.profile`, `python -m project_b.evaluate`,
`python -m project_b.pipeline`, `python -m project_b.scale`, `python -m project_b.report`.

The committed LLM results were re-scored once from the replay cache, with no new request:
`python -m project_b.evaluate --reuse-base-records project_b/results/stage1_before_rescore/eval_records.jsonl`
with `LLM_MODE=replay`. A row Gemini gave no readable verdict for counts as an abstention, not as
a clean row. `--reuse-base-records` keeps the earlier rule and statistical records, timings
included, after checking that the rerun reproduces every other field.

On Windows, set `PYTHONIOENCODING=utf-8` first if the console shows encoding errors. In PowerShell:
`$env:PYTHONIOENCODING="utf-8"`.

## Tests

```
python -m pytest tests -q
```

The tests use two small slices of the real data in `tests/data/` and a fake LLM client. They never
call Gemini and never need the full download.

## Layout

```
project_b/
  config.py            every experiment setting (paths, split, batch size, rates, prices)
  core.py              Detector interface, Alert, DetectionResult
  llm.py               Gemini client: JSON mode, replay cache, rate limit, circuit breaker
  data/                download, load (typed feed), clean (pre-existing issues -> clean pool)
  profile.py           Task 1 profile -> results/profiling_report.md, profile.json
  inject.py            Task 2 corruption injector (12 problems, 30 variants, ground-truth screens)
  detectors/           Task 3: 12 problems x {rule, stat, llm} = 36 detectors
  evaluate.py          Task 4 benchmark harness -> results/summary.json, summary_tables.md
  metrics.py           Wilson intervals, clarity, grounding, cost, throughput
  pipeline.py          Task 5 hybrid pipeline + its evaluation -> results/pipeline_*
  scale.py             Task 6 projection -> results/scale.json, scale_tables.md
  report.py            RESULTS_B.md / DECISION_MATRIX.md from templates/ and results/
tests/                 unit, integration and regression tests
data/                  raw download and derived files (git-ignored, re-created)
```

## Data and reproducibility

* Source: NYC Open Data `erm2-nwe9`, requests created in 2023: 3,224,723 rows. Rows can still be
  updated at the source, so `data/raw/manifest.json` records the snapshot's per-month counts,
  download time and sha256.
* Every random draw is seeded from `config.SEED`.
* Detectors are fitted and tuned on January-August only and evaluated on September-December.
