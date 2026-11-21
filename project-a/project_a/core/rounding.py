from fractions import Fraction
from math import floor


def round_statutory(x, unit=10):
    """Sections 288A / 288B: ignore paise, then round to the nearest `unit`.

    A last figure of half the unit or more (5 or more for unit 10) rounds up.
    Input must be >= 0. unit=1 only drops the paise.
    Assumption D5 in PLAN.md: the official calculator follows this. Verify (risk R1).
    """
    x = Fraction(x)
    if x < 0:
        raise ValueError("round_statutory expects a non-negative amount")
    rupees = floor(x)
    if unit <= 1:
        return rupees
    q, r = divmod(rupees, unit)
    if 2 * r >= unit:
        q += 1
    return q * unit
