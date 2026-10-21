"""T2 capability declaration: query, validation, dependency diagnostics and trusted acquisition.

Covers 140 号 §6.1/§9.1 V2: the workflow source's minimal access declaration is
accepted and queried by the project, and the Host really acquires each role
along the declared trusted callable binding.  Missing core declarations, wrong
bindings, broken dependency references and environment mismatches fail
explicitly with safe reason codes; ordinary management and definition reads work
without any model configuration; static validation creates no database,
execution lease or credential store; close/borrow boundaries stay as delivered
by T0-T1.
"""

import http.client
import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

import phase1_agent.workflow_capability as workflow_capability
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService
from phase1_agent.project_support import ProjectDefinitionManagement, ProjectStorageEnvironment
from phase1_agent.server import create_server
from phase1_agent.workflow_host import WorkflowHost

from test_graph_service import text_graph


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


@contextmanager
def _running(owner):
    server = create_server(owner, port=0)
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


def _base_declaration():
    return workflow_capability.local_workflow_capability_source().declarations[0]


def _host_with(declaration, database, **options):
    source = workflow_capability.WorkflowCapabilitySource(
        workflow_capability.SOURCE_ID, (declaration,))
    return WorkflowHost(database, capability_source=source, **options)


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


def test_workflow_source_declaration_is_queryable_from_two_read_points(tmp_path):
    database = tmp_path / "not-created" / "capability.sqlite"
    standalone = ProjectDefinitionManagement()
    host = WorkflowHost(database)
    try:
        assert standalone.is_populated() is True
        assert host.definitions.is_populated() is True
        assert standalone.registered_sub_capabilities() == ("workflow.graph",)
        assert standalone.capabilities() == host.definitions.capabilities()
        assert standalone.describe() == host.definitions.describe()

        declaration = host.definitions.capability("workflow.graph")
        assert declaration["capability"] == {
            "schema_version": 1,
            "capability_id": "workflow.graph",
            "capability_kind": "workflow",
            "access_contract_version": 1,
            "source": {"source_id": "workflow.local-source", "source_kind": "local-source",
                       "source_version": "1.0.0", "maintainer": "workflow"},
        }
        roles = {row["role"]: row for row in declaration["roles"]}
        assert set(roles) == {"management", "execution"}
        assert {row["service_contract"] for row in roles.values()} == {
            "workflow.management-service", "workflow.execution-service"}
        assert {row["capability_id"] for row in roles.values()} == {"workflow.graph"}

        assert declaration["environment"] == {
            "schema_version": 1,
            "contract_id": "project.storage-environment",
            "contract_version": 1,
            "requirements": ["same_database_path", "per_operation_store"],
        }
        assert declaration["lifecycle"]["owner"] == "project.workflow-host"
        assert declaration["lifecycle"]["close_entry"] == "close"
        assert set(declaration["lifecycle"]["roles"]) == {"management", "execution"}

        operations = set()
        management_operations = set()
        for group in declaration["public_contracts"]["operation_groups"]:
            operations.update(group["operations"])
            if group["scope"] == "management":
                management_operations.update(group["operations"])
        assert {"definition.update", "run.start", "request-evidence.write"} <= operations
        # The already-landed E1 public management contracts (summaries queries
        # and the copy command) are real public contracts of this capability.
        assert {"definition.summaries", "session.summaries", "definition.copy"} <= (
            management_operations)
        assert declaration["public_contracts"]["service_definitions"] == [
            {"service_id": "workflow.models", "exact_version": "2.0.0"}]
        assert {row["extension_id"] for row in declaration["frontend"]["extensions"]} == {
            "workflow.frontend.node-entry-fields-v2",
            "workflow.frontend.chat-session-object",
        }
        assert declaration["frontend"]["containers"] == ["workbench"]

        report = host.definitions.dependency_report()
        assert [row["dependency_id"] for row in report] == [
            "dep.storage-environment", "dep.resource-content", "dep.provider-model",
            "dep.model-request-service", "dep.credentials", "dep.request-evidence",
            "dep.frontend-access",
        ]
        for row in report:
            assert row["static_result"] == "present", row
            assert row["requirement"] in ("required", "conditional", "optional")
            assert row["caller"] and row["provider"] and row["purpose"] and row["condition"]
            assert row["carrier"]
            assert row["call_site"]["module"].startswith("phase1_agent.")
            assert row["contracts"]
        kinds = {contract["kind"] for row in report for contract in row["contracts"]}
        assert kinds == {
            "environment", "operation", "service_definition", "private_store",
            "frontend_package", "frontend_extension", "frontend_container",
        }

        # Provider/content/model supply stays a contract reference: only the
        # workflow capability is registered as a project sub-capability.
        assert set(host.definitions.registered_sub_capabilities()) == {"workflow.graph"}
        assert host.definitions.diagnostics() == []
        assert host.definitions.describe()["connected"] is True
        assert host._graph is None and host._management_graph is None
    finally:
        host.close()
    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_host_first_role_acquisition_consumes_declared_trusted_binding(tmp_path, monkeypatch):
    database = tmp_path / "declared-bindings.sqlite"
    calls = []
    real_management = workflow_capability.build_management_service
    real_execution = workflow_capability.build_execution_service

    def recording_management(*, database, environment):
        calls.append(("management", database, environment))
        return real_management(database=database, environment=environment)

    def recording_execution(*, database, environment):
        calls.append(("execution", database, environment))
        return real_execution(database=database, environment=environment)

    monkeypatch.setattr(workflow_capability, "build_management_service", recording_management)
    monkeypatch.setattr(workflow_capability, "build_execution_service", recording_execution)
    host = WorkflowHost(database)
    try:
        assert calls == []
        management = host.graph_management_service
        assert type(management) is GraphManagementService
        assert management._execution_owner is False and management._lease is None
        assert host._graph is None and host._management_graph is management
        assert calls == [("management", host.database, host.environment)]
        binding = host.definitions.require_role_binding(
            "management", database=host.database, environment=host.environment)
        assert binding.factory is recording_management

        execution = host.graph_service
        assert type(execution) is GraphWorkflowService
        assert execution._execution_owner is True and execution._lease is not None
        assert calls == [("management", host.database, host.environment),
                         ("execution", host.database, host.environment)]
        assert host.graph_service is execution
        assert host.graph_management_service is execution
        assert host.execution_management.management_entry() is execution
        assert host.execution_management.lifecycle_entry() is execution
        assert len(calls) == 2
        assert management.environment is host.environment
        assert execution.environment is host.environment
    finally:
        host.close()

    calls.clear()
    execution_first = tmp_path / "declared-bindings-execution-first.sqlite"
    host = WorkflowHost(execution_first)
    try:
        execution = host.execution_management.execution_entry()
        assert calls == [("execution", host.database, host.environment)]
        assert host._management_graph is None
        assert host.graph_management_service is execution
    finally:
        host.close()


