"""Before/after impact of the PII false-positive fixes C1, C3, P1 (DESIGN.md section 4, l), January-August only.

C1  rule:     a cue word that directly follows a capitalised word (inside a Title Case label) is no cue
C3  novelty:  digit runs that the explicit lists call public (PUBLIC_PHONE_NUMBERS, TOLL_FREE_AREA_CODES)
              are not personal-data-shaped
P1  rule:     (202) 366-2220, a public federal number printed in a DCWP template, joins PUBLIC_PHONE_NUMBERS
C2' (no capitalisation signal in Title Case category labels) was considered and NOT adopted.

The split stays inside the reference period: the detectors are fitted on the clean pool created before
2023-07-01 and measured on every clean 5,000-row Jul-Aug batch, into which the project's injector also
puts synthetic personal data (2 %, its own seeds, tag 105) to measure recall per kind. The production
fit (all of Jan-Aug) is used only to report the novelty threshold it lands on. Sep-Dec is never read.
Read-only: nothing is written. About 4 minutes.

Usage: python -m project_b.audits.pii_false_positives
"""
import json
import pickle
from collections import Counter

import numpy as np

from project_b import config, evaluate, inject
from project_b.data.load import make_batches
from project_b.detectors import pii

SPLIT = "2023-07-01T00:00:00"
P1_NUMBER = "2023662220"
AFTER_PUBLIC = pii.PUBLIC_PHONE_NUMBERS
BEFORE_PUBLIC = AFTER_PUBLIC - {P1_NUMBER}


def configure(c1: bool, c3: bool, p1: bool) -> None:
    """Switch the detector module to a given combination of the three fixes."""
    pii.CUE_INSIDE_LABEL_IS_NOT_A_CUE = c1
    pii.NOVELTY_SKIPS_PUBLIC_NUMBERS = c3
    pii.PUBLIC_PHONE_NUMBERS = AFTER_PUBLIC if p1 else BEFORE_PUBLIC


def main() -> dict:
    reference = evaluate.read_period(evaluate.DERIVED_DIR / evaluate.POOL_FILE, reference=True)
    assert (reference["created_date"] < config.REFERENCE_END).all()
    fit_part = reference[reference["created_date"] < SPLIT]
    batches = make_batches(reference[reference["created_date"] >= SPLIT].reset_index(drop=True), config.BATCH_SIZE)
    with open(evaluate.DERIVED_DIR / evaluate.CONTEXT_FILE, "rb") as fh:
        ctx = pickle.load(fh)
    injected = [inject.inject(b, "l", np.random.default_rng([config.SEED, 105, i]), ctx, rate=config.ROW_INJECTION_RATE)
                for i, b in enumerate(batches)]
    variants = {"before": (False, False, False), "C1": (True, False, False), "C3": (False, True, False),
                "P1": (False, False, True), "after (C1+C3+P1)": (True, True, True)}
    results, flagged_before = {}, Counter()
    for name, flags in variants.items():
        configure(*flags)
        rule, stat = pii.RulePiiRegex(), pii.StatPiiTemplateNovelty().fit(fit_part)
        clean, hit, per_kind, n_kind = Counter(), Counter(), Counter(), Counter()
        for b, inj in zip(batches, injected):
            r, s = rule.detect(b), stat.detect(b)
            union = r.row_flags | s.row_flags
            clean.update(rule=int(r.row_flags.sum()), stat=int(s.row_flags.sum()), union=int(union.sum()), batches=int(union.any()))
            if name == "before":
                for pos in np.flatnonzero(union):
                    flagged_before[(r.row_reasons.get(int(pos)) or s.row_reasons.get(int(pos)) or "")[:120]] += 1
            labels = np.asarray(inj.row_labels, dtype=bool)
            ri, si = rule.detect(inj.df).row_flags, stat.detect(inj.df).row_flags
            hit.update(rule=int((ri & labels).sum()), stat=int((si & labels).sum()), union=int(((ri | si) & labels).sum()))
            for kind, caught in zip(np.asarray(inj.row_variant)[labels], ((ri | si) & labels)[labels]):
                n_kind[kind] += 1
                per_kind[kind] += int(caught)
        n = sum(n_kind.values())
        results[name] = {
            "clean_rows_flagged": dict(clean), "injected_rows": n,
            "recall": {k: round(v / n, 4) for k, v in hit.items()},
            "union_caught_per_kind": {k: f"{per_kind[k]}/{n_kind[k]}" for k in sorted(n_kind)},
            "novelty_threshold_split_fit": round(stat.threshold_, 3),
            "novelty_threshold_production_fit": round(pii.StatPiiTemplateNovelty().fit(reference).threshold_, 3),
        }
    configure(True, True, True)  # leave the module as the code defines it
    result = {"split": {"fit": f"clean pool before {SPLIT[:10]} ({len(fit_part):,} rows)", "measure": f"{len(batches)} clean Jul-Aug batches"},
              "results": results, "clean_flags_before_by_reason": [f"{n}x {r}" for r, n in flagged_before.most_common()]}
    print(json.dumps(result, indent=1))
    return result


if __name__ == "__main__":
    main()
