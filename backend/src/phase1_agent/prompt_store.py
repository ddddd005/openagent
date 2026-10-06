"""Immutable prompt revisions and a CAS-selectable catalog in the workflow store."""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import Any
from uuid import UUID

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, content_digest, loads_strict, validate_json_value
from .prompt_config import (
    expand_prompt_config,
    prompt_record_identity,
    validate_prompt_record,
)
from .storage import SqliteStore
from .prompt_errors import PromptProcessingError


_KINDS = frozenset(("item", "group", "config"))


def _error(reason: str, status: int, message: str) -> ContractValidationError:
    error = ContractValidationError(message)
    error.reason_code = reason
    error.status_code = status
    return error


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _error("invalid_request", 400, message)


def _identity(kind: str, definition_id: str) -> None:
    _require(type(kind) is str and kind in _KINDS, "Invalid prompt configuration kind")
    _require(type(definition_id) is str, "Invalid prompt configuration identity")
    try:
        parsed = UUID(definition_id)
    except (ValueError, AttributeError) as exc:
        raise _error("invalid_request", 400, "Invalid prompt configuration identity") from exc
    _require(parsed.version == 4 and str(parsed) == definition_id,
             "Invalid prompt configuration identity")


def _revision(revision: int, *, allow_zero: bool = False) -> None:
    _require(type(revision) is int and revision >= (0 if allow_zero else 1),
             "Invalid prompt configuration revision")


def _corrupt(message: str) -> ContractValidationError:
    return _error("storage_contract_violation", 500, message)


