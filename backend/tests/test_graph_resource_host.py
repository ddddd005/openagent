"""Public resource preflight and current-value execution boundaries."""

from contextlib import closing
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import create_content_package, default_presentation
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_resource_host import GraphResourceHost
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ResourceIdentity
from phase1_agent.prompt_package import PROMPT_RESOURCE_TYPE, create_prompt_package
from phase1_agent.tool_package import create_tool_package


def uid(number):
    return str(UUID(int=number, version=4))


def independent_registry():
    return CapabilityPackageLoader((
        create_content_package(), create_prompt_package(), create_tool_package(),
    )).load({"workflow.prompts": "1.0.0", "workflow.tools": "1.0.0"}).registry


def prompt_record(text="frozen prompt", *, scope="project:resources"):
    return {
        **ResourceIdentity(scope, PROMPT_RESOURCE_TYPE, uid(1)).to_dict(),
        "data_schema_version": 2, "update_sequence": 1,
        "value": {"enabled": True, "members": [
            {"id": uid(2), "text": text, "presentation": default_presentation(), "metadata": {},
             "lifecycle": "per_request", "compaction": "never"}]},
    }


def identity(record):
    return ResourceIdentity(record["scope"], record["type_id"], record["resource_id"]).to_dict()


def graph_node(registry, component, number, **config):
    version = "2" if component.startswith("prompts.") else "1"
    definition = registry.get(component, version).definition
    return {
        "node_binding_id": uid(number), "component_id": component, "component_version": version,
        "title": component, "position": {"x": number * 100, "y": 0},
        "config": {**deepcopy(definition.default_config), **config},
    }


def current_graph(registry, record):
    reference = graph_node(registry, "prompts.global-reference", 10, reference=identity(record))
    resolve = graph_node(registry, "prompts.global-resolve", 11)
    assembly = graph_node(registry, "prompts.assembly", 13)
    output = graph_node(registry, "tools.output", 12, mode="prompt")
    output["public_outputs"] = ["output"]
    nodes = [reference, resolve, assembly, output]
    return {
        "schema_version": 2, "workflow_definition_id": uid(20), "revision": 1,
        "name": "Independent resource graph", "nodes": nodes, "object_bindings": [],
        "package_lock": deepcopy(list(registry.execution_package_lock)),
        "edges": [
            {"edge_id": uid(30 + index), "source_node_id": nodes[index]["node_binding_id"],
             "source_port_id": "output", "target_node_id": nodes[index + 1]["node_binding_id"],
             "target_port_id": "input", "order": 0}
            for index in range(3)],
    }


def create_session(service, document):
    service.save_definition(document, expected_revision=0, idempotency_key=str(uuid4()))
    return service.create_session(document["workflow_definition_id"], 1, idempotency_key=str(uuid4()))


