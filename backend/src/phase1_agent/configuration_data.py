"""Configuration-domain data interface, management entry and record repository.

The configuration functional layer only sees ``ConfigurationDataSession``: a
limited set of configuration intents bound to the caller's existing command
transaction.  The shared ``SqliteStore``, ``GraphRecordStore`` and the current
covering table stay inside this data layer.  Nothing here decides whether a
configuration field is fixed or adjustable, or whether an edit must ask, save
or derive.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from .contract_errors import require


if TYPE_CHECKING:
    from .graph_store import GraphRecordStore


def initialize_configuration_tables(connection) -> None:
    """Create the current-configuration covering table; no schema change."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS workflow_current_configuration ("
        "workflow_definition_id TEXT PRIMARY KEY NOT NULL, edit_sequence INTEGER NOT NULL, "
        "fixed_definition_revision INTEGER NOT NULL, display_name TEXT NOT NULL, "
        "node_presentation_json TEXT NOT NULL, adjustable_config_json TEXT NOT NULL)")


class ConfigurationRepository:
    """Map fixed revisions, node projections and current coverage to records."""

    def __init__(self, records: "GraphRecordStore"):
        self._records = records

    def current_configuration(self, identity: str) -> dict | None:
        return self._records.current_configuration(identity)

    def definition_revisions(self, identity: str) -> list[dict]:
        return self._records.rows("workflow_definition_revision",
                                  workflow_definition_id=identity)

    def all_definition_revisions(self) -> list[dict]:
        return self._records.rows("workflow_definition_revision")

    def fixed_document(self, identity: str, revision: int | None = None) -> dict:
        if revision is None:
            revisions = self.definition_revisions(identity)
            require(bool(revisions), "not_found", "Workflow definition not found", 404)
            revision = max(row["revision"] for row in revisions)
        return self._records.get(
            "workflow_definition_revision", workflow_definition_id=identity,
            revision=revision)["document"]

    def definition_sessions(self, identity: str) -> list[dict]:
        return self._records.rows("workflow_session", workflow_definition_id=identity)

    def session(self, session_id: str) -> dict:
        return self._records.get("workflow_session", workflow_session_id=session_id)

    def archived(self, kind: str, identity: str) -> bool:
        row = self._records.store._connection.execute(
            "SELECT archived FROM workflow_management WHERE object_kind=? AND object_id=?",
            (kind, identity)).fetchone()
        return bool(row and row["archived"])

    def save_fixed_revision(self, document: dict, expected_revision: int) -> dict:
        """Write one fixed revision and its node projections through ``repo.put``.

        Using ``repo.put`` keeps the original changed-definition tracking, so
        ``GraphRecordStore.atomic`` still validates the affected bundle before
        commit.  The expected revision and one-step advance checks are the same
        storage conditions as the former ``_save_definition``.
        """
        from .graph_contracts import validate_graph_document
        from .graph_records import graph_record

        document = validate_graph_document(document)
        document.pop("edit_sequence", None)
        require(type(expected_revision) is int and expected_revision >= 0,
                "invalid_request", "Invalid expected revision")
        existing = self.definition_revisions(document["workflow_definition_id"])
        current = max((row["revision"] for row in existing), default=0)
        require(current == expected_revision, "stale_revision", "Definition changed", 409)
        require(document["revision"] == current + 1, "invalid_request",
                "Definition revision must advance once")
        for node in document["nodes"]:
            self._records.put("node_binding", graph_record(
                "node_binding",
                workflow_definition_id=document["workflow_definition_id"],
                workflow_definition_revision=document["revision"],
                node_binding_id=node["node_binding_id"],
                component_id=node["component_id"],
                component_version=node["component_version"],
                config=node["config"]))
        self._records.put("workflow_definition_revision", graph_record(
            "workflow_definition_revision",
            workflow_definition_id=document["workflow_definition_id"],
            revision=document["revision"],
            bindings=[node["node_binding_id"] for node in document["nodes"]],
            edges=document["edges"], document=document))
        return document

    def save_current_configuration(self, value: dict) -> None:
        self._records.put_current_configuration(value)


class ConfigurationDataSession:
    """Limited configuration-domain port bound to an existing transaction."""

    def __init__(self, repository: ConfigurationRepository):
        self._access = repository

    def current_configuration(self, identity: str) -> dict | None:
        return self._access.current_configuration(identity)

    def definition_revisions(self, identity: str) -> list[dict]:
        return self._access.definition_revisions(identity)

    def all_definition_revisions(self) -> list[dict]:
        return self._access.all_definition_revisions()

    def fixed_document(self, identity: str, revision: int | None = None) -> dict:
        return self._access.fixed_document(identity, revision)

    def definition_sessions(self, identity: str) -> list[dict]:
        return self._access.definition_sessions(identity)

    def session(self, session_id: str) -> dict:
        return self._access.session(session_id)

    def archived(self, kind: str, identity: str) -> bool:
        return self._access.archived(kind, identity)

    def configuration_view(self, identity: str) -> tuple[dict | None, dict | None]:
        """Return the current covering row and its exact fixed document."""
        current = self._access.current_configuration(identity)
        if current is None:
            return None, None
        return current, self._access.fixed_document(
            identity, current["fixed_definition_revision"])

    def save_fixed_revision(self, document: dict, expected_revision: int) -> dict:
        return self._access.save_fixed_revision(document, expected_revision)

    def save_current_configuration(self, value: dict) -> None:
        self._access.save_current_configuration(value)


class WorkflowDataManagement:
    """Data-layer intake for defined workflow domains; currently configuration."""

    def __init__(self, store_factory):
        self._store_factory = store_factory

    def configuration_transaction(self, records: "GraphRecordStore") -> ConfigurationDataSession:
        """Bind limited configuration operations to the caller's transaction."""
        return ConfigurationDataSession(ConfigurationRepository(records))

    @contextmanager
    def read_configuration(self):
        """Open a fresh store for an independent configuration read.

        Never call this while the caller holds an open command transaction;
        use ``configuration_transaction`` with the existing repo instead.  A
        second store would block on its own ``BEGIN IMMEDIATE``.
        """
        from .graph_store import GraphRecordStore

        store = self._store_factory()
        try:
            yield self.configuration_transaction(GraphRecordStore(store))
        finally:
            store.close()


class WorkflowConfigurationData:
    """Configuration-layer data interface over the data management entry."""

    def __init__(self, management: WorkflowDataManagement):
        self._management = management

    def transaction(self, records: "GraphRecordStore") -> ConfigurationDataSession:
        return self._management.configuration_transaction(records)

    @contextmanager
    def read(self):
        with self._management.read_configuration() as data:
            yield data
