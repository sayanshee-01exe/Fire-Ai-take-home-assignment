# NL query set: review copy

Generated from `queries.jsonl` by `render_md.py`; edit the JSONL, not this file.

---

### NL-01

- **Language:** English
- **Split:** heldout

> I'm 32 and work in Bengaluru. For FY 2024-25 my gross salary is 6 LPA; basic is 25k a month and HRA 10k a month. I pay rent of 12,000 per month, put 1.5 lakh in PPF and pay 25k health insurance for myself.

- **Expected behaviour:** compute (source OC-01)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 32
    - Gross salary (annual): ₹6,00,000
    - Basic salary (annual): ₹3,00,000
    - HRA received (annual): ₹1,20,000
    - Rent paid (annual): ₹1,44,000
    - Metro city: no
    - 80C: ₹1,50,000
    - 80D self and family: ₹25,000
- **Tests:** HRA from monthly figures (non-metro), plus 80C and 80D self; 'LPA' and 'k' units.

---

### NL-02

- **Language:** Hinglish
- **Split:** heldout

> Main 63 saal ka hoon aur abhi bhi job karta hoon. FY 2024-25 mein salary 50 hazaar mahina hai, LIC mein 1 lakh dala aur apne health insurance ka premium 50k bhara. Kitna tax banega?

- **Expected behaviour:** compute (source OC-02)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 63
    - Gross salary (annual): ₹6,00,000
    - 80C: ₹1,00,000
    - 80D self and family: ₹50,000
- **Tests:** Senior (60-79) with monthly salary in 'hazaar'; 80D self cap is 50k for a senior.

---

### NL-03

- **Language:** Mixed
- **Split:** heldout

> Age 63, salary ₹10,00,000 for 2024-25. 80C mein 1 lakh (PPF), and mediclaim for myself 50k. Old vs new kaunsa better?

- **Expected behaviour:** compute (source OC-03)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 63
    - Gross salary (annual): ₹10,00,000
    - 80C: ₹1,00,000
    - 80D self and family: ₹50,000
- **Tests:** Senior regime comparison; Indian digit grouping with rupee sign; 'mediclaim' as 80D self.

---

### NL-04

- **Language:** Hinglish
- **Split:** dev

> Meri age 32 hai, Hyderabad mein rehta hoon. FY 2024-25 ki gross salary 1.25 lakh per month hai, jisme basic 62,500 aur HRA 25,000 mahine ka hai. Kiraya 30k mahina deta hoon. ELSS mein 1.5 lakh aur health insurance 25k.

- **Expected behaviour:** compute (source OC-04)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 32
    - Gross salary (annual): ₹15,00,000
    - Basic salary (annual): ₹7,50,000
    - HRA received (annual): ₹3,00,000
    - Rent paid (annual): ₹3,60,000
    - Metro city: no
    - 80C: ₹1,50,000
    - 80D self and family: ₹25,000
- **Tests:** All salary parts monthly ('per month', 'mahine ka'); Hyderabad is non-metro for HRA; 'kiraya' = rent.

---

### NL-05

- **Language:** English
- **Split:** heldout

> 32-year-old in Pune. Gross salary ₹25,00,000 in FY 24-25, of which basic is ₹12.5 lakh and HRA ₹5 lakh. Rent is 50k a month. 80C investments 1.5 lakh, self health cover premium 25,000.

- **Expected behaviour:** compute (source OC-05)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 32
    - Gross salary (annual): ₹25,00,000
    - Basic salary (annual): ₹12,50,000
    - HRA received (annual): ₹5,00,000
    - Rent paid (annual): ₹6,00,000
    - Metro city: no
    - 80C: ₹1,50,000
    - 80D self and family: ₹25,000
- **Tests:** Short FY form 'FY 24-25'; annual basic/HRA with monthly rent; Pune non-metro.

---

### NL-06

- **Language:** Hinglish
- **Split:** heldout

> Papa ki age 63 hai aur unki salary 25 lakh saalana hai (FY 2024-25). Unhone 80C mein 1 lakh aur apne mediclaim pe 50 hazaar diye. Unka tax kitna hoga?

