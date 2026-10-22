"""T0-T1 project support: acquisition roles, storage environment and ownership.

These narrow checks cover only the new relations of the project base runtime
(136 号 §7.1 V1-V6, extended by 147 R1 to the three fixed capability sources):
lazy construction, management/execution acquisition, environment consumption,
fault-injection rollback, borrow/self-built close ownership, HTTP acquisition
organization, three-source definition-management queries and honest placeholder
state.  The workflow's own domain behavior stays covered by its existing test
files.
"""

import http.client
import json
import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

import phase1_agent.content_capability as content_capability
import phase1_agent.provider_capability as provider_capability
from phase1_agent.content_configuration import ContentConfigurationService
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_provider_host import ProviderConfigurationService
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService
from phase1_agent.project_capability import CapabilitySource
from phase1_agent.project_support import (
    MANAGEMENT_ROLE,
    ProjectDefinitionManagement,
    ProjectExecutionManagement,
    ProjectStorageEnvironment,
    local_project_capability_sources,
)
from phase1_agent.server import create_server
from phase1_agent.storage import STORAGE_VERSION, SqliteStore
from phase1_agent.workflow_host import WorkflowHost

from test_graph_service import run, text_graph

FIXED_CAPABILITY_IDS = ("content.global", "provider.configuration", "workflow.graph")


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
        # T2/147 R1: definition management carries the three fixed capability
        # sources' access declarations instead of an empty placeholder; querying
        # it still has no business side effect.
        assert definitions.registered_sub_capabilities() == FIXED_CAPABILITY_IDS
        assert definitions.is_populated() is True
        described = definitions.describe()
        assert described["sub_capabilities"] == list(FIXED_CAPABILITY_IDS)
        assert described["connected"] is True
        assert described["diagnostics"] == []
        assert [row["source_id"] for row in described["sources"]] == [
            "workflow.local-source", "provider.local-source", "content.local-source"]
        assert host.definitions.registered_sub_capabilities() == FIXED_CAPABILITY_IDS
        assert host.definitions.is_populated() is True
        assert host.definitions.describe() == definitions.describe()
        assert host._graph is None and host._management_graph is None
        _assert_nothing_created(database)
    finally:
        host.close()
    _assert_nothing_created(database)


