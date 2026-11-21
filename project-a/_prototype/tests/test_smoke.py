"""Hand-verified smoke cases for the hand-coded calculator.
The full 200-case suite comes next; these catch basic regressions."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from schema import TaxInput
from hand_coded import calculate

# (inputs, expected old tax, expected new tax); worked out by hand from the rule text
CASES = [
    # gross 10L, nothing claimed
    (dict(gross_salary=1_000_000), 106_600, 44_200),
    # gross 10L with 1.5L under 80C: old taxable 8L -> 72,500 + cess
    (dict(gross_salary=1_000_000, sec_80c=150_000), 75_400, 44_200),
    # new regime taxable exactly 7L -> rebate wipes the tax; old: 12,500+45,000=57,500 +4% = 59,800
    (dict(gross_salary=775_000), 59_800, 0),
    # new regime 5k above the 7L limit -> marginal relief caps tax at 5,000 (+4% cess = 5,200)
    # old: taxable 7.3L -> 58,500 +4% = 60,840
    (dict(gross_salary=780_000), 60_840, 5_200),
    # old regime taxable exactly 5L -> 87A rebate wipes the tax; new taxable 4.75L -> rebate too
    (dict(gross_salary=550_000), 0, 0),
]


def test_cases():
    bad = []
    for inp, exp_old, exp_new in CASES:
        res = calculate(TaxInput(**inp))
        if (res.old.tax, res.new.tax) != (exp_old, exp_new):
            bad.append((inp, (res.old.tax, res.new.tax), (exp_old, exp_new)))
    assert not bad, bad


if __name__ == "__main__":
    test_cases()
    print("smoke OK")
