"""Declared graph events over the ordinary session execution lifecycle."""

from copy import deepcopy

from .graph_records import require, uuid_value
from .host_sdk import bounded_name
from .notification_results import ROUND_NOTIFICATION_FAILURE

_EVENT_ATTENTION_MESSAGE = "Event execution requires attention"


def _public_event_diagnostic(diagnostic) -> dict | None:
    """Project a stored chain diagnostic to the public event-read diagnostic.

    A successful post-commit notification record is durable evidence, not an
    execution failure: it is omitted from the public projection only when the
    business result succeeded, the record is marked committed and every local
    notification stage succeeded.  Any other stored diagnostic stays a failure
    signal; a failed notification stage is reported under the committed
    notification reason code.  The persisted chain diagnostic is never cleared
    or rewritten by a read.
    """
    if not diagnostic:
        return None
    if type(diagnostic) is not dict:
        return {"reason_code": "event_failed", "message": _EVENT_ATTENTION_MESSAGE}
    record = diagnostic.get("round_notifications")
    residual = {key: value for key, value in diagnostic.items() if key != "round_notifications"}
    if residual:
        reason = residual.get("reason_code")
        if not (isinstance(reason, str) and reason):
            reason = residual.get("code")
        return {"reason_code": reason if isinstance(reason, str) and reason else "event_failed",
                "message": _EVENT_ATTENTION_MESSAGE}
    if type(record) is not dict:
        return {"reason_code": "event_failed", "message": _EVENT_ATTENTION_MESSAGE}
    notifications = record.get("notifications")
    if (record.get("business_committed") is True
            and record.get("business_status") == "succeeded"
            and isinstance(notifications, list) and notifications
            and all(type(row) is dict and row.get("outcome") == "succeeded"
                    for row in notifications)):
        return None
    if record.get("business_status") == "succeeded":
        return {"reason_code": ROUND_NOTIFICATION_FAILURE,
                "message": _EVENT_ATTENTION_MESSAGE}
    return {"reason_code": "event_failed", "message": _EVENT_ATTENTION_MESSAGE}


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
        with self._lock, self._data_management.read_execution(
                registry=self.registry) as data:
            self._check_open()
            session, document = self._event_scope(
                data, sid, workflow_definition_id, definition_revision)
            bindings = [deepcopy(row) for row in document.get("event_bindings", [])
                        if not consumer or row["audience"] == "consumer"]
            return {"schema_version": 1, "kind": "workflow.event-bindings",
                    **self._consumer_identity(session), "can_submit": session["active_chain_run_id"] is None,
                    "bindings": bindings}

    def list_event_bindings(self, sid, *, workflow_definition_id, definition_revision):
        return self._event_bindings(sid, workflow_definition_id=workflow_definition_id,
                                    definition_revision=definition_revision)

    def _submit_event(self, sid, *, workflow_definition_id, definition_revision, event_id,
                      event_schema_version, payload, expected_revision, idempotency_key):
        request = {"session": sid, "workflow_definition_id": workflow_definition_id,
                   "definition_revision": definition_revision, "event_id": event_id,
                   "event_schema_version": event_schema_version, "payload": payload,
                   "expected_revision": expected_revision}
        # Public authorization is current, including on a persisted receipt replay.
        with self._lock:
            with self._data_management.read_execution(registry=self.registry) as data:
                self._check_open()
                _, document = self._event_scope(
                    data, sid, workflow_definition_id, definition_revision)
                self._event_binding(document, event_id, event_schema_version)

            def change(data, basis):
                _, document = self._event_scope(
                    data, sid, workflow_definition_id, definition_revision)
                binding = self._event_binding(document, event_id, event_schema_version)
                event = {"event_id": event_id, "schema_version": event_schema_version,
                         "audience": binding["audience"], "idempotency_key": idempotency_key}
                return self._prepare_run(
                    data, sid, expected_revision, payload, event=event, basis=basis)

            return self._start_run(sid, idempotency_key, request, change, operation="graph.event.submit")

    def submit_event(self, sid, *, workflow_definition_id, definition_revision, event_id,
                     event_schema_version, payload, expected_revision, idempotency_key):
        return self._submit_event(sid, workflow_definition_id=workflow_definition_id,
            definition_revision=definition_revision, event_id=event_id, event_schema_version=event_schema_version,
            payload=payload, expected_revision=expected_revision, idempotency_key=idempotency_key)

    def _read_event(self, sid, *, chain_id, consumer=False):
        with self._lock, self._data_management.read_execution(
                registry=self.registry) as data:
            self._check_open()
            session, current = self._consumer_scope(data, sid)
            require(uuid_value(chain_id) and chain_id in data.records.history(sid),
                    "not_found", "Event is outside authorized session history", 404)
            chain = data.records.chain(chain_id)
            require(chain.get("execution_kind", "round") == "event",
                    "graph_event_not_found", "This execution is not an event", 404)
            event = chain["event"]
            original = data.configuration.fixed_document(
                chain["workflow_definition_id"], chain["definition_revision"])
            self._event_binding(original, event["event_id"], event["schema_version"], consumer=consumer)
            if consumer:
                self._event_binding(current, event["event_id"], event["schema_version"], consumer=True)
            diagnostic = _public_event_diagnostic(chain["diagnostic"])
            return {"schema_version": 1, "kind": "workflow.event-run",
                    **self._consumer_identity(session),
                    "source": {key: chain[key] for key in (
                        "workflow_definition_id", "definition_revision", "workflow_session_id")},
                    "chain_run_id": chain_id, "execution_kind": "event", "event": deepcopy(event),
                    **{key: deepcopy(chain[key]) for key in (
                        "status", "revision", "targets", "ordered_nodes", "completed_nodes", "next_node_index")},
                    "diagnostic": diagnostic}

    def read_event(self, sid, *, chain_id):
        return self._read_event(sid, chain_id=chain_id)
