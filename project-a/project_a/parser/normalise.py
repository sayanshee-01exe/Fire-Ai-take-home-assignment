"""Pure code: turns an Extraction into rupees, a financial year and an outcome. No LLM here.

Code owns every number and every decision: amount = value x scale x (12 if monthly), AY -> FY,
which required facts are missing, and whether the outcome is compute / ask / unsupported.
FY 2024-25 and FY 2025-26 are computed, each with its own params file. A missing age, year or
salary is never guessed or defaulted.
"""
import copy
import re
from fractions import Fraction

from project_a.core.params import load_params
from project_a.core.schema import TaxInput, validate_input
from project_a.parser.schema_out import (AMBIGUITY_TAGS, CODE_CHECKED_TAGS, SALARY_FIELDS, Amount,
                                         Ambiguity)

SUPPORTED_FYS = ("2024-25", "2025-26")   # each has a params file the hand-coded engine loads by year
SCALE = {"ones": 1, "thousand": 1_000, "lakh": 100_000, "crore": 10_000_000}
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
METRO_NAMES = {"delhi", "new delhi", "mumbai", "bombay", "kolkata", "calcutta", "chennai", "madras",
               "दिल्ली", "नई दिल्ली", "मुंबई", "मुम्बई", "कोलकाता", "चेन्नई"}

TEMPLATES = {
    "age_missing": "How old are you (or the person whose salary this is) on 31 March of the tax year?",
    "year_missing_or_ambiguous": "Which financial year is this for (for example FY 2024-25)?",
    "city_missing": "Which city is the rented home in? (Delhi, Mumbai, Kolkata and Chennai count as metro.)",
    "interest_portion_unknown": "How much of the home-loan EMI for the year is interest? Only the interest counts.",
    "parents_age_missing": "Is either of your parents 60 or older?",
    "period_unclear": "Is the {field} figure per month or per year?",
    "amount_missing": "What is the {field} amount for the year, in rupees?",
    "conflicting_amounts": "You gave more than one figure for {field}. Which one is right, for the whole year?",
    "gross_vs_ctc": "What is your gross salary for the year (not CTC: leave out employer PF and unpaid variable pay)?",
    "other_ambiguity": "Could you clarify the {field}?",
}
FIELD_WORDS = {
    "gross_salary": "gross salary", "basic_salary": "basic salary", "hra_received": "HRA received",
    "rent_paid": "rent", "sec_80c": "80C investment", "sec_80ccd1b": "own NPS (80CCD(1B))",
    "sec_80d_self": "health-insurance premium for yourself and family",
    "sec_80d_parents": "parents' health-insurance premium", "home_loan_interest": "home-loan interest",
    "home_loan_emi": "home-loan EMI", None: "details",
}


class BadValue(ValueError):
    pass


# ---- amounts --------------------------------------------------------------------------------------
def parse_value(v):
    """'10,00,000' / '1.25' / 1.25 / '८२' -> Fraction. Rejects anything else (no guessing)."""
    if isinstance(v, bool) or v is None:
        raise BadValue("not a number: %r" % (v,))
    if isinstance(v, (int, float)):
        return Fraction(str(v))
    s = str(v).translate(DEVANAGARI_DIGITS).replace(",", "").replace("₹", "").replace(" ", "")
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        raise BadValue("not a plain number: %r" % (v,))
    return Fraction(s)


def normalise_amount(field, amt):
    """Returns (rupees or None, trace line, missing (tag, field) or None)."""
    if amt.value is None:
        return None, "%s: mentioned without a figure (%r)" % (field, amt.text), ("amount_missing", field)
    x = parse_value(amt.value) * SCALE[amt.scale]
    period = amt.period
    note = ""
    if period == "unknown" and x == 0:
        period, note = "annual", " (zero: per month or per year makes no difference)"
    if period == "unknown":
        if field not in SALARY_FIELDS:
            period, note = "annual", " (deductions are annual amounts)"
        elif amt.scale in ("lakh", "crore") and field != "rent_paid":
            period, note = "annual", " (salary in lakh/crore is quoted per year by convention)"
        else:
            return None, "%s: %r: per month or per year is unclear" % (field, amt.text), ("period_unclear", field)
    if period == "monthly":
        x *= 12
    if x.denominator != 1:
        return None, "%s: %r is not whole rupees" % (field, amt.text), ("other_ambiguity", field)
    rupees = int(x)
    trace = "%s: %s x %s%s = %d%s" % (field, amt.value, amt.scale,
                                       " x 12 (monthly)" if period == "monthly" else "", rupees, note)
    return rupees, trace, None


# ---- year -----------------------------------------------------------------------------------------
def _fy_label(start):
    return "%d-%02d" % (start, (start + 1) % 100)


