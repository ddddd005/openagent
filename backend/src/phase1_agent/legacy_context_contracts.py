"""Frozen legacy context projection without component or runtime construction."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history, validate_turn_final
from .contract_json import canonical_bytes, dumps_pretty
from .contracts_v2 import validate_record
from .prepared_request import PREPARATION_CAPABILITY


BASIC_CONTEXT_COMPONENT = "7be319b8-30bd-4674-b7bf-d1cf54a1a103"
BASIC_CONTEXT_VERSION = "1.0.0"
PREPARED_CONTEXT_COMPONENT = "7be319b8-30bd-4674-b7bf-d1cf54a1a10e"
PREPARED_CONTEXT_VERSION = "1.0.0"
PREPARED_PROJECTION_VERSION = 2


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _input_source(node_input: dict[str, Any]) -> dict[str, Any]:
    source = node_input["source"]
    if source["kind"] == "visible_message":
        return {"kind": "human", "visible_message_id": source["visible_message_id"]}
    if source["kind"] == "upstream_output":
        return {"kind": "upstream_node", "output_id": source["output_id"]}
    raise ContractValidationError("Prepared Context requires an explicit input source")


def validate_frozen_preparation(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Validate saved preparation without rerunning a rule or reading live values."""
    snapshot = validate_record("input_snapshot", snapshot)
    return _validate_validated_frozen_preparation(snapshot)


