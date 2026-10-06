"""Explicit variable assignment rules and frozen root-input dependency replay."""

from __future__ import annotations

import copy
import re
from typing import Any

from .contract_json import canonical_bytes, content_digest, validate_json_value
from .contracts_v2 import validate_record
from .prompt_errors import PromptProcessingError
from .prompt_variables import (
    PRESET_VARIABLE_NAMES, VARIABLE_NAME_PATTERN, assign_variable,
    validate_variable_snapshot,
)


MAX_VARIABLE_ASSIGNMENTS = 64
_NAME = re.compile(VARIABLE_NAME_PATTERN)
_FROZEN_FIELDS = {
    "schema_version", "kind", "plan", "basis_snapshot", "node_input",
    "resolved_assignments", "snapshot", "dependencies", "evidence_digest",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PromptProcessingError("invalid_variable_preparation", message)


def _same(left: Any, right: Any) -> bool:
    return canonical_bytes(left) == canonical_bytes(right)


def validate_variable_assignment_plan(value: Any) -> dict[str, Any]:
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {"schema_version", "kind", "assignments"},
             "Variable assignment plan fields are invalid")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1
             and value["kind"] == "variable_assignment_plan",
             "Variable assignment plan version or kind is invalid")
    assignments = value["assignments"]
    _require(type(assignments) is list and len(assignments) <= MAX_VARIABLE_ASSIGNMENTS,
             "Variable assignment plan must be a bounded ordered JSON array")
    seen = set()
    for assignment in assignments:
        _require(type(assignment) is dict and set(assignment) == {"node_id", "name", "source"},
                 "Variable assignment rule fields are invalid")
        node_id = assignment["node_id"]
        _require(type(node_id) is str and bool(node_id.strip()) and node_id not in seen,
                 "Variable assignment processing node identity is invalid or repeated")
        seen.add(node_id)
        name = assignment["name"]
        _require(type(name) is str and _NAME.fullmatch(name) is not None
                 and name not in PRESET_VARIABLE_NAMES,
                 "Variable assignment requires a registered non-preset name")
        source = assignment["source"]
        _require(type(source) is dict and type(source.get("kind")) is str,
                 "Variable assignment source is invalid")
        if source["kind"] == "constant":
            _require(set(source) == {"kind", "value"}, "Constant assignment source fields are invalid")
        else:
            _require(source == {"kind": "root_input_text"},
                     "Only explicit root_input.text dependency is supported")
    return copy.deepcopy(value)


def make_variable_assignment_plan(assignments: list[dict[str, Any]]) -> dict[str, Any]:
    return validate_variable_assignment_plan({
        "schema_version": 1, "kind": "variable_assignment_plan", "assignments": assignments,
    })


