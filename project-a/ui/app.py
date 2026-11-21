"""Local demo UI for Project A: the real parser pipeline and the hand-coded engine.

    /opt/anaconda3/bin/python3 -m streamlit run ui/app.py

Default LLM mode is replay (answers from llm_cache/, no network, no key). Live calls Gemini with the key from
.env and records replies in ui/live_cache/ (llm_cache/ is never written). See ui/README.md.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import streamlit as st  # noqa: E402

import steps as ui_steps  # noqa: E402

st.set_page_config(page_title="Project A demo", layout="wide")
ss = st.session_state
ss.setdefault("query", "")
ss.setdefault("pr", None)
ss.setdefault("steps", None)
ss.setdefault("round", 0)


def fill(text):
    ss["query"] = text


with st.sidebar:
    st.header("Example queries")
    st.caption("Click to fill the text box, then press Run.")
    for i, (label, text) in enumerate(ui_steps.examples()):
        st.button(label, key="ex%d" % i, on_click=fill, args=(text,), help=text, use_container_width=True)

st.title("Project A: income-tax question to answer")
ask_tab, meas_tab = st.tabs(["Ask", "Measurements"])

with ask_tab:
    st.text_area("Your tax question", key="query", height=100)
    c1, c2 = st.columns([1, 3])
    mode = c2.radio("LLM mode", ["replay", "live"], index=0, horizontal=True, key="mode",
                    help="replay: recorded LLM replies only, no network. live: calls Gemini (uses the key in .env).")
    if c1.button("Run", type="primary"):
        if not ss["query"].strip():
            st.warning("Type a question first.")
        else:
            with st.spinner("Asking the LLM to extract the facts (%s)..." % mode):
                ss["pr"], ss["steps"] = ui_steps.run_query(ss["query"], mode)
            ss["round"] = 0

    s = ss["steps"]
    if s:
        if s["status"] == "error":
            st.error(s["error"])
            if s["hint"]:
                st.info(s["hint"])
        llm = s.get("llm") or {}
        if llm:
            st.caption("LLM: %s, prompt %s, %s, %.2f s, %d prompt + %d output tokens" % (
                llm.get("model"), llm.get("prompt_version"), "replayed from cache" if llm.get("cached") else "live call",
                llm.get("latency_s", 0), llm.get("prompt_tokens", 0), llm.get("output_tokens", 0)))

        with st.container(border=True):
            st.subheader("(a) What the LLM extracted")
            st.caption("As the user wrote it: amounts are value, scale and period, not yet rupees.")
            if s["extraction"] is not None:
                st.json(s["extraction"])
            else:
                st.write("Nothing was extracted.")

        with st.container(border=True):
            st.subheader("(b) What code decided")
            st.markdown("**Normalised input (rupees, financial year)**")
            if s["normalised_input"] is not None:
                st.json(s["normalised_input"])
            else:
                st.write("None: the engine is called only when the status is compute.")
            st.markdown("**Decisions**")
            st.code("\n".join("- " + d for d in s["decisions"]) or "(none)", language=None)

        with st.container(border=True):
            st.subheader("(c) Status")
            label = {"compute": ":green[compute]", "ask": ":orange[ask]", "unsupported": ":red[unsupported]",
                     "error": ":red[error]"}.get(s["status"], s["status"])
            st.markdown("### " + label)

            if s["status"] == "ask":
                if ss["round"] >= ui_steps.MAX_ROUNDS:
                    st.warning("Stopped after %d rounds of questions, as the CLI does." % ui_steps.MAX_ROUNDS)
                else:
                    with st.form("answers_%d" % ss["round"]):
                        typed = {q["id"]: st.text_input(q["question"], key="a_%d_%s" % (ss["round"], q["id"]),
                                                        help="question id %s (%s)" % (q["id"], q["source"]))
                                 for q in s["questions"]}
                        if st.form_submit_button("Submit answers"):
                            ss["pr"], ss["steps"] = ui_steps.submit_answers(ss["pr"], typed)
                            ss["round"] += 1
                            st.rerun()

            elif s["status"] == "unsupported":
                st.write("Not supported: %s. No tax computed." % ", ".join(s["unsupported"]))

            elif s["status"] == "compute":
                r = s["result"]
                st.success(r["badge"])
                st.markdown("**FY %s** (rules %s)" % (r["fy"], r["rule_version"]))
                st.table(r["rows"])
                st.markdown(r["better"])
                if r["assumption"]:
                    st.warning(r["assumption"])

                if r["breakup"] is None:
                    st.error("The engine's clause trace was not available, so no breakup is shown.")
                else:
                    for reg in ("old", "new"):
                        b = r["breakup"][reg]
                        with st.expander("Tax breakup: %s regime (final tax %s)" % (reg, ui_steps.money(b["final_tax"]))):
                            st.caption("Every amount is a step from the hand-coded engine's clause trace; nothing is "
                                       "recomputed here. 'derived' rows are the gap between two trace steps.")
                            st.table([{"Line": l["label"], "Clause": l["clause"],
                                       "Amount": l["shown"] if l["kind"] != "missing" else "not in trace",
                                       "Kind": l["kind"], "Note": l["note"]} for l in b["lines"]])
                            for n in b["notes"]:
                                st.caption("Engine note: " + n)
                            if b["missing"]:
                                st.caption("Missing from the trace: " + "; ".join(b["missing"]) + ".")

                with st.container(border=True):
                    st.markdown("**Assumptions**")
                    st.markdown("\n".join("- " + a for a in r["assumptions"]))

with meas_tab:
    path = ROOT / "RESULTS_TABLES.md"
    st.caption("Read-only view of %s, as committed. Nothing is recomputed here; "
               "regenerate it with `python3 -m project_a.eval.compare`." % path.name)
    st.markdown(path.read_text(encoding="utf-8") if path.exists() else "RESULTS_TABLES.md not found.")
