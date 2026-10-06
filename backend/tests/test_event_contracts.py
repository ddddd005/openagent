"""Explicit legacy envelopes and exact typed payloads for I09."""

from copy import deepcopy
import json
import traceback

from jsonschema import Draft202012Validator
import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts_v2 import CONTRACT_SCHEMAS, validate_record
from phase1_agent.event_contracts import (
    CORE_EVENT_TYPES, WORKFLOW_OPERATION_KINDS, core_payload_schema_ref, validate_core_payload,
    validate_payload, validate_payload_schema,
)


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def payloads():
    return {
        "command_result": {
            "command_id": uid(1),
            "target": {"workflow_session_id": uid(2), "node_binding_id": uid(3), "run_id": uid(4)},
            "status": "accepted", "reason_code": None,
        },
        "workflow_operation_result": {
            "operation_id": uid(1), "operation_kind": "interrupt",
            "target": {"workflow_session_id": uid(2), "node_binding_id": uid(3), "run_id": uid(4)},
            "status": "accepted", "reason_code": None,
        },
        "run_started": {
            "chain_run_id": uid(5), "input_id": uid(6), "input_snapshot_id": uid(7),
            "parent_turn_id": None,
            "source_chain_run_id": None, "source_run_id": None, "source_turn_id": None,
        },
        "run_state": {
            "previous_status": "running", "status": "paused", "phase": None,
            "available_actions": ["resume"], "action_rejections": {"reroll": "invalid_state"},
            "reason_code": "pause_requested",
        },
        "request_attempt": {
            "request_id": uid(8), "request_index": 1, "attempt_id": uid(9),
            "attempt_index": 1, "outcome": "started", "phase": "requesting",
        },
        "tool_progress": {
            "tool_call_id": uid(10), "tool_execution_id": uid(11),
            "model_order": 1, "status": "settled", "outcome": "success",
        },
        "budget_changed": {
            "budget_kind": "model_requests", "additional": 2, "limit": 10, "used": 8,
            "reason_code": "user_extension",
        },
        "final_ready": {"result_id": uid(12), "turn_id": uid(13), "snapshot_id": uid(7)},
        "archive_result": {
            "result_id": uid(12), "status": "succeeded", "turn_id": uid(13), "reason_code": None,
        },
        "ports_released": {
            "result_id": uid(12), "turn_id": uid(13), "output_id": uid(14),
            "status": "succeeded", "ports": ["final", "context_delta"], "reason_code": None,
        },
        "workflow_published": {
            "chain_run_id": uid(5), "output_id": uid(14), "delivery_id": uid(15),
            "visible_message_id": uid(16), "status": "succeeded", "reason_code": None,
        },
        "preview_delta": {"preview_id": uid(17), "fragment_index": 1, "text": "draft"},
        "preview_cleared": {"preview_id": uid(17), "reason_code": "final_accepted"},
        "diagnostic": {"code": "MODEL_FAILED", "category": "model"},
    }


def event(event_type="run_state"):
    return {
        "schema_version": 2, "event_id": uid(18), "workflow_session_id": uid(2),
        "node_binding_id": uid(3), "run_id": uid(4), "sequence": 1,
        "event_type": event_type, "payload_schema_ref": core_payload_schema_ref(event_type),
        "payload": payloads()[event_type], "visibility": "private", "generation": uid(19),
    }


@pytest.mark.parametrize("event_type", sorted(CORE_EVENT_TYPES))
def test_all_core_payloads_and_v2_envelopes_are_exact_detached_values(event_type):
    value = event(event_type)
    result = validate_record("run_event", json.loads(json.dumps(value)))
    assert result == value
    assert result is not value and result["payload"] is not value["payload"]
    assert validate_core_payload(event_type, value["payload_schema_ref"], value["payload"]) == value["payload"]
    assert Draft202012Validator(CONTRACT_SCHEMAS["run_event"]).is_valid(value)


@pytest.mark.parametrize("event_type", sorted(CORE_EVENT_TYPES))
def test_core_payloads_reject_untyped_or_extra_data(event_type):
    value = event(event_type)
    value["payload"]["raw_exception"] = "PRIVATE-CANARY"
    with pytest.raises(ContractValidationError):
        validate_record("run_event", value)
    assert not Draft202012Validator(CONTRACT_SCHEMAS["run_event"]).is_valid(value)
    value = event(event_type)
    value["payload"].pop(next(iter(value["payload"])))
    with pytest.raises(ContractValidationError):
        validate_record("run_event", value)


@pytest.mark.parametrize("event_type", sorted(CORE_EVENT_TYPES))
def test_payload_version_is_independent_and_cannot_fallback_from_envelope_v2(event_type):
    value = event(event_type)
    value["payload_schema_ref"]["version"] = 2
    with pytest.raises(ContractValidationError):
        validate_record("run_event", value)
    value["payload_schema_ref"] = {"schema_id": "another-type", "version": 1}
    with pytest.raises(ContractValidationError):
        validate_record("run_event", value)


