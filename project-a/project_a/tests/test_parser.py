"""Plain-language parser: pure-code normalisation and decisions, then the pipeline with canned LLM
output (fake transport, no network)."""
import hashlib
import json
import re
from pathlib import Path

import pytest

from project_a.core.schema import TaxInput
from project_a.engines.hand_coded import HandCodedEngine
from project_a.eval import nl_eval
from project_a.parser import pipeline
from project_a.parser.llm_parse import load_prompt, parse_extraction, prompt_versions
from project_a.parser.normalise import (apply_answers, decide, normalise_amount, parse_answer_amount,
                                        year_to_fy)
from project_a.parser.schema_out import AMBIGUITY_TAGS, UNSUPPORTED_TAGS, Amount, Extraction
from project_a.tests.test_llm import FakeTransport, client_for, no_network, ok  # noqa: F401 (fixture)

NL = Path(__file__).resolve().parent / "nl"
TOPIC_MAP_SHA256 = "766d5d15d5d45bbabfa9fd8ec9b680372208ecef4be079031da25807492680c0"


def A(value, scale="ones", period="annual", text="t"):
    return Amount(value=value, scale=scale, period=period, text=text)


def ext(**kw):
    base = dict(age={"value": "30", "text": "30"}, year={"label": "FY 2024-25", "type": "FY", "text": "x"},
                amounts={"gross_salary": A("12", "lakh")})
    base.update(kw)
    return Extraction(**base)


# ---- amounts ------------------------------------------------------------------------------------
@pytest.mark.parametrize("field, amt, rupees", [
    ("gross_salary", A("1.25", "lakh", "monthly"), 1_500_000),       # "1.25 lakh per month"
    ("gross_salary", A("0.5", "crore", "annual"), 5_000_000),        # "0.5 cr"
    ("gross_salary", A("6", "lakh", "annual"), 600_000),             # "6 LPA"
    ("gross_salary", A("65", "thousand", "monthly"), 780_000),       # "65k per month"
    ("gross_salary", A("10,00,000", "ones", "annual"), 1_000_000),
    ("gross_salary", A("5,00,50,010"), 50_050_010),
    ("gross_salary", A("1", "crore", "unknown"), 10_000_000),        # lakh/crore salary is annual
    ("rent_paid", A("25", "thousand", "monthly"), 300_000),
    ("sec_80c", A("1.5", "lakh", "unknown"), 150_000),               # deductions are annual
    ("gross_salary", A("१२", "lakh"), 1_200_000),                    # Devanagari digits
    ("gross_salary", A(797230), 797_230),                            # a JSON number
])
def test_amount_normalisation(field, amt, rupees):
    value, _, missing = normalise_amount(field, amt)
    assert (value, missing) == (rupees, None)


@pytest.mark.parametrize("field, amt, tag", [
    ("gross_salary", A("85,000", "ones", "unknown"), "period_unclear"),
    ("rent_paid", A("30", "thousand", "unknown"), "period_unclear"),
    ("rent_paid", A("1.2", "lakh", "unknown"), "period_unclear"),
    ("sec_80c", A(None), "amount_missing"),
    ("gross_salary", A("1.234567", "lakh"), "other_ambiguity"),      # not whole rupees: never rounded
])
def test_amounts_that_must_be_asked_about(field, amt, tag):
    value, _, missing = normalise_amount(field, amt)
    assert value is None and missing == (tag, field)


@pytest.mark.parametrize("year, fy", [
    ({"label": "FY 2024-25", "type": "FY"}, "2024-25"), ({"label": "FY24-25", "type": "FY"}, "2024-25"),
    ({"label": "2024-25 ke liye", "type": "FY"}, "2024-25"), ({"label": "AY 2025-26", "type": "AY"}, "2024-25"),
    ({"label": "Assessment year 2025-26", "type": "AY"}, "2024-25"),
    ({"label": "April 2024 to March 2025", "type": "calendar"}, "2024-25"),
    ({"label": "FY 2025-26", "type": "FY"}, "2025-26"), ({"label": "AY 2026-27", "type": "AY"}, "2025-26"),
    ({"label": "2024", "type": "calendar"}, None), ({"label": "is saal", "type": "none"}, None),
    ({"label": None, "type": "none"}, None), ({"label": "FY 2024-26", "type": "FY"}, None),
])
def test_year_mapping(year, fy):
    assert year_to_fy(year)[0] == fy