def run(service, session):
    started = service.start(session["workflow_session_id"], expected_revision=session["revision"],
                            idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    return service.get_session(session["workflow_session_id"])


def test_current_start_reopen_and_next_head_use_current_resource_host(tmp_path):
    installed, record = independent_registry(), prompt_record()
    path = tmp_path / "current-resources.sqlite"
    document = current_graph(installed, record)
    with closing(GraphWorkflowService(path, registry=installed)) as service:
        assert service._preflight_capabilities.__func__ is GraphResourceHost._preflight_capabilities
        service.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
        completed = run(service, create_session(service, document))
        assert completed["status"] == "succeeded"
        assert completed["nodes"][-1]["outputs"]["output"]["items"][0]["text"] == "frozen prompt"
        chain_id = completed["chains"][-1]["chain_run_id"]
        historical = service.get_run(completed["workflow_session_id"], chain_id)
        assert historical["chain"]["inputs"]["_workflow_frozen_resources"] == {
            "global_resource_ids": {uid(10): [identity(record)], uid(11): [identity(record)]},
        }
        assert "frozen prompt" not in canonical_bytes(historical["chain"]).decode()
        assert not hasattr(service, "_native_runtime") and service._resource_frames == {}

    with closing(GraphWorkflowService(path, registry=installed)) as reopened:
        assert reopened.get_run(completed["workflow_session_id"], chain_id) == historical
        changed = deepcopy(record)
        changed["update_sequence"] = 2
        changed["value"]["members"][0]["text"] = "next head"
        reopened.save_global_resource(changed, expected_sequence=1, idempotency_key=str(uuid4()))
        next_round = run(reopened, reopened.get_session(completed["workflow_session_id"]))
        assert next_round["nodes"][-1]["outputs"]["output"]["items"][0]["text"] == "next head"
        assert reopened.get_run(completed["workflow_session_id"], chain_id) == historical
        assert reopened._resource_frames == {}


def test_current_pause_resume_keeps_start_frame_and_next_run_uses_current_head(tmp_path):
    installed, record = independent_registry().detached(), prompt_record()
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10)
        return {}

    installed.register(NodeDefinition(
        "test.pause-gate", "1", "Pause gate", "Test", {},
        {"type": "object", "additionalProperties": False},
    ), gate)
    document = current_graph(installed, record)
    document["nodes"].insert(0, graph_node(installed, "test.pause-gate", 9))
    document["control_edges"] = [{
        "edge_id": uid(40), "source_node_id": uid(9), "target_node_id": uid(10),
    }]
    with closing(GraphWorkflowService(tmp_path / "pause.sqlite", registry=installed)) as service:
        service.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
        initial = create_session(service, document)
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        try:
            assert entered.wait(10)
            running = service.get_session(initial["workflow_session_id"])
            service.control(initial["workflow_session_id"], action="pause",
                            expected_revision=running["revision"], idempotency_key=str(uuid4()))
        finally:
            release.set()
            service.wait(chain_id)
        paused = service.get_session(initial["workflow_session_id"])
        assert paused["status"] == "paused"
        assert service._resource_frames[chain_id][uid(11)] == [record]
        changed = deepcopy(record)
        changed["update_sequence"] = 2
        changed["value"]["members"][0]["text"] = "updated while paused"
        service.save_global_resource(changed, expected_sequence=1, idempotency_key=str(uuid4()))
        service.control(initial["workflow_session_id"], action="resume",
                        expected_revision=paused["revision"], idempotency_key=str(uuid4()))
        service.wait(chain_id)
        completed = service.get_session(initial["workflow_session_id"])
        assert completed["status"] == "succeeded"
        assert completed["nodes"][-1]["outputs"]["output"]["items"][0]["text"] == "frozen prompt"
        assert service._resource_frames == {}
        next_round = run(service, completed)
        assert next_round["nodes"][-1]["outputs"]["output"]["items"][0]["text"] == "updated while paused"
        assert service._resource_frames == {} and not hasattr(service, "_native_runtime")


@pytest.mark.parametrize("problem,reason", [
    ("missing", "global_resource_missing"), ("disabled", "global_content_disabled"),
])
def test_current_preflight_rejects_before_earlier_effects(tmp_path, problem, reason):
    installed, record, effects = independent_registry().detached(), prompt_record(), []

    def effect(config, inputs, context):
        effects.append(context.node_binding_id)
        return {}

    installed.register(NodeDefinition(
        "test.earlier-effect", "1", "Earlier effect", "Test", {},
        {"type": "object", "additionalProperties": False},
    ), effect)
    document = current_graph(installed, record)
    earlier = graph_node(installed, "test.earlier-effect", 9)
    document["nodes"].insert(0, earlier)
    document["control_edges"] = [{
        "edge_id": uid(40), "source_node_id": uid(9), "target_node_id": uid(10),
    }]
    with closing(GraphWorkflowService(tmp_path / "preflight.sqlite", registry=installed)) as service:
        if problem == "disabled":
            record["value"]["enabled"] = False
            service.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
        initial = create_session(service, document)
        with pytest.raises(ContractValidationError) as failure:
            run(service, initial)
        assert failure.value.reason_code == reason
        assert failure.value.diagnostics[0]["node_id"] == uid(10)
        assert service.get_session(initial["workflow_session_id"]) == initial
        assert effects == [] and service._resource_frames == {}


def dependency_host(callback):
    entry = SimpleNamespace(resource_dependencies_declaration=callback)
    return SimpleNamespace(registry=SimpleNamespace(get=lambda *args: entry))


@pytest.mark.parametrize("callback,reason", [
    (None, "graph_resource_dependencies_undeclared"),
    (lambda config: {}, "graph_resource_dependency_invalid"),
    (lambda config: [None], "graph_resource_dependency_invalid"),
    (lambda config: [{"kind": "unknown"}], "graph_resource_dependency_invalid"),
    (lambda config: [{"kind": "global-content", "resource_id": "not-an-id"}],
     "graph_resource_dependency_invalid"),
    (lambda config: [{"kind": "global-resource", "reference": identity(prompt_record()), "extra": True}],
     "graph_resource_dependency_invalid"),
    (lambda config: [None] * 129, "graph_resource_dependency_invalid"),
])
def test_dependency_errors_preserve_reason_and_node(callback, reason):
    host = dependency_host(callback)
    node = {"component_id": "test.reader", "component_version": "1",
            "node_binding_id": uid(10), "config": {}}
    with pytest.raises(ContractValidationError) as failure:
        GraphResourceHost._content_dependencies(host, node)
    assert failure.value.reason_code == reason
    assert failure.value.status_code == 409
    assert failure.value.diagnostics[0]["node_id"] == uid(10)