def year_to_fy(year):
    """{'label','type'} -> (fy or None, trace). AY maps to the FY before it. Ambiguity -> None."""
    label, kind = (year or {}).get("label"), (year or {}).get("type", "none")
    if not label or kind == "none":
        return None, "year: not stated"
    s = str(label).translate(DEVANAGARI_DIGITS)
    m = re.search(r"(\d{4}|\d{2})\s*[-–/]\s*(\d{4}|\d{2})", s)
    if kind in ("FY", "AY") and m:
        a, b = m.groups()
        start = int(a) if len(a) == 4 else 2000 + int(a)
        end = int(b) if len(b) == 4 else start // 100 * 100 + int(b)
        if end != start + 1:
            return None, "year: %r is not a valid year pair" % label
        fy = start if kind == "FY" else start - 1
        return _fy_label(fy), "year: %s %r -> FY %s" % (kind, label, _fy_label(fy))
    if kind == "calendar":
        years = [int(y) for y in re.findall(r"\d{4}", s)]
        if len(years) == 2 and years[1] == years[0] + 1 and re.search(r"apr", s, re.I) and re.search(r"mar", s, re.I):
            return _fy_label(years[0]), "year: %r -> FY %s" % (label, _fy_label(years[0]))
        return None, "year: calendar %r does not fix a financial year" % label
    return None, "year: %r (%s) not understood" % (label, kind)


def is_metro(city_name):
    return str(city_name).strip().lower() in METRO_NAMES


# ---- decision -------------------------------------------------------------------------------------
def decide(ext):
    """Extraction -> dict(status, normalised_input, missing [(tag, field)], unsupported, trace)."""
    trace = []
    out = {"status": None, "normalised_input": None, "missing": [], "unsupported": [], "trace": trace}

    if ext.unsupported:
        out.update(status="unsupported", unsupported=sorted(set(ext.unsupported)))
        trace.append("unsupported: %s -> engine not called" % ", ".join(out["unsupported"]))
        return out

    fy, line = year_to_fy(ext.year)
    trace.append(line)
    if fy is not None and fy not in SUPPORTED_FYS:
        out.update(status="unsupported", unsupported=["other_unsupported"])
        trace.append("FY %s: no rules for this year -> engine not called" % fy)
        return out

    missing = []
    if fy is None:
        missing.append(("year_missing_or_ambiguous", None))

    age = None
    if not ext.age or ext.age.get("value") is None:
        missing.append(("age_missing", None))
        trace.append("age: not stated (never defaulted)")
    else:
        try:
            a = parse_value(ext.age["value"])
            if a.denominator != 1 or not 0 <= a <= 120:
                raise BadValue(a)
            age = int(a)
            trace.append("age: %d" % age)
        except BadValue:
            missing.append(("age_missing", None))
            trace.append("age: %r is not a whole number of years" % ext.age.get("value"))

    rupees = {}
    for field, amt in ext.amounts.items():
        if amt is None:
            continue
        value, line, miss = normalise_amount(field, amt)
        trace.append(line)
        if miss:
            missing.append(miss)
        else:
            rupees[field] = value

    mentioned = {f for f, a in ext.amounts.items() if a is not None}
    if "gross_salary" not in mentioned:
        missing.append(("amount_missing", "gross_salary"))
    if "home_loan_emi" in mentioned and "home_loan_interest" not in mentioned:
        missing.append(("interest_portion_unknown", "home_loan_interest"))
    rent = rupees.get("rent_paid", 0)
    city = (ext.city or {}).get("name")
    if rent > 0:
        if not city:
            missing.append(("city_missing", None))
        if "hra_received" not in mentioned:
            missing.append(("amount_missing", "hra_received"))
        elif rupees.get("hra_received", 0) > 0 and "basic_salary" not in mentioned:
            missing.append(("amount_missing", "basic_salary"))
    par_cap = load_params(fy or SUPPORTED_FYS[0])["limits"]["sec_80d"]["parents"]
    parents_senior = ext.parents_senior
    if rupees.get("sec_80d_parents", 0) > par_cap and parents_senior is None:
        missing.append(("parents_age_missing", None))
    elif "sec_80d_parents" in rupees and parents_senior is None:
        parents_senior = False
        trace.append("parents' age not stated but irrelevant: premium is within both 80D caps")

    for amb in ext.ambiguities:                    # tags code cannot check: trust the LLM's flag
        if amb.tag not in CODE_CHECKED_TAGS and (amb.tag, amb.field) not in missing:
            missing.append((amb.tag, amb.field))
            trace.append("LLM flagged %s%s" % (amb.tag, " (%s)" % amb.field if amb.field else ""))

    if missing:
        out.update(status="ask", missing=list(dict.fromkeys(missing)))
        trace.append("ask: %s -> engine not called" % ", ".join(
            t + (":" + f if f else "") for t, f in out["missing"]))
        return out

    inp = {"fy": fy, "age": age}
    inp.update({f: v for f, v in rupees.items() if f != "home_loan_emi"})
    if city or rent > 0 or "hra_received" in rupees:
        inp["metro"] = bool(city) and is_metro(city)
        trace.append("city %r -> %s" % (city, "metro" if inp["metro"] else "non-metro"))
    if "sec_80d_parents" in rupees:
        inp["parents_senior"] = bool(parents_senior)
    errors = validate_input(TaxInput.from_dict(inp))
    if errors:
        out.update(status="ask", missing=[("other_ambiguity", None)])
        trace.append("inconsistent input: %s" % "; ".join(errors))
        return out
    out.update(status="compute", normalised_input=inp)
    return out