# ---- decisions ----------------------------------------------------------------------------------
def test_compute_builds_a_schema_valid_input():
    d = decide(ext(city={"name": "Delhi", "text": "Delhi"}, amounts={
        "gross_salary": A("1", "lakh", "monthly"), "basic_salary": A("3", "lakh"),
        "hra_received": A("1.5", "lakh"), "rent_paid": A("20", "thousand", "monthly")}))
    assert d["status"] == "compute"
    assert d["normalised_input"] == {"fy": "2024-25", "age": 30, "gross_salary": 1_200_000, "basic_salary": 300_000,
                                     "hra_received": 150_000, "rent_paid": 240_000, "metro": True}


@pytest.mark.parametrize("change, missing", [
    (dict(age=None), ("age_missing", None)),
    (dict(year={"label": None, "type": "none", "text": None}), ("year_missing_or_ambiguous", None)),
    (dict(amounts={"sec_80c": A("1", "lakh")}), ("amount_missing", "gross_salary")),
    (dict(amounts={"gross_salary": A("12", "lakh"), "basic_salary": A("6", "lakh"), "hra_received": A("3", "lakh"),
                   "rent_paid": A("30", "thousand", "monthly")}), ("city_missing", None)),
    (dict(amounts={"gross_salary": A("18", "lakh"), "home_loan_emi": A("40", "thousand", "monthly")}),
     ("interest_portion_unknown", "home_loan_interest")),
    (dict(amounts={"gross_salary": A("22", "lakh"), "sec_80d_parents": A("45", "thousand")}),
     ("parents_age_missing", None)),
])
def test_required_facts_are_asked_never_defaulted(change, missing):
    d = decide(ext(**change))
    assert d["status"] == "ask" and missing in d["missing"] and d["normalised_input"] is None


def test_parents_age_is_not_asked_when_it_cannot_change_the_result():
    d = decide(ext(amounts={"gross_salary": A("22", "lakh"), "sec_80d_parents": A("20", "thousand")}))
    assert d["status"] == "compute" and d["normalised_input"]["parents_senior"] is False


def test_code_checked_tags_ignore_the_llm_but_other_tags_are_trusted():
    from project_a.parser.schema_out import Ambiguity
    d = decide(ext(ambiguities=[Ambiguity("age_missing")]))          # age is present: code says compute
    assert d["status"] == "compute"
    d = decide(ext(ambiguities=[Ambiguity("gross_vs_ctc", "gross_salary")]))
    assert d["status"] == "ask" and ("gross_vs_ctc", "gross_salary") in d["missing"]


@pytest.mark.parametrize("tag", UNSUPPORTED_TAGS)
def test_unsupported_tag_routes_to_unsupported(tag):
    d = decide(ext(unsupported=[tag], age=None))
    assert d["status"] == "unsupported" and d["unsupported"] == [tag]


def test_fy_2025_26_is_computed_not_routed_to_law_change():
    d = decide(ext(year={"label": "FY 2025-26", "type": "FY", "text": "x"}))
    assert d["status"] == "compute" and d["normalised_input"]["fy"] == "2025-26"
    assert decide(ext(year={"label": "FY 2023-24", "type": "FY", "text": "x"}))["status"] == "unsupported"


@pytest.mark.parametrize("period", ["annual", "unknown", "monthly"])
def test_zero_amount_period_never_triggers_a_question(period):
    # NL-14 ("rent nahi deta"): the model reports rent 0 sometimes as annual, sometimes as unknown.
    amounts = {"gross_salary": A("12", "lakh"), "basic_salary": A("6", "lakh"), "hra_received": A("2", "lakh"),
               "rent_paid": A("0", "ones", period, "rent nahi deta")}
    d = decide(ext(amounts=amounts, city={"name": "Pune", "text": "Pune"}))
    assert d["status"] == "compute", d["trace"]
    assert d["normalised_input"]["rent_paid"] == 0


# ---- pipeline with canned LLM output ------------------------------------------------------------
def canned(**kw):
    d = {"age": {"value": "30", "text": "age 30"}, "year": {"label": "FY 2024-25", "type": "FY", "text": "FY 2024-25"},
         "city": None, "parents_senior": None,
         "amounts": {"gross_salary": {"value": "65", "scale": "thousand", "period": "monthly", "text": "65k per month"}},
         "unsupported": [], "ambiguities": []}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


class SpyEngine(HandCodedEngine):
    calls = 0

    def calculate(self, inp):
        SpyEngine.calls += 1
        return super().calculate(inp)


@pytest.fixture
def spy():
    SpyEngine.calls = 0
    return SpyEngine()


