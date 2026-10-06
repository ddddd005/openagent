"""Public model boundary tests: no external model calls or Agent executor."""

from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import create_content_package
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_contracts import GraphCompiler
from phase1_agent.graph_execution import execute_graph
from phase1_agent.model_contract import (
    CHAT_PROVIDER_TYPE, validate_chat_provider, validate_public_model_binding,
    validate_public_model_result,
)
from phase1_agent.model_package import create_model_package, MODEL_FRONTEND_EXTENSIONS
from phase1_agent.model_service import (
    EnvironmentCredentialBroker, ModelCapabilityAdapter, PublicModelService, create_chat_transport,
)
from phase1_agent.prompt_contract import assemble_prompt
from phase1_agent.prompt_package import create_prompt_package


def test_model_package_declares_exact_current_resource_management_and_source_editor():
    package = create_model_package()
    loaded = CapabilityPackageLoader((create_content_package(), package)).load({"workflow.models": "1.0.0"})
    expected = sorted([
        {**deepcopy(row), "schema_version": 2, "host_protocol_version": 1,
         "package_id": "workflow.models", "package_version": "1.0.0"}
        for row in MODEL_FRONTEND_EXTENSIONS
    ], key=lambda row: row["extension_id"])
    assert list(loaded.frontend_extensions) == expected
    assert package.manifest.to_dict()["exports"]["frontend_extensions"] == [
        {"extension_id": row["extension_id"]} for row in MODEL_FRONTEND_EXTENSIONS]
    fields = next(row for row in loaded.frontend_extensions if row["kind"] == "field-editor")
    assert fields["binding"]["target"] == {"component_id": "models.source", "component_version": "1"}


def uid(number):
    return str(UUID(int=number, version=4))


def source_config():
    return {
        "reference": {"envelope_version": 1, "scope": "workspace",
                      "type_id": CHAT_PROVIDER_TYPE, "resource_id": uid(1)},
        "parameters": {"model": "deepseek-test", "max_tokens": 32,
                       "temperature": 0.5, "thinking": "disabled", "stream": False},
    }


class BoundaryFixture:
    def __init__(self, *, transport=None):
        self.config = source_config()
        self.record = {
            **self.config["reference"], "data_schema_version": 1, "update_sequence": 1,
            "value": {"name": "Test", "protocol": "chat", "base_url": "https://example.test",
                      "credential_ref": "env:DEEPSEEK_API_KEY", "enabled": True},
        }
        self.environ = {"DEEPSEEK_API_KEY": "private-fixture-secret"}
        self.facts, self.factory_args = [], []
        self.calls, self.closes = 0, 0
        self.fail_stage = None
        self.transport = transport
        self.prompt = assemble_prompt([], {"schema_version": 2, "kind": "workflow.text",
                                          "text": "Hello"})
        self.source = self.context(10, 11, ("models:resolve",))
        self.consumer = self.context(20, 21, ("models:call",))
        self.service = PublicModelService(
            provider_reader=lambda reference: deepcopy(self.record),
            credential_broker=EnvironmentCredentialBroker(self.environ),
            accept_fact=self.accept, resolve_input=self.resolve,
            transport_factory=self.factory,
        )
        self.service.prepare_run(workflow_session_id=uid(2), chain_run_id=uid(3),
                                 node_configs={uid(10): self.config}, records=[self.record])
        self.binding = self.service(self.source, "models:resolve", "bind-model", self.config)
        self.service.accept_binding_output(workflow_session_id=uid(2), chain_run_id=uid(3),
                                           node_run_id=uid(11), binding_id=self.binding["binding_id"],
                                           output_id=uid(30))

    def context(self, node, invocation, capabilities):
        refs = {"model": [{"edge_id": uid(40), "output_id": uid(30), "order": 0}],
                "prompt": [{"edge_id": uid(41), "output_id": uid(31), "order": 0}]}
        return SimpleNamespace(
            workflow_session_id=uid(2), chain_run_id=uid(3), node_binding_id=uid(node),
            node_run_id=uid(invocation), definition=SimpleNamespace(capabilities=capabilities),
            refs=refs, input_artifact_refs=lambda port: deepcopy(refs.get(port, [])),
        )

    def resolve(self, context, port):
        return deepcopy(self.binding if port == "model" else self.prompt)

    def accept(self, context, fact):
        if self.fail_stage == fact["stage"]:
            raise RuntimeError("private-fixture-secret must never reach diagnostics")
        self.facts.append(deepcopy(fact))
        return {"fact_id": fact["fact_id"]}

    def factory(self, **kwargs):
        self.factory_args.append(deepcopy(kwargs))
        fixture = self

        class Transport:
            max_retries = 0

            def generate(self, messages, tools):
                fixture.calls += 1
                if fixture.transport:
                    return fixture.transport(messages, tools)
                return ModelResponse("stop", "World", usage={"completion_tokens": 1},
                                     response_id="response-1", model="deepseek-test")

            def close(self):
                fixture.closes += 1

        return Transport()

    def chat(self):
        return self.service(self.consumer, "models:call", "chat",
                            {"binding_id": self.binding["binding_id"]})


