"""Prepared request dispatch keeps ordinary public execution import-independent."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.prepared_request import PREPARATION_CAPABILITY, check_prepared_request_capacity
from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent.tools import final_answer_tool, register_callable


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def tools():
    return (
        register_callable(
            "inspect_text", "Inspect text", {
                "type": "object", "properties": {
                    "text": {"type": "string", "description": "Text to inspect"},
                },
                "required": ["text"], "additionalProperties": False,
            }, lambda text: {"text": text},
        ),
        final_answer_tool(),
    )


def ordinary_snapshot():
    snapshot = json.loads(EXAMPLE.read_text(encoding="utf-8"))["input_snapshot"][0]
    registered = tools()
    snapshot["tool_definitions"] = [
        {
            "name": tool.name, "version": "1",
            "description": tool.definition["function"]["description"],
            "parameters_schema": deepcopy(tool.schema),
        }
        for tool in registered
    ]
    return snapshot, registered


def public_snapshot():
    from phase1_agent.agent_executor import make_public_agent_snapshot
    from phase1_agent.content_contracts import text_content
    from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE, read_context_view
    from phase1_agent.context_prompt import assemble_context_prompt
    from phase1_agent.host_sdk import ObjectBinding

    registered = tools()
    context = SimpleNamespace(
        workflow_session_id=uid(1), node_binding_id=uid(3),
        input_artifact_refs=lambda port: [{"output_id": uid(81 if port == "prompt" else 83)}],
    )
    reference = lambda number: {"scope": "artifact", "output_id": uid(number)}
    record = {
        "revision_id": uid(50), "revision": 1, "type_id": EFFECTIVE_CONTEXT_TYPE,
        "schema_version": 1, "deleted": False,
        "binding": ObjectBinding(
            "context", EFFECTIVE_CONTEXT_TYPE, 1, "shared",
            readers=(uid(3),), writers=(uid(3),),
        ).to_dict(),
        "value": {"view_ref": None, "accepted_delta_ids": []},
    }
    prompt = assemble_context_prompt(
        [], read_context_view(record, uid(1), "context"), text_content("question"),
        context_ref=reference(60), current_input_ref=reference(61),
    )
    binding = {
        "schema_version": 1, "kind": "workflow.model-binding", "binding_id": uid(70),
        "reference": {
            "envelope_version": 1, "scope": "workspace",
            "type_id": "workflow.chat-provider", "resource_id": uid(71),
        },
        "parameters": {
            "model": "fixture", "max_tokens": 32, "temperature": 0.5,
            "thinking": "disabled", "stream": False,
        },
        "capabilities": {
            "protocol": "chat", "tools": True, "stream": False, "thinking": "disabled",
        },
    }
    return make_public_agent_snapshot(context, {}, prompt, binding, registered), registered


def response(name, arguments, identity):
    return ModelResponse("tool_calls", tool_calls=(
        ModelToolCall(identity, name, json.dumps(arguments)),
    ))


class ScriptedAdapter:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []
        self.closed = False

    def generate(self, messages, wire_tools):
        self.calls.append((deepcopy(messages), deepcopy(wire_tools)))
        return self.steps.pop(0)

    def close(self):
        self.closed = True


def prepared_snapshot(*, max_messages=4096, max_total_chars=4_000_000):
    from phase1_agent.prompt_preparation import make_prompt_context_config, prepare_prompt_context

    snapshot, registered = ordinary_snapshot()
    node_input = json.loads(EXAMPLE.read_text(encoding="utf-8"))["node_input"][0]
    config = make_prompt_context_config({
        "schema_version": 1, "kind": "prompt_collection", "items": [],
    })
    config["limits"]["assembly"].update(
        max_messages=max_messages, max_total_chars=max_total_chars,
    )
    evidence = prepare_prompt_context(
        node_input, [], workflow_session_id=snapshot["workflow_session_id"],
        node_binding_id=snapshot["node_binding_id"], parent_turn_id=snapshot["parent_turn_id"],
        logical_floors=[], protected_blocks=[], config=config, root_message_id=uid(600),
    )
    snapshot["projection_version"] = 2
    snapshot["s0"] = deepcopy(evidence["s0"])
    snapshot["config"]["payload"] = {
        "resolved": {"context": {"capabilities": [PREPARATION_CAPABILITY]}},
        "context": deepcopy(config), "context_preparation": evidence,
    }
    return snapshot, registered


@pytest.mark.parametrize("family", ["ordinary", "public", "resolved"])
def test_fresh_process_dispatches_each_public_request_with_old_preparation_imports_blocked(family):
    program = r"""
import importlib.abc
import sys
from copy import deepcopy

