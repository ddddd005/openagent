"""Structural examples for the public v2 record boundary."""

import copy
import json
from pathlib import Path
import traceback

import pytest
from jsonschema import Draft202012Validator

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import content_digest, loads_strict
from phase1_agent.contracts_v2 import CONTRACT_SCHEMAS, validate_record


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


COMPONENT = uid(1)
DEFINITION = uid(2)
BINDING = uid(3)
SESSION = uid(4)
INPUT = uid(5)
SNAPSHOT = uid(6)
RUN = uid(7)
CHAIN = uid(8)
TURN = uid(9)
REQUEST = uid(10)
MESSAGE = uid(11)
CHECKPOINT = uid(12)
OUTPUT = uid(13)
VISIBLE = uid(14)
GROUP = uid(15)
CALL = uid(16)
EXECUTION = uid(17)
STATE_SNAPSHOT = uid(27)
COMMIT = uid(28)
PARENT_COMMIT = uid(29)
WORKFLOW_REF = uid(30)
SESSION_SELECTION = uid(33)
WORKFLOW_CANDIDATE = uid(31)
OPERATION = uid(32)
EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2"


def schema_ref():
    return {"schema_id": "text", "version": 1}


def envelope():
    return {"owner_component_id": COMPONENT, "schema_version": 1, "payload": {"seed": ["a", 2]}}


def message():
    return {
        "schema_version": 1,
        "message_id": MESSAGE,
        "role": "user",
        "source": {"kind": "human", "visible_message_id": VISIBLE},
        "blocks": [{"kind": "text", "text": "hello"}],
    }


