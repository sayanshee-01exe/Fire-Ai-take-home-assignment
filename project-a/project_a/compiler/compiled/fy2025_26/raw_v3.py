# LLM-compiled tax engine: raw compile 3 (UNREVIEWED; generated, not hand-edited)
# model: gemini-3.5-flash-lite   role: compiler   temperature: 0.2   run_index: 2
# prompt_sha256: 924637088322a06ef88f455a069826a5af059df6867bdb7271f9d586464cf530
# rules: RULES_FY2025_26.md sha256 62601fbe20d73cef82a1028accb799c42ccdfb1cef71666781e05641ba86d10f (FY 2025-26)
# written_at: 2026-11-20T20:43:13+00:00   from cache: False   cache_key: 45913fca156f0a155b6a70975f05702e4482dd7ff284bbb53d71e224a535a549
# tokens: prompt 2944, output 3904 (thinking 0)   latency_s: 7.52
# allow-list: REJECTED (13 violations)

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
                tax = tax + (income - 500000) * 20 // 100
        elif age >= 60:
            if income > 300000 and income <= 500000:
                tax = (income - 300000) * 5 // 100
            elif income > 500000:
                tax = (500000 - 300000) * 5 // 100 + (income - 500000) * 20 // 100
        else:
            if income > 250000 and income <= 500000:
                tax = (income - 250000) * 5 // 100
            elif income > 500000 and income <= 1000000:
                tax = (500000 - 250000) * 5 // 100 + (income - 500000) * 20 // 100
            elif income > 1000000:
                tax = (500000 - 250000) * 5 // 100 + (1000000 - 500000) * 20 // 100 + (income - 1000000) * 30 // 100
        return tax

    def calc_slab_new(income):
        tax = 0
        if income > 400000 and income <= 800000:
            tax = (income - 400000) * 5 // 100
        elif income > 800000 and income <= 1200000:
            tax = (800000 - 400000) * 5 // 100 + (income - 800000) * 10 // 100
        elif income > 1200000 and income <= 1600000:
            tax = (800000 - 400000) * 5 // 100 + (1200000 - 800000) * 10 // 100 + (income - 1200000) * 15 // 100
        elif income > 1600000 and income <= 2000000:
            tax = (800000 - 400000) * 5 // 100 + (1200000 - 800000) * 10 // 100 + (1600000 - 1200000) * 15 // 100 + (income - 1600000) * 20 // 100
        elif income > 2000000 and income <= 2400000:
            tax = (800000 - 400000) * 5 // 100 + (1200000 - 800000) * 10 // 100 + (1600000 - 1200000) * 15 // 100 + (2000000 - 1600000) * 20 // 100 + (income - 2000000) * 25 // 100
        elif income > 2400000:
            tax = (800000 - 400000) * 5 // 100 + (1200000 - 800000) * 10 // 100 + (1600000 - 1200000) * 15 // 100 + (2000000 - 1600000) * 20 // 100 + (2400000 - 2000000) * 25 // 100 + (income - 2400000) * 30 // 100
        return tax

    def get_surcharge_rate(income, regime):
        rate = 0
        if income > 5000000 and income <= 10000000:
            rate = 10
        elif income > 10000000 and income <= 20000000:
            rate = 15
        elif income > 20000000 and income <= 50000000:
            rate = 25
        elif income > 50000000:
            if regime == "old":
                rate = 37
            else:
                rate = 25
        return rate

    def calc_surcharge_with_mr(income, tax_after_rebate, regime, age):
        sur_rate = get_surcharge_rate(income, regime)
        if sur_rate == 0:
            return tax_after_rebate

        raw_sur = tax_after_rebate * sur_rate // 100
        total_tax_sur = tax_after_rebate + raw_sur

        thresh = 0
        thresh_tax_base = 0
        thresh_sur_rate = 0

        if income > 5000000 and income <= 10000000:
            thresh = 5000000
            thresh_sur_rate = 0
        elif income > 10000000 and income <= 20000000:
            thresh = 10000000
            thresh_sur_rate = 10
        elif income > 20000000 and income <= 50000000:
            thresh = 20000000
            thresh_sur_rate = 15
        elif income > 50000000:
            thresh = 50000000
            thresh_sur_rate = 25

        if regime == "old":
            t_tax = calc_slab_old(thresh, age)
            if thresh <= 500000:
                t_rebate = min(t_tax, 12500)
                t_tax_after_rebate = t_tax - t_rebate
            else:
                t_tax_after_rebate = t_tax
        else:
            t_tax = calc_slab_new(thresh)
            if thresh <= 1200000:
                t_rebate = min(t_tax, 60000)
                t_tax_after_rebate = t_tax - t_rebate
            else:
                if thresh > 1200000 and thresh <= 1264860:
                    t_mr_tax = thresh - 1200000
                    t_tax_after_rebate = min(t_tax - 60000, t_mr_tax)
                else:
                    t_tax_after_rebate = t_tax

        thresh_sur = t_tax_after_rebate * thresh_sur_rate // 100
        thresh_total = t_tax_after_rebate + thresh_sur
        allowed_total = thresh_total + (income - thresh)

        if total_tax_sur > allowed_total:
            if allowed_total < tax_after_rebate:
                return tax_after_rebate
            return allowed_total - tax_after_rebate
        else:
            return raw_sur

    gross_salary = inp["gross_salary"]
    basic_salary = inp["basic_salary"]
    hra_received = inp["hra_received"]
    rent_paid = inp["rent_paid"]
    metro = inp["metro"]
    age = inp["age"]
    sec_80c = inp["sec_80c"]
    sec_80ccd1b = inp["sec_80ccd1b"]
    sec_80d_self = inp["sec_80d_self"]
    sec_80d_parents = inp["sec_80d_parents"]
    parents_senior = inp["parents_senior"]
    home_loan_interest = inp["home_loan_interest"]

    steps = []

    hra_limit_b = 0
    if metro:
        hra_limit_b = basic_salary * 50 // 100
    else:
        hra_limit_b = basic_salary * 40 // 100

    hra_limit_c = rent_paid - (basic_salary * 10 // 100)
    if hra_limit_c < 0:
        hra_limit_c = 0

    exempt_hra = min(hra_received, hra_limit_b, hra_limit_c)
    if exempt_hra < 0:
        exempt_hra = 0

    salary_after_hra = gross_salary - exempt_hra

    std_old = 50000
    if std_old > salary_after_hra:
        std_old = salary_after_hra

    after_std_old = salary_after_hra - std_old
    steps = steps + [["C-STD-OLD", std_old]]

    s24b_allowed = min(home_loan_interest, 200000)
    if s24b_allowed < 0:
        s24b_allowed = 0

    after_24b = after_std_old - s24b_allowed
    steps = steps + [["C-24B", s24b_allowed]]

    c_80c_allowed = min(sec_80c, 150000)
    c_80ccd1b_allowed = min(sec_80ccd1b, 50000)

    limit_80d_self = 50000 if age >= 60 else 25000
    c_80d_self_allowed = min(sec_80d_self, limit_80d_self)

    limit_80d_parents = 50000 if parents_senior else 25000
    c_80d_parents_allowed = min(sec_80d_parents, limit_80d_parents)

    total_via = c_80c_allowed + c_80ccd1b_allowed + c_80d_self_allowed + c_80d_parents_allowed
    steps = steps + [["C-80C", c_80c_allowed]]
    steps = steps + [["C-80CCD1B", c_80ccd1b_allowed]]
    steps = steps + [["C-80D-SELF", c_80d_self_allowed]]
    steps = steps + [["C-80D-PARENTS", c_80d_parents_allowed]]

    old_income_raw = after_24b - total_via
    if old_income_raw < 0:
        old_income_raw = 0

    old_total_income = round_to_10(old_income_raw)
    steps = steps + [["C-288A", old_total_income]]

    old_tax_slab = calc_slab_old(old_total_income, age)
    steps = steps + [["C-SLAB-OLD", old_tax_slab]]

    old_rebate = 0
    if old_total_income <= 500000:
        old_rebate = min(old_tax_slab, 12500)
    
    old_tax_after_rebate = old_tax_slab - old_rebate
    steps = steps + [["C-87A-OLD", old_tax_after_rebate]]

    old_sur = calc_surcharge_with_mr(old_total_income, old_tax_after_rebate, "old", age)
    steps = steps + [["C-SUR", old_sur]]

    old_tax_plus_sur = old_tax_after_rebate + old_sur
    old_cess = old_tax_plus_sur * 4 // 100
    steps = steps + [["C-CESS", old_cess]]

    old_tax_raw = old_tax_plus_sur + old_cess
    old_tax = round_to_10(old_tax_raw)
    steps = steps + [["C-288B", old_tax]]

    std_new = 75000
    new_income_raw = gross_salary - std_new
    if new_income_raw < 0:
        new_income_raw = 0

    new_total_income = round_to_10(new_income_raw)
    steps = steps + [["C-STD-NEW", std_new]]
    steps = steps + [["C-288A", new_total_income]]

    new_tax_slab = calc_slab_new(new_total_income)
    steps = steps + [["C-SLAB-NEW", new_tax_slab]]

    new_rebate = 0
    new_tax_after_rebate = new_tax_slab
    if new_total_income <= 1200000:
        new_rebate = min(new_tax_slab, 60000)
        new_tax_after_rebate = new_tax_slab - new_rebate
        steps = steps + [["C-87A-NEW", new_tax_after_rebate]]
    else:
        if new_total_income > 1200000 and new_total_income <= 1264860:
            mr_tax = new_total_income - 1200000
            new_tax_after_rebate = min(new_tax_slab - 60000, mr_tax)
            steps = steps + [["C-87A-NEW-MR", new_tax_after_rebate]]
        else:
            steps = steps + [["C-87A-NEW-MR", new_tax_after_rebate]]

    new_sur = calc_surcharge_with_mr(new_total_income, new_tax_after_rebate, "new", age)
    steps = steps + [["C-SUR", new_sur]]

    new_tax_plus_sur = new_tax_after_rebate + new_sur
    new_cess = new_tax_plus_sur * 4 // 100
    steps = steps + [["C-CESS", new_cess]]

    new_tax_raw = new_tax_plus_sur + new_cess
    new_tax = round_to_10(new_tax_raw)
    steps = steps + [["C-288B", new_tax]]

    best = "new"
    if old_tax < new_tax:
        best = "old"
    else:
        best = "new"

    return {
        "old_total_income": old_total_income,
        "old_tax": old_tax,
        "new_total_income": new_total_income,
        "new_tax": new_tax,
        "best": best,
        "steps": steps
    }
