"""Runs the demo queries through the UI code path (ui/steps.py and the Streamlit app itself, headless via
streamlit.testing) and through the real CLI, in replay mode, and checks that the numbers agree.

    /opt/anaconda3/bin/python3 ui/check_demo.py

No network: replay only. Exit code 0 if every check passes.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(ROOT), str(HERE)]
os.environ["LLM_MODE"] = "replay"               # the CLI subprocess and the app both replay

import steps as ui_steps  # noqa: E402

HRA, TWELVE, CLI_EXAMPLE = (t for _, t in ui_steps.DEMO_QUERIES)
# Answers typed when asked, by question id: the HRA case asks in two rounds; the Hinglish 18.5 lakh query
# states no age, so it is asked and answered 30.
HRA_ANSWERS = {"amount_missing:hra_received": "2.4 lakh per year", "city_missing:": "Pune",
               "amount_missing:basic_salary": "6 lakh per year", "age_missing:": "30"}
CASES = [("HRA case, full details after answers", HRA, (98_280, 71_500)),
         ("12 lakh, no deductions", TWELVE, (163_800, 71_500)),
         ("CLI example (65k per month)", CLI_EXAMPLE, (60_840, 5_200))]
# The Hinglish 18.5 lakh query is not in the NL set or the replay cache; pass its exact text to include it:
#   ui/check_demo.py --hinglish-185 "<query text>"   (it must have been recorded, e.g. by a live run)
if "--hinglish-185" in sys.argv:
    CASES.append(("Hinglish 18.5 lakh", sys.argv[sys.argv.index("--hinglish-185") + 1], (230_570, 231_400)))
failures, skipped = [], []


def check(name, ok, detail=""):
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, (": " + detail) if detail else ""))
    if not ok:
        failures.append(name)


def via_steps(text):
    """The UI wrapper, answering every round the way the UI form does. Returns (steps, answers typed in order)."""
    pr, s = ui_steps.run_query(text, "replay")
    typed, rounds = [], 0
    while s["status"] == "ask" and rounds < ui_steps.MAX_ROUNDS:
        rounds += 1
        answers = {q["id"]: HRA_ANSWERS.get(q["id"], "") for q in s["questions"]}
        typed += list(answers.values())
        pr, s = ui_steps.submit_answers(pr, answers)
    return s, typed


def via_cli(text, typed):
    p = subprocess.run([sys.executable, "-m", "project_a.cli", text, "--interactive"], cwd=ROOT, text=True,
                       input="\n".join(typed) + "\n", capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    old = re.findall(r"Old regime: .*tax Rs ([\d,]+)", p.stdout)
    new = re.findall(r"New regime: .*tax Rs ([\d,]+)", p.stdout)
    note = "Assumed no deductions" in p.stdout
    return (int(old[-1].replace(",", "")), int(new[-1].replace(",", ""))) if old and new else None, note, p


def via_app(text, typed_by_id):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(HERE / "app.py"), default_timeout=60).run()
    at.text_area(key="query").set_value(text)
    at.button[[b.label for b in at.button].index("Run")].click().run()
    for _ in range(ui_steps.MAX_ROUNDS):
        inputs = [t for t in at.text_input if t.key and t.key.startswith("a_")]
        if not inputs:
            break
        for t in inputs:
            t.set_value(typed_by_id.get(t.key.split("_", 2)[2], ""))
        at.button[[b.label for b in at.button].index("Submit answers")].click().run()
    assert not at.exception, at.exception
    table = at.table[0].value if len(at.table) else None
    badge = [x.value for x in at.success]
    warn = [x.value for x in at.warning]
    if table is None:
        return None, badge, warn, ([], (), [])
    taxes = [int(str(v).replace(",", "")) for v in table["Tax (Rs)"]]
    labels = [x.label for x in at.expander]
    finals = [int(str(t.value["Amount"].iloc[-1]).replace("Rs ", "").replace(",", "")) for t in at.table[1:3]]
    boxes = [m.value for m in at.markdown if m.value.startswith("- ")]
    return tuple(taxes), badge, warn, (labels, tuple(finals), boxes)


for name, text, want in CASES:
    s, typed = via_steps(text)
    got = (s["result"]["old_tax"], s["result"]["new_tax"]) if s["result"] else None
    check("%s: UI wrapper gives %s" % (name, want), got == want, "got %s, status %s" % (got, s["status"]))
    cli, cli_note, p = via_cli(text, typed)
    check("%s: CLI gives the same" % name, cli == got, "CLI %s (exit %d)" % (cli, p.returncode))
    check("%s: assumption line shown iff the CLI prints it" % name, bool(s["result"] and s["result"]["assumption"]) == cli_note,
          "UI %s, CLI %s" % (bool(s["result"] and s["result"]["assumption"]), cli_note))
    for reg in ("old", "new"):
        b = s["result"]["breakup"][reg] if s["result"] and s["result"]["breakup"] else None
        sums = ui_steps.breakup_sums(b) if b else {}
        check("%s: %s-regime breakup adds up to the engine's final tax %s" % (name, reg, b and b["final_tax"]),
              bool(sums) and all(sums.values()) and b["final_tax"] == s["result"]["%s_tax" % reg], str(sums))
    app_taxes, badge, warn, (exp_labels, app_finals, boxes) = via_app(text, HRA_ANSWERS)
    check("%s: app shows two breakup expanders whose final tax equals the CLI's" % name,
          len(exp_labels) == 2 and app_finals == cli, "%s %s" % (exp_labels, app_finals))
    check("%s: app shows the assumptions box" % name, bool(boxes) and all(a in boxes[-1] for a in s["result"]["assumptions"]))
    check("%s: Streamlit app table shows the same taxes" % name, app_taxes == want, "app %s" % (app_taxes,))
    check("%s: badge shown" % name, ui_steps.ENGINE_BADGE in badge, str(badge))
    check("%s: app shows the assumption line iff the CLI does" % name,
          any("Assumed no deductions" in w for w in warn) == cli_note, str(warn))

# The two NL-set examples replay too (FY 2025-26 computes on its own rules).
for label, text in ui_steps.examples()[3:]:
    _, s = ui_steps.run_query(text, "replay")
    check("%s replays: status %s" % (label, s["status"]), s["status"] == "compute",
          str(s["result"] and (s["result"]["fy"], s["result"]["old_tax"], s["result"]["new_tax"])))

# Errors are shown, not raised: a query that was never recorded, in replay mode.
_, s = ui_steps.run_query("never recorded query %s" % os.getpid(), "replay")
check("unrecorded query in replay -> error with a hint", s["status"] == "error" and "live" in s["hint"], s["error"][:80])

# A failing live call: a dummy key (so .env is not read) and a closed local port (nothing leaves the machine).
saved = {k: os.environ.get(k) for k in ("GEMINI_API_KEY", "GEMINI_API_BASE", "LLM_MAX_RETRIES")}
os.environ.update(GEMINI_API_KEY="dummy-not-a-key", GEMINI_API_BASE="http://127.0.0.1:9", LLM_MAX_RETRIES="0")
usage = ROOT / "llm_cache" / "usage.jsonl"
before, had_live_cache = usage.read_bytes(), ui_steps.LIVE_CACHE.exists()
_, s = ui_steps.run_query(CLI_EXAMPLE, "live")
check("failed live call -> error that suggests replay", s["status"] == "error" and "replay" in s["hint"].lower(),
      s["error"][:90])
check("live mode leaves llm_cache/usage.jsonl untouched (logs to ui/live_cache)", usage.read_bytes() == before)
if not had_live_cache:                          # remove what this check created
    import shutil
    shutil.rmtree(ui_steps.LIVE_CACHE)
for k, v in saved.items():
    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

# Sidebar buttons fill the text box; the Measurements tab shows RESULTS_TABLES.md as committed.
from streamlit.testing.v1 import AppTest  # noqa: E402
at = AppTest.from_file(str(HERE / "app.py"), default_timeout=60).run()
labels = [b.label for b in at.sidebar.button]
check("sidebar has 5 example buttons", len(labels) == 5, "; ".join(labels))
at.sidebar.button[3].click().run()
check("an example button fills the text box", at.text_area(key="query").value == ui_steps.examples()[3][1])
md = "\n".join(m.value for m in at.tabs[1].markdown)
check("Measurements tab shows RESULTS_TABLES.md verbatim",
      (ROOT / "RESULTS_TABLES.md").read_text(encoding="utf-8").strip() in md)
check("default mode is replay", at.radio(key="mode").value == "replay")

# No LLM: the breakup of every golden and official case (both years, both regimes) adds up.
import glob  # noqa: E402
from project_a.core.schema import TaxInput  # noqa: E402
from project_a.engines.hand_coded import HandCodedEngine  # noqa: E402
n, bad = 0, []
for f in sorted(glob.glob(str(ROOT / "project_a" / "tests" / "cases" / "*.jsonl"))):
    for line in Path(f).read_text(encoding="utf-8").splitlines():
        case = json.loads(line) if line.strip() else {}
        if "input" not in case:
            continue
        r = HandCodedEngine().calculate(TaxInput.from_dict(case["input"]))
        for reg in (r.old, r.new):
            n += 1
            if not all(ui_steps.breakup_sums(ui_steps.breakup(reg, case["input"])).values()):
                bad.append("%s %s" % (case.get("id"), reg.regime))
check("breakup adds up on all %d regime results of the golden and official cases" % n, not bad, ", ".join(bad[:5]))

if "--hinglish-185" not in sys.argv:
    skipped.append("Hinglish 18.5 lakh query (text not given; it is not in the NL set or the replay cache)")
for x in skipped:
    print("SKIP " + x)
print("\nUI CHECK: %s%s" % ("PASS" if not failures else "FAIL (%d)" % len(failures),
                           " (%d skipped)" % len(skipped) if skipped else ""))
sys.exit(1 if failures else 0)
