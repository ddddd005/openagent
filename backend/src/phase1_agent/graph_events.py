"""Declared graph events over the ordinary session execution lifecycle."""

from contextlib import closing
from copy import deepcopy

from .graph_records import require, uuid_value
from .graph_store import GraphRecordStore
from .host_sdk import bounded_name


class GraphEvents:
    @staticmethod
    def _event_binding(document, event_id, schema_version, *, consumer=False):
        require(bounded_name(event_id) and type(schema_version) is int and 1 <= schema_version <= 2**53 - 1,
                "invalid_request", "An exact event identity and schema version are required")
        binding = next((row for row in document.get("event_bindings", [])
                        if row["event_id"] == event_id and row["schema_version"] == schema_version), None)
        require(binding is not None, "graph_event_not_declared", "Event is not declared by this definition", 404)
        require(not consumer or binding["audience"] == "consumer",
                "graph_event_not_public", "This event is not publicly declared", 403)
        return binding

    def _event_scope(self, repo, sid, workflow_definition_id, definition_revision):
        return self._consumer_scope(repo, sid, workflow_definition_id, definition_revision)

    def _event_bindings(self, sid, *, workflow_definition_id, definition_revision, consumer=False):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            session, document = self._event_scope(repo, sid, workflow_definition_id, definition_revision)
            bindings = [deepcopy(row) for row in document.get("event_bindings", [])
                        if not consumer or row["audience"] == "consumer"]
            return {"schema_version": 1, "kind": "workflow.event-bindings",
                    **self._consumer_identity(session), "can_submit": session["active_chain_run_id"] is None,
                    "bindings": bindings}

    def list_event_bindings(self, sid, *, workflow_definition_id, definition_revision):
        return self._event_bindings(sid, workflow_definition_id=workflow_definition_id,
                                    definition_revision=definition_revision)

    def list_consumer_event_bindings(self, sid, *, workflow_definition_id, definition_revision):
        return self._event_bindings(sid, workflow_definition_id=workflow_definition_id,
                                    definition_revision=definition_revision, consumer=True)

    def _submit_event(self, sid, *, workflow_definition_id, definition_revision, event_id,
                      event_schema_version, payload, expected_revision, idempotency_key, consumer=False):
        request = {"session": sid, "workflow_definition_id": workflow_definition_id,
                   "definition_revision": definition_revision, "event_id": event_id,
                   "event_schema_version": event_schema_version, "payload": payload,
                   "expected_revision": expected_revision}
        # Public authorization is current, including on a persisted receipt replay.
        with self._lock:
            with closing(self._store()) as store:
                self._check_open()
                _, document = self._event_scope(GraphRecordStore(store), sid,
                                                workflow_definition_id, definition_revision)
                self._event_binding(document, event_id, event_schema_version, consumer=consumer)

            def change(repo):
                _, document = self._event_scope(repo, sid, workflow_definition_id, definition_revision)
                binding = self._event_binding(document, event_id, event_schema_version, consumer=consumer)
                event = {"event_id": event_id, "schema_version": event_schema_version,
                         "audience": binding["audience"], "idempotency_key": idempotency_key}
                return self._prepare_run(repo, sid, expected_revision, payload, event=event)

            result = self._start_run(sid, idempotency_key, request, change,
                                     operation="graph.consumer.event.submit" if consumer else "graph.event.submit")
            return self._consumer_receipt(result, "event", idempotency_key) if consumer else result

    def submit_event(self, sid, *, workflow_definition_id, definition_revision, event_id,
                     event_schema_version, payload, expected_revision, idempotency_key):
        return self._submit_event(sid, workflow_definition_id=workflow_definition_id,
            definition_revision=definition_revision, event_id=event_id, event_schema_version=event_schema_version,
            payload=payload, expected_revision=expected_revision, idempotency_key=idempotency_key)

    def submit_consumer_event(self, sid, *, workflow_definition_id, definition_revision, event_id,
                              event_schema_version, payload, expected_revision, idempotency_key):
        return self._submit_event(sid, workflow_definition_id=workflow_definition_id,
            definition_revision=definition_revision, event_id=event_id, event_schema_version=event_schema_version,
            payload=payload, expected_revision=expected_revision, idempotency_key=idempotency_key, consumer=True)

    def _read_event(self, sid, *, chain_id, consumer=False):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            session, current = self._consumer_scope(repo, sid)
            require(uuid_value(chain_id) and chain_id in self._history(repo, sid),
                    "not_found", "Event is outside authorized session history", 404)
            chain = repo.get("chain_run", chain_run_id=chain_id)
            require(chain.get("execution_kind", "round") == "event",
                    "graph_event_not_found", "This execution is not an event", 404)
            event = chain["event"]
            original = self._document(repo, chain["workflow_definition_id"], chain["definition_revision"])
            self._event_binding(original, event["event_id"], event["schema_version"], consumer=consumer)
            if consumer:
                self._event_binding(current, event["event_id"], event["schema_version"], consumer=True)
            diagnostic = chain["diagnostic"]
            return {"schema_version": 1, "kind": "workflow.event-run",
                    **self._consumer_identity(session),
                    "source": {key: chain[key] for key in (
                        "workflow_definition_id", "definition_revision", "workflow_session_id")},
                    "chain_run_id": chain_id, "execution_kind": "event", "event": deepcopy(event),
                    **{key: deepcopy(chain[key]) for key in (
                        "status", "revision", "targets", "ordered_nodes", "completed_nodes", "next_node_index")},
                    "diagnostic": ({"reason_code": diagnostic.get("reason_code", diagnostic.get("code", "event_failed")),
                                    "message": "Event execution requires attention"} if diagnostic else None)}

    def read_event(self, sid, *, chain_id):
        return self._read_event(sid, chain_id=chain_id)

    def read_consumer_event(self, sid, *, chain_id):
        return self._read_event(sid, chain_id=chain_id, consumer=True)