class PromptConfigStore:
    """Share the workflow connection, transaction ownership and fault injector.

    ``expected_revision`` addresses the catalog head, not a definition revision.
    Hiding a definition changes only the catalog revision. Exact historical
    references remain readable and a later definition write can select it again.
    """

    def __init__(self, store: SqliteStore) -> None:
        _require(isinstance(store, SqliteStore), "Prompt catalog requires a workflow store")
        self._store = store
        self._connection = store._connection

    @staticmethod
    def _loaded_record(
        kind: str, payload: str, definition_id: str, revision: int,
    ) -> dict[str, Any]:
        try:
            result = validate_prompt_record(kind, loads_strict(payload))
            if prompt_record_identity(kind, result) != (definition_id, revision):
                raise _corrupt("Stored prompt configuration identity is inconsistent")
            return result
        except ContractValidationError as exc:
            if getattr(exc, "reason_code", None) == "storage_contract_violation":
                raise
            raise _corrupt("Stored prompt configuration is invalid") from exc

    @staticmethod
    def _loaded_head(
        kind: str, definition_id: str, definition_revision: int,
        catalog_revision: int, selectable: Any, *, stored_integer: bool = True,
    ) -> dict[str, Any]:
        try:
            _identity(kind, definition_id)
            _revision(definition_revision)
            _revision(catalog_revision)
        except ContractValidationError as exc:
            raise _corrupt("Stored prompt catalog head is invalid") from exc
        valid_selectable = (
            type(selectable) is int and selectable in (0, 1)
            if stored_integer else type(selectable) is bool
        )
        if catalog_revision < definition_revision or not valid_selectable:
            raise _corrupt("Stored prompt catalog head is invalid")
        return {
            "kind": kind, "id": definition_id,
            "definition_revision": definition_revision,
            "catalog_revision": catalog_revision, "selectable": bool(selectable),
        }

    def get_revision(
        self, kind: str, definition_id: str, revision: int,
    ) -> dict[str, Any] | None:
        _identity(kind, definition_id)
        _revision(revision)
        try:
            row = self._connection.execute(
                "SELECT payload FROM prompt_revisions "
                "WHERE kind = ? AND definition_id = ? AND revision = ?",
                (kind, definition_id, revision),
            ).fetchone()
            if row is None:
                return None
            return self._loaded_record(kind, row["payload"], definition_id, revision)
        except sqlite3.Error as exc:
            raise _error("storage_error", 500, "Prompt catalog read failed") from exc

    def get_head(self, kind: str, definition_id: str) -> dict[str, Any] | None:
        _identity(kind, definition_id)
        try:
            row = self._connection.execute(
                "SELECT definition_revision, catalog_revision, selectable FROM prompt_heads "
                "WHERE kind = ? AND definition_id = ?",
                (kind, definition_id),
            ).fetchone()
            if row is None:
                return None
            head = self._loaded_head(
                kind, definition_id, row["definition_revision"],
                row["catalog_revision"], row["selectable"],
            )
            if self.get_revision(kind, definition_id, head["definition_revision"]) is None:
                raise _corrupt("Stored prompt catalog head has no definition")
            return head
        except sqlite3.Error as exc:
            raise _error("storage_error", 500, "Prompt catalog read failed") from exc

    def list_current(
        self, kind: str, *, include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        _require(type(kind) is str and kind in _KINDS, "Invalid prompt configuration kind")
        _require(type(include_hidden) is bool, "Invalid prompt catalog filter")
        try:
            rows = self._connection.execute(
                "SELECT h.definition_id, h.definition_revision, h.catalog_revision, "
                "h.selectable, r.payload FROM prompt_heads h LEFT JOIN prompt_revisions r "
                "ON h.kind = r.kind AND h.definition_id = r.definition_id "
                "AND h.definition_revision = r.revision WHERE h.kind = ? "
                "AND (? OR h.selectable = 1) ORDER BY h.definition_id",
                (kind, int(include_hidden)),
            ).fetchall()
            result = []
            for row in rows:
                head = self._loaded_head(
                    kind, row["definition_id"], row["definition_revision"],
                    row["catalog_revision"], row["selectable"],
                )
                if row["payload"] is None:
                    raise _corrupt("Stored prompt catalog head has no definition")
                record = self._loaded_record(
                    kind, row["payload"], row["definition_id"], row["definition_revision"],
                )
                result.append({"record": record, "head": head})
            return result
        except sqlite3.Error as exc:
            raise _error("storage_error", 500, "Prompt catalog read failed") from exc

    def _resolve(self, kind: str, definition_id: str, revision: int) -> dict[str, Any]:
        result = self.get_revision(kind, definition_id, revision)
        if result is None:
            raise _error("invalid_request", 400, "Prompt configuration reference is missing")
        if kind == "group":
            try:
                self._check_references(kind, result)
            except ContractValidationError as exc:
                if getattr(exc, "status_code", None) == 500:
                    raise
                raise _corrupt("Stored prompt group references are invalid") from exc
        return result

    def resolve_config(self, config_id: str, revision: int) -> list[dict[str, Any]]:
        config = self.get_revision("config", config_id, revision)
        if config is None:
            raise _error("not_found", 404, "Prompt configuration was not found")
        try:
            return self._expand(config)
        except ContractValidationError as exc:
            if getattr(exc, "status_code", None) == 500:
                raise
            raise _corrupt("Stored prompt configuration references are invalid") from exc

    def _expand(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        return expand_prompt_config(
            config,
            lambda identity, revision: self._resolve("item", identity, revision),
            lambda identity, revision: self._resolve("group", identity, revision),
        )

    def _check_references(self, kind: str, record: dict[str, Any]) -> None:
        if kind == "group":
            for member in record["members"]:
                item = self._resolve("item", member["item_id"], member["revision"])
                item.update(member["overrides"])
                validate_prompt_record("item", item)
        elif kind == "config":
            self._expand(record)

    def _receipt(self, key: str, digest: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT digest, result_payload FROM prompt_mutations WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        try:
            result = loads_strict(row["result_payload"])
            if type(result) is not dict or set(result) != {"record", "head"}:
                raise _corrupt("Stored prompt mutation receipt is invalid")
            head = result["head"]
            head_fields = {
                "kind", "id", "definition_revision", "catalog_revision", "selectable",
            }
            if type(head) is not dict or set(head) != head_fields:
                raise _corrupt("Stored prompt mutation receipt is invalid")
            head = self._loaded_head(
                head["kind"], head["id"], head["definition_revision"],
                head["catalog_revision"], head["selectable"], stored_integer=False,
            )
            record = validate_prompt_record(head["kind"], result["record"])
            if prompt_record_identity(head["kind"], record) != (
                head["id"], head["definition_revision"],
            ):
                raise _corrupt("Stored prompt mutation receipt identity is inconsistent")
            saved = self.get_revision(head["kind"], head["id"], head["definition_revision"])
            if saved is None or canonical_bytes(saved) != canonical_bytes(record):
                raise _corrupt("Stored prompt mutation receipt has no matching definition")
            request = {
                "operation": "write_revision" if head["selectable"] else "delete",
                "kind": head["kind"], "expected_revision": head["catalog_revision"] - 1,
            }
            if head["selectable"]:
                request["record"] = record
            else:
                request["id"] = head["id"]
            if row["digest"] != content_digest(request):
                raise _corrupt("Stored prompt mutation receipt digest is inconsistent")
        except ContractValidationError as exc:
            if getattr(exc, "status_code", None) == 500:
                raise
            raise _corrupt("Stored prompt mutation receipt is invalid") from exc
        if row["digest"] != digest:
            raise _error("idempotency_conflict", 409,
                         "Idempotency key has a different prompt catalog request")
        return {"record": record, "head": head}

    def _save_receipt(self, key: str, digest: str, result: dict[str, Any]) -> None:
        self._connection.execute(
            "INSERT INTO prompt_mutations (idempotency_key, digest, result_payload) "
            "VALUES (?, ?, ?)",
            (key, digest, canonical_bytes(result).decode("utf-8")),
        )

    def _write_head(
        self, kind: str, definition_id: str, definition_revision: int,
        catalog_revision: int, selectable: bool,
    ) -> dict[str, Any]:
        self._connection.execute(
            "INSERT INTO prompt_heads "
            "(kind, definition_id, definition_revision, catalog_revision, selectable) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (kind, definition_id) DO UPDATE SET "
            "definition_revision = excluded.definition_revision, "
            "catalog_revision = excluded.catalog_revision, selectable = excluded.selectable",
            (kind, definition_id, definition_revision, catalog_revision, int(selectable)),
        )
        self._store._inject("prompt_after_head_write")
        return {
            "kind": kind, "id": definition_id,
            "definition_revision": definition_revision,
            "catalog_revision": catalog_revision, "selectable": selectable,
        }

    @staticmethod
    def _request(expected_revision: int, idempotency_key: str) -> None:
        _revision(expected_revision, allow_zero=True)
        _require(type(idempotency_key) is str and bool(idempotency_key.strip())
                 and len(idempotency_key) <= 128, "Idempotency key must contain 1 to 128 characters")
        try:
            validate_json_value(idempotency_key)
        except ContractValidationError as exc:
            raise _error("invalid_request", 400, "Idempotency key must be valid text") from exc

    @staticmethod
    def _cas(head: dict[str, Any] | None, expected_revision: int) -> None:
        current = head["catalog_revision"] if head is not None else 0
        if current != expected_revision:
            raise _error("stale_revision", 409, "Prompt catalog revision conflict")

    def _rollback(self, exc: Exception) -> None:
        if self._connection.in_transaction:
            self._connection.execute("ROLLBACK")
        if isinstance(exc, sqlite3.Error):
            raise _error("storage_error", 500, "Prompt catalog operation failed") from exc

    def _available_transaction(self) -> None:
        if self._connection.in_transaction:
            raise _corrupt("Prompt catalog requires ownership of its transaction")

    def write_revision(
        self, kind: str, value: Mapping[str, Any], *,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        self._request(expected_revision, idempotency_key)
        try:
            record = validate_prompt_record(kind, value)
            definition_id, revision = prompt_record_identity(kind, record)
        except ContractValidationError as exc:
            if isinstance(exc, PromptProcessingError):
                exc.status_code, exc.reason_code = 400, exc.code
                raise
            raise _error("invalid_request", 400, "Invalid prompt configuration") from exc
        digest = content_digest({
            "operation": "write_revision", "kind": kind, "record": record,
            "expected_revision": expected_revision,
        })
        self._available_transaction()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            receipt = self._receipt(idempotency_key, digest)
            if receipt is not None:
                self._connection.execute("COMMIT")
                return receipt
            head = self.get_head(kind, definition_id)
            self._cas(head, expected_revision)
            previous = head["definition_revision"] if head is not None else 0
            _require(revision == previous + 1, "Prompt definition revision must advance by one")
            self._check_references(kind, record)
            self._store._inject("prompt_before_write")
            self._connection.execute(
                "INSERT INTO prompt_revisions (kind, definition_id, revision, payload) "
                "VALUES (?, ?, ?, ?)",
                (kind, definition_id, revision, canonical_bytes(record).decode("utf-8")),
            )
            self._store._inject("prompt_after_revision_write")
            new_head = self._write_head(kind, definition_id, revision, expected_revision + 1, True)
            result = {"record": record, "head": new_head}
            self._save_receipt(idempotency_key, digest, result)
            self._store._inject("prompt_before_commit")
            self._connection.execute("COMMIT")
            return result
        except Exception as exc:
            self._rollback(exc)
            if isinstance(exc, ContractValidationError) and not hasattr(exc, "reason_code"):
                raise _error("invalid_request", 400, "Invalid prompt configuration reference") from exc
            raise

    def delete(
        self, kind: str, definition_id: str, *,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        _identity(kind, definition_id)
        self._request(expected_revision, idempotency_key)
        digest = content_digest({
            "operation": "delete", "kind": kind, "id": definition_id,
            "expected_revision": expected_revision,
        })
        self._available_transaction()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            receipt = self._receipt(idempotency_key, digest)
            if receipt is not None:
                self._connection.execute("COMMIT")
                return receipt
            head = self.get_head(kind, definition_id)
            if head is None:
                raise _error("not_found", 404, "Prompt configuration was not found")
            self._cas(head, expected_revision)
            record = self._resolve(kind, definition_id, head["definition_revision"])
            self._store._inject("prompt_before_write")
            new_head = self._write_head(
                kind, definition_id, head["definition_revision"], expected_revision + 1, False,
            )
            result = {"record": record, "head": new_head}
            self._save_receipt(idempotency_key, digest, result)
            self._store._inject("prompt_before_commit")
            self._connection.execute("COMMIT")
            return result
        except Exception as exc:
            self._rollback(exc)
            raise
