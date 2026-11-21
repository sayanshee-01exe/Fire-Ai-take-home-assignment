# LLM-compiled tax engine: raw compile 3 (UNREVIEWED; generated, not hand-edited)
# model: gemini-3.5-flash-lite   role: compiler   temperature: 0.2   run_index: 2
# prompt_sha256: 27200f8c63a06ea490236d4074ad3e10c22addc1a217e77f161b783e7368f13d
# rules: RULES_FY2024_25.md sha256 8b32902a838e90d8fb6a64282994597c896c1c92275c3659bc29c3df6fe18f89 (FY 2024-25)
# written_at: 2026-11-20T19:04:46+00:00   from cache: True   cache_key: e915108ada32107edb7782cdcc9b18af09f02e307cc0c7e008f4aac8d3105ad6
# tokens: prompt 2424, output 4390 (thinking 0)   latency_s: 9.55
# allow-list: REJECTED (18 violations)

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
        metro_pct_salary = (basic_salary * 50) // 100
    else:
        metro_pct_salary = (basic_salary * 40) // 100
    rent_minus_10_pct = rent_paid - ((basic_salary * 10) // 100)
    if rent_minus_10_pct < 0:
        rent_minus_10_pct = 0
    hra_candidates = [hra_received, metro_pct_salary, rent_minus_10_pct]
    exempt_hra = hra_candidates[0]
    if hra_candidates[1] < exempt_hra:
        exempt_hra = hra_candidates[1]
    if hra_candidates[2] < exempt_hra:
        exempt_hra = hra_candidates[2]
    if exempt_hra < 0:
        exempt_hra = 0

    salary_after_hra = gross_salary - exempt_hra
    std_old_limit = 50000
    if salary_after_hra < std_old_limit:
        c_std_old = salary_after_hra
    else:
        c_std_old = std_old_limit

    salary_after_std_old = salary_after_hra - c_std_old
    if home_loan_interest > 200000:
        c_24b = 200000
    else:
        c_24b = home_loan_interest
    if c_24b < 0:
        c_24b = 0

    income_after_24b = salary_after_std_old - c_24b

    if sec_80c > 150000:
        c_80c = 150000
    else:
        c_80c = sec_80c
    if c_80c < 0:
        c_80c = 0

    if sec_80ccd1b > 50000:
        c_80ccd1b = 50000
    else:
        c_80ccd1b = sec_80ccd1b
    if c_80ccd1b < 0:
        c_80ccd1b = 0

    if age >= 60:
        limit_80d_self = 50000
    else:
        limit_80d_self = 25000
    if sec_80d_self > limit_80d_self:
        c_80d_self = limit_80d_self
    else:
        c_80d_self = sec_80d_self
    if c_80d_self < 0:
        c_80d_self = 0

    if parents_senior:
        limit_80d_parents = 50000
    else:
        limit_80d_parents = 25000
    if sec_80d_parents > limit_80d_parents:
        c_80d_parents = limit_80d_parents
    else:
        c_80d_parents = sec_80d_parents
    if c_80d_parents < 0:
        c_80d_parents = 0

    total_via = c_80c + c_80ccd1b + c_80d_self + c_80d_parents
    old_raw_income = income_after_24b - total_via
    if old_raw_income < 0:
        old_raw_income = 0
    old_total_income = round_nearest_10(old_raw_income)

    def calc_slab_old(inc, age_val):
        tax = 0
        if age_val >= 80:
            if inc > 500000:
                if inc <= 1000000:
                    tax = (inc - 500000) * 20 // 100
                else:
                    tax = 500000 * 20 // 100 + (inc - 1000000) * 30 // 100
        elif age_val >= 60:
            if inc > 300000:
                if inc <= 500000:
                    tax = (inc - 300000) * 5 // 100
                elif inc <= 1000000:
                    tax = 200000 * 5 // 100 + (inc - 500000) * 20 // 100
                else:
                    tax = 200000 * 5 // 100 + 500000 * 20 // 100 + (inc - 1000000) * 30 // 100
        else:
            if inc > 250000:
                if inc <= 500000:
                    tax = (inc - 250000) * 5 // 100
                elif inc <= 1000000:
                    tax = 250000 * 5 // 100 + (inc - 500000) * 20 // 100
                else:
                    tax = 250000 * 5 // 100 + 500000 * 20 // 100 + (inc - 1000000) * 30 // 100
        return tax

    old_slab_tax = calc_slab_old(old_total_income, age)

    old_tax_after_rebate = old_slab_tax
    if old_total_income <= 500000:
        rebate_old = old_slab_tax
        if rebate_old > 12500:
            rebate_old = 12500
        old_tax_after_rebate = old_slab_tax - rebate_old

    def get_surcharge_rate(inc, is_new_regime):
        rate = 0
        if inc > 50000000:
            if is_new_regime:
                rate = 25
            else:
                rate = 37
        elif inc > 20000000:
            rate = 25
        elif inc > 10000000:
            rate = 15
        elif inc > 5000000:
            rate = 10
        return rate

    def get_lower_threshold_and_rate(inc, is_new_regime):
        if inc > 50000000:
            return 50000000, 25
        elif inc > 20000000:
            return 20000000, 15
        elif inc > 10000000:
            return 10000000, 10
        elif inc > 5000000:
            return 5000000, 0
        return 0, 0

    def calc_tax_with_surcharge_and_cess(inc, slab_tax_val, is_new_regime, is_old_87a, age_val):
        tax_ar = slab_tax_val
        if is_old_87a:
            if inc <= 500000:
                reb = slab_tax_val
                if reb > 12500:
                    reb = 12500
                tax_ar = slab_tax_val - reb
        else:
            if inc <= 700000:
                reb = slab_tax_val
                if reb > 25000:
                    reb = 25000
                tax_ar = slab_tax_val - reb
            else:
                if inc <= 700000:
                    pass

        sur_rate = get_surcharge_rate(inc, is_new_regime)
        surcharge = (tax_ar * sur_rate) // 100

        thresh, lower_sur_rate = get_lower_threshold_and_rate(inc, is_new_regime)
        if thresh > 0:
            if is_new_regime:
                thresh_slab = calc_slab_new(thresh)
                thresh_tax_ar = thresh_slab
                if thresh <= 700000:
                    reb_t = thresh_slab
                    if reb_t > 25000:
                        reb_t = 25000
                    thresh_tax_ar = thresh_slab - reb_t
            else:
                thresh_slab = calc_slab_old(thresh, age_val)
                thresh_tax_ar = thresh_slab
                if thresh <= 500000:
                    reb_t = thresh_slab
                    if reb_t > 12500:
                        reb_t = 12500
                    thresh_tax_ar = thresh_slab - reb_t
            thresh_surcharge = (thresh_tax_ar * lower_sur_rate) // 100
            max_allowed_tax_sur = thresh_tax_ar + thresh_surcharge + (inc - thresh)
            if (tax_ar + surcharge) > max_allowed_tax_sur:
                surcharge = max_allowed_tax_sur - tax_ar
                if surcharge < 0:
                    surcharge = 0

        tax_plus_sur = tax_ar + surcharge
        cess = (tax_plus_sur * 4) // 100
        return tax_plus_sur + cess

    old_final_tax_calc = calc_tax_with_surcharge_and_cess(old_total_income, old_slab_tax, False, True, age)
    old_tax = round_nearest_10(old_final_tax_calc)

    def calc_slab_new(inc):
        tax = 0
        if inc > 300000:
            if inc <= 700000:
                tax = (inc - 300000) * 5 // 100
            elif inc <= 1000000:
                tax = 400000 * 5 // 100 + (inc - 700000) * 10 // 100
            elif inc <= 1200000:
                tax = 400000 * 5 // 100 + 300000 * 10 // 100 + (inc - 1000000) * 15 // 100
            elif inc <= 1500000:
                tax = 400000 * 5 // 100 + 300000 * 10 // 100 + 200000 * 15 // 100 + (inc - 1200000) * 20 // 100
            else:
                tax = 400000 * 5 // 100 + 300000 * 10 // 100 + 200000 * 15 // 100 + 300000 * 20 // 100 + (inc - 1500000) * 30 // 100
        return tax

    std_new_limit = 75000
    if gross_salary < std_new_limit:
        c_std_new = gross_salary
    else:
        c_std_new = std_new_limit

    new_raw_income = gross_salary - c_std_new
    if new_raw_income < 0:
        new_raw_income = 0
    new_total_income = round_nearest_10(new_raw_income)

    new_slab_tax = calc_slab_new(new_total_income)
    new_tax_after_rebate = new_slab_tax
    if new_total_income <= 700000:
        rebate_new = new_slab_tax
        if rebate_new > 25000:
            rebate_new = 25000
        new_tax_after_rebate = new_slab_tax - rebate_new
    else:
        marginal_diff = new_total_income - 700000
        tax_at_7l = calc_slab_new(700000)
        rebate_at_7l = tax_at_7l
        if rebate_at_7l > 25000:
            rebate_at_7l = 25000
        tax_ar_7l = tax_at_7l - rebate_at_7l
        max_allowed_new = tax_ar_7l + marginal_diff
        if new_tax_after_rebate > max_allowed_new:
            new_tax_after_rebate = max_allowed_new

    new_final_tax_calc = calc_tax_with_surcharge_and_cess(new_total_income, new_slab_tax, True, False, age)
    if new_total_income > 700000:
        sur_rate_new = get_surcharge_rate(new_total_income, True)
        surcharge_new = (new_tax_after_rebate * sur_rate_new) // 100
        thresh_n, lower_sur_rate_n = get_lower_threshold_and_rate(new_total_income, True)
        if thresh_n > 0:
            thresh_slab_n = calc_slab_new(thresh_n)
            thresh_tax_ar_n = thresh_slab_n
            if thresh_n <= 700000:
                reb_tn = thresh_slab_n
                if reb_tn > 25000:
                    reb_tn = 25000
                thresh_tax_ar_n = thresh_slab_n - reb_tn
            thresh_surcharge_n = (thresh_tax_ar_n * lower_sur_rate_n) // 100
            max_allowed_tax_sur_n = thresh_tax_ar_n + thresh_surcharge_n + (new_total_income - thresh_n)
            if (new_tax_after_rebate + surcharge_new) > max_allowed_tax_sur_n:
                surcharge_new = max_allowed_tax_sur_n - new_tax_after_rebate
                if surcharge_new < 0:
                    surcharge_new = 0
        tax_plus_sur_n = new_tax_after_rebate + surcharge_new
        cess_n = (tax_plus_sur_n * 4) // 100
        new_final_tax_calc = tax_plus_sur_n + cess_n

    new_tax = round_nearest_10(new_final_tax_calc)

    if old_tax < new_tax:
        best = "old"
    elif new_tax < old_tax:
        best = "new"
    else:
        best = "new"

    steps = [
        ["C-HRA", exempt_hra],
        ["C-STD-OLD", c_std_old],
        ["C-24B", c_24b],
        ["C-80C", c_80c],
        ["C-80CCD1B", c_80ccd1b],
        ["C-80D-SELF", c_80d_self],
        ["C-80D-PARENTS", c_80d_parents],
        ["C-288A", old_total_income],
        ["C-SLAB-OLD", old_slab_tax],
        ["C-87A-OLD", old_tax_after_rebate],
        ["C-SUR", old_tax],
        ["C-CESS", old_tax],
        ["C-288B", old_tax],
        ["C-STD-NEW", c_std_new],
        ["C-288A", new_total_income],
        ["C-SLAB-NEW", new_slab_tax],
        ["C-87A-NEW", new_tax_after_rebate],
        ["C-SUR", new_tax],
        ["C-CESS", new_tax],
        ["C-288B", new_tax],
    ]

    return {
        "old_total_income": old_total_income,
        "old_tax": old_tax,
        "new_total_income": new_total_income,
        "new_tax": new_tax,
        "best": best,
        "steps": steps,
    }
