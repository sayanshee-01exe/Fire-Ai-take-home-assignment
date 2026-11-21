# LLM-compiled tax engine: REVIEWED version of raw compile 1 (see REVIEW.md and reviewed.diff)
# base: raw_v1.py, cache_key a734eff90c6e41bb92113bdc666f5e7cd43bca24899d126a5921e5295eea901c
# rules: RULES_FY2024_25.md sha256 8b32902a838e90d8fb6a64282994597c896c1c92275c3659bc29c3df6fe18f89 (FY 2024-25)
# edits: R1 helpers inlined (allow-list), R2 exact HRA, R3 exact surcharge and cess, R4 invented constant removed

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

    # R2: HRA amounts kept exact, in hundredths of a rupee (x100), instead of floored with // 100.
    hra_pct = 40
    if metro:
        hra_pct = 50
    cond_a = hra_received * 100
    cond_b = basic_salary * hra_pct
    cond_c = rent_paid * 100 - basic_salary * 10
    if cond_c < 0:
        cond_c = 0
    exempt_hra = cond_a
    if cond_b < exempt_hra:
        exempt_hra = cond_b
    if cond_c < exempt_hra:
        exempt_hra = cond_c
    if exempt_hra < 0:
        exempt_hra = 0

    salary_after_hra = gross_salary * 100 - exempt_hra
    std_old = 50000 * 100
    if std_old > salary_after_hra:
        std_old = salary_after_hra

    home_loan_deduction = home_loan_interest
    if home_loan_deduction > 200000:
        home_loan_deduction = 200000

    income_after_std_old = salary_after_hra - std_old - home_loan_deduction * 100
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
    old_net_income = income_after_std_old - total_vi_a * 100
    if old_net_income < 0:
        old_net_income = 0
    old_net_income = old_net_income // 100          # R2: paise ignored once, on the total
    # R1: round_nearest_10 inlined
    old_total_income = old_net_income
    rem = old_total_income % 10
    if rem >= 5:
        old_total_income = old_total_income + (10 - rem)
    else:
        old_total_income = old_total_income - rem

    std_new = 75000
    new_income_raw = gross_salary - std_new
    if new_income_raw < 0:
        new_income_raw = 0
    new_total_income = new_income_raw
    rem = new_total_income % 10
    if rem >= 5:
        new_total_income = new_total_income + (10 - rem)
    else:
        new_total_income = new_total_income - rem

    # R1: the helpers calc_slab_old, calc_slab_new, get_surcharge_rate and compute_surcharge_and_mr
    # are inlined: one pass of this loop per regime, with their bodies kept as they were.
    results = {}
    for regime in ["old", "new"]:
        is_new = regime == "new"
        ti = old_total_income
        if is_new:
            ti = new_total_income

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

        slab_taxes = []
        for income in [ti, threshold]:
            tax = 0
            if is_new:
                if income > 300000:
                    if income <= 700000:
                        tax = tax + (income - 300000) * 5 // 100
                    elif income <= 1000000:
                        tax = tax + 400000 * 5 // 100 + (income - 700000) * 10 // 100
                    elif income <= 1200000:
                        tax = tax + 400000 * 5 // 100 + 300000 * 10 // 100 + (income - 1000000) * 15 // 100
                    elif income <= 1500000:
                        tax = tax + 400000 * 5 // 100 + 300000 * 10 // 100 + 200000 * 15 // 100 + (income - 1200000) * 20 // 100
                    else:
                        tax = tax + 400000 * 5 // 100 + 300000 * 10 // 100 + 200000 * 15 // 100 + 300000 * 20 // 100 + (income - 1500000) * 30 // 100
            elif age >= 80:
                if income > 500000:
                    if income <= 1000000:
                        tax = tax + (income - 500000) * 20 // 100
                    else:
                        tax = tax + 500000 * 20 // 100 + (income - 1000000) * 30 // 100
            elif age >= 60:
                if income > 300000:
                    if income <= 500000:
                        tax = tax + (income - 300000) * 5 // 100
                    elif income <= 1000000:
                        tax = tax + 200000 * 5 // 100 + (income - 500000) * 20 // 100
                    else:
                        tax = tax + 200000 * 5 // 100 + 500000 * 20 // 100 + (income - 1000000) * 30 // 100
            else:
                if income > 250000:
                    if income <= 500000:
                        tax = tax + (income - 250000) * 5 // 100
                    elif income <= 1000000:
                        tax = tax + 250000 * 5 // 100 + (income - 500000) * 20 // 100
                    else:
                        tax = tax + 250000 * 5 // 100 + 500000 * 20 // 100 + (income - 1000000) * 30 // 100
            slab_taxes = slab_taxes + [tax]
        tax_raw = slab_taxes[0]
        tax_thresh = slab_taxes[1]

        rebate = 0
        mr_applied = False
        if not is_new:
            if ti <= 500000:
                rebate = tax_raw
                if rebate > 12500:
                    rebate = 12500
        elif ti <= 700000:
            rebate = tax_raw
            if rebate > 25000:
                rebate = 25000
        else:
            excess = ti - 700000                     # R4: no invented upper limit (was ti <= 727770)
            if tax_raw > excess:
                rebate = tax_raw - excess
                mr_applied = True
        tax_after_rebate = tax_raw - rebate
        if tax_after_rebate < 0:
            tax_after_rebate = 0

        # R3: surcharge, its marginal relief and cess kept exact (x100, then x10000), rounded once.
        total_tax_sur = tax_after_rebate * (100 + rate)
        if threshold > 0:
            max_allowed = tax_thresh * (100 + lower_rate) + (ti - threshold) * 100
            if total_tax_sur > max_allowed:
                total_tax_sur = max_allowed
        final_exact = total_tax_sur * 104
        final = final_exact // 10000                 # paise ignored once, on the final figure
        rem = final % 10
        if rem >= 5:
            final = final + (10 - rem)
        else:
            final = final - rem
        results[regime] = [tax_raw, rebate, mr_applied, total_tax_sur / 100, final_exact / 10000, final]

    old_tax_raw = results["old"][0]
    old_rebate = results["old"][1]
    old_tax_with_sur = results["old"][3]
    old_final = results["old"][4]
    old_tax = results["old"][5]
    new_tax_raw = results["new"][0]
    new_rebate = results["new"][1]
    new_mr_applied = results["new"][2]
    new_tax_with_sur = results["new"][3]
    new_final = results["new"][4]
    new_tax = results["new"][5]

    best = "new"
    if old_tax < new_tax:
        best = "old"

    steps = []
    steps = steps + [["C-HRA", exempt_hra / 100]]
    steps = steps + [["C-STD-OLD", std_old / 100]]
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
