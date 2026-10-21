"""T0-T1 project support: acquisition roles, storage environment and ownership.

These narrow checks cover only the new relations of the project base runtime
(136 号 §7.1 V1-V6): lazy construction, management/execution acquisition,
environment consumption, fault-injection rollback, borrow/self-built close
ownership, HTTP acquisition organization and honest placeholder state.  The
workflow's own domain behavior stays covered by its existing test files.
"""

import http.client
import json
import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService
from phase1_agent.project_support import (
    ProjectDefinitionManagement,
    ProjectExecutionManagement,
    ProjectStorageEnvironment,
)
from phase1_agent.server import create_server
from phase1_agent.storage import STORAGE_VERSION, SqliteStore
from phase1_agent.workflow_host import WorkflowHost

from test_graph_service import run, text_graph


def _request(port, method, path, payload=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if method == "POST":
        headers["Origin"] = "http://127.0.0.1:" + str(port)
    body = json.dumps(payload) if payload is not None else None
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None
    finally:
        connection.close()


def _raw_get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@contextmanager
def _running(owner, *, graph_service=None):
    server = create_server(owner, port=0, graph_service=graph_service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


def _side_effect_paths(database):
    database = Path(database)
    return (database, Path(str(database) + ".graph.lock"),
            Path(str(database) + ".credentials.sqlite"))


def _assert_nothing_created(database):
    for path in _side_effect_paths(database):
        assert not path.exists(), path


def _editable_graph(service):
    """Promote the shared text fixture into one complete schema-3 document."""
    document = deepcopy(text_graph(service.registry))
    document["schema_version"] = 3
    for entry in document["nodes"]:
        entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    return document


def _save(service, document, key):
    return service.update_definition(
        document, workflow_definition_id=document["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=key)


def _receipt(database, key):
    with closing(SqliteStore(database)) as store:
        return store.read_receipt("graph.definition.update", key)


def _assert_store_closed(store):
    with pytest.raises(sqlite3.ProgrammingError):
        store._connection.execute("SELECT 1")


def _table_names(database):
    connection = sqlite3.connect(database)
    try:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        connection.close()


def test_host_environment_and_definition_construction_has_no_side_effects(tmp_path):
    database = tmp_path / "not-created" / "project.sqlite"
    environment = ProjectStorageEnvironment(database)
    definitions = ProjectDefinitionManagement()
    host = WorkflowHost(database)
    try:
        assert environment.database == database.resolve()
        assert host.database == database.resolve()
        assert isinstance(host.environment, ProjectStorageEnvironment)
        assert host.environment.database == host.database
        assert isinstance(host.definitions, ProjectDefinitionManagement)
        assert isinstance(host.execution_management, ProjectExecutionManagement)
        assert host._graph is None and host._management_graph is None
        # T2: definition management carries the workflow source access declaration
        # instead of an empty placeholder; querying it still has no business side effect.
        assert definitions.registered_sub_capabilities() == ("workflow.graph",)
        assert definitions.is_populated() is True
        assert definitions.describe()["sub_capabilities"] == ["workflow.graph"]
        assert definitions.describe()["connected"] is True
        assert host.definitions.registered_sub_capabilities() == ("workflow.graph",)
        assert host.definitions.is_populated() is True
        assert host.definitions.describe() == definitions.describe()
        assert host._graph is None and host._management_graph is None
        _assert_nothing_created(database)
    finally:
        host.close()
    _assert_nothing_created(database)


def test_explicit_database_mismatch_is_rejected_before_use(tmp_path):
    allowed = tmp_path / "allowed" / "main.sqlite"
    environment = ProjectStorageEnvironment(allowed)
    builders = (
        lambda database: GraphWorkflowService(database, environment=environment),
        lambda database: GraphManagementService(database, environment=environment),
        lambda database: WorkflowHost(database, environment=environment),
    )
    for build in builders:
        target = tmp_path / "rejected" / "main.sqlite"
        with pytest.raises(ContractValidationError) as error:
            build(target)
        assert error.value.reason_code == "project_environment_mismatch"
        assert not target.parent.exists() and not target.exists()
        _assert_nothing_created(target)

    service = GraphWorkflowService(allowed, environment=environment)
    try:
        assert service.environment is environment
        assert service.database == allowed.resolve()
    finally:
        service.close()
    manager = GraphManagementService(allowed, environment=environment)
    try:
        assert manager.environment is environment
        assert manager.database == allowed.resolve()
    finally:
        manager.close()
    host = WorkflowHost(allowed, environment=environment)
    try:
        assert host.environment is environment
    finally:
        host.close()


def test_management_first_acquisition_roles(tmp_path):
    database = tmp_path / "management-first.sqlite"
    host = WorkflowHost(database)
    try:
        assert host.execution_management.management_entry() is host
        management = host.graph_management_service
        assert management._execution_owner is False
        assert management._lease is None and management._executor is None
        assert host._graph is None
        assert host.execution_management.management_entry() is host
        assert host.execution_management.lifecycle_entry() is management
        assert not Path(str(host.database) + ".graph.lock").exists()

        execution = host.execution_management.execution_entry()
        assert execution is host.graph_service is host._graph
        assert execution._execution_owner is True and execution._lease is not None
        assert host.execution_management.execution_entry() is execution
        assert host.execution_management.management_entry() is execution
        assert host.execution_management.lifecycle_entry() is execution
        assert host.graph_management_service is execution
        assert management.environment is host.environment
        assert execution.environment is host.environment
        assert Path(str(host.database) + ".graph.lock").exists()
    finally:
        host.close()
    assert management._closed is True and execution._closed is True


def test_execution_first_acquisition_roles(tmp_path):
    database = tmp_path / "execution-first.sqlite"
    host = WorkflowHost(database)
    try:
        assert host.execution_management.management_entry() is host
        execution = host.execution_management.execution_entry()
        assert execution is host.graph_service is host._graph
        assert host._management_graph is None
        assert host.execution_management.management_entry() is execution
        assert host.execution_management.execution_entry() is execution
        assert host.execution_management.lifecycle_entry() is execution
        assert host.graph_management_service is execution
    finally:
        host.close()
    assert execution._closed is True


def test_environment_is_consumed_by_real_management_and_execution(tmp_path, monkeypatch):
    database = tmp_path / "consumed.sqlite"
    host = WorkflowHost(database)
    stores = []
    original_open = host.environment.open_store

    def record(*, fault_injector=None):
        store = original_open(fault_injector=fault_injector)
        stores.append(store)
        return store

    monkeypatch.setattr(host.environment, "open_store", record)
    try:
        management = host.graph_management_service
        assert management.environment is host.environment
        assert host._graph is None
        document = _editable_graph(management)
        saved = _save(management, document, "consumed-save")
        assert saved["outcome"] == "saved"

        independent = GraphManagementService(host.database)
        try:
            assert independent.environment is not host.environment
            listed = independent.list_definitions()
            assert [item["document"]["workflow_definition_id"] for item in listed] == [
                document["workflow_definition_id"]]
            reread = independent.get_definition(document["workflow_definition_id"])
            assert reread["name"] == document["name"]
            assert {entry["node_binding_id"] for entry in reread["nodes"]} == {
                entry["node_binding_id"] for entry in document["nodes"]}
        finally:
            independent.close()

        execution = host.execution_management.execution_entry()
        assert execution.environment is host.environment
        session = execution.create_session(
            document["workflow_definition_id"], saved["definition_revision"],
            idempotency_key="consumed-session")
        completed = run(execution, session)
        assert completed["status"] == "succeeded"
        assert completed["chains"][0]["status"] == "succeeded"
    finally:
        host.close()

    assert len(stores) >= 3
    assert all(isinstance(store, SqliteStore) for store in stores)
    assert len({id(store) for store in stores}) == len(stores)
    for store in stores:
        _assert_store_closed(store)


def test_fault_injection_rolls_back_business_and_receipt(tmp_path):
    database = tmp_path / "fault.sqlite"
    host = WorkflowHost(database)
    service = host.graph_service
    document = _editable_graph(service)
    identity = document["workflow_definition_id"]
    key = "faulted-definition-save"
    armed = [True]

    def fail_before_commit(point):
        if armed[0] and point == "before_commit":
            raise RuntimeError("injected before commit")

    service._fault_injector = fail_before_commit
    try:
        with pytest.raises(RuntimeError):
            _save(service, document, key)
        assert service.list_definitions() == []
        with pytest.raises(ContractValidationError) as missing:
            service.get_definition(identity)
        assert missing.value.reason_code == "not_found"
        assert _receipt(host.database, key) is None

        armed[0] = False
        saved = _save(service, deepcopy(document), key)
        assert saved["outcome"] == "saved"
        replayed = _save(service, deepcopy(document), key)
        assert replayed == saved
        assert _receipt(host.database, key) == [{"graph_result": saved}]
    finally:
        service._fault_injector = None
        host.close()


def test_server_borrows_explicit_services(tmp_path):
    database = tmp_path / "borrowed.sqlite"
    service = GraphWorkflowService(database)
    try:
        with _running(service) as port:
            status, listed = _request(port, "GET", "/api/graph/definitions")
            assert status == 200 and listed == []
        assert service._closed is False
        assert service.list_definitions() == []
    finally:
        service.close()

    injected_database = tmp_path / "injected.sqlite"
    injected = GraphWorkflowService(injected_database)
    try:
        with _running(SimpleNamespace(database=injected_database),
                      graph_service=injected) as port:
            status, listed = _request(port, "GET", "/api/graph/definitions")
            assert status == 200 and listed == []
        assert injected._closed is False
        assert injected.list_definitions() == []
    finally:
        injected.close()


def test_server_closes_compat_owned_objects(tmp_path, monkeypatch):
    import phase1_agent.workflow_host as workflow_host_module

    database = tmp_path / "compat.sqlite"
    created_hosts = []
    real_host = workflow_host_module.WorkflowHost

    class RecordingHost(real_host):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_hosts.append(self)

    monkeypatch.setattr(workflow_host_module, "WorkflowHost", RecordingHost)
    with _running(SimpleNamespace(database=database)) as port:
        status, listed = _request(port, "GET", "/api/graph/definitions")
        assert status == 200 and listed == []
        assert len(created_hosts) == 1
        managed = created_hosts[0]
        assert managed._management_graph is not None
        status, error = _request(port, "POST", "/api/graph/commands", {
            "operation": "packages.configure", "parameters": {},
        })
        assert status == 400 and error["error"]["reason_code"] == "invalid_request"

    assert managed._management_graph._closed is True
    reopened = GraphWorkflowService(database)
    try:
        assert reopened.list_definitions() == []
    finally:
        reopened.close()


def test_normal_host_close_releases_execution_lease(tmp_path):
    database = tmp_path / "leased.sqlite"
    host = WorkflowHost(database)
    try:
        service = host.execution_management.execution_entry()
        document = _editable_graph(service)
        saved = _save(service, document, "leased-save")
        assert saved["outcome"] == "saved"
        assert Path(str(database) + ".graph.lock").exists()
    finally:
        host.close()
    assert service._closed is True

    reopened = GraphWorkflowService(database)
    try:
        assert reopened.environment is not host.environment
        listed = reopened.list_definitions()
        assert [item["document"]["workflow_definition_id"] for item in listed] == [
            document["workflow_definition_id"]]
    finally:
        reopened.close()


def test_http_dispatch_uses_acquisition_entries(tmp_path, monkeypatch):
    import phase1_agent.graph_http as graph_http

    database = tmp_path / "http-acquisition.sqlite"
    host = WorkflowHost(database)
    real_dispatch = graph_http.dispatch_graph
    targets = []

    def record(target, method, path, data=None):
        targets.append((target, method, path))
        return real_dispatch(target, method, path, data)

    monkeypatch.setattr(graph_http, "dispatch_graph", record)
    try:
        with _running(host) as port:
            assert _raw_get(port, "/api/health")[0] == 200
            assert _raw_get(port, "/static/style.css")[0] == 200
            assert _request(port, "GET", "/api/project/support")[0] == 404
            assert targets == []
            _assert_nothing_created(database)

            status, listed = _request(port, "GET", "/api/graph/definitions")
            assert status == 200 and listed == []
            assert targets[-1][0] is host
            assert host._graph is None

            status, error = _request(port, "POST", "/api/graph/commands", {
                "operation": "packages.configure", "parameters": {},
            })
            assert status == 400 and error["error"]["reason_code"] == "invalid_request"
            assert targets[-1][0] is host.graph_service
            assert host._graph is host.graph_service

            status, listed = _request(port, "GET", "/api/graph/definitions")
            assert status == 200 and listed == []
            assert targets[-1][0] is host.graph_service
            assert host._management_graph is not None

        assert host._graph is host.graph_service and host._graph._closed is False
        assert host._management_graph._closed is False
    finally:
        host.close()


def test_thin_execution_keeps_existing_storage_and_definition_placeholder(tmp_path):
    assert STORAGE_VERSION == 20
    baseline_database = tmp_path / "baseline.sqlite"
    with closing(SqliteStore(baseline_database)):
        pass
    baseline = _table_names(baseline_database)
    control = GraphWorkflowService(tmp_path / "workflow-control.sqlite")
    control.close()
    workflow_tables = _table_names(control.database)

    host = WorkflowHost(tmp_path / "project-host.sqlite")
    try:
        assert not hasattr(host.definitions, "node_types")
        assert not hasattr(host.definitions, "catalog")
        assert host.definitions.registered_sub_capabilities() == ("workflow.graph",)
        assert host.definitions.describe()["connected"] is True
        host.execution_management.execution_entry()
        tables = _table_names(host.database)
        # The project support framework adds no tables of its own: the base
        # storage initialization and the existing workflow service account for
        # every table that exists after the execution entry is acquired.
        assert baseline <= workflow_tables and tables == workflow_tables
        assert not any(name.startswith("project") for name in tables)
        with closing(SqliteStore(host.database)) as store:
            operations = {row[0] for row in store._connection.execute(
                "SELECT operation FROM idempotency")}
        assert not any(name.startswith("project") for name in operations)
    finally:
        host.close()