def _validate_validated_frozen_preparation(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Validate preparation on a detached snapshot already checked by its caller."""
    payload = snapshot["config"]["payload"]
    descriptor = payload.get("resolved", {}).get("context", {})
    prepared = PREPARATION_CAPABILITY in descriptor.get("capabilities", [])
    evidence = payload.get("context_preparation")
    if not prepared:
        _require(evidence is None, "Legacy Context cannot carry prepared projection evidence")
        return None
    from .prompt_preparation import validate_context_preparation
    from .variable_preparation import (
        validate_frozen_variable_preparation, validate_root_variable_rederivation,
    )

    _require(evidence is not None, "Prepared Context requires frozen projection evidence")
    evidence = validate_context_preparation(evidence)
    _require(snapshot["projection_version"] == PREPARED_PROJECTION_VERSION,
             "Prepared Context projection version is incompatible")
    base = evidence["s0"]
    _require(canonical_bytes(snapshot["s0"][:len(base)]) == canonical_bytes(base),
             "Frozen S0 differs from its preparation evidence")
    for observation in snapshot["s0"][len(base):]:
        _require(observation["schema_version"] == 2
                 and observation["source"]["kind"] == "runtime_execution_observation",
                 "Only saved continuation observations may extend prepared S0")
    validate_message_history(snapshot["s0"])
    _require(canonical_bytes(payload["context"]) == canonical_bytes(evidence["config"]),
             "Frozen Context config differs from its preparation evidence")
    _require(snapshot["parent_turn_id"] == evidence["send_view"]["parent_turn_id"],
             "Frozen parent differs from its preparation evidence")
    _require(snapshot["node_binding_id"] == evidence["send_view"]["node_binding_id"],
             "Frozen node binding differs from its preparation evidence")
    variables = payload.get("variable_preparation")
    if payload["context"]["schema_version"] == 2:
        _require(type(variables) is dict and set(variables) == {
            "schema_version", "kind", "state_ref", "receipt_key", "frozen", "rederivation", "seed",
        }, "Variable Context requires exact frozen variable evidence")
        _require(type(variables["schema_version"]) is int and variables["schema_version"] == 1
                 and variables["kind"] == "workflow_variable_preparation",
                 "Frozen variable evidence version or kind is invalid")
        frozen = validate_frozen_variable_preparation(variables["frozen"])
        rederived = variables["rederivation"]
        if rederived is not None:
            rederived = validate_root_variable_rederivation(rederived, frozen)
        expected = rederived["snapshot"] if rederived is not None else frozen["snapshot"]
        _require(canonical_bytes(expected) == canonical_bytes(payload["context"]["variables"]),
                 "Frozen variable outcome differs from prepared Context")
        _require(canonical_bytes(frozen["plan"]) == canonical_bytes(payload["context"]["variable_plan"]),
                 "Frozen variable rules differ from prepared Context")
        reference = variables["state_ref"]
        _require(type(reference) is dict and set(reference) == {
            "workflow_session_id", "workflow_id", "registry_revision", "revision",
        } and all(type(reference[field]) is int and reference[field] >= 1
                  for field in ("registry_revision", "revision")),
                 "Frozen variable state reference is invalid")
        for field in ("workflow_session_id", "workflow_id"):
            try:
                identity = UUID(reference[field])
                _require(identity.version == 4 and str(identity) == reference[field],
                         "Frozen variable owner must be a canonical UUID4")
            except (ValueError, TypeError, AttributeError):
                raise ContractValidationError("Frozen variable owner must be a canonical UUID4") from None
        _require(type(variables["receipt_key"]) is str and bool(variables["receipt_key"].strip())
                 and len(variables["receipt_key"]) <= 128,
                 "Frozen variable receipt identity is invalid")
        seed = variables["seed"]
        _require(seed is None or type(seed) is dict and set(seed) == {"commit_id", "state_ref"},
                 "Frozen variable seed fields are invalid")
        if seed is not None:
            try:
                commit_identity = UUID(seed["commit_id"])
                _require(commit_identity.version == 4 and str(commit_identity) == seed["commit_id"],
                         "Frozen variable seed commit identity is invalid")
            except (ValueError, TypeError, AttributeError):
                raise ContractValidationError("Frozen variable seed commit identity is invalid") from None
            seed_ref = seed["state_ref"]
            _require(type(seed_ref) is dict and set(seed_ref) == set(reference)
                     and all(seed_ref[field] == reference[field] for field in (
                         "workflow_session_id", "workflow_id", "registry_revision",
                     )) and type(seed_ref["registry_revision"]) is int
                     and type(seed_ref["revision"]) is int
                     and 1 <= seed_ref["revision"] < reference["revision"],
                     "Frozen variable seed reference is inconsistent")
        _require(reference["workflow_id"] == expected["registry"]["workflow_id"]
                 and reference["registry_revision"] == expected["registry"]["revision"]
                 and reference["workflow_session_id"] == expected["workflow_session_id"],
                 "Frozen variable state reference differs from its variable owner")
        original_input = rederived["node_input"] if rederived is not None else frozen["node_input"]
        _require(all(canonical_bytes(original_input[field]) == canonical_bytes(evidence["node_input"][field])
                     for field in ("schema_version", "port_id", "payload_schema_ref", "source", "payload")),
                 "Frozen variable dependency input differs from Context preparation")
    else:
        _require(variables is None, "Static variable Context cannot carry transaction evidence")
    program_variables = payload.get("program_variable_preparation")
    if payload["context"]["schema_version"] == 3:
        _require(type(program_variables) is dict and set(program_variables) == {
            "schema_version", "kind", "session_id", "revision", "receipt_key",
        } and program_variables["schema_version"] == 1
                 and program_variables["kind"] == "program_variable_preparation"
                 and type(program_variables["revision"]) is int and program_variables["revision"] >= 1,
                 "Preparation program requires a durable variable result")
    else:
        _require(program_variables is None, "Legacy context cannot carry program variable evidence")
    # Cloned starts retain origin identities; the record owner checks semantic input.
    return evidence


def project_basic_turn(turn: dict, node_input: dict, snapshot: dict) -> list[dict[str, Any]]:
    """Project one archived turn's own input and accepted message delta."""
    node_input = validate_record("node_input", node_input)
    snapshot = validate_record("input_snapshot", snapshot)
    turn = validate_record("turn", turn)
    _require(turn["snapshot_id"] == snapshot["snapshot_id"]
             and turn["input_id"] == snapshot["input_id"] == node_input["input_id"],
             "Turn, frozen snapshot and input identities differ")
    validate_turn_final(turn, snapshot)
    source = node_input["source"]
    if source["kind"] == "visible_message":
        expected_source = {"kind": "human", "visible_message_id": source["visible_message_id"]}
    elif source["kind"] == "upstream_output":
        expected_source = {"kind": "upstream_node", "output_id": source["output_id"]}
    else:
        raise ContractValidationError("External input requires an explicit v2 source projection")
    expected_text = dumps_pretty(node_input["payload"])
    input_message = next((
        message for message in reversed(snapshot["s0"])
        if message["role"] == "user"
        and message["source"] == expected_source
        and message["blocks"] == [{"kind": "text", "text": expected_text}]
    ), None)
    _require(input_message is not None, "Frozen S0 has no matching input message")
    projected = [input_message, *turn["messages"]]
    validate_message_history(projected)
    return projected


def project_prepared_turn(turn: dict, node_input: dict, snapshot: dict) -> list[dict[str, Any]]:
    turn = validate_record("turn", turn)
    node_input = validate_record("node_input", node_input)
    snapshot = validate_record("input_snapshot", snapshot)
    _require(turn["snapshot_id"] == snapshot["snapshot_id"]
             and turn["input_id"] == snapshot["input_id"] == node_input["input_id"],
             "Turn, snapshot and input identities differ")
    validate_turn_final(turn, snapshot)
    evidence = validate_frozen_preparation(snapshot)
    if evidence is None:
        return project_basic_turn(turn, node_input, snapshot)
    root = next(
        message for message in evidence["canonical_messages"]
        if message["message_id"] == evidence["root_locator"]["message_id"]
    )
    _require(root["source"] == _input_source(node_input)
             and root["blocks"] == [{"kind": "text", "text": dumps_pretty(node_input["payload"])}],
             "Prepared history root differs from its canonical input")
    projected = [root, *turn["messages"]]
    validate_message_history(projected)
    return deepcopy(projected)
