# LLM-compiled tax engine: raw compile 1 (UNREVIEWED; generated, not hand-edited)
# model: gemini-3.5-flash-lite   role: compiler   temperature: 0.2   run_index: 0
# prompt_sha256: 924637088322a06ef88f455a069826a5af059df6867bdb7271f9d586464cf530
# rules: RULES_FY2025_26.md sha256 62601fbe20d73cef82a1028accb799c42ccdfb1cef71666781e05641ba86d10f (FY 2025-26)
# written_at: 2026-11-20T20:42:56+00:00   from cache: False   cache_key: 494f39a236953b06fd2a757ae0add3afe76bbea8809f380aafed183a9a0dc668
# tokens: prompt 2944, output 3729 (thinking 0)   latency_s: 7.39
# allow-list: REJECTED (12 violations)

def compute(inp):
    def round_to_10(n):
        rem = n % 10
        if rem >= 5:
            return n + (10 - rem)
        else:
            return n - rem

    def calc_slab_old(income, age):
        tax = 0
        if age >= 80:
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
        return tax

    def calc_slab_new(income):
        tax = 0
        if income > 400000:
            if income <= 800000:
                tax = tax + (income - 400000) * 5 // 100
            elif income <= 1200000:
                tax = tax + 400000 * 5 // 100 + (income - 800000) * 10 // 100
            elif income <= 1600000:
                tax = tax + 400000 * 5 // 100 + 400000 * 10 // 100 + (income - 1200000) * 15 // 100
            elif income <= 2000000:
                tax = tax + 400000 * 5 // 100 + 400000 * 10 // 100 + 400000 * 15 // 100 + (income - 1600000) * 20 // 100
            elif income <= 2400000:
                tax = tax + 400000 * 5 // 100 + 400000 * 10 // 100 + 400000 * 15 // 100 + 400000 * 20 // 100 + (income - 2000000) * 25 // 100
            else:
                tax = tax + 400000 * 5 // 100 + 400000 * 10 // 100 + 400000 * 15 // 100 + 400000 * 20 // 100 + 400000 * 25 // 100 + (income - 2400000) * 30 // 100
        return tax

    def calc_surcharge(tax_after_rebate, income, is_new):
        surcharge = 0
        surcharge_rate = 0
        if income > 50000000:
            surcharge_rate = 37
            if is_new:
                surcharge_rate = 25
        elif income > 20000000:
            surcharge_rate = 25
        elif income > 10000000:
            surcharge_rate = 15
        elif income > 5000000:
            surcharge_rate = 10

        surcharge = tax_after_rebate * surcharge_rate // 100

        # Marginal relief for surcharge
        if income > 50000000:
            base_threshold = 50000000
            base_rate = 25
            base_tax_at_threshold = calc_slab_new(base_threshold) if is_new else calc_slab_old(base_threshold, inp["age"])
            base_surcharge_at_threshold = base_tax_at_threshold * base_rate // 100
            max_surcharge = base_surcharge_at_threshold + (income - base_threshold)
            if surcharge > max_surcharge:
                surcharge = max_surcharge
        elif income > 20000000:
            base_threshold = 20000000
            base_rate = 15
            base_tax_at_threshold = calc_slab_new(base_threshold) if is_new else calc_slab_old(base_threshold, inp["age"])
            base_surcharge_at_threshold = base_tax_at_threshold * base_rate // 100
            max_surcharge = base_surcharge_at_threshold + (income - base_threshold)
            if surcharge > max_surcharge:
                surcharge = max_surcharge
        elif income > 10000000:
            base_threshold = 10000000
            base_rate = 10
            base_tax_at_threshold = calc_slab_new(base_threshold) if is_new else calc_slab_old(base_threshold, inp["age"])
            base_surcharge_at_threshold = base_tax_at_threshold * base_rate // 100
            max_surcharge = base_surcharge_at_threshold + (income - base_threshold)
            if surcharge > max_surcharge:
                surcharge = max_surcharge
        elif income > 5000000:
            base_threshold = 5000000
            base_rate = 0
            base_tax_at_threshold = calc_slab_new(base_threshold) if is_new else calc_slab_old(base_threshold, inp["age"])
            base_surcharge_at_threshold = 0
            max_surcharge = base_surcharge_at_threshold + (income - base_threshold)
            if surcharge > max_surcharge:
                surcharge = max_surcharge

        return surcharge

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
    age = inp["age"]

    # Old regime HRA
    limit_b = basic_salary * 50 // 100 if metro else basic_salary * 40 // 100
    limit_c = rent_paid - (basic_salary * 10 // 100)
    if limit_c < 0:
        limit_c = 0
    hra_exempt = hra_received
    if limit_b < hra_exempt:
        hra_exempt = limit_b
    if limit_c < hra_exempt:
        hra_exempt = limit_c
    if hra_exempt < 0:
        hra_exempt = 0

    salary_after_hra = gross_salary - hra_exempt

    # Standard deduction old
    std_old = 50000
    if salary_after_hra < std_old:
        std_old = salary_after_hra
    after_std_old = salary_after_hra - std_old

    # s24b old
    s24b_old = home_loan_interest
    if s24b_old > 200000:
        s24b_old = 200000
    after_s24b_old = after_std_old - s24b_old
    if after_s24b_old < 0:
        after_s24b_old = 0

    # Chapter VI-A deductions old
    ded_80c = sec_80c
    if ded_80c > 150000:
        ded_80c = 150000

    ded_80ccd1b = sec_80ccd1b
    if ded_80ccd1b > 50000:
        ded_80ccd1b = 50000

    limit_80d_self = 50000 if age >= 60 else 25000
    ded_80d_self = sec_80d_self
    if ded_80d_self > limit_80d_self:
        ded_80d_self = limit_80d_self

    limit_80d_parents = 50000 if parents_senior else 25000
    ded_80d_parents = sec_80d_parents
    if ded_80d_parents > limit_80d_parents:
        ded_80d_parents = limit_80d_parents

    total_via = ded_80c + ded_80ccd1b + ded_80d_self + ded_80d_parents

    old_income_raw = after_s24b_old - total_via
    if old_income_raw < 0:
        old_income_raw = 0
    old_total_income = round_to_10(old_income_raw)

    # New regime income
    std_new = 75000
    new_income_raw = gross_salary - std_new
    if new_income_raw < 0:
        new_income_raw = 0
    new_total_income = round_to_10(new_income_raw)

    # Tax old
    tax_old_slab = calc_slab_old(old_total_income, age)
    rebate_old = 0
    if old_total_income <= 500000:
        rebate_old = tax_old_slab
        if rebate_old > 12500:
            rebate_old = 12500
    tax_old_after_rebate = tax_old_slab - rebate_old
    if tax_old_after_rebate < 0:
        tax_old_after_rebate = 0

    surcharge_old = calc_surcharge(tax_old_after_rebate, old_total_income, False)
    cess_old = (tax_old_after_rebate + surcharge_old) * 4 // 100
    total_tax_old = tax_old_after_rebate + surcharge_old + cess_old
    old_tax = round_to_10(total_tax_old)

    # Tax new
    tax_new_slab = calc_slab_new(new_total_income)
    rebate_new = 0
    if new_total_income <= 1200000:
        rebate_new = tax_new_slab
        if rebate_new > 60000:
            rebate_new = 60000
    tax_new_after_rebate = tax_new_slab - rebate_new
    if tax_new_after_rebate < 0:
        tax_new_after_rebate = 0

    # C-87A-NEW-MR
    if new_total_income > 1200000:
        max_tax = new_total_income - 1200000
        if tax_new_after_rebate > max_tax:
            tax_new_after_rebate = max_tax

    surcharge_new = calc_surcharge(tax_new_after_rebate, new_total_income, True)
    cess_new = (tax_new_after_rebate + surcharge_new) * 4 // 100
    total_tax_new = tax_new_after_rebate + surcharge_new + cess_new
    new_tax = round_to_10(total_tax_new)

    best = "old" if old_tax < new_tax else "new"

    steps = [
        ["C-HRA", hra_exempt],
        ["C-STD-OLD", std_old],
        ["C-24B", s24b_old],
        ["C-80C", ded_80c],
        ["C-80CCD1B", ded_80ccd1b],
        ["C-80D-SELF", ded_80d_self],
        ["C-80D-PARENTS", ded_80d_parents],
        ["C-288A", old_total_income],
        ["C-SLAB-OLD", tax_old_slab],
        ["C-87A-OLD", rebate_old],
        ["C-SUR", surcharge_old],
        ["C-CESS", cess_old],
        ["C-288B", old_tax],
        ["C-STD-NEW", std_new],
        ["C-288A", new_total_income],
        ["C-SLAB-NEW", tax_new_slab],
        ["C-87A-NEW", rebate_new],
        ["C-87A-NEW-MR", tax_new_after_rebate],
        ["C-SUR", surcharge_new],
        ["C-CESS", cess_new],
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
