from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import ValidationError

from phase1_agent.tools import final_answer_tool, register_callable


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {"value": {"type": "string", "description": "Value to record"}},
        "required": ["value"],
        "additionalProperties": False,
    }


def test_register_callable_uses_one_schema_for_definition_and_validation():
    values = []

    def record(value):
        values.append(value)
        return f"recorded:{value}"

    original = _schema()
    tool = register_callable("record", "Record a value", original, record)
    original["properties"]["value"]["type"] = "integer"

    assert tool.definition["function"]["parameters"] is tool.schema
    assert tool.schema["properties"]["value"]["type"] == "string"
    tool.validate({"value": "hello"})
    assert tool.execute({"value": "hello"}) == "recorded:hello"
    assert values == ["hello"]
    for arguments in ({}, {"value": 1}, {"value": "ok", "extra": 1}, {"value": None}):
        with pytest.raises(ValidationError):
            tool.validate(arguments)


def test_registered_schema_and_definition_are_immutable():
    tool = register_callable("echo", "Echo", _schema(), lambda value: value)
    with pytest.raises(TypeError):
        tool.schema["properties"]["value"]["type"] = "integer"
    with pytest.raises(TypeError):
        tool.definition["function"]["parameters"]["properties"]["value"]["type"] = "integer"
    assert tool.definition["function"]["parameters"] is tool.schema


def test_in_place_schema_operations_cannot_mutate_before_raising():
    tool = register_callable("echo", "Echo", _schema(), lambda value: value)
    with pytest.raises(TypeError):
        tool.schema["required"] += ["extra"]
    with pytest.raises(TypeError):
        tool.schema["properties"] |= {"extra": {"type": "number"}}
    with pytest.raises(TypeError):
        tool.schema["required"] *= 2
    assert tool.schema["required"] == ["value"]
    assert list(tool.schema["properties"]) == ["value"]


@pytest.mark.parametrize("input_type,value", [("integer", 7), ("number", 1.5), ("boolean", True)])
def test_register_callable_supports_other_scalars(input_type, value):
    schema = _schema()
    schema["properties"]["value"]["type"] = input_type
    tool = register_callable("echo", "Echo", schema, lambda value: value)
    tool.validate({"value": value})
    assert tool.execute({"value": value}) == value


def test_register_callable_accepts_required_list_in_any_order_and_keyword_only_inputs():
    schema = _schema()
    schema["properties"]["count"] = {"type": "integer", "description": "Count"}
    schema["required"] = ["count", "value"]

    def repeat(value, *, count):
        return value * count

    tool = register_callable("repeat", "Repeat", schema, repeat)
    tool.validate({"value": "x", "count": 2})
    assert tool.execute({"value": "x", "count": 2}) == "xx"


@pytest.mark.parametrize(
    "change",
    [
        lambda schema: schema["properties"]["value"].update({"type": "array"}),
        lambda schema: schema["properties"]["value"].update({"nullable": True}),
        lambda schema: schema["properties"]["value"].update({"type": ["string", "null"]}),
        lambda schema: schema.update({"required": []}),
        lambda schema: schema.update({"additionalProperties": True}),
        lambda schema: schema.update({"title": "unrestricted"}),
    ],
)
def test_register_callable_rejects_schema_outside_required_scalars(change):
    schema = _schema()
    change(schema)
    with pytest.raises(ValueError):
        register_callable("echo", "Echo", schema, lambda value: value)


@pytest.mark.parametrize(
    "implementation",
    [
        lambda value="default": value,
        lambda other: other,
        lambda *args: args,
        lambda **kwargs: kwargs,
        lambda value, extra: value,
    ],
)
def test_register_callable_rejects_mismatched_or_optional_signature(implementation):
    with pytest.raises(ValueError):
        register_callable("echo", "Echo", _schema(), implementation)


def test_register_callable_rejects_reserved_control_name():
    with pytest.raises(ValueError, match="reserved"):
        register_callable("final_answer", "Override", _schema(), lambda value: value)
    assert final_answer_tool().execute({"answer": {"ok": True}}) == {"ok": True}


def test_snapshot_kernel_runs_without_importing_smolagents_in_isolated_process():
    source = Path(__file__).resolve().parents[1] / "src"
    script = """
import builtins
from copy import deepcopy
import json
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
original_import = builtins.__import__
def blocked_import(name, *args, **kwargs):
    if name == "smolagents" or name.startswith("smolagents."):
        raise AssertionError("smolagents import attempted: " + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = blocked_import

from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.runtime import SnapshotKernel
from phase1_agent.tools import register_callable, final_answer_tool

effects = []
def record(value):
    effects.append(value)
    return "recorded:" + value

schema = {
    "type": "object",
    "properties": {"value": {"type": "string", "description": "Value"}},
    "required": ["value"],
    "additionalProperties": False,
}
tool = register_callable("record", "Record", schema, record)
registered = (tool, final_answer_tool())
example = Path(sys.argv[1]).parent / "examples" / "contracts_v2" / "success.json"
snapshot = json.loads(example.read_text(encoding="utf-8"))["input_snapshot"][0]
snapshot["tool_definitions"] = [
    {"name": entry.name, "version": "1",
     "description": entry.definition["function"]["description"],
     "parameters_schema": deepcopy(entry.schema)}
    for entry in registered
]
snapshot["output_schema"] = {
    "type": "object", "properties": {"result": {"type": "string"}},
    "required": ["result"], "additionalProperties": False,
}
original = deepcopy(snapshot)
class FakeModel:
    def __init__(self):
        self.requests = 0
    def generate(self, messages, tools):
        self.requests += 1
        if self.requests == 1:
            return ModelResponse("tool_calls", tool_calls=(
                ModelToolCall("call-1", "record", '{"value":"offline"}'),
            ))
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["blocks"][0]["content"] == "recorded:offline"
        assert messages[-1]["blocks"][0]["tool_call_id"] == messages[-2]["blocks"][0]["tool_call_id"]
        return ModelResponse("tool_calls", tool_calls=(
            ModelToolCall("call-2", "final_answer", '{"answer":{"result":"done"}}'),
        ))

model = FakeModel()
result = SnapshotKernel().run(snapshot, registered, model, max_model_requests=2)
assert result.final["value"] == {"result": "done"}
assert effects == ["offline"] and model.requests == 2
assert len(result.messages) == 4
assert (result.model_requests, result.attempts) == (2, 2)
assert snapshot == original
assert not any(name == "smolagents" or name.startswith("smolagents.") for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(source)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
