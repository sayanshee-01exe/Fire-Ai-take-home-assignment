"""LLM-compiled engine: allow-list, sandbox, engine error handling, compile step. No network."""
import dataclasses
import json
import time

import pytest

from project_a.compiler import compile as comp
from project_a.compiler.safety import validate
from project_a.compiler.sandbox import Sandbox, run_once
from project_a.core.schema import TaxInput
from project_a.engines.llm_compiled import LLMCompiledEngine
from project_a.eval import run as eval_run
from project_a.tests.test_llm import FakeTransport, client_for, no_network, ok  # noqa: F401
from project_a.tests.test_llm_ops import QUOTA

GOOD = '''def compute(inp):
    """A tiny, obviously incomplete tax function used to exercise the plumbing."""
    gross = inp["gross_salary"]
    steps = []
    old_ti = max(0, gross - 50000)
    new_ti = max(0, gross - 75000)
    steps = steps + [["C-STD-OLD", 50000]]
    total = 0
    for x in [1, 2, 3]:
        total = total + x
    for i in range(2):
        total = total - 1
    best = "new" if new_ti <= old_ti else "old"
    return {"old_total_income": old_ti, "old_tax": 0, "new_total_income": new_ti, "new_tax": total,
            "best": best, "steps": steps}
'''


def fn(body):
    return "def compute(inp):\n" + "".join("    " + l + "\n" for l in body.splitlines())


HOSTILE = [
    ("import os\n" + fn("return 1"), 1, "import"),
    ("from os import system\n" + fn("return 1"), 1, "import"),
    (fn("import os\nreturn 1"), 2, "import"),
    (fn("return __import__('os')"), 2, "underscore"),
    (fn("return open('/etc/passwd')"), 2, "builtin 'open'"),
    (fn("return ().__class__.__bases__"), 2, "attribute"),
    (fn("return eval('1+1')"), 2, "builtin 'eval'"),
    (fn("exec('x=1')\nreturn 1"), 2, "bare expressions"),
    (fn("x = exec\nreturn 1"), 2, "builtin 'exec'"),
    (fn("return getattr(inp, 'keys')"), 2, "builtin 'getattr'"),
    (fn("while True:\n    pass\nreturn 1"), 2, "while"),
    (fn("f = lambda x: x\nreturn 1"), 2, "lambda"),
    (fn("global counter\nreturn 1"), 2, "global"),
    ("counter = 0\n" + fn("return counter"), 1, "top level"),
    (fn("try:\n    x = 1 / 0\nexcept Exception:\n    x = 0\nreturn x"), 2, "try/except"),
    ("class A:\n    pass\n" + fn("return 1"), 1, "class"),
    (fn("return [x for x in inp]"), 2, "comprehension"),
    (fn("return sum(x for x in range(10))"), 2, "generator"),
    (fn("def helper():\n    return 1\nreturn helper()"), 2, "only one function"),
    (fn("return inp.get('age')"), 2, "attribute"),
    (fn("_secret = 1\nreturn _secret"), 2, "underscore"),
    (fn("return 10 ** 10 ** 10"), 2, "power"),
    (fn("print(inp)\nreturn 1"), 2, "bare expressions"),
    (fn("return min(inp, key=len)"), 2, "keyword"),
    (fn("min = 3\nreturn min"), 2, "assign to the builtin"),
    (fn("with x:\n    pass\nreturn 1"), 2, "with"),
    (fn("return f'{inp}'"), 2, "f-string"),
    (fn("for k in inp.items():\n    pass\nreturn 1"), 2, "attribute"),
    ("def compute(inp, extra=1):\n    return 1\n", 1, "signature"),
    ("def run(inp):\n    return 1\n", 1, "named compute"),
    (fn("return type(inp)"), 2, "builtin 'type'"),
    (fn("raise ValueError('x')"), 2, "raise"),
    ("def compute(inp):\n    return (\n", None, "syntax error"),
]


@pytest.mark.parametrize("source, line, reason", HOSTILE, ids=[h[2] + "-%d" % i for i, h in enumerate(HOSTILE)])
def test_allow_list_rejects_hostile_or_sloppy_code(source, line, reason):
    problems = validate(source)
    assert problems, source
    assert any(reason in p for p in problems), problems
    if line is not None:
        assert any(p.startswith("line %d:" % line) for p in problems if reason in p), problems


def test_allow_list_accepts_plain_code():
    assert validate(GOOD) == []


