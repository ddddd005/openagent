"""The v2 binding boundary never dispatches a model or a tool."""

import copy
import json
from pathlib import Path
from uuid import UUID

import pytest

from phase1_agent.bindings import BasicContext, ComponentRegistry, build_snapshot
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import dumps_pretty
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.tools import register_callable


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


COMPONENT = uid(1)
SESSION = uid(4)
BINDING = uid(3)
PROMPT = uid(77)
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"


def node_input(*, source=None, payload=None):
    return {
        "schema_version": 1,
        "input_id": uid(5),
        "port_id": "request",
        "payload_schema_ref": {"schema_id": "writing_text", "version": 1},
        "source": source or {"kind": "visible_message", "visible_message_id": uid(14)},
        "payload": {"nested": {"text": "hello", "count": 2}} if payload is None else payload,
    }


def definition():
    return {
        "schema_version": 1,
        "component_id": COMPONENT,
        "component_version": "1.0.0",
        "kind": "agent",
        "capabilities": ["json_final"],
        "ports": [
            {"port_id": "request", "direction": "input",
             "schema_ref": {"schema_id": "writing_text", "version": 1}, "required": True},
        ],
    }


def envelope(payload=None):
    return {
        "owner_component_id": COMPONENT,
        "schema_version": 1,
        "payload": {"prompt": {"revision": 2}} if payload is None else payload,
    }


def test_registry_checks_exact_version_capabilities_and_private_schema_without_execution():
    calls = []

    def implementation():
        calls.append("executed")

    registry = ComponentRegistry()
    schema = {
        "type": "object", "properties": {"revision": {"type": "integer", "minimum": 1}},
        "required": ["revision"], "additionalProperties": False,
    }
    registry.register("context", COMPONENT, "1.2", implementation,
                      capabilities=frozenset({"source", "prompt_revision"}), config_schema=schema)
    schema["properties"]["revision"]["minimum"] = 99
    resolved = registry.resolve("context", COMPONENT, "1.2",
                                required_capabilities=("source",), config=envelope({"revision": 2}))
    assert resolved.implementation is implementation
    assert resolved.descriptor == {
        "kind": "context", "component_id": COMPONENT, "component_version": "1.2",
        "contract_version": 1, "capabilities": ["prompt_revision", "source"],
    }
    json.dumps(resolved.descriptor)
    resolved.descriptor["capabilities"].clear()
    assert registry.resolve("context", COMPONENT, "1.2",
                            config=envelope({"revision": 2})).descriptor["capabilities"]

    bad_configs = [
        envelope({"revision": 0}),
        envelope({"revision": 2, "token": "not allowed by this schema"}),
        {**envelope({"revision": 2}), "owner_component_id": uid(33)},
        {**envelope({"revision": 2}), "schema_version": 2},
        {**envelope({"revision": 2}), "extra": True},
    ]
    for config in bad_configs:
        with pytest.raises(ContractValidationError):
            registry.resolve("context", COMPONENT, "1.2", config=config)
    with pytest.raises(ContractValidationError):
        registry.resolve("context", COMPONENT, "1.3", config=envelope({"revision": 2}))
    with pytest.raises(ContractValidationError, match="Missing component capabilities"):
        registry.resolve("context", COMPONENT, "1.2",
                         required_capabilities=("missing",), config=envelope({"revision": 2}))
    with pytest.raises(ContractValidationError, match="Duplicate"):
        registry.register("context", COMPONENT, "1.2", implementation,
                          capabilities=frozenset(), config_schema={})
    with pytest.raises(ContractValidationError, match="local references"):
        registry.register("kernel", COMPONENT, "1", implementation,
                          capabilities=frozenset(),
                          config_schema={"$ref": "https://example.invalid/config.json"})
    assert calls == []


def test_registry_contract_version_and_detached_registration_are_explicit():
    implementation = object()
    registry = ComponentRegistry()
    with pytest.raises(ContractValidationError, match="contract version"):
        registry.register("context", COMPONENT, "1", implementation,
                          capabilities=frozenset(), config_schema={}, contract_version=2)
    registry.register("context", COMPONENT, "1", implementation,
                      capabilities=frozenset(), config_schema={})
    frozen = registry.detached()
    registry.register("context", COMPONENT, "2", object(),
                      capabilities=frozenset(), config_schema={})
    assert frozen.resolve("context", COMPONENT, "1", config=envelope({})).implementation is implementation
    with pytest.raises(ContractValidationError, match="not registered"):
        frozen.resolve("context", COMPONENT, "2", config=envelope({}))


