"""Definition-domain data interface and read-only evidence repository.

The definition functional layer sees exact evidence reads only.  Saving a
declaration stays in the owning package-selection transaction; this module
never calls ``check``/``register`` and never opens a write transaction.
"""

from __future__ import annotations

from contextlib import contextmanager

from .contract_errors import require
from .type_contract_store import TypeContractStore


class DefinitionRepository:
    """Read stored type-contract evidence without establishing a baseline."""

    def __init__(self, store):
        self._contracts = TypeContractStore(store)

    def type_evidence(self, scope, type_id, schema_version):
        return self._contracts.get(type_id, schema_version, scope)


class DefinitionDataSession:
    """Limited definition-data port bound to one existing store read."""

    def __init__(self, repository: DefinitionRepository):
        self._access = repository

    def type_evidence(self, scope, type_id, schema_version):
        return self._access.type_evidence(scope, type_id, schema_version)


class WorkflowDefinitionData:
    """Definition-layer interface over the shared data-management entry."""

    def __init__(self, management):
        self._management = management

    def transaction(self, records):
        return self._management.definition_transaction(records)

    @contextmanager
    def read(self):
        with self._management.read_definitions() as data:
            yield data


def require_evidence(view, reference):
    """Validate one evidence envelope against the exact reference its caller asked for."""
    require(view is not None, "definition_evidence_missing",
            "Exact type definition evidence is unavailable", 404)
    declaration = view.get("declaration", {})
    require((declaration.get("scope"), declaration.get("type_id"), declaration.get("schema_version"))
            == (reference["scope"], reference["type_id"], reference["schema_version"]),
            "definition_evidence_mismatch",
            "Type definition evidence differs from its exact reference", 409)
    return view