# ---- sandbox ------------------------------------------------------------------------------------
def test_sandbox_runs_validated_code_fast():
    box = Sandbox(GOOD)
    try:
        r1 = box.run({"gross_salary": 1_000_000})
        r2 = box.run({"gross_salary": 50_000})              # same process: no start-up cost
    finally:
        box.close()
    assert r1["ok"] and r1["output"]["old_total_income"] == 950_000 and r1["output"]["new_tax"] == 4
    assert r2["ok"] and r2["output"]["new_total_income"] == 0
    assert r2["compute_s"] < 0.01 and r2["roundtrip_s"] < 0.5


def test_sandbox_kills_a_runaway_loop_on_the_wall_clock():
    loop = fn("t = 0\nfor i in range(1000000000000):\n    t = t + i\nreturn t")
    assert validate(loop) == []                            # valid code can still run forever
    t0 = time.monotonic()
    r = run_once(loop, {}, timeout_s=1.0)
    assert not r["ok"] and "timeout" in r["error"] and time.monotonic() - t0 < 5


def test_sandbox_kills_a_memory_hog():
    hog = fn('s = "ab"\nfor i in range(40):\n    s = s + s\nreturn len(s)')
    r = run_once(hog, {}, timeout_s=20.0, mem_mb=256)
    assert not r["ok"] and ("memory" in r["error"].lower() or "died" in r["error"]), r


def test_sandbox_restarts_after_a_kill():
    src = fn('if inp["spin"]:\n    for i in range(1000000000000):\n        pass\nreturn 7')
    box = Sandbox(src, timeout_s=1.0)
    try:
        assert "timeout" in box.run({"spin": True})["error"]
        assert box.run({"spin": False})["output"] == 7
    finally:
        box.close()


def test_sandbox_refuses_unvalidated_code_and_has_no_dangerous_builtins():
    assert "allow-list" in run_once("import os\n" + fn("return 1"), {})["error"]
    box = Sandbox(fn("return open('x')"))
    box.violations = []                                    # pretend the validator missed it
    try:
        r = box.run({})
    finally:
        box.close()
    assert not r["ok"] and "NameError" in r["error"]


def test_sandbox_reports_exceptions_as_errors():
    r = run_once(fn('return inp["missing"]'), {})
    assert not r["ok"] and "KeyError" in r["error"]


# ---- engine -------------------------------------------------------------------------------------
def engine_for(tmp_path, source, name="raw_v1"):
    (tmp_path / (name + ".py")).write_text(source)
    return LLMCompiledEngine(name, tmp_path, timeout_s=2.0)


CASE = TaxInput.from_dict({"fy": "2024-25", "age": 30, "gross_salary": 780000})


@pytest.mark.parametrize("source, problem", [
    ("import os\n" + fn("return 1"), "allow-list"),
    (fn('return inp["nope"]'), "KeyError"),
    (fn('return [1, 2]'), "not a dict"),
    (fn('return {"old_total_income": 1, "old_tax": "12", "new_total_income": 1, "new_tax": 1, "best": "new", "steps": []}'),
     "old_tax missing or not a number"),
    (fn('return {"old_total_income": 1, "old_tax": 1, "new_total_income": 1, "new_tax": 1, "best": "both", "steps": []}'),
     "best is 'both'"),
])
def test_engine_returns_error_results_never_crashes(tmp_path, source, problem):
    e = engine_for(tmp_path, source)
    try:
        res = e.calculate(CASE)
    finally:
        e.close()
    assert res.error and problem in res.error and res.old.tax is None


def test_engine_reports_wrong_numbers_exactly(tmp_path):
    e = engine_for(tmp_path, fn('return {"old_total_income": 730001, "old_tax": 60841.5, "new_total_income": 705000, '
                                '"new_tax": 21320, "best": "old", "steps": [["C-X", 1]]}'))
    try:
        res = e.calculate(CASE)
    finally:
        e.close()
    assert res.error is None and (res.old.tax, res.new.tax, res.best) == (60841.5, 21320, "old")
    assert res.steps == [["C-X", 1]] and res.latency_s > 0


# ---- compile step (fake transport) --------------------------------------------------------------
def test_rules_file_has_no_code_and_the_prompt_has_only_rules_and_schemas():
    assert comp.check_rules_file(comp.RULES.read_text()) == []
    system, user, sha, _ = comp.build_prompt()
    assert comp.RULES.read_text() in user and "OUTPUT: return a dict" in user and len(sha) == 64
    for leaked in ("_slab_tax", "round_statutory", "Fraction", "def compute(inp):\n    "):
        assert leaked not in user
    assert comp.check_rules_file("use _slab_tax here") and comp.check_rules_file("```\ncode\n```")


