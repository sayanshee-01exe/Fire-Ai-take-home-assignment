"""What the LLM is allowed to report, and the closed tag lists.

The LLM extracts; it does not compute. Every amount is reported as written (value, scale, period,
phrase); code in normalise.py turns it into rupees and decides the outcome.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

UNSUPPORTED_TAGS = (
    "employer_nps_80ccd2", "multi_city_hra", "capital_gains", "business_income",
    "non_resident", "foreign_income", "esop_perquisite", "other_unsupported",
)
AMBIGUITY_TAGS = (
    "age_missing", "city_missing", "interest_portion_unknown", "conflicting_amounts",
    "period_unclear", "gross_vs_ctc", "year_missing_or_ambiguous", "parents_age_missing",
    "amount_missing", "other_ambiguity",
)
# Tags code can check from the extraction itself; for these the LLM's flag is ignored and code decides.
CODE_CHECKED_TAGS = ("age_missing", "city_missing", "interest_portion_unknown", "period_unclear",
                     "year_missing_or_ambiguous", "parents_age_missing")

SCALES = ("ones", "thousand", "lakh", "crore")
PERIODS = ("annual", "monthly", "unknown")
YEAR_TYPES = ("FY", "AY", "calendar", "none")

# Schema money fields the LLM may fill, plus home_loan_emi (not in the schema: it only triggers a question).
AMOUNT_FIELDS = ("gross_salary", "basic_salary", "hra_received", "rent_paid", "sec_80c", "sec_80ccd1b",
                 "sec_80d_self", "sec_80d_parents", "home_loan_interest", "home_loan_emi")
SALARY_FIELDS = ("gross_salary", "basic_salary", "hra_received", "rent_paid")   # period matters

STATUSES = ("compute", "ask", "unsupported", "law-change", "parse_error")


@dataclass
class Amount:
    value: Optional[Union[str, int, float]]   # as written ("10,00,000", "1.25"); None = mentioned, no figure
    scale: str
    period: str
    text: str


@dataclass
class Ambiguity:
    tag: str
    field: Optional[str] = None
    question: Optional[str] = None             # the LLM's proposed wording (code may use it)


@dataclass
class Extraction:
    age: Optional[Dict] = None                 # {"value": <as stated>, "text": ...}
    year: Dict = field(default_factory=lambda: {"label": None, "type": "none", "text": None})
    city: Optional[Dict] = None                # {"name": ..., "text": ...}
    parents_senior: Optional[bool] = None
    amounts: Dict[str, Optional[Amount]] = field(default_factory=dict)
    unsupported: List[str] = field(default_factory=list)
    ambiguities: List[Ambiguity] = field(default_factory=list)


@dataclass
class ParseResult:
    status: str
    normalised_input: Optional[Dict] = None    # schema input (compute only)
    questions: List[Dict] = field(default_factory=list)
    unsupported_features: List[str] = field(default_factory=list)
    extraction: Optional[Extraction] = None
    trace: List[str] = field(default_factory=list)
    result: Optional[Dict] = None              # hand-coded engine result (compute only)
    raw_output: Optional[str] = None           # the LLM's text (always kept; needed for parse_error)
    llm: Dict = field(default_factory=dict)    # prompt version/sha, tokens, latency, attempts
    query: str = ""
