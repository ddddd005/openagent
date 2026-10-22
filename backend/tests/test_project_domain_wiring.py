"""147 project-domain wiring: the Host and Graph really consume the declared bindings.

This file covers the Lead wiring surface added by 147 §4.2/§4.3 and §8.1 V1/V2/V3/V7:
the project registers exactly the three fixed trusted capability sources, the Host
acquires both management domains through their declared management bindings on the
shared project environment, the normal project workflow path passes only the
validated domain factories to the original Graph construction, and both domains
stay manageable without an active workflow.  Business semantics of each domain are
covered by the existing authoritative test files listed in 147 §8.2; this file
does not duplicate them.
"""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

import phase1_agent.content_capability as content_capability
import phase1_agent.provider_capability as provider_capability
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService
from phase1_agent.model_contract import CHAT_PROVIDER_TYPE
from phase1_agent.model_host_service import MODEL_SERVICE_REF
from phase1_agent.prompt_lifecycle_nodes import PROMPT_RESOURCE_TYPE
from phase1_agent.workflow_host import WorkflowHost

from credential_test_support import TestProtector
from test_content_configuration import prompt_record, provider_record, reference
from test_graph_service import save_definition_fixture
from test_model_package import source_config
from test_models_service_integration import ModelDatabaseFixture
from test_prompt_current_resources import (
    identity as current_prompt_identity, prompt_record as current_prompt_record,
)
from test_prompt_package import graph_document, graph_edge, graph_node, uid as fixture_uid


def _recording_factory(module, recorded):
    real = module.build_management_service

    def recording(*, database, environment, **options):
        recorded.append({"module": module.__name__, "database": database,
                         "environment": environment, "options": options})
        return real(database=database, environment=environment, **options)

    return recording


def _capability_ids():
    return (provider_capability.CAPABILITY_ID, content_capability.CAPABILITY_ID)


def test_project_registers_the_three_fixed_trusted_capabilities(tmp_path):
    database = tmp_path / "not-created" / "declarations.sqlite"
    host = WorkflowHost(database)
    try:
        identities = host.definitions.registered_sub_capabilities()
        assert identities == ("content.global", "provider.configuration", "workflow.graph")
        assert host.definitions.describe()["connected"] is True
        assert host.definitions.diagnostics() == []
        declared_roles = {}
        for capability_id in identities:
            declaration = host.definitions.capability(capability_id)
            declared_roles[capability_id] = {row["role"] for row in declaration["roles"]}
        assert declared_roles == {
            "content.global": {"management"},
            "provider.configuration": {"management"},
            "workflow.graph": {"management", "execution"},
        }
        bindings = host.definitions.describe()["bindings"]
        assert {(row["capability_id"], row["role"]) for row in bindings} == {
            ("content.global", "management"),
            ("provider.configuration", "management"),
            ("workflow.graph", "management"),
            ("workflow.graph", "execution"),
        }
        # Each identity is queried by its exact capability + role: no "first
        # sorted declaration" fallback and no invented execution role.
        for capability_id in _capability_ids():
            binding = host.definitions.require_role_binding(
                "management", database=host.database, environment=host.environment,
                capability_id=capability_id)
            assert binding.capability_id == capability_id
            with pytest.raises(ContractValidationError):
                host.definitions.require_role_binding(
                    "execution", database=host.database, environment=host.environment,
                    capability_id=capability_id)
        with pytest.raises(ContractValidationError):
            host.definitions.require_role_binding(
                "management", database=host.database, environment=host.environment,
                capability_id="project.missing")
        assert host._graph is None and host._management_graph is None
    finally:
        host.close()
    assert not database.parent.exists() and not database.exists()


