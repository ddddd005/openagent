"""Legacy dispatch stops while immutable requests remain independently readable."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import COMMANDS, QUERIES, RETIRED_RECEIPT_COMMANDS
from phase1_agent.graph_application_identity import application_command_digest, bind_application_command
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.request_evidence import RequestEvidenceService, evidence_receipt_key, validate_evidence_record
from phase1_agent.workflow_host import WorkflowHost

from legacy_consumer_receipt_fixture import seed_legacy_consumer_receipt
from test_frontend_node_http import serving
from frontend_node_test_support import create as _create, graph as _graph, save as _save
from test_graph_application_receipt_custody import raw_database
from test_graph_server import request


RETIRED_WRITES = (
    "consumer.draft.save", "consumer.session.create", "consumer.run.start",
    "consumer.run.control", "consumer.event.submit",
)
RETIRED_READS = (
    "consumer.definition", "consumer.sessions", "consumer.read", "consumer.actions",
    "consumer.candidate.list", "consumer.event.bindings", "consumer.event.read",
)
MANAGEMENT_READS = (
    "output.list", "output.read", "output.artifact.read", "output.history",
    "frontend.extensions.read", "registration.list", "information.read",
)
WORKFLOW = "00000000-0000-4000-8000-000000000951"
SESSION = "00000000-0000-4000-8000-000000000952"


def forbidden(*args, **kwargs):
    pytest.fail("Retired or read-only request initialized execution or a model")


def test_retired_registry_and_same_process_methods_do_not_dispatch():
    class NoInvocation:
        def __getattr__(self, name):
            pytest.fail("Retired dispatch reached its service: " + name)

    command_names = {item.name for item in COMMANDS}
    query_names = {item.name for item in QUERIES}
    frozen = {item.name: item for item in RETIRED_RECEIPT_COMMANDS}
    for operation in RETIRED_WRITES:
        assert operation not in command_names and frozen[operation].consumer
        for scope in ("management", "consumer"):
            with pytest.raises(ContractValidationError) as rejected:
                GraphApplication(NoInvocation(), scope=scope).command(operation, {})
            assert rejected.value.reason_code == "not_found"
    assert not set(RETIRED_READS) & query_names
    assert set(MANAGEMENT_READS) <= query_names
    consumer = GraphApplication(NoInvocation(), scope="consumer").describe()
    assert consumer["commands"] == []
    assert {item["name"] for item in consumer["queries"]} == {
        "receipt.read", "consumer.draft.read", "consumer.draft.list",
    }
    for name in (
        "create_consumer_session", "start_consumer", "control_consumer", "submit_consumer_event",
        "save_consumer_draft", "consumer_definition", "list_consumer_sessions",
        "list_consumer_candidates", "list_consumer_event_bindings", "read_consumer_event",
    ):
        assert not hasattr(GraphWorkflowService, name)


def test_all_old_http_dispatches_reject_without_runtime_or_database(tmp_path, monkeypatch):
    database = tmp_path / "must-not-exist.sqlite"
    monkeypatch.setattr(WorkflowHost, "graph_service", property(forbidden))
    with closing(WorkflowHost(database)) as host, serving(host) as (_, port):
        for endpoint in ("/api/graph/commands", "/api/graph/consumer/commands"):
            for operation in RETIRED_WRITES:
                status, error = request(port, "POST", endpoint, {"operation": operation, "parameters": {}})
                assert status == 404 and error["error"]["reason_code"] == "not_found"
        for endpoint in ("/api/graph/queries", "/api/graph/consumer/queries"):
            for operation in RETIRED_READS:
                assert request(port, "POST", endpoint, {"operation": operation, "parameters": {}})[0] == 404
        for operation in MANAGEMENT_READS:
            status, error = request(port, "POST", "/api/graph/consumer/queries", {
                "operation": operation, "parameters": {},
            })
            assert status == 403 and error["error"]["reason_code"] == "application_scope_denied"
        for method, endpoint in (
            ("POST", "/api/graph/consumer/sessions"),
            ("POST", f"/api/graph/sessions/{SESSION}/consumer/runs"),
            ("POST", f"/api/graph/sessions/{SESSION}/consumer/control"),
            ("POST", f"/api/graph/sessions/{SESSION}/consumer/events/submit"),
            ("POST", f"/api/graph/sessions/{SESSION}/consumer/events/bindings"),
            ("POST", f"/api/graph/sessions/{SESSION}/consumer/events/read"),
            ("POST", f"/api/graph/sessions/{SESSION}/consumer/candidates/fork"),
            ("GET", f"/api/graph/definitions/{WORKFLOW}/consumer"),
            ("GET", f"/api/graph/definitions/{WORKFLOW}/consumer-sessions"),
            ("GET", f"/api/graph/sessions/{SESSION}/consumer"),
            ("GET", f"/api/graph/sessions/{SESSION}/consumer/actions"),
        ):
            assert request(port, method, endpoint, {} if method == "POST" else None)[0] == 404
        assert host._graph is None and host._management_graph is None
        assert host._providers is None and host._content is None and host._evidence is None
        assert not database.exists()


def _seed_legacy_evidence(database, operation, original):
    """Construct an old accepted evidence transaction without enabling its command."""
    parameters = {
        "operation": operation, "idempotency_key": original["idempotency_key"],
        "owner_kind": "legacy-consumer", "owner_id": original["idempotency_key"], "request": deepcopy(original),
    }
    record = validate_evidence_record({
        "schema_version": 1, "kind": "workflow.request-evidence", **deepcopy(parameters),
        "request_digest": application_command_digest(operation, original), "resolution": "pending",
    })

    def insert(connection):
        connection.execute("INSERT INTO pending_commands VALUES(?,?,?,?,?,?,?)", (
            operation, record["idempotency_key"], record["owner_kind"], record["owner_id"],
            canonical_bytes(record["request"]).decode("utf-8"), record["request_digest"], "pending"))
        return record

    native = {key: value for key, value in parameters.items() if key != "idempotency_key"}
    with bind_application_command("request-evidence.write", "management", parameters):
        saved = RequestEvidenceService(database)._mutate(
            "graph.request-evidence.write", native, evidence_receipt_key(
                operation, original["idempotency_key"]), insert)
    assert saved == record
    return parameters, record


def _seed_legacy_request(service, operation):
    graph, _, frontends = _graph(service, bound=True)
    graph["event_bindings"] = [{
        "event_id": "legacy.append", "schema_version": 1, "display_name": "Legacy append",
        "audience": "consumer", "target_node_ids": [frontends[0]["node_binding_id"]],
        "payload_schema": {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        },
    }]
    graph = _save(service, graph)
    initial = _create(service, graph)
    sid, key = initial["workflow_session_id"], str(uuid4())
    if operation == "consumer.session.create":
        parameters = {
            "workflow_definition_id": graph["workflow_definition_id"],
            "definition_revision": 1, "idempotency_key": key,
        }
    elif operation == "consumer.draft.save":
        pending = {
            "action": "start", "workflow_session_id": sid,
            "path": f"/api/graph/sessions/{sid}/consumer/runs",
            "body": {"expected_revision": 1, "idempotency_key": str(uuid4()),
                     "inputs": {"text": " frozen old request "}},
        }
        parameters = {
            "address": {
                "scope": "consumer", "kind": "chat-client", "key": "tavern:client",
                "workflow_definition_id": graph["workflow_definition_id"], "workflow_session_id": sid,
            },
            "format_version": 1, "expected_sequence": 0, "idempotency_key": key,
            "value": {
                "schema_version": 1, "kind": "workflow.chat-client",
                "workflow_definition_id": graph["workflow_definition_id"],
                "workflow_session_id": sid, "pending": pending,
            },
        }
    elif operation == "consumer.run.control":
        initial = service._change(
            "graph.run.start", str(uuid4()), {"session": sid, "expected_revision": 1, "inputs": {}},
            lambda repo: service._prepare_run(repo, sid, 1, {}))
        parameters = {
            "session_id": sid, "action": "pause", "expected_revision": initial["revision"], "idempotency_key": key,
        }
    else:
        parameters = {"session_id": sid, "expected_revision": 1, "idempotency_key": key}
        if operation == "consumer.run.start":
            parameters["inputs"] = {"text": " original external inputs "}
        else:
            parameters.update(
                workflow_definition_id=graph["workflow_definition_id"], definition_revision=1,
                event_id="legacy.append", event_schema_version=1, payload={"text": " original event "})
    original = seed_legacy_consumer_receipt(service, operation, parameters)
    if operation in ("consumer.run.start", "consumer.event.submit"):
        service.wait(original["result"]["receipt"]["chain_run_id"])
    return parameters, original


@pytest.mark.parametrize("operation", RETIRED_WRITES)
def test_stored_receipts_evidence_and_drafts_survive_http_retirement(tmp_path, monkeypatch, operation):
    database = tmp_path / "legacy-frozen.sqlite"
    with closing(GraphWorkflowService(
        database, public_model_factory=forbidden, provider_discovery_client_factory=forbidden,
    )) as service:
        parameters, origin = _seed_legacy_request(service, operation)
        evidence_parameters, record = _seed_legacy_evidence(database, operation, parameters)
    before = raw_database(database)
    monkeypatch.setattr(WorkflowHost, "graph_service", property(forbidden))
    with closing(WorkflowHost(database)) as host, serving(host) as (_, port):
        for endpoint in ("/api/graph/receipts/read", "/api/graph/consumer/receipts/read"):
            status, frozen = request(port, "POST", endpoint, {"operation": operation, "parameters": parameters})
            assert status == 200 and frozen["outcome"] == "matched"
            assert frozen["receipt"] == origin["receipt"]
            expected = origin["result"] if operation == "consumer.draft.save" else {
                "receipt": origin["result"]["receipt"]}
            assert frozen["result"] == expected
        for scope in ("", "/consumer"):
            assert request(port, "POST", f"/api/graph{scope}/queries", {
                "operation": "receipt.read", "parameters": {"operation": operation, "parameters": parameters},
            })[1]["outcome"] == "matched"
            assert request(port, "POST", f"/api/graph{scope}/commands", {
                "operation": operation, "parameters": parameters,
            })[0] == 404
        evidence_owner = {key: record[key] for key in ("owner_kind", "owner_id")}
        assert request(port, "POST", "/api/graph/queries", {
            "operation": "request-evidence.read", "parameters": evidence_owner,
        }) == (200, [record])
        assert request(port, "POST", "/api/graph/receipts/read", {
            "operation": "request-evidence.write", "parameters": evidence_parameters,
        })[1]["result"] == record
        assert request(port, "POST", "/api/graph/commands", {
            "operation": "request-evidence.write", "parameters": evidence_parameters,
        })[0] == 400
        changed_key = {**deepcopy(parameters), "idempotency_key": parameters["idempotency_key"] + "-wrong"}
        changed_body = deepcopy(parameters)
        field = ("expected_sequence" if operation == "consumer.draft.save" else
                 "definition_revision" if operation == "consumer.session.create" else "expected_revision")
        changed_body[field] += 1
        for changed in (changed_key, changed_body):
            assert request(port, "POST", "/api/graph/consumer/receipts/read", {
                "operation": operation, "parameters": changed,
            })[1]["outcome"] == "unresolved"
        if operation == "consumer.draft.save":
            for scope in ("", "/consumer"):
                assert request(port, "POST", f"/api/graph{scope}/queries", {
                    "operation": "consumer.draft.read", "parameters": {"address": parameters["address"]},
                }) == (200, origin["result"])
                assert request(port, "POST", f"/api/graph{scope}/queries", {
                    "operation": "consumer.draft.list", "parameters": {
                        "workflow_definition_id": parameters["address"]["workflow_definition_id"]},
                })[1]["records"] == [origin["result"]]
        assert raw_database(database) == before
        resolution = {key: record[key] for key in ("operation", "idempotency_key", "request_digest")}
        resolution["resolution"] = "verified"
        resolved = request(port, "POST", "/api/graph/commands", {
            "operation": "request-evidence.resolve", "parameters": resolution,
        })[1]["result"]
        assert resolved == {**record, "resolution": "verified"}
        assert request(port, "POST", "/api/graph/receipts/read", {
            "operation": "request-evidence.resolve", "parameters": resolution,
        })[1]["result"] == resolved
        assert host._graph is None
    after = raw_database(database)
    pending_columns, pending_rows = after[2]["pending_commands"]
    old_columns, old_rows = before[2]["pending_commands"]
    assert pending_columns == old_columns
    assert pending_rows == tuple((*row[:-1], "verified") for row in old_rows)
    assert after[2]["local_editor_drafts"] == before[2]["local_editor_drafts"]
    assert after[2]["records"] == before[2]["records"]
    assert set(before[2]["idempotency"][1]) <= set(after[2]["idempotency"][1])


def test_ordinary_management_http_keeps_execution_events_objects_and_public_reads(tmp_path):
    with closing(GraphWorkflowService(
        tmp_path / "ordinary-management.sqlite", public_model_factory=forbidden,
        provider_discovery_client_factory=forbidden,
    )) as service:
        graph, messages, frontends = _graph(service, bound=True)
        frontend = frontends[0]
        frontend["public_outputs"] = ["commit"]
        graph["event_bindings"] = [{
            "event_id": "input.read", "schema_version": 1, "display_name": "Read input",
            "audience": "management", "target_node_ids": [messages[0]["node_binding_id"]],
            "payload_schema": {"type": "object", "additionalProperties": False},
        }]
        graph = _save(service, graph)
        with serving(service) as (_, port):
            status, initial = request(port, "POST", "/api/graph/sessions", {
                "workflow_definition_id": graph["workflow_definition_id"], "definition_revision": 1,
                "idempotency_key": str(uuid4()),
            })
            assert status == 201
            sid = initial["workflow_session_id"]
            status, started = request(port, "POST", f"/api/graph/sessions/{sid}/runs", {
                "expected_revision": initial["revision"], "inputs": {}, "idempotency_key": str(uuid4()),
            })
            assert status == 202
            service.wait(started["active_chain_run_id"])
            current = request(port, "GET", f"/api/graph/sessions/{sid}")[1]
            assert current["status"] == "succeeded"
            rounds = current["objects"]["chat-0"]["value"]["rounds"]
            assert len(rounds) == 1 and len(rounds[0]["candidates"]) == 1
            assert rounds[0]["selected_candidate_id"] == rounds[0]["candidates"][0]["candidate_id"]
            assert [message["text"] for message in rounds[0]["candidates"][0]["messages"]] == ["message 0"]
            status, public = request(port, "GET", f"/api/graph/sessions/{sid}/outputs/public")
            assert status == 200 and public["outputs"][0]["availability"] == "produced"
            public_owner = {
                "session_id": sid, "workflow_definition_id": graph["workflow_definition_id"],
                "definition_revision": 1, "node_id": frontend["node_binding_id"], "port_id": "commit",
            }
            for operation, parameters in (
                ("output.read", public_owner), ("output.history", public_owner),
                ("frontend.extensions.read", public_owner), ("registration.list", {"session_id": sid}),
                ("object.read", {
                    "session_id": sid, "object_key": "chat-0", "node_id": frontend["node_binding_id"]}),
            ):
                assert request(port, "POST", "/api/graph/queries", {
                    "operation": operation, "parameters": parameters,
                })[0] == 200
            checkpoints = request(port, "GET", f"/api/graph/sessions/{sid}/checkpoints")
            assert checkpoints[0] == 200
            event_scope = {
                "workflow_definition_id": graph["workflow_definition_id"], "definition_revision": 1,
            }
            assert request(port, "POST", f"/api/graph/sessions/{sid}/events/bindings", event_scope)[0] == 200
            status, event = request(port, "POST", f"/api/graph/sessions/{sid}/events/submit", {
                **event_scope, "event_id": "input.read", "event_schema_version": 1, "payload": {},
                "expected_revision": current["revision"], "idempotency_key": str(uuid4()),
            })
            assert status == 202
            service.wait(event["active_chain_run_id"])
            assert request(port, "POST", f"/api/graph/sessions/{sid}/events/read", {
                "chain_id": event["active_chain_run_id"],
            })[1]["status"] == "succeeded"
            final = service.get_session(sid)
            assert final["objects"]["chat-0"]["value"]["rounds"] == rounds
            assert final["head_revision"] == current["head_revision"]