blocked = (
    "phase1_agent.prepared_context", "phase1_agent.prompt_preparation",
    "phase1_agent.variable_preparation", "phase1_agent.preparation_program",
    "phase1_agent.workflow", "phase1_agent.workflow_host",
    "phase1_agent.graph_agent_host", "phase1_agent.graph_agent_runtime",
    "phase1_agent.kernel",
)
class NoOldPreparation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Ordinary public request imported old execution: " + fullname)
sys.meta_path.insert(0, NoOldPreparation())

from test_prepared_request_gate import ordinary_snapshot, public_snapshot, response, ScriptedAdapter
from phase1_agent import prepared_request
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.runtime import KernelContractError, SnapshotKernel

family = sys.argv[1]
snapshot, tools = ordinary_snapshot() if family == "ordinary" else public_snapshot()
if family == "resolved":
    snapshot["config"]["payload"]["resolved"] = {"context": {"capabilities": ["sources"]}}
original = deepcopy(snapshot)
validations = []
validate_record = prepared_request.validate_record
def checked(kind, value):
    validations.append(deepcopy(value))
    return validate_record(kind, value)
prepared_request.validate_record = checked

adapter = ScriptedAdapter(
    response("inspect_text", {"text": "checked"}, "provider.inspect"),
    response("final_answer", {"answer": {"text": "complete"}}, "provider.final"),
)
result = SnapshotKernel().run(snapshot, tools, adapter)
assert result.final["value"] == {"text": "complete"}
assert (result.model_requests, result.attempts) == (2, 2)
assert len(adapter.calls) == len(validations) == 2
assert validations == [original, original]
assert snapshot == original

for evidence in ({}, [], False, "untrusted", {"s0": []}):
    forged = deepcopy(snapshot)
    forged["config"]["payload"]["context_preparation"] = evidence
    never = ScriptedAdapter()
    try:
        SnapshotKernel().run(forged, tools, never)
    except KernelContractError as error:
        assert error.code == "message_contract_error"
        assert isinstance(error.__cause__, ContractValidationError)
        assert "Legacy Context cannot carry" in str(error.__cause__)
        assert (error.model_requests, error.attempts) == (0, 0)
    else:
        raise AssertionError("Non-prepared evidence reached dispatch")
    assert not never.calls

for defect in ("identity", "secret", "schema", "extra"):
    damaged = deepcopy(snapshot)
    if defect == "identity":
        damaged["snapshot_id"] = "not-a-uuid"
    elif defect == "secret":
        damaged["config"]["payload"]["api_key"] = "forbidden"
    elif defect == "schema":
        damaged["output_schema"] = {"$ref": "https://example.test/schema"}
    else:
        damaged["unexpected"] = True
    try:
        prepared_request.check_prepared_request_capacity(damaged)
    except ContractValidationError:
        pass
    else:
        raise AssertionError("Snapshot validation was skipped")

