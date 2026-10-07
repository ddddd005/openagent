"""Errors raised at the versioned public-data boundary."""


class ContractValidationError(ValueError):
    """The supplied data or requested operation violates a public contract."""


class ModelRequestError(Exception):
    """A controlled request failure which must not authorize transport retry."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