def test_model_package_exports_without_agent_dependency():
    loaded = CapabilityPackageLoader((create_content_package(), create_model_package())).load(
        {"workflow.models": "1.0.0"})
    assert loaded.registry.get("workflow.agent", "1") is None
    assert loaded.registry.get("models.source", "1").resource_dependencies_declaration is not None
    assert loaded.registry.get("models.chat", "1").definition.input_storage == "references"
    assert loaded.registry.data_types.get(CHAT_PROVIDER_TYPE, 1, scope="global") is not None
    assert loaded.package_lock == ({"package_id": "workflow.content", "version": "1.0.0"},
                                   {"package_id": "workflow.models", "version": "1.0.0"})


@pytest.mark.parametrize("changes", [
    {"api_key": "secret"}, {"credential_ref": "env:ARBITRARY_KEY"},
    {"base_url": "https://user:secret@example.test"},
    {"base_url": "https://example.test?token=secret"}, {"base_url": "http://remote.test"},
])
def test_provider_rejects_secret_fields_and_uncontrolled_connections(changes):
    fixture = BoundaryFixture()
    with pytest.raises(ContractValidationError):
        validate_chat_provider({**fixture.record["value"], **changes})


def test_binding_and_request_facts_are_secret_free_and_reentry_never_redispatches():
    fixture = BoundaryFixture()
    result = fixture.chat()
    assert validate_public_model_binding(fixture.binding) == fixture.binding
    assert validate_public_model_result(result) == result
    assert [fact["stage"] for fact in fixture.facts] == ["request", "attempt", "outcome"]
    assert [fact["sequence"] for fact in fixture.facts] == [1, 2, 3]
    assert fixture.facts[0]["details"]["wire_request"]["thinking"] == {"type": "disabled"}
    assert fixture.facts[0]["details"]["messages"] == [
        {"kind": "text", "role": "user", "content": "Hello"}]
    assert fixture.facts[0]["details"]["input_refs"]["model"] == fixture.consumer.refs["model"]
    public = json.dumps([fixture.binding, fixture.facts, result])
    assert "private-fixture-secret" not in public
    assert "credential" not in public
    assert "base_url" not in public
    assert "final_answer" not in public
    assert fixture.chat() == result
    assert fixture.calls == 1


def test_current_edit_keeps_frozen_address_parameters_and_new_run_uses_current():
    fixture = BoundaryFixture()
    fixture.record["value"]["base_url"] = "https://edited.test"
    fixture.record["update_sequence"] = 2
    fixture.chat()
    assert fixture.factory_args[0]["provider"]["base_url"] == "https://example.test"
    fixture.service.prepare_run(workflow_session_id=uid(2), chain_run_id=uid(4),
                               node_configs={uid(10): fixture.config}, records=[fixture.record])
    context = fixture.context(10, 12, ("models:resolve",))
    context.chain_run_id = uid(4)
    binding = fixture.service.bind_model(context, fixture.config)
    assert binding["binding_id"] != fixture.binding["binding_id"]
    assert fixture.service.active_frame_count == 2


@pytest.mark.parametrize("policy_change", ["disabled", "credential_changed", "credential_missing"])
def test_dispatch_boundary_rechecks_revocation_and_credential_lease(policy_change):
    fixture = BoundaryFixture()
    if policy_change == "disabled":
        fixture.record["value"]["enabled"] = False
    elif policy_change == "credential_changed":
        fixture.environ["DEEPSEEK_API_KEY"] = "rotated-secret"
    else:
        fixture.environ.pop("DEEPSEEK_API_KEY")
    with pytest.raises(ContractValidationError) as caught:
        fixture.chat()
    assert fixture.calls == 0
    assert caught.value.reason_code == "model_not_dispatched"
    assert fixture.facts[-1]["details"]["classification"] == "not_dispatched"