def test_dependency_callback_failure_and_detached_deduplication():
    node = {"component_id": "test.reader", "component_version": "1",
            "node_binding_id": uid(10), "config": {"nested": {"original": True}}}

    def broken(config):
        raise RuntimeError("private callback detail")

    with pytest.raises(ContractValidationError) as failure:
        GraphResourceHost._content_dependencies(dependency_host(broken), node)
    assert failure.value.reason_code == "graph_resource_dependency_invalid"
    assert "private callback detail" not in str(failure.value)
    assert isinstance(failure.value.__cause__, RuntimeError)

    reference = {"kind": "global-resource", "reference": identity(prompt_record())}

    def declaration(config):
        config["nested"]["original"] = False
        return [reference, deepcopy(reference)]

    host = dependency_host(declaration)
    result = GraphResourceHost._content_dependencies(host, node)
    assert result == [reference]
    result[0]["reference"]["scope"] = "project:changed"
    assert reference["reference"]["scope"] == "project:resources"
    assert node["config"]["nested"]["original"] is True
    assert GraphResourceHost._content_dependencies(dependency_host(None), node, allow_inputs=True) == []


class PreflightHost(GraphResourceHost):
    def __init__(self, entries, records):
        self.registry = SimpleNamespace(get=lambda component, version: entries[component])
        self.records = records
        self.reads = []

    def _global_store(self, store):
        def read_many(references):
            self.reads.append(deepcopy(references))
            return [deepcopy(record) for reference in references for record in self.records
                    if identity(record) == reference]
        return SimpleNamespace(read_many=read_many)

def inherited_plan(source_dependencies, *, input_type="GLOBAL_RESOURCE_REF"):
    source = {"node_binding_id": uid(10), "component_id": "test.source",
              "component_version": "1", "config": {}}
    consumer = {"node_binding_id": uid(11), "component_id": "test.consumer",
                "component_version": "1", "config": {}}
    entries = {
        "test.source": SimpleNamespace(
            resource_dependencies_declaration=lambda config: deepcopy(source_dependencies),
            resource_input_ports=(), resource_preflight_validator=None),
        "test.consumer": SimpleNamespace(
            resource_dependencies_declaration=None, resource_input_ports=("input",),
            resource_preflight_validator=None),
    }
    definitions = {
        uid(10): NodeDefinition("test.source", "1", "Source", "Test", {}, {"type": "object"},
                                capabilities=("resources:read",)),
        uid(11): NodeDefinition("test.consumer", "1", "Consumer", "Test", {}, {"type": "object"},
                                inputs=(NodePort("input", input_type),), capabilities=("resources:read",)),
    }
    plan = SimpleNamespace(
        document={"nodes": [source, consumer]}, ordered_node_ids=[uid(10), uid(11)], definitions=definitions,
        input_edges={uid(10): {}, uid(11): {"input": [{"source_node_id": uid(10)}]}},
    )
    return entries, plan


@pytest.mark.parametrize("source_dependencies,input_type,reason", [
    ([], "GLOBAL_RESOURCE_REF", "graph_resource_dependency_undeclared"),
    ([{"kind": "global-resource", "reference": identity(prompt_record())},
      {"kind": "global-resource", "reference": identity(prompt_record(scope="workspace"))}],
     "GLOBAL_RESOURCE_REF", "graph_resource_dependency_undeclared"),
    ([{"kind": "global-resource", "reference": identity(prompt_record())}],
     "TEXT", "graph_resource_dependency_invalid"),
])
def test_reference_inheritance_requires_one_declared_current_identity(
    source_dependencies, input_type, reason,
):
    entries, plan = inherited_plan(source_dependencies, input_type=input_type)
    host = PreflightHost(entries, [prompt_record(), prompt_record(scope="workspace")])
    with pytest.raises(ContractValidationError) as failure:
        host._preflight_capabilities(SimpleNamespace(store=object()), uid(50), plan)
    assert failure.value.reason_code == reason
    assert failure.value.diagnostics[0]["node_id"] == uid(11)


def test_current_preflight_validator_receives_detached_current_records():
    current = prompt_record()
    node = {"node_binding_id": uid(10), "component_id": "test.reader",
            "component_version": "1", "config": {"source_node_id": uid(10)}}
    calls = []

    def validate(config, records):
        calls.append(deepcopy(records))
        config["source_node_id"] = uid(999)
        records[0]["value"]["enabled"] = False

    entries = {"test.reader": SimpleNamespace(
        resource_dependencies_declaration=lambda config: [
            {"kind": "global-resource", "reference": identity(current)},
        ], resource_input_ports=(), resource_preflight_validator=validate)}
    definition = NodeDefinition("test.reader", "1", "Reader", "Test", {}, {"type": "object"},
                                capabilities=("resources:read",))
    plan = SimpleNamespace(document={"nodes": [node]}, ordered_node_ids=[uid(10)],
                           definitions={uid(10): definition}, input_edges={uid(10): {}})
    host, repo = PreflightHost(entries, [current]), SimpleNamespace(store=object())
    frozen = host._preflight_capabilities(repo, uid(50), plan)
    assert frozen == {
        "global_resource_ids": {uid(10): [identity(current)]},
        "current_global_resources": {uid(10): [current]},
    }
    assert calls == [[current]]
    assert current["value"]["enabled"] is True
    assert node["config"] == {"source_node_id": uid(10)}