def test_context_preserves_structured_input_source_and_detaches_history():
    context = BasicContext()
    original = node_input()
    history = [{
        "schema_version": 1, "message_id": uid(101), "role": "assistant",
        "source": {"kind": "model", "request_id": uid(102)},
        "blocks": [{"kind": "text", "text": '{"answer":"old"}'}],
    }]
    prepared = context.prepare(original, history, prompt_id=PROMPT,
                               prompt_revision=2, system_prompt="System")
    assert [item["role"] for item in prepared] == ["system", "assistant", "user"]
    assert prepared[0]["source"] == {"kind": "prompt", "prompt_id": PROMPT, "revision": 2}
    assert prepared[-1]["source"] == {"kind": "human", "visible_message_id": uid(14)}
    assert prepared[-1]["blocks"] == [{"kind": "text", "text": dumps_pretty(original["payload"])}]
    assert UUID(prepared[0]["message_id"]).version == 4
    assert UUID(prepared[-1]["message_id"]).version == 4
    assert prepared[0]["message_id"] != prepared[-1]["message_id"]
    for item in prepared:
        validate_record("agent_message", item)
    history[0]["blocks"][0]["text"] = "changed"
    original["payload"]["nested"]["text"] = "changed"
    assert prepared[1]["blocks"][0]["text"] == '{"answer":"old"}'
    assert "changed" not in prepared[-1]["blocks"][0]["text"]
    second = context.prepare(
        node_input(source={"kind": "upstream_output", "output_id": uid(13)}, payload=[{"a": 1}]),
        [], prompt_id=PROMPT, prompt_revision=1, system_prompt="Other",
    )
    assert second[-1]["source"] == {"kind": "upstream_node", "output_id": uid(13)}
    assert second[-1]["blocks"][0]["text"] == dumps_pretty([{"a": 1}])
    assert second[0]["blocks"][0]["text"] == "Other"


def test_context_rejects_invalid_history_and_external_source_without_dispatch():
    context = BasicContext()
    external = node_input(source={"kind": "external", "source_ref": "import"})
    with pytest.raises(ContractValidationError):
        context.prepare(external, [], prompt_id=PROMPT, prompt_revision=1, system_prompt="s")
    with pytest.raises(ContractValidationError):
        context.prepare(node_input(), [{"role": "assistant", "content": "legacy"}],
                        prompt_id=PROMPT, prompt_revision=1, system_prompt="s")
    with pytest.raises(ContractValidationError):
        context.prepare(node_input(), [], prompt_id=PROMPT, prompt_revision=True, system_prompt="s")


def test_snapshot_freezes_registered_tool_and_all_inputs():
    called = []

    def echo(text):
        called.append(text)
        return text

    schema = {
        "type": "object", "properties": {
            "text": {"type": "string", "description": "Text"},
        }, "required": ["text"], "additionalProperties": False,
    }
    tool = register_callable("echo", "Echo text", schema, echo)
    original = node_input()
    s0 = BasicContext().prepare(original, [], prompt_id=PROMPT,
                                prompt_revision=2, system_prompt="System")
    config = envelope({"resolved": {
        "context": {"kind": "context", "component_id": COMPONENT,
                    "component_version": "1", "contract_version": 1, "capabilities": ["source"]},
        "kernel": {"kind": "kernel"}, "adapter": {"kind": "adapter"},
    }})
    model = {"model": "explicit-model", "max_tokens": 100, "temperature": 0.5,
             "thinking": "disabled", "stream": False}
    output = {"type": "object", "properties": {"answer": {"type": "string"}}}
    snapshot = build_snapshot(
        workflow_session_id=SESSION, node_binding_id=BINDING,
        node_input=original, parent_turn_id=None, node_definition=definition(),
        config=config, s0=s0, tools=(tool,), model_parameters=model, output_schema=output,
    )
    assert validate_record("input_snapshot", snapshot) == snapshot
    assert UUID(snapshot["snapshot_id"]).version == 4
    assert snapshot["projection_version"] == 1
    assert snapshot["component_version"] == "1.0.0"
    assert snapshot["tool_definitions"] == [{
        "name": "echo", "version": "1", "description": "Echo text", "parameters_schema": schema,
    }]
    frozen = copy.deepcopy(snapshot)
    config["payload"]["resolved"]["context"]["capabilities"].append("mutated")
    s0[-1]["blocks"][0]["text"] = "mutated"
    original["payload"]["nested"]["text"] = "mutated"
    model["model"] = "mutated"
    output["properties"]["answer"]["type"] = "integer"
    schema["properties"]["text"]["description"] = "mutated"
    assert snapshot == frozen
    assert called == []