- **Expected behaviour:** compute (source OC-06)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 63
    - Gross salary (annual): ₹25,00,000
    - 80C: ₹1,00,000
    - 80D self and family: ₹50,000
- **Tests:** Taxpayer is the user's father (third person); 'apne mediclaim' is his own 80D, not a parents' premium.

---

### NL-07

- **Language:** Mixed
- **Split:** heldout

> I'm 32, Ahmedabad based. Salary 0.5 cr gross for FY 2024-25 — basic 25 lakh, HRA 10 lakh. Ghar ka rent 1 lakh per month hai. 80C full 1.5L and 25k health insurance (self).

- **Expected behaviour:** compute (source OC-07)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 32
    - Gross salary (annual): ₹50,00,000
    - Basic salary (annual): ₹25,00,000
    - HRA received (annual): ₹10,00,000
    - Rent paid (annual): ₹12,00,000
    - Metro city: no
    - 80C: ₹1,50,000
    - 80D self and family: ₹25,000
- **Tests:** Crore written as '0.5 cr'; '1.5L' shorthand; gross is exactly 50L but taxable income is below the surcharge threshold.

---

### NL-08

- **Language:** English
- **Split:** dev

> Age 32, living in Bengaluru. FY 2024-25 gross pay is 1 crore: basic ₹50 lakh, HRA ₹20 lakh. I pay ₹2 lakh a month in rent. Maxed out 80C (1.5 lakh) and paid 25k for my own health insurance.

- **Expected behaviour:** compute (source OC-08)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 32
    - Gross salary (annual): ₹1,00,00,000
    - Basic salary (annual): ₹50,00,000
    - HRA received (annual): ₹20,00,000
    - Rent paid (annual): ₹24,00,000
    - Metro city: no
    - 80C: ₹1,50,000
    - 80D self and family: ₹25,000
- **Tests:** 'Maxed out 80C' with the amount stated; large HRA; both regimes in the 10% surcharge band.

---

### NL-09

- **Language:** Hinglish
- **Split:** heldout

> Meri umar 63 hai, salary 1 cr per annum, AY 2025-26. 80C mein 1,00,000 aur self ka health insurance 50,000. Tax batao dono regime mein.

- **Expected behaviour:** compute (source OC-09)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 63
    - Gross salary (annual): ₹1,00,00,000
    - 80C: ₹1,00,000
    - 80D self and family: ₹50,000
- **Tests:** Year given as AY 2025-26, which must map to FY 2024-25; 'umar' = age.

---

### NL-10

- **Language:** English
- **Split:** heldout

> I'm 30, earning 10 LPA in FY 2024-25. I invest 1.5 lakh under 80C, put an extra 50k into my own NPS account under 80CCD(1B), and pay 25k for my health insurance. Which regime should I pick?

- **Expected behaviour:** compute (source OC-10)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹10,00,000
    - 80C: ₹1,50,000
    - 80CCD(1B) own NPS: ₹50,000
    - 80D self and family: ₹25,000
- **Tests:** Own NPS contribution named as 80CCD(1B) (in scope), distinct from employer NPS.

---

### NL-11

- **Language:** Hinglish
- **Split:** heldout

> Main 30 ka hoon, FY 2024-25 mein ₹7.5 lakh salary. 80C 1.5 lakh, NPS mein khud se 50k (1B), aur 25k ka health insurance. Old regime lu ya new?

- **Expected behaviour:** compute (source OC-11)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹7,50,000
    - 80C: ₹1,50,000
    - 80CCD(1B) own NPS: ₹50,000
    - 80D self and family: ₹25,000
- **Tests:** Decimal lakh with rupee sign; 'khud se' NPS = own contribution 80CCD(1B).

---

### NL-12

- **Language:** Mixed
- **Split:** dev

> Age 62, gross salary 15 lakh for FY 2024-25. Home loan ka interest 2 lakh for the flat I live in, aur 80C mein 1.5 lakh. Best regime?

- **Expected behaviour:** compute (source OC-12)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 62
    - Gross salary (annual): ₹15,00,000
    - 80C: ₹1,50,000
    - Home-loan interest (self-occupied): ₹2,00,000
- **Tests:** Home-loan interest stated as interest on a self-occupied flat (not EMI); senior age.

---