def _resolve(
    snapshot: dict[str, Any], plan: dict[str, Any], node_input: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved, dependencies = [], []
    last_writer = {
        assignment["name"]: index for index, assignment in enumerate(plan["assignments"])
    }
    for index, assignment in enumerate(plan["assignments"]):
        name = assignment["name"]
        _require(name in snapshot["values"], "Variable assignment refers to an unregistered name")
        source = assignment["source"]
        if source["kind"] == "root_input_text":
            _require(snapshot["values"][name]["type"] == "string",
                     "Root input text can only be assigned to a registered string variable")
            payload = node_input["payload"]
            _require(type(payload) is dict and type(payload.get("text")) is str,
                     "Root input dependency requires an explicit payload.text string")
            value = payload["text"]
            dependencies.append({
                "assignment_index": index, "node_id": assignment["node_id"], "name": name,
                "source": {"kind": "root_input_text", "input_id": node_input["input_id"]},
                "active": last_writer[name] == index,
            })
        else:
            value = source["value"]
        resolved.append({"node_id": assignment["node_id"], "name": name, "value": value})
    return resolved, dependencies


def prepare_variable_assignments(
    snapshot: dict[str, Any], plan: dict[str, Any], node_input: dict[str, Any],
) -> dict[str, Any]:
    """Resolve once; return exact rules, values and provenance for durable preparation."""
    basis = validate_variable_snapshot(snapshot)
    plan = validate_variable_assignment_plan(plan)
    node_input = validate_record("node_input", node_input)
    resolved, dependencies = _resolve(basis, plan, node_input)
    result_snapshot = basis
    for assignment in resolved:
        result_snapshot = assign_variable(
            result_snapshot, assignment["name"], assignment["value"],
            node_id=assignment["node_id"],
        )
    result = {
        "schema_version": 1, "kind": "frozen_variable_preparation",
        "plan": plan, "basis_snapshot": basis, "node_input": node_input,
        "resolved_assignments": resolved, "snapshot": result_snapshot,
        "dependencies": dependencies,
    }
    result["evidence_digest"] = content_digest(result)
    return copy.deepcopy(result)


def validate_frozen_variable_preparation(value: Any) -> dict[str, Any]:
    validate_json_value(value)
    _require(type(value) is dict and set(value) == _FROZEN_FIELDS,
             "Frozen variable preparation fields are invalid")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1
             and value["kind"] == "frozen_variable_preparation",
             "Frozen variable preparation version or kind is invalid")
    expected = prepare_variable_assignments(value["basis_snapshot"], value["plan"], value["node_input"])
    _require(_same(value, expected), "Frozen variable preparation correspondence is invalid")
    return copy.deepcopy(value)


def rederive_root_variable_assignments(
    frozen: dict[str, Any], node_input: dict[str, Any], *,
    workflow_session_id: str | None = None, node_binding_id: str | None = None,
) -> dict[str, Any]:
    """Refresh only effective root dependencies, keeping all other frozen values.

    A later constant masks an earlier root rule. Inactive dependency rules remain
    recorded but do not overwrite that frozen constant on a replacement B run.
    """
    frozen = validate_frozen_variable_preparation(frozen)
    node_input = validate_record("node_input", node_input)
    snapshot = copy.deepcopy(frozen["snapshot"])
    for name, value in (
        ("workflow_session_id", workflow_session_id), ("node_binding_id", node_binding_id),
    ):
        if value is not None:
            snapshot[name] = value
            snapshot["values"][name]["value"] = value
    snapshot = validate_variable_snapshot(snapshot)
    resolved, _ = _resolve(snapshot, frozen["plan"], node_input)
    assignments = [
        resolved[dependency["assignment_index"]]
        for dependency in frozen["dependencies"] if dependency["active"]
    ]
    for assignment in assignments:
        snapshot = assign_variable(
            snapshot, assignment["name"], assignment["value"], node_id=assignment["node_id"],
        )
    result = {
        "schema_version": 1, "kind": "root_variable_rederivation",
        "source_preparation_digest": frozen["evidence_digest"],
        "node_input": copy.deepcopy(node_input), "assignments": copy.deepcopy(assignments),
        "snapshot": snapshot,
    }
    result["evidence_digest"] = content_digest(result)
    return result


def validate_root_variable_rederivation(
    value: Any, frozen: dict[str, Any],
) -> dict[str, Any]:
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {
        "schema_version", "kind", "source_preparation_digest", "node_input",
        "assignments", "snapshot", "evidence_digest",
    }, "Root variable rederivation fields are invalid")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1
             and value["kind"] == "root_variable_rederivation",
             "Root variable rederivation version or kind is invalid")
    snapshot = validate_variable_snapshot(value["snapshot"])
    expected = rederive_root_variable_assignments(
        frozen, value["node_input"], workflow_session_id=snapshot["workflow_session_id"],
        node_binding_id=snapshot["node_binding_id"],
    )
    _require(_same(value, expected), "Root variable rederivation correspondence is invalid")
    return copy.deepcopy(value)