def test_missing_core_declaration_blocks_acquisition_with_safe_reason_codes(tmp_path):
    base = _base_declaration()
    scenarios = (
        ("capability_environment_missing", replace(base, environment=None)),
        ("capability_role_missing",
         replace(base, roles=tuple(binding for binding in base.roles
                                   if binding.role != "execution"))),
        ("capability_access_contract_unsupported",
         replace(base, capability={**base.capability, "access_contract_version": 2})),
        ("capability_environment_contract_unsupported",
         replace(base, environment={**base.environment, "contract_version": 2})),
        ("capability_lifecycle_missing", replace(base, lifecycle=None)),
        ("capability_public_contract_missing", replace(base, public_contracts=None)),
        ("capability_frontend_missing", replace(base, frontend=None)),
        ("capability_dependencies_missing", replace(base, dependencies=())),
        ("capability_dependency_coverage_missing",
         replace(base, dependencies=(base.dependencies[0],))),
    )
    sensitive = ("secret", "password", "token", "bearer", "api_key", "private_key")
    for expected, declaration in scenarios:
        database = tmp_path / expected / "workflow.sqlite"
        host = _host_with(declaration, database)
        try:
            with pytest.raises(ContractValidationError) as error:
                host.graph_management_service
            assert error.value.reason_code == expected
            message = str(error.value).lower()
            assert all(word not in message for word in sensitive), message
            assert error.value.status_code == 409
            assert host._graph is None and host._management_graph is None
            diagnostics = host.definitions.diagnostics()
            assert diagnostics
            for item in diagnostics:
                assert {"reason_code", "severity", "message"} <= set(item)
                assert item["severity"] == "blocking"
        finally:
            host.close()
        _assert_nothing_created(database)
        assert not database.parent.exists()