@pytest.mark.parametrize("kind", ["state_changed", "progress", "error", "control_result", "output_preview"])
def test_legacy_v1_is_parsed_without_inventing_live_generation_or_payload_schema(kind):
    value = {
        "schema_version": 1, "event_id": uid(18), "workflow_session_id": uid(2),
        "node_binding_id": uid(3), "run_id": uid(4), "sequence": 1,
        "event_type": kind, "payload": {"legacy": True},
    }
    assert validate_record("run_event", value) == value
    assert "generation" not in validate_record("run_event", value)
    value.update(payload_schema_ref={"schema_id": "legacy", "version": 1}, generation=uid(19), visibility="private")
    with pytest.raises(ContractValidationError):
        validate_record("run_event", value)
    value["schema_version"] = 2
    with pytest.raises(ContractValidationError):
        validate_record("run_event", value)


@pytest.mark.parametrize("field,value", [
    ("schema_version", 3), ("generation", 1), ("generation", None),
    ("sequence", True), ("sequence", 1.0), ("sequence", 0),
    ("visibility", "public"), ("event_type", "run_state\n"),
    ("event_type", "registered_behavior"), ("group_path", ["root"]), ("event_version", 1),
])
def test_live_envelope_rejects_unknown_fields_invalid_identity_or_modes(field, value):
    candidate = event()
    candidate[field] = value
    with pytest.raises(ContractValidationError):
        validate_record("run_event", candidate)


def test_custom_envelope_shape_does_not_claim_registration_or_schema_validation():
    candidate = event()
    candidate.update(
        event_type="chapter.check.progress",
        payload_schema_ref={"schema_id": "chapter-progress", "version": 7},
        payload={"unregistered": "structurally valid"},
    )
    assert validate_record("run_event", candidate) == candidate
    with pytest.raises(ContractValidationError, match="Unknown core"):
        validate_core_payload(candidate["event_type"], candidate["payload_schema_ref"], candidate["payload"])


def test_workflow_operation_vocabulary_matches_public_operation_contract_without_fake_pause():
    actual = {
        variant["properties"]["kind"]["const"]
        for variant in CONTRACT_SCHEMAS["workflow_operation"]["oneOf"]
    }
    assert WORKFLOW_OPERATION_KINDS == actual
    assert "interrupt" in actual and "pause" not in actual


@pytest.mark.parametrize("operation_kind", sorted(WORKFLOW_OPERATION_KINDS))
def test_workflow_operation_results_preserve_actual_id_kind_and_not_local_command_id(operation_kind):
    candidate = event("workflow_operation_result")
    candidate["payload"]["operation_kind"] = operation_kind
    result = validate_record("run_event", candidate)
    assert result["payload"]["operation_id"] == uid(1)
    assert result["payload"]["operation_kind"] == operation_kind
    assert "command_id" not in result["payload"]
    candidate["payload"]["command_id"] = uid(1)
    with pytest.raises(ContractValidationError):
        validate_record("run_event", candidate)


def test_local_and_workflow_result_payload_identities_cannot_be_exchanged():
    candidate = event("command_result")
    candidate["payload"]["operation_id"] = candidate["payload"].pop("command_id")
    with pytest.raises(ContractValidationError):
        validate_record("run_event", candidate)
    candidate = event("workflow_operation_result")
    candidate["payload"]["command_id"] = candidate["payload"].pop("operation_id")
    with pytest.raises(ContractValidationError):
        validate_record("run_event", candidate)
    candidate = event("workflow_operation_result")
    candidate["payload"]["operation_kind"] = "pause"
    with pytest.raises(ContractValidationError):
        validate_record("run_event", candidate)


def test_run_started_keeps_frozen_parent_distinct_from_actual_result_origin():
    candidate = event("run_started")
    candidate["payload"]["parent_turn_id"] = uid(40)
    result = validate_record("run_event", candidate)
    assert result["payload"]["parent_turn_id"] == uid(40)
    assert result["payload"]["source_turn_id"] is None
    assert result["payload"]["source_run_id"] is None
    candidate["payload"].update(
        source_run_id=uid(41), source_turn_id=uid(42), source_chain_run_id=uid(43),
    )
    result = validate_record("run_event", candidate)
    assert result["payload"]["parent_turn_id"] != result["payload"]["source_turn_id"]
    candidate["payload"].pop("parent_turn_id")
    with pytest.raises(ContractValidationError):
        validate_record("run_event", candidate)