@pytest.mark.parametrize("change", [
    {"model": ""},
    {"model": "m", "max_tokens": 0},
    {"model": "m", "max_tokens": True},
    {"model": "m", "temperature": 2.1},
    {"model": "m", "temperature": True},
    {"model": "m", "thinking": "enabled"},
    {"model": "m", "stream": True},
    {"model": "m", "api_key": "secret"},
    {"model": "m", "base_url": "https://example.test"},
    {"max_tokens": 100},
])
def test_snapshot_rejects_unsupported_model_parameters(change):
    with pytest.raises(ContractValidationError):
        build_snapshot(
            workflow_session_id=SESSION, node_binding_id=BINDING, node_input=node_input(),
            parent_turn_id=None, node_definition=definition(), config=envelope(),
            s0=BasicContext().prepare(node_input(), [], prompt_id=PROMPT,
                                      prompt_revision=1, system_prompt="s"),
            tools=[], model_parameters=change, output_schema={"type": "object"},
        )


def test_snapshot_rejects_owner_nonagent_and_live_tool():
    common = dict(
        workflow_session_id=SESSION, node_binding_id=BINDING, node_input=node_input(),
        parent_turn_id=None,
        s0=BasicContext().prepare(node_input(), [], prompt_id=PROMPT,
                                  prompt_revision=1, system_prompt="s"),
        model_parameters={"model": "explicit"}, output_schema={"type": "object"},
    )
    with pytest.raises(ContractValidationError, match="owner"):
        build_snapshot(**common, node_definition=definition(),
                       config={**envelope(), "owner_component_id": uid(31)}, tools=[])
    with pytest.raises(ContractValidationError, match="Agent"):
        build_snapshot(**common, node_definition={**definition(), "kind": "io"},
                       config=envelope(), tools=[])
    with pytest.raises(ContractValidationError, match="RegisteredTool"):
        build_snapshot(**common, node_definition=definition(),
                       config=envelope(), tools=[lambda: None])


def test_project_turn_selects_last_matching_source_and_payload_not_last_s0():
    bundle = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    snapshot = bundle["input_snapshot"][0]
    turn = bundle["turn"][0]
    original = bundle["node_input"][0]
    snapshot["s0"][0]["blocks"][0]["text"] = dumps_pretty(original["payload"])
    matching = copy.deepcopy(snapshot["s0"][0])
    matching["message_id"] = uid(201)
    unrelated = copy.deepcopy(matching)
    unrelated["message_id"] = uid(202)
    unrelated["blocks"][0]["text"] = dumps_pretty({"different": True})
    snapshot["s0"] = [snapshot["s0"][0], matching, unrelated]
    projected = BasicContext().project_turn(turn, original, snapshot)
    assert projected[0]["message_id"] == matching["message_id"]
    assert projected[1:] == turn["messages"]
    prepared = BasicContext().prepare(original, [turn], prompt_id=PROMPT,
                                      prompt_revision=1, system_prompt="System")
    assert prepared[1:-1] == turn["messages"]
    snapshot["s0"][1]["blocks"][0]["text"] = "mutated"
    assert projected[0]["blocks"][0]["text"] == dumps_pretty(original["payload"])
    bad_input = copy.deepcopy(original)
    bad_input["payload"] = {"changed": True}
    with pytest.raises(ContractValidationError, match="matching input"):
        BasicContext().project_turn(turn, bad_input, snapshot)
    bad_turn = copy.deepcopy(turn)
    bad_turn["snapshot_id"] = uid(222)
    with pytest.raises(ContractValidationError, match="identities"):
        BasicContext().project_turn(bad_turn, original, snapshot)
    snapshot["s0"][0]["blocks"].append({"kind": "text", "text": "unrelated"})
    snapshot["s0"][1]["blocks"].append({"kind": "text", "text": "unrelated"})
    with pytest.raises(ContractValidationError, match="matching input"):
        BasicContext().project_turn(turn, original, snapshot)