assert not any(
    name == entry or name.startswith(entry + ".")
    for name in sys.modules for entry in blocked
)
print("public request gate and zero-dispatch rejection stay import-independent")
"""
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    roots = [str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parent)]
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [*roots, environment.get("PYTHONPATH", "")]))
    result = subprocess.run(
        [sys.executable, "-c", program, family], text=True, capture_output=True,
        env=environment, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == (
        "public request gate and zero-dispatch rejection stay import-independent"
    )


@pytest.mark.parametrize("resolved", [
    None, [], {"context": None}, {"context": []},
    {"context": {"capabilities": PREPARATION_CAPABILITY}},
    {"context": {"capabilities": [None]}},
])
def test_malformed_descriptor_is_rejected_instead_of_skipping_the_gate(resolved):
    snapshot, _ = ordinary_snapshot()
    snapshot["config"]["payload"]["resolved"] = resolved
    with pytest.raises(ContractValidationError, match="Resolved"):
        check_prepared_request_capacity(snapshot)


def test_old_compatibility_export_and_prepared_adapter_keep_the_same_gate():
    from phase1_agent.prepared_context import (
        PREPARATION_CAPABILITY as legacy_capability,
        check_prepared_request_capacity as legacy_gate,
    )
    from phase1_agent.runtime import check_prepared_request_capacity as runtime_gate

    assert legacy_gate is runtime_gate is check_prepared_request_capacity
    assert legacy_capability == PREPARATION_CAPABILITY


@pytest.mark.parametrize("public_flag", [False, True])
def test_prepared_descriptor_always_delegates_to_complete_frozen_validation(monkeypatch, public_flag):
    from phase1_agent import prepared_context, prompt_preparation
    from phase1_agent.runtime import SnapshotKernel

    snapshot, registered = prepared_snapshot()
    if public_flag:
        snapshot["config"]["payload"]["public_agent"] = {}
    frozen = deepcopy(snapshot)
    observed = []
    validate_frozen = prepared_context._validate_validated_frozen_preparation

    def validate(value):
        observed.append(deepcopy(value))
        return validate_frozen(value)

    def never_process(*args, **kwargs):
        raise AssertionError("Frozen request validation must not execute preparation again")

    monkeypatch.setattr(prepared_context, "_validate_validated_frozen_preparation", validate)
    monkeypatch.setattr(prompt_preparation, "apply_context_regex", never_process)
    monkeypatch.setattr(prompt_preparation, "process_prompt_collection", never_process)
    adapter = ScriptedAdapter(response("final_answer", {"answer": {"text": "prepared"}}, "final"))
    result = SnapshotKernel().run(snapshot, registered, adapter)
    assert result.final["value"] == {"text": "prepared"}
    assert observed == [frozen]
    assert snapshot == frozen
    assert adapter.calls[0][0] == frozen["s0"]


@pytest.mark.parametrize("public_flag", [False, True])
def test_prepared_gate_validates_each_snapshot_once_without_caching(monkeypatch, public_flag):
    from phase1_agent import prepared_context, prepared_request

    snapshot, _ = prepared_snapshot()
    if public_flag:
        snapshot["config"]["payload"]["public_agent"] = {}
    original = deepcopy(snapshot)
    structural, frozen = [], []
    validate_record = prepared_request.validate_record
    validate_frozen = prepared_context._validate_validated_frozen_preparation

    def validate(kind, value):
        if kind == "input_snapshot":
            structural.append(deepcopy(value))
        return validate_record(kind, value)

    def validate_preparation(value):
        assert value is not snapshot
        frozen.append(deepcopy(value))
        return validate_frozen(value)

    monkeypatch.setattr(prepared_request, "validate_record", validate)
    monkeypatch.setattr(prepared_context, "validate_record", validate)
    monkeypatch.setattr(prepared_context, "_validate_validated_frozen_preparation", validate_preparation)
    for count in range(1, 3):
        check_prepared_request_capacity(snapshot)
        assert len(structural) == len(frozen) == count
        assert structural[-1] == frozen[-1] == original
        assert snapshot == original
    snapshot["snapshot_id"] = "not-a-uuid"
    with pytest.raises(ContractValidationError):
        check_prepared_request_capacity(snapshot)
    assert len(structural) == 3
    assert len(frozen) == 2


@pytest.mark.parametrize("family", ["ordinary", "prepared"])
def test_public_frozen_validator_always_validates_snapshot_before_its_core(monkeypatch, family):
    from phase1_agent import prepared_context

    snapshot, _ = ordinary_snapshot() if family == "ordinary" else prepared_snapshot()
    original = deepcopy(snapshot)
    structural, frozen = [], []
    validate_record = prepared_context.validate_record
    validate_frozen = prepared_context._validate_validated_frozen_preparation

    def validate(kind, value):
        if kind == "input_snapshot":
            structural.append(deepcopy(value))
        return validate_record(kind, value)

    def validate_preparation(value):
        assert value is not snapshot
        frozen.append(deepcopy(value))
        return validate_frozen(value)

    monkeypatch.setattr(prepared_context, "validate_record", validate)
    monkeypatch.setattr(prepared_context, "_validate_validated_frozen_preparation", validate_preparation)
    for count in range(1, 3):
        result = prepared_context.validate_frozen_preparation(snapshot)
        assert (result is None) == (family == "ordinary")
        assert len(structural) == len(frozen) == count
        assert structural[-1] == frozen[-1] == original
        assert snapshot == original
    for count, defect in enumerate(("identity", "secret", "schema", "extra"), start=3):
        damaged = deepcopy(snapshot)
        if defect == "identity":
            damaged["snapshot_id"] = "not-a-uuid"
        elif defect == "secret":
            damaged["config"]["payload"]["api_key"] = "forbidden"
        elif defect == "schema":
            damaged["output_schema"] = {"$ref": "https://example.test/schema"}
        else:
            damaged["unexpected"] = True
        with pytest.raises(ContractValidationError):
            prepared_context.validate_frozen_preparation(damaged)
        assert len(structural) == count
        assert len(frozen) == 2


@pytest.mark.parametrize("defect", [
    "missing", "empty", "projection", "s0", "config", "parent", "binding",
    "variable_evidence", "program_variable_evidence", "continuation",
])
def test_prepared_frozen_evidence_failures_reach_the_original_validator(defect):
    snapshot, _ = prepared_snapshot()
    payload = snapshot["config"]["payload"]
    if defect == "missing":
        payload.pop("context_preparation")
    elif defect == "empty":
        payload["context_preparation"] = {}
    elif defect == "projection":
        snapshot["projection_version"] = 1
    elif defect == "s0":
        snapshot["s0"][0]["blocks"][0]["text"] = "changed"
    elif defect == "config":
        payload["context"]["limits"]["assembly"]["max_messages"] -= 1
    elif defect == "parent":
        snapshot["parent_turn_id"] = uid(601)
    elif defect == "binding":
        snapshot["node_binding_id"] = uid(602)
    elif defect == "variable_evidence":
        payload["variable_preparation"] = {}
    elif defect == "program_variable_evidence":
        payload["program_variable_preparation"] = {}
    else:
        snapshot["s0"].append({
            "schema_version": 1, "message_id": uid(603), "role": "assistant",
            "source": {"kind": "model", "request_id": uid(604)},
            "blocks": [{"kind": "text", "text": "not a saved continuation observation"}],
        })
    with pytest.raises(ContractValidationError):
        check_prepared_request_capacity(snapshot)


def test_prepared_capacity_preserves_exact_complete_character_boundary_and_message_limit():
    from phase1_agent.prompt_assembly import message_content_chars

    initial, _ = prepared_snapshot()
    size = message_content_chars(initial["s0"]) + len(
        canonical_bytes(initial["tool_definitions"]).decode("utf-8"),
    )
    snapshot, _ = prepared_snapshot(max_messages=len(initial["s0"]), max_total_chars=size)
    check_prepared_request_capacity(snapshot)
    overflow, _ = prepared_snapshot(max_total_chars=size - 1)
    with pytest.raises(PromptProcessingError) as error:
        check_prepared_request_capacity(overflow)
    assert error.value.code == "prompt_capacity_exceeded"
    later = snapshot["s0"] + [{
        "schema_version": 1, "message_id": uid(610), "role": "assistant",
        "source": {"kind": "model", "request_id": uid(611)},
        "blocks": [{"kind": "text", "text": "accepted delta"}],
    }]
    with pytest.raises(PromptProcessingError) as error:
        check_prepared_request_capacity(snapshot, messages=later)
    assert error.value.code == "prompt_capacity_exceeded"


def test_prepared_capacity_counts_both_tool_payload_projections_and_frozen_definitions():
    from phase1_agent.prompt_assembly import message_content_chars

    snapshot, _ = prepared_snapshot()
    delta = json.loads(EXAMPLE.read_text(encoding="utf-8"))["turn"][0]["messages"][:2]
    messages = snapshot["s0"] + delta
    call, result = (message["blocks"][0] for message in delta)
    delta_size = (
        len(call["tool_name"]) + len(call["tool_definition_version"])
        + len(call["raw_arguments"]) + len(canonical_bytes(call["parsed_arguments"]).decode("utf-8"))
        + len(result["model_visible_text"]) + len(canonical_bytes(result["content"]).decode("utf-8"))
    )
    assert message_content_chars(messages) == message_content_chars(snapshot["s0"]) + delta_size
    size = message_content_chars(messages) + len(
        canonical_bytes(snapshot["tool_definitions"]).decode("utf-8"),
    )
    exact, _ = prepared_snapshot(max_total_chars=size)
    check_prepared_request_capacity(exact, messages=messages)
    overflow, _ = prepared_snapshot(max_total_chars=size - 1)
    with pytest.raises(PromptProcessingError) as error:
        check_prepared_request_capacity(overflow, messages=messages)
    assert error.value.code == "prompt_capacity_exceeded"


def test_prepared_adapter_keeps_exact_tool_guard_capacity_and_close_forwarding():
    from phase1_agent.prepared_context import PreparedRequestAdapter

    snapshot, registered = prepared_snapshot(max_total_chars=2000)
    adapter = ScriptedAdapter("response")
    guard = PreparedRequestAdapter(adapter, snapshot)
    wire_tools = [deepcopy(tool.definition) for tool in registered]
    assert guard.generate(snapshot["s0"], wire_tools) == "response"
    changed = deepcopy(wire_tools)
    changed[0]["function"]["description"] = "different tools"
    with pytest.raises(ContractValidationError, match="tools differ"):
        guard.generate(snapshot["s0"], changed)
    oversized = deepcopy(snapshot["s0"])
    oversized[0]["blocks"][0]["text"] = "x" * 2001
    with pytest.raises(PromptProcessingError) as error:
        guard.generate(oversized, wire_tools)
    assert error.value.code == "prompt_capacity_exceeded"
    assert len(adapter.calls) == 1
    guard.close()
    assert adapter.closed
