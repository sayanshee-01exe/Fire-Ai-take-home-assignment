"""Implementation (c): rules compiled to Python by an LLM once; queries run that code, no LLM call.

The chosen artifact (compiler/compiled/raw_vN.py or reviewed.py) is checked by the AST allow-list
and run in a sandboxed subprocess. This engine reports exactly what the compiled code returned. It
never repairs, rounds or recomputes; malformed output gives an error result that keeps the output.
Only `saving` is derived here (from the two returned taxes), because TaxResult needs it.
"""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from project_a.compiler.safety import validate
from project_a.compiler.sandbox import Sandbox
from project_a.core.errors import InvalidInput, UnknownRuleVersion
from project_a.core.params import load_params
from project_a.core.schema import RegimeResult, TaxInput, TaxResult, validate_input

COMPILED = Path(__file__).resolve().parent.parent / "compiler" / "compiled"
COMPILED_FY = "2024-25"
NUMERIC = ("old_total_income", "old_tax", "new_total_income", "new_tax")


@dataclass
class CompiledTaxResult(TaxResult):
    error: Optional[str] = None
    returned: Any = None                     # exactly what compute() returned
    steps: List[Any] = field(default_factory=list)
    compute_s: float = 0.0                   # time inside compute() in the sandbox
    latency_s: float = 0.0                   # round trip to the sandbox (what a caller waits)
    artifact: str = ""


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check_output(out):
    if not isinstance(out, dict):
        return ["compute() returned %s, not a dict" % type(out).__name__]
    problems = ["%s missing or not a number (%r)" % (k, out.get(k)) for k in NUMERIC if not _number(out.get(k))]
    if out.get("best") not in ("old", "new"):
        problems.append("best is %r, not 'old' or 'new'" % (out.get("best"),))
    if not isinstance(out.get("steps"), list):
        problems.append("steps is not a list")
    return problems


class LLMCompiledEngine:
    name = "llm_compiled"

    def __init__(self, artifact="reviewed", compiled_dir=COMPILED, allow_helpers=False, **sandbox_limits):
        """allow_helpers=True is a diagnostic relaxation of the allow-list (see safety.validate); off by default."""
        self.artifact, self.allow_helpers = artifact, allow_helpers
        path = Path(compiled_dir) / ("%s.py" % artifact)
        self.source = path.read_text(encoding="utf-8")
        self.violations = validate(self.source, allow_helpers)
        self.sandbox = Sandbox(self.source, allow_helpers=allow_helpers, **sandbox_limits)
        manifest = Path(compiled_dir) / "manifest.json"
        self.artifact_meta = json.loads(manifest.read_text()).get(artifact, {}) if manifest.exists() else {}
        self.fy = self.artifact_meta.get("fy", COMPILED_FY)     # the year the artifact was compiled for

    def close(self):
        self.sandbox.close()

    def _error(self, msg, rule_version, returned=None, latency=0.0):
        return CompiledTaxResult(old=RegimeResult("old", None, None), new=RegimeResult("new", None, None),
                                 best=None, saving=None, reason="compiled code failed: " + msg,
                                 rule_version=rule_version, engine=self.name, error=msg, returned=returned,
                                 latency_s=latency, artifact=self.artifact)

    def calculate(self, inp: TaxInput) -> CompiledTaxResult:
        errors = validate_input(inp)
        if errors:
            raise InvalidInput(errors)
        if inp.fy != self.fy:                                 # year guard: generated code never reads fy
            raise UnknownRuleVersion("artifact %s was compiled for FY %s, not FY %s" % (self.artifact, self.fy, inp.fy))
        rule_version = load_params(inp.fy)["rule_version"]
        if self.violations:
            return self._error("rejected by the allow-list: " + "; ".join(self.violations), rule_version)
        r = self.sandbox.run(asdict(inp))
        if not r.get("ok"):
            return self._error(r.get("error", "unknown sandbox error"), rule_version, latency=r.get("roundtrip_s", 0.0))
        out = r["output"]
        problems = check_output(out)
        if problems:
            return self._error("; ".join(problems), rule_version, returned=out, latency=r["roundtrip_s"])
        old = RegimeResult("old", out["old_total_income"], out["old_tax"])      # exactly as returned
        new = RegimeResult("new", out["new_total_income"], out["new_tax"])
        return CompiledTaxResult(
            old=old, new=new, best=out["best"], saving=abs(out["old_tax"] - out["new_tax"]),
            reason="As returned by compiled artifact %s." % self.artifact, rule_version=rule_version,
            engine=self.name, returned=out, steps=out["steps"], compute_s=r.get("compute_s", 0.0),
            latency_s=r["roundtrip_s"], artifact=self.artifact)