def examples():
    return {
        "node_definition": {
            "schema_version": 1, "component_id": COMPONENT, "component_version": "1.0.0",
            "kind": "io", "capabilities": [], "ports": [
                {"port_id": "in", "direction": "input", "schema_ref": schema_ref(), "required": True},
                {"port_id": "out", "direction": "output", "schema_ref": schema_ref()},
            ],
        },
        "node_binding": {
            "schema_version": 1, "node_binding_id": BINDING,
            "workflow_definition_id": DEFINITION, "workflow_definition_revision": 1,
            "component_id": COMPONENT, "component_version": "1.0.0", "config": envelope(),
        },
        "workflow_definition_revision": {
            "schema_version": 1, "workflow_definition_id": DEFINITION, "revision": 1,
            "bindings": [BINDING], "edges": [],
        },
        "workflow_session": {
            "schema_version": 1, "workflow_session_id": SESSION,
            "workflow_definition_id": DEFINITION, "definition_revision": 1,
            "revision": 1, "source": {"kind": "new"},
        },
        "node_session": {
            "schema_version": 1, "workflow_session_id": SESSION,
            "node_binding_id": BINDING, "data_version": 1, "private_data": envelope(),
        },
        "node_input": {
            "schema_version": 1, "input_id": INPUT, "port_id": "in",
            "payload_schema_ref": schema_ref(),
            "source": {"kind": "visible_message", "visible_message_id": VISIBLE},
            "payload": {"text": "hello"},
        },
        "input_snapshot": {
            "schema_version": 1, "snapshot_id": SNAPSHOT, "workflow_session_id": SESSION,
            "node_binding_id": BINDING, "input_id": INPUT, "parent_turn_id": None,
            "component_id": COMPONENT, "component_version": "1.0.0",
            "config": envelope(), "s0": [message()],
            "tool_definitions": [{"name": "final_answer", "version": "v1",
                                  "parameters_schema": {"type": "object"}}],
            "model_parameters": {"temperature": 0.0}, "output_schema": {"type": "object"},
            "projection_version": 1,
        },
        "run_record": {
            "schema_version": 1, "profile": "agent", "run_id": RUN, "workflow_session_id": SESSION,
            "node_binding_id": BINDING, "chain_run_id": CHAIN, "input_id": INPUT,
            "snapshot_id": SNAPSHOT, "source_run_id": None, "status": "prepared",
            "revision": 1,
            "initial_budget": {"max_model_requests": 4, "max_automatic_retries": 3},
            "result_turn_id": None, "superseded_by_run_id": None,
        },
        "node_run": {
            "schema_version": 1, "profile": "node", "run_id": uid(401),
            "workflow_session_id": SESSION, "node_binding_id": BINDING,
            "chain_run_id": CHAIN, "input_id": INPUT, "input_snapshot_id": None,
            "source_run_id": None, "status": "prepared", "revision": 1,
            "result_output_id": None, "superseded_by_run_id": None,
        },
        "turn": {
            "schema_version": 1, "turn_id": TURN, "parent_turn_id": None,
            "run_id": RUN, "snapshot_id": SNAPSHOT, "input_id": INPUT,
            "messages": [{
                "schema_version": 1, "message_id": uid(21), "role": "assistant",
                "source": {"kind": "model", "request_id": REQUEST},
                "blocks": [{"kind": "text", "text": '{"answer":"yes"}'}],
            }],
            "final": {"message_id": uid(21), "value": {"answer": "yes"}},
            "projection_version": 1,
        },
        "candidate_group": {
            "schema_version": 1, "candidate_group_id": GROUP, "workflow_session_id": SESSION,
            "node_binding_id": BINDING, "logical_input_id": INPUT, "parent_turn_id": None,
            "frozen_snapshot_id": SNAPSHOT, "candidate_refs": [{"run_id": RUN, "turn_id": TURN}],
        },
        "candidate_selection": {
            "schema_version": 1, "candidate_group_id": GROUP,
            "selected_turn_id": TURN, "revision": 1,
        },
        "agent_message": message(),
        "workflow_checkpoint": {
            "schema_version": 1, "checkpoint_id": CHECKPOINT,
            "workflow_session_id": SESSION, "workflow_definition_id": DEFINITION,
            "definition_revision": 1, "revision": 1, "kind": "initial",
            "nodes": [{"node_binding_id": BINDING, "data_version": 1,
                       "private_data": envelope(), "selected_turn_id": None}],
            "selection_refs": [{"candidate_group_id": GROUP, "selected_turn_id": None}],
        },
        "state_snapshot": {
            "schema_version": 1, "state_snapshot_id": STATE_SNAPSHOT,
            "workflow_session_id": SESSION, "workflow_definition_id": DEFINITION,
            "definition_revision": 1,
            "node_states": [{
                "node_binding_id": BINDING, "data_version": 1,
                "private_data": envelope(),
                "selected_result": {"kind": "agent_turn", "turn_id": TURN},
            }],
            "selection_refs": [{"candidate_group_id": GROUP, "selected_turn_id": TURN}],
            "visible_message_refs": [{
                "visible_message_id": VISIBLE, "sequence": 1, "role": "user",
                "boundary": {"before_checkpoint_id": CHECKPOINT, "input_id": INPUT,
                             "input_status": "completed", "chain_run_id": CHAIN},
            }],
            "pending_input_id": None,
        },
        "workflow_commit": {
            "schema_version": 1, "commit_id": COMMIT, "workflow_session_id": SESSION,
            "state_snapshot_id": STATE_SNAPSHOT, "parent_commit_id": PARENT_COMMIT,
            "operation_id": OPERATION, "source": {"kind": "chain_run", "chain_run_id": CHAIN},
        },
        "workflow_ref": {
            "schema_version": 1, "workflow_ref_id": WORKFLOW_REF,
            "workflow_session_id": SESSION, "head_commit_id": COMMIT, "revision": 1,
        },
        "session_selection": {
            "schema_version": 1, "session_selection_id": SESSION_SELECTION,
            "active_workflow_session_id": SESSION, "revision": 1,
        },
        "workflow_candidate": {
            "schema_version": 1, "candidate_id": WORKFLOW_CANDIDATE,
            "workflow_session_id": SESSION, "chain_run_id": CHAIN,
            "base_commit_id": PARENT_COMMIT, "result_commit_id": COMMIT,
            "output_id": OUTPUT, "checkpoint_id": CHECKPOINT,
        },
        "workflow_operation": {
            "schema_version": 1, "operation_id": OPERATION, "kind": "reroll",
            "scope": {"kind": "workflow_session", "id": SESSION},
            "target": {"kind": "chain_run", "id": CHAIN},
            "idempotency_key": "reroll-once",
            "expected_revisions": [
                {"kind": "workflow_session", "id": SESSION, "revision": 1},
                {"kind": "workflow_ref", "id": WORKFLOW_REF, "revision": 1},
            ],
            "expected_head_commit_id": PARENT_COMMIT,
            "payload": {},
        },
        "chain_run": {
            "schema_version": 1, "chain_run_id": CHAIN, "workflow_session_id": SESSION,
            "input_id": INPUT, "status": "prepared", "node_run_ids": [RUN], "output_id": None,
        },
        "chain_input_origin": {
            "schema_version": 1, "chain_run_id": CHAIN,
            "workflow_session_id": SESSION, "visible_message_id": VISIBLE,
            "input_id": INPUT, "source_chain_run_id": uid(402),
        },
        "execution_closeout": {
            "schema_version": 1, "closeout_id": uid(403), "workflow_session_id": SESSION,
            "chain_run_id": CHAIN, "reason": "lost_runtime", "run_ids": [RUN],
            "fact_refs": [{"run_id": RUN, "sequence_count": 0, "digest": content_digest([])}],
            "diagnostic": {"code": "runtime_unavailable", "category": "interrupted"},
        },
        "execution_continuation": {
            "schema_version": 1, "chain_run_id": uid(404), "workflow_session_id": SESSION,
            "source_chain_run_id": CHAIN, "closeout_id": uid(403),
            "source_run_id": RUN, "reused_run_ids": [],
            "evidence_message": {
                "schema_version": 2, "message_id": uid(405), "role": "system",
                "source": {"kind": "runtime_execution_observation",
                           "closeout_id": uid(403), "source_run_id": RUN},
                "blocks": [{"kind": "text", "text": "Prior execution ended; effects remain unknown."}],
            },
        },
        "workflow_output": {
            "schema_version": 1, "output_id": OUTPUT, "workflow_session_id": SESSION,
            "chain_run_id": CHAIN,
            "source": {"kind": "node_run", "node_binding_id": BINDING,
                       "run_id": RUN, "turn_id": TURN},
            "port_id": "out", "payload_schema_ref": schema_ref(), "payload": {"answer": "yes"},
        },
        "output_delivery": {
            "schema_version": 1, "delivery_id": uid(18), "output_id": OUTPUT,
            "target": {"kind": "ui", "workflow_session_id": SESSION},
            "status": "pending", "idempotency_key": "publish-1",
        },
        "visible_message": {
            "schema_version": 1, "visible_message_id": VISIBLE,
            "origin_workflow_session_id": SESSION, "role": "user",
            "payload_schema_ref": schema_ref(), "payload": {"text": "hello"},
            "source": {"kind": "workflow_input", "input_id": INPUT},
        },
        "visible_message_ref": {
            "schema_version": 1, "workflow_session_id": SESSION,
            "visible_message_id": VISIBLE, "sequence": 1, "role": "user",
            "boundary": {"before_checkpoint_id": CHECKPOINT, "input_id": INPUT,
                         "input_status": "pending", "chain_run_id": None},
        },
        "fork_anchor": {
            "schema_version": 1, "fork_anchor_id": uid(26), "source_workflow_session_id": SESSION,
            "visible_message_id": VISIBLE, "role": "user", "checkpoint_id": CHECKPOINT,
            "pending_input": {"input_id": uid(25), "source_visible_message_id": VISIBLE,
                              "port_id": "in", "payload_schema_ref": schema_ref(),
                              "payload": {"text": "hello"}},
        },
        "control_command": {
            "schema_version": 1, "command_id": uid(19),
            "target": {"kind": "run", "workflow_session_id": SESSION, "run_id": RUN},
            "action": "pause", "expected_revision": 1,
        },
        "run_event": {
            "schema_version": 1, "event_id": uid(20), "workflow_session_id": SESSION,
            "node_binding_id": BINDING, "run_id": RUN, "sequence": 1,
            "event_type": "progress", "payload": {"steps": 1},
        },
    }


