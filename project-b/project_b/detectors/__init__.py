"""Detector registry: 12 problem types x 3 families = 36 detectors.

Each module lists its detector classes in a module-level DETECTORS list.
"""
from __future__ import annotations

import importlib

from project_b.core import Detector

MODULES = ["schema", "location", "duplicates", "completeness", "semantic", "pii"]


def detector_classes() -> list[type[Detector]]:
    classes: list[type[Detector]] = []
    for mod_name in MODULES:
        mod = importlib.import_module(f"project_b.detectors.{mod_name}")
        classes.extend(mod.DETECTORS)
    return classes


def all_detectors(families: tuple[str, ...] = ("rule", "stat", "llm"), problems: str = "abcdefghijkl") -> list[Detector]:
    """Fresh, unfitted instances, sorted by (problem, family)."""
    order = {"rule": 0, "stat": 1, "llm": 2}
    dets = [cls() for cls in detector_classes() if cls.family in families and cls.problem in problems]
    return sorted(dets, key=lambda d: (d.problem, order[d.family], d.name))
