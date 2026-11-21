# LLM-compiled tax engine: raw compile 2 (UNREVIEWED; generated, not hand-edited)
# model: gemini-3.5-flash-lite   role: compiler   temperature: 0.2   run_index: 1
# prompt_sha256: 27200f8c63a06ea490236d4074ad3e10c22addc1a217e77f161b783e7368f13d
# rules: RULES_FY2024_25.md sha256 8b32902a838e90d8fb6a64282994597c896c1c92275c3659bc29c3df6fe18f89 (FY 2024-25)
# written_at: 2026-11-20T19:04:46+00:00   from cache: True   cache_key: 8143b9daca9c3aa221d4d14199067232e0c56730c9303f5cef8ebff7120b3621
# tokens: prompt 2424, output 5696 (thinking 0)   latency_s: 12.45
# allow-list: REJECTED (5 violations)

def compute(inp):
    def round_nearest_10(val):
        rem = val % 10
        if rem >= 5:
            return val + (10 - rem)
        else:
            return val - rem

    age = inp["age"]
    gross_salary = inp["gross_salary"]
    basic_salary = inp["basic_salary"]
    hra_received = inp["hra_received"]
    rent_paid = inp["rent_paid"]
    metro = inp["metro"]
    sec_80c = inp["sec_80c"]
    sec_80ccd1b = inp["sec_80ccd1b"]
    sec_80d_self = inp["sec_80d_self"]
    sec_80d_parents = inp["sec_80d_parents"]
    parents_senior = inp["parents_senior"]
    home_loan_interest = inp["home_loan_interest"]

    if metro:
        metro_rate_val = 50
    else:
        metro_rate_val = 40

    hra_b = (basic_salary * metro_rate_val) // 100
    hra_c = rent_paid - ((basic_salary * 10) // 100)
    if hra_c < 0:
        hra_c = 0

    if hra_received <= hra_b:
        min_hra1 = hra_received
    else:
        min_hra1 = hra_b

    if min_hra1 <= hra_c:
        exempt_hra = min_hra1
    else:
        exempt_hra = hra_c

    salary_after_hra = gross_salary - exempt_hra

    if salary_after_hra <= 50000:
        std_old = salary_after_hra
    else:
        std_old = 50000

    if home_loan_interest <= 200000:
        ded_24b = home_loan_interest
    else:
        ded_24b = 200000

    income_after_std_24b = salary_after_hra - std_old - ded_24b

    if sec_80c <= 150000:
        d_80c = sec_80c
    else:
        d_80c = 150000

    if sec_80ccd1b <= 50000:
        d_80ccd1b = sec_80ccd1b
    else:
        d_80ccd1b = 50000

    if age >= 60:
        limit_80d_self = 50000
    else:
        limit_80d_self = 25000

    if sec_80d_self <= limit_80d_self:
        d_80d_self = sec_80d_self
    else:
        d_80d_self = limit_80d_self

    if parents_senior:
        limit_80d_parents = 50000
    else:
        limit_80d_parents = 25000

    if sec_80d_parents <= limit_80d_parents:
        d_80d_parents = sec_80d_parents
    else:
        d_80d_parents = limit_80d_parents

    total_vi_a = d_80c + d_80ccd1b + d_80d_self + d_80d_parents
    old_income_raw = income_after_std_24b - total_vi_a
    if old_income_raw < 0:
        old_income_raw = 0

    old_total_income = round_nearest_10(old_income_raw)

    if age >= 80:
        slab_1_limit = 500000
        slab_2_limit = 1000000
    elif age >= 60:
        slab_1_limit = 300000
        slab_2_limit = 500000
        slab_3_limit = 1000000
    else:
        slab_1_limit = 250000
        slab_2_limit = 500000
        slab_3_limit = 1000000

    old_slab_tax = 0
    if age >= 80:
        if old_total_income > 500000:
            if old_total_income <= 1000000:
                old_slab_tax = (old_total_income - 500000) * 20 // 100
            else:
                old_slab_tax = (500000 * 20 // 100) + (old_total_income - 1000000) * 30 // 100
    elif age >= 60:
        if old_total_income > 300000:
            if old_total_income <= 500000:
                old_slab_tax = (old_total_income - 300000) * 5 // 100
            elif old_total_income <= 1000000:
                old_slab_tax = (200000 * 5 // 100) + (old_total_income - 500000) * 20 // 100
            else:
                old_slab_tax = (200000 * 5 // 100) + (500000 * 20 // 100) + (old_total_income - 1000000) * 30 // 100
    else:
        if old_total_income > 250000:
            if old_total_income <= 500000:
                old_slab_tax = (old_total_income - 250000) * 5 // 100
            elif old_total_income <= 1000000:
                old_slab_tax = (250000 * 5 // 100) + (old_total_income - 500000) * 20 // 100
            else:
                old_slab_tax = (250000 * 5 // 100) + (500000 * 20 // 100) + (old_total_income - 1000000) * 30 // 100

    old_rebate = 0
    if old_total_income <= 500000:
        if old_slab_tax <= 12500:
            old_rebate = old_slab_tax
        else:
            old_rebate = 12500

    old_tax_after_rebate = old_slab_tax - old_rebate

    old_surcharge = 0
    if old_total_income > 5000000 and old_total_income <= 10000000:
        old_surcharge = old_tax_after_rebate * 10 // 100
    elif old_total_income > 10000000 and old_total_income <= 20000000:
        old_surcharge = old_tax_after_rebate * 15 // 100
    elif old_total_income > 20000000 and old_total_income <= 50000000:
        old_surcharge = old_tax_after_rebate * 25 // 100
    elif old_total_income > 50000000:
        old_surcharge = old_tax_after_rebate * 37 // 100

    old_tax_with_surcharge = old_tax_after_rebate + old_surcharge

    old_mr_surcharge = 0
    if old_total_income > 5000000 and old_total_income <= 10000000:
        base_inc_old = 5000000
        base_slab_old = 0
        if age >= 80:
            base_slab_old = (5000000 - 500000) * 20 // 100
        elif age >= 60:
            base_slab_old = (200000 * 5 // 100) + (4500000 * 20 // 100)
        else:
            base_slab_old = (250000 * 5 // 100) + (4500000 * 20 // 100)
        base_tax_old = base_slab_old
        base_sur_old = 0
        limit_tax_old = base_tax_old + base_sur_old + (old_total_income - 5000000)
        if old_tax_with_surcharge > limit_tax_old:
            old_mr_surcharge = limit_tax_old - old_tax_after_rebate
            old_tax_with_surcharge = limit_tax_old
    elif old_total_income > 10000000 and old_total_income <= 20000000:
        base_inc_old = 10000000
        base_slab_old = 0
        if age >= 80:
            base_slab_old = (500000 * 20 // 100) + (9500000 * 30 // 100)
        elif age >= 60:
            base_slab_old = (200000 * 5 // 100) + (500000 * 20 // 100) + (9500000 * 30 // 100)
        else:
            base_slab_old = (250000 * 5 // 100) + (500000 * 20 // 100) + (9500000 * 30 // 100)
        base_tax_old = base_slab_old
        base_sur_old = base_tax_old * 10 // 100
        limit_tax_old = base_tax_old + base_sur_old + (old_total_income - 10000000)
        if old_tax_with_surcharge > limit_tax_old:
            old_mr_surcharge = limit_tax_old - old_tax_after_rebate
            old_tax_with_surcharge = limit_tax_old
    elif old_total_income > 20000000 and old_total_income <= 50000000:
        base_inc_old = 20000000
        base_slab_old = 0
        if age >= 80:
            base_slab_old = (500000 * 20 // 100) + (19500000 * 30 // 100)
        elif age >= 60:
            base_slab_old = (200000 * 5 // 100) + (500000 * 20 // 100) + (19500000 * 30 // 100)
        else:
            base_slab_old = (250000 * 5 // 100) + (500000 * 20 // 100) + (19500000 * 30 // 100)
        base_tax_old = base_slab_old
        base_sur_old = base_tax_old * 15 // 100
        limit_tax_old = base_tax_old + base_sur_old + (old_total_income - 20000000)
        if old_tax_with_surcharge > limit_tax_old:
            old_mr_surcharge = limit_tax_old - old_tax_after_rebate
            old_tax_with_surcharge = limit_tax_old
    elif old_total_income > 50000000:
        base_inc_old = 50000000
        base_slab_old = 0
        if age >= 80:
            base_slab_old = (500000 * 20 // 100) + (49500000 * 30 // 100)
        elif age >= 60:
            base_slab_old = (200000 * 5 // 100) + (500000 * 20 // 100) + (49500000 * 30 // 100)
        else:
            base_slab_old = (250000 * 5 // 100) + (500000 * 20 // 100) + (49500000 * 30 // 100)
        base_tax_old = base_slab_old
        base_sur_old = base_tax_old * 25 // 100
        limit_tax_old = base_tax_old + base_sur_old + (old_total_income - 50000000)
        if old_tax_with_surcharge > limit_tax_old:
            old_mr_surcharge = limit_tax_old - old_tax_after_rebate
            old_tax_with_surcharge = limit_tax_old

    old_cess = (old_tax_with_surcharge * 4) // 100
    old_tax_final_raw = old_tax_with_surcharge + old_cess
    old_tax = round_nearest_10(old_tax_final_raw)

    if gross_salary <= 75000:
        std_new = gross_salary
    else:
        std_new = 75000

    new_income_raw = gross_salary - std_new
    if new_income_raw < 0:
        new_income_raw = 0

    new_total_income = round_nearest_10(new_income_raw)

    new_slab_tax = 0
    if new_total_income > 300000:
        if new_total_income <= 700000:
            new_slab_tax = (new_total_income - 300000) * 5 // 100
        elif new_total_income <= 1000000:
            new_slab_tax = (400000 * 5 // 100) + (new_total_income - 700000) * 10 // 100
        elif new_total_income <= 1200000:
            new_slab_tax = (400000 * 5 // 100) + (300000 * 10 // 100) + (new_total_income - 1000000) * 15 // 100
        elif new_total_income <= 1500000:
            new_slab_tax = (400000 * 5 // 100) + (300000 * 10 // 100) + (200000 * 15 // 100) + (new_total_income - 1200000) * 20 // 100
        else:
            new_slab_tax = (400000 * 5 // 100) + (300000 * 10 // 100) + (200000 * 15 // 100) + (300000 * 20 // 100) + (new_total_income - 1500000) * 30 // 100

    new_rebate = 0
    if new_total_income <= 700000:
        if new_slab_tax <= 25000:
            new_rebate = new_slab_tax
        else:
            new_rebate = 25000

    new_tax_after_rebate = new_slab_tax - new_rebate

    new_mr_87a = 0
    if new_total_income > 700000 and new_total_income <= 727770:
        excess_income = new_total_income - 700000
        if new_tax_after_rebate > excess_income:
            new_mr_87a = new_tax_after_rebate - excess_income
            new_tax_after_rebate = excess_income

    new_surcharge = 0
    if new_total_income > 5000000 and new_total_income <= 10000000:
        new_surcharge = new_tax_after_rebate * 10 // 100
    elif new_total_income > 10000000 and new_total_income <= 20000000:
        new_surcharge = new_tax_after_rebate * 15 // 100
    elif new_total_income > 20000000:
        new_surcharge = new_tax_after_rebate * 25 // 100

    new_tax_with_surcharge = new_tax_after_rebate + new_surcharge

    new_mr_surcharge = 0
    if new_total_income > 5000000 and new_total_income <= 10000000:
        base_tax_new = (400000 * 5 // 100) + (300000 * 10 // 100) + (200000 * 15 // 100) + (300000 * 20 // 100) + (3500000 * 30 // 100)
        base_sur_new = 0
        limit_tax_new = base_tax_new + base_sur_new + (new_total_income - 5000000)
        if new_tax_with_surcharge > limit_tax_new:
            new_mr_surcharge = limit_tax_new - new_tax_after_rebate
            new_tax_with_surcharge = limit_tax_new
    elif new_total_income > 10000000 and new_total_income <= 20000000:
        base_tax_new = (400000 * 5 // 100) + (300000 * 10 // 100) + (200000 * 15 // 100) + (300000 * 20 // 100) + (8500000 * 30 // 100)
        base_sur_new = base_tax_new * 10 // 100
        limit_tax_new = base_tax_new + base_sur_new + (new_total_income - 10000000)
        if new_tax_with_surcharge > limit_tax_new:
            new_mr_surcharge = limit_tax_new - new_tax_after_rebate
            new_tax_with_surcharge = limit_tax_new
    elif new_total_income > 20000000:
        base_tax_new = (400000 * 5 // 100) + (300000 * 10 // 100) + (200000 * 15 // 100) + (300000 * 20 // 100) + (18500000 * 30 // 100)
        base_sur_new = base_tax_new * 15 // 100
        limit_tax_new = base_tax_new + base_sur_new + (new_total_income - 20000000)
        if new_tax_with_surcharge > limit_tax_new:
            new_mr_surcharge = limit_tax_new - new_tax_after_rebate
            new_tax_with_surcharge = limit_tax_new

    new_cess = (new_tax_with_surcharge * 4) // 100
    new_tax_final_raw = new_tax_with_surcharge + new_cess
    new_tax = round_nearest_10(new_tax_final_raw)

    if old_tax <= new_tax:
        best = "old"
    else:
        best = "new"

    steps = [
        ["C-HRA", exempt_hra],
        ["C-STD-OLD", std_old],
        ["C-24B", ded_24b],
        ["C-80C", d_80c],
        ["C-80CCD1B", d_80ccd1b],
        ["C-80D-SELF", d_80d_self],
        ["C-80D-PARENTS", d_80d_parents],
        ["C-288A", old_total_income],
        ["C-SLAB-OLD", old_slab_tax],
        ["C-87A-OLD", old_rebate],
        ["C-SUR", old_surcharge],
        ["C-SUR-MR", old_mr_surcharge],
        ["C-CESS", old_cess],
        ["C-288B", old_tax],
        ["C-STD-NEW", std_new],
        ["C-288A", new_total_income],
        ["C-SLAB-NEW", new_slab_tax],
        ["C-87A-NEW", new_rebate],
        ["C-87A-NEW-MR", new_mr_87a],
        ["C-SUR", new_surcharge],
        ["C-SUR-MR", new_mr_surcharge],
        ["C-CESS", new_cess],
        ["C-288B", new_tax]
    ]

    return {
        "old_total_income": old_total_income,
        "old_tax": old_tax,
        "new_total_income": new_total_income,
        "new_tax": new_tax,
        "best": best,
        "steps": steps
    }