def test_host_management_domains_use_declared_bindings_and_shared_environment(tmp_path, monkeypatch):
    database = tmp_path / "project.sqlite"
    provider_calls, content_calls = [], []
    monkeypatch.setattr(provider_capability, "build_management_service",
                        _recording_factory(provider_capability, provider_calls))
    monkeypatch.setattr(content_capability, "build_management_service",
                        _recording_factory(content_capability, content_calls))
    host = WorkflowHost(database)
    stores = []
    try:
        assert provider_calls == [] and content_calls == []
        # Static declaration queries stay side-effect free.
        assert host.definitions.registered_sub_capabilities() == (
            "content.global", "provider.configuration", "workflow.graph")
        assert not database.exists()
        original_open = host.environment.open_store

        def record_store(*, fault_injector=None):
            store = original_open(fault_injector=fault_injector)
            stores.append(store)
            return store

        monkeypatch.setattr(host.environment, "open_store", record_store)

        providers = host.provider_configuration
        assert len(provider_calls) == 1 and content_calls == []
        provider_call = provider_calls[0]
        assert provider_call["module"] == "phase1_agent.provider_capability"
        assert provider_call["database"] == host.database
        assert provider_call["environment"] is host.environment
        assert provider_call["environment"].database == host.database
        assert provider_call["options"] == {}
        assert providers.list_global_resources() == []
        assert stores
        for store in stores:
            with pytest.raises(sqlite3.ProgrammingError):
                store._connection.execute("SELECT 1")

        content = host.content_configuration
        assert len(content_calls) == 1
        content_call = content_calls[0]
        assert content_call["module"] == "phase1_agent.content_capability"
        assert content_call["database"] == host.database
        assert content_call["environment"] is host.environment
        assert content_call["options"] == {}
        assert content.list_global_resources() == []
        assert stores
        for store in stores:
            with pytest.raises(sqlite3.ProgrammingError):
                store._connection.execute("SELECT 1")

        # Managing either domain does not acquire execution or the graph lease.
        assert host._graph is None and host._management_graph is None
        assert not Path(str(database) + ".graph.lock").exists()
    finally:
        host.close()


def test_domain_binding_rejects_a_wrong_path_before_side_effects(tmp_path):
    database = tmp_path / "allowed.sqlite"
    host = WorkflowHost(database)
    try:
        for capability_id in _capability_ids():
            binding = host.definitions.require_role_binding(
                "management", database=host.database, environment=host.environment,
                capability_id=capability_id)
            rejected = tmp_path / "rejected" / "other.sqlite"
            with pytest.raises(ContractValidationError) as error:
                binding.factory(database=rejected, environment=host.environment)
            assert error.value.reason_code == "project_environment_mismatch"
            assert not rejected.parent.exists() and not rejected.exists()
            assert not Path(str(rejected) + ".credentials.sqlite").exists()
        assert not database.exists()
    finally:
        host.close()


@pytest.mark.parametrize("execution_first", [False, True])
def test_graph_normal_path_uses_validated_domain_factories(tmp_path, monkeypatch, execution_first):
    database = tmp_path / ("execution-first.sqlite" if execution_first else "management-first.sqlite")
    provider_calls, content_calls = [], []
    monkeypatch.setattr(provider_capability, "build_management_service",
                        _recording_factory(provider_capability, provider_calls))
    monkeypatch.setattr(content_capability, "build_management_service",
                        _recording_factory(content_capability, content_calls))
    host = WorkflowHost(database)
    try:
        if execution_first:
            execution = host.execution_management.execution_entry()
            assert execution is host.graph_service
            assert len(provider_calls) == 1 and len(content_calls) == 1
            management = host.graph_management_service
            assert management is execution
        else:
            management = host.graph_management_service
            assert type(management) is GraphManagementService
            assert provider_calls == [] and content_calls == []
            management.provider_configuration
            management.content_configuration
            assert len(provider_calls) == 1 and len(content_calls) == 1
            assert host._graph is None
            execution = host.graph_service
            assert type(execution) is GraphWorkflowService
            assert len(provider_calls) == 2 and len(content_calls) == 2

        # The execution view keeps the graph's own store, fault injection,
        # credentials and types.  When the lightweight management service was
        # acquired first it owns its own domain view; once the execution entry
        # exists the Host returns that entry as the management view (same object).
        if not execution_first:
            assert execution.provider_configuration is not management.provider_configuration
            assert execution.content_configuration is not management.content_configuration
        else:
            assert management.provider_configuration is execution.provider_configuration
            assert management.content_configuration is execution.content_configuration
        assert execution.provider_configuration._store_factory.__self__ is execution
        assert execution.content_configuration._store_factory.__self__ is execution
        provider_options = provider_calls[-1]["options"]
        assert provider_options["credential_protector"] is None
        assert provider_options["discovery_client_factory"] is None
        assert provider_options["store_factory"].__self__ is execution
        content_options = content_calls[-1]["options"]
        assert content_options["store_factory"].__self__ is execution
        live_types = content_options["type_registry_factory"]()
        assert live_types is execution.registry.data_types
        service_options = execution._service_options[MODEL_SERVICE_REF]
        assert service_options["credential_broker_factory"].__self__ is (
            execution.provider_configuration)

        # The workflow Graph still consumes the validated bindings of the
        # project definitions rather than constructing its domains directly.
        assert host.definitions.require_role_binding(
            "management", database=host.database, environment=host.environment,
            capability_id=provider_capability.CAPABILITY_ID).factory is not None
        assert host._graph is execution
    finally:
        host.close()