def test_wrong_bindings_fail_explicitly_before_any_service_is_constructed(tmp_path):
    base = _base_declaration()
    management = next(binding for binding in base.roles if binding.role == "management")
    execution = next(binding for binding in base.roles if binding.role == "execution")
    called = []

    def unused_factory(*, database, environment):
        called.append((database, environment))
        return object()

    scenarios = (
        ("capability_binding_not_callable",
         (management, replace(execution, factory="phase1_agent.graph_service:GraphWorkflowService"))),
        ("capability_binding_mismatch",
         (management, replace(execution, capability_id="workflow.other", factory=unused_factory))),
        ("capability_role_duplicated",
         (management, replace(execution, role="management", factory=unused_factory))),
    )
    for expected, roles in scenarios:
        database = tmp_path / expected / "workflow.sqlite"
        host = _host_with(replace(base, roles=roles), database)
        try:
            with pytest.raises(ContractValidationError) as error:
                host.graph_management_service
            assert error.value.reason_code == expected
            assert host._graph is None and host._management_graph is None
        finally:
            host.close()
        _assert_nothing_created(database)
    assert called == []


def test_dependency_static_check_reports_condition_and_result_and_blocks_lying_entries(tmp_path):
    base = _base_declaration()
    storage = next(row for row in base.dependencies
                   if row.dependency_id == "dep.storage-environment")
    scenarios = (
        ("capability_dependency_call_site_missing",
         replace(storage, anchor="def workflow_call_site_anchor_that_does_not_exist(self)")),
        ("capability_dependency_contract_uncovered",
         replace(storage, contracts=(workflow_capability.CapabilityContractReference(
             "operation", "workflow.operation-not-in-declaration"),))),
    )
    for expected, dependency in scenarios:
        declaration = replace(base, dependencies=tuple(
            dependency if item.dependency_id == dependency.dependency_id else item
            for item in base.dependencies))
        database = tmp_path / expected / "workflow.sqlite"
        host = _host_with(declaration, database)
        try:
            report = {row["dependency_id"]: row
                      for row in host.definitions.dependency_report()}
            row = report[dependency.dependency_id]
            assert row["static_result"] == "missing"
            assert row["condition"] == storage.condition
            assert row["requirement"] == storage.requirement
            assert expected in row["problems"]
            assert host.definitions.describe()["connected"] is False
            matches = [item for item in host.definitions.diagnostics()
                       if item["reason_code"] == expected]
            assert matches and matches[0]["dependency_id"] == dependency.dependency_id
            assert matches[0]["condition"] == storage.condition
            assert matches[0]["result"] == "missing"
            with pytest.raises(ContractValidationError) as error:
                host.graph_management_service
            assert error.value.reason_code == expected
            assert host._management_graph is None
        finally:
            host.close()
        _assert_nothing_created(database)


