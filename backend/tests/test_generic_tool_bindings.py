"""Pure tools through disk discovery, Host selection and real offline Agent calls."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeDefinition, NodeRegistry
from phase1_agent.model_host_service import MODEL_SERVICE_REF
from phase1_agent.tool_binding_contract import PreparedPureToolSpec, PureToolBindingAdapter
from phase1_agent.runtime_executor_contracts import ExecutorReference
from phase1_agent.graph_tool_host import GraphToolHost
from phase1_agent.workflow_host import WorkflowHost

from test_graph_service import edge, node
from test_local_package_integration import save_configuration
from test_models_service_integration import ModelDatabaseFixture
from test_variable_tool_bindings import (
    KEY, VariableToolTransport, _events, _registered, _run, build_graph,
)
from workflow_test_support import graph


PACKAGE = "example.text-metrics"
TARGET = "example.text-metrics"
LOADER = "example.text-metrics-tools"
EXAMPLE = Path(__file__).resolve().parents[2] / "examples/workflow-packages/text-metrics-1.0.0"


@pytest.fixture
def tool_host(tmp_path):
    root = tmp_path / "packages/workflow"
    shutil.copytree(EXAMPLE, root / EXAMPLE.name)
    database = tmp_path / "pure-tools.sqlite"
    transport = VariableToolTransport([
        ("text_metrics", {"text": "a b\nc", "include_spaces": False}),
        ("final_answer", {"answer": {"text": "computed"}}),
    ])
    with closing(WorkflowHost(database)) as host:
        service = host.graph_service
        application = GraphApplication(host)
        assert service.registry.get(LOADER, "1") is None
        assert any(row.get("package_id") == PACKAGE or row.get("manifest", {}).get("package_id") == PACKAGE
                   for row in service.platform_capabilities()["local_package_sources"]["packages"])
        application.command("packages.configure", {
            "enabled_packages": {**DEFAULT_PACKAGES, PACKAGE: "1.0.0"}})
        service._service_options[MODEL_SERVICE_REF]["transport_factory"] = transport.factory
        ModelDatabaseFixture.write(service, 1)
        document = graph(service)
        agent = next(row for row in document["nodes"] if row["component_id"] == "agents.execute")
        assembly = next(row for row in document["nodes"] if row["component_id"] == "context.assembly")
        loader = node(service.registry, LOADER, 501, component_version="1",
                      agent_node_id=agent["node_binding_id"])
        document["nodes"].append(loader)
        document["edges"].append(edge(
            loader, assembly, 1501, source_port="materials", target_port="materials"))
        document.update(schema_version=3, edit_sequence=0, control_edges=[],
                        package_lock=deepcopy(list(service.registry.execution_package_lock)))
        for row in document["nodes"]:
            row["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        _, saved = save_configuration(application, document)
        revision = saved["result"]["definition_revision"]
        reread = application.query("definition.read", {"identity": document["workflow_definition_id"]})
        assert next(row for row in reread["nodes"] if row["component_id"] == LOADER)["config"] == loader["config"]
        session = application.command("session.create", {
            "workflow_definition_id": document["workflow_definition_id"],
            "definition_revision": revision, "idempotency_key": str(uuid4())})["result"]
        yield SimpleNamespace(
            host=host, service=service, application=application, transport=transport,
            document=document, session=session, agent=agent, loader=loader, revision=revision,
            reread=reread, database=database)


def start(case):
    return case.application.command("run.start", {
        "session_id": case.session["workflow_session_id"],
        "expected_revision": case.session["revision"],
        "idempotency_key": str(uuid4()), "inputs": {"text": "compute text metrics"},
    })["result"]["active_chain_run_id"]


def finish(case):
    chain_id = start(case)
    case.service.wait(chain_id)
    sid = case.session["workflow_session_id"]
    return chain_id, case.service.get_session(sid)


def replace_entry(case, component, **changes):
    entry = case.service.registry.get(component, "1")
    case.service.registry._nodes[(component, "1")] = replace(entry, **changes)
    return entry


def test_disk_tool_formal_configuration_model_call_facts_and_reopen(tool_host):
    case = tool_host
    chain_id, session = finish(case)
    assert session["status"] == "succeeded", session["chains"]
    assert not any(row["object_key"].startswith("variable/") for row in case.document["object_bindings"])
    assert len(case.transport.calls) == 2
    definitions = case.transport.calls[0]["tools"]
    actual = next(row["function"] for row in definitions if row["function"]["name"] == "text_metrics")
    assert actual["parameters"]["required"] == ["text", "include_spaces"]
    prompt = next(row["outputs"]["materials"] for row in session["nodes"]
                  if row["node_binding_id"] == case.loader["node_binding_id"])
    assert {row["purpose"] for row in prompt["items"]} == {"tool-description", "tool-schema"}
    assert all(row["source"]["agent_node_id"] == case.agent["node_binding_id"]
               for row in prompt["items"])
    assert "optionally excluding spaces" in str(case.transport.calls[0]["messages"])
    settled = [event["payload"] for event in _events(case.service, session, chain_id)
               if event["kind"] == "tool_settled"]
    assert settled[0]["outcome"] == "success"
    assert settled[0]["message"]["blocks"][0]["content"] == {"characters": 4, "lines": 2}
    sid = case.session["workflow_session_id"]
    run = case.application.query("run.read", {"session_id": sid, "chain_id": chain_id})
    case.host.close()
    with closing(WorkflowHost(case.database)) as reopened:
        application = GraphApplication(reopened)
        assert application.query("definition.read", {
            "identity": case.document["workflow_definition_id"]}) == case.reread
        assert application.query("run.read", {"session_id": sid, "chain_id": chain_id}) == run
        assert application.query("session.read", {"session_id": sid}) == session


def test_preparation_never_calls_business_and_freezes_specs_and_registry(tool_host, monkeypatch):
    case = tool_host
    entry = case.service.registry.get(LOADER, "1")
    declaration = entry.tool_binding.normalize(case.loader["config"])
    calls = []
    original_target = case.service.registry.get(TARGET, "1")

    def counted_call(spec, arguments):
        calls.append("call")
        return entry.tool_binding.call(spec, arguments)

    def counted_result(spec, outputs):
        calls.append("result")
        return entry.tool_binding.result(spec, outputs)

    def counted_execute(config, inputs, context):
        calls.append("execute")
        assert context._state == {} and context._object_states == {} and context._host is None
        assert context._tool_bridge is None and context._private_states == {}
        return original_target.executor(config, inputs, context)

    replace_entry(case, LOADER, tool_binding=replace(
        entry.tool_binding, normalize=lambda config: declaration,
        call=counted_call, result=counted_result))
    replace_entry(case, TARGET, executor=counted_execute)
    pending = []
    original_submit = case.service._executor.submit
    monkeypatch.setattr(case.service._executor, "submit",
                        lambda *args, **kwargs: pending.append((args, kwargs)))
    chain_id = start(case)
    assert calls == [] and case.transport.calls == []
    frame = case.service._tool_frames[chain_id]
    binding = frame.bindings_for(case.agent["node_binding_id"])[0]
    spec = binding.tools[0]
    assert isinstance(spec, PreparedPureToolSpec) and spec.operation == "invoke"
    assert not hasattr(spec, "object_key") and not hasattr(spec, "access")
    assert spec.source == ({"package_id": PACKAGE, "package_version": "1.0.0"},)
    declaration[0]["name"] = "tampered"
    declaration[0]["parameters"]["properties"].clear()
    spec.public()["parameters"]["properties"].clear()
    spec.adapter_spec()["config"].clear()
    assert spec.name == "text_metrics" and len(spec.parameters["properties"]) == 2
    replace_entry(case, TARGET, executor=lambda *args: pytest.fail("current registry was reread"))
    args, kwargs = pending[0]
    future = original_submit(*args, **kwargs)
    future.result(timeout=30)
    assert case.service.get_session(case.session["workflow_session_id"])["status"] == "succeeded"
    assert calls == ["call", "execute", "result"]


def test_pure_and_variable_tools_share_one_agent_without_changing_acceptance(tool_host):
    case = tool_host
    case.transport.rounds = [
        ("score_set", {"value": 10}),
        ("text_metrics", {"text": "a b\nc", "include_spaces": False}),
        ("score_read", {}),
        ("final_answer", {"answer": {"text": "both completed"}}),
    ]
    document, parts = build_graph(case.service, with_normal=False)
    loader = node(case.service.registry, LOADER, 501, component_version="1",
                  agent_node_id=parts["execute"]["node_binding_id"])
    document["nodes"].append(loader)
    document["edges"].append(edge(
        loader, parts["assembly"], 1501, source_port="materials", target_port="materials"))
    registered = _registered(case.service, document)
    before_revision = registered["objects"][KEY]["revision"]
    chain_id, session = _run(case.service, registered)
    assert session["status"] == "succeeded", session["chains"]
    assert session["objects"][KEY]["value"]["value"] == 10
    assert session["objects"][KEY]["revision"] == before_revision + 1
    settled = [event["payload"] for event in _events(case.service, session, chain_id)
               if event["kind"] == "tool_settled"]
    contents = [event["message"]["blocks"][0]["content"] for event in settled]
    assert contents[0]["value"] == contents[2]["value"] == 10
    assert contents[1] == {"characters": 4, "lines": 2}


def test_invalid_model_arguments_never_reach_pure_executor(tool_host):
    case = tool_host
    executed = []
    target = case.service.registry.get(TARGET, "1")

    def execute(config, inputs, context):
        executed.append({"config": deepcopy(config), "inputs": deepcopy(inputs)})
        return target.executor(config, inputs, context)

    replace_entry(case, TARGET, executor=execute)
    calls = []

    def factory(*, provider, parameters, api_key):
        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate_prepared(self, prepared):
                wire = prepared["transport_arguments"]
                return self.generate(wire["messages"], wire.get("tools", []))

            def generate(self, messages, tools):
                calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
                if len(calls) == 1:
                    # One otherwise valid call stays in the same batch: a schema
                    # error must reject the complete batch, not just one call.
                    return ModelResponse("tool_calls", tool_calls=(
                        ModelToolCall("call.invalid", "text_metrics", json.dumps(
                            {"text": "a b", "include_spaces": "wrong"})),
                        ModelToolCall("call.not-executed", "text_metrics", json.dumps(
                            {"text": "not executed", "include_spaces": False})),
                    ))
                if len(calls) == 2:
                    assert executed == []
                    return ModelResponse("tool_calls", tool_calls=(
                        ModelToolCall("call.corrected", "text_metrics", json.dumps(
                            {"text": "a b", "include_spaces": False})),
                    ))
                return ModelResponse("tool_calls", tool_calls=(
                    ModelToolCall("call.final", "final_answer", json.dumps(
                        {"answer": {"text": "computed"}})),
                ))

            def close(self):
                pass

        return Transport()

    case.service._service_options[MODEL_SERVICE_REF]["transport_factory"] = factory
    chain_id, session = finish(case)
    assert session["status"] == "succeeded", session["chains"]
    # Three model rounds: invalid batch -> full feedback -> corrected call -> final.
    assert len(calls) == 3
    expected_feedback = (
        "Invalid tool arguments for: text_metrics. No tools in this batch were executed. "
        "Generate a new tool call batch with valid arguments.")
    assert not any(isinstance(message, dict)
                   and "Invalid tool arguments for: text_metrics." in str(message.get("content", ""))
                   for message in calls[0]["messages"])
    last = calls[1]["messages"][-1]
    assert isinstance(last, dict) and last.get("role") == "user"
    assert last.get("content") == expected_feedback
    # The rejected batch executed no tool at all; only the corrected call ran.
    assert len(executed) == 1
    assert executed[0]["config"] == {"include_spaces": False}
    assert executed[0]["inputs"]["input"]["text"] == "a b"
    settled = [event["payload"] for event in _events(case.service, session, chain_id)
               if event["kind"] == "tool_settled"]
    assert [row["outcome"] for row in settled] == ["success", "success"]
    assert [row["message"]["blocks"][0]["content"] for row in settled] == [
        {"characters": 2, "lines": 1}, {"text": "computed"}]


@pytest.mark.parametrize("capability", [
    "objects:read", "objects:write", "variables:read", "variables:write",
    "shared:read", "shared:write", "resources:read", "external:read",
    "private:read", "private:write",
])
def test_pure_target_rejects_business_capabilities_before_model(tool_host, capability):
    case = tool_host
    entry = case.service.registry.get(TARGET, "1")
    replace_entry(case, TARGET, definition=replace(entry.definition, capabilities=(capability,)))
    with pytest.raises(ContractValidationError) as caught:
        start(case)
    assert caught.value.reason_code == "tool_binding_target_unsupported"
    assert case.transport.calls == []


@pytest.mark.parametrize("field,value", [
    ("external_inputs_declaration", lambda config: []),
    ("resource_dependencies_declaration", lambda config: []),
    ("resource_input_ports", ("input",)),
    ("settlement_builder", lambda *args: {}),
    ("executor_ref", ExecutorReference("example.hosted", "1")),
])
def test_pure_target_rejects_host_or_special_execution(tool_host, field, value):
    case = tool_host
    replace_entry(case, TARGET, **{field: value})
    with pytest.raises(ContractValidationError) as caught:
        start(case)
    assert caught.value.reason_code == "tool_binding_target_unsupported"
    assert case.transport.calls == []


@pytest.mark.parametrize("change", ["duplicate", "optional", "nested", "missing-source", "missing-target"])
def test_invalid_pure_preparation_is_not_silently_reduced(tool_host, change):
    case = tool_host
    entry = case.service.registry.get(LOADER, "1")
    declarations = entry.tool_binding.normalize(case.loader["config"])
    if change == "duplicate":
        declarations.append(deepcopy(declarations[0]))
    elif change == "optional":
        declarations[0]["parameters"]["required"] = ["text"]
    elif change == "nested":
        declarations[0]["parameters"]["properties"]["text"]["type"] = "object"
    elif change == "missing-target":
        declarations[0]["target"]["component_id"] = "example.absent"
    else:
        manifests = deepcopy(case.service._package_manifests)
        manifest = next(row for row in manifests if row["package_id"] == PACKAGE)
        manifest["exports"]["nodes"] = [
            row for row in manifest["exports"]["nodes"] if row["component_id"] != TARGET]
        case.service._package_manifests = tuple(manifests)
    replace_entry(case, LOADER, tool_binding=replace(
        entry.tool_binding, normalize=lambda config: declarations))
    with pytest.raises((ContractValidationError, ValueError)):
        start(case)
    assert case.transport.calls == []


@pytest.mark.parametrize("failure", ["bad-config", "bad-input", "bad-output", "effect", "bad-result"])
def test_pure_invocation_rejects_invalid_adapter_or_executor_result(tool_host, failure):
    case = tool_host
    loader = case.service.registry.get(LOADER, "1")
    target = case.service.registry.get(TARGET, "1")
    executed = []

    def execute(config, inputs, context):
        executed.append(True)
        if failure == "effect":
            context.effects.append({"kind": "unexpected-business-effect"})
        if failure == "bad-output":
            return {"undeclared": {}}
        return target.executor(config, inputs, context)

    if failure == "bad-config":
        replace_entry(case, LOADER, tool_binding=replace(
            loader.tool_binding, call=lambda spec, args: ({"include_spaces": "wrong"}, {})))
    elif failure == "bad-input":
        replace_entry(case, LOADER, tool_binding=replace(
            loader.tool_binding, call=lambda spec, args: (spec["config"], {"input": {"wrong": True}})))
    elif failure == "bad-result":
        replace_entry(case, LOADER, tool_binding=replace(
            loader.tool_binding, result=lambda spec, outputs: ["not-an-object"]))
    replace_entry(case, TARGET, executor=execute)
    chain_id, session = finish(case)
    assert session["status"] == "failed", session["chains"]
    settled = [event["payload"] for event in _events(case.service, session, chain_id)
               if event["kind"] == "tool_settled"]
    assert not any(event["outcome"] == "success" for event in settled)
    assert len(case.transport.calls) == 1
    assert bool(executed) == (failure not in ("bad-config", "bad-input"))


def test_pure_loading_node_keeps_existing_registry_constraints():
    adapter = PureToolBindingAdapter(
        normalize=lambda config: [], call=lambda spec, args: ({}, {}),
        result=lambda spec, outputs: {})
    registry = NodeRegistry()
    definition = NodeDefinition("example.invalid-loader", "1", "Invalid", "Examples", {}, {})
    with pytest.raises(ContractValidationError):
        registry.register(definition, lambda *args: {}, tool_binding=adapter)


@pytest.mark.parametrize("form", ["async-function", "async-callable", "generator-callable",
                                  "async-generator-callable"])
def test_pure_target_requires_synchronous_executor(tool_host, form):
    async def asynchronous(config, inputs, context):
        return {}

    class AsyncCallable:
        async def __call__(self, config, inputs, context):
            return {}

    class GeneratorCallable:
        def __call__(self, config, inputs, context):
            yield {}

    class AsyncGeneratorCallable:
        async def __call__(self, config, inputs, context):
            yield {}

    case = tool_host
    replace_entry(case, TARGET, executor={
        "async-function": asynchronous, "async-callable": AsyncCallable(),
        "generator-callable": GeneratorCallable(),
        "async-generator-callable": AsyncGeneratorCallable(),
    }[form])
    with pytest.raises(ContractValidationError) as caught:
        start(case)
    assert caught.value.reason_code == "tool_binding_target_unsupported"
    assert case.transport.calls == []


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")],
                         ids=["nan", "infinity", "negative-infinity"])
def test_direct_pure_bridge_rejects_nonfinite_json_before_host_or_adapter(number):
    spec = PreparedPureToolSpec(
        "numeric", "Numeric input", {
            "type": "object", "properties": {"value": {"type": "number", "description": "Value"}},
            "required": ["value"], "additionalProperties": False},
        TARGET, "1", {}, str(uuid4()), str(uuid4()))
    frame = SimpleNamespace(
        agent_bindings={spec.agent_node_id: ()}, find=lambda *args: (object(), spec))
    host = GraphToolHost()
    context = SimpleNamespace(chain_run_id=str(uuid4()), node_binding_id=spec.agent_node_id)
    host._tool_frames = {context.chain_run_id: frame}
    with pytest.raises(ContractValidationError) as caught:
        host._invoke_bound_tool(context, {
            "binding_id": spec.binding_id, "operation": "invoke",
            "arguments": {"value": number}, "tool_call_id": "nonfinite",
            "tool_execution_id": str(uuid4())})
    assert caught.value.reason_code == "tool_arguments_invalid"