@pytest.mark.parametrize("status,outcome,execution,valid", [
    ("pending", None, None, True), ("settled", "never_started", None, True),
    ("uncertain", "started", uid(11), True), ("uncertain", "unknown", uid(11), True),
    ("uncertain", "outcome_unknown", uid(11), True), ("uncertain", "interrupted", uid(11), True),
    ("settled", "success", uid(11), True), ("settled", "error", uid(11), True),
    ("settled", "success", None, False), ("settled", "never_started", uid(11), False),
    ("pending", None, uid(11), False), ("settled", "unknown", uid(11), False),
])
def test_tool_events_preserve_explicit_execution_knowledge(status, outcome, execution, valid):
    candidate = event("tool_progress")
    candidate["payload"].update(status=status, outcome=outcome, tool_execution_id=execution)
    if valid:
        assert validate_record("run_event", candidate) == candidate
    else:
        with pytest.raises(ContractValidationError, match="execution knowledge"):
            validate_record("run_event", candidate)


def test_archive_ports_and_publication_do_not_confuse_preallocated_and_saved_identity():
    candidate = event("final_ready")
    candidate["payload"]["output_id"] = uid(14)
    with pytest.raises(ContractValidationError):
        validate_record("run_event", candidate)
    candidate = event("archive_result")
    candidate["payload"].update(status="failed", turn_id=None, reason_code="write_failed")
    assert validate_record("run_event", candidate) == candidate
    candidate["payload"]["turn_id"] = uid(13)
    with pytest.raises(ContractValidationError, match="unsaved"):
        validate_record("run_event", candidate)
    candidate = event("ports_released")
    candidate["payload"].update(status="failed", output_id=None, ports=[], reason_code="write_failed")
    assert validate_record("run_event", candidate) == candidate
    candidate["payload"]["output_id"] = uid(14)
    with pytest.raises(ContractValidationError, match="release stage"):
        validate_record("run_event", candidate)
    candidate = event("ports_released")
    candidate["payload"]["ports"] = ["final"]
    with pytest.raises(ContractValidationError, match="release stage"):
        validate_record("run_event", candidate)
    candidate = event("workflow_published")
    candidate["payload"]["visible_message_id"] = None
    with pytest.raises(ContractValidationError, match="visible message"):
        validate_record("run_event", candidate)


@pytest.mark.parametrize("value", ["raw exception text", "bad\n", "", "https://api.example.invalid"])
def test_diagnostic_codes_are_safe_symbols_not_exception_bodies(value):
    candidate = event("diagnostic")
    candidate["payload"]["code"] = value
    with pytest.raises(ContractValidationError):
        validate_record("run_event", candidate)


def test_payload_failures_have_no_private_text_or_chained_schema_exception():
    canary = "PRIVATE-CREDENTIAL-CANARY"
    invalid = False
    try:
        validate_core_payload("diagnostic", core_payload_schema_ref("diagnostic"), {"code": canary, "category": canary})
    except ContractValidationError as error:
        invalid = True
        assert canary not in "".join(traceback.format_exception(error))
        assert error.__cause__ is None and error.__context__ is None
    assert invalid


@pytest.mark.parametrize("reference", ["#/$defs/value", "#/%24defs/value", "#/$defs/a~1b", "#named"])
def test_local_schema_refs_use_standard_resolution_including_encoded_fragments(reference):
    schema = {
        "type": "object",
        "$defs": {"value": {"type": "integer"}, "a/b": {"$anchor": "named", "type": "integer"}},
        "properties": {"count": {"$ref": reference}}, "required": ["count"],
        "additionalProperties": False,
    }
    frozen = validate_payload_schema(schema)
    assert frozen == schema and frozen is not schema
    assert validate_payload(frozen, {"count": 1}) == {"count": 1}
    with pytest.raises(ContractValidationError):
        validate_payload(frozen, {"count": True})


@pytest.mark.parametrize("schema", [
    {"type": "not-json-schema"},
    {"$ref": "https://example.invalid/schema"},
    {"$defs": {"hidden": {"$dynamicRef": "another.json#node"}}, "type": "object"},
    {"$ref": "#/$defs/missing"}, {"$ref": "#missing"},
    {"properties": {"unused": {"$ref": "#/$defs/missing"}}, "type": "object"},
    {"$defs": {"value": {"$id": "https://example.invalid", "type": "string"}}},
    {"$defs": {"a": {"$anchor": "same"}, "b": {"$anchor": "same"}}},
    {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"},
    {"$schema": "https://example.invalid/unknown-dialect", "type": "object"},
])
def test_schema_registration_rejects_bad_external_or_unresolved_refs_without_payload_visits(schema):
    with pytest.raises(ContractValidationError):
        validate_payload_schema(schema)


def test_literal_ref_shaped_data_is_not_executed_as_a_schema_reference():
    literal = {"$ref": "https://example.invalid/private"}
    schema = {"type": "object", "properties": {"metadata": {"const": literal}}}
    assert validate_payload_schema(schema) == schema
    assert validate_payload(schema, {"metadata": deepcopy(literal)})["metadata"] == literal


def test_exact_supported_schema_dialect_is_valid_without_loading_remote_resource():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "properties": {"done": {"type": "boolean"}},
    }
    assert validate_payload_schema(schema) == schema
    assert validate_payload(schema, {"done": False}) == {"done": False}
