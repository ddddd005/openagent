"""Persist exact variable preparation state without owning workflow execution."""

from __future__ import annotations

from collections.abc import Callable
import copy
import re
import sqlite3
from typing import Any
from uuid import UUID

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, content_digest, loads_strict, validate_json_value
from .contracts_v2 import validate_record
from .prompt_variables import (
    PRESET_VARIABLE_NAMES,
    assign_variable,
    create_variable_snapshot,
    validate_variable_registry,
    validate_variable_snapshot,
)
from .storage import SqliteStore


_REF_FIELDS = {"workflow_session_id", "workflow_id", "registry_revision", "revision"}
_STATE_FIELDS = {
    "schema_version", "kind", *_REF_FIELDS, "values", "previous_ref", "source",
}
_BINDING_KINDS = frozenset({"input_snapshot", "state_snapshot", "workflow_commit", "fork_anchor"})
_PREPARATION_DIGEST = re.compile(r"json-v1:sha256:[0-9a-f]{64}")


def _error(reason: str, status: int, message: str) -> ContractValidationError:
    error = ContractValidationError(message)
    error.reason_code = reason
    error.status_code = status
    return error


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _error("invalid_request", 400, message)


def _corrupt(message: str) -> ContractValidationError:
    return _error("storage_contract_violation", 500, message)