def test_project_chain_manages_both_domains_without_workflow_and_reopens(tmp_path):
    database = tmp_path / "chain.sqlite"
    prompt, provider = prompt_record(), provider_record()
    host = WorkflowHost(database)
    application = GraphApplication(host)
    try:
        for record in (prompt, provider):
            application.command("resource.save", {
                "record": deepcopy(record), "expected_sequence": 0,
                "idempotency_key": str(uuid4())})
        assert application.query("resource.read", {"identity": reference(prompt)}) == prompt
        assert application.query("resource.read", {"identity": reference(provider)}) == provider
        assert application.query("resource.list", {"type_id": PROMPT_RESOURCE_TYPE}) == [prompt]
        assert application.query("resource.list", {"type_id": CHAT_PROVIDER_TYPE}) == [provider]
        # Both domains are manageable before any workflow execution exists.
        assert host._graph is None and host._management_graph is None
        assert not Path(str(database) + ".graph.lock").exists()
        assert not Path(str(database) + ".credentials.sqlite").exists()

        execution = host.graph_service
        assert host.graph_management_service is execution
        executed_prompt = execution.get_global_resource(reference(prompt))
        executed_provider = execution.get_global_resource(reference(provider))
        assert executed_prompt == prompt and executed_provider == provider
        broker = execution.provider_configuration.new_credential_broker()
        assert broker is not None
        assert execution.content_configuration.list_global_resources(
            type_id=PROMPT_RESOURCE_TYPE) == [prompt]
        assert execution.provider_configuration.list_global_resources(
            type_id=CHAT_PROVIDER_TYPE) == [provider]
    finally:
        host.close()
    assert host._graph._closed is True

    reopened = WorkflowHost(database)
    try:
        assert reopened.content_configuration.get_global_resource(reference(prompt)) == prompt
        assert reopened.provider_configuration.get_global_resource(reference(provider)) == provider
    finally:
        reopened.close()


def _host_consumption_document(registry, prompt_reference):
    """A real graph: saved global prompt -> current content -> prompt assembly -> chat."""
    source = graph_node(registry, "models.source", 10, **source_config())
    reference = graph_node(registry, "prompts.global-reference", 11, reference=prompt_reference)
    resolve = graph_node(registry, "prompts.global-resolve", 12)
    assembly = graph_node(registry, "prompts.assembly", 14)
    chat = graph_node(registry, "models.chat", 13)
    resolve["public_outputs"], chat["public_outputs"] = ["output"], ["output"]
    nodes = [source, reference, resolve, assembly, chat]
    edges = [
        graph_edge(1001, reference, resolve),
        graph_edge(1002, resolve, assembly),
        graph_edge(1003, assembly, chat, target_port="prompt"),
        graph_edge(1004, source, chat, target_port="model"),
    ]
    return graph_document(nodes, edges)