### NL-13

- **Language:** English
- **Split:** heldout

> I'm 30 and work in Hyderabad. From April 2024 to March 2025 my gross salary was 1 lakh a month, with basic of 50k a month and HRA of ₹1,00,000 for the whole year. Rent is 25k per month.

- **Expected behaviour:** compute (source OC-13)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹12,00,000
    - Basic salary (annual): ₹6,00,000
    - HRA received (annual): ₹1,00,000
    - Rent paid (annual): ₹3,00,000
    - Metro city: no
- **Tests:** Year as a date range; mix of monthly and annual figures; HRA received is the binding limit.

---

### NL-14

- **Language:** Hinglish
- **Split:** dev

> Main 30 saal ka hoon, Pune mein parents ke saath rehta hoon, rent nahi deta. FY 2024-25 mein 12 lakh gross, basic 6 lakh aur HRA 2 lakh milta hai. Tax kitna?

- **Expected behaviour:** compute (source OC-14)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹12,00,000
    - Basic salary (annual): ₹6,00,000
    - HRA received (annual): ₹2,00,000
    - Rent paid (annual): ₹0
    - Metro city: no
- **Tests:** HRA received but zero rent, so HRA exemption is 0; must not invent a rent figure.

---

### NL-15

- **Language:** Hinglish
- **Split:** heldout

> Delhi mein kiraye pe rehta hoon, 20k mahina. Age 30. FY 2024-25 ki salary 12,00,000 hai, basic 3 lakh aur HRA 1.5 lakh saal ka.

- **Expected behaviour:** compute (source OC-15)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹12,00,000
    - Basic salary (annual): ₹3,00,000
    - HRA received (annual): ₹1,50,000
    - Rent paid (annual): ₹2,40,000
    - Metro city: yes
- **Tests:** Metro city (Delhi, 50% of basic); rent monthly, basic and HRA annual ('saal ka').

---

### NL-16

- **Language:** English
- **Split:** heldout

> 30 years old, 20 LPA salary in FY 2024-25, and ₹1 lakh in 80C (EPF + PPF). Nothing else. Old or new regime?

- **Expected behaviour:** compute (source OC-16)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹20,00,000
    - 80C: ₹1,00,000
- **Tests:** Single deduction below the 80C cap; 'Nothing else' means no other deductions.

---

### NL-17

- **Language:** Mixed
- **Split:** dev

> I'm 30, salary ₹20 lakh (FY 2024-25). Deductions: 80C 1.5L, NPS 1B 50k, self health insurance 25k, parents ka insurance 50k — dono parents 60 se upar hain — aur home loan interest 2 lakh on my self-occupied flat.

- **Expected behaviour:** compute (source OC-17)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹20,00,000
    - 80C: ₹1,50,000
    - 80CCD(1B) own NPS: ₹50,000
    - 80D self and family: ₹25,000
    - 80D parents: ₹50,000
    - A parent is 60 or older: yes
    - Home-loan interest (self-occupied): ₹2,00,000
- **Tests:** Every deduction at its cap; parents are seniors (80D parents cap 50k); many facts in one query.

---

### NL-18

- **Language:** Hinglish
- **Split:** heldout

> Meri salary 5.5 lakh hai FY 2024-25 mein, age 30, koi investment nahi. Tax lagega?

- **Expected behaviour:** compute (source OC-18)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹5,50,000
- **Tests:** Old-regime taxable income exactly 5,00,000 (87A rebate applies); 'koi investment nahi' = no deductions.

---

### NL-19

- **Language:** English
- **Split:** heldout

> I'm 30. My total salary for FY 2024-25 was ₹5,50,010 and I have no deductions. How much tax do I owe?

- **Expected behaviour:** compute (source OC-19)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹5,50,010
- **Tests:** Old-regime taxable income 10 rupees above 5,00,000 (no 87A, no relief): exact odd figure must survive.

---

### NL-20

- **Language:** Hinglish
- **Split:** dev

> Age 30, FY 2024-25 mein 65k per month salary, aur kuch nahi. Kitna tax?

- **Expected behaviour:** compute (source OC-20)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹7,80,000
- **Tests:** Monthly salary x12 = 7,80,000; new-regime 87A marginal-relief zone (tax 5,200).

