from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass
class TaxInput:
    age: int = 30                       # age on 31 March 2025
    gross_salary: float = 0             # annual, includes HRA and all allowances
    basic_salary: float = 0             # basic + DA (the "salary" for HRA)
    hra_received: float = 0
    rent_paid: float = 0                # annual rent
    metro: bool = False                 # Delhi, Mumbai, Kolkata, Chennai
    sec_80c: float = 0
    sec_80ccd1b: float = 0
    sec_80d_self: float = 0             # premium for self, spouse, children
    sec_80d_parents: float = 0
    parents_senior: bool = False        # any parent aged 60 or more
    home_loan_interest: float = 0       # self-occupied house

    @staticmethod
    def from_dict(d):
        known = TaxInput.__dataclass_fields__.keys()
        return TaxInput(**{k: v for k, v in d.items() if k in known})


@dataclass
class TraceStep:
    step: str
    amount: float
    clause: str


@dataclass
class RegimeResult:
    regime: str
    total_income: int = 0
    tax: int = 0                        # final payable, rounded per C-288B
    trace: List[TraceStep] = field(default_factory=list)


@dataclass
class TaxResult:
    old: RegimeResult = None
    new: RegimeResult = None
    best: str = ""                      # "old" or "new"
    saving: int = 0                     # tax difference between the two regimes
    reason: str = ""

    def to_dict(self):
        return asdict(self)
