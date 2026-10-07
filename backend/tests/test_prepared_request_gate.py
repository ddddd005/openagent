"""Current request gates validate snapshots and reject retired prepared execution."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.prepared_request import PREPARATION_CAPABILITY, check_prepared_request_capacity
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


def family_snapshot(family):
    snapshot, registered = ordinary_snapshot() if family == "ordinary" else public_snapshot()
    if family == "resolved":
        snapshot["config"]["payload"]["resolved"] = {"context": {"capabilities": ["sources"]}}
    return snapshot, registered


def response(name, arguments, identity):
    return ModelResponse("tool_calls", tool_calls=(
        ModelToolCall(identity, name, json.dumps(arguments)),
    ))


class ScriptedAdapter:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def generate(self, messages, wire_tools):
        self.calls.append((deepcopy(messages), deepcopy(wire_tools)))
        return self.steps.pop(0)


@pytest.mark.parametrize("family", ["ordinary", "public", "resolved"])
def test_fresh_process_dispatches_current_requests_without_retired_preparation(family):
    program = r"""
import importlib.abc
import sys
from copy import deepcopy

blocked = {
    "phase1_agent.prepared_context", "phase1_agent.workflow",
    "phase1_agent.graph_agent_host", "phase1_agent.graph_agent_runtime",
    "phase1_agent.kernel",
}
class NoRetiredExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError("Current request imported retired execution: " + fullname)
        return None
sys.meta_path.insert(0, NoRetiredExecution())

from test_prepared_request_gate import family_snapshot, response, ScriptedAdapter
from phase1_agent import prepared_request
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.runtime import KernelContractError, SnapshotKernel

snapshot, tools = family_snapshot(sys.argv[1])
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

retired = deepcopy(snapshot)
retired["config"]["payload"]["resolved"] = {
    "context": {"capabilities": [prepared_request.PREPARATION_CAPABILITY]},
}
never = ScriptedAdapter()
try:
    SnapshotKernel().run(retired, tools, never)
except KernelContractError as error:
    assert error.code == "message_contract_error"
    assert isinstance(error.__cause__, ContractValidationError)
    assert "no longer supported" in str(error.__cause__)
    assert (error.model_requests, error.attempts) == (0, 0)
else:
    raise AssertionError("Retired preparation reached model dispatch")
assert not never.calls
assert not blocked.intersection(sys.modules)
print("current request gate remains import-independent")
"""
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    roots = [str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parent)]
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [*roots, environment.get("PYTHONPATH", "")]))
    result = subprocess.run(
        [sys.executable, "-c", program, family], text=True, capture_output=True,
        env=environment, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "current request gate remains import-independent"


@pytest.mark.parametrize("family", ["ordinary", "public", "resolved"])
def test_current_gate_validates_each_snapshot_once_without_caching(monkeypatch, family):
    from phase1_agent import prepared_request

    snapshot, _ = family_snapshot(family)
    original, observed = deepcopy(snapshot), []
    validate_record = prepared_request.validate_record

    def checked(kind, value):
        observed.append(deepcopy(value))
        return validate_record(kind, value)

    monkeypatch.setattr(prepared_request, "validate_record", checked)
    for count in range(1, 3):
        check_prepared_request_capacity(snapshot)
        assert len(observed) == count and observed[-1] == original
        assert snapshot == original
    snapshot["snapshot_id"] = "not-a-uuid"
    with pytest.raises(ContractValidationError):
        check_prepared_request_capacity(snapshot)
    assert len(observed) == 3


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


@pytest.mark.parametrize("family", ["ordinary", "public", "resolved"])
@pytest.mark.parametrize("evidence", [None, {}, [], False, "untrusted"])
def test_retired_prepared_capability_never_dispatches(family, evidence):
    from phase1_agent.runtime import KernelContractError, SnapshotKernel

    snapshot, registered = family_snapshot(family)
    snapshot["config"]["payload"]["resolved"] = {"context": {"capabilities": [PREPARATION_CAPABILITY]}}
    snapshot["config"]["payload"]["context_preparation"] = evidence
    original, adapter = deepcopy(snapshot), ScriptedAdapter()
    with pytest.raises(KernelContractError) as rejected:
        SnapshotKernel().run(snapshot, registered, adapter)
    assert rejected.value.code == "message_contract_error"
    assert isinstance(rejected.value.__cause__, ContractValidationError)
    assert "no longer supported" in str(rejected.value.__cause__)
    assert (rejected.value.model_requests, rejected.value.attempts) == (0, 0)
    assert not adapter.calls and snapshot == original


@pytest.mark.parametrize("evidence", [{}, [], False, "untrusted", {"s0": []}])
def test_unselected_preparation_evidence_also_never_dispatches(evidence):
    from phase1_agent.runtime import KernelContractError, SnapshotKernel

    snapshot, registered = ordinary_snapshot()
    snapshot["config"]["payload"]["context_preparation"] = evidence
    adapter = ScriptedAdapter()
    with pytest.raises(KernelContractError) as rejected:
        SnapshotKernel().run(snapshot, registered, adapter)
    assert rejected.value.code == "message_contract_error"
    assert "cannot carry" in str(rejected.value.__cause__)
    assert (rejected.value.model_requests, rejected.value.attempts) == (0, 0)
    assert not adapter.calls


@pytest.mark.parametrize("defect", ["identity", "secret", "schema", "extra"])
def test_current_gate_rejects_damaged_snapshot_structure(defect):
    snapshot, _ = ordinary_snapshot()
    if defect == "identity":
        snapshot["snapshot_id"] = "not-a-uuid"
    elif defect == "secret":
        snapshot["config"]["payload"]["api_key"] = "forbidden"
    elif defect == "schema":
        snapshot["output_schema"] = {"$ref": "https://example.test/schema"}
    else:
        snapshot["unexpected"] = True
    with pytest.raises(ContractValidationError):
        check_prepared_request_capacity(snapshot)
