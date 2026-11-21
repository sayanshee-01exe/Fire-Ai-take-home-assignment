from dataclasses import dataclass, field, fields
from fractions import Fraction
from typing import Any, Dict, List

from project_a.core.errors import InvalidInput

MONEY_FIELDS = (
    "gross_salary", "basic_salary", "hra_received", "rent_paid",
    "sec_80c", "sec_80ccd1b", "sec_80d_self", "sec_80d_parents",
    "home_loan_interest",
)
BOOL_FIELDS = ("metro", "parents_senior")


@dataclass(frozen=True)
class TaxInput:
    """Annual amounts in whole rupees. `fy` has no default on purpose (failure LAW-6)."""
    fy: str
    age: int
    gross_salary: int                  # includes HRA and all allowances
    basic_salary: int = 0              # basic + DA: the "salary" used for HRA
    hra_received: int = 0
    rent_paid: int = 0                 # annual
    metro: bool = False                # Delhi, Mumbai, Kolkata, Chennai (FY 2024-25)
    sec_80c: int = 0
    sec_80ccd1b: int = 0
    sec_80d_self: int = 0              # premium for self, spouse, children
    sec_80d_parents: int = 0
    parents_senior: bool = False       # any parent aged 60 or more
    home_loan_interest: int = 0        # self-occupied house

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaxInput":
        known = {f.name for f in fields(cls)}
        errors = ["unknown field: " + k for k in sorted(set(d) - known)]
        errors += ["missing field: " + k for k in ("fy", "age", "gross_salary") if k not in d]
        if errors:
            raise InvalidInput(errors)
        return cls(**d)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_input(inp: TaxInput) -> List[str]:
    errs = []
    if not isinstance(inp.fy, str) or not inp.fy:
        errs.append("fy must be a non-empty string such as '2024-25'")
    if not _is_int(inp.age) or not 0 <= inp.age <= 120:
        errs.append("age must be a whole number between 0 and 120")
    for name in MONEY_FIELDS:
        v = getattr(inp, name)
        if not _is_int(v):
            errs.append(name + " must be a whole number of rupees")
        elif v < 0:
            errs.append(name + " must not be negative")
    for name in BOOL_FIELDS:
        if not isinstance(getattr(inp, name), bool):
            errs.append(name + " must be true or false")
    if not errs:
        if inp.basic_salary > inp.gross_salary:
            errs.append("basic_salary cannot exceed gross_salary")
        if inp.hra_received > inp.gross_salary:
            errs.append("hra_received cannot exceed gross_salary")
    return errs


def _amount(x):
    x = Fraction(x)
    return int(x) if x.denominator == 1 else "%d/%d" % (x.numerator, x.denominator)


@dataclass
class TraceStep:
    step: str
    amount: Fraction
    clause: str

    def to_dict(self):
        return {"step": self.step, "amount": _amount(self.amount), "clause": self.clause}


@dataclass
class RegimeResult:
    regime: str
    total_income: int
    tax: int                           # payable, rounded per C-288B
    trace: List[TraceStep] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "regime": self.regime, "total_income": self.total_income, "tax": self.tax,
            "trace": [t.to_dict() for t in self.trace], "notes": list(self.notes),
        }


@dataclass
class TaxResult:
    old: RegimeResult
    new: RegimeResult
    best: str                          # "old" or "new"
    saving: int
    reason: str
    rule_version: str
    engine: str
    assumptions: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "old": self.old.to_dict(), "new": self.new.to_dict(), "best": self.best,
            "saving": self.saving, "reason": self.reason, "rule_version": self.rule_version,
            "engine": self.engine, "assumptions": list(self.assumptions),
        }