---

### NL-21

- **Language:** English
- **Split:** heldout

> Single salary income of 797230 rupees in FY 2024-25, I'm 30, no deductions. What's my tax under each regime?

- **Expected behaviour:** compute (source OC-21)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹7,97,230
- **Tests:** Ungrouped digits; new-regime taxable income just past the 7L marginal-relief zone.

---

### NL-22

- **Language:** Hinglish
- **Split:** heldout

> Gross salary ₹50,75,010 hai 2024-25 ke liye, umar 30, koi deduction nahi. Surcharge lagega kya?

- **Expected behaviour:** compute (source OC-22)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹50,75,010
- **Tests:** Just above 50L taxable income in the new regime (surcharge with marginal relief).

---

### NL-23

- **Language:** English
- **Split:** heldout

> 30 years old with a salary of Rs 1,00,75,010 in FY 2024-25 and no deductions. Compute my tax.

- **Expected behaviour:** compute (source OC-23)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹1,00,75,010
- **Tests:** 'Rs' prefix; just above 1 crore taxable income (15% surcharge band with relief).

---

### NL-24

- **Language:** English
- **Split:** dev

> Salary of 2,00,75,010 for FY 2024-25, age 30, no deductions. What's my tax including surcharge?

- **Expected behaviour:** compute (source OC-24)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹2,00,75,010
- **Tests:** Just above 2 crore taxable income in the new regime (25% band with relief).

---

### NL-25

- **Language:** Hinglish
- **Split:** heldout

> Meri gross salary 5,00,50,010 rupaye hai FY 2024-25 mein, age 30, koi deduction nahi. Tax kitna banega?

- **Expected behaviour:** compute (source OC-25)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹5,00,50,010
- **Tests:** Old-regime taxable income just above 5 crore (37% band); new regime capped at 25%.

---

### NL-26

- **Language:** English
- **Split:** heldout

> I'm 30 and earned 50.75 lakh gross salary in FY 2024-25. No investments. What's the tax?

- **Expected behaviour:** compute (source OC-26)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹50,75,000
- **Tests:** Decimal lakh; old-regime taxable income inside the 50L surcharge marginal-relief zone.

---

### NL-27

- **Language:** Hinglish
- **Split:** dev

> Pehli job hai, age 30, FY24-25 mein total salary 3,74,990 mili. Tax dena padega?

- **Expected behaviour:** compute (source OC-27)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 30
    - Gross salary (annual): ₹3,74,990
- **Tests:** Compact 'FY24-25'; new-regime taxable income 10 below the 3L slab boundary.

---

### NL-28

- **Language:** Mixed
- **Split:** heldout

> मेरी उम्र 82 साल है. Salary income 10,50,010 for FY 2024-25. No deductions. Tax kitna?

- **Expected behaviour:** compute (source OC-28)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 82
    - Gross salary (annual): ₹10,50,010
- **Tests:** Age in Devanagari script (super senior, 80+); old-regime taxable income just above 10L.

---

### NL-29

- **Language:** English
- **Split:** heldout

> I'm 59 and my salary for FY 2024-25 is 75k per month. No deductions. How much tax?

- **Expected behaviour:** compute (source OC-29)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 59
    - Gross salary (annual): ₹9,00,000
- **Tests:** Age one below the senior band; monthly salary x12 = 9,00,000.

---

### NL-30

- **Language:** Mixed
- **Split:** dev

> Dadaji 80 saal ke hain, unki salary 12 lakh per annum hai for FY 2024-25. No deductions. Unka tax?

- **Expected behaviour:** compute (source OC-30)
- **Gold input:**
    - Financial year: 2024-25
    - Age: 80
    - Gross salary (annual): ₹12,00,000
- **Tests:** Grandfather is the taxpayer; age exactly 80 (super-senior band starts at 80).

---

### NL-31

- **Language:** English
- **Split:** dev

> Salary ₹9 lakh for FY 2024-25, I pay 50,000 a year for my own health insurance and 1.5 lakh in 80C. What's my old-regime tax?

- **Expected behaviour:** ask
- **Should ask about:**
    - age (senior slabs and the 80D self cap of 25k vs 50k depend on it)