@pytest.mark.parametrize("record_type", sorted(CONTRACT_SCHEMAS))
def test_every_schema_accepts_json_round_trip_and_returns_detached_copy(record_type):
    Draft202012Validator.check_schema(CONTRACT_SCHEMAS[record_type])
    original = examples()[record_type]
    result = validate_record(record_type, json.loads(json.dumps(original)))
    assert result == original
    assert result is not original
    result["schema_version"] = 99
    assert original["schema_version"] == 1
    if record_type == "node_input":
        result["payload"]["text"] = "changed"
        assert original["payload"]["text"] == "hello"


@pytest.mark.parametrize("record_type", sorted(CONTRACT_SCHEMAS))
def test_every_record_rejects_unknown_public_fields_and_versions(record_type):
    bad = examples()[record_type]
    bad["unexpected"] = True
    with pytest.raises(ContractValidationError):
        validate_record(record_type, bad)
    bad = examples()[record_type]
    bad["schema_version"] = 2
    with pytest.raises(ContractValidationError):
        validate_record(record_type, bad)
    bad["schema_version"] = 1.0
    with pytest.raises(ContractValidationError):
        validate_record(record_type, bad)
    bad["schema_version"] = True
    with pytest.raises(ContractValidationError):
        validate_record(record_type, bad)


@pytest.mark.parametrize("status", [
    "prepared", "running", "pausing", "paused", "failed", "final_ready",
    "succeeded", "superseded", "recovery_unavailable",
])
def test_public_run_statuses(status):
    run = examples()["run_record"]
    run["status"] = status
    validate_record("run_record", run)


