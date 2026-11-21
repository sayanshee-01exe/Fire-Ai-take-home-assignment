"""Compiles the written rules into Python with an LLM, once, offline from query time.

    python3 -m project_a.compiler.compile [--runs 3] [--temperature 0.2]

The LLM sees only: the rules text (rules/RULES_FY2024_25.md), the input schema, the output schema and
the language restrictions enforced by safety.py. It returns one function compute(inp). Each run is
saved as compiled/raw_v<N>.py with a metadata header, and summarised in compiled/manifest.json.
Uses the compiler role's model (GEMINI_MODEL_COMPILER). Retries are off: any 429 stops cleanly.
"""
import argparse
import ast
import dataclasses
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from project_a.compiler.safety import ALLOWED_CALLS, validate

HERE = Path(__file__).resolve().parent
COMPILED = HERE / "compiled"
PKG = HERE.parent
RULES = PKG / "rules" / "RULES_FY2024_25.md"
FY = "2024-25"
YEARS = {  # per financial year: rules file, output directory, the 31 March that decides age
    "2024-25": (RULES, COMPILED, "2025"),
    "2025-26": (PKG / "rules" / "RULES_FY2025_26.md", COMPILED / "fy2025_26", "2026"),
}

INPUT_SCHEMA = """\
INPUT: `inp` is a dict that always has ALL of these keys (amounts are annual, whole rupees, int):
  fy (str, "<FY>"), age (int, years on 31 March <MARCH>),
  gross_salary (int; total salary including HRA received and all allowances),
  basic_salary (int; basic + dearness allowance, the "salary" for the HRA rule),
  hra_received (int), rent_paid (int; rent paid in the year),
  metro (bool; rented home is in Delhi, Mumbai, Kolkata or Chennai),
  sec_80c (int), sec_80ccd1b (int), sec_80d_self (int), sec_80d_parents (int)  [amounts paid, before limits],
  parents_senior (bool; a parent is 60 or older),
  home_loan_interest (int; interest on a loan for a self-occupied house, before the limit)."""

OUTPUT_SCHEMA = """\
OUTPUT: return a dict with exactly these keys:
  "old_total_income": int  (total income under the old regime, after C-288A rounding)
  "old_tax": int           (final tax payable under the old regime, after C-288B rounding)
  "new_total_income": int
  "new_tax": int
  "best": "old" or "new"   (the lower final tax; a tie goes to "new")
  "steps": a list of [rule_name, value] pairs, in order, one per computation step, where rule_name is
           the clause ID from the rules (for example "C-STD-OLD") and value is the amount at that step."""

LANGUAGE = """\
CODE RESTRICTIONS (the code is checked by an allow-list and rejected if it breaks any of these):
- Exactly one function, `def compute(inp):`. No other top-level code, no nested functions.
- Allowed: assignments, if/elif/else, for loops over a list, a tuple or range(...), return, arithmetic
  (+ - * / // %%), comparisons, and/or/not, conditional expressions, dict/list/tuple literals, indexing.
- The only functions you may call are: %s (positional arguments only).
- NOT allowed: import, attribute access of any kind (so no inp.get(...), no list.append(...): read
  inp["key"] and write steps = steps + [["C-X", value]]), the ** operator, while, lambda, comprehensions,
  try/except, classes, global, f-strings, print, and any name starting with an underscore.
- Work in whole rupees where the rules say so; be exact about the rounding rules as written.
Return ONLY the Python source code: no explanation and no markdown fences.""" % ", ".join(ALLOWED_CALLS)

SYSTEM = ("You translate written Indian income-tax rules into one deterministic Python function. "
          "Implement the rules exactly as written, including every limit, rebate, marginal relief, "
          "surcharge band, cess and rounding rule. Do not use any knowledge beyond the rules given.")


