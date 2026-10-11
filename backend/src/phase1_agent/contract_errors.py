"""Neutral diagnostics raised at versioned public-data boundaries."""

from typing import Any
from uuid import UUID
from copy import deepcopy


class ContractValidationError(ValueError):
    """The supplied data or requested operation violates a public contract."""


class DiagnosticError(ContractValidationError):
    def __init__(self, code: str, message: str, *, node_id: str | None = None,
                 port_id: str | None = None, edge_id: str | None = None,
                 dependent_outputs: list[str] | None = None, status: int = 400,
                 **details: Any):
        super().__init__(message)
        self.reason_code, self.status_code = code, status
        self.diagnostics = [{
            "code": code, "message": message, "node_id": node_id,
            "port_id": port_id, "edge_id": edge_id,
            "dependent_outputs": dependent_outputs or [], **details,
        }]


def require(condition: bool, code: str, message: str, status: int = 400, **location: Any) -> None:
    if not condition:
        raise DiagnosticError(code, message, status=status, **location)


def uuid_value(value: Any) -> bool:
    try:
        parsed = UUID(value) if type(value) is str else None
    except (ValueError, TypeError, AttributeError):
        parsed = None
    return parsed is not None and parsed.version == 4 and str(parsed) == value


def uuid4_string(value: Any) -> str:
    require(uuid_value(value), "graph_invalid_document", "Graph identities must be canonical UUID4")
    return value


class ModelRequestError(Exception):
    """A controlled request failure which must not authorize transport retry."""

    def __init__(self, code: str, *, public_diagnostic=None):
        super().__init__(code)
        self.code = code
        self.public_diagnostic = (public_diagnostic
                                  if isinstance(public_diagnostic, PublicDiagnostic) else None)


class PublicDiagnostic:
    """Explicitly sanitized details supplied by a trusted diagnostic boundary."""

    def __init__(self, value: dict):
        self._value = deepcopy(value)

    def to_dict(self) -> dict:
        return deepcopy(self._value)
