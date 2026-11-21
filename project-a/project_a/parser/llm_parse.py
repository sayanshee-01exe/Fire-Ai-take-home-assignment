"""The LLM extraction call: prompt file, strict JSON parsing, one retry on malformed output.

The prompt lives in prompt_v<N>.txt next to this file; every version is kept and its sha256 is
recorded with every result. The LLM is asked only to extract (see schema_out.py); nothing it
returns is used for arithmetic except through normalise.py.
"""
import hashlib
import json
import re
from pathlib import Path

from project_a.parser.schema_out import (AMBIGUITY_TAGS, AMOUNT_FIELDS, PERIODS, SCALES,
                                         UNSUPPORTED_TAGS, YEAR_TYPES, Ambiguity, Amount, Extraction)

# Few-shot examples (the six "Query:" blocks in each prompt file) were written new for the parser.
# How they were kept apart from the 50 NL queries: different ages (44, 47, 51, 67, 71: none used in
# the set), different cities (Jaipur, Chennai), different amounts (no salary, rent, HRA or deduction
# figure from the set except the 1.5 lakh 80C cap), and test_parser.py checks mechanically that each
# example's word-set Jaccard similarity to every query text is below 0.4 (highest at v1: 0.35).
# Only query texts were compared, never expected outputs.
HERE = Path(__file__).resolve().parent
TOP_KEYS = {"age", "year", "city", "parents_senior", "amounts", "unsupported", "ambiguities"}


def prompt_versions():
    return sorted((int(m.group(1)), p) for p in HERE.glob("prompt_v*.txt")
                  if (m := re.fullmatch(r"prompt_v(\d+)\.txt", p.name)))


def load_prompt(version=None):
    """Returns (version name, text, sha256). Default: the highest version on disk."""
    versions = dict(prompt_versions())
    n = int(str(version).lstrip("v")) if version else max(versions)
    text = versions[n].read_text(encoding="utf-8")
    return "v%d" % n, text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def user_prompt(query, problems=None):
    p = "Query:\n" + query
    if problems:
        p += ("\n\nYour previous reply was rejected: " + "; ".join(problems)
              + ". Reply again with only the JSON object, exactly in the required shape.")
    return p


def _check_keys(obj, required, where, problems, optional=()):
    if not isinstance(obj, dict):
        problems.append("%s is not an object" % where)
        return False
    extra = set(obj) - set(required) - set(optional)
    missing = set(required) - set(obj)
    if extra:
        problems.append("%s has unexpected keys %s" % (where, sorted(extra)))
    if missing:
        problems.append("%s is missing keys %s" % (where, sorted(missing)))
    return not extra and not missing


def _value_ok(v):
    if v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)):
        return True
    return isinstance(v, str) and re.fullmatch(r"[\d०-९][\d०-९,]*(\.[\d०-९]+)?", v.replace(" ", "")) is not None


def parse_extraction(text):
    """Strict: exact keys, closed vocabularies, no coercion. Returns (Extraction or None, problems)."""
    try:
        d = json.loads(text)
    except ValueError as e:
        return None, ["not valid JSON: %s" % e]
    problems = []
    if not _check_keys(d, TOP_KEYS, "top level", problems):
        return None, problems

    age = d["age"]
    if age is not None and _check_keys(age, ("value", "text"), "age", problems):
        if not _value_ok(age["value"]) or age["value"] is None:
            problems.append("age.value %r is not a number" % (age["value"],))
    year = d["year"]
    if _check_keys(year, ("label", "type", "text"), "year", problems):
        if year["type"] not in YEAR_TYPES:
            problems.append("year.type %r not in %s" % (year["type"], YEAR_TYPES))
    city = d["city"]
    if city is not None and _check_keys(city, ("name", "text"), "city", problems):
        if not isinstance(city["name"], str) or not city["name"].strip():
            problems.append("city.name must be a non-empty string")
    if d["parents_senior"] not in (True, False, None):
        problems.append("parents_senior must be true, false or null")

    amounts = {}
    if isinstance(d["amounts"], dict):
        for k, a in d["amounts"].items():
            if k not in AMOUNT_FIELDS:
                problems.append("amounts has unknown field %r" % k)
            elif a is None:
                amounts[k] = None
            elif _check_keys(a, ("value", "scale", "period", "text"), "amounts.%s" % k, problems):
                if not _value_ok(a["value"]):
                    problems.append("amounts.%s.value %r is not a number as written" % (k, a["value"]))
                if a["scale"] not in SCALES:
                    problems.append("amounts.%s.scale %r not in %s" % (k, a["scale"], SCALES))
                if a["period"] not in PERIODS:
                    problems.append("amounts.%s.period %r not in %s" % (k, a["period"], PERIODS))
                amounts[k] = Amount(**a)
    else:
        problems.append("amounts is not an object")

    unsupported = d["unsupported"]
    if not isinstance(unsupported, list) or any(t not in UNSUPPORTED_TAGS for t in unsupported):
        problems.append("unsupported must be a list of tags from %s" % (UNSUPPORTED_TAGS,))
    ambiguities = []
    if isinstance(d["ambiguities"], list):
        for i, a in enumerate(d["ambiguities"]):
            if _check_keys(a, ("tag", "field", "question"), "ambiguities[%d]" % i, problems):
                if a["tag"] not in AMBIGUITY_TAGS:
                    problems.append("ambiguities[%d].tag %r not in the closed list" % (i, a["tag"]))
                if a["field"] is not None and a["field"] not in AMOUNT_FIELDS:
                    problems.append("ambiguities[%d].field %r is not an amount field" % (i, a["field"]))
                ambiguities.append(Ambiguity(**a))
    else:
        problems.append("ambiguities is not a list")

    if problems:
        return None, problems
    return Extraction(age=age, year=year, city=city, parents_senior=d["parents_senior"], amounts=amounts,
                      unsupported=list(unsupported), ambiguities=ambiguities), []


def extract(query, client, version=None):
    """Calls the LLM; retries once with the problems listed. Returns (Extraction|None, raw, meta)."""
    name, system, sha = load_prompt(version)
    meta = {"prompt_version": name, "prompt_sha256": sha, "model": client.model, "attempts": 0,
            "prompt_tokens": 0, "output_tokens": 0, "latency_s": 0.0, "cached": True, "problems": []}
    problems, raw = None, ""
    for _ in range(2):
        resp = client.generate(user_prompt(query, problems), system=system, json_mode=True)
        meta["attempts"] += 1
        meta["prompt_tokens"] += resp.prompt_tokens
        meta["output_tokens"] += resp.output_tokens
        meta["latency_s"] += resp.latency_s
        meta["cached"] = meta["cached"] and resp.cached
        raw = resp.text
        ext, problems = parse_extraction(raw)
        if ext is not None:
            return ext, raw, meta
        meta["problems"].append(problems)
    return None, raw, meta
