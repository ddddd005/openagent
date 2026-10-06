"""Explicit constants/root dependencies have exact, nonrecursive frozen replay."""

from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.prompt_variables import create_variable_registry, create_variable_snapshot
from phase1_agent.variable_preparation import (
    make_variable_assignment_plan, prepare_variable_assignments,
    rederive_root_variable_assignments, validate_frozen_variable_preparation,
    validate_root_variable_rederivation, validate_variable_assignment_plan,
)


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def snapshot():
    registry = create_variable_registry(
        workflow_id=uid(1), revision=1, definitions=[
            {"name": "dependency", "type": "string", "default": "before"},
            {"name": "fixed", "type": "string", "default": "fixed-default"},
            {"name": "number", "type": "integer", "default": 3},
        ],
    )
    return create_variable_snapshot(registry, workflow_session_id=uid(2), node_binding_id=uid(3))


def node_input(text="first", number=4):
    return {
        "schema_version": 1, "input_id": uid(number), "port_id": "request",
        "payload_schema_ref": {"schema_id": "writing_text", "version": 1},
        "source": {"kind": "upstream_output", "output_id": uid(5)},
        "payload": {"text": text},
    }


def root(name="dependency", node="root-node"):
    return {"node_id": node, "name": name, "source": {"kind": "root_input_text"}}


def constant(name="fixed", value="stable", node="constant-node"):
    return {"node_id": node, "name": name, "source": {"kind": "constant", "value": value}}


def test_resolve_once_records_exact_input_rule_order_and_detached_values():
    basis, assignments, root_input = snapshot(), [root(), constant()], node_input("{{literal}}")
    plan = make_variable_assignment_plan(assignments)
    frozen = prepare_variable_assignments(basis, plan, root_input)
    assert frozen["snapshot"]["values"]["dependency"]["value"] == "{{literal}}"
    assert frozen["snapshot"]["values"]["fixed"]["value"] == "stable"
    assert frozen["snapshot"]["values"]["number"]["value"] == 3
    assert frozen["resolved_assignments"] == [
        {"node_id": "root-node", "name": "dependency", "value": "{{literal}}"},
        {"node_id": "constant-node", "name": "fixed", "value": "stable"},
    ]
    assert frozen["dependencies"] == [{
        "assignment_index": 0, "node_id": "root-node", "name": "dependency",
        "source": {"kind": "root_input_text", "input_id": uid(4)}, "active": True,
    }]
    assert validate_frozen_variable_preparation(frozen) == frozen
    assignments[0]["source"]["kind"] = "changed"
    root_input["payload"]["text"] = "changed"
    basis["values"]["fixed"]["value"] = "changed"
    assert frozen["node_input"]["payload"]["text"] == "{{literal}}"
    assert frozen["basis_snapshot"]["values"]["fixed"]["value"] == "fixed-default"


def test_new_b_rederives_only_effective_root_rules_and_keeps_other_frozen_values():
    frozen = prepare_variable_assignments(
        snapshot(), make_variable_assignment_plan([constant(), root()]), node_input("old A"),
    )
    derived = rederive_root_variable_assignments(
        frozen, node_input("new A", 40), workflow_session_id=uid(20), node_binding_id=uid(30),
    )
    assert derived["snapshot"]["values"]["dependency"]["value"] == "new A"
    assert derived["snapshot"]["values"]["fixed"] == frozen["snapshot"]["values"]["fixed"]
    assert derived["snapshot"]["values"]["number"] == frozen["snapshot"]["values"]["number"]
    assert derived["snapshot"]["values"]["workflow_session_id"]["value"] == uid(20)
    assert derived["snapshot"]["values"]["node_binding_id"]["value"] == uid(30)
    assert frozen["snapshot"]["values"]["dependency"]["value"] == "old A"
    assert validate_root_variable_rederivation(derived, frozen) == derived
    derived["snapshot"]["values"]["dependency"]["value"] = "caller change"
    assert frozen["snapshot"]["values"]["dependency"]["value"] == "old A"


@pytest.mark.parametrize("rules,expected,active", [
    ([root(), constant("dependency", "last constant")], "last constant", [False]),
    ([constant("dependency", "first constant"), root()], "new root", [True]),
    ([root(), root(node="second-root")], "new root", [False, True]),
])
def test_later_assignment_masks_earlier_dependency_without_reapplying_constants(rules, expected, active):
    frozen = prepare_variable_assignments(
        snapshot(), make_variable_assignment_plan(rules), node_input("old root"),
    )
    assert [entry["active"] for entry in frozen["dependencies"]] == active
    derived = rederive_root_variable_assignments(frozen, node_input("new root", 41))
    assert derived["snapshot"]["values"]["dependency"]["value"] == expected
    assert len(derived["assignments"]) == sum(active)


def test_no_dependency_replay_keeps_exact_declared_values():
    frozen = prepare_variable_assignments(
        snapshot(), make_variable_assignment_plan([constant(value="saved")]), node_input(),
    )
    derived = rederive_root_variable_assignments(frozen, node_input("different", 41))
    assert derived["assignments"] == []
    assert derived["snapshot"] == frozen["snapshot"]


@pytest.mark.parametrize("rule", [
    root(name="number"), root(name="unknown"),
    constant(name="number", value=True), constant(name="number", value=1.0),
    constant(name="workflow_session_id"), constant(value=None),
])
def test_assignment_requires_exact_registered_type_and_supported_nonpreset_target(rule):
    with pytest.raises(ContractValidationError):
        prepare_variable_assignments(snapshot(), make_variable_assignment_plan([rule]), node_input())


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 2}, {"kind": "another"},
    {"assignments": ()}, {"extra": 1},
    {"assignments": [root()] * 2},
    {"assignments": [{**root(), "source": {"kind": "expression", "text": "payload.text"}}]},
    {"assignments": [{**root(), "source": {"kind": "root_input_text", "path": "text"}}]},
    {"assignments": [{**root(), "node_id": ""}]},
    {"assignments": [{**root(), "name": "invalid-name"}]},
    {"assignments": [root(node=f"node-{index}") for index in range(65)]},
])
def test_plan_is_strict_versioned_and_bounded(change):
    value = make_variable_assignment_plan([root()])
    value.update(change)
    with pytest.raises(ContractValidationError):
        validate_variable_assignment_plan(value)


@pytest.mark.parametrize("field", [
    "evidence_digest", "dependencies", "resolved_assignments", "snapshot", "node_input", "extra",
])
def test_frozen_correspondence_rejects_mutated_evidence(field):
    frozen = prepare_variable_assignments(
        snapshot(), make_variable_assignment_plan([root()]), node_input(),
    )
    if field == "snapshot":
        frozen["snapshot"]["values"]["dependency"]["value"] = "forged"
    elif field == "node_input":
        frozen["node_input"]["payload"]["text"] = "forged"
    else:
        frozen[field] = [] if field in {"dependencies", "resolved_assignments"} else "forged"
    with pytest.raises(ContractValidationError):
        validate_frozen_variable_preparation(frozen)


def test_rederivation_does_not_invent_a_changed_constant_or_scope():
    frozen = prepare_variable_assignments(
        snapshot(), make_variable_assignment_plan([root(), constant()]), node_input(),
    )
    derived = rederive_root_variable_assignments(frozen, node_input("new", 41))
    original = deepcopy(derived)
    derived["snapshot"]["values"]["fixed"]["value"] = "forged constant"
    with pytest.raises(ContractValidationError):
        validate_root_variable_rederivation(derived, frozen)
    assert validate_root_variable_rederivation(original, frozen) == original
