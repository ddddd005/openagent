"""Ordinary graph management does not initialize execution ownership."""

from contextlib import closing
from copy import deepcopy
import http.client
import json
import sqlite3
from threading import Event, Thread
import traceback
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding, WriteIntent
from phase1_agent.server import create_server
from phase1_agent.storage import SqliteStore
from phase1_agent.tool_package import VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE
from phase1_agent.workflow_host import WorkflowHost


def _request(port, method, path, payload=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if method == "POST":
        headers["Origin"] = f"http://127.0.0.1:{port}"
    try:
        connection.request(method, path,
                           body=None if payload is None else json.dumps(payload),
                           headers=headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None
    finally:
        connection.close()


def _start_server(service):
    server = create_server(service, port=0)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker, server.server_address[1]


def _reject_execution_initialization(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("management request initialized execution")

    monkeypatch.setattr(GraphWorkflowService, "__init__", forbidden)
    monkeypatch.setattr("phase1_agent.graph_service._GraphLease", forbidden)
    monkeypatch.setattr("phase1_agent.graph_service.ThreadPoolExecutor", forbidden)


def _draft_address():
    return {"scope": "management", "kind": "workbench", "key": "ordinary",
            "workflow_definition_id": None, "workflow_session_id": None}


def _new_incomplete_document(service):
    text_entry = service.registry.get("tools.text", "1")
    variable_entry = service.registry.get("tools.variable-register", "1")
    output_entry = service.registry.get("tools.output", "1")
    variable_id, output_id = str(uuid4()), str(uuid4())
    variable_config = deepcopy(variable_entry.definition.default_config)
    variable_config["object_key"] = "http-value"
    output_config = deepcopy(output_entry.definition.default_config)
    return {
        "schema_version": 3,
        "workflow_definition_id": str(uuid4()),
        "revision": 1,
        "name": "Incomplete graph",
        "package_lock": list(service.registry.execution_package_lock),
        "object_bindings": [ObjectBinding(
            "http-value", VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION, "shared",
            readers=(variable_id,), writers=(variable_id,),
        ).to_dict()],
        "nodes": [
            {"node_binding_id": str(uuid4()), "component_id": "tools.text", "component_version": "1",
             "title": "Unconnected text", "position": {"x": 0, "y": 0},
             "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
             "config": deepcopy(text_entry.definition.default_config)},
            {"node_binding_id": variable_id, "component_id": "tools.variable-register", "component_version": "1",
             "title": "Session variable", "position": {"x": 0, "y": 160},
             "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
             "config": variable_config},
            {"node_binding_id": output_id, "component_id": "tools.output", "component_version": "1",
             "title": "Public output", "position": {"x": 260, "y": 0}, "public_outputs": ["output"],
             "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
             "config": output_config},
        ],
        "edges": [],
        "execution_roots": [output_id],
        "control_edges": [],
        "event_bindings": [
            {"event_id": "management-event", "schema_version": 1, "display_name": "Management event",
             "audience": "management", "target_node_ids": [variable_id],
             "payload_schema": {"type": "object"}},
            {"event_id": "consumer-event", "schema_version": 1, "display_name": "Consumer event",
             "audience": "consumer", "target_node_ids": [variable_id],
             "payload_schema": {"type": "object"}},
        ],
    }


def _assert_no_execution_or_package_baseline(database, *, type_contracts=()):
    assert not (database.parent / (database.name + ".graph.lock")).exists()
    assert not (database.parent / (database.name + ".credentials.sqlite")).exists()
    with closing(SqliteStore(database)) as store:
        assert store._connection.execute(
            "SELECT COUNT(*) FROM graph_project_packages").fetchone()[0] == 0
        registered_types = tuple(tuple(row) for row in store._connection.execute(
            "SELECT scope,type_id,schema_version FROM registered_type_contracts "
            "ORDER BY scope,type_id,schema_version"))
        assert registered_types == tuple(type_contracts)
        columns = {row["name"] for row in store._connection.execute("PRAGMA table_info(provider_key_requests)")}
        assert not columns


def _database_snapshot(database):
    connection = sqlite3.connect(database)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
        )]
        return {table: tuple(sorted(
            connection.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr,
        )) for table in tables}
    finally:
        connection.close()


@pytest.mark.parametrize("explicit_manager", [False, True])
def test_http_graph_management_uses_only_the_lightweight_service(
        tmp_path, monkeypatch, explicit_manager):
    database = tmp_path / ("explicit-management.sqlite" if explicit_manager else "workflow-host.sqlite")
    _reject_execution_initialization(monkeypatch)
    owner = GraphManagementService(database) if explicit_manager else WorkflowHost(database)
    server, worker, port = _start_server(owner)
    try:
        status, listed = _request(port, "GET", "/api/graph/definitions")
        assert status == 200 and listed == []
        status, drafts = _request(port, "POST", "/api/graph/queries", {
            "operation": "draft.list", "parameters": {"kind": "workbench"},
        })
        assert status == 200 and drafts["records"] == []
        status, draft = _request(port, "POST", "/api/graph/queries", {
            "operation": "draft.read", "parameters": {"address": _draft_address()},
        })
        assert status == 200 and draft is None

        manager = (owner if explicit_manager else owner.graph_management_service)
        document = _new_incomplete_document(manager)
        status, saved = _request(port, "POST", "/api/graph/commands", {
            "operation": "definition.update",
            "parameters": {
                "workflow_definition_id": document["workflow_definition_id"],
                "document": document, "expected_edit_sequence": 0,
                "idempotency_key": "management-configuration-save",
            },
        })
        assert status == 200 and saved["result"]["outcome"] == "saved"
        assert saved["result"]["definition_revision"] == 1
        assert saved["result"]["edit_sequence"] == 1

        changed = deepcopy(saved["result"]["document"])
        changed["name"] = "Display-only edit"
        status, updated = _request(port, "POST", "/api/graph/commands", {
            "operation": "definition.update",
            "parameters": {
                "workflow_definition_id": document["workflow_definition_id"],
                "document": changed, "expected_edit_sequence": 1,
                "idempotency_key": "management-display-save",
            },
        })
        assert status == 200
        assert (updated["result"]["definition_revision"], updated["result"]["edit_sequence"]) == (1, 2)

        current = _request(port, "GET",
                           f"/api/graph/definitions/{document['workflow_definition_id']}")[1]
        fixed = _request(port, "GET",
                         f"/api/graph/definitions/{document['workflow_definition_id']}/revisions/1")[1]
        assert current["name"] == "Display-only edit"
        assert fixed["name"] == "Incomplete graph"

        status, created = _request(port, "POST", "/api/graph/commands", {
            "operation": "session.create",
            "parameters": {
                "workflow_definition_id": document["workflow_definition_id"],
                "definition_revision": 1, "idempotency_key": "management-session-create",
            },
        })
        assert status == 201
        sid = created["result"]["workflow_session_id"]
        session_status, session = _request(port, "GET", f"/api/graph/sessions/{sid}")
        assert session_status == 200 and session["definition_revision"] == 1
        assert session["status"] == "idle"

        status, registrations = _request(port, "POST", "/api/graph/registrations/list", {})
        assert status == 200 and any(item["kind"] == "node" for item in registrations["items"])
        variable_id = next(node["node_binding_id"] for node in document["nodes"]
                           if node["component_id"] == "tools.variable-register")
        output_id = next(node["node_binding_id"] for node in document["nodes"]
                         if node["component_id"] == "tools.output")
        endpoint = f"/api/graph/sessions/{sid}"
        status, object_value = _request(port, "POST", endpoint + "/objects/read", {
            "object_key": "http-value", "node_id": variable_id,
        })
        assert status == 200 and object_value["value"] == {
            "registered": False, "name": None, "value_type": None, "assigned": False, "value": None,
        }
        write_value = {"registered": True, "name": "value", "value_type": "string",
                       "assigned": True, "value": "management write"}
        status, changed_objects = _request(port, "POST", endpoint + "/objects/write", {
            "node_id": variable_id, "writes": [
                WriteIntent("http-value", 1, "management-http-write", write_value).to_dict(),
            ],
            "expected_revision": session["revision"], "idempotency_key": "management-http-write",
        })
        assert status == 200
        session = changed_objects["session"]
        assert changed_objects["results"][0]["revision"] == 2
        assert session["objects"]["http-value"]["value"] == write_value

        status, denied_writer = _request(port, "POST", endpoint + "/objects/write", {
            "node_id": str(uuid4()), "writes": [
                WriteIntent("http-value", 2, "management-http-denied", write_value).to_dict(),
            ],
            "expected_revision": session["revision"], "idempotency_key": "management-http-denied",
        })
        assert status == 403 and denied_writer["error"]["reason_code"] == "session_object_access_denied"
        status, stale_writer = _request(port, "POST", endpoint + "/objects/write", {
            "node_id": variable_id, "writes": [
                WriteIntent("http-value", 2, "management-http-stale", write_value).to_dict(),
            ],
            "expected_revision": session["revision"] - 1, "idempotency_key": "management-http-stale",
        })
        assert status == 409 and stale_writer["error"]["reason_code"] == "stale_revision"

        status, artifact_denied = _request(port, "POST", endpoint + "/objects/artifacts/read", {
            "object_key": "http-value", "node_id": variable_id,
            "reference": {"scope": "artifact", "output_id": str(uuid4())},
        })
        assert status == 403
        status, registrations = _request(port, "POST", endpoint + "/information/read", {
            "reference": {"source_id": "missing-source", "exact_version": "1"},
            "owner": {"workflow_session_id": sid, "chain_run_id": str(uuid4()),
                      "node_binding_id": variable_id, "node_run_id": str(uuid4())},
            "generation": 1, "source_scope": "history",
        })
        assert status == 404
        status, missing_manifest = _request(port, "POST", endpoint + "/manifests/read", {
            "snapshot_id": str(uuid4()),
        })
        assert status == 404

        output_parameters = {"workflow_definition_id": document["workflow_definition_id"],
                             "definition_revision": 1, "node_id": output_id, "port_id": "output"}
        status, output = _request(port, "POST", endpoint + "/outputs/read", output_parameters)
        assert status == 200 and output["output"]["availability"] == "unproduced"
        status, output_history = _request(port, "POST", endpoint + "/outputs/history", output_parameters)
        assert status == 200 and output_history["outputs"] == []

        event_parameters = {"workflow_definition_id": document["workflow_definition_id"],
                            "definition_revision": 1}
        status, events = _request(port, "POST", endpoint + "/events/bindings", event_parameters)
        assert status == 200 and {item["audience"] for item in events["bindings"]} == {
            "management", "consumer",
        }
        status, consumer_events = _request(port, "POST", endpoint + "/consumer/events/bindings", event_parameters)
        assert status == 200 and {item["audience"] for item in consumer_events["bindings"]} == {"consumer"}
        missing_chain = str(uuid4())
        status, event = _request(port, "POST", endpoint + "/events/read", {"chain_id": missing_chain})
        assert status == 404
        status, consumer_event = _request(port, "POST", endpoint + "/consumer/events/read", {
            "chain_id": missing_chain,
        })
        assert status == 404

        status, archived = _request(port, "POST", "/api/graph/commands", {
            "operation": "session.archive",
            "parameters": {"session_id": sid, "expected_revision": session["revision"],
                           "idempotency_key": "management-idle-archive"},
        })
        assert status == 200 and archived["result"]["archived"]
        assert _request(port, "GET", f"/api/graph/sessions/{sid}")[1]["archived"]

        assert owner._graph is None if isinstance(owner, WorkflowHost) else owner._execution_owner is False
        if explicit_manager:
            assert owner._lease is None and owner._executor is None
        else:
            manager = owner._management_graph
            assert manager is not None and manager._lease is None and manager._executor is None
        _assert_no_execution_or_package_baseline(
            database, type_contracts=(("session", VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION),))
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()
        owner.close()


def test_http_context_adoption_uses_management_without_restarting_execution(tmp_path, monkeypatch):
    from test_context_native_integration import NativeTransport, native_graph, node_output
    from test_graph_service import create
    from test_models_service_integration import ModelDatabaseFixture
    from workflow_test_support import run

    database = tmp_path / "context-adoption-management.sqlite"
    transport = NativeTransport()
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, entries = native_graph(service, policy=False, once=False)
        completed = run(service, create(service, document), "Accepted context for management adoption.")
        sid = completed["workflow_session_id"]
        writer = entries["a"]["merge"]["node_binding_id"]
        view_output = node_output(completed, entries["a"]["merge"], "commit")["view_ref"]["output_id"]
        assert completed["objects"]["context-a"]["revision"] == 2
        model_calls = len(transport.calls)
        information_bindings = GraphApplication(service).query("registration.list", {
            "session_id": sid, "chain_id": completed["selected_chain_run_id"],
            "kind": "information_binding",
        })["items"]
        assert information_bindings
        information_binding = information_bindings[0]

    _reject_execution_initialization(monkeypatch)
    owner = WorkflowHost(database)
    server, worker, port = _start_server(owner)
    try:
        status, unavailable = _request(port, "POST", f"/api/graph/sessions/{sid}/information/read", {
            "reference": information_binding["registration_ref"],
            "owner": information_binding["owner"],
            "generation": information_binding["generation"],
            "source_scope": "live",
        })
        assert status == 409
        assert unavailable["error"]["reason_code"] == "information_live_unavailable"
        status, adopted = _request(port, "POST", f"/api/graph/sessions/{sid}/context/adopt", {
            "node_id": writer, "view_output_id": view_output,
            "expected_revision": completed["revision"], "idempotency_key": "http-context-adopt",
        })
        assert status == 200 and adopted["results"] == []
        assert adopted["session"]["workflow_session_id"] == sid
        assert adopted["session"]["objects"]["context-a"] == completed["objects"]["context-a"]
        assert len(transport.calls) == model_calls
        assert owner._graph is None
        assert owner._management_graph is not None
        assert owner._management_graph._lease is None and owner._management_graph._executor is None
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()
        owner.close()


def test_definition_session_and_run_history_reads_do_not_create_execution_owner(tmp_path, monkeypatch):
    from test_graph_service import create, run, text_graph

    database = tmp_path / "history-read.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        document = text_graph(service.registry)
        completed = run(service, create(service, document))
        sid = completed["workflow_session_id"]
        chain_id = completed["selected_chain_run_id"]
        expected_run = service.get_run(sid, chain_id)
    before = _database_snapshot(database)

    _reject_execution_initialization(monkeypatch)
    host = WorkflowHost(database)
    try:
        app = GraphApplication(host)
        assert app.query("definition.read", {"identity": document["workflow_definition_id"]}) == document
        assert app.query("session.read", {"session_id": sid}) == completed
        assert app.query("run.read", {"session_id": sid, "chain_id": chain_id}) == expected_run
        assert host._graph is None
        assert host._management_graph is not None
        assert host._management_graph._lease is None and host._management_graph._executor is None
        assert _database_snapshot(database) == before
    finally:
        host.close()


@pytest.mark.parametrize("action", ["archive", "delete"])
def test_lifecycle_command_reuses_the_existing_execution_coordinator(tmp_path, action):
    from phase1_agent.graph_application import GraphApplication
    from test_graph_service import gate_graph

    host = WorkflowHost(tmp_path / f"active-{action}.sqlite")
    service = host.graph_service
    sid, chain_id, release = gate_graph(service)
    before = service.get_session(sid)
    called, completed = Event(), Event()
    results, failures = [], []

    def manage():
        called.set()
        try:
            results.append(GraphApplication(host).command("session." + action, {
                "session_id": sid,
                "expected_revision": before["revision"],
                "idempotency_key": "active-lifecycle-" + action,
            })["result"])
        except BaseException as error:
            failures.append((error, traceback.format_exc()))
        finally:
            completed.set()

    thread = Thread(target=manage)
    try:
        thread.start()
        assert called.wait(2)
        assert not completed.wait(0.2)
        assert host._graph is service
        release.set()
        assert completed.wait(5)
        thread.join(5)
        assert not thread.is_alive() and not failures, failures[0][1] if failures else ""
        assert results[0]["archived" if action == "archive" else "deleted"]
        if action == "archive":
            assert service.get_run(sid, chain_id)["chain"]["status"] == "closed"
        else:
            with pytest.raises(ContractValidationError) as removed:
                service.get_session(sid)
            assert removed.value.reason_code == "not_found"
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(5)
        try:
            host.close()
        except BaseException as error:
            if not failures:
                raise


def test_failed_delete_keeps_execution_references_and_allows_close(tmp_path):
    from phase1_agent.graph_application import GraphApplication
    from test_graph_service import gate_graph

    host = WorkflowHost(tmp_path / "failed-delete.sqlite")
    service = host.graph_service
    sid, chain_id, release = gate_graph(service)
    future = service._futures[chain_id]
    fault_entered, finish_fault, called, completed = Event(), Event(), Event(), Event()
    marker = RuntimeError("retained execution error evidence")
    failures = []

    def fail_delete_commit(point):
        current = service._futures.get(chain_id)
        if point == "before_commit" and current is not None and current.done():
            service._execution_errors[chain_id] = marker
            fault_entered.set()
            assert finish_fault.wait(5)
            raise RuntimeError("delete transaction failed")

    def delete():
        called.set()
        try:
            GraphApplication(host).command("session.delete", {
                "session_id": sid,
                "expected_revision": service.get_session(sid)["revision"],
                "idempotency_key": "failed-active-delete",
            })
        except BaseException as error:
            failures.append(error)
        finally:
            completed.set()

    service._fault_injector = fail_delete_commit
    thread = Thread(target=delete)
    try:
        thread.start()
        assert called.wait(2)
        assert not completed.wait(0.2)
        release.set()
        assert fault_entered.wait(5)
        connection = sqlite3.connect(service.database)
        try:
            retained_session = json.loads(connection.execute(
                "SELECT payload FROM records WHERE record_type='workflow_session' AND record_id=?",
                (sid,),
            ).fetchone()[0])
            retained_chain = json.loads(connection.execute(
                "SELECT payload FROM records WHERE record_type='chain_run' AND record_id=?",
                (chain_id,),
            ).fetchone()[0])
        finally:
            connection.close()
        finish_fault.set()
        assert completed.wait(5)
        thread.join(5)
        assert not thread.is_alive()
        assert len(failures) == 1 and str(failures[0]) == "delete transaction failed"
        assert service._futures[chain_id] is future
        assert service._execution_errors[chain_id] is marker
        assert service.get_session(sid)["workflow_session_id"] == retained_session["workflow_session_id"]
        assert service.get_run(sid, chain_id)["chain"] == retained_chain
        with closing(SqliteStore(service.database)) as store:
            assert store.read_receipt("graph.session.delete", "failed-active-delete") is None
    finally:
        release.set()
        finish_fault.set()
        if thread.ident is not None:
            thread.join(5)
        service._fault_injector = None
        service._execution_errors.pop(chain_id, None)
        host.close()
