"""Errors raised at the versioned public-data boundary."""


class ContractValidationError(ValueError):
    """The supplied data or requested operation violates a public contract."""
