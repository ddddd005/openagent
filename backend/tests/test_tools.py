from __future__ import annotations

from typing import Any

import pytest
from jsonschema import ValidationError
from smolagents import Tool

from phase1_agent.tools import final_answer_tool, register_tool


def _make_tool(
    input_type: str | list[str] = "string",
    annotation: Any = str,
    *,
    input_options: dict[str, Any] | None = None,
    optional: bool = False,
    name: str = "lookup",
    signature_name: str = "value",
) -> Tool:
    if optional:
        input_options = {**(input_options or {}), "nullable": True}

    if optional:

        def forward(self, value="default"):
            return value

    else:

        def forward(self, value):
            return value

    forward.__annotations__ = {"value": annotation, "return": str}
    input_spec = {"type": input_type, "description": "Input value"}
    input_spec.update(input_options or {})
    return type(
        "TestTool",
        (Tool,),
        {
            "name": name,
            "description": "A test tool",
            "inputs": {signature_name: input_spec},
            "output_type": "string",
            "forward": forward,
        },
    )()


def test_register_tool_builds_shared_strict_function_schema_and_executes_kwargs():
    tool = _make_tool()
    registered = register_tool(tool)

    assert registered.name == "lookup"
    assert registered.definition["type"] == "function"
    function = registered.definition["function"]
    assert function["name"] == "lookup"
    assert function["parameters"] is registered.schema
    assert registered.schema == {
        "type": "object",
        "properties": {"value": {"type": "string", "description": "Input value"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    registered.validate({"value": "hello"})
    assert registered.execute({"value": "hello"}) == "hello"


@pytest.mark.parametrize(
    ("input_type", "annotation", "value"),
    [
        ("integer", int, 7),
        ("number", float, 2.5),
        ("boolean", bool, True),
    ],
)
def test_register_tool_supports_each_other_simple_scalar(input_type, annotation, value):
    registered = register_tool(_make_tool(input_type, annotation))

    assert registered.schema["properties"]["value"]["type"] == input_type
    registered.validate({"value": value})
    assert registered.execute({"value": value}) == value


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "required"),
        ({"value": "ok", "extra": True}, "Additional properties"),
        ({"value": 3}, "not of type"),
    ],
)
def test_registered_tool_rejects_arguments_outside_schema(arguments, message):
    registered = register_tool(_make_tool())
    with pytest.raises(ValidationError, match=message):
        registered.validate(arguments)


@pytest.mark.parametrize(
    ("input_type", "annotation"),
    [
        ("any", Any),
        ("array", list),
        ("object", dict),
        (["string", "integer"], str | int),
    ],
)
def test_register_tool_rejects_non_scalar_or_union_types(input_type, annotation):
    registered_tool = _make_tool(input_type, annotation)
    with pytest.raises(ValueError, match="scalar type"):
        register_tool(registered_tool)


def test_register_tool_rejects_optional_parameters():
    with pytest.raises(ValueError, match="optional"):
        register_tool(_make_tool(optional=True))


def test_register_tool_rejects_nullable_metadata_and_unknown_input_keys():
    nullable_tool = _make_tool(
        annotation=str | None,
        input_options={"nullable": True},
    )
    with pytest.raises(ValueError, match="unknown keys"):
        register_tool(nullable_tool)

    unknown_key_tool = _make_tool(input_options={"enum": ["a", "b"]})
    with pytest.raises(ValueError, match="unknown keys"):
        register_tool(unknown_key_tool)


def test_smolagents_signature_validation_still_happens_when_tool_is_created():
    with pytest.raises(Exception, match="forward.*parameters"):
        _make_tool(signature_name="other")


def test_final_answer_requires_one_object_and_returns_its_answer():
    registered = final_answer_tool()

    assert registered.name == "final_answer"
    assert registered.definition["type"] == "function"
    assert registered.definition["function"]["parameters"] is registered.schema
    assert registered.schema["required"] == ["answer"]
    assert registered.schema["properties"]["answer"]["type"] == "object"
    assert registered.schema["additionalProperties"] is False

    answer = {"result": 42}
    registered.validate({"answer": answer})
    assert registered.execute({"answer": answer}) is answer
    with pytest.raises(ValidationError):
        registered.validate({"answer": answer, "extra": True})
    with pytest.raises(ValidationError):
        registered.validate({})


def test_register_tool_rejects_reserved_final_answer_name():
    with pytest.raises(ValueError, match="reserved"):
        register_tool(_make_tool(name="final_answer"))