def resource_context(reference):
    return SimpleNamespace(
        workflow_session_id=uid(50), node_binding_id=uid(10), chain_run_id=uid(60),
        _external_inputs={"_workflow_frozen_resources": {"global_resource_ids": {uid(10): [reference]}}},
    )


@pytest.mark.parametrize("field,value", [
    ("scope", "workspace"), ("type_id", "example.other"), ("resource_id", uid(999)),
])
def test_current_frame_authorization_uses_full_identity(field, value):
    record = prompt_record()
    host = SimpleNamespace(_resource_frames={uid(60): {uid(10): [record]}})
    context = resource_context(identity(record))
    reference = {**identity(record), field: value}
    with pytest.raises(ContractValidationError) as failure:
        GraphResourceHost._read_current_resource(host, context, reference)
    assert failure.value.reason_code == "graph_resource_dependency_undeclared"


@pytest.mark.parametrize("frames", [{}, {uid(999): {uid(10): [prompt_record()]}},
                                  {uid(60): {uid(999): [prompt_record()]}},
                                  {uid(60): {uid(10): [prompt_record(scope="workspace")]}}])
def test_declared_resource_never_falls_back_when_exact_frame_is_missing(frames):
    record = prompt_record()
    host = SimpleNamespace(_resource_frames=frames)
    with pytest.raises(ContractValidationError) as failure:
        GraphResourceHost._read_current_resource(host, resource_context(identity(record)), identity(record))
    assert failure.value.reason_code == "recovery_unavailable"


def test_current_frame_returns_detached_frozen_record_through_current_host():
    record = prompt_record()
    host = GraphResourceHost()
    host._resource_frames = {uid(60): {uid(10): [record]}}
    context = resource_context(identity(record))
    returned = GraphResourceHost._read_current_resource(host, context, identity(record))
    returned["value"]["members"][0]["text"] = "caller edit"
    assert record["value"]["members"][0]["text"] == "frozen prompt"
    assert host._host_call(context, "resources:read", "current-global-resource", identity(record)) == record


def test_current_resource_host_refuses_unregistered_operations():
    with pytest.raises(ContractValidationError) as failure:
        GraphResourceHost()._host_call(None, "resources:read", "global-content", {})
    assert failure.value.reason_code == "graph_capability_unknown"


def test_fresh_process_current_start_blocks_legacy_resolver_modules(tmp_path):
    script = r"""
import importlib.abc
import sys
from contextlib import closing

blocked = {
    "phase1_agent.graph_agent_host",
    "phase1_agent.graph_agent_runtime",
    "phase1_agent.graph_nodes",
    "phase1_agent.model_configuration_store",
    "phase1_agent.workbench_resources",
    "phase1_agent.workflow",
}
class NoLegacyResolvers(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError("Current resources loaded a legacy resolver: " + fullname)
        return None
sys.meta_path.insert(0, NoLegacyResolvers())
sys.path.insert(0, sys.argv[2])
from test_graph_resource_host import (
    GraphResourceHost, GraphWorkflowService, create_session,
    current_graph, independent_registry, prompt_record, run, uid, uuid4,
)
record = prompt_record("fresh isolated prompt")
registry = independent_registry()
with closing(GraphWorkflowService(sys.argv[1], registry=registry)) as service:
    assert service._preflight_capabilities.__func__ is GraphResourceHost._preflight_capabilities
    service.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
    result = run(service, create_session(service, current_graph(registry, record)))
    assert result["status"] == "succeeded"
    assert result["nodes"][-1]["outputs"]["output"]["items"][0]["text"] == "fresh isolated prompt"
    assert service._resource_frames == {} and not hasattr(service, "_native_runtime")
with closing(GraphWorkflowService(sys.argv[1], registry=registry)) as reopened:
    result = run(reopened, reopened.get_session(result["workflow_session_id"]))
    assert result["status"] == "succeeded"
assert not blocked.intersection(sys.modules)
print("isolated current resources passed")
"""
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path / "isolated.sqlite"),
         str(Path(__file__).resolve().parent)],
        capture_output=True, text=True, env=environment, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "isolated current resources passed"