def test_first_role_acquisition_rechecks_declared_environment(tmp_path):
    database = tmp_path / "environment-check" / "workflow.sqlite"
    host = WorkflowHost(database)
    try:
        host.environment = ProjectStorageEnvironment(tmp_path / "other" / "other.sqlite")
        with pytest.raises(ContractValidationError) as error:
            host.graph_management_service
        assert error.value.reason_code == "capability_environment_mismatch"
        assert host._graph is None and host._management_graph is None
    finally:
        host.close()
    _assert_nothing_created(database)
    assert not database.parent.exists()

    host = WorkflowHost(database)
    try:
        host.environment = SimpleNamespace(database=host.database)
        with pytest.raises(ContractValidationError) as error:
            host.graph_management_service
        assert error.value.reason_code == "capability_environment_mismatch"
        assert host._management_graph is None
    finally:
        host.close()
    _assert_nothing_created(database)


def test_ordinary_management_and_definition_reads_work_without_model_configuration(tmp_path):
    database = tmp_path / "ordinary.sqlite"
    host = WorkflowHost(database)
    try:
        assert host.execution_management.management_entry() is host
        service = host.graph_management_service
        document = _editable_graph(service)
        saved = _save(service, document, "declaration-ordinary-save")
        assert saved["outcome"] == "saved"
        listed = service.list_definitions()
        assert [row["document"]["workflow_definition_id"] for row in listed] == [
            document["workflow_definition_id"]]
        reread = service.get_definition(document["workflow_definition_id"])
        assert reread["name"] == document["name"]
        assert {entry["node_binding_id"] for entry in reread["nodes"]} == {
            entry["node_binding_id"] for entry in document["nodes"]}
        # Ordinary management activates no model/provider/content/evidence supply.
        assert host._graph is None
        assert host._providers is None and host._content is None and host._evidence is None
        assert not Path(str(database) + ".credentials.sqlite").exists()
        assert not Path(str(database) + ".graph.lock").exists()
        assert host.definitions.describe()["connected"] is True
    finally:
        host.close()


def test_static_declaration_queries_create_no_database_lease_or_credential_store(tmp_path):
    database = tmp_path / "not-created" / "static.sqlite"
    host = WorkflowHost(database)
    try:
        assert host.definitions.registered_sub_capabilities() == ("workflow.graph",)
        assert host.definitions.describe()["connected"] is True
        assert host.definitions.capability(
            "workflow.graph")["capability"]["capability_id"] == "workflow.graph"
        assert host.definitions.capabilities()
        assert host.definitions.dependency_report()
        assert host.definitions.diagnostics() == []
        assert host._graph is None and host._management_graph is None
        assert host._providers is None and host._content is None and host._evidence is None
    finally:
        host.close()
    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_declared_acquisition_preserves_close_release_and_server_borrow_boundaries(tmp_path):
    database = tmp_path / "close-release.sqlite"
    host = WorkflowHost(database)
    try:
        execution = host.execution_management.execution_entry()
        assert execution is host.graph_service
        document = _editable_graph(execution)
        assert _save(execution, document, "declaration-close-save")["outcome"] == "saved"
        assert Path(str(database) + ".graph.lock").exists()
    finally:
        host.close()
    assert execution._closed is True

    reopened = GraphWorkflowService(database)
    try:
        listed = reopened.list_definitions()
        assert [row["document"]["workflow_definition_id"] for row in listed] == [
            document["workflow_definition_id"]]
    finally:
        reopened.close()

    borrowed_database = tmp_path / "borrowed.sqlite"
    borrowed_host = WorkflowHost(borrowed_database)
    try:
        with _running(borrowed_host) as port:
            status, listed = _request(port, "GET", "/api/graph/definitions")
            assert status == 200 and listed == []
            assert borrowed_host._management_graph is not None
            assert borrowed_host._graph is None
        assert borrowed_host._management_graph._closed is False

        explicit = GraphWorkflowService(borrowed_database)
        try:
            with _running(explicit) as port:
                status, listed = _request(port, "GET", "/api/graph/definitions")
                assert status == 200 and listed == []
            assert explicit._closed is False
            assert explicit.list_definitions() == []
        finally:
            explicit.close()
    finally:
        borrowed_host.close()