- **Tests:** Age not stated where it matters: old-regime slabs and the 80D cap both change at 60.

---

### NL-32

- **Language:** Hinglish
- **Split:** dev

> Main 30 ka hoon, FY 2024-25 salary 12 lakh, basic 6 lakh, HRA 3 lakh, aur rent 30k mahina deta hoon. HRA exemption ke baad tax kitna?

- **Expected behaviour:** ask
- **Should ask about:**
    - city (metro or non-metro) for the HRA limit
- **Tests:** Rent stated but city missing: exemption is 2,40,000 non-metro vs 3,00,000 metro.

---

### NL-33

- **Language:** English
- **Split:** heldout

> I'm 35, salary 18 LPA for FY 2024-25. I pay a home loan EMI of 40k a month on the flat I live in. How much tax can I save in the old regime?

- **Expected behaviour:** ask
- **Should ask about:**
    - interest portion of the home-loan EMI (only interest counts under 24(b))
- **Tests:** EMI given but not the interest part; must not treat the whole EMI as interest.

---

### NL-34

- **Language:** Hinglish
- **Split:** heldout

> Meri salary 14 lakh hai, lekin salary slip ke hisaab se gross 16.2 lakh banta hai. Age 28, FY 2024-25, 80C 1.5 lakh. Tax kitna?

- **Expected behaviour:** ask
- **Should ask about:**
    - gross salary (two conflicting figures: 14 lakh and 16.2 lakh)
- **Tests:** Conflicting salary figures in one query; must not pick one silently.

---

### NL-35

- **Language:** Mixed
- **Split:** heldout

> Meri salary 85,000 hai, I'm 27, FY 2024-25, no investments. Tax kitna?

- **Expected behaviour:** ask
- **Should ask about:**
    - whether 85,000 is monthly or annual
- **Tests:** Monthly vs annual unclear: 10,20,000 a year vs 85,000 a year give very different tax.

---

### NL-36

- **Language:** Mixed
- **Split:** heldout

> Mera CTC 18 lakh hai for FY 2024-25 — includes employer PF and variable pay. Age 31, 80C 1.5L. Tax kitna banega?

- **Expected behaviour:** ask
- **Should ask about:**
    - gross salary (CTC includes employer PF and variable pay)
- **Tests:** CTC vs gross unclear: employer PF is not salary income and variable pay may not be paid in full.

---

### NL-37

- **Language:** Hinglish
- **Split:** dev

> Pichhle saal aur is saal dono mein meri salary 13 lakh thi. Age 33, koi deduction nahi. Tax kitna hoga?

- **Expected behaviour:** ask
- **Should ask about:**
    - financial year (FY 2024-25 or FY 2025-26; the rules differ)
- **Tests:** Same salary in two tax years with no year given; the system has no default year.

---

### NL-38

- **Language:** English
- **Split:** heldout

> I'm 40 with a salary of 22 lakh in FY 2024-25. I pay 45k health insurance for my parents and 25k for myself. What's my old-regime tax?

- **Expected behaviour:** ask
- **Should ask about:**
    - whether either parent is 60 or older (80D parents cap 25k vs 50k)
- **Tests:** Parents' age not given: 45k is fully deductible only if a parent is a senior.

---

### NL-39

- **Language:** Hinglish
- **Split:** heldout

> Salary 10 lakh hai plus March mein bonus bhi mila tha. Age 29, FY 2024-25. Tax kitna lagega?

- **Expected behaviour:** ask
- **Should ask about:**
    - bonus amount (and whether it is already in the 10 lakh)
- **Tests:** Bonus mentioned with no amount; gross salary is unknown.

---

### NL-40

- **Language:** Mixed
- **Split:** heldout

> Age 26, salary 5.6 lakh FY 2024-25, aur 80C mein kuch investments kiye hain. Old regime mein tax zero hoga?

- **Expected behaviour:** ask
- **Should ask about:**
    - 80C amount ('some investments'; 10k or more brings old-regime income to 5L)
- **Tests:** Near the old-regime rebate threshold: 80C of 10,000 or more makes tax zero, less does not.

---

### NL-41

- **Language:** English
- **Split:** heldout

