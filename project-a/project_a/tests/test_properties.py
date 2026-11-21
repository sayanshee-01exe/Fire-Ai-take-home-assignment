"""Property tests: no expected values needed. They catch missing marginal relief anywhere.

Known exception (see FAILURES.md F-001): the OLD regime has no marginal relief at the 5 lakh
rebate limit, so tax really does jump there. That cliff is asserted separately below, and the
no-cliff windows never include it.
"""
import random

import pytest

from project_a.core.schema import TaxInput
from project_a.engines.hand_coded import HandCodedEngine

FY = "2024-25"
ENGINE = HandCodedEngine()


def _taxes(**kw):
    kw.setdefault("age", 30)
    r = ENGINE.calculate(TaxInput(fy=FY, **kw))
    return r.old.tax, r.new.tax


# (start gross, stop gross, step). Windows sit on the new-regime 7 lakh rebate edge and on every
# surcharge threshold, and extend past the end of each marginal-relief zone.
WINDOWS = [
    (760_000, 810_000, 100),
    (4_900_000, 5_700_000, 10_000),
    (9_900_000, 11_300_000, 10_000),
    (19_900_000, 22_500_000, 10_000),
    (49_900_000, 62_500_000, 10_000),
]


@pytest.mark.parametrize("start,stop,step", WINDOWS)
def test_tax_is_monotone_and_has_no_cliff(start, stop, step):
    previous = None
    for gross in range(start, stop + 1, step):
        old, new = _taxes(gross_salary=gross)
        if previous is not None:
            g0, old0, new0 = previous
            delta = gross - g0
            for name, before, after in (("old", old0, old), ("new", new0, new)):
                assert after >= before, (name, g0, gross, before, after)
                # raising income by d may raise tax by at most d plus 4% cess (+ rounding slack)
                assert after - before <= 1.04 * delta + 30, (name, g0, gross, before, after)
        previous = (gross, old, new)


def test_old_regime_has_a_real_cliff_at_5_lakh():
    # Documented behaviour, to confirm on the official calculator (R1).
    assert _taxes(gross_salary=550_000)[0] == 0
    assert _taxes(gross_salary=550_010)[0] == 13_000


def test_new_regime_ignores_old_only_items():
    rng = random.Random(1)
    for _ in range(150):
        gross = rng.randint(100_000, 30_000_000)
        base = dict(age=rng.choice([25, 45, 65, 85]), gross_salary=gross)
        extra = dict(basic_salary=gross // 2, hra_received=gross // 10, rent_paid=gross // 8,
                     metro=rng.random() < 0.5, sec_80c=150_000, sec_80ccd1b=50_000,
                     sec_80d_self=25_000, sec_80d_parents=50_000, parents_senior=True,
                     home_loan_interest=200_000)
        assert _taxes(**base)[1] == _taxes(**base, **extra)[1]


@pytest.mark.parametrize("name,bump", [
    ("sec_80c", 10_000), ("sec_80ccd1b", 10_000), ("sec_80d_self", 5_000),
    ("sec_80d_parents", 5_000), ("home_loan_interest", 10_000),
    ("hra_received", 10_000), ("rent_paid", 10_000),
])
def test_old_tax_never_rises_when_a_deduction_rises(name, bump):
    rng = random.Random(7)
    for _ in range(120):
        gross = rng.randint(300_000, 20_000_000)
        base = dict(age=rng.choice([30, 62, 83]), gross_salary=gross, basic_salary=gross // 2,
                    hra_received=gross // 10, rent_paid=gross // 8, metro=rng.random() < 0.5)
        more = dict(base)
        more[name] = more.get(name, 0) + bump
        assert _taxes(**more)[0] <= _taxes(**base)[0], (name, base)


def test_never_negative_and_best_is_consistent():
    rng = random.Random(3)
    for _ in range(300):
        gross = int(10 ** rng.uniform(0, 7.6))
        inp = TaxInput(fy=FY, age=rng.choice([20, 61, 85]), gross_salary=gross,
                       sec_80c=rng.choice([0, 150_000, 400_000]),
                       home_loan_interest=rng.choice([0, 200_000, 500_000]))
        r = ENGINE.calculate(inp)
        assert r.old.tax >= 0 and r.new.tax >= 0
        assert r.old.total_income >= 0 and r.new.total_income >= 0
        assert r.saving == abs(r.old.tax - r.new.tax)
        assert (r.best == "new") == (r.new.tax <= r.old.tax)