def run(tmp_path, monkeypatch, *texts, engine=None):
    t = FakeTransport(*[ok(x) for x in texts])
    fresh = tmp_path / ("run%d" % len(list(tmp_path.glob("run*"))))   # own cache per call: no replay
    fresh.mkdir()
    pr = pipeline.parse("query", client=client_for(fresh, monkeypatch, t), version="v1", engine=engine)
    return pr, t


def test_pipeline_compute_uses_code_arithmetic_and_the_engine(tmp_path, monkeypatch, spy):
    pr, _ = run(tmp_path, monkeypatch, canned(), engine=spy)
    assert pr.status == "compute" and spy.calls == 1
    assert pr.normalised_input == {"fy": "2024-25", "age": 30, "gross_salary": 780_000}
    assert (pr.result["old_tax"], pr.result["new_tax"]) == (60_840, 5_200)   # official OC-20 figures
    assert pr.llm["prompt_sha256"] == load_prompt("v1")[2]


@pytest.mark.parametrize("kw, status", [
    (dict(age=None), "ask"),
    (dict(unsupported=["capital_gains"]), "unsupported"),
    (dict(year={"label": "FY 2023-24", "type": "FY", "text": "x"}), "unsupported"),
])
def test_pipeline_non_compute_outcomes_never_call_the_engine(tmp_path, monkeypatch, spy, kw, status):
    pr, _ = run(tmp_path, monkeypatch, canned(**kw), engine=spy)
    assert pr.status == status and spy.calls == 0 and pr.result is None


def test_pipeline_fy_2025_26_computes_with_the_fy_2025_26_rules(tmp_path, monkeypatch, spy):
    pr, _ = run(tmp_path, monkeypatch, canned(year={"label": "FY 2025-26", "type": "FY", "text": "x"}), engine=spy)
    assert pr.status == "compute" and spy.calls == 1
    assert pr.result["rule_version"] == HandCodedEngine().calculate(TaxInput(fy="2025-26", age=30,
                                                                           gross_salary=780_000)).rule_version
    # 7,80,000 - 75,000 = 7,05,000 new-regime taxable: within the FY 2025-26 87A rebate (12 lakh); old regime as FY 2024-25.
    assert (pr.result["old_tax"], pr.result["new_tax"]) == (60_840, 0)


def test_cli_states_the_no_deductions_assumption_only_when_none_were_given(tmp_path, monkeypatch, capsys):
    from project_a import cli
    pr, _ = run(tmp_path, monkeypatch, canned())                          # salary only
    cli.show(pr)
    out = capsys.readouterr().out
    assert cli.NO_DEDUCTIONS_NOTE in out
    assert "tax Rs 60,840" in out and "tax Rs 5,200" in out               # numbers unchanged (OC-20)
    with_80c = json.loads(canned())
    with_80c["amounts"]["sec_80c"] = {"value": "1.5", "scale": "lakh", "period": "annual", "text": "80C 1.5L"}
    pr, _ = run(tmp_path, monkeypatch, json.dumps(with_80c))
    cli.show(pr)
    assert pr.status == "compute" and cli.NO_DEDUCTIONS_NOTE not in capsys.readouterr().out


def test_llm_numbers_are_never_used_for_arithmetic(tmp_path, monkeypatch, spy):
    # A model that "helpfully" adds its own total or tax is rejected outright (unknown keys) ...
    with_total = json.loads(canned())
    with_total["amounts"]["gross_salary"]["annual_total"] = 999
    pr, t = run(tmp_path, monkeypatch, json.dumps(with_total), json.dumps(dict(with_total, tax=1)), engine=spy)
    assert pr.status == "parse_error" and spy.calls == 0 and len(t.calls) == 2
    # ... and a pre-multiplied figure is taken literally, as written: 780000 'monthly' is x12 by code.
    pre = json.loads(canned())
    pre["amounts"]["gross_salary"] = {"value": "780000", "scale": "ones", "period": "annual", "text": "65k pm"}
    pr, _ = run(tmp_path, monkeypatch, json.dumps(pre), engine=spy)
    assert pr.normalised_input["gross_salary"] == 780_000           # value x scale, nothing else


def test_malformed_json_is_retried_once_then_parse_error(tmp_path, monkeypatch):
    pr, t = run(tmp_path, monkeypatch, "```json\n{}\n```", canned())
    assert pr.status == "compute" and pr.llm["attempts"] == 2 and len(t.calls) == 2
    assert "rejected" in json.loads(t.calls[1]["body"])["contents"][0]["parts"][0]["text"]
    pr, t = run(tmp_path, monkeypatch, "not json", "still not json")
    assert pr.status == "parse_error" and pr.raw_output == "still not json" and len(t.calls) == 2