def test_project_definition_management_exposes_three_fixed_capabilities(tmp_path):
    """147 R1 V1: the project queries three exact sub-capabilities by identity."""
    database = tmp_path / "not-created" / "project.sqlite"
    definitions = ProjectDefinitionManagement()
    assert definitions.registered_sub_capabilities() == FIXED_CAPABILITY_IDS
    assert definitions.is_populated() is True and definitions.connected() is True
    assert definitions.diagnostics() == []
    assert len(definitions.capabilities()) == 3

    described = definitions.describe()
    assert described["sub_capabilities"] == list(FIXED_CAPABILITY_IDS)
    assert described["connected"] is True
    assert [row["source_id"] for row in described["sources"]] == [
        "workflow.local-source", "provider.local-source", "content.local-source"]
    assert [row["capability_ids"] for row in described["sources"]] == [
        ["workflow.graph"], ["provider.configuration"], ["content.global"]]
    assert described["sources"][0]["required_roles"] == ["management", "execution"]
    assert described["sources"][1]["required_roles"] == ["management"]
    assert described["sources"][2]["required_roles"] == ["management"]
    assert described["sources"][1]["required_dependency_contract_kinds"] == []
    assert described["sources"][2]["required_dependency_contract_kinds"] == []
    assert [(row["capability_id"], row["role"]) for row in described["bindings"]] == [
        ("content.global", "management"), ("provider.configuration", "management"),
        ("workflow.graph", "execution"), ("workflow.graph", "management")]

    assert set(definitions.capability("workflow.graph")["roles"][0]) == {
        "role", "capability_id", "service_contract"}
    workflow_roles = {row["role"] for row in definitions.capability("workflow.graph")["roles"]}
    assert workflow_roles == {"management", "execution"}
    for identity in ("provider.configuration", "content.global"):
        declaration = definitions.capability(identity)
        assert [row["role"] for row in declaration["roles"]] == ["management"]
        assert declaration["lifecycle"]["roles"] == ["management"]
        assert declaration["environment"]["contract_id"] == "project.storage-environment"

    # Exact identity+role indexing: each capability yields its own declared
    # trusted binding and never another capability's factory.
    environment = ProjectStorageEnvironment(database)
    workflow_binding = definitions.require_role_binding(
        "management", database=environment.database, environment=environment)
    assert workflow_binding.capability_id == "workflow.graph"
    assert workflow_binding.service_contract == "workflow.management-service"
    provider_binding = definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="provider.configuration")
    assert provider_binding.capability_id == "provider.configuration"
    assert provider_binding.service_contract == "provider.management-service"
    content_binding = definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="content.global")
    assert content_binding.capability_id == "content.global"
    assert content_binding.service_contract == "content.management-service"
    assert len({workflow_binding.capability_id, provider_binding.capability_id,
                content_binding.capability_id}) == 3

    with pytest.raises(ContractValidationError) as wrong_role:
        definitions.require_role_binding(
            "execution", database=environment.database, environment=environment,
            capability_id="provider.configuration")
    assert wrong_role.value.reason_code == "capability_role_missing"
    with pytest.raises(ContractValidationError) as unknown:
        definitions.require_role_binding(
            MANAGEMENT_ROLE, database=environment.database, environment=environment,
            capability_id="project.not-declared")
    assert unknown.value.reason_code == "capability_not_registered"
    # The declared environment requirement is rechecked per requested capability
    # and fails before any directory, database or credential-store side effect.
    other = ProjectStorageEnvironment(tmp_path / "other" / "other.sqlite")
    with pytest.raises(ContractValidationError) as mismatch:
        definitions.require_role_binding(
            MANAGEMENT_ROLE, database=environment.database, environment=other,
            capability_id="content.global")
    assert mismatch.value.reason_code == "capability_environment_mismatch"

    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_fixed_composition_frontend_references_match_trusted_manifests():
    """P2: every declared package/version/extension pair exists in the real composition."""
    from phase1_agent.builtin_packages import builtin_capability_packages

    registry = {}
    for package in builtin_capability_packages():
        manifest = package.manifest
        registry[(manifest.package_id, manifest.version)] = {
            row["extension_id"]
            for row in manifest.exports.get("frontend_extensions", ())}
    definitions = ProjectDefinitionManagement()
    assert definitions.diagnostics() == []
    assert definitions.connected() is True
    for identity in definitions.registered_sub_capabilities():
        declaration = definitions.capability(identity)
        versions = {}
        for package in declaration["frontend"]["packages"]:
            key = (package["package_id"], package["version"])
            assert key in registry, key
            versions.setdefault(package["package_id"], set()).add(package["version"])
        for extension in declaration["frontend"]["extensions"]:
            candidates = versions.get(extension["package_id"], set())
            assert candidates, extension
            assert any(extension["extension_id"] in registry[
                (extension["package_id"], version)] for version in candidates), extension
    content = definitions.capability("content.global")
    tavern_extension = next(row for row in content["frontend"]["extensions"]
                            if row["package_id"] == "workflow.tavern")
    # The current 1.2.0 package exports the derived identity; the base literal is
    # a 1.1.0 identity and must not satisfy the declared 1.2.0 pairing.
    assert tavern_extension["extension_id"] == "workflow.tavern.lorebook-resources-v1-2"
    assert "workflow.tavern.lorebook-resources" not in registry[("workflow.tavern", "1.2.0")]