def test_initial_budget_is_required_and_strictly_positive_with_three_retries():
    run = examples()["run_record"]
    for budget in (
        None, {},
        {"max_model_requests": 0, "max_automatic_retries": 3},
        {"max_model_requests": True, "max_automatic_retries": 3},
        {"max_model_requests": 1.0, "max_automatic_retries": 3},
        {"max_model_requests": 4, "max_automatic_retries": 2},
        {"max_model_requests": 4, "max_automatic_retries": 3.0},
        {"max_model_requests": 4, "max_automatic_retries": 3, "secret": "x"},
    ):
        bad = copy.deepcopy(run)
        if budget is None:
            bad.pop("initial_budget")
        else:
            bad["initial_budget"] = budget
        with pytest.raises(ContractValidationError):
            validate_record("run_record", bad)


@pytest.mark.parametrize("invalid", ["READY", "archived", "requesting"])
def test_legacy_or_internal_phases_are_not_public_statuses(invalid):
    run = examples()["run_record"]
    run["status"] = invalid
    with pytest.raises(ContractValidationError):
        validate_record("run_record", run)


def test_role_discriminators_and_ui_boundaries():
    records = examples()
    assistant = records["visible_message"]
    assistant.update({
        "role": "assistant", "source": {"kind": "workflow_output", "output_id": OUTPUT},
    })
    validate_record("visible_message", assistant)

    ref = records["visible_message_ref"]
    ref.update({
        "role": "assistant",
        "boundary": {"chain_run_id": CHAIN, "output_id": OUTPUT,
                     "after_checkpoint_id": None},
    })
    validate_record("visible_message_ref", ref)
    ref["boundary"]["after_checkpoint_id"] = CHECKPOINT
    validate_record("visible_message_ref", ref)

    anchor = records["fork_anchor"]
    anchor.pop("pending_input")
    anchor.update({"role": "assistant", "chain_run_id": CHAIN, "output_id": OUTPUT})
    validate_record("fork_anchor", anchor)

    assistant["source"] = {"kind": "workflow_input", "input_id": INPUT}
    with pytest.raises(ContractValidationError):
        validate_record("visible_message", assistant)
    ref["boundary"] = {"before_checkpoint_id": CHECKPOINT, "input_id": INPUT,
                       "input_status": "pending", "chain_run_id": None}
    with pytest.raises(ContractValidationError):
        validate_record("visible_message_ref", ref)
    anchor["pending_input"] = examples()["fork_anchor"]["pending_input"]
    with pytest.raises(ContractValidationError):
        validate_record("fork_anchor", anchor)


def test_user_pending_input_has_no_execution_and_assistant_anchor_requires_output():
    ref = examples()["visible_message_ref"]
    validate_record("visible_message_ref", ref)
    ref["boundary"]["chain_run_id"] = None
    ref["boundary"]["output_id"] = OUTPUT
    with pytest.raises(ContractValidationError):
        validate_record("visible_message_ref", ref)

    ref = examples()["visible_message_ref"]
    ref["boundary"]["input_status"] = "executed"
    with pytest.raises(ContractValidationError):
        validate_record("visible_message_ref", ref)


def test_agent_role_block_compatibility_and_exact_tool_arguments():
    assistant = {
        "schema_version": 1, "message_id": uid(21), "role": "assistant",
        "source": {"kind": "model", "request_id": REQUEST},
        "blocks": [
            {"kind": "text", "text": "running"},
            {"kind": "tool_call", "tool_call_id": CALL, "tool_name": "search",
             "tool_definition_version": "v1", "raw_arguments": '{ "x": 1 }',
             "parsed_arguments": {"x": 1}},
        ],
    }
    validate_record("agent_message", assistant)
    result = {
        "schema_version": 1, "message_id": uid(22), "role": "tool",
        "source": {"kind": "tool", "tool_execution_id": EXECUTION},
        "blocks": [{"kind": "tool_result", "tool_call_id": CALL,
                    "tool_execution_id": EXECUTION, "status": "error",
                    "is_error": True, "content": {"code": "not_found"},
                    "model_visible_text": "Not found"}],
    }
    validate_record("agent_message", result)
    result["blocks"][0] = {"kind": "tool_result", "tool_call_id": CALL,
                           "tool_execution_id": EXECUTION, "status": "unknown",
                           "is_error": False, "model_visible_text": "unknown"}
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", result)
    result["blocks"][0]["status"] = "not_executed"
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", result)
    result["blocks"][0].update({"status": "success", "content": {"ok": True}})
    result["source"]["tool_execution_id"] = uid(24)
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", result)

    for raw, parsed in [
        ('{"x":1,"x":2}', {"x": 2}), ('{"x": NaN}', {"x": 1}),
        ('{"x":1}', {"x": 1.0}), ('{"x":1}', {"x": 2}), ("[]", {}),
    ]:
        bad = copy.deepcopy(assistant)
        bad["blocks"][1].update(raw_arguments=raw, parsed_arguments=parsed)
        with pytest.raises(ContractValidationError):
            validate_record("agent_message", bad)
    for bad_block in [{"kind": "image", "url": "x"}, {"kind": "tool_result"}]:
        bad = copy.deepcopy(assistant)
        bad["blocks"].append(bad_block)
        with pytest.raises(ContractValidationError):
            validate_record("agent_message", bad)
    assistant["role"] = "user"
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", assistant)
    assistant["blocks"] = []
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", assistant)


