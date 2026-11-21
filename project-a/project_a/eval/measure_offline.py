"""Runs every no-LLM configuration through the harness once, so compare.py can read fresh results.

    python3 -m project_a.eval.measure_offline

Hand-coded engine and compiled artifacts (raw_v1..v3 strict and diagnostic, reviewed strict), FY 2024-25 and
FY 2025-26, splits dev, heldout and official. No LLM calls. Writes the usual files under results/.
"""
from project_a.eval import run as eval_run

SUITES = ("fy2024_25", "fy2025_26")
SPLITS = ("dev", "heldout", "official")


def configs():
    for suite in SUITES:
        for split in SPLITS:
            yield dict(engine_name="hand_coded", suite=suite, split=split)
            for art in ("raw_v1", "raw_v2", "raw_v3"):
                yield dict(engine_name="llm_compiled", suite=suite, split=split, artifact=art, allow_helpers=False)
                yield dict(engine_name="llm_compiled", suite=suite, split=split, artifact=art, allow_helpers=True)
            yield dict(engine_name="llm_compiled", suite=suite, split=split, artifact="reviewed", allow_helpers=False)


def main():
    done = []
    for cfg in configs():
        s = eval_run.run(**cfg)
        done.append(s["results_file"])
        print("%-12s %-10s %-9s %-9s %-5s tax exact %3d/%3d" % (
            cfg["engine_name"], cfg["suite"], cfg["split"], cfg.get("artifact", ""),
            "diag" if cfg.get("allow_helpers") else "", s["tax_exact"], s["runs"]))
    return done


if __name__ == "__main__":
    main()
