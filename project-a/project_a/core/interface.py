from typing import Protocol

from project_a.core.schema import TaxInput, TaxResult


class Engine(Protocol):
    """Every implementation (hand-coded, LLM at query time, LLM-compiled) exposes this."""

    name: str

    def calculate(self, inp: TaxInput) -> TaxResult:
        ...