def test_extract_code_takes_only_the_fenced_block():
    assert comp.extract_code("Here:\n```python\ndef compute(inp):\n    return 1\n```\nbye") == (
        "def compute(inp):\n    return 1\n", True)
    assert comp.extract_code(GOOD) == (GOOD, False)


def _compile_client(tmp_path, monkeypatch, transport, temperature=0.2):
    c = client_for(tmp_path, monkeypatch, transport)
    c.config = dataclasses.replace(c.config, temperature=temperature, max_retries=0)
    return c


def test_three_compiles_are_saved_with_headers_and_distinct_cache_keys(tmp_path, monkeypatch):
    t = FakeTransport(ok("```python\n" + GOOD + "```"))
    done, stopped, client = comp.compile_rules(3, 0.2, client=_compile_client(tmp_path, monkeypatch, t),
                                               out_dir=tmp_path / "compiled")
    assert stopped is None and len(t.calls) == 3
    assert len({m["cache_key"] for m in done}) == 3
    assert all(json.loads(c["body"])["generationConfig"]["temperature"] == 0.2 for c in t.calls)
    text = (tmp_path / "compiled" / "raw_v2.py").read_text()
    assert text.startswith("# LLM-compiled tax engine: raw compile 2") and "prompt_sha256:" in text
    assert validate(text) == [] and done[1]["violations"] == []
    manifest = json.loads((tmp_path / "compiled" / "manifest.json").read_text())
    assert set(manifest) == {"raw_v1", "raw_v2", "raw_v3"} and manifest["raw_v1"]["fence_stripped"]


def test_compile_cache_key_includes_temperature():
    from project_a.llm.client import cache_key
    assert cache_key("m", "s", "p", 0.2, 0) != cache_key("m", "s", "p", 0.0, 0) != cache_key("m", "s", "p", 0.2, 1)


def test_a_429_stops_the_compile_cleanly(tmp_path, monkeypatch):
    t = FakeTransport(ok(GOOD), QUOTA)
    done, stopped, _ = comp.compile_rules(3, 0.2, client=_compile_client(tmp_path, monkeypatch, t),
                                          out_dir=tmp_path / "compiled")
    assert len(done) == 1 and "quota" in stopped and len(t.calls) == 2      # no retry after the 429
    assert sorted(p.name for p in (tmp_path / "compiled").glob("raw_v*.py")) == ["raw_v1.py"]


# ---- harness --------------------------------------------------------------------------------------
def test_harness_compiled_refuses_heldout(tmp_path):
    with pytest.raises(SystemExit) as e:
        eval_run.main(["--engine", "llm_compiled", "--artifact", "raw_v1", "--split", "heldout",
                       "--out-dir", str(tmp_path)])
    assert e.value.code == 2 and list(tmp_path.iterdir()) == []


def test_harness_compiled_runs_with_zero_tokens(tmp_path):
    (tmp_path / "raw_v1.py").write_text(GOOD)
    s = eval_run.run("llm_compiled", "official", artifact="raw_v1", compiled_dir=tmp_path, out_dir=tmp_path / "out")
    assert s["runs"] == 30 and s["prompt_tokens"] == 0 and s["output_tokens"] == 0 and s["unparseable"] == 0
    assert s["compute_mean_s"] < 0.01


# ---- the committed artifacts ----------------------------------------------------------------------
HELPER = '''def helper(x):
    return x + 1

def compute(inp):
    def inner(y):
        return y * 2
    return {"v": helper(inner(inp["a"]))}
'''


def test_helper_functions_are_rejected_strictly_and_allowed_only_in_diagnostic_mode():
    strict = validate(HELPER)
    assert any("only one function" in p for p in strict) and any("called helper" in p for p in strict)
    assert validate(HELPER, allow_helpers=True) == []
    assert validate("import os\n" + HELPER, allow_helpers=True)           # nothing else is relaxed
    assert run_once(HELPER, {"a": 3})["ok"] is False
    assert run_once(HELPER, {"a": 3}, allow_helpers=True)["output"] == {"v": 7}


def test_reviewed_artifact_passes_strictly_and_matches_the_official_30(tmp_path):
    from project_a.engines.llm_compiled import COMPILED
    assert validate((COMPILED / "reviewed.py").read_text()) == []
    s = eval_run.run("llm_compiled", "official", artifact="reviewed", out_dir=tmp_path)
    assert (s["tax_exact"], s["income_exact"], s["unparseable"]) == (30, 30, 0)


@pytest.mark.parametrize("artifact", ["raw_v1", "raw_v2", "raw_v3"])
def test_raw_compiles_are_rejected_by_the_strict_allow_list(artifact):
    from project_a.engines.llm_compiled import COMPILED
    assert validate((COMPILED / (artifact + ".py")).read_text())
