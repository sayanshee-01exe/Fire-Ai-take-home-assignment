# LLM-compiled tax engine FY 2025-26: REVIEWED version of raw compile 2 (see REVIEW.md and reviewed.diff)
# base: raw_v2.py, cache_key 331ad420e0348847f6e649918bc96401295cef58d06821f9836d0e9abf936604
# rules: RULES_FY2025_26.md sha256 62601fbe20d73cef82a1028accb799c42ccdfb1cef71666781e05641ba86d10f (FY 2025-26)
# edits: D1 exact HRA, D2 exact surcharge and cess, D3 invented constant removed, D4 trace labels

def compute(inp):
  fy = inp["fy"]
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

  hra_factor = 0
  if metro:
    hra_factor = 50
  else:
    hra_factor = 40

  hra_b = basic_salary * hra_factor                       # D1: x100, exact (was // 100)
  hra_c = rent_paid * 100 - basic_salary * 10
  if hra_c < 0:
    hra_c = 0

  exempt_hra = hra_received * 100
  if hra_b < exempt_hra:
    exempt_hra = hra_b
  if hra_c < exempt_hra:
    exempt_hra = hra_c
  if exempt_hra < 0:
    exempt_hra = 0

  salary_after_hra = gross_salary * 100 - exempt_hra

  std_old = 50000 * 100
  if std_old > salary_after_hra:
    std_old = salary_after_hra

  old_after_std = salary_after_hra - std_old

  ded_24b = home_loan_interest
  if ded_24b > 200000:
    ded_24b = 200000
  if ded_24b < 0:
    ded_24b = 0

  old_after_24b = old_after_std - ded_24b * 100
  if old_after_24b < 0:
    old_after_24b = 0

  c_80c_val = sec_80c
  if c_80c_val > 150000:
    c_80c_val = 150000
  if c_80c_val < 0:
    c_80c_val = 0

  c_80ccd1b_val = sec_80ccd1b
  if c_80ccd1b_val > 50000:
    c_80ccd1b_val = 50000
  if c_80ccd1b_val < 0:
    c_80ccd1b_val = 0

  limit_self = 25000
  if age >= 60:
    limit_self = 50000
  c_80d_self_val = sec_80d_self
  if c_80d_self_val > limit_self:
    c_80d_self_val = limit_self
  if c_80d_self_val < 0:
    c_80d_self_val = 0

  limit_parents = 25000
  if parents_senior:
    limit_parents = 50000
  c_80d_parents_val = sec_80d_parents
  if c_80d_parents_val > limit_parents:
    c_80d_parents_val = limit_parents
  if c_80d_parents_val < 0:
    c_80d_parents_val = 0

  ch_via = c_80c_val + c_80ccd1b_val + c_80d_self_val + c_80d_parents_val

  old_ti_raw = old_after_24b - ch_via * 100
  if old_ti_raw < 0:
    old_ti_raw = 0
  old_ti_raw = old_ti_raw // 100                          # D1: paise ignored once, on the total

  rem_old = old_ti_raw % 10
  old_ti = old_ti_raw - rem_old
  if rem_old >= 5:
    old_ti = old_ti + 10

  std_new = 75000
  if std_new > gross_salary:
    std_new = gross_salary

  new_ti_raw = gross_salary - std_new
  if new_ti_raw < 0:
    new_ti_raw = 0

  rem_new = new_ti_raw % 10
  new_ti = new_ti_raw - rem_new
  if rem_new >= 5:
    new_ti = new_ti + 10

  old_slab_tax = 0
  if age < 60:
    if old_ti > 250000:
      if old_ti <= 500000:
        old_slab_tax = (old_ti - 250000) * 5 // 100
      else:
        if old_ti <= 1000000:
          old_slab_tax = 12500 + (old_ti - 500000) * 20 // 100
        else:
          old_slab_tax = 112500 + (old_ti - 1000000) * 30 // 100
  else:
    if age < 80:
      if old_ti > 300000:
        if old_ti <= 500000:
          old_slab_tax = (old_ti - 300000) * 5 // 100
        else:
          if old_ti <= 1000000:
            old_slab_tax = 10000 + (old_ti - 500000) * 20 // 100
          else:
            old_slab_tax = 110000 + (old_ti - 1000000) * 30 // 100
    else:
      if old_ti > 500000:
        if old_ti <= 1000000:
          old_slab_tax = (old_ti - 500000) * 20 // 100
        else:
          old_slab_tax = 100000 + (old_ti - 1000000) * 30 // 100

  old_rebate = 0
  if old_ti <= 500000:
    old_rebate = old_slab_tax
    if old_rebate > 12500:
      old_rebate = 12500

  old_tax_after_rebate = old_slab_tax - old_rebate
  if old_tax_after_rebate < 0:
    old_tax_after_rebate = 0

  old_surcharge_rate = 0
  if old_ti > 5000000:
    old_surcharge_rate = 10
  if old_ti > 10000000:
    old_surcharge_rate = 15
  if old_ti > 20000000:
    old_surcharge_rate = 25
  if old_ti > 50000000:
    old_surcharge_rate = 37

  old_total = old_tax_after_rebate * (100 + old_surcharge_rate)   # D2: x100, exact (was // 100)

  old_threshold_income = 0
  old_threshold_tax_base = 0
  old_threshold_surcharge_rate = 0
  if old_ti > 5000000 and old_ti <= 10000000:
    old_threshold_income = 5000000
    old_threshold_surcharge_rate = 0
  elif old_ti > 10000000 and old_ti <= 20000000:
    old_threshold_income = 10000000
    old_threshold_surcharge_rate = 10
  elif old_ti > 20000000 and old_ti <= 50000000:
    old_threshold_income = 20000000
    old_threshold_surcharge_rate = 15
  elif old_ti > 50000000:
    old_threshold_income = 50000000
    old_threshold_surcharge_rate = 25

  if old_threshold_income > 0:
    t_slab = 0
    if age < 60:
      t_slab = 112500 + (5000000 - 1000000) * 30 // 100
      if old_threshold_income == 5000000:
        t_slab = 112500 + (5000000 - 1000000) * 30 // 100
      elif old_threshold_income == 10000000:
        t_slab = 112500 + (10000000 - 1000000) * 30 // 100
      elif old_threshold_income == 20000000:
        t_slab = 112500 + (20000000 - 1000000) * 30 // 100
      elif old_threshold_income == 50000000:
        t_slab = 112500 + (50000000 - 1000000) * 30 // 100
    else:
      if age < 80:
        if old_threshold_income == 5000000:
          t_slab = 110000 + (5000000 - 1000000) * 30 // 100
        elif old_threshold_income == 10000000:
          t_slab = 110000 + (10000000 - 1000000) * 30 // 100
        elif old_threshold_income == 20000000:
          t_slab = 110000 + (20000000 - 1000000) * 30 // 100
        elif old_threshold_income == 50000000:
          t_slab = 110000 + (50000000 - 1000000) * 30 // 100
      else:
        if old_threshold_income == 5000000:
          t_slab = 100000 + (5000000 - 1000000) * 30 // 100
        elif old_threshold_income == 10000000:
          t_slab = 100000 + (10000000 - 1000000) * 30 // 100
        elif old_threshold_income == 20000000:
          t_slab = 100000 + (20000000 - 1000000) * 30 // 100
        elif old_threshold_income == 50000000:
          t_slab = 100000 + (50000000 - 1000000) * 30 // 100

    t_total = t_slab * (100 + old_threshold_surcharge_rate) + (old_ti - old_threshold_income) * 100
    if old_total > t_total:
      old_total = t_total

  old_tax_plus_sur = old_total / 100
  old_cess = old_total * 4 / 10000
  old_final_raw = old_total * 104 // 10000               # D2: paise ignored once, on the final figure

  rem_old_tax = old_final_raw % 10
  old_tax = old_final_raw - rem_old_tax
  if rem_old_tax >= 5:
    old_tax = old_tax + 10

  new_slab_tax = 0
  if new_ti > 400000:
    if new_ti <= 800000:
      new_slab_tax = (new_ti - 400000) * 5 // 100
    else:
      if new_ti <= 1200000:
        new_slab_tax = 20000 + (new_ti - 800000) * 10 // 100
      else:
        if new_ti <= 1600000:
          new_slab_tax = 60000 + (new_ti - 1200000) * 15 // 100
        else:
          if new_ti <= 2000000:
            new_slab_tax = 120000 + (new_ti - 1600000) * 20 // 100
          else:
            if new_ti <= 2400000:
              new_slab_tax = 200000 + (new_ti - 2000000) * 25 // 100
            else:
              new_slab_tax = 300000 + (new_ti - 2400000) * 30 // 100

  new_rebate = 0
  if new_ti <= 1200000:
    new_rebate = new_slab_tax
    if new_rebate > 60000:
      new_rebate = 60000

  new_tax_after_rebate = new_slab_tax - new_rebate
  if new_tax_after_rebate < 0:
    new_tax_after_rebate = 0

  if new_ti > 1200000:                                    # D3: no invented upper limit (was <= 1276595)
    mr_tax_limit = new_ti - 1200000
    if new_tax_after_rebate > mr_tax_limit:
      new_tax_after_rebate = mr_tax_limit

  new_surcharge_rate = 0
  if new_ti > 5000000:
    new_surcharge_rate = 10
  if new_ti > 10000000:
    new_surcharge_rate = 15
  if new_ti > 20000000:
    new_surcharge_rate = 25
  if new_ti > 50000000:
    new_surcharge_rate = 25

  new_total = new_tax_after_rebate * (100 + new_surcharge_rate)   # D2: x100, exact (was // 100)

  new_threshold_income = 0
  new_threshold_tax_base = 0
  new_threshold_surcharge_rate = 0
  if new_ti > 5000000 and new_ti <= 10000000:
    new_threshold_income = 5000000
    new_threshold_surcharge_rate = 0
  elif new_ti > 10000000 and new_ti <= 20000000:
    new_threshold_income = 10000000
    new_threshold_surcharge_rate = 10
  elif new_ti > 20000000:
    new_threshold_income = 20000000
    new_threshold_surcharge_rate = 15

  if new_threshold_income > 0:
    t_slab_new = 300000 + (new_threshold_income - 2400000) * 30 // 100
    t_total_new = t_slab_new * (100 + new_threshold_surcharge_rate) + (new_ti - new_threshold_income) * 100
    if new_total > t_total_new:
      new_total = t_total_new

  new_tax_plus_sur = new_total / 100
  new_cess = new_total * 4 / 10000
  new_final_raw = new_total * 104 // 10000               # D2: paise ignored once, on the final figure

  rem_new_tax = new_final_raw % 10
  new_tax = new_final_raw - rem_new_tax
  if rem_new_tax >= 5:
    new_tax = new_tax + 10

  best_regime = "new"
  if old_tax < new_tax:
    best_regime = "old"

  steps = []
  steps = steps + [["C-HRA", exempt_hra / 100]]
  steps = steps + [["C-STD-OLD", std_old / 100]]
  steps = steps + [["C-24B", ded_24b]]
  steps = steps + [["C-80C", c_80c_val]]
  steps = steps + [["C-80CCD1B", c_80ccd1b_val]]
  steps = steps + [["C-80D-SELF", c_80d_self_val]]
  steps = steps + [["C-80D-PARENTS", c_80d_parents_val]]
  steps = steps + [["C-288A", old_ti]]
  steps = steps + [["C-STD-NEW", std_new]]
  steps = steps + [["C-288A", new_ti]]
  steps = steps + [["C-SLAB-OLD", old_slab_tax]]
  steps = steps + [["C-87A-OLD", old_rebate]]            # D4: the rebate, not the tax after it
  steps = steps + [["C-SUR", old_tax_plus_sur]]
  steps = steps + [["C-CESS", old_cess]]                  # D4: the cess, not the final tax
  steps = steps + [["C-288B", old_tax]]
  steps = steps + [["C-SLAB-NEW", new_slab_tax]]
  steps = steps + [["C-87A-NEW", new_slab_tax - new_tax_after_rebate]]   # D4: rebate plus any relief
  steps = steps + [["C-SUR", new_tax_plus_sur]]
  steps = steps + [["C-CESS", new_cess]]
  steps = steps + [["C-288B", new_tax]]

  return {
    "old_total_income": old_ti,
    "old_tax": old_tax,
    "new_total_income": new_ti,
    "new_tax": new_tax,
    "best": best_regime,
    "steps": steps
  }
