"""Immutable preparation-variable revisions with a session-owned live head."""

from __future__ import annotations

import copy
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, content_digest, loads_strict
from .preparation_program import validate_program_state


def program_variable_error(reason: str, message: str, status: int = 400) -> ContractValidationError:
    error = ContractValidationError(message)
    error.reason_code, error.status_code = reason, status
    return error


class ProgramVariableStore:
    """No registry keyed by a prompt revision: matching names keep session values."""

    def __init__(self, store: Any):
        self._store, self._connection = store, store._connection

    def current(self, session_id: str) -> dict:
        row = self._connection.execute(
            "SELECT revision FROM program_variable_heads WHERE session_id=?", (session_id,),
        ).fetchone()
        return self.read(session_id, row["revision"]) if row else {"revision": 0, "values": {}}

    def read(self, session_id: str, revision: int) -> dict:
        row = self._connection.execute(
            "SELECT payload FROM program_variable_states WHERE session_id=? AND revision=?",
            (session_id, revision),
        ).fetchone()
        if row is None:
            raise program_variable_error("storage_contract_violation", "Saved variable state is missing", 500)
        try:
            state = validate_program_state(loads_strict(row["payload"]))
            if state["revision"] != revision:
                raise ValueError("revision")
            return state
        except (ContractValidationError, ValueError) as exc:
            raise program_variable_error("storage_contract_violation", "Saved variable state is invalid", 500) from exc

    def receipt(self, session_id: str, key: str, request: dict) -> dict | None:
        saved = self._connection.execute(
            "SELECT digest,payload FROM program_variable_receipts WHERE idempotency_key=?", (key,),
        ).fetchone()
        if saved is None:
            return None
        if saved["digest"] != content_digest(request):
            raise program_variable_error("idempotency_conflict", "Variable key has another request", 409)
        result = loads_strict(saved["payload"])
        if (type(result) is not dict or set(result) != {"session_id", "state"}
                or result["session_id"] != session_id
                or canonical_bytes(self.read(session_id, result["state"]["revision"]))
                != canonical_bytes(result["state"])):
            raise program_variable_error("storage_contract_violation", "Saved variable receipt is invalid", 500)
        return result

    def write_in_transaction(self, session_id: str, state: dict, *, expected_revision: int,
                             key: str, request: dict, advance_head: bool = True) -> dict:
        if not self._connection.in_transaction:
            raise program_variable_error("storage_contract_violation", "Variable write requires a transaction", 500)
        if type(key) is not str or not 0 < len(key) <= 128:
            raise program_variable_error("invalid_request", "Variable operation key is invalid")
        state = validate_program_state(state)
        digest = content_digest(request)
        existing = self.receipt(session_id, key, request)
        if existing is not None:
            return existing
        current = self.current(session_id)
        if current["revision"] != expected_revision:
            raise program_variable_error("stale_revision", "Session variable revision changed", 409)
        result_state = copy.deepcopy(state)
        maximum = self._connection.execute(
            "SELECT MAX(revision) FROM program_variable_states WHERE session_id=?", (session_id,),
        ).fetchone()[0]
        result_state["revision"] = (maximum or 0) + 1
        self._connection.execute(
            "INSERT INTO program_variable_states(session_id,revision,payload) VALUES(?,?,?)",
            (session_id, result_state["revision"], canonical_bytes(result_state).decode("utf-8")),
        )
        if advance_head:
            self._connection.execute(
                "INSERT INTO program_variable_heads(session_id,revision) VALUES(?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision",
                (session_id, result_state["revision"]),
            )
        result = {"session_id": session_id, "state": result_state}
        self._connection.execute(
            "INSERT INTO program_variable_receipts(idempotency_key,digest,payload) VALUES(?,?,?)",
            (key, digest, canonical_bytes(result).decode("utf-8")),
        )
        return result

    def bind(self, owner_kind: str, owner_id: str, session_id: str, revision: int) -> None:
        if revision == 0:
            return
        self.read(session_id, revision)
        existing = self.reference(owner_kind, owner_id)
        if existing is not None:
            if existing != {"session_id": session_id, "revision": revision}:
                raise program_variable_error("immutable_conflict", "Saved variable binding differs", 409)
            return
        self._connection.execute(
            "INSERT INTO program_variable_bindings(owner_kind,owner_id,session_id,revision) VALUES(?,?,?,?)",
            (owner_kind, owner_id, session_id, revision),
        )

    def reference(self, owner_kind: str, owner_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT session_id,revision FROM program_variable_bindings WHERE owner_kind=? AND owner_id=?",
            (owner_kind, owner_id),
        ).fetchone()
        return dict(row) if row else None
