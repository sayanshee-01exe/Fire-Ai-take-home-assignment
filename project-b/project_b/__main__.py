"""One command reproduces every number Project B reports:

    python -m project_b                       verify snapshot -> profile -> benchmark -> pipeline -> scale -> report
    python -m project_b --download-fresh      download the live dataset instead (checked against dataset_manifest.json)
    python -m project_b --llm-mode replay     LLM answers only from project_b/llm_cache (no key, no API call)
    python -m project_b --quick               every step on a small budget (results/partial, for development)
    python -m project_b --steps report        re-render RESULTS_B.md / DECISION_MATRIX.md from existing results
    python -m project_b --dry-run             print what would run, run nothing
    python -m project_b --results-dir DIR     write every result (and the two reports) to DIR instead, e.g. to
                                              check that a replay reproduces the committed results

LLM mode: 'replay' when no GEMINI_API_KEY is set (a cached answer or "not run", never a call);
'auto' when a key is set (cached answers first, the API only for prompts never answered before).
"""
from __future__ import annotations

import argparse
import os
import time

STEPS = ("download", "profile", "evaluate", "pipeline", "scale", "report")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m project_b", description="Reproduce every Project B result with one command.")
    ap.add_argument("--steps", default=",".join(STEPS), help=f"comma-separated subset of {','.join(STEPS)}")
    ap.add_argument("--quick", action="store_true", help="small budgets; results go to project_b/results/partial")
    ap.add_argument("--llm-mode", choices=("auto", "replay", "live"), help="default: replay without a key, auto with one")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    ap.add_argument("--download-fresh", action="store_true", help="download the live dataset from NYC Open Data; it is used "
                    "only if it matches dataset_manifest.json (it most likely will not: the source is updated daily)")
    ap.add_argument("--results-dir", help="write results and reports here instead of project_b/results and the repository root")
    args = ap.parse_args(argv)
    args.steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = set(args.steps) - set(STEPS)
    if unknown:
        ap.error(f"unknown steps: {sorted(unknown)}")
    return args


def llm_mode(requested: str | None) -> str:
    from project_b.llm import _load_dotenv

    _load_dotenv()
    if requested:
        return requested
    return "auto" if (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")) else "replay"


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    mode = llm_mode(args.llm_mode)
    os.environ["LLM_MODE"] = mode  # read when the Gemini client is created
    from project_b import config, evaluate, llm, pipeline, profile, report, scale
    from project_b.data import download

    llm.set_client(None)
    from pathlib import Path

    results_dir = Path(args.results_dir) if args.results_dir else (config.RESULTS_DIR / "partial" if args.quick else config.RESULTS_DIR)
    elsewhere = results_dir.resolve() != config.RESULTS_DIR.resolve()  # quick runs and checks never overwrite the reports
    quick = ["--quick"] if args.quick else []
    plan = {
        "download": ("verify the NYC 311 2023 snapshot against dataset_manifest.json", lambda: download.main(fresh=args.download_fresh)),
        "profile": ("Task 1 profile of the raw data", lambda: profile.main()),
        "evaluate": ("Tasks 3-4 detector benchmark", lambda: evaluate.main(quick + ["--out-dir", str(results_dir)])),
        "pipeline": ("Task 5 hybrid pipeline", lambda: pipeline.main(quick + ["--out-dir", str(results_dir)])),
        "scale": ("Task 6 scale projection", lambda: scale.main(["--results-dir", str(results_dir)])),
        "report": ("RESULTS_B.md and DECISION_MATRIX.md", lambda: report.main(["--results-dir", str(results_dir)]
                                                                            + (["--out-dir", str(results_dir)] if elsewhere else []))),
    }
    print(f"[project_b] LLM mode: {mode}; results: {results_dir}; steps: {', '.join(args.steps)}")
    for step in args.steps:
        print(f"  - {step}: {plan[step][0]}")
    if args.dry_run:
        return
    t0 = time.perf_counter()
    for step in args.steps:
        started = time.perf_counter()
        print(f"[project_b] {step} ...", flush=True)
        plan[step][1]()
        print(f"[project_b] {step} done in {time.perf_counter() - started:.0f} s", flush=True)
    print(f"[project_b] all done in {time.perf_counter() - t0:.0f} s")


if __name__ == "__main__":
    main()
