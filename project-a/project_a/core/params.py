import json
import re
from functools import lru_cache
from pathlib import Path

from project_a.core.errors import UnknownRuleVersion

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"

REQUIRED_KEYS = (
    "fy", "rule_version", "rounding_unit", "cess_percent", "age_bands",
    "standard_deduction", "slabs", "rebate_87a", "surcharge", "hra", "limits",
)


@lru_cache(maxsize=None)
def load_params(fy):
    """Return the parameter dict for a financial year such as '2024-25'.

    There is deliberately no default year and no fallback: an unknown year raises.
    """
    if not isinstance(fy, str) or not re.fullmatch(r"\d{4}-\d{2}", fy):
        raise UnknownRuleVersion("invalid FY format: %r (expected like '2024-25')" % (fy,))
    path = RULES_DIR / ("params_fy" + fy.replace("-", "_") + ".json")
    if not path.exists():
        raise UnknownRuleVersion("no rule parameters for FY %s (looked for %s)" % (fy, path.name))
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise ValueError("%s is missing keys: %s" % (path.name, ", ".join(missing)))
    if data["fy"] != fy:
        raise ValueError("%s declares fy=%s, expected %s" % (path.name, data["fy"], fy))
    return data