def _uuid(value: Any) -> None:
    _require(type(value) is str, "Variable owner identity must be a UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise _error("invalid_request", 400, "Variable owner identity must be a UUID") from exc
    _require(parsed.version == 4 and str(parsed) == value,
             "Variable owner identity must be a canonical UUID")


def _revision(value: Any, *, allow_zero: bool = False) -> None:
    _require(type(value) is int and value >= (0 if allow_zero else 1),
             "Variable revision must be an integer in range")


def _key(value: Any) -> None:
    _require(type(value) is str and bool(value.strip()) and len(value) <= 128,
             "Variable preparation key must contain 1 to 128 characters")
    validate_json_value(value)


def _ref(value: Any) -> dict[str, Any]:
    _require(type(value) is dict and set(value) == _REF_FIELDS,
             "Variable state reference fields are invalid")
    _uuid(value["workflow_session_id"])
    _uuid(value["workflow_id"])
    _revision(value["registry_revision"])
    _revision(value["revision"])
    return copy.deepcopy(value)


def _identity(
    workflow_session_id: str, workflow_id: str, registry_revision: int,
) -> dict[str, Any]:
    _uuid(workflow_session_id)
    _uuid(workflow_id)
    _revision(registry_revision)
    return {
        "workflow_session_id": workflow_session_id,
        "workflow_id": workflow_id, "registry_revision": registry_revision,
    }


def _state_ref(state: dict[str, Any]) -> dict[str, Any]:
    return {field: state[field] for field in _REF_FIELDS}


class VariableStore:
    """Session-global values, immutable revisions and durable preparation receipts.

    Registry revisions address exact workflow definition revisions. Presets are
    supplied only when a consumer reads a pure VariableSnapshot; they are not
    stored as globally mutable session variables. The ``*_in_transaction``
    methods require the caller's transaction and use a savepoint, never commit
    or roll back that transaction. Workflow RunStart/fork integration is separate.
    """

    def __init__(self, store: SqliteStore) -> None:
        _require(isinstance(store, SqliteStore), "Variables require a workflow store")
        self._store = store
        self._connection = store._connection

    def _definition(self, workflow_id: str, registry_revision: int) -> dict[str, Any]:
        raw = self._store._get(
            "workflow_definition_revision",
            canonical_bytes([workflow_id, registry_revision]).decode("utf-8"),
        )
        if raw is None:
            raise _error("invalid_request", 400, "Variable workflow definition reference is missing")
        try:
            definition = validate_record("workflow_definition_revision", raw)
            if (
                definition["workflow_definition_id"] != workflow_id
                or definition["revision"] != registry_revision
            ):
                raise _corrupt("Stored variable workflow definition identity is inconsistent")
            return definition
        except ContractValidationError as exc:
            if getattr(exc, "status_code", None) == 500:
                raise
            raise _corrupt("Stored variable workflow definition is invalid") from exc

    def _owner(self, identity: dict[str, Any]) -> dict[str, Any]:
        definition = self._definition(identity["workflow_id"], identity["registry_revision"])
        raw = self._store._get("workflow_session", identity["workflow_session_id"])
        if raw is None:
            raise _error("invalid_request", 400, "Variable workflow session reference is missing")
        try:
            session = validate_record("workflow_session", raw)
            if session["workflow_session_id"] != identity["workflow_session_id"]:
                raise _corrupt("Stored variable session identity is inconsistent")
        except ContractValidationError as exc:
            if getattr(exc, "status_code", None) == 500:
                raise
            raise _corrupt("Stored variable session is invalid") from exc
        _require(
            session["workflow_definition_id"] == identity["workflow_id"]
            and session["definition_revision"] == identity["registry_revision"],
            "Variable registry does not belong to the workflow session definition",
        )
        return definition

    def _binding(self, identity: dict[str, Any], node_binding_id: str) -> None:
        _uuid(node_binding_id)
        definition = self._owner(identity)
        _require(node_binding_id in definition["bindings"],
                 "Variable consumer is not a binding of this workflow definition")
        raw = self._store._get(
            "node_binding", canonical_bytes([
                identity["workflow_id"], identity["registry_revision"], node_binding_id,
            ]).decode("utf-8"),
        )
        if raw is None:
            raise _error("invalid_request", 400, "Variable consumer binding reference is missing")
        try:
            binding = validate_record("node_binding", raw)
            if (
                binding["workflow_definition_id"] != identity["workflow_id"]
                or binding["workflow_definition_revision"] != identity["registry_revision"]
                or binding["node_binding_id"] != node_binding_id
            ):
                raise _corrupt("Stored variable binding identity is inconsistent")
        except ContractValidationError as exc:
            if getattr(exc, "status_code", None) == 500:
                raise
            raise _corrupt("Stored variable consumer binding is invalid") from exc

    def _source_scope(self, identity: dict[str, Any], reference: dict[str, Any]) -> None:
        _require(reference["workflow_id"] == identity["workflow_id"]
                 and reference["registry_revision"] == identity["registry_revision"],
                 "Frozen variable source belongs to a different registry")
        cursor = identity["workflow_session_id"]
        visited = set()
        while cursor != reference["workflow_session_id"]:
            if cursor in visited:
                raise _corrupt("Stored variable session ancestry is cyclic")
            visited.add(cursor)
            raw = self._store._get("workflow_session", cursor)
            if raw is None:
                raise _corrupt("Stored variable session ancestry reference is missing")
            session = validate_record("workflow_session", raw)
            _require(session["source"]["kind"] == "fork",
                     "Frozen variable source is not this session or its fork ancestor")
            cursor = session["source"]["source_workflow_session_id"]
        self._owner(reference)

    def get_registry(self, workflow_id: str, registry_revision: int) -> dict[str, Any] | None:
        _uuid(workflow_id)
        _revision(registry_revision)
        try:
            row = self._connection.execute(
                "SELECT payload FROM variable_registries "
                "WHERE workflow_id = ? AND registry_revision = ?",
                (workflow_id, registry_revision),
            ).fetchone()
            if row is None:
                return None
            try:
                registry = validate_variable_registry(loads_strict(row["payload"]))
                _uuid(registry["workflow_id"])
                if (
                    registry["workflow_id"] != workflow_id
                    or registry["revision"] != registry_revision
                ):
                    raise _corrupt("Stored variable registry identity is inconsistent")
                self._definition(workflow_id, registry_revision)
                return registry
            except ContractValidationError as exc:
                if getattr(exc, "status_code", None) == 500:
                    raise
                raise _corrupt("Stored variable registry is invalid") from exc
        except sqlite3.Error as exc:
            raise _error("storage_error", 500, "Variable registry read failed") from exc

    def _registry(self, identity: dict[str, Any]) -> dict[str, Any]:
        self._owner(identity)
        registry = self.get_registry(identity["workflow_id"], identity["registry_revision"])
        if registry is None:
            raise _error("invalid_request", 400, "Exact variable registry reference is missing")
        return registry

    def _within_transaction(self, action: Callable[[], Any]) -> Any:
        if not self._connection.in_transaction:
            raise _corrupt("Variable preparation requires the caller's transaction")
        self._connection.execute("SAVEPOINT variable_preparation")
        try:
            result = action()
            self._connection.execute("RELEASE SAVEPOINT variable_preparation")
            return result
        except Exception as exc:
            self._connection.execute("ROLLBACK TO SAVEPOINT variable_preparation")
            self._connection.execute("RELEASE SAVEPOINT variable_preparation")
            if isinstance(exc, sqlite3.Error):
                raise _error("storage_error", 500, "Variable preparation write failed") from exc
            raise

    def _atomic(self, action: Callable[[], Any]) -> Any:
        if self._connection.in_transaction:
            raise _corrupt("Atomic variable operation cannot own an existing transaction")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            result = action()
            self._store._inject("variable_before_commit")
            self._connection.execute("COMMIT")
            return result
        except Exception as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise _error("storage_error", 500, "Variable operation failed") from exc
            raise

    def register_registry_in_transaction(self, value: dict[str, Any]) -> dict[str, Any]:
        registry = validate_variable_registry(value)
        _uuid(registry["workflow_id"])

        def write() -> dict[str, Any]:
            self._definition(registry["workflow_id"], registry["revision"])
            existing = self.get_registry(registry["workflow_id"], registry["revision"])
            if existing is not None:
                if canonical_bytes(existing) != canonical_bytes(registry):
                    raise _error("immutable_conflict", 409,
                                 "Exact variable registry revision has different definitions")
                return existing
            self._store._inject("variable_before_registry_write")
            self._connection.execute(
                "INSERT INTO variable_registries (workflow_id, registry_revision, payload) "
                "VALUES (?, ?, ?)",
                (registry["workflow_id"], registry["revision"],
                 canonical_bytes(registry).decode("utf-8")),
            )
            self._store._inject("variable_after_registry_write")
            return copy.deepcopy(registry)

        return self._within_transaction(write)

    def register_registry(self, value: dict[str, Any]) -> dict[str, Any]:
        return self._atomic(lambda: self.register_registry_in_transaction(value))

    @staticmethod
    def _snapshot(
        state: dict[str, Any], registry: dict[str, Any], node_binding_id: str,
    ) -> dict[str, Any]:
        snapshot = create_variable_snapshot(
            registry, workflow_session_id=state["workflow_session_id"],
            node_binding_id=node_binding_id,
        )
        snapshot["values"].update(copy.deepcopy(state["values"]))
        return validate_variable_snapshot(snapshot)

    def _loaded_state(
        self, value: Any, reference: dict[str, Any], registry: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            _require(type(value) is dict and set(value) == _STATE_FIELDS,
                     "Variable state fields are invalid")
            _require(type(value["schema_version"]) is int and value["schema_version"] == 1
                     and value["kind"] == "session_variable_state",
                     "Variable state version or kind is invalid")
            _require(canonical_bytes(_ref(_state_ref(value))) == canonical_bytes(reference),
                     "Variable state identity differs from its columns")
            _require(type(value["values"]) is dict
                     and not PRESET_VARIABLE_NAMES.intersection(value["values"]),
                     "Session variable state must not persist consumer presets")
            self._snapshot(value, registry, "stored-state-validation")
            previous_ref = value["previous_ref"]
            if previous_ref is not None:
                previous_ref = _ref(previous_ref)
                _require(all(previous_ref[field] == reference[field] for field in (
                    "workflow_session_id", "workflow_id", "registry_revision",
                )) and previous_ref["revision"] == reference["revision"] - 1,
                         "Variable state predecessor is inconsistent")
            else:
                _require(reference["revision"] == 1, "Variable state predecessor is missing")
            source = value["source"]
            _require(type(source) is dict and type(source.get("kind")) is str,
                     "Variable state source is invalid")
            if source["kind"] == "preparation":
                _require(set(source) == {"kind", "idempotency_key"},
                         "Variable preparation source fields are invalid")
                _key(source["idempotency_key"])
            elif source["kind"] in {"fork", "frozen_snapshot"}:
                _require(set(source) == {"kind", "idempotency_key", "source_ref"},
                         "Variable fork source fields are invalid")
                _key(source["idempotency_key"])
                source_ref = _ref(source["source_ref"])
                _require(source_ref["workflow_id"] == reference["workflow_id"]
                         and source_ref["registry_revision"] == reference["registry_revision"]
                         and (source["kind"] != "fork" or (
                             source_ref["workflow_session_id"] != reference["workflow_session_id"]
                             and reference["revision"] == 1
                         )), "Variable snapshot source identity is inconsistent")
                if source["kind"] == "frozen_snapshot":
                    self._source_scope(reference, source_ref)
            else:
                raise _corrupt("Stored variable state source kind is invalid")
            return copy.deepcopy(value)
        except ContractValidationError as exc:
            if getattr(exc, "status_code", None) == 500:
                raise
            raise _corrupt("Stored variable state is invalid") from exc

    def get_state(self, reference: dict[str, Any]) -> dict[str, Any] | None:
        reference = _ref(reference)
        registry = self._registry(reference)
        try:
            row = self._connection.execute(
                "SELECT payload FROM variable_states WHERE workflow_session_id = ? "
                "AND workflow_id = ? AND registry_revision = ? AND revision = ?",
                tuple(reference[field] for field in (
                    "workflow_session_id", "workflow_id", "registry_revision", "revision",
                )),
            ).fetchone()
            if row is None:
                return None
            try:
                state = self._loaded_state(loads_strict(row["payload"]), reference, registry)
                links = [state["previous_ref"]] if state["previous_ref"] is not None else []
                if state["source"]["kind"] in {"fork", "frozen_snapshot"}:
                    links.append(state["source"]["source_ref"])
                for link in links:
                    linked = self._connection.execute(
                        "SELECT payload FROM variable_states WHERE workflow_session_id = ? "
                        "AND workflow_id = ? AND registry_revision = ? AND revision = ?",
                        tuple(link[field] for field in (
                            "workflow_session_id", "workflow_id", "registry_revision", "revision",
                        )),
                    ).fetchone()
                    if linked is None:
                        raise _corrupt("Stored variable state has a missing history reference")
                    self._owner(link)
                    self._loaded_state(loads_strict(linked["payload"]), link, registry)
                return state
            except ContractValidationError as exc:
                if getattr(exc, "status_code", None) == 500:
                    raise
                raise _corrupt("Stored variable state JSON is invalid") from exc
        except sqlite3.Error as exc:
            raise _error("storage_error", 500, "Variable state read failed") from exc

    def get_current(
        self, *, workflow_session_id: str, workflow_id: str, registry_revision: int,
    ) -> dict[str, Any] | None:
        identity = _identity(workflow_session_id, workflow_id, registry_revision)
        self._registry(identity)
        try:
            row = self._connection.execute(
                "SELECT revision FROM variable_heads WHERE workflow_session_id = ? "
                "AND workflow_id = ? AND registry_revision = ?",
                (workflow_session_id, workflow_id, registry_revision),
            ).fetchone()
            if row is None:
                return None
            try:
                _revision(row["revision"])
                state = self.get_state({**identity, "revision": row["revision"]})
                if state is None:
                    raise _corrupt("Stored variable head has no matching state")
                return state
            except ContractValidationError as exc:
                if getattr(exc, "status_code", None) == 500:
                    raise
                raise _corrupt("Stored variable head is invalid") from exc
        except sqlite3.Error as exc:
            raise _error("storage_error", 500, "Variable head read failed") from exc

    def read_snapshot(
        self, reference: dict[str, Any], *, node_binding_id: str,
    ) -> dict[str, Any]:
        reference = _ref(reference)
        self._binding(reference, node_binding_id)
        state = self.get_state(reference)
        if state is None:
            raise _error("not_found", 404, "Variable state reference was not found")
        return self._snapshot(state, self._registry(reference), node_binding_id)

    def snapshot_from_state(
        self, state: dict[str, Any], *, node_binding_id: str,
    ) -> dict[str, Any]:
        """Bind presets from a validated preparation result before its commit."""
        _require(type(state) is dict and _REF_FIELDS <= state.keys(),
                 "Variable preparation state has no identity")
        reference = _ref(_state_ref(state))
        self._binding(reference, node_binding_id)
        registry = self._registry(reference)
        state = self._loaded_state(state, reference, registry)
        return self._snapshot(state, registry, node_binding_id)

    def _bound_owner(self, owner_kind: str, owner_id: str) -> dict[str, Any]:
        _require(type(owner_kind) is str and owner_kind in _BINDING_KINDS,
                 "Variable binding owner kind is invalid")
        _uuid(owner_id)
        raw = self._store._get(owner_kind, owner_id)
        if raw is None:
            raise _error("invalid_request", 400, "Variable binding owner reference is missing")
        try:
            owner = validate_record(owner_kind, raw)
            identity_field = {
                "input_snapshot": "snapshot_id", "state_snapshot": "state_snapshot_id",
                "workflow_commit": "commit_id", "fork_anchor": "fork_anchor_id",
            }[owner_kind]
            if owner[identity_field] != owner_id:
                raise _corrupt("Stored variable binding owner identity is inconsistent")
            return owner
        except ContractValidationError as exc:
            if getattr(exc, "status_code", None) == 500:
                raise
            raise _corrupt("Stored variable binding owner is invalid") from exc

    def _binding_value(
        self, owner_kind: str, owner_id: str, reference: dict[str, Any],
    ) -> dict[str, Any]:
        reference = _ref(reference)
        owner = self._bound_owner(owner_kind, owner_id)
        session_id = (
            owner["source_workflow_session_id"]
            if owner_kind == "fork_anchor" else owner["workflow_session_id"]
        )
        _require(reference["workflow_session_id"] == session_id,
                 "Variable state binding belongs to another workflow session")
        state = self.get_state(reference)
        _require(state is not None, "Variable binding state reference is missing")
        return {
            "schema_version": 1, "kind": "variable_state_binding",
            "owner_kind": owner_kind, "owner_id": owner_id,
            "workflow_session_id": session_id, "state_ref": reference,
        }

    def get_bound_state_ref(
        self, owner_kind: str, owner_id: str,
    ) -> dict[str, Any] | None:
        """Read an immutable saved snapshot/commit/anchor binding, never latest."""
        self._bound_owner(owner_kind, owner_id)
        try:
            row = self._connection.execute(
                "SELECT workflow_session_id, payload FROM variable_bindings "
                "WHERE owner_kind = ? AND owner_id = ?", (owner_kind, owner_id),
            ).fetchone()
            if row is None:
                return None
            try:
                value = loads_strict(row["payload"])
                _require(type(value) is dict and set(value) == {
                    "schema_version", "kind", "owner_kind", "owner_id",
                    "workflow_session_id", "state_ref",
                }, "Variable binding fields are invalid")
                _require(type(value["schema_version"]) is int and value["schema_version"] == 1
                         and value["kind"] == "variable_state_binding",
                         "Variable binding version or kind is invalid")
                expected = self._binding_value(owner_kind, owner_id, value["state_ref"])
                _require(canonical_bytes(value) == canonical_bytes(expected)
                         and row["workflow_session_id"] == expected["workflow_session_id"],
                         "Variable binding correspondence is inconsistent")
                return copy.deepcopy(expected["state_ref"])
            except ContractValidationError as exc:
                if getattr(exc, "status_code", None) == 500:
                    raise
                raise _corrupt("Stored variable state binding is invalid") from exc
        except sqlite3.Error as exc:
            raise _error("storage_error", 500, "Variable binding read failed") from exc

    def bind_state_in_transaction(
        self, owner_kind: str, owner_id: str, reference: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind a newly saved stable owner inside its existing bundle transaction."""
        def write() -> dict[str, Any]:
            value = self._binding_value(owner_kind, owner_id, reference)
            existing = self.get_bound_state_ref(owner_kind, owner_id)
            if existing is not None:
                if canonical_bytes(existing) != canonical_bytes(value["state_ref"]):
                    raise _error("immutable_conflict", 409,
                                 "Stable owner already has a different variable state binding")
                return copy.deepcopy(value)
            self._store._inject("variable_before_binding_write")
            self._connection.execute(
                "INSERT INTO variable_bindings (owner_kind, owner_id, workflow_session_id, payload) "
                "VALUES (?, ?, ?, ?)",
                (owner_kind, owner_id, value["workflow_session_id"],
                 canonical_bytes(value).decode("utf-8")),
            )
            self._store._inject("variable_after_binding_write")
            return copy.deepcopy(value)

        return self._within_transaction(write)

    @staticmethod
    def _assignments(value: Any) -> list[dict[str, Any]]:
        validate_json_value(value)
        _require(type(value) is list, "Variable assignments must be an ordered JSON array")
        for assignment in value:
            _require(type(assignment) is dict
                     and set(assignment) == {"node_id", "name", "value"},
                     "Variable assignment fields are invalid")
            _require(type(assignment["node_id"]) is str
                     and bool(assignment["node_id"].strip()),
                     "Variable assignment requires an explicit processing node identity")
        return copy.deepcopy(value)

    def _request(self, value: Any) -> dict[str, Any]:
        validate_json_value(value)
        _require(type(value) is dict and type(value.get("operation")) is str
                 and value["operation"] in {"prepare", "fork", "prepare_snapshot"},
                 "Variable preparation request is invalid")
        identity_fields = {"workflow_session_id", "workflow_id", "registry_revision"}
        common = {"operation", *identity_fields, "expected_revision"}
        extra = {
            "prepare": {"assignments"}, "fork": {"source_ref"},
            "prepare_snapshot": {"source_ref", "snapshot"},
        }[value["operation"]]
        fields = common | extra
        optional = {"preparation_digest"} if value["operation"] != "fork" else set()
        _require(fields <= set(value) <= fields | optional, "Variable request fields are invalid")
        if "preparation_digest" in value:
            _require(type(value["preparation_digest"]) is str
                     and _PREPARATION_DIGEST.fullmatch(value["preparation_digest"]) is not None,
                     "Variable preparation digest is invalid")
        _identity(*(value[field] for field in (
            "workflow_session_id", "workflow_id", "registry_revision",
        )))
        _revision(value["expected_revision"], allow_zero=True)
        if value["operation"] == "prepare":
            self._assignments(value["assignments"])
        elif value["operation"] == "fork":
            source = _ref(value["source_ref"])
            _require(value["expected_revision"] == 0
                     and source["workflow_session_id"] != value["workflow_session_id"]
                     and source["workflow_id"] == value["workflow_id"]
                     and source["registry_revision"] == value["registry_revision"],
                     "Variable fork must clone an explicit same-definition source into a new session")
        else:
            _ref(value["source_ref"])
            snapshot = validate_variable_snapshot(value["snapshot"])
            _require(snapshot["workflow_session_id"] == value["workflow_session_id"]
                     and snapshot["registry"]["workflow_id"] == value["workflow_id"]
                     and snapshot["registry"]["revision"] == value["registry_revision"],
                     "Frozen variable snapshot belongs to another preparation scope")
        return copy.deepcopy(value)

    def _derive(
        self, request: dict[str, Any], key: str, before: dict[str, Any] | None,
        registry: dict[str, Any],
    ) -> dict[str, Any]:
        identity = {field: request[field] for field in (
            "workflow_session_id", "workflow_id", "registry_revision",
        )}
        if request["operation"] in {"fork", "prepare_snapshot"}:
            source_state = self.get_state(request["source_ref"])
            if source_state is None:
                raise _error("invalid_request", 400, "Variable fork source reference is missing")
            if request["operation"] == "prepare_snapshot":
                self._source_scope(identity, request["source_ref"])
                snapshot = validate_variable_snapshot(request["snapshot"])
                self._binding(identity, snapshot["node_binding_id"])
                _require(canonical_bytes(snapshot["registry"]) == canonical_bytes(registry),
                         "Frozen variable snapshot registry differs from its stored exact definition")
                values = {name: copy.deepcopy(entry) for name, entry in snapshot["values"].items()
                          if name not in PRESET_VARIABLE_NAMES}
            else:
                values = copy.deepcopy(source_state["values"])
            source = {"kind": "fork" if request["operation"] == "fork" else "frozen_snapshot",
                      "idempotency_key": key,
                      "source_ref": request["source_ref"]}
        else:
            if before is None:
                snapshot = create_variable_snapshot(
                    registry, workflow_session_id=identity["workflow_session_id"],
                    node_binding_id="stored-state-preparation",
                )
            else:
                snapshot = self._snapshot(before, registry, "stored-state-preparation")
            for assignment in request["assignments"]:
                snapshot = assign_variable(
                    snapshot, assignment["name"], assignment["value"],
                    node_id=assignment["node_id"],
                )
            values = {name: entry for name, entry in snapshot["values"].items()
                      if name not in PRESET_VARIABLE_NAMES}
            source = {"kind": "preparation", "idempotency_key": key}
        state = {
            "schema_version": 1, "kind": "session_variable_state", **identity,
            "revision": request["expected_revision"] + 1, "values": values,
            "previous_ref": _state_ref(before) if before is not None else None,
            "source": source,
        }
        return self._loaded_state(state, _state_ref(state), registry)

    def _receipt(self, key: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT digest, payload FROM variable_preparations WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        try:
            result = loads_strict(row["payload"])
            _require(type(result) is dict and set(result) == {
                "schema_version", "kind", "idempotency_key", "request", "before", "after",
            }, "Variable receipt fields are invalid")
            _require(type(result["schema_version"]) is int and result["schema_version"] == 1
                     and result["kind"] == "variable_preparation_receipt"
                     and result["idempotency_key"] == key,
                     "Variable receipt identity or version is invalid")
            request = self._request(result["request"])
            if content_digest(request) != row["digest"]:
                raise _corrupt("Stored variable receipt digest is inconsistent")
            registry = self._registry(request)
            before = result["before"]
            expected = request["expected_revision"]
            if expected == 0:
                _require(before is None, "Initial variable receipt must have no predecessor")
            else:
                reference = {
                    field: request[field] for field in (
                        "workflow_session_id", "workflow_id", "registry_revision",
                    )
                }
                prior = self.get_state({**reference, "revision": expected})
                _require(prior is not None and canonical_bytes(prior) == canonical_bytes(before),
                         "Variable receipt predecessor has no matching immutable state")
            derived = self._derive(request, key, before, registry)
            saved = self.get_state(_state_ref(derived))
            _require(saved is not None
                     and canonical_bytes(saved) == canonical_bytes(derived)
                     and canonical_bytes(result["after"]) == canonical_bytes(derived),
                     "Variable receipt result differs from its saved preparation")
            return copy.deepcopy(result)
        except ContractValidationError as exc:
            if getattr(exc, "status_code", None) == 500:
                raise
            raise _corrupt("Stored variable preparation receipt is invalid") from exc

    def read_preparation(self, idempotency_key: str) -> dict[str, Any] | None:
        _key(idempotency_key)
        try:
            return self._receipt(idempotency_key)
        except sqlite3.Error as exc:
            raise _error("storage_error", 500, "Variable receipt read failed") from exc

    def _prepare_in_transaction(self, request: dict[str, Any], key: str) -> dict[str, Any]:
        request = self._request(request)
        _key(key)

        def write() -> dict[str, Any]:
            registry = self._registry(request)
            receipt = self._receipt(key)
            if receipt is not None:
                if canonical_bytes(receipt["request"]) != canonical_bytes(request):
                    raise _error("idempotency_conflict", 409,
                                 "Variable preparation key has a different request")
                return receipt
            identity = {field: request[field] for field in (
                "workflow_session_id", "workflow_id", "registry_revision",
            )}
            before = self.get_current(**identity)
            current = before["revision"] if before is not None else 0
            if current != request["expected_revision"]:
                raise _error("stale_revision", 409, "Session variable revision conflict")
            after = self._derive(request, key, before, registry)
            self._store._inject("variable_before_state_write")
            self._connection.execute(
                "INSERT INTO variable_states "
                "(workflow_session_id, workflow_id, registry_revision, revision, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (*identity.values(), after["revision"], canonical_bytes(after).decode("utf-8")),
            )
            self._store._inject("variable_after_state_write")
            self._connection.execute(
                "INSERT INTO variable_heads "
                "(workflow_session_id, workflow_id, registry_revision, revision) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (workflow_session_id, workflow_id, registry_revision) "
                "DO UPDATE SET revision = excluded.revision",
                (*identity.values(), after["revision"]),
            )
            self._store._inject("variable_after_head_write")
            receipt = {
                "schema_version": 1, "kind": "variable_preparation_receipt",
                "idempotency_key": key, "request": request,
                "before": before, "after": after,
            }
            self._connection.execute(
                "INSERT INTO variable_preparations (idempotency_key, digest, payload) VALUES (?, ?, ?)",
                (key, content_digest(request), canonical_bytes(receipt).decode("utf-8")),
            )
            self._store._inject("variable_after_receipt_write")
            return copy.deepcopy(receipt)

        return self._within_transaction(write)

    def prepare_in_transaction(
        self, *, workflow_session_id: str, workflow_id: str, registry_revision: int,
        expected_revision: int, assignments: list[dict[str, Any]], idempotency_key: str,
        preparation_digest: str | None = None,
    ) -> dict[str, Any]:
        """Apply ordered explicit assignments within the caller's durable boundary."""
        return self._prepare_in_transaction({
            "operation": "prepare",
            **_identity(workflow_session_id, workflow_id, registry_revision),
            "expected_revision": expected_revision, "assignments": assignments,
            **({"preparation_digest": preparation_digest} if preparation_digest is not None else {}),
        }, idempotency_key)

    def prepare(
        self, *, workflow_session_id: str, workflow_id: str, registry_revision: int,
        expected_revision: int, assignments: list[dict[str, Any]], idempotency_key: str,
        preparation_digest: str | None = None,
    ) -> dict[str, Any]:
        """Convenience atomic preparation, not a WorkflowService startup operation."""
        return self._atomic(lambda: self.prepare_in_transaction(
            workflow_session_id=workflow_session_id, workflow_id=workflow_id,
            registry_revision=registry_revision, expected_revision=expected_revision,
            assignments=assignments, idempotency_key=idempotency_key,
            preparation_digest=preparation_digest,
        ))

    def fork_in_transaction(
        self, *, source_ref: dict[str, Any], workflow_session_id: str,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        """Clone the specified stable state; copy no live execution or consumer preset."""
        reference = _ref(source_ref)
        return self._prepare_in_transaction({
            "operation": "fork", "workflow_session_id": workflow_session_id,
            "workflow_id": reference["workflow_id"],
            "registry_revision": reference["registry_revision"],
            "expected_revision": expected_revision, "source_ref": reference,
        }, idempotency_key)

    def fork(
        self, *, source_ref: dict[str, Any], workflow_session_id: str,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        return self._atomic(lambda: self.fork_in_transaction(
            source_ref=source_ref, workflow_session_id=workflow_session_id,
            expected_revision=expected_revision, idempotency_key=idempotency_key,
        ))

    def prepare_snapshot_in_transaction(
        self, snapshot: dict[str, Any], *, expected_revision: int,
        idempotency_key: str, source_ref: dict[str, Any],
        preparation_digest: str | None = None,
    ) -> dict[str, Any]:
        """Import exact frozen values; the live Head supplies CAS, never values."""
        snapshot = validate_variable_snapshot(snapshot)
        return self._prepare_in_transaction({
            "operation": "prepare_snapshot",
            **_identity(
                snapshot["workflow_session_id"], snapshot["registry"]["workflow_id"],
                snapshot["registry"]["revision"],
            ),
            "expected_revision": expected_revision, "snapshot": snapshot,
            "source_ref": source_ref,
            **({"preparation_digest": preparation_digest} if preparation_digest is not None else {}),
        }, idempotency_key)

    def prepare_snapshot(
        self, snapshot: dict[str, Any], *, expected_revision: int,
        idempotency_key: str, source_ref: dict[str, Any],
        preparation_digest: str | None = None,
    ) -> dict[str, Any]:
        return self._atomic(lambda: self.prepare_snapshot_in_transaction(
            snapshot, expected_revision=expected_revision,
            idempotency_key=idempotency_key, source_ref=source_ref,
            preparation_digest=preparation_digest,
        ))