def check_rules_file(text):
    """The prompt must carry rule TEXT only: no code, and no identifier from the engine or the oracle."""
    problems = []
    for pattern, what in ((r"```", "a code fence"), (r"(?m)^\s*(def|class|import|from)\s+\w", "Python code"),
                          (r"\blambda\b|\breturn\b", "Python code"), (r"\bFraction\b", "engine code")):
        if re.search(pattern, text):
            problems.append("rules file contains %s" % what)
    names = set()
    for path in (PKG / "engines" / "hand_coded.py", PKG / "tests" / "oracle.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    for n in sorted(names):
        if len(n) > 3 and re.search(r"\b%s\b" % re.escape(n), text):
            problems.append("rules file mentions the code identifier %r" % n)
    return problems


def build_prompt(fy=FY):
    rules_path, _, march = YEARS[fy]
    rules = rules_path.read_text(encoding="utf-8")
    problems = check_rules_file(rules)
    if problems:
        raise SystemExit("refusing to compile: " + "; ".join(problems))
    schema = INPUT_SCHEMA.replace("<FY>", fy).replace("<MARCH>", march)
    user = "RULES\n\n" + rules + "\n\n" + schema + "\n\n" + OUTPUT_SCHEMA + "\n\n" + LANGUAGE
    sha = hashlib.sha256((SYSTEM + "\n\n" + user).encode("utf-8")).hexdigest()
    return SYSTEM, user, sha, hashlib.sha256(rules.encode("utf-8")).hexdigest()


def extract_code(text):
    """The model is told not to use fences; if it does, take the first fenced block. Nothing else changes."""
    m = re.search(r"```(?:python)?[ \t]*\n(.*?)```", text, re.S)
    return (m.group(1), True) if m else (text, False)


def header(meta):
    lines = ["LLM-compiled tax engine: raw compile %d (UNREVIEWED; generated, not hand-edited)" % meta["n"],
             "model: %s   role: compiler   temperature: %s   run_index: %d"
             % (meta["model"], meta["temperature"], meta["run_index"]),
             "prompt_sha256: %s" % meta["prompt_sha256"],
             "rules: %s sha256 %s (FY %s)" % (YEARS[meta["fy"]][0].name, meta["rules_sha256"], meta["fy"]),
             "written_at: %s   from cache: %s   cache_key: %s" % (meta["written_at"], meta["cached"], meta["cache_key"]),
             "tokens: prompt %d, output %d (thinking %d)   latency_s: %.2f" % (
                 meta["prompt_tokens"], meta["output_tokens"], meta["thoughts_tokens"], meta["latency_s"]),
             "allow-list: %s" % ("passed" if not meta["violations"] else "REJECTED (%d violations)" % len(meta["violations"]))]
    return "".join("# " + l + "\n" for l in lines) + "\n"


def compile_rules(runs=3, temperature=0.2, client=None, out_dir=None, fy=FY):
    from project_a.llm.client import GeminiClient, QuotaExhausted
    from project_a.llm.config import get_config
    if client is None:
        cfg = dataclasses.replace(get_config("compiler"), max_retries=0, temperature=temperature)
        client = GeminiClient(config=cfg)
    system, user, sha, rules_sha = build_prompt(fy)
    out_dir = Path(out_dir or YEARS[fy][1])
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    done, stopped = [], None
    for i in range(runs):
        try:
            resp = client.generate(user, system=system, json_mode=False, run_index=i)
        except QuotaExhausted as e:
            stopped = "quota exhausted before compile %d: %s" % (i + 1, str(e)[:300])
            break
        code, fenced = extract_code(resp.text)
        violations = validate(code)
        meta = {"n": i + 1, "model": client.model, "temperature": client.config.temperature, "run_index": i,
                "prompt_sha256": sha, "rules_sha256": rules_sha, "cache_key": resp.cache_key,
                "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cached": resp.cached,
                "prompt_tokens": resp.prompt_tokens, "output_tokens": resp.output_tokens,
                "thoughts_tokens": resp.thoughts_tokens, "latency_s": resp.latency_s,
                "fence_stripped": fenced, "violations": violations, "fy": fy,
                "code_lines": len([l for l in code.splitlines() if l.strip() and not l.strip().startswith("#")])}
        (out_dir / ("raw_v%d.py" % (i + 1))).write_text(header(meta) + code.rstrip() + "\n", encoding="utf-8")
        manifest["raw_v%d" % (i + 1)] = meta
        done.append(meta)
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return done, stopped, client


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--fy", default=FY, choices=sorted(YEARS), help="which year's rules file to compile")
    args = ap.parse_args(argv)
    done, stopped, client = compile_rules(args.runs, args.temperature, fy=args.fy)
    for m in done:
        print("raw_v%d: %s, %d code lines, tokens %d/%d, %.1fs, cached %s%s" % (
            m["n"], "allow-list PASSED" if not m["violations"] else "allow-list REJECTED", m["code_lines"],
            m["prompt_tokens"], m["output_tokens"], m["latency_s"], m["cached"],
            "" if not m["violations"] else "\n    " + "\n    ".join(m["violations"])))
    print(client.calls_today_line())
    if stopped:
        print("STOPPED:", stopped)
        raise SystemExit(3)


if __name__ == "__main__":
    main()
