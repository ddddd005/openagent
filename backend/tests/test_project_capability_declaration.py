"""T2/147 R1 capability declaration: three fixed sources, validation and trusted acquisition.

Covers 140 号 §6.1/§9.1 V2 and 147 §4.1/§8.1 V1: the three fixed trusted
sources (workflow graph, provider/model-profile configuration, global content)
are accepted and queried by the project by exact capability identity, and the
Host really acquires each role along the declared trusted callable binding.
Missing core declarations, wrong bindings, broken dependency references and
environment mismatches fail explicitly with safe reason codes; a broken
provider/content declaration blocks only that capability; ordinary management
and definition reads work without any model configuration; static validation
creates no database, execution lease or credential store; close/borrow
boundaries stay as delivered by T0-T1.
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

import phase1_agent.content_capability as content_capability
import phase1_agent.provider_capability as provider_capability
import phase1_agent.workflow_capability as workflow_capability
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService
from phase1_agent.project_capability import CapabilitySource
from phase1_agent.project_support import (
    EXECUTION_ROLE,
    MANAGEMENT_ROLE,
    ProjectDefinitionManagement,
    ProjectStorageEnvironment,
    local_project_capability_sources,
)
from phase1_agent.server import create_server
from phase1_agent.workflow_host import WorkflowHost

from test_graph_service import text_graph

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


def _sources():
    return local_project_capability_sources()


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


def _workflow_dependency_rows(report):
    return [row for row in report if row["capability_id"] == "workflow.graph"]


def _trusted_frontend_registry():
    """The real installed composition's (package_id, version) -> extension ids."""
    from phase1_agent.builtin_packages import builtin_capability_packages
    return {(package.manifest.package_id, package.manifest.version):
            {row["extension_id"]
             for row in package.manifest.exports.get("frontend_extensions", ())}
            for package in builtin_capability_packages()}


def _variant_source(source, declaration):
    """A trusted source variant carrying the source's own policy."""
    return CapabilitySource(
        source.source_id, (declaration,), required_roles=source.required_roles,
        required_dependency_contract_kinds=source.required_dependency_contract_kinds)


