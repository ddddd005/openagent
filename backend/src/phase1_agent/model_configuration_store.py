"""Versioned global provider metadata and workflow model configuration."""

from __future__ import annotations

import sqlite3
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, content_digest, loads_strict, validate_json_value
from .model_configuration import (
    MODEL_BINDINGS, chat_parameter_diagnostic, configuration_error, configuration_identity, identifier,
    require, revision, validate_configuration,
)
from .storage import SqliteStore


class ModelConfigurationStore:
    def __init__(self, store: SqliteStore):
        self._store = store
        self._connection = store._connection

    @staticmethod
    def _decode(kind, payload, identity, version):
        try:
            record = validate_configuration(kind, loads_strict(payload))
            require(configuration_identity(kind, record) == identity and record["revision"] == version)
            return record
        except ContractValidationError:
            raise configuration_error("storage_contract_violation", 500) from None

    def get_revision(self, kind: str, identity: str, version: int) -> dict[str, Any] | None:
        require(kind in ("provider", "model"))
        identifier(identity)
        revision(version)
        try:
            row = self._connection.execute(
                "SELECT payload FROM model_configuration_revisions "
                "WHERE kind = ? AND identity = ? AND revision = ?", (kind, identity, version),
            ).fetchone()
            return self._decode(kind, row["payload"], identity, version) if row else None
        except sqlite3.Error:
            raise configuration_error("storage_error", 500) from None

    def get_current(self, kind: str, identity: str) -> dict[str, Any] | None:
        require(kind in ("provider", "model"))
        identifier(identity)
        try:
            row = self._connection.execute(
                "SELECT revision, payload FROM model_configuration_revisions "
                "WHERE kind = ? AND identity = ? ORDER BY revision DESC LIMIT 1", (kind, identity),
            ).fetchone()
            return self._decode(kind, row["payload"], identity, row["revision"]) if row else None
        except sqlite3.Error:
            raise configuration_error("storage_error", 500) from None

    def list_providers(self) -> list[dict[str, Any]]:
        try:
            identities = self._connection.execute(
                "SELECT DISTINCT identity FROM model_configuration_revisions "
                "WHERE kind = 'provider' ORDER BY identity",
            ).fetchall()
            return [self.get_current("provider", row["identity"]) for row in identities]
        except sqlite3.Error:
            raise configuration_error("storage_error", 500) from None

    def write(
        self, kind: str, record: dict[str, Any], *,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        revision(expected_revision, zero=True)
        require(type(idempotency_key) is str and 0 < len(idempotency_key.strip()) <= 128)
        try:
            validate_json_value(idempotency_key)
        except ContractValidationError:
            raise configuration_error("invalid_request") from None
        record = validate_configuration(kind, record)
        identity = configuration_identity(kind, record)
        digest = content_digest({"kind": kind, "record": record, "expected_revision": expected_revision})
        require(not self._connection.in_transaction)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            receipt = self._connection.execute(
                "SELECT digest, payload FROM model_configuration_mutations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if receipt:
                if receipt["digest"] != digest:
                    raise configuration_error("idempotency_conflict", 409)
                saved = self._decode(kind, receipt["payload"], identity, record["revision"])
                stored = self.get_revision(kind, identity, record["revision"])
                if canonical_bytes(saved) != canonical_bytes(record) or stored != saved:
                    raise configuration_error("storage_contract_violation", 500)
                self._connection.execute("COMMIT")
                return saved
            current = self.get_current(kind, identity)
            if (current["revision"] if current else 0) != expected_revision:
                raise configuration_error("stale_revision", 409)
            require(record["revision"] == expected_revision + 1)
            payload = canonical_bytes(record).decode("utf-8")
            self._connection.execute(
                "INSERT INTO model_configuration_revisions (kind, identity, revision, payload) VALUES (?, ?, ?, ?)",
                (kind, identity, record["revision"], payload),
            )
            self._store._inject("model_configuration_after_write")
            self._connection.execute(
                "INSERT INTO model_configuration_mutations (idempotency_key, digest, payload) VALUES (?, ?, ?)",
                (idempotency_key, digest, payload),
            )
            self._store._inject("model_configuration_before_commit")
            self._connection.execute("COMMIT")
            return record
        except Exception as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise configuration_error("storage_error", 500) from None
            raise

    def diagnostics(self, record: dict[str, Any]) -> list[dict[str, str]]:
        """Do not repair missing dependencies or select another provider."""
        result = []
        if record["nodes"]:
            connected = {edge["target_binding_id"] for edge in record["edges"]}
            for binding in sorted(MODEL_BINDINGS - connected):
                result.append({"target": binding, "code": "model_dependency_missing"})
        for node in record["nodes"]:
            parameter_code = chat_parameter_diagnostic(node["parameters"])
            if parameter_code:
                result.append({"target": node["id"], "code": parameter_code})
            reference = node["provider_ref"]
            if reference is None:
                result.append({"target": node["id"], "code": "provider_missing"})
                continue
            provider = self.get_revision("provider", reference["provider_id"], reference["revision"])
            current = self.get_current("provider", reference["provider_id"])
            if provider is None:
                code = "provider_missing"
            elif not provider["enabled"] or not current or not current["enabled"]:
                code = "provider_unavailable"
            elif not provider["credential_ref"] or current["credential_ref"] != provider["credential_ref"]:
                code = "credential_reference_missing"
            else:
                continue
            result.append({"target": node["id"], "code": code})
        return result
