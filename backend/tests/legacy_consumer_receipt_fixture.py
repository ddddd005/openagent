"""Seed frozen legacy facts through native transactions, never active dispatch."""

from contextlib import closing
from copy import deepcopy

from phase1_agent.graph_application_contracts import RETIRED_RECEIPT_COMMANDS, validate_parameters
from phase1_agent.graph_application_identity import application_command_receipt, bind_application_command
from phase1_agent.graph_drafts import LocalDraftStore


def seed_legacy_consumer_receipt(service, operation, parameters):
    spec = next(item for item in RETIRED_RECEIPT_COMMANDS if item.name == operation)
    request = deepcopy(validate_parameters(spec, parameters))
    arguments = {key: value for key, value in request.items() if key != "idempotency_key"}
    key = request["idempotency_key"]
    with bind_application_command(operation, "consumer", request):
        if operation == "consumer.draft.save":
            with closing(service._store()) as store:
                raw = LocalDraftStore(store).save(
                    "graph.consumer.draft.save", arguments, key,
                    lambda owner, content: service._draft_owner(store, owner, content))
        elif operation == "consumer.session.create":
            raw = service.create_session(**arguments, idempotency_key=key)
        else:
            sid = arguments.pop("session_id")
            if operation == "consumer.run.start":
                raw = service.start(sid, **arguments, idempotency_key=key)
            elif operation == "consumer.run.control":
                raw = service.control(sid, **arguments, idempotency_key=key)
            else:
                assert operation == "consumer.event.submit"
                native = {"session": sid, **arguments}

                def change(repo):
                    _, document = service._event_scope(
                        repo, sid, request["workflow_definition_id"], request["definition_revision"])
                    binding = service._event_binding(
                        document, request["event_id"], request["event_schema_version"], consumer=True)
                    event = {
                        "event_id": request["event_id"], "schema_version": request["event_schema_version"],
                        "audience": binding["audience"], "idempotency_key": key,
                    }
                    return service._prepare_run(
                        repo, sid, request["expected_revision"], request["payload"], event=event)

                raw = service._start_run(
                    sid, key, native, change, operation="graph.consumer.event.submit")
    if operation == "consumer.draft.save":
        result = raw
    else:
        verb = {
            "consumer.session.create": "create", "consumer.run.start": "start",
            "consumer.run.control": "control", "consumer.event.submit": "event",
        }[operation]
        result = {
            "receipt": {
                "workflow_definition_id": raw["workflow_definition_id"],
                "definition_revision": raw["definition_revision"],
                "workflow_session_id": raw["workflow_session_id"],
                "session_revision": raw["revision"], "status": raw["status"],
                "chain_run_id": raw["active_chain_run_id"], "operation": verb, "idempotency_key": key,
            },
            "consumer": service.get_consumer(raw["workflow_session_id"]),
        }
    return {
        "schema_version": 1, "kind": "workflow.application-command", "result": result,
        "receipt": application_command_receipt(
            operation, "consumer", request, result, authority="service_receipt"),
    }