def test_nested_messages_are_checked_and_snapshot_has_no_credentials():
    snapshot = examples()["input_snapshot"]
    call = {
        "kind": "tool_call", "tool_call_id": CALL, "tool_name": "search",
        "tool_definition_version": "v1", "raw_arguments": '{"q":"x"}',
        "parsed_arguments": {"q": "y"},
    }
    snapshot["s0"] = [{
        "schema_version": 1, "message_id": MESSAGE, "role": "assistant",
        "source": {"kind": "model", "request_id": REQUEST}, "blocks": [call],
    }]
    with pytest.raises(ContractValidationError):
        validate_record("input_snapshot", snapshot)
    snapshot["s0"] = [message()]
    snapshot["config"]["payload"]["api_key"] = "should-not-be-frozen"
    with pytest.raises(ContractValidationError):
        validate_record("input_snapshot", snapshot)
    snapshot["config"]["payload"].pop("api_key")
    snapshot["tool_definitions"][0]["parameters_schema"] = {
        "type": "object", "properties": {"password": {"type": "string"}},
    }
    validate_record("input_snapshot", snapshot)
    snapshot["tool_definitions"][0]["parameters_schema"] = {"type": "not-a-json-schema-type"}
    with pytest.raises(ContractValidationError):
        validate_record("input_snapshot", snapshot)
    snapshot["tool_definitions"][0]["parameters_schema"] = {"type": "object"}
    snapshot["output_schema"] = {"type": "bad-type"}
    with pytest.raises(ContractValidationError):
        validate_record("input_snapshot", snapshot)
    snapshot["output_schema"] = {"type": "object"}
    snapshot["model_parameters"]["client"] = object()
    with pytest.raises(ContractValidationError):
        validate_record("input_snapshot", snapshot)


@pytest.mark.parametrize("invalid", [
    "2026-09-29T08:30:00Z", "2026-09-29T08:30:00.000+00:00",
    "2026-02-30T08:30:00.000Z", "2026-09-29T08:30:00.000z",
])
def test_timestamp_requires_real_utc_milliseconds(invalid):
    record = examples()["node_definition"]
    record["created_at"] = invalid
    with pytest.raises(ContractValidationError):
        validate_record("node_definition", record)


def test_canonical_uuid_and_positive_revisions():
    record = examples()["workflow_session"]
    record["created_at"] = "2026-09-29T08:30:00.000Z"
    validate_record("workflow_session", record)
    for invalid in [uid(10).upper(), COMPONENT.replace("-", ""), "00000000-0000-1000-8000-000000000001"]:
        record["workflow_session_id"] = invalid
        with pytest.raises(ContractValidationError):
            validate_record("workflow_session", record)
    record["workflow_session_id"] = SESSION
    record["revision"] = 0
    with pytest.raises(ContractValidationError):
        validate_record("workflow_session", record)
    record["revision"] = True
    with pytest.raises(ContractValidationError):
        validate_record("workflow_session", record)


def test_exported_patterns_work_without_custom_format_checkers():
    plain = Draft202012Validator(CONTRACT_SCHEMAS["workflow_session"])
    record = examples()["workflow_session"]
    record["created_at"] = "2026-09-29T08:30:00.000Z"
    assert plain.is_valid(record)
    for invalid_id in (uid(10).upper(), uid(10).replace("-", ""),
                       "00000000-0000-1000-8000-000000000001"):
        bad = copy.deepcopy(record)
        bad["workflow_session_id"] = invalid_id
        assert not plain.is_valid(bad)
    for invalid_time in ("2026-09-29T08:30:00Z", "2026-09-29T08:30:00.000+00:00",
                         "2026-09-29T08:30:00.000Z\n"):
        bad = copy.deepcopy(record)
        bad["created_at"] = invalid_time
        assert not plain.is_valid(bad)