def test_workflow_source_declaration_is_queryable_from_two_read_points(tmp_path):
    database = tmp_path / "not-created" / "capability.sqlite"
    standalone = ProjectDefinitionManagement()
    host = WorkflowHost(database)
    try:
        assert standalone.is_populated() is True
        assert host.definitions.is_populated() is True
        assert standalone.registered_sub_capabilities() == FIXED_CAPABILITY_IDS
        assert host.definitions.registered_sub_capabilities() == FIXED_CAPABILITY_IDS
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
        workflow_report = _workflow_dependency_rows(report)
        assert [row["dependency_id"] for row in workflow_report] == [
            "dep.storage-environment", "dep.local-package-source", "dep.resource-content",
            "dep.content-use-time-read", "dep.content-host-service-read",
            "dep.provider-model", "dep.model-request-resource-reader",
            "dep.model-request-service", "dep.credentials", "dep.request-evidence",
            "dep.frontend-access",
        ]
        assert len(report) == 18  # workflow 11 + provider 4 + content 3
        for row in workflow_report:
            assert row["static_result"] == "present", row
            assert row["requirement"] in ("required", "conditional", "optional")
            assert row["caller"] and row["provider"] and row["purpose"] and row["condition"]
            assert row["carrier"]
            assert row["call_site"]["module"].startswith("phase1_agent.")
            assert row["contracts"]
        rows = {row["dependency_id"]: row for row in workflow_report}
        # JA-1: the restricted model request resource reader is recorded as its
        # own read relation (authorization + one read-only transaction + head
        # check), separate from ordinary management/discovery.
        reader = rows["dep.model-request-resource-reader"]
        assert reader["call_site"] == {"module": "phase1_agent.model_request_resources",
                                       "anchor": "def read_resource_bundle"}
        assert reader["contracts"] == [{"kind": "service_definition",
                                        "identity": "workflow.models", "version": "2.0.0"}]
        assert "read-only transaction" in reader["purpose"]
        assert "without reloading values" in reader["purpose"]
        assert "model_resource_reader" in reader["condition"]
        assert "update_sequence/deleted" in reader["condition"]
        assert "not ordinary management CRUD" in reader["carrier"]
        assert "not two ordinary reads" in reader["carrier"]
        assert "do not cover the restricted request bundle read" in (
            rows["dep.provider-model"]["purpose"])
        # JA-2: preflight direct read, use-time direct read and host service read
        # are three distinguishable real paths, not one generic content read.
        preflight = rows["dep.resource-content"]
        use_time = rows["dep.content-use-time-read"]
        host_service = rows["dep.content-host-service-read"]
        assert preflight["call_site"]["anchor"] == "def _preflight_capabilities"
        assert "records.read_global_resources" in preflight["purpose"]
        assert "original records transaction" in preflight["carrier"]
        assert use_time["call_site"]["anchor"] == "def _read_current_resource"
        assert "_global_store" in use_time["carrier"]
        assert "not the wired content service" in use_time["carrier"]
        assert host_service["call_site"]["anchor"] == (
            "self.content_configuration.get_global_resource(")
        assert "ContentConfigurationService" in host_service["carrier"]
        assert "registry.data_types" in host_service["carrier"]
        assert len({preflight["carrier"], use_time["carrier"],
                    host_service["carrier"]}) == 3
        kinds = {contract["kind"] for row in workflow_report for contract in row["contracts"]}
        assert kinds == {
            "environment", "operation", "service_definition", "private_store",
            "frontend_package", "frontend_extension", "frontend_container",
        }
        # The two domain sources' real dependency reports follow the workflow
        # rows in fixed source order; every row carries its capability identity.
        expected_domain_rows = [
            (source.declarations[0].to_dict()["capability"]["capability_id"],
             dependency["dependency_id"])
            for source in _sources()[1:]
            for dependency in source.declarations[0].to_dict()["dependencies"]]
        assert [(row["capability_id"], row["dependency_id"])
                for row in report[len(workflow_report):]] == expected_domain_rows
        assert all(row["static_result"] == "present" for row in report)

        # All three fixed capabilities are registered project sub-capabilities;
        # the provider/content/model supplies are no longer unregistered
        # contract references.
        assert set(host.definitions.registered_sub_capabilities()) == {
            "workflow.graph", "provider.configuration", "content.global"}
        assert host.definitions.diagnostics() == []
        assert host.definitions.describe()["connected"] is True
        assert host._graph is None and host._management_graph is None
    finally:
        host.close()
    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_three_fixed_sources_expose_identity_kind_and_unique_roles(tmp_path):
    """147 R1 V1: identities, kinds and per-capability roles are exact and unique."""
    definitions = ProjectDefinitionManagement()
    assert definitions.registered_sub_capabilities() == FIXED_CAPABILITY_IDS
    kinds = {}
    for identity in definitions.registered_sub_capabilities():
        declaration = definitions.capability(identity)
        kinds[identity] = declaration["capability"]["capability_kind"]
        role_names = [row["role"] for row in declaration["roles"]]
        assert len(role_names) == len(set(role_names))
        assert all(row["capability_id"] == identity for row in declaration["roles"])
        assert declaration["capability"]["access_contract_version"] == 1
        assert declaration["lifecycle"]["owner"] == "project.workflow-host"
        assert declaration["environment"]["contract_id"] == "project.storage-environment"
    assert kinds == {"content.global": "content",
                     "provider.configuration": "provider", "workflow.graph": "workflow"}
    assert {row["role"] for row in
            definitions.capability("provider.configuration")["roles"]} == {"management"}
    assert {row["role"] for row in
            definitions.capability("content.global")["roles"]} == {"management"}
    assert {row["role"] for row in
            definitions.capability("workflow.graph")["roles"]} == {"management", "execution"}

    described = definitions.describe()
    assert [(row["capability_id"], row["role"]) for row in described["bindings"]] == [
        ("content.global", "management"), ("provider.configuration", "management"),
        ("workflow.graph", "execution"), ("workflow.graph", "management")]
    report = definitions.dependency_report()
    assert {row["capability_id"] for row in report} == set(FIXED_CAPABILITY_IDS)
    database = tmp_path / "not-created" / "three.sqlite"
    environment = ProjectStorageEnvironment(database)
    for identity, role, contract in (
            ("workflow.graph", "management", "workflow.management-service"),
            ("workflow.graph", "execution", "workflow.execution-service"),
            ("provider.configuration", "management", "provider.management-service"),
            ("content.global", "management", "content.management-service")):
        binding = definitions.require_role_binding(
            role, database=environment.database, environment=environment,
            capability_id=identity)
        assert (binding.capability_id, binding.role, binding.service_contract) == (
            identity, role, contract)
    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_duplicate_capability_identity_fails_explicitly_and_keeps_the_first(tmp_path):
    """A duplicate capability identity is blocking and never overwrites the first."""
    workflow_source = workflow_capability.local_workflow_capability_source()
    provider_source = provider_capability.local_provider_capability_source()
    content_source = content_capability.local_content_capability_source()
    duplicate = CapabilitySource(
        provider_source.source_id, provider_source.declarations,
        required_roles=provider_source.required_roles,
        required_dependency_contract_kinds=provider_source.required_dependency_contract_kinds)
    definitions = ProjectDefinitionManagement(
        sources=(workflow_source, provider_source, content_source, duplicate))
    assert definitions.connected() is False
    matches = [row for row in definitions.diagnostics()
               if row["reason_code"] == "capability_identity_duplicated"]
    assert matches and matches[0]["capability_id"] == "provider.configuration"
    assert matches[0]["severity"] == "blocking"
    # The first declaration is kept byte-for-byte.
    assert definitions.capability("provider.configuration") == (
        provider_source.declarations[0].to_dict())

    database = tmp_path / "not-created" / "duplicate.sqlite"
    environment = ProjectStorageEnvironment(database)
    with pytest.raises(ContractValidationError) as error:
        definitions.require_role_binding(
            MANAGEMENT_ROLE, database=environment.database, environment=environment,
            capability_id="provider.configuration")
    assert error.value.reason_code == "capability_identity_duplicated"
    # The other declared capabilities are unaffected.
    assert definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="workflow.graph").service_contract == "workflow.management-service"
    assert definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="content.global").service_contract == "content.management-service"
    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_broken_domain_declaration_blocks_only_its_own_capability(tmp_path):
    """147 R1: a broken provider/content declaration never decides another capability."""
    workflow_source = workflow_capability.local_workflow_capability_source()
    provider_source = provider_capability.local_provider_capability_source()
    content_source = content_capability.local_content_capability_source()
    database = tmp_path / "not-created" / "broken-domain.sqlite"
    environment = ProjectStorageEnvironment(database)

    provider_declaration = provider_source.declarations[0]
    broken_provider = CapabilitySource(
        provider_source.source_id,
        (replace(provider_declaration, roles=(replace(
            provider_declaration.roles[0],
            factory="phase1_agent.graph_provider_host:ProviderConfigurationService"),)),),
        required_roles=provider_source.required_roles,
        required_dependency_contract_kinds=provider_source.required_dependency_contract_kinds)
    definitions = ProjectDefinitionManagement(
        sources=(workflow_source, broken_provider, content_source))
    blocking = [row for row in definitions.diagnostics() if row["severity"] == "blocking"]
    assert blocking and all(row["capability_id"] == "provider.configuration"
                            for row in blocking)
    assert blocking[0]["reason_code"] == "capability_binding_not_callable"
    with pytest.raises(ContractValidationError) as error:
        definitions.require_role_binding(
            MANAGEMENT_ROLE, database=environment.database, environment=environment,
            capability_id="provider.configuration")
    assert error.value.reason_code == "capability_binding_not_callable"
    assert definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="workflow.graph").service_contract == "workflow.management-service"
    assert definitions.require_role_binding(
        EXECUTION_ROLE, database=environment.database, environment=environment).service_contract \
        == "workflow.execution-service"
    assert definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="content.global").service_contract == "content.management-service"

    content_declaration = content_source.declarations[0]
    lying_content = CapabilitySource(
        content_source.source_id,
        (replace(content_declaration, frontend={
            **content_declaration.frontend,
            "extensions": (*content_declaration.frontend["extensions"],
                           {"package_id": "workflow.prompts",
                            "extension_id": "workflow.prompts.not-a-real-extension"}),}),),
        required_roles=content_source.required_roles,
        required_dependency_contract_kinds=content_source.required_dependency_contract_kinds)
    definitions = ProjectDefinitionManagement(
        sources=(workflow_source, provider_source, lying_content))
    blocking = [row for row in definitions.diagnostics() if row["severity"] == "blocking"]
    assert blocking and all(row["capability_id"] == "content.global" for row in blocking)
    assert blocking[0]["reason_code"] == "capability_frontend_unknown"
    with pytest.raises(ContractValidationError) as error:
        definitions.require_role_binding(
            MANAGEMENT_ROLE, database=environment.database, environment=environment,
            capability_id="content.global")
    assert error.value.reason_code == "capability_frontend_unknown"
    assert definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="workflow.graph").service_contract == "workflow.management-service"
    assert definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="provider.configuration").service_contract == "provider.management-service"
    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_frontend_package_extension_pairing_rejects_mismatched_composition(tmp_path):
    """P2: frontend references need a real package/version/extension pairing."""
    workflow_source = workflow_capability.local_workflow_capability_source()
    provider_source = provider_capability.local_provider_capability_source()
    base = provider_source.declarations[0]
    database = tmp_path / "not-created" / "pairing.sqlite"
    environment = ProjectStorageEnvironment(database)

    # 1) The declared package/version pair does not exist in the composition.
    wrong_version = _variant_source(provider_source, replace(base, frontend={
        "schema_version": 1,
        "packages": ({"package_id": "workflow.models", "version": "9.9.9"},),
        "extensions": base.frontend["extensions"],
        "containers": base.frontend["containers"],
    }))
    definitions = ProjectDefinitionManagement(sources=(workflow_source, wrong_version))
    blocking = [row for row in definitions.diagnostics() if row["severity"] == "blocking"]
    assert blocking and all(row["capability_id"] == "provider.configuration"
                            for row in blocking)
    package_problem = next(row for row in blocking
                           if row.get("package_id") == "workflow.models"
                           and row.get("version") == "9.9.9")
    assert package_problem["reason_code"] == "capability_frontend_unknown"
    assert package_problem["known_versions"] == ["1.0.0"]
    extension_problem = next(row for row in blocking
                             if row.get("extension_id") == "workflow.models.workbench-panel")
    assert extension_problem["declared_versions"] == ["9.9.9"]
    assert extension_problem["exported_by"] == [
        {"package_id": "workflow.models", "version": "1.0.0"}]
    # Dependency coverage uses the same validated pairing: the frontend contract
    # of the lying declaration is reported missing instead of silently covered.
    frontend_rows = [row for row in definitions.dependency_report()
                     if row["capability_id"] == "provider.configuration"
                     and row["dependency_id"] == "dep.frontend-access"]
    assert frontend_rows and frontend_rows[0]["static_result"] == "missing"
    assert "capability_dependency_contract_uncovered" in frontend_rows[0]["problems"]
    with pytest.raises(ContractValidationError) as error:
        definitions.require_role_binding(
            MANAGEMENT_ROLE, database=environment.database, environment=environment,
            capability_id="provider.configuration")
    assert error.value.reason_code == "capability_frontend_unknown"
    assert definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="workflow.graph").service_contract == "workflow.management-service"

    # 2) The extension exists, but under another declared package identity.
    cross_package = _variant_source(provider_source, replace(base, frontend={
        "schema_version": 1,
        "packages": ({"package_id": "workflow.models", "version": "1.0.0"},
                     {"package_id": "workflow.prompts", "version": "1.0.0"}),
        "extensions": ({"package_id": "workflow.prompts",
                        "extension_id": "workflow.models.workbench-panel"},),
        "containers": ("workbench",),
    }))
    definitions = ProjectDefinitionManagement(sources=(workflow_source, cross_package))
    blocking = [row for row in definitions.diagnostics() if row["severity"] == "blocking"]
    assert blocking and all(row["capability_id"] == "provider.configuration"
                            for row in blocking)
    problem = next(row for row in blocking
                   if row.get("extension_id") == "workflow.models.workbench-panel")
    assert problem["package_id"] == "workflow.prompts"
    assert problem["declared_versions"] == ["1.0.0"]
    assert problem["exported_by"] == [{"package_id": "workflow.models", "version": "1.0.0"}]

    # 3) The extension's package is not declared by the capability at all.
    undeclared_package = _variant_source(provider_source, replace(base, frontend={
        "schema_version": 1,
        "packages": ({"package_id": "workflow.models", "version": "1.0.0"},),
        "extensions": ({"package_id": "workflow.prompts",
                        "extension_id": "workflow.prompts.workbench-panel"},),
        "containers": ("workbench",),
    }))
    definitions = ProjectDefinitionManagement(
        sources=(workflow_source, undeclared_package))
    blocking = [row for row in definitions.diagnostics() if row["severity"] == "blocking"]
    problem = next(row for row in blocking
                   if row.get("extension_id") == "workflow.prompts.workbench-panel")
    assert problem["reason_code"] == "capability_frontend_unknown"
    assert problem["package_id"] == "workflow.prompts"
    assert problem["declared_packages"] == ["workflow.models"]

    # 4) The untouched real declaration has a complete pairing and passes alone;
    # the whole fixed composition is clean only because every pair is real.
    assert ProjectDefinitionManagement(sources=(provider_source,)).diagnostics() == []
    assert ProjectDefinitionManagement().diagnostics() == []
    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_tavern_extension_identity_must_match_current_package_derivation(tmp_path):
    """P2: the old base lorebook id belongs to 1.1.0, not the current 1.2.0 package."""
    registry = _trusted_frontend_registry()
    assert "workflow.tavern.lorebook-resources-v1-2" in registry[("workflow.tavern", "1.2.0")]
    assert "workflow.tavern.lorebook-resources" not in registry[("workflow.tavern", "1.2.0")]
    assert "workflow.tavern.lorebook-resources" in registry[("workflow.tavern", "1.1.0")]

    content_source = content_capability.local_content_capability_source()
    declaration = content_source.declarations[0]
    legacy = _variant_source(content_source, replace(declaration, frontend={
        **declaration.frontend,
        "extensions": (
            {"package_id": "workflow.prompts",
             "extension_id": "workflow.prompts.workbench-panel"},
            {"package_id": "workflow.tavern",
             "extension_id": "workflow.tavern.lorebook-resources"},
        ),
    }))
    workflow_source = workflow_capability.local_workflow_capability_source()
    database = tmp_path / "not-created" / "tavern-pairing.sqlite"
    environment = ProjectStorageEnvironment(database)
    definitions = ProjectDefinitionManagement(sources=(workflow_source, legacy))
    blocking = [row for row in definitions.diagnostics() if row["severity"] == "blocking"]
    assert blocking and all(row["capability_id"] == "content.global" for row in blocking)
    problem = next(row for row in blocking
                   if row.get("extension_id") == "workflow.tavern.lorebook-resources")
    assert problem["reason_code"] == "capability_frontend_unknown"
    assert problem["package_id"] == "workflow.tavern"
    assert problem["declared_versions"] == ["1.2.0"]
    assert problem["exported_by"] == [{"package_id": "workflow.tavern", "version": "1.1.0"}]
    with pytest.raises(ContractValidationError) as error:
        definitions.require_role_binding(
            MANAGEMENT_ROLE, database=environment.database, environment=environment,
            capability_id="content.global")
    assert error.value.reason_code == "capability_frontend_unknown"
    assert definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database, environment=environment,
        capability_id="workflow.graph").service_contract == "workflow.management-service"

    # The real current declaration carries the package's real derived identity,
    # which the same trusted registry exports under the declared package version.
    current = ProjectDefinitionManagement().capability("content.global")
    tavern_package = next(row for row in current["frontend"]["packages"]
                          if row["package_id"] == "workflow.tavern")
    tavern_extension = next(row for row in current["frontend"]["extensions"]
                            if row["package_id"] == "workflow.tavern")
    assert tavern_extension["extension_id"] == "workflow.tavern.lorebook-resources-v1-2"
    assert tavern_extension["extension_id"] in registry[
        ("workflow.tavern", tavern_package["version"])]
    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_missing_domain_sources_fail_acquisition_explicitly(tmp_path):
    """No provider/content source injected: their acquisition fails explicitly."""
    definitions = ProjectDefinitionManagement(
        sources=(workflow_capability.local_workflow_capability_source(),))
    assert definitions.registered_sub_capabilities() == ("workflow.graph",)
    database = tmp_path / "not-created" / "missing-domain.sqlite"
    environment = ProjectStorageEnvironment(database)
    assert definitions.require_role_binding(
        MANAGEMENT_ROLE, database=environment.database,
        environment=environment).service_contract == "workflow.management-service"
    for capability_id in ("provider.configuration", "content.global"):
        with pytest.raises(ContractValidationError) as error:
            definitions.require_role_binding(
                MANAGEMENT_ROLE, database=environment.database, environment=environment,
                capability_id=capability_id)
        assert error.value.reason_code == "capability_not_registered"
    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_declaration_queries_and_definition_construction_have_no_side_effects(tmp_path):
    """Static queries and role lookup construct no service and create no path."""
    database = tmp_path / "not-created" / "static-only.sqlite"
    definitions = ProjectDefinitionManagement()
    assert definitions.registered_sub_capabilities() == FIXED_CAPABILITY_IDS
    assert definitions.diagnostics() == []
    assert definitions.capabilities() and definitions.dependency_report()
    described = definitions.describe()
    assert described["connected"] is True and described["diagnostics"] == []
    environment = ProjectStorageEnvironment(database)
    binding = definitions.require_role_binding(
        "execution", database=environment.database, environment=environment)
    assert binding.service_contract == "workflow.execution-service"
    assert not callable(getattr(definitions, "graph_service", None))
    _assert_nothing_created(database)
    assert not database.parent.exists()


