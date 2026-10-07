"""Independent prompt current management without compatibility packages or model calls."""

from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import create_content_package, default_presentation
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.global_resources import global_resource_reference
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ResourceIdentity
from phase1_agent.model_contract import CHAT_PROVIDER_TYPE
from phase1_agent.model_package import create_model_package
from phase1_agent.prompt_package import PROMPT_RESOURCE_TYPE, create_prompt_package


def uid(number):
    return str(UUID(int=number, version=4))


def registry(*, models=False):
    packages = [create_content_package(), create_prompt_package()]
    enabled = {"workflow.prompts": "1.0.0"}
    if models:
        packages.append(create_model_package())
        enabled["workflow.models"] = "1.0.0"
    capabilities = CapabilityPackageLoader(packages).load(enabled)
    assert not any(row["package_id"] == "workflow.compat" for row in capabilities.package_lock)
    assert capabilities.registry.get("workflow.global-content", "2") is None
    return capabilities.registry


def prompt_record(text="current prompt", *, scope="workspace"):
    reference = ResourceIdentity(scope, PROMPT_RESOURCE_TYPE, uid(1)).to_dict()
    return {**reference, "data_schema_version": 1, "update_sequence": 1,
            "value": {"enabled": True, "members": [
                {"id": uid(2), "text": text, "presentation": default_presentation(),
                 "metadata": {"label": "Fixture member"}}]}}


def identity(record):
    return ResourceIdentity(record["scope"], record["type_id"], record["resource_id"]).to_dict()


def save(app, record):
    return app.command("resource.save", {
        "record": record, "expected_sequence": record["update_sequence"] - 1,
        "idempotency_key": str(uuid4()),
    })


def prompt_graph(installed, reference):
    installed = installed.detached()
    installed.register(NodeDefinition(
        "test.prompt-sink", "1", "Prompt sink", "Test", {},
        {"type": "object", "additionalProperties": False},
        inputs=(NodePort("input", "PROMPT", data_schema_version=2),),
        outputs=(NodePort("output", "PROMPT", data_schema_version=2),), is_output=True,
    ), lambda config, inputs, context: {"output": inputs["input"]})
    components = ("prompts.global-reference", "prompts.global-resolve", "test.prompt-sink")
    nodes = [
        {"node_binding_id": uid(10 + index), "component_id": component, "component_version": "1",
         "title": component, "position": {"x": index * 150, "y": 0},
         "config": {"reference": reference} if index == 0 else {}}
        for index, component in enumerate(components)
    ]
    document = {
        "schema_version": 1, "workflow_definition_id": uid(20), "revision": 1,
        "name": "Current prompt graph", "nodes": nodes,
        "edges": [{"edge_id": uid(30 + index), "source_node_id": nodes[index]["node_binding_id"],
                   "source_port_id": "output", "target_node_id": nodes[index + 1]["node_binding_id"],
                   "target_port_id": "input", "order": 0} for index in range(2)],
    }
    return installed, document


def create_session(service, document):
    service.save_definition(document, expected_revision=0, idempotency_key=str(uuid4()))
    return service.create_session(
        document["workflow_definition_id"], 1, idempotency_key=str(uuid4()))


def test_prompt_current_named_save_read_replay_and_reopen_without_compat(tmp_path):
    path = tmp_path / "prompt-current.sqlite"
    installed = registry()
    original = prompt_record("original prompt body")
    request = {"record": original, "expected_sequence": 0, "idempotency_key": str(uuid4())}
    with closing(GraphWorkflowService(path, registry=installed)) as service:
        app = GraphApplication(service)
        accepted = app.command("resource.save", request)
        assert accepted["result"] == {
            "reference": identity(original), "update_sequence": 1, "deleted": False}
        assert accepted["receipt"]["accepted"] == accepted["result"]
        assert accepted["receipt"]["operation"] == "resource.save"
        assert accepted["receipt"]["operation_scope"] == "management"
        assert "original prompt body" not in canonical_bytes(accepted).decode()
        assert app.query("resource.read", {"identity": identity(original)}) == original
        assert app.query("resource.list", {"scope": "workspace", "type_id": PROMPT_RESOURCE_TYPE}) == [original]

        changed = deepcopy(original)
        changed["update_sequence"] = 2
        changed["value"]["members"][0]["text"] = "updated prompt body"
        save(app, changed)
        assert app.command("resource.save", request) == accepted
        assert app.query("resource.read", {"identity": identity(original)}) == changed
        assert not hasattr(service, "_native_runtime")

    with closing(GraphWorkflowService(path, registry=installed)) as service:
        assert GraphApplication(service).query("resource.read", {"identity": identity(original)}) == changed
        assert not hasattr(service, "_native_runtime")


def test_prompt_current_reference_resolve_graph_executes_without_compat(tmp_path):
    record = prompt_record("independent current body", scope="project:one")
    installed, document = prompt_graph(registry(), identity(record))
    with closing(GraphWorkflowService(tmp_path / "prompt-run.sqlite", registry=installed)) as service:
        save(GraphApplication(service), record)
        initial = create_session(service, document)
        started = service.start(
            initial["workflow_session_id"], expected_revision=initial["revision"],
            idempotency_key=str(uuid4()))
        service.wait(started["active_chain_run_id"])
        completed = service.get_session(initial["workflow_session_id"])
        assert completed["status"] == "succeeded"
        assert completed["nodes"][0]["outputs"]["output"] == global_resource_reference(identity(record))
        assert completed["nodes"][-1]["outputs"]["output"]["items"][0]["text"] == "independent current body"
        assert not hasattr(service, "_native_runtime") and service._resource_frames == {}