def test_frozen_json_rejects_nonfinite_values_custom_containers_cycles_and_surrogates():
    record = examples()["node_input"]
    for payload in (float("nan"), float("inf"), (1, 2), {"bad": object()}, "\ud800"):
        record["payload"] = payload
        with pytest.raises(ContractValidationError):
            validate_record("node_input", record)
    cycle = []
    cycle.append(cycle)
    record["payload"] = cycle
    with pytest.raises(ContractValidationError):
        validate_record("node_input", record)
    with pytest.raises(ContractValidationError):
        validate_record("unknown_record", examples()["node_input"])


def test_nested_unknowns_private_versions_and_explicit_sources():
    binding = examples()["node_binding"]
    binding["config"]["schema_version"] = 0
    with pytest.raises(ContractValidationError):
        validate_record("node_binding", binding)
    binding["config"]["schema_version"] = 1
    binding["config"]["unexpected"] = "not public"
    with pytest.raises(ContractValidationError):
        validate_record("node_binding", binding)
    binding["config"].pop("unexpected")
    binding["config"]["schema_version"] = 1.0
    with pytest.raises(ContractValidationError):
        validate_record("node_binding", binding)

    node_input = examples()["node_input"]
    node_input["source"] = {"kind": "upstream_output", "output_id": OUTPUT}
    validate_record("node_input", node_input)
    node_input["source"] = {"kind": "upstream_output", "visible_message_id": VISIBLE}
    with pytest.raises(ContractValidationError):
        validate_record("node_input", node_input)
    node_input = examples()["node_input"]
    node_input["payload_schema_ref"]["version"] = True
    with pytest.raises(ContractValidationError):
        validate_record("node_input", node_input)
    node_input["payload_schema_ref"]["version"] = 1.0
    with pytest.raises(ContractValidationError):
        validate_record("node_input", node_input)


def test_checkpoint_kinds_and_control_targets():
    checkpoint = examples()["workflow_checkpoint"]
    checkpoint["kind"] = "before_input"
    checkpoint["input_id"] = INPUT
    validate_record("workflow_checkpoint", checkpoint)
    checkpoint.pop("input_id")
    checkpoint["kind"] = "completed_output"
    checkpoint["output_id"] = OUTPUT
    validate_record("workflow_checkpoint", checkpoint)
    checkpoint["input_id"] = INPUT
    with pytest.raises(ContractValidationError):
        validate_record("workflow_checkpoint", checkpoint)

    command = examples()["control_command"]
    command["target"] = {"kind": "fork", "source_workflow_session_id": SESSION,
                         "visible_message_id": VISIBLE}
    command["action"] = "create_and_switch_branch"
    validate_record("control_command", command)
    command["action"] = "resume"
    with pytest.raises(ContractValidationError):
        validate_record("control_command", command)


def test_budget_extension_requires_explicit_positive_increment_and_limit():
    command = examples()["control_command"]
    command["action"] = "extend_budget"
    command["payload"] = {
        "additional_model_requests": 2,
        "max_total_model_requests": 10,
    }
    validate_record("control_command", command)
    for payload in (
        {}, {"additional_model_requests": 1},
        {"additional_model_requests": 0, "max_total_model_requests": 10},
        {"additional_model_requests": 1.0, "max_total_model_requests": 10},
        {"additional_model_requests": 1, "max_total_model_requests": True},
        {"additional_model_requests": 1, "max_total_model_requests": 10, "unknown": 1},
    ):
        bad = copy.deepcopy(command)
        bad["payload"] = payload
        with pytest.raises(ContractValidationError):
            validate_record("control_command", bad)
    command["target"] = {"kind": "fork", "source_workflow_session_id": SESSION,
                         "visible_message_id": VISIBLE}
    with pytest.raises(ContractValidationError):
        validate_record("control_command", command)
    command = examples()["control_command"]
    command["payload"] = {"additional_model_requests": 1, "max_total_model_requests": 10}
    with pytest.raises(ContractValidationError):
        validate_record("control_command", command)