@pytest.mark.parametrize("stage", ["request", "attempt"])
def test_fact_acceptance_failure_fences_dispatch_and_sanitizes_errors(stage):
    fixture = BoundaryFixture()
    fixture.fail_stage = stage
    with pytest.raises(ContractValidationError) as caught:
        fixture.chat()
    assert fixture.calls == 0
    assert "private-fixture-secret" not in str(caught.value)


def test_outcome_acceptance_failure_retains_response_and_retries_only_commit():
    fixture = BoundaryFixture()
    fixture.fail_stage = "outcome"
    with pytest.raises(ContractValidationError) as caught:
        fixture.chat()
    assert caught.value.reason_code == "model_fact_acceptance_failed"
    assert fixture.calls == 1
    fixture.fail_stage = None
    result = fixture.service.retry_acceptance(fixture.consumer)
    assert result["content"] == "World"
    assert fixture.calls == 1
    assert fixture.chat() == result
    assert len(fixture.facts) == 3


@pytest.mark.parametrize("error", [
    httpx.ReadTimeout("secret failed read"), ConnectionError("secret disconnected"),
    RuntimeError("private-fixture-secret raw SDK error"),
])
def test_dispatched_failures_are_unknown_and_never_retried(error):
    def fail(messages, tools):
        raise error
    fixture = BoundaryFixture(transport=fail)
    for _ in range(2):
        with pytest.raises(ContractValidationError) as caught:
            fixture.chat()
        assert caught.value.reason_code == "model_dispatch_unknown"
        assert "private-fixture-secret" not in str(caught.value)
    assert fixture.calls == 1
    assert fixture.facts[-1]["details"]["classification"] == "dispatch_unknown"


@pytest.mark.parametrize("change", ["run", "capability", "output", "unaccepted", "binding"])
def test_binding_requires_exact_owner_capability_and_accepted_producer_output(change):
    fixture = BoundaryFixture()
    if change == "run":
        fixture.consumer.chain_run_id = uid(88)
    elif change == "capability":
        fixture.consumer.definition.capabilities = ()
    elif change == "output":
        fixture.consumer.refs["model"][0]["output_id"] = uid(89)
    elif change == "unaccepted":
        fixture.consumer.refs["model"].clear()
    else:
        fixture.binding["parameters"]["model"] = "forged-model"
    with pytest.raises(ContractValidationError):
        fixture.chat()
    assert fixture.calls == 0


def test_released_or_restart_lost_bindings_cannot_reactivate_history():
    fixture = BoundaryFixture()
    fixture.chat()
    fixture.service.release_run(uid(2), uid(3))
    assert fixture.service.active_frame_count == 0
    assert fixture.closes == 1
    with pytest.raises(ContractValidationError) as caught:
        fixture.chat()
    assert caught.value.reason_code == "model_recovery_unavailable"
    assert fixture.calls == 1


def test_changed_input_reentry_remains_fenced_without_request_allowance():
    fixture = BoundaryFixture()
    fixture.chat()
    fixture.prompt = assemble_prompt([], {"schema_version": 2, "kind": "workflow.text",
                                          "text": "Changed"})
    with pytest.raises(ContractValidationError) as caught:
        fixture.chat()
    assert caught.value.reason_code == "model_request_conflict"
    assert fixture.calls == 1


def test_more_than_old_run_request_limit_succeeds_and_idempotent_reentry_reuses_each_response():
    fixture = BoundaryFixture()
    messages = [{"kind": "text", "role": "user", "content": "Hello"}]
    results = []
    for index in range(129):
        request = {"binding_id": fixture.binding["binding_id"], "request_key": f"loop-{index}",
                   "messages": messages, "tools": []}
        result = fixture.service.call_model(fixture.consumer, **request)
        assert result["content"] == "World"
        assert fixture.service.call_model(fixture.consumer, **request) == result
        assert fixture.calls == index + 1
        results.append(result)
    assert len({result["request_id"] for result in results}) == 129
    assert fixture.calls == 129 and len(fixture.factory_args) == 1
    assert len(fixture.facts) == 129 * 3
    assert [fact["stage"] for fact in fixture.facts] == ["request", "attempt", "outcome"] * 129
    fixture.service.release_run(uid(2), uid(3))
    assert fixture.closes == 1 and fixture.service.active_frame_count == 0


def test_public_facade_can_return_tool_calls_without_executing_them():
    fixture = BoundaryFixture(transport=lambda messages, tools: ModelResponse(
        "tool_calls", tool_calls=(ModelToolCall("call-1", "inspect", "{}"),)))
    result = fixture.service.call_model(
        fixture.consumer, binding_id=fixture.binding["binding_id"], request_key="loop-1",
        messages=[{"kind": "text", "role": "user", "content": "Hello"}],
        tools=[{"type": "function", "function": {"name": "inspect", "description": "Inspect",
                                               "parameters": {"type": "object"}}}])
    assert result["tool_calls"][0]["name"] == "inspect"
    assert fixture.calls == 1


