# LLM-compiled tax engine: raw compile 1 (UNREVIEWED; generated, not hand-edited)
# model: gemini-3.5-flash-lite   role: compiler   temperature: 0.2   run_index: 0
# prompt_sha256: 27200f8c63a06ea490236d4074ad3e10c22addc1a217e77f161b783e7368f13d
# rules: RULES_FY2024_25.md sha256 8b32902a838e90d8fb6a64282994597c896c1c92275c3659bc29c3df6fe18f89 (FY 2024-25)
# written_at: 2026-11-20T19:04:46+00:00   from cache: True   cache_key: a734eff90c6e41bb92113bdc666f5e7cd43bca24899d126a5921e5295eea901c
# tokens: prompt 2424, output 3576 (thinking 0)   latency_s: 8.62
# allow-list: REJECTED (13 violations)

def round_nearest_10(val):
    rem = val % 10
    if rem >= 5:
        return val + (10 - rem)
    else:
        return val - rem

def compute(inp):
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

    hra_pct = 40
    if metro:
        hra_pct = 50
    cond_a = hra_received
    cond_b = (basic_salary * hra_pct) // 100
    cond_c = rent_paid - ((basic_salary * 10) // 100)
    if cond_c < 0:
        cond_c = 0
    exempt_hra = cond_a
    if cond_b < exempt_hra:
        exempt_hra = cond_b
    if cond_c < exempt_hra:
        exempt_hra = cond_c
    if exempt_hra < 0:
        exempt_hra = 0

    salary_after_hra = gross_salary - exempt_hra
    std_old = 50000
    if std_old > salary_after_hra:
        std_old = salary_after_hra
    
    home_loan_deduction = home_loan_interest
    if home_loan_deduction > 200000:
        home_loan_deduction = 200000

    income_after_std_old = salary_after_hra - std_old - home_loan_deduction
    if income_after_std_old < 0:
        income_after_std_old = 0

    c_80c_val = sec_80c
    if c_80c_val > 150000:
        c_80c_val = 150000
    c_80ccd1b_val = sec_80ccd1b
    if c_80ccd1b_val > 50000:
        c_80ccd1b_val = 50000
    
    limit_self = 25000
    if age >= 60:
        limit_self = 50000
    c_80d_self_val = sec_80d_self
    if c_80d_self_val > limit_self:
        c_80d_self_val = limit_self

    limit_parents = 25000
    if parents_senior:
        limit_parents = 50000
    c_80d_parents_val = sec_80d_parents
    if c_80d_parents_val > limit_parents:
        c_80d_parents_val = limit_parents

    total_vi_a = c_80c_val + c_80ccd1b_val + c_80d_self_val + c_80d_parents_val
    old_net_income = income_after_std_old - total_vi_a
    if old_net_income < 0:
        old_net_income = 0
    old_total_income = round_nearest_10(old_net_income)

    std_new = 75000
    new_income_raw = gross_salary - std_new
    if new_income_raw < 0:
        new_income_raw = 0
    new_total_income = round_nearest_10(new_income_raw)

    def calc_slab_old(ti):
        tax = 0
        if age >= 80:
            if ti > 500000:
                if ti <= 1000000:
                    tax = tax + (ti - 500000) * 20 // 100
                else:
                    tax = tax + 500000 * 20 // 100 + (ti - 1000000) * 30 // 100
        elif age >= 60:
            if ti > 300000:
                if ti <= 500000:
                    tax = tax + (ti - 300000) * 5 // 100
                elif ti <= 1000000:
                    tax = tax + 200000 * 5 // 100 + (ti - 500000) * 20 // 100
                else:
                    tax = tax + 200000 * 5 // 100 + 500000 * 20 // 100 + (ti - 1000000) * 30 // 100
        else:
            if ti > 250000:
                if ti <= 500000:
                    tax = tax + (ti - 250000) * 5 // 100
                elif ti <= 1000000:
                    tax = tax + 250000 * 5 // 100 + (ti - 500000) * 20 // 100
                else:
                    tax = tax + 250000 * 5 // 100 + 500000 * 20 // 100 + (ti - 1000000) * 30 // 100
        return tax

    def calc_slab_new(ti):
        tax = 0
        if ti > 300000:
            if ti <= 700000:
                tax = tax + (ti - 300000) * 5 // 100
            elif ti <= 1000000:
                tax = tax + 400000 * 5 // 100 + (ti - 700000) * 10 // 100
            elif ti <= 1200000:
                tax = tax + 400000 * 5 // 100 + 300000 * 10 // 100 + (ti - 1000000) * 15 // 100
            elif ti <= 1500000:
                tax = tax + 400000 * 5 // 100 + 300000 * 10 // 100 + 200000 * 15 // 100 + (ti - 1200000) * 20 // 100
            else:
                tax = tax + 400000 * 5 // 100 + 300000 * 10 // 100 + 200000 * 15 // 100 + 300000 * 20 // 100 + (ti - 1500000) * 30 // 100
        return tax

    def get_surcharge_rate(ti, is_new):
        rate = 0
        if ti > 5000000 and ti <= 10000000:
            rate = 10
        elif ti > 10000000 and ti <= 20000000:
            rate = 15
        elif ti > 20000000 and ti <= 50000000:
            rate = 25
        elif ti > 50000000:
            if is_new:
                rate = 25
            else:
                rate = 37
        return rate

    def compute_surcharge_and_mr(ti, tax_after_rebate, is_new):
        rate = get_surcharge_rate(ti, is_new)
        surcharge = (tax_after_rebate * rate) // 100
        total_tax_sur = tax_after_rebate + surcharge

        threshold = 0
        lower_rate = 0
        if ti > 5000000 and ti <= 10000000:
            threshold = 5000000
            lower_rate = 0
        elif ti > 10000000 and ti <= 20000000:
            threshold = 10000000
            lower_rate = 10
        elif ti > 20000000 and ti <= 50000000:
            threshold = 20000000
            lower_rate = 15
        elif ti > 50000000:
            threshold = 50000000
            if is_new:
                lower_rate = 25
            else:
                lower_rate = 25

        if threshold > 0:
            if is_new:
                tax_thresh = calc_slab_new(threshold)
            else:
                tax_thresh = calc_slab_old(threshold)
            sur_thresh = (tax_thresh * lower_rate) // 100
            total_thresh = tax_thresh + sur_thresh
            max_allowed = total_thresh + (ti - threshold)
            if total_tax_sur > max_allowed:
                total_tax_sur = max_allowed
        return total_tax_sur

    old_tax_raw = calc_slab_old(old_total_income)
    old_rebate = 0
    if old_total_income <= 500000:
        old_rebate = old_tax_raw
        if old_rebate > 12500:
            old_rebate = 12500
    old_tax_after_rebate = old_tax_raw - old_rebate
    if old_tax_after_rebate < 0:
        old_tax_after_rebate = 0

    old_tax_with_sur = compute_surcharge_and_mr(old_total_income, old_tax_after_rebate, False)
    old_cess = (old_tax_with_sur * 4) // 100
    old_final = old_tax_with_sur + old_cess
    old_tax = round_nearest_10(old_final)

    new_tax_raw = calc_slab_new(new_total_income)
    new_rebate = 0
    new_mr_applied = False
    if new_total_income <= 700000:
        new_rebate = new_tax_raw
        if new_rebate > 25000:
            new_rebate = 25000
    else:
        if new_total_income <= 727770:
            excess = new_total_income - 700000
            if new_tax_raw > excess:
                new_rebate = new_tax_raw - excess
                new_mr_applied = True

    new_tax_after_rebate = new_tax_raw - new_rebate
    if new_tax_after_rebate < 0:
        new_tax_after_rebate = 0

    new_tax_with_sur = compute_surcharge_and_mr(new_total_income, new_tax_after_rebate, True)
    new_cess = (new_tax_with_sur * 4) // 100
    new_final = new_tax_with_sur + new_cess
    new_tax = round_nearest_10(new_final)

    best = "new"
    if old_tax < new_tax:
        best = "old"

    steps = []
    steps = steps + [["C-HRA", exempt_hra]]
    steps = steps + [["C-STD-OLD", std_old]]
    steps = steps + [["C-24B", home_loan_deduction]]
    steps = steps + [["C-80C", c_80c_val]]
    steps = steps + [["C-80CCD1B", c_80ccd1b_val]]
    steps = steps + [["C-80D-SELF", c_80d_self_val]]
    steps = steps + [["C-80D-PARENTS", c_80d_parents_val]]
    steps = steps + [["C-288A", old_total_income]]
    steps = steps + [["C-SLAB-OLD", old_tax_raw]]
    if old_total_income <= 500000:
        steps = steps + [["C-87A-OLD", old_rebate]]
    steps = steps + [["C-SUR", old_tax_with_sur]]
    steps = steps + [["C-CESS", old_final]]
    steps = steps + [["C-288B", old_tax]]

    steps = steps + [["C-STD-NEW", std_new]]
    steps = steps + [["C-288A", new_total_income]]
    steps = steps + [["C-SLAB-NEW", new_tax_raw]]
    if new_total_income <= 700000:
        steps = steps + [["C-87A-NEW", new_rebate]]
    elif new_mr_applied:
        steps = steps + [["C-87A-NEW-MR", new_rebate]]
    steps = steps + [["C-SUR", new_tax_with_sur]]
    steps = steps + [["C-CESS", new_final]]
    steps = steps + [["C-288B", new_tax]]

    return {
        "old_total_income": old_total_income,
        "old_tax": old_tax,
        "new_total_income": new_total_income,
        "new_tax": new_tax,
        "best": best,
        "steps": steps
    }
