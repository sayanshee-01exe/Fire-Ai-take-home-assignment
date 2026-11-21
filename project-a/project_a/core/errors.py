class TaxError(Exception):
    """Base class. Errors are loud on purpose: wrong numbers must never be silent."""


class InvalidInput(TaxError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class UnknownRuleVersion(TaxError):
    """Raised when no rule parameters exist for the requested financial year."""