def test_capability_adapter_returns_public_tool_response_and_keeps_failed_request_key():
    fixture = BoundaryFixture(transport=lambda messages, tools: ModelResponse(
        "tool_calls", tool_calls=(ModelToolCall("call-1", "inspect", "{}"),)))
    fixture.consumer.host_call = lambda capability, operation, payload: fixture.service(
        fixture.consumer, capability, operation, payload)
    adapter = ModelCapabilityAdapter(fixture.consumer, fixture.binding)
    messages = [{"kind": "text", "role": "user", "content": "Hello"}]
    tools = [{"type": "function", "function": {"name": "inspect", "description": "Inspect",
                                              "parameters": {"type": "object"}}}]
    fixture.fail_stage = "outcome"
    with pytest.raises(ContractValidationError):
        adapter.generate(messages, tools)
    fixture.fail_stage = None
    response = adapter.generate(messages, tools)
    assert response.tool_calls == (ModelToolCall("call-1", "inspect", "{}"),)
    assert fixture.calls == 1
    assert adapter.model_parameters == fixture.binding["parameters"]
    assert adapter.max_retries == 0


def test_kernel_model_requires_exact_accepted_prompt_and_strict_json_payload():
    fixture = BoundaryFixture()
    payload = {"binding_id": fixture.binding["binding_id"], "request_key": "loop-1",
               "messages": [{"kind": "text", "role": "user", "content": "Hello"}], "tools": []}
    with pytest.raises(ContractValidationError):
        fixture.service(fixture.consumer, "models:call", "kernel-model", {**payload, "api_key": "secret"})
    fixture.consumer.refs["prompt"].clear()
    with pytest.raises(ContractValidationError):
        fixture.service(fixture.consumer, "models:call", "kernel-model", payload)
    assert fixture.calls == 0


def test_ordinary_chat_rejects_unexpected_tool_calls_after_recording_response():
    fixture = BoundaryFixture(transport=lambda messages, tools: ModelResponse(
        "tool_calls", tool_calls=(ModelToolCall("call-1", "inspect", "{}"),)))
    with pytest.raises(ContractValidationError) as caught:
        fixture.chat()
    assert caught.value.reason_code == "model_unexpected_tool_calls"
    assert fixture.facts[-1]["details"]["classification"] == "response_received"
    assert fixture.calls == 1