@pytest.mark.parametrize("problem,reason", [
    ("missing", "global_resource_missing"), ("disabled", "global_content_disabled"),
])
def test_prompt_current_run_preflight_rejects_before_start_and_preserves_session(tmp_path, problem, reason):
    record = prompt_record()
    installed, document = prompt_graph(registry(), identity(record))
    with closing(GraphWorkflowService(tmp_path / "prompt-run-preflight.sqlite", registry=installed)) as service:
        if problem == "disabled":
            record["value"]["enabled"] = False
            save(GraphApplication(service), record)
        initial = create_session(service, document)
        with pytest.raises(ContractValidationError) as failure:
            service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                          idempotency_key=str(uuid4()))
        assert failure.value.reason_code == reason
        assert service.get_session(initial["workflow_session_id"]) == initial
        assert not hasattr(service, "_native_runtime") and service._resource_frames == {}


def test_prompt_current_same_uuid_scope_and_type_are_isolated(tmp_path):
    installed = registry(models=True)
    workspace = prompt_record("workspace prompt")
    project = prompt_record("project prompt", scope="project:one")
    provider = {
        **ResourceIdentity("workspace", CHAT_PROVIDER_TYPE, workspace["resource_id"]).to_dict(),
        "data_schema_version": 1, "update_sequence": 1,
        "value": {"name": "Fixture provider", "protocol": "chat", "base_url": "https://example.test",
                  "credential_ref": "env:DEEPSEEK_API_KEY", "enabled": True},
    }
    with closing(GraphWorkflowService(tmp_path / "prompt-identities.sqlite", registry=installed)) as service:
        app = GraphApplication(service)
        for record in (workspace, project, provider):
            save(app, record)
            assert app.query("resource.read", {"identity": identity(record)}) == record
        assert app.query("resource.list", {"scope": "workspace", "type_id": PROMPT_RESOURCE_TYPE}) == [workspace]
        assert app.query("resource.list", {"scope": "project:one", "type_id": PROMPT_RESOURCE_TYPE}) == [project]
        assert app.query("resource.list", {"scope": "workspace", "type_id": CHAT_PROVIDER_TYPE}) == [provider]

        entry = installed.get("prompts.global-reference", "1")
        project_config = {"reference": identity(project)}
        entry.config_validator(project_config)
        entry.resource_preflight_validator(project_config, [workspace, provider, project])
        assert entry.resource_dependencies_declaration(project_config) == [
            {"kind": "global-resource", "reference": identity(project)}]
        assert entry.executor(project_config, {}, SimpleNamespace())["output"] == global_resource_reference(
            identity(project))
        with pytest.raises(ContractValidationError) as missing:
            entry.resource_preflight_validator(project_config, [workspace, provider])
        assert missing.value.reason_code == "global_resource_missing"
        with pytest.raises(ContractValidationError) as wrong_type:
            entry.config_validator({"reference": identity(provider)})
        assert wrong_type.value.reason_code == "graph_prompt_resource_type_mismatch"

        calls = []

        def host_call(capability, operation, reference):
            calls.append((capability, operation, deepcopy(reference)))
            return app.query("resource.read", {"identity": reference})

        context = SimpleNamespace(node_binding_id=uid(3), reads=[], host_call=host_call)
        resolved = installed.get("prompts.global-resolve", "1").executor(
            {}, {"input": global_resource_reference(identity(project))}, context)["output"]
        assert [item["text"] for item in resolved["items"]] == ["project prompt"]
        assert calls == [("resources:read", "current-global-resource", identity(project))]
        assert context.reads == [{"kind": "global_resource_read", "reference": identity(project)}]


@pytest.mark.parametrize("field,value", [("name", "Unsupported name"), ("kind", "role_card")])
def test_prompt_current_schema_one_rejects_legacy_top_level_fields_without_writing(tmp_path, field, value):
    installed = registry()
    original = prompt_record()
    with closing(GraphWorkflowService(tmp_path / "prompt-schema.sqlite", registry=installed)) as service:
        app = GraphApplication(service)
        save(app, original)
        invalid = deepcopy(original)
        invalid["update_sequence"] = 2
        invalid["value"][field] = value
        with pytest.raises(ContractValidationError):
            save(app, invalid)
        assert app.query("resource.read", {"identity": identity(original)}) == original


@pytest.mark.parametrize("problem,reason", [
    ("missing", "global_resource_missing"),
    ("disabled", "global_content_disabled"),
    ("schema", "graph_prompt_resource_type_mismatch"),
])
def test_prompt_current_reference_preflight_checks_saved_identity_and_value(tmp_path, problem, reason):
    installed = registry()
    record = prompt_record()
    reference = identity(record)
    with closing(GraphWorkflowService(tmp_path / "prompt-preflight.sqlite", registry=installed)) as service:
        app = GraphApplication(service)
        if problem == "disabled":
            record["value"]["enabled"] = False
        save(app, record)
        current = app.query("resource.read", {"identity": reference})
        records = [] if problem == "missing" else [current]
        if problem == "schema":
            records[0]["data_schema_version"] = 2
        entry = installed.get("prompts.global-reference", "1")
        with pytest.raises(ContractValidationError) as failure:
            entry.resource_preflight_validator({"reference": reference}, records)
        assert failure.value.reason_code == reason
        assert app.query("resource.read", {"identity": reference}) == record