def questions_for(missing, llm_ambiguities):
    """Code picks the facts; the LLM's wording is used when it proposed one for the same tag."""
    proposed = {}
    for a in llm_ambiguities:
        if a.question:
            proposed.setdefault((a.tag, a.field), a.question)
            proposed.setdefault((a.tag, None), a.question)
    qs = []
    for tag, field in missing:
        text = proposed.get((tag, field)) or (proposed.get((tag, None)) if field is None else None)
        qs.append({"id": "%s:%s" % (tag, field or ""), "tag": tag, "field": field,
                   "question": text or TEMPLATES[tag].format(field=FIELD_WORDS.get(field, field)),
                   "source": "llm" if text else "template"})
    return qs


# ---- answers to clarifying questions --------------------------------------------------------------
UNIT_WORDS = [(r"k|thousand|hazaar|hazar|hajar|हज़ार|हजार", "thousand"),
              (r"l|lakh|lakhs|lac|lacs|lpa|लाख", "lakh"), (r"cr|crore|crores|करोड़|करोड", "crore")]


def parse_answer_amount(text):
    """A short typed answer such as '1.8 lakh', '15k per month', '180000' -> Amount, or None."""
    s = str(text).translate(DEVANAGARI_DIGITS).strip()
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([^\d\s,.]*)", s)
    if not m:
        return None
    value, unit = m.group(1), m.group(2).lower().strip(".")
    scale = "ones"
    for pattern, name in UNIT_WORDS:
        if unit and re.fullmatch(pattern, unit):
            scale = name
    if unit and scale == "ones" and unit not in ("rs", "₹", "rupees", "rupaye"):
        return None
    period = parse_period(s) or ("annual" if unit == "lpa" else "unknown")
    return Amount(value=value, scale=scale, period=period, text=str(text))


def parse_period(text):
    t = str(text).lower()
    if re.search(r"month|mahin|महीन|monthly|\bpm\b|/m\b", t):
        return "monthly"
    if re.search(r"year|annual|annum|saal|साल|yearly|\bpa\b|lpa", t):
        return "annual"
    return None


def parse_yes_no(text):
    t = str(text).strip().lower()
    if re.fullmatch(r"(yes|y|haan|han|ha|ji|ji haan|हाँ|हां)\b.*", t):
        return True
    if re.fullmatch(r"(no|n|nahi|nahin|na|नहीं)\b.*", t):
        return False
    return None


def apply_answers(ext, answers):
    """Returns a new Extraction with the answers applied (question id -> typed answer)."""
    ext = copy.deepcopy(ext)
    answered = set()
    for qid, text in answers.items():
        tag, _, field = qid.partition(":")
        field = field or None
        if tag not in AMBIGUITY_TAGS or text is None or not str(text).strip():
            continue
        ok = True
        if tag == "age_missing":
            ext.age = {"value": str(text).strip(), "text": "answer: %s" % text}
        elif tag == "year_missing_or_ambiguous":
            kind = "AY" if re.search(r"\bAY\b|assessment", str(text), re.I) else "FY"
            ext.year = {"label": str(text).strip(), "type": kind, "text": "answer: %s" % text}
        elif tag == "city_missing":
            ext.city = {"name": str(text).strip(), "text": "answer: %s" % text}
        elif tag == "parents_age_missing":
            yn = parse_yes_no(text)
            ok = yn is not None
            if ok:
                ext.parents_senior = yn
        elif tag == "period_unclear" and field in ext.amounts and ext.amounts[field]:
            p = parse_period(text)
            ok = p is not None
            if ok:
                ext.amounts[field].period = p
        else:                                      # an amount answer for the field
            target = field or ("home_loan_interest" if tag == "interest_portion_unknown" else "gross_salary")
            amt = parse_answer_amount(text)
            ok = amt is not None
            if ok:
                if amt.period == "unknown" and target not in SALARY_FIELDS:
                    amt.period = "annual"
                ext.amounts[target] = amt
        if ok:
            answered.add((tag, field))
    ext.ambiguities = [a for a in ext.ambiguities if (a.tag, a.field) not in answered
                       and (a.tag, None) not in answered]
    return ext