def test_existing_transport_http_projection_matches_accepted_wire_basis_and_zero_sdk_retries():
    observed = []
    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "response-http", "object": "chat.completion", "created": 0,
            "model": "deepseek-test", "choices": [
                {"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "HTTP mock"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        })
    fixture = BoundaryFixture()
    fixture.service._factory = lambda **kwargs: create_chat_transport(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = fixture.chat()
    assert result["content"] == "HTTP mock"
    assert observed == [fixture.facts[0]["details"]["wire_request"]]
    adapter = next(iter(fixture.service._bindings.values())).adapter
    assert adapter.implementation.max_retries == 0
    assert adapter.implementation._client.max_retries == 0
    fixture.service.release_run(uid(2), uid(3))


def test_http_timeout_gets_one_actual_transport_attempt():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive SDK error", request=request)
    fixture = BoundaryFixture()
    fixture.service._factory = lambda **kwargs: create_chat_transport(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ContractValidationError) as caught:
        fixture.chat()
    assert caught.value.reason_code == "model_dispatch_unknown"
    assert len(calls) == 1
    fixture.service.release_run(uid(2), uid(3))


@pytest.mark.parametrize("status", [400, 429, 503])
def test_http_rejection_records_status_without_retry_or_raw_provider_error(status):
    observed = []
    def handler(request):
        observed.append(request)
        return httpx.Response(status, json={"error": {"message": "private-fixture-secret"}})
    fixture = BoundaryFixture()
    fixture.service._factory = lambda **kwargs: create_chat_transport(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    for _ in range(2):
        with pytest.raises(ContractValidationError) as caught:
            fixture.chat()
        assert caught.value.reason_code == "model_provider_error"
    assert len(observed) == 1
    assert fixture.facts[-1]["details"]["classification"] == "provider_error"
    assert fixture.facts[-1]["details"]["status_code"] == status
    assert "private-fixture-secret" not in json.dumps(fixture.facts)
    fixture.service.close()


def test_malformed_http_response_records_confirmed_protocol_failure():
    fixture = BoundaryFixture()
    fixture.service._factory = lambda **kwargs: create_chat_transport(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": []}))))
    with pytest.raises(ContractValidationError) as caught:
        fixture.chat()
    assert caught.value.reason_code == "model_response_invalid"
    assert fixture.facts[-1]["details"]["classification"] == "response_invalid"
    fixture.service.close()


def test_model_result_rejects_extra_fields_and_binding_boolean_lookalikes():
    fixture = BoundaryFixture()
    result = fixture.chat()
    with pytest.raises(ContractValidationError):
        validate_public_model_result({**result, "raw_headers": {"Authorization": "secret"}})
    forged = deepcopy(fixture.binding)
    forged["capabilities"]["tools"] = 1
    with pytest.raises(ContractValidationError):
        validate_public_model_binding(forged)


def test_two_consumers_share_frame_but_distinct_invocations_and_response_cache():
    fixture = BoundaryFixture()
    first = fixture.chat()
    fixture.consumer.node_run_id = uid(22)
    second = fixture.chat()
    assert first["request_id"] != second["request_id"]
    assert fixture.calls == 2
    assert fixture.chat() == second
    assert fixture.calls == 2
    assert [fact["sequence"] for fact in fixture.facts] == [1, 2, 3, 1, 2, 3]


def test_no_agent_graph_assembles_prompt_calls_model_and_accepts_result():
    fixture = BoundaryFixture()
    registry = CapabilityPackageLoader((
        create_content_package(), create_prompt_package(), create_model_package(),
    )).load({"workflow.prompts": "1.0.0", "workflow.models": "1.0.0"}).registry
    def node(component, number, config=None):
        return {"node_binding_id": uid(number), "component_id": component, "component_version": "1",
                "title": component, "position": {"x": 0, "y": 0},
                "config": deepcopy(config if config is not None
                                   else registry.get(component, "1").definition.default_config)}
    source = node("models.source", 10, fixture.config)
    prompt = node("prompts.assembly", 50)
    chat = node("models.chat", 20)
    # A fixed prompt group supplies effective material for the ordinary assembly.
    group_config = deepcopy(registry.get("prompts.group", "1").definition.default_config)
    group_config["members"][0]["text"] = "Say hello"
    group = node("prompts.group", 51, group_config)
    def edge(number, producer, consumer, port):
        return {"edge_id": uid(number), "source_node_id": producer["node_binding_id"],
                "source_port_id": "output", "target_node_id": consumer["node_binding_id"],
                "target_port_id": port, "order": 0}
    doc = {"schema_version": 1, "workflow_definition_id": uid(60), "revision": 1,
           "name": "Public model graph", "nodes": [source, group, prompt, chat],
           "edges": [edge(61, group, prompt, "input"), edge(62, source, chat, "model"),
                     edge(63, prompt, chat, "prompt")]}
    plan = GraphCompiler(registry).compile(doc)
    accepted = {}
    output_index = [100]
    def on_node(event):
        if event["event"] != "succeeded":
            return
        refs = {}
        for port, value in event["outputs"].items():
            output_index[0] += 1
            output_id = uid(output_index[0])
            refs[port] = output_id
            accepted[output_id] = deepcopy(value)
            if value.get("kind") == "workflow.model-binding":
                fixture.service.accept_binding_output(
                    workflow_session_id=uid(2), chain_run_id=uid(3),
                    node_run_id=event["node_run_id"], binding_id=value["binding_id"],
                    output_id=output_id)
        event["output_refs"] = refs
    # Recreate frame to use the graph's precise producer invocation.
    fixture.service.release_run(uid(2), uid(3))
    fixture.service.prepare_run(workflow_session_id=uid(2), chain_run_id=uid(3),
                               node_configs={uid(10): fixture.config}, records=[fixture.record])
    fixture.service._resolve_input = lambda context, port: accepted[
        context.input_artifact_refs(port)[0]["output_id"]]
    result = execute_graph(plan, registry, workflow_session_id=uid(2), chain_run_id=uid(3),
                           node_run_ids={identity: uid(200 + index)
                                         for index, identity in enumerate(plan.ordered_node_ids)},
                           state={"revision": 0, "values": {}},
                           host=fixture.service, on_node=on_node)
    assert result.status == "succeeded", result.diagnostic
    assert result.outputs[uid(20)]["output"]["content"] == "World"
    assert fixture.calls == 1