> My employer puts 10% of basic into NPS — about 1.2 lakh a year — under 80CCD(2). Salary 24 LPA, age 34, FY 2024-25. How much tax in the new regime?

- **Expected behaviour:** unsupported
- **Unsupported:**
    - employer NPS contribution, 80CCD(2)
- **Tests:** Employer NPS is out of scope (PLAN D8) and must not be mapped to 80CCD(1B).

---

### NL-42

- **Language:** Hinglish
- **Split:** heldout

> April se September Mumbai mein tha (rent 40k), phir October se Bengaluru shift ho gaya (rent 30k). Salary 20 lakh, basic 10 lakh, HRA 4 lakh, age 29, FY 2024-25. HRA exemption kitna milega?

- **Expected behaviour:** unsupported
- **Unsupported:**
    - HRA with a mid-year move between a metro and a non-metro city
- **Tests:** Schema has one metro flag and one annual rent, so a two-city year cannot be represented.

---

### NL-43

- **Language:** Mixed
- **Split:** dev

> Salary 15 lakh hai and I also booked ₹3 lakh LTCG on equity mutual funds in FY 2024-25. Age 36. Total tax kitna?

- **Expected behaviour:** unsupported
- **Unsupported:**
    - capital gains (LTCG on equity mutual funds)
- **Tests:** Capital gains are a different income head; salary-only scope.

---

### NL-44

- **Language:** Hinglish
- **Split:** dev

> Job se 12 lakh salary aur side mein freelancing se 6 lakh kamaye FY 2024-25 mein. Age 27. Tax kaise calculate hoga?

- **Expected behaviour:** unsupported
- **Unsupported:**
    - business or professional income (freelancing)
- **Tests:** Freelance income is business/profession income; salary-only scope.

---

### NL-45

- **Language:** English
- **Split:** heldout

> I'm an NRI living in Dubai; my Indian salary for three months of FY 2024-25 was 6 lakh. Age 31. Am I eligible for the 87A rebate, and what's my tax?

- **Expected behaviour:** unsupported
- **Unsupported:**
    - non-resident status
- **Tests:** No residency field; the engine assumes a resident and would wrongly allow 87A.

---

### NL-46

- **Language:** Mixed
- **Split:** heldout

> Resident hoon, but US company ke liye remote kaam karke $30,000 kamaye FY 2024-25 mein, US mein tax bhi kata. Indian salary 8 lakh. Age 33. Foreign tax credit ke saath total tax?

- **Expected behaviour:** unsupported
- **Unsupported:**
    - foreign income with tax paid abroad (foreign tax credit)
- **Tests:** Foreign-currency income with foreign tax credit; no field or rule for either.

---

### NL-47

- **Language:** English
- **Split:** heldout

> In FY 2024-25 I exercised 2,000 ESOPs at ₹100 each when the fair market value was ₹600. My salary is 30 lakh, age 35. How much tax on the ESOP perquisite and in total?

- **Expected behaviour:** unsupported
- **Unsupported:**
    - ESOP perquisite (FMV minus exercise price)
- **Tests:** Perquisite valuation is out of scope (PLAN section 2); the query gives FMV and exercise price, not a salary figure.

---

### NL-48

- **Language:** English
- **Split:** heldout

> What changed in the FY 2025-26 budget for someone earning 12 lakh? Is a 12 lakh income really tax-free now?

- **Expected behaviour:** law-change
- **Tests:** FY 2025-26 rules (87A limit 12L); for Task 5.

---

### NL-49

- **Language:** Hinglish
- **Split:** dev

> FY 2025-26 ke naye budget ke baad 12.75 lakh salary pe kitna tax lagega? Age 30.

- **Expected behaviour:** law-change
- **Tests:** Asks about the FY 2025-26 budget changes; the year is stated explicitly, so it is not an "ask"; for Task 5.

---

### NL-50

- **Language:** Mixed
- **Split:** heldout

> FY 2025-26 mein new regime ke slabs kya hain? Meri salary 18 lakh hai, age 34 — please compare with FY 2024-25.

- **Expected behaviour:** law-change
- **Tests:** Asks for FY 2025-26 slabs and a comparison with FY 2024-25; for Task 5.
