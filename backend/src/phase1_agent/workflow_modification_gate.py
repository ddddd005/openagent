"""Process-shared per-workflow modification gate.

The gate is deliberately process-local.  Every workflow service created in the
same process over the same database file (execution and management instances)
shares this registry, so a real definition modification in flight is visible to
all of them.  Separate processes are still protected by the existing SQLite
write transaction and the in-transaction CAS; no distributed lock is built.

A busy workflow is rejected immediately; callers never queue and never retry on
their own.  The gate is released when the guarded command returns, i.e. after
its transaction committed or rolled back, not when the frontend accepted the
result or closed its evidence.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from uuid import uuid4

from .contract_errors import DiagnosticError


_registry_lock = Lock()
# (canonical database path, workflow definition identity) -> owner token
_in_flight: dict[tuple[str, str], str] = {}


def _database_key(database) -> str:
    return str(Path(database).resolve())


def _busy(workflow_definition_id) -> DiagnosticError:
    return DiagnosticError(
        "workflow_modification_busy", "Workflow is already being modified", status=409,
        workflow_definition_id=workflow_definition_id)


@contextmanager
def modification_gate(database, workflow_definition_id):
    """Acquire the process-wide modification entitlement or reject immediately."""
    key = (_database_key(database), str(workflow_definition_id))
    token = str(uuid4())
    with _registry_lock:
        if key in _in_flight:
            raise _busy(workflow_definition_id)
        _in_flight[key] = token
    try:
        yield
    finally:
        with _registry_lock:
            if _in_flight.get(key) == token:
                del _in_flight[key]
