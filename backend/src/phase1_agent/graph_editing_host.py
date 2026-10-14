"""Trusted management and narrowly scoped consumer editor storage."""

from contextlib import closing

from .graph_drafts import LocalDraftStore, _KINDS, validate_draft_address, validate_draft_value
from .graph_records import require, uuid_value
from .graph_store import GraphRecordStore


class GraphEditingHost:
    def _draft_owner(self, store, address, value=None):
        workflow, session = address["workflow_definition_id"], address["workflow_session_id"]
        repo = GraphRecordStore(store)
        if address["scope"] == "consumer":
            self._document(repo, workflow)
        if session is not None:
            require(repo.get("workflow_session", workflow_session_id=session)["workflow_definition_id"] == workflow,
                    "draft_owner_mismatch", "Draft session belongs to another workflow", 409)
        if address["scope"] == "consumer" and address["kind"] == "chat-client" and value is not None:
            selected = value["workflow_session_id"]
            if selected is not None:
                require(repo.get("workflow_session", workflow_session_id=selected)["workflow_definition_id"] == workflow,
                        "draft_owner_mismatch", "Client session belongs to another workflow", 409)
            pending = value["pending"]
            if pending and pending["action"] == "create":
                self._document(repo, workflow, pending["body"]["definition_revision"])
            elif pending and pending["action"] == "event":
                self._document(repo, workflow, pending["body"]["definition_revision"])
            elif pending and pending["action"] == "fork":
                candidate = pending["candidate"]
                require(repo.get("workflow_session", workflow_session_id=candidate["source_session_id"])[
                    "workflow_definition_id"] == workflow,
                    "draft_owner_mismatch", "Candidate source belongs to another workflow", 409)
                self._document(repo, workflow, candidate["source_definition_revision"])

    def _read_draft(self, address, scope):
        address = validate_draft_address(address, scope)
        with self._lock, closing(self._store()) as store:
            self._check_open()
            self._draft_owner(store, address)
            record = LocalDraftStore(store).read(address)
            if record is not None:
                self._draft_owner(store, address, record["value"])
            return record

    def _list_drafts(self, scope, *, kind=None, workflow_definition_id=None):
        require(kind is None or type(kind) is str and kind in _KINDS[scope],
                "invalid_draft_address", "Draft kind is not permitted")
        require(workflow_definition_id is None or uuid_value(workflow_definition_id),
                "invalid_draft_address", "Draft workflow identity is invalid")
        require(scope != "consumer" or uuid_value(workflow_definition_id),
                "invalid_draft_address", "Consumer listing requires an exact workflow")
        with self._lock, closing(self._store()) as store:
            self._check_open()
            if scope == "consumer":
                self._document(GraphRecordStore(store), workflow_definition_id)
            records = LocalDraftStore(store).list(scope, kind=kind, workflow_definition_id=workflow_definition_id)
            for record in records:
                self._draft_owner(store, record["address"], record["value"])
            return {"schema_version": 1, "kind": "workflow.local-drafts", "records": records}

    def _save_draft(self, address, format_version, value, expected_sequence, idempotency_key):
        scope = "consumer"
        address = validate_draft_address(address, scope)
        validate_draft_value(address, format_version, value)
        require(type(expected_sequence) is int and 0 <= expected_sequence <= 2**53 - 1,
                "invalid_request", "Draft expected sequence is invalid")
        require(type(idempotency_key) is str and 1 <= len(idempotency_key) <= 128 and bool(idempotency_key.strip()),
                "invalid_request", "An idempotency key is required")
        request = {"address": address, "format_version": format_version, "value": value,
                   "expected_sequence": expected_sequence}
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return LocalDraftStore(store).save("graph.consumer.draft.save", request, idempotency_key,
                lambda owner, content: self._draft_owner(store, owner, content))

    def read_draft(self, address):
        return self._read_draft(address, "management")

    def read_consumer_draft(self, address):
        return self._read_draft(address, "consumer")

    def list_drafts(self, **filters):
        return self._list_drafts("management", **filters)

    def list_consumer_drafts(self, **filters):
        return self._list_drafts("consumer", **filters)

    def save_consumer_draft(self, **parameters):
        return self._save_draft(**parameters)