@pytest.mark.parametrize("bad", [
    canned(unsupported=["crypto"]), canned(ambiguities=[{"tag": "vibes", "field": None, "question": "?"}]),
    canned(amounts={"gross_salary": {"value": "12 lakh", "scale": "ones", "period": "annual", "text": "x"}}),
    canned(amounts={"salary": {"value": "12", "scale": "lakh", "period": "annual", "text": "x"}}),
    canned(amounts={"gross_salary": {"value": "12", "scale": "lakhs", "period": "annual", "text": "x"}}),
])
def test_strict_parsing_rejects_off_list_values(bad):
    ext_, problems = parse_extraction(bad)
    assert ext_ is None and problems


def test_answers_are_applied_by_code_and_re_decided(tmp_path, monkeypatch, spy):
    pr, _ = run(tmp_path, monkeypatch, canned(age=None, amounts={
        "gross_salary": {"value": "12", "scale": "lakh", "period": "annual", "text": "12 lakh"},
        "basic_salary": {"value": "6", "scale": "lakh", "period": "annual", "text": "basic 6 lakh"},
        "hra_received": {"value": "3", "scale": "lakh", "period": "annual", "text": "HRA 3 lakh"},
        "rent_paid": {"value": "30", "scale": "thousand", "period": "monthly", "text": "rent 30k"}}), engine=spy)
    assert pr.status == "ask" and {q["id"] for q in pr.questions} == {"age_missing:", "city_missing:"}
    pr2 = pipeline.answer(pr, {"age_missing:": "34"}, engine=spy)
    assert pr2.status == "ask" and [q["id"] for q in pr2.questions] == ["city_missing:"]
    pr3 = pipeline.answer(pr2, {"city_missing:": "Pune"}, engine=spy)
    assert pr3.status == "compute" and spy.calls == 1
    assert pr3.normalised_input["metro"] is False and pr3.normalised_input["rent_paid"] == 360_000


@pytest.mark.parametrize("text, rupees", [("1.8 lakh", 180_000), ("1,80,000", 180_000), ("15k per month", 180_000),
                                          ("Rs 180000", 180_000), ("0.18 cr", 1_800_000)])
def test_typed_answers_are_parsed_by_code(text, rupees):
    field = "gross_salary" if "cr" in text else "home_loan_interest"
    amt = parse_answer_amount(text)
    if amt.period == "unknown":
        amt.period = "annual"
    assert normalise_amount(field, amt)[0] == rupees


def test_unparseable_answer_leaves_the_question_open():
    e = apply_answers(ext(age=None), {"age_missing:": ""})
    assert decide(e)["status"] == "ask"


# ---- harness, topic map, few-shot examples --------------------------------------------------------
def test_nl_eval_refuses_heldout_without_the_flag(tmp_path):
    with pytest.raises(SystemExit) as e:
        nl_eval.main(["--split", "heldout", "--out-dir", str(tmp_path)])
    assert e.value.code == 2 and list(tmp_path.iterdir()) == []


def test_topic_map_is_unchanged_and_uses_closed_tags():
    raw = (NL / "topic_map.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == TOPIC_MAP_SHA256
    m = json.loads(raw)["map"]
    rows = {json.loads(l)["id"]: json.loads(l) for l in (NL / "queries.jsonl").read_text().splitlines() if l.strip()}
    assert set(m) == set(rows)
    for qid, tags in m.items():
        assert all(t in AMBIGUITY_TAGS + UNSUPPORTED_TAGS for t in tags)
        assert bool(tags) == (rows[qid]["expected_behavior"] in ("ask", "unsupported"))


@pytest.mark.parametrize("version", [v for v, _ in prompt_versions()])
def test_few_shot_examples_are_valid_and_not_copies_of_nl_queries(version):
    _, text, _ = load_prompt(version)
    blocks = [b for b in text.split("\n\n") if b.startswith("Query: ")]
    assert len(blocks) == 6
    tok = lambda s: set(re.findall(r"\w+", s.lower()))
    queries = [json.loads(l)["text"] for l in (NL / "queries.jsonl").read_text().splitlines() if l.strip()]
    for b in blocks:
        q, out = b.split("\n", 1)
        q = q[len("Query: "):]
        assert parse_extraction(out)[1] == []
        assert decide(parse_extraction(out)[0])["status"] in ("compute", "ask")
        for t in queries:
            assert len(tok(q) & tok(t)) / len(tok(q) | tok(t)) < 0.4, (q, t)