def test_host_first_role_acquisition_consumes_declared_trusted_binding(tmp_path, monkeypatch):
    database = tmp_path / "declared-bindings.sqlite"
    calls = []
    real_management = workflow_capability.build_management_service
    real_execution = workflow_capability.build_execution_service

    def recording_management(*, database, environment, provider_configuration_factory=None,
                             content_configuration_factory=None, workflow_package_source=None):
        calls.append(("management", database, environment,
                      provider_configuration_factory, content_configuration_factory))
        return real_management(
            database=database, environment=environment,
            provider_configuration_factory=provider_configuration_factory,
            content_configuration_factory=content_configuration_factory,
            workflow_package_source=workflow_package_source)

    def recording_execution(*, database, environment, provider_configuration_factory=None,
                            content_configuration_factory=None, workflow_package_source=None):
        calls.append(("execution", database, environment,
                      provider_configuration_factory, content_configuration_factory))
        return real_execution(
            database=database, environment=environment,
            provider_configuration_factory=provider_configuration_factory,
            content_configuration_factory=content_configuration_factory,
            workflow_package_source=workflow_package_source)

    monkeypatch.setattr(workflow_capability, "build_management_service", recording_management)
    monkeypatch.setattr(workflow_capability, "build_execution_service", recording_execution)
    host = WorkflowHost(database)
    try:
        assert calls == []
        management = host.graph_management_service
        assert type(management) is GraphManagementService
        assert management._execution_owner is False and management._lease is None
        assert host._graph is None and host._management_graph is management
        assert len(calls) == 1
        role, called_database, called_environment, provider_factory, content_factory = calls[0]
        assert role == "management"
        assert called_database == host.database and called_environment is host.environment
        # The narrow hand-off carries the domain sources' own declared factories;
        # acquiring the workflow role constructs neither domain service.
        assert provider_factory is provider_capability.build_management_service
        assert content_factory is content_capability.build_management_service
        assert host._providers is None and host._content is None
        binding = host.definitions.require_role_binding(
            "management", database=host.database, environment=host.environment)
        assert binding.factory is recording_management
        assert binding.capability_id == "workflow.graph"

        execution = host.graph_service
        assert type(execution) is GraphWorkflowService
        assert execution._execution_owner is True and execution._lease is not None
        assert [call[0] for call in calls] == ["management", "execution"]
        role, called_database, called_environment, provider_factory, content_factory = calls[1]
        assert called_database == host.database and called_environment is host.environment
        assert provider_factory is provider_capability.build_management_service
        assert content_factory is content_capability.build_management_service
        assert host.graph_service is execution
        assert host.graph_management_service is execution
        assert host.execution_management.management_entry() is execution
        assert host.execution_management.lifecycle_entry() is execution
        assert management.environment is host.environment
        assert execution.environment is host.environment
    finally:
        host.close()

    calls.clear()
    execution_first = tmp_path / "declared-bindings-execution-first.sqlite"
    host = WorkflowHost(execution_first)
    try:
        execution = host.execution_management.execution_entry()
        assert [call[0] for call in calls] == ["execution"]
        assert calls[0][3] is provider_capability.build_management_service
        assert calls[0][4] is content_capability.build_management_service
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
            diagnostics = [item for item in host.definitions.diagnostics()
                           if item["severity"] == "blocking"]
            assert diagnostics
            for item in diagnostics:
                assert {"reason_code", "severity", "message"} <= set(item)
                assert item["capability_id"] == "workflow.graph"
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

    def unused_factory(*, database, environment, **kwargs):
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
            report = [row for row in host.definitions.dependency_report()
                      if row["capability_id"] == "workflow.graph"
                      and row["dependency_id"] == dependency.dependency_id]
            assert len(report) == 1
            row = report[0]
            assert row["static_result"] == "missing"
            assert row["condition"] == storage.condition
            assert row["requirement"] == storage.requirement
            assert expected in row["problems"]
            assert host.definitions.describe()["connected"] is False
            matches = [item for item in host.definitions.diagnostics()
                       if item["reason_code"] == expected]
            assert matches and matches[0]["capability_id"] == "workflow.graph"
            assert matches[0]["dependency_id"] == dependency.dependency_id
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
        assert host.definitions.registered_sub_capabilities() == FIXED_CAPABILITY_IDS
        assert host.definitions.describe()["connected"] is True
        assert host.definitions.capability(
            "workflow.graph")["capability"]["capability_id"] == "workflow.graph"
        assert len(host.definitions.capabilities()) == 3
        assert len(host.definitions.dependency_report()) == sum(
            len(source.declarations[0].dependencies) for source in _sources())
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