def test_schema_failure_reports_only_record_path_and_keyword():
    secret = "PRIVATE-CREDENTIAL-CANARY"
    record = examples()["visible_message"]
    record["payload"]["text"] = secret
    record["source"] = {"kind": "workflow_output", "output_id": OUTPUT}
    with pytest.raises(ContractValidationError) as captured:
        validate_record("visible_message", record)
    assert str(captured.value) == "visible_message: oneOf"
    assert secret not in "".join(traceback.format_exception(captured.value))
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None

    record = examples()["node_input"]
    record["payload_schema_ref"]["version"] = 0
    with pytest.raises(ContractValidationError, match=r"^node_input\.payload_schema_ref\.version: minimum$"):
        validate_record("node_input", record)
    record = examples()["node_input"]
    record[secret] = secret
    with pytest.raises(ContractValidationError) as captured:
        validate_record("node_input", record)
    assert secret not in str(captured.value)


def test_failed_tool_and_output_schema_errors_do_not_reveal_content():
    secret = "PRIVATE-CREDENTIAL-CANARY"
    snapshot = examples()["input_snapshot"]
    snapshot["output_schema"] = {"type": secret}
    with pytest.raises(ContractValidationError) as captured:
        validate_record("input_snapshot", snapshot)
    assert secret not in "".join(traceback.format_exception(captured.value))
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None

    call = {
        "kind": "tool_call", "tool_call_id": CALL, "tool_name": "search",
        "tool_definition_version": "v1", "raw_arguments": '{"x":"' + secret,
        "parsed_arguments": {},
    }
    message_with_call = {
        "schema_version": 1, "message_id": MESSAGE, "role": "assistant",
        "source": {"kind": "model", "request_id": REQUEST}, "blocks": [call],
    }
    with pytest.raises(ContractValidationError) as captured:
        validate_record("agent_message", message_with_call)
    assert secret not in "".join(traceback.format_exception(captured.value))
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize("location", ["output", "tool"])
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
@pytest.mark.parametrize("reference", [
    "https://example.invalid/schema.json#/$defs/value",
    "other.json#/$defs/value",
    "file:///tmp/other.json",
])
def test_frozen_schemas_reject_external_references_without_resolution(location, keyword, reference):
    snapshot = examples()["input_snapshot"]
    schema = {"$defs": {"nested": {"allOf": [{keyword: reference}]}}, "type": "object"}
    if location == "output":
        snapshot["output_schema"] = schema
    else:
        snapshot["tool_definitions"][0]["parameters_schema"] = schema
    with pytest.raises(ContractValidationError, match="only local fragment references"):
        validate_record("input_snapshot", snapshot)


def test_frozen_schema_profile_accepts_local_refs_and_literal_ref_shaped_data():
    snapshot = examples()["input_snapshot"]
    snapshot["output_schema"] = {
        "$defs": {"value": {"$dynamicAnchor": "value", "type": "string"}},
        "type": "object",
        "properties": {
            "answer": {"$ref": "#/$defs/value"},
            "another": {"$dynamicRef": "#value"},
            "metadata": {"const": {"$ref": "https://example.invalid/literal-data"}},
        },
    }
    snapshot["tool_definitions"][0]["parameters_schema"] = {
        "$defs": {"term": {"type": "string"}},
        "type": "object", "properties": {"query": {"$ref": "#/$defs/term"}},
    }
    validate_record("input_snapshot", snapshot)


def test_saved_example_documents_validate_under_public_schemas():
    paths = sorted(EXAMPLES.glob("*.json"))
    assert paths
    for path in paths:
        bundle = loads_strict(path.read_text(encoding="utf-8"))
        assert set(bundle) <= set(CONTRACT_SCHEMAS)
        for record_type, records in bundle.items():
            for record in records:
                assert validate_record(record_type, record) == record


def test_generic_node_run_does_not_require_agent_snapshot_or_turn():
    record = {
        "schema_version": 1,
        "profile": "node",
        "run_id": uid(401),
        "workflow_session_id": SESSION,
        "node_binding_id": BINDING,
        "chain_run_id": CHAIN,
        "input_id": INPUT,
        "input_snapshot_id": None,
        "source_run_id": None,
        "status": "succeeded",
        "revision": 2,
        "result_output_id": OUTPUT,
        "superseded_by_run_id": None,
    }
    assert validate_record("node_run", record) == record
    record["status"] = "final_ready"
    with pytest.raises(ContractValidationError):
        validate_record("node_run", record)


def test_chain_run_uses_workflow_status_not_agent_private_status():
    record = examples()["chain_run"]
    record["status"] = "final_ready"
    with pytest.raises(ContractValidationError):
        validate_record("chain_run", record)
    record["status"] = "paused"
    assert validate_record("chain_run", record) == record


