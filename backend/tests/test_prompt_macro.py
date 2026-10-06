from __future__ import annotations

import copy

import pytest

from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent.prompt_macro import MacroLimits, macro_replace
from phase1_agent.prompt_variables import create_variable_registry, create_variable_snapshot


def snapshot():
    return create_variable_snapshot(
        create_variable_registry(
            workflow_id="workflow", revision=1, definitions=[
                {"name": "a", "type": "string", "default": "{{b}}"},
                {"name": "b", "type": "string", "default": "done"},
                {"name": "self", "type": "string", "default": "{{self}}"},
                {"name": "empty", "type": "string", "default": ""},
                {"name": "unset", "type": "string"},
                {"name": "integer", "type": "integer", "default": -7},
                {"name": "number", "type": "number", "default": 1.5},
                {"name": "bool", "type": "boolean", "default": False},
                {"name": "escaped", "type": "string", "default": r"\{\{b\}\}"},
            ],
        ),
        workflow_session_id="session", node_binding_id="binding",
    )


def run(text, *, limits=None, values=None):
    return macro_replace(
        text, snapshot() if values is None else values, node_id="macro-node",
        limits=limits if limits is not None else MacroLimits(1000, 1000, 100),
    )


def test_one_pass_does_not_scan_replacement_but_next_node_can():
    first = run("{{a}}")
    assert first == "{{b}}"
    assert run(first) == "done"
    assert run("{{self}}") == "{{self}}"


def test_replaces_all_original_matches_and_renders_primitive_types():
    assert run("{{b}}|{{b}}|{{integer}}|{{number}}|{{bool}}|{{empty}}") == "done|done|-7|1.5|false|"
    assert run("{{workflow_session_id}}/{{node_binding_id}}") == "session/binding"


def test_only_original_input_escapes_are_consumed():
    assert run(r"\{\{b\}\}") == "{{b}}"
    assert run(run(r"\{\{b\}\}")) == "done"
    assert run("{{escaped}}") == r"\{\{b\}\}"
    assert run(r"\\ \{ \} \q") == "\\ { } \\q"


def test_macro_never_assigns_or_mutates_snapshot():
    original = snapshot()
    before = copy.deepcopy(original)
    assert run("{{a}}/{{empty}}", values=original) == "{{b}}/"
    assert original == before


@pytest.mark.parametrize("text", [
    "{{}}", "{{ b}}", "{{b }}", "{{a.b}}", "{{a-b}}", "{{1a}}",
    "{{a+b}}", "{{a()}}", "{{a|b}}", "{{{{b}}}}", "{{a{{b}}}}",
    "{{b", "{{b}", "}}", "before }} after", "{{b}}}}",
])
def test_invalid_macro_syntax_explicitly_fails_with_location(text):
    with pytest.raises(PromptProcessingError) as error:
        run(text)
    assert error.value.code == "invalid_macro_reference"
    assert error.value.node_id == "macro-node"
    assert error.value.offset is not None
    assert text not in str(error.value)


@pytest.mark.parametrize("text,offset", [("prefix {{missing}}", 7), ("{{unset}}", 0)])
def test_unknown_and_unassigned_fail_but_do_not_leak_text(text, offset):
    with pytest.raises(PromptProcessingError) as error:
        run(text)
    assert error.value.code == "missing_macro_variable"
    assert error.value.offset == offset
    assert error.value.node_id == "macro-node"
    assert text not in str(error.value)


def test_single_braces_unknown_escape_and_trailing_slash_are_plain_text():
    assert run("{plain} { }x") == "{plain} { }x"
    assert run("a{b}c\\") == "a{b}c\\"
    assert run(r"\q") == r"\q"


def test_valid_name_allows_underscore_digits_and_case():
    values = create_variable_snapshot(
        create_variable_registry(
            workflow_id="workflow", revision=1,
            definitions=[{"name": "_Name_2", "type": "string", "default": "valid"}],
        ),
        workflow_session_id="session", node_binding_id="binding",
    )
    assert run("{{_Name_2}}", values=values) == "valid"
    with pytest.raises(PromptProcessingError) as error:
        run("{{_name_2}}", values=values)
    assert error.value.code == "missing_macro_variable"


def test_input_output_and_substitution_boundaries_are_inclusive():
    assert run("{{b}}", limits=MacroLimits(5, 4, 1)) == "done"
    assert run("", limits=MacroLimits(0, 0, 0)) == ""
    assert run("{{empty}}", limits=MacroLimits(9, 0, 1)) == ""
    assert run("literal", limits=MacroLimits(7, 7, 0)) == "literal"
    assert run("{{b}}{{b}}", limits=MacroLimits(10, 8, 2)) == "donedone"


@pytest.mark.parametrize("text,limits,code,offset", [
    ("{{b}}", MacroLimits(4, 100, 10), "macro_input_limit", 0),
    ("{{b}}", MacroLimits(100, 3, 10), "macro_output_limit", 0),
    ("12345", MacroLimits(100, 4, 10), "macro_output_limit", 4),
    ("{{b}}{{b}}", MacroLimits(100, 100, 1), "macro_substitution_limit", 5),
    ("{{empty}}", MacroLimits(100, 100, 0), "macro_substitution_limit", 0),
])
def test_exceeded_resource_limits_fail_with_node_and_offset(text, limits, code, offset):
    with pytest.raises(PromptProcessingError) as error:
        run(text, limits=limits)
    assert error.value.code == code
    assert error.value.node_id == "macro-node"
    assert error.value.offset == offset


@pytest.mark.parametrize("value", [-1, True, 1.0, None, "10"])
@pytest.mark.parametrize("field", ["max_input_chars", "max_output_chars", "max_substitutions"])
def test_limits_are_explicit_strict_nonnegative_integers(field, value):
    fields = {"max_input_chars": 100, "max_output_chars": 100, "max_substitutions": 10}
    fields[field] = value
    with pytest.raises(PromptProcessingError) as error:
        MacroLimits(**fields)
    assert error.value.code == "invalid_macro_limits"


@pytest.mark.parametrize("text", [None, {}, 1, "\ud800"])
def test_invalid_macro_input_is_redacted(text):
    with pytest.raises(PromptProcessingError) as error:
        run(text)
    assert error.value.code == "invalid_macro_input"
    assert error.value.node_id == "macro-node"


def test_invalid_snapshot_error_is_located_at_macro_node():
    values = snapshot()
    values["values"]["b"]["value"] = "secret-other-value"
    with pytest.raises(PromptProcessingError) as error:
        run("{{b}}", values=values)
    assert error.value.node_id == "macro-node"
    assert "secret-other-value" not in str(error.value)


def test_explicit_limits_and_node_identity_required():
    with pytest.raises(PromptProcessingError) as error:
        macro_replace("text", snapshot(), node_id="node", limits=None)
    assert error.value.code == "invalid_macro_limits"
    with pytest.raises(PromptProcessingError) as error:
        macro_replace("text", snapshot(), node_id="", limits=MacroLimits(10, 10, 0))
    assert error.value.code == "invalid_macro_node"
