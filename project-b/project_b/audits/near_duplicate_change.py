"""Before/after impact of the near-duplicate change (DESIGN.md section 4, f), on January-August data only.

Fit on the clean pool created before 2023-07-01; measure on every clean 5,000-row Jul-Aug batch:
  - clean: rows each f detector flags (all false by the benchmark's definition, though a real
    near-duplicate the sanitiser missed can be among them), and batches with any flag;
  - injected: the project's injector adds near-duplicates at 2 % (its own seeds, tag 104) to the same
    batches; recall of each detector and of the pipeline's union (rule OR stat).
Sep-Dec is never read. Read-only: nothing is written. About 3 minutes.

Usage: python -m project_b.audits.near_duplicate_change
"""
import json
import pickle

import numpy as np

from project_b import config, evaluate, inject
from project_b.data.load import make_batches
from project_b.detectors import duplicates as dup

SPLIT = "2023-07-01T00:00:00"
NEW_MAP = dict(dup.STREET_ABBREVIATIONS)
OLD_MAP = {k: v for k, v in NEW_MAP.items() if k not in ("PKW", "AV")}


def configure(after: bool) -> None:
    """Switch the detectors' module settings to the state before or after the change."""
    mapping = NEW_MAP if after else OLD_MAP
    dup.STREET_ABBREVIATIONS = mapping
    dup.ABBREVIATION_PATTERN = r"\b(" + "|".join(mapping) + r")\b"
    dup.DIFFERENT_BUILDING_IS_NOT_A_COPY = after


def main() -> dict:
    reference = evaluate.read_period(evaluate.DERIVED_DIR / evaluate.POOL_FILE, reference=True)
    assert (reference["created_date"] < config.REFERENCE_END).all()
    fit_part = reference[reference["created_date"] < SPLIT]
    batches = make_batches(reference[reference["created_date"] >= SPLIT].reset_index(drop=True), config.BATCH_SIZE)
    del reference
    with open(evaluate.DERIVED_DIR / evaluate.CONTEXT_FILE, "rb") as fh:
        ctx = pickle.load(fh)
    injected = [inject.inject(b, "f", np.random.default_rng([config.SEED, 104, i]), ctx, rate=config.ROW_INJECTION_RATE)
                for i, b in enumerate(batches)]

    out, flags_by = {}, {}
    for after in (False, True):
        configure(after)
        rule, stat = dup.RuleNearDuplicateKey(), dup.StatNearDuplicateSimilarity().fit(fit_part)
        clean_flags = {"rule": [], "stat": []}
        hit = {"rule": 0, "stat": 0, "union": 0}
        n_injected = 0
        for b, inj in zip(batches, injected):
            r, s = rule.detect(b).row_flags, stat.detect(b).row_flags
            clean_flags["rule"].append(r)
            clean_flags["stat"].append(s)
            labels = np.asarray(inj.row_labels, dtype=bool)
            ri, si = rule.detect(inj.df).row_flags, stat.detect(inj.df).row_flags
            n_injected += int(labels.sum())
            hit["rule"] += int((ri & labels).sum())
            hit["stat"] += int((si & labels).sum())
            hit["union"] += int(((ri | si) & labels).sum())
        rows = len(batches) * config.BATCH_SIZE
        union = [r | s for r, s in zip(clean_flags["rule"], clean_flags["stat"])]
        key = "after" if after else "before"
        flags_by[key] = clean_flags
        out[key] = {
            "stat_threshold": stat.threshold_,
            "clean_rows_flagged": {k: int(sum(f.sum() for f in v)) for k, v in clean_flags.items()} | {"union": int(sum(u.sum() for u in union))},
            "clean_row_fpr_union": sum(u.sum() for u in union) / rows,
            "clean_batches_any_flag_union": int(sum(u.any() for u in union)),
            "clean_batches_reaching_alert_threshold_7": int(sum(u.sum() >= 7 for u in union)),
            "injected_rows": n_injected,
            "recall": {k: v / n_injected for k, v in hit.items()},
        }
    # what changed on clean rows, per detector
    change = {}
    for det in ("rule", "stat"):
        new_only = sum(int((a & ~b).sum()) for a, b in zip(flags_by["after"][det], flags_by["before"][det]))
        gone = sum(int((b & ~a).sum()) for a, b in zip(flags_by["after"][det], flags_by["before"][det]))
        change[det] = {"newly_flagged": new_only, "no_longer_flagged": gone}
    # examples of the rule's new clean flags (the pairs the old normaliser split)
    configure(True)
    examples = []
    for b, (a, o) in zip(batches, zip(flags_by["after"]["rule"], flags_by["before"]["rule"])):
        for pos in np.flatnonzero(a & ~o)[:2]:
            examples.append(str(b["incident_address"].iat[pos]))
    result = {"calibration": {"fit": f"clean pool before {SPLIT[:10]}", "measure": f"{len(batches)} clean Jul-Aug batches"},
              **out, "clean_row_changes": change, "rule_new_flag_examples": examples[:8]}
    print(json.dumps(result, indent=1, default=float))
    return result


if __name__ == "__main__":
    main()