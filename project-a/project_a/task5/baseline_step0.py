"""Task 5, step 0: silent-failure baseline. No LLM calls.

    python3 -m project_a.task5.baseline_step0            # record (run BEFORE any FY 2025-26 change)
    python3 -m project_a.task5.baseline_step0 --classify # later: compare with the FY 2025-26 oracle

Runs the UNCHANGED hand-coded engine and the UNCHANGED FY 2024-25 compiled artifacts on the FY 2025-26
case inputs with fy="2025-26". Each outcome is either LOUD (an error was raised or returned) or an answer;
an answer that differs from the FY 2025-26 oracle is a SILENT failure. Three layers are recorded:
  hand_coded                     the hand-coded engine
  compiled_engine:<artifact>     LLMCompiledEngine (strict allow-list, with its FY guard)
  compiled_code:<artifact>       the artifact's code run directly in the sandbox, without the engine's FY
                                 guard (what the generated code itself does with a 2025-26 input); raw_v1
                                 and raw_v3 only run with the diagnostic helper relaxation, so both the
                                 strict and the diagnostic outcome are recorded
"""
import json
import sys
from dataclasses import asdict
from pathlib import Path

from project_a.compiler.sandbox import Sandbox
from project_a.core.schema import TaxInput
from project_a.engines.hand_coded import HandCodedEngine
from project_a.engines.llm_compiled import COMPILED, LLMCompiledEngine
from project_a.tests.gen_cases_fy2025_26 import build_cases

HERE = Path(__file__).resolve().parent
OUT = HERE / "baseline_step0.json"
FIELDS = ("old_total_income", "old_tax", "new_total_income", "new_tax")


def _answer(res):
    return {"old_total_income": res.old.total_income, "old_tax": res.old.tax,
            "new_total_income": res.new.total_income, "new_tax": res.new.tax}


def record():
    cases = build_cases()
    layers = {}

    def run_layer(name, fn):
        rows = []
        for c in cases:
            try:
                out = fn(c["input"])
                rows.append({"id": c["id"], **out})
            except Exception as e:                       # a raised error is a loud failure
                rows.append({"id": c["id"], "outcome": "loud", "error": "%s: %s" % (type(e).__name__, str(e)[:160])})
        layers[name] = rows

    engine = HandCodedEngine()
    run_layer("hand_coded", lambda inp: {"outcome": "answer", "answer": _answer(engine.calculate(TaxInput.from_dict(inp)))})

    for artifact in ("raw_v1", "raw_v3", "reviewed"):
        eng = LLMCompiledEngine(artifact)

        def via_engine(inp, eng=eng):
            res = eng.calculate(TaxInput.from_dict(inp))
            if res.error:
                return {"outcome": "loud", "error": res.error[:160]}
            return {"outcome": "answer", "answer": _answer(res)}
        run_layer("compiled_engine:" + artifact, via_engine)
        eng.close()

        source = (COMPILED / (artifact + ".py")).read_text()
        for mode, helpers in (("strict", False), ("diagnostic", True)):
            if artifact == "reviewed" and helpers:
                continue
            box = Sandbox(source, allow_helpers=helpers)

            def direct(inp, box=box):
                r = box.run(asdict(TaxInput.from_dict(inp)))
                if not r.get("ok"):
                    return {"outcome": "loud", "error": r.get("error", "")[:160]}
                return {"outcome": "answer", "answer": {k: r["output"].get(k) for k in FIELDS}}
            run_layer("compiled_code:%s:%s" % (artifact, mode), direct)
            box.close()

    OUT.write_text(json.dumps({"cases": len(cases), "layers": layers}, indent=1) + "\n")
    for name, rows in layers.items():
        loud = sum(r["outcome"] == "loud" for r in rows)
        print("%-34s loud %3d, answered %3d  e.g. %s" % (name, loud, len(rows) - loud,
                                                         (rows[0].get("error") or rows[0].get("answer"))))


def classify():
    from project_a.tests.oracle import oracle
    data = json.loads(OUT.read_text())
    expected = {c["id"]: oracle(c["input"]) for c in build_cases()}
    summary = {}
    for name, rows in data["layers"].items():
        loud = [r for r in rows if r["outcome"] == "loud"]
        answered = [r for r in rows if r["outcome"] == "answer"]
        silent = [r for r in answered if any(r["answer"][k] != expected[r["id"]][k] for k in FIELDS)]
        summary[name] = {"cases": len(rows), "loud": len(loud), "answered": len(answered),
                         "silent_wrong": len(silent), "answered_right": len(answered) - len(silent),
                         "silent_examples": [{"id": r["id"], "got": r["answer"], "expected": {k: expected[r["id"]][k] for k in FIELDS}}
                                             for r in silent[:3]]}
        print("%-34s loud %3d | answered %3d: right %3d, SILENT wrong %3d" % (
            name, len(loud), len(answered), len(answered) - len(silent), len(silent)))
    data["classification"] = summary
    OUT.write_text(json.dumps(data, indent=1) + "\n")
    return summary


if __name__ == "__main__":
    classify() if "--classify" in sys.argv[1:] else record()