def test_frontend_pairing_mismatch_blocks_only_that_capability(tmp_path):
    """P2: a wrong package/version pairing blocks only its own capability."""
    workflow_source, provider_source, content_source = local_project_capability_sources()
    base = provider_source.declarations[0]
    broken_provider = CapabilitySource(
        provider_source.source_id,
        (replace(base, frontend={
            "schema_version": 1,
            "packages": ({"package_id": "workflow.models", "version": "9.9.9"},),
            "extensions": base.frontend["extensions"],
            "containers": base.frontend["containers"],
        }),),
        required_roles=provider_source.required_roles,
        required_dependency_contract_kinds=provider_source.required_dependency_contract_kinds)
    definitions = ProjectDefinitionManagement(
        sources=(workflow_source, broken_provider, content_source))
    database = tmp_path / "not-created" / "pairing-block.sqlite"
    environment = ProjectStorageEnvironment(database)
    blocking = [row for row in definitions.diagnostics() if row["severity"] == "blocking"]
    assert blocking and all(row["capability_id"] == "provider.configuration"
                            for row in blocking)
    assert any(row["reason_code"] == "capability_frontend_unknown"
               and row.get("package_id") == "workflow.models"
               and row.get("version") == "9.9.9"
               and row.get("known_versions") == ["1.0.0"] for row in blocking)
    assert definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="workflow.graph").service_contract == "workflow.management-service"
    assert definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="content.global").service_contract == "content.management-service"
    with pytest.raises(ContractValidationError) as error:
        definitions.require_role_binding(
            MANAGEMENT_ROLE, database=environment.database, environment=environment,
            capability_id="provider.configuration")
    assert error.value.reason_code == "capability_frontend_unknown"
    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_workflow_dependency_report_registers_restricted_read_relations():
    """JA-1/JA-2: the workflow declaration records its real restricted read paths."""
    definitions = ProjectDefinitionManagement()
    report = definitions.dependency_report()
    assert len(report) == 18  # workflow 11 + provider 4 + content 3
    workflow_rows = [row for row in report if row["capability_id"] == "workflow.graph"]
    assert len(workflow_rows) == 11
    assert [row["dependency_id"] for row in workflow_rows] == [
        "dep.storage-environment", "dep.local-package-source", "dep.resource-content",
        "dep.content-use-time-read", "dep.content-host-service-read",
        "dep.provider-model", "dep.model-request-resource-reader",
        "dep.model-request-service", "dep.credentials", "dep.request-evidence",
        "dep.frontend-access",
    ]
    by_id = {row["dependency_id"]: row for row in workflow_rows}
    assert all(row["static_result"] == "present" for row in workflow_rows)
    assert by_id["dep.model-request-resource-reader"]["call_site"] == {
        "module": "phase1_agent.model_request_resources",
        "anchor": "def read_resource_bundle"}
    assert by_id["dep.model-request-resource-reader"]["contracts"] == [
        {"kind": "service_definition", "identity": "workflow.models", "version": "2.0.0"}]
    assert by_id["dep.resource-content"]["call_site"] == {
        "module": "phase1_agent.graph_resource_host",
        "anchor": "def _preflight_capabilities"}
    assert by_id["dep.content-use-time-read"]["call_site"] == {
        "module": "phase1_agent.graph_resource_host",
        "anchor": "def _read_current_resource"}
    assert by_id["dep.content-host-service-read"]["call_site"] == {
        "module": "phase1_agent.graph_runtime_host",
        "anchor": "self.content_configuration.get_global_resource("}
    carriers = {by_id[row_id]["carrier"] for row_id in (
        "dep.resource-content", "dep.content-use-time-read",
        "dep.content-host-service-read")}
    assert len(carriers) == 3
    assert "records.read_global_resources" in by_id["dep.resource-content"]["purpose"]
    assert "not the wired content service" in by_id["dep.content-use-time-read"]["carrier"]
    assert "registry.data_types" in by_id["dep.content-host-service-read"]["carrier"]
    assert "do not cover the restricted request bundle read" in (
        by_id["dep.provider-model"]["purpose"])
    kinds = {contract["kind"] for row in workflow_rows for contract in row["contracts"]}
    assert kinds == {"environment", "operation", "service_definition", "private_store",
                     "frontend_package", "frontend_extension", "frontend_container"}
    assert definitions.connected() is True and definitions.diagnostics() == []


def test_host_domain_management_views_acquire_declared_bindings(tmp_path, monkeypatch):
    """147 R1/V2: Host domain views are acquired along the declared bindings."""
    database = tmp_path / "declared-domains.sqlite"
    calls = []
    real_provider = provider_capability.build_management_service
    real_content = content_capability.build_management_service

    def recording_provider(*, database, environment):
        calls.append(("provider", database, environment))
        return real_provider(database=database, environment=environment)

    def recording_content(*, database, environment):
        calls.append(("content", database, environment))
        return real_content(database=database, environment=environment)

    # Patch before Host construction: the trusted sources build their role
    # bindings from the module-level callables at declaration time.
    monkeypatch.setattr(provider_capability, "build_management_service", recording_provider)
    monkeypatch.setattr(content_capability, "build_management_service", recording_content)
    host = WorkflowHost(database)
    try:
        assert calls == []
        provider_binding = host.definitions.require_role_binding(
            MANAGEMENT_ROLE, database=host.database, environment=host.environment,
            capability_id="provider.configuration")
        assert provider_binding.factory is recording_provider
        content_binding = host.definitions.require_role_binding(
            MANAGEMENT_ROLE, database=host.database, environment=host.environment,
            capability_id="content.global")
        assert content_binding.factory is recording_content
        assert calls == []
        assert host._providers is None and host._content is None

        provider_view = host.provider_configuration
        assert type(provider_view) is ProviderConfigurationService
        assert calls == [("provider", host.database, host.environment)]
        content_view = host.content_configuration
        assert type(content_view) is ContentConfigurationService
        assert calls == [("provider", host.database, host.environment),
                         ("content", host.database, host.environment)]
        assert host.provider_configuration is provider_view
        assert host.content_configuration is content_view
        # Domain management never activates workflow execution or the lease.
        assert host._graph is None and host._management_graph is None
        assert not Path(str(host.database) + ".graph.lock").exists()
    finally:
        host.close()


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
        assert host.definitions.registered_sub_capabilities() == FIXED_CAPABILITY_IDS
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