def test_host_injected_factories_run_a_model_workflow_consuming_saved_content(tmp_path, monkeypatch):
    """147 §8.1 isolated real chain: Host bindings + current content + offline model call.

    Resource preparation is segmented as the test actually does it: the global
    prompt is saved through the public ``GraphApplication`` dispatch, while the
    provider and model-profile are saved by the test fixture calling the Host
    domain methods (``save_global_resource`` / ``provider_configuration.credentials``)
    and the synthetic local credential is prepared by the test helpers with the
    TestProtector.  The execution entry is then acquired through the Host's
    declared bindings (recorded to prove the domain factories are used with the
    graph's own store), the workflow really runs and consumes the saved global
    prompt through the resource read path, an offline model transport receives
    one request containing that current content, and the run history is re-read
    after a close/reopen of the project Host.
    """
    database = tmp_path / "consumption-chain.sqlite"
    provider_calls, content_calls = [], []
    monkeypatch.setattr(provider_capability, "build_management_service",
                        _recording_factory(provider_capability, provider_calls))
    monkeypatch.setattr(content_capability, "build_management_service",
                        _recording_factory(content_capability, content_calls))
    fixture = ModelDatabaseFixture()
    fixture.response = ModelResponse("stop", "offline chain answer",
                                     usage={"completion_tokens": 1}, model="deepseek-test",
                                     response_id="offline-chain-response")
    host = WorkflowHost(database)
    history = None
    session_id = chain_run_id = None
    try:
        application = GraphApplication(host)
        prompt = current_prompt_record("host chain current prompt body")
        application.command("resource.save", {
            "record": deepcopy(prompt), "expected_sequence": 0,
            "idempotency_key": str(uuid4())})
        host.provider_configuration.credentials.protector = TestProtector()
        fixture.write(host, 1)
        assert provider_calls and content_calls

        execution = host.graph_service
        # The execution views were constructed through the Host's declared domain
        # bindings with the graph's own per-operation store.
        assert provider_calls[-1]["environment"] is host.environment
        assert provider_calls[-1]["options"]["store_factory"].__self__ is execution
        assert content_calls[-1]["options"]["store_factory"].__self__ is execution
        execution.provider_configuration.credentials.protector = TestProtector()
        execution._service_options[MODEL_SERVICE_REF]["transport_factory"] = fixture.factory

        document = _host_consumption_document(execution.registry, current_prompt_identity(prompt))
        save_definition_fixture(execution, document)
        session = execution.create_session(
            document["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
        session_id = session["workflow_session_id"]
        started = execution.start(session_id, expected_revision=session["revision"],
                                  idempotency_key=str(uuid4()))
        chain_run_id = started["active_chain_run_id"]
        execution.wait(chain_run_id)
        final = execution.get_session(session_id)
        assert final["status"] == "succeeded", final

        # Current content really reached the run: the saved prompt was resolved and
        # its text is present in the single offline model request.
        resolved = next(node for node in final["nodes"]
                        if node["node_binding_id"] == fixture_uid(12))
        assert [item["text"] for item in resolved["outputs"]["output"]["items"]] == [
            "host chain current prompt body"]
        assert len(fixture.calls) == 1
        wire = json.dumps(fixture.calls[0]["messages"], ensure_ascii=False)
        assert "host chain current prompt body" in wire
        chat = next(node for node in final["nodes"]
                    if node["node_binding_id"] == fixture_uid(13))
        assert chat["outputs"]["output"]["content"] == "offline chain answer"
        history = execution.get_run(session_id, chain_run_id)
        assert history["runtime_facts"]
    finally:
        host.close()
    assert host._graph._closed is True

    reopened = WorkflowHost(database)
    try:
        reopened_execution = reopened.graph_service
        assert reopened_execution.get_run(session_id, chain_run_id) == history
    finally:
        reopened.close()