def test_state_snapshot_freezes_typed_results_and_role_specific_ui_boundaries():
    snapshot = examples()["state_snapshot"]
    snapshot["node_states"][0]["selected_result"] = None
    validate_record("state_snapshot", snapshot)
    snapshot["node_states"][0]["selected_result"] = {
        "kind": "node_output", "output_id": OUTPUT,
    }
    validate_record("state_snapshot", snapshot)
    snapshot["visible_message_refs"][0].update({
        "role": "assistant",
        "boundary": {"chain_run_id": CHAIN, "output_id": OUTPUT,
                     "after_checkpoint_id": CHECKPOINT},
    })
    validate_record("state_snapshot", snapshot)
    for bad_result in (
        {"kind": "agent_turn", "output_id": OUTPUT},
        {"kind": "node_output", "turn_id": TURN},
        {"kind": "input_snapshot", "snapshot_id": SNAPSHOT},
    ):
        bad = copy.deepcopy(snapshot)
        bad["node_states"][0]["selected_result"] = bad_result
        with pytest.raises(ContractValidationError):
            validate_record("state_snapshot", bad)
    snapshot["visible_message_refs"][0]["boundary"] = {
        "before_checkpoint_id": CHECKPOINT, "input_id": INPUT,
        "input_status": "pending", "chain_run_id": None,
    }
    with pytest.raises(ContractValidationError):
        validate_record("state_snapshot", snapshot)


def test_commit_sources_parent_chain_and_two_snapshot_identifiers_are_distinct():
    commit = examples()["workflow_commit"]
    for source in (
        {"kind": "session_seed", "workflow_session_id": SESSION},
        {"kind": "candidate", "candidate_id": WORKFLOW_CANDIDATE},
        {"kind": "control_operation", "operation_id": OPERATION},
    ):
        commit["source"] = source
        validate_record("workflow_commit", commit)
    commit["parent_commit_id"] = None
    validate_record("workflow_commit", commit)
    commit["parent_turn_id"] = TURN
    with pytest.raises(ContractValidationError):
        validate_record("workflow_commit", commit)
    commit.pop("parent_turn_id")
    commit["state_snapshot_id"] = SNAPSHOT.replace("-4000-", "-1000-")
    with pytest.raises(ContractValidationError):
        validate_record("workflow_commit", commit)
    snapshot = examples()["state_snapshot"]
    snapshot["snapshot_id"] = SNAPSHOT
    with pytest.raises(ContractValidationError):
        validate_record("state_snapshot", snapshot)


def test_workflow_ref_empty_head_and_candidate_require_complete_typed_refs():
    ref = examples()["workflow_ref"]
    ref["head_commit_id"] = None
    validate_record("workflow_ref", ref)
    ref["revision"] = 0
    with pytest.raises(ContractValidationError):
        validate_record("workflow_ref", ref)
    candidate = examples()["workflow_candidate"]
    candidate.pop("output_id")
    with pytest.raises(ContractValidationError):
        validate_record("workflow_candidate", candidate)
    candidate["output_id"] = OUTPUT
    candidate["result_commit_id"] = TURN.replace("-4000-", "-1000-")
    with pytest.raises(ContractValidationError):
        validate_record("workflow_candidate", candidate)


def test_workflow_operation_record_matches_pure_control_envelope():
    operation = examples()["workflow_operation"]
    operation["created_at"] = "2026-09-29T08:30:00.000Z"
    validate_record("workflow_operation", operation)
    for change in (
        lambda r: r["expected_revisions"][0].update(id=uid(999)),
        lambda r: r["expected_revisions"][0].update(revision=True),
        lambda r: r["expected_revisions"].append(r["expected_revisions"][0]),
        lambda r: r["target"].update(kind="run_record"),
        lambda r: r.update(generation=1),
        lambda r: r.update(payload={"generation": 1}),
        lambda r: r.update(expected_head_commit_id="bad"),
    ):
        bad = copy.deepcopy(operation)
        change(bad)
        with pytest.raises(ContractValidationError):
            validate_record("workflow_operation", bad)


def test_initial_budget_accepts_old_records_without_inventing_attempt_allowance():
    run = examples()["run_record"]
    assert validate_record("run_record", run) == run
    assert "max_model_attempts" not in run["initial_budget"]
    run["initial_budget"].update(max_model_requests=64, max_model_attempts=256)
    assert validate_record("run_record", run) == run


@pytest.mark.parametrize("field,value", [
    ("max_model_requests", 65), ("max_model_requests", 0),
    ("max_model_attempts", 257), ("max_model_attempts", 0),
    ("max_model_attempts", True), ("max_model_attempts", 1.0),
])
def test_initial_budget_limits_requests_and_attempts_separately(field, value):
    run = examples()["run_record"]
    run["initial_budget"][field] = value
    with pytest.raises(ContractValidationError):
        validate_record("run_record", run)
