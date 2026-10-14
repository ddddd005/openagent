"""Uninstalled M3 proofs over accepted model facts, without current resource I/O.

The caller obtains fact sets through the trusted live/history reader. A stream
argument identifies that reader's verified persistence coordinates; fact JSON
alone cannot prove its database membership. Transport resolution supplies pure
projectors and estimators only, and never constructs a transport or a lease.
"""

from __future__ import annotations

from copy import deepcopy

from .context_compaction import (
    compaction_instruction, is_compactable_text, replace_compacted_messages,
)
from .context_compaction_policy import DEFAULT_COMPACTION_PROMPT, frame_checkpoint
from .context_contract import artifact_ref
from .contract_errors import require, uuid4_string
from .contract_graph import (
    validate_context_message_history, validate_pending_context_message_history,
)
from .contract_json import validate_json_value
from .contracts_v2 import CONTRACT_SCHEMAS, _check_snapshot_schemas, validate_record
from .model_request_budget import (
    measure_context, measure_prepared_request, validate_context_reduction,
)
from .model_request_contracts import (
    MAX_SAFE_INTEGER, _validate, digest_schema, validate_compaction_policy,
    validate_model_binding, validate_model_result, validate_model_settings,
    validate_operation_payload, validate_transport_capability,
)
from .runtime_hosting import InvocationOwner


AGENT_EXECUTOR_REF_V2 = {"executor_id": "agents.snapshot-kernel", "exact_version": "2.0.0"}
_STAGES = ("request", "attempt", "outcome")
_REQUEST_FIELDS = {
    "request_id", "request_index", "messages", "tools", "settings_id", "reserved_model_fact_ids",
}
_FACT_FIELDS = {
    "schema_version", "kind", "fact_id", "session_id", "chain_run_id", "node_binding_id",
    "node_run_id", "binding_id", "request_id", "stage", "sequence", "details",
}
_REQUEST_DETAIL_FIELDS = {
    "messages", "tools", "input_refs", "wire_request", "projection", "settings",
    "preparation_id", "intent_digest", "wire_digest", "measurement",
}


def _fields(value, fields, code="model_execution_invalid"):
    require(type(value) is dict and set(value) == set(fields), code,
            "Model execution evidence fields differ from its exact contract")


def _ids(values, *, maximum=4096, nonempty=True):
    require(type(values) is list and len(values) <= maximum and (bool(values) or not nonempty),
            "model_execution_invalid", "Evidence identities require a bounded ordered list")
    for identity in values:
        uuid4_string(identity)
    require(len(values) == len(set(values)), "model_execution_duplicate_identity",
            "Evidence identities cannot repeat")


def model_request_stream_id(request_id):
    return "service:workflow.models@2.0.0:model:" + uuid4_string(request_id)


def input_snapshot_v2_schema():
    schema = deepcopy(CONTRACT_SCHEMAS["input_snapshot"])
    schema["properties"]["schema_version"] = {"type": "integer", "const": 2}
    schema["properties"].pop("model_parameters")
    schema["required"].remove("model_parameters")
    return schema


def validate_input_snapshot_v2(value):
    """Validate fixed public inputs, with no whole-run model parameters/capacity."""
    value = _validate(value, input_snapshot_v2_schema(), "agent_snapshot_invalid")
    for field in ("snapshot_id", "workflow_session_id", "node_binding_id", "input_id", "component_id"):
        uuid4_string(value[field])
    if value["parent_turn_id"] is not None:
        uuid4_string(value["parent_turn_id"])
    require(value["component_version"] == "public-3"
            and value["config"]["owner_component_id"] == value["component_id"],
            "agent_snapshot_invalid", "Snapshot requires its exact public component owner")
    payload = value["config"]["payload"]
    _fields(payload, {
        "public_agent", "executor_ref", "frozen_prompt_ref", "model_ref", "public_model_binding",
        "frozen_compaction_policy_ref", "context_compaction",
    }, "agent_snapshot_invalid")
    require(payload["public_agent"] == {} and payload["executor_ref"] == AGENT_EXECUTOR_REF_V2,
            "agent_snapshot_invalid", "Snapshot requires the precise Agent executor and configuration")
    artifact_ref(payload["frozen_prompt_ref"])
    artifact_ref(payload["model_ref"])
    validate_model_binding(payload["public_model_binding"])
    descriptor = payload["context_compaction"]
    _fields(descriptor, {"policy", "layout"}, "agent_snapshot_invalid")
    if descriptor["policy"] is None:
        require(payload["frozen_compaction_policy_ref"] is None, "agent_snapshot_invalid",
                "Absent policy cannot claim an accepted policy artifact")
    else:
        validate_compaction_policy(descriptor["policy"])
        artifact_ref(payload["frozen_compaction_policy_ref"])
    require(type(value["s0"]) is list and 0 < len(value["s0"]) <= 4096,
            "agent_snapshot_invalid", "Snapshot requires a bounded canonical initial view")
    validate_context_message_history(value["s0"])
    layout = descriptor["layout"]
    require(type(layout) is list and len(layout) == len(value["s0"]), "agent_snapshot_invalid",
            "Layout must retain every frozen message position")
    for item in layout:
        require(type(item) is dict and set(item) in (
            {"kind", "round_id"}, {"kind", "round_id", "compaction"},
        ) and item["kind"] in ("fixed", "history", "summary", "once", "current")
            and (item["round_id"] is None or type(item["round_id"]) is str and bool(item["round_id"]))
            and item.get("compaction", "allowed") in ("allowed", "never"),
            "agent_snapshot_invalid", "Frozen layout fields or range are invalid")
    names = [tool["name"] for tool in value["tool_definitions"]]
    require(len(names) == len(set(names)) and len(names) <= 128, "agent_snapshot_invalid",
            "Frozen tools require distinct names within the request budget")
    _check_snapshot_schemas(value)
    return deepcopy(value)


def validate_agent_receipts_v2(value):
    validate_json_value(value)
    _fields(value, {
        "schema_version", "kind", "owner", "executor_ref", "snapshot_id", "frozen_prompt_ref",
        "model_ref", "compaction_policy_ref", "unit_id", "fact_ids", "model_requests",
    }, "agent_invalid_receipts")
    require(type(value["schema_version"]) is int and value["schema_version"] == 2
            and value["kind"] == "workflow.agent-receipts"
            and value["executor_ref"] == AGENT_EXECUTOR_REF_V2,
            "agent_invalid_receipts", "Receipts require schema 2 and the exact Agent executor")
    InvocationOwner.from_dict(value["owner"])
    for field in ("snapshot_id", "unit_id"):
        uuid4_string(value[field])
    artifact_ref(value["frozen_prompt_ref"])
    artifact_ref(value["model_ref"])
    if value["compaction_policy_ref"] is not None:
        artifact_ref(value["compaction_policy_ref"])
    _ids(value["fact_ids"])
    requests = value["model_requests"]
    require(type(requests) is list and 0 < len(requests) <= 4096, "agent_invalid_receipts",
            "Receipts require their ordered accepted model requests")
    ids, settings_ids, fact_ids = [], [], []
    for item in requests:
        _fields(item, {"request_id", "settings_id", "fact_ids"}, "agent_invalid_receipts")
        ids.append(uuid4_string(item["request_id"]))
        settings_ids.append(uuid4_string(item["settings_id"]))
        _ids(item["fact_ids"], maximum=3)
        require(len(item["fact_ids"]) == 3, "agent_invalid_receipts",
                "Accepted model requests retain request/attempt/outcome facts")
        fact_ids.extend(item["fact_ids"])
    require(len(ids) == len(set(ids)) and len(settings_ids) == len(set(settings_ids))
            and len(fact_ids) == len(set(fact_ids)), "agent_invalid_receipts",
            "Model requests, settings and accepted fact identities cannot repeat")
    return deepcopy(value)


def validate_model_execution_request(request):
    validate_json_value(request)
    _fields(request, _REQUEST_FIELDS)
    uuid4_string(request["request_id"])
    uuid4_string(request["settings_id"])
    require(type(request["request_index"]) is int and 1 <= request["request_index"] <= MAX_SAFE_INTEGER
            and type(request["messages"]) is list and 0 < len(request["messages"]) <= 4096
            and type(request["tools"]) is list and len(request["tools"]) <= 128,
            "model_execution_invalid", "Request requires an index and complete bounded canonical input")
    validate_context_message_history(request["messages"])
    _fields(request["reserved_model_fact_ids"], _STAGES)
    _ids([request["reserved_model_fact_ids"][stage] for stage in _STAGES], maximum=3)
    return deepcopy(request)


def _transport(settings, resolve_transport, *, tools):
    require(callable(resolve_transport), "model_execution_projector_unavailable",
            "Historical proof requires a trusted pure transport declaration")
    definition = resolve_transport(settings["protocol"], settings["parameters"]["model"])
    require(callable(getattr(definition, "projector", None)),
            "model_execution_projector_unavailable", "Historical projector is unavailable")
    capability = validate_transport_capability(
        definition.capability, protocol=settings["protocol"], model=settings["parameters"]["model"],
        thinking=settings["parameters"]["thinking"], requires_tools=bool(tools))
    require((capability["output_limit_tokens"] is None
             or settings["budget"]["normal_output_limit_tokens"] <= capability["output_limit_tokens"])
            and settings["capabilities"]["tools"] == capability["tools"],
            "model_execution_settings_mismatch", "Historical settings differ from their transport capability")
    return definition


def _estimator(definition):
    return {"counter": definition.counter, "estimator_id": definition.estimator_id,
            "estimator_version": definition.estimator_version}


def _project(messages, tools, parameters, *, project_messages, definition):
    require(callable(project_messages), "model_execution_projector_unavailable",
            "Canonical messages require their trusted boundary projection")
    projected = project_messages(deepcopy(messages))
    prepared = definition.projector(deepcopy(projected), deepcopy(tools), deepcopy(parameters))
    require(type(prepared) is dict and prepared.get("projection") == definition.projection_version,
            "model_execution_wire_mismatch", "Projection differs from the exact captured declaration")
    return projected, prepared


def prove_model_request(request, facts, *, owner, binding, stream_id, project_messages,
                        resolve_transport, input_refs=None):
    """Prove one accepted request, including its original wire, estimate and result.

    M2 retains an intent_digest but does not publish its logical request_key.
    That digest has a strict format here; original input equality is established
    from the accepted request messages/tools, settings, owner and actual wire.
    """
    request = validate_model_execution_request(request)
    owner = InvocationOwner.from_dict(owner).to_dict()
    binding = validate_model_binding(binding)
    validate_json_value(facts)
    require(stream_id == model_request_stream_id(request["request_id"])
            and type(facts) is list and len(facts) == 3,
            "model_execution_fact_mismatch", "Proof requires the exact accepted model service stream")
    coordinates = {
        "session_id": owner["workflow_session_id"], "chain_run_id": owner["chain_run_id"],
        "node_binding_id": owner["node_binding_id"], "node_run_id": owner["node_run_id"],
        "binding_id": binding["binding_id"], "request_id": request["request_id"],
    }
    for index, (stage, fact) in enumerate(zip(_STAGES, facts), start=1):
        _fields(fact, _FACT_FIELDS, "model_execution_fact_mismatch")
        require(type(fact["schema_version"]) is int and fact["schema_version"] == 2
                and fact["kind"] == "workflow.model-fact" and fact["stage"] == stage
                and type(fact["sequence"]) is int and fact["sequence"] == index
                and fact["fact_id"] == request["reserved_model_fact_ids"][stage]
                and all(fact[key] == expected for key, expected in coordinates.items()),
                "model_execution_fact_mismatch", "Model fact owner, phase or reserved identity differs")
    detail, attempt, outcome = (fact["details"] for fact in facts)
    _fields(detail, _REQUEST_DETAIL_FIELDS, "model_execution_fact_mismatch")
    _fields(attempt, {"attempt_id", "attempt_index", "dispatch_state", "attempts_consumed"},
            "model_execution_fact_mismatch")
    uuid4_string(attempt["attempt_id"])
    require(type(attempt["attempt_index"]) is int and attempt["attempt_index"] == 1
            and type(attempt["attempts_consumed"]) is int and attempt["attempts_consumed"] == 1
            and attempt["dispatch_state"] == "intent",
            "model_execution_fact_mismatch", "A model request must have one original attempt")
    settings = validate_model_settings(detail["settings"])
    require(settings["request_id"] == request["request_id"]
            and settings["settings_id"] == request["settings_id"]
            and settings["binding_id"] == binding["binding_id"]
            and settings["configuration"]["reference"] == binding["reference"]
            and settings["purpose"] in binding["authorization"]["purposes"]
            and detail["tools"] == request["tools"]
            and type(detail["input_refs"]) is dict
            and (input_refs is None or detail["input_refs"] == input_refs),
            "model_execution_settings_mismatch", "Request differs from its accepted input and settings")
    uuid4_string(detail["preparation_id"])
    _validate(detail["intent_digest"], digest_schema(), "model_execution_fact_mismatch")
    definition = _transport(settings, resolve_transport, tools=request["tools"])
    projected, prepared = _project(
        request["messages"], request["tools"], settings["parameters"],
        project_messages=project_messages, definition=definition)
    measured = measure_prepared_request(prepared, settings, **_estimator(definition))
    require(projected == detail["messages"] and prepared["wire_request"] == detail["wire_request"]
            and detail["projection"] == measured["projection"]["projection_version"]
            and detail["wire_digest"] == measured["projection"]["wire_digest"]
            and detail["measurement"] == measured["measurement"]
            and measured["hard_capacity_ok"],
            "model_execution_wire_mismatch", "Accepted request does not match its actual wire and measurement")
    require(type(outcome) is dict and outcome.get("attempt_id") == attempt["attempt_id"],
            "model_execution_fact_mismatch", "Outcome belongs to another original attempt")
    classification = outcome.get("classification")
    result = None
    if classification == "response_received":
        _fields(outcome, {"classification", "response", "attempt_id"}, "model_execution_fact_mismatch")
        require(type(outcome["response"]) is dict and "fact_refs" not in outcome["response"],
                "model_execution_fact_mismatch", "Outcome retains the original response without receipt claims")
        result = validate_model_result({
            **outcome["response"], "fact_refs": [{"fact_id": fact["fact_id"]} for fact in facts],
        }, settings=settings)
    else:
        require(classification in ("not_dispatched", "provider_error", "dispatch_unknown", "response_invalid"),
                "model_execution_fact_mismatch", "Outcome classification is unsupported")
        allowed = {"classification", "attempt_id"}
        if classification == "not_dispatched":
            allowed.add("diagnostic")
            _fields(outcome.get("diagnostic"), {"cause_code", "step", "request_id", "settings_id"},
                    "model_execution_fact_mismatch")
            require(outcome["diagnostic"]["request_id"] == request["request_id"]
                    and outcome["diagnostic"]["settings_id"] == request["settings_id"],
                    "model_execution_fact_mismatch", "Dispatch diagnostic names another request")
        elif classification == "provider_error" and "status_code" in outcome:
            allowed.add("status_code")
            require(type(outcome["status_code"]) is int and 100 <= outcome["status_code"] <= 599,
                    "model_execution_fact_mismatch", "Provider status must be an explicit HTTP status")
        _fields(outcome, allowed, "model_execution_fact_mismatch")
    return {"request": request, "settings": settings, "measurement": measured,
            "facts": deepcopy(facts), "result": result, "classification": classification}


def snapshot_wire_tools(snapshot):
    snapshot = validate_input_snapshot_v2(snapshot)
    return [{"type": "function", "function": {
        "name": tool["name"], "description": tool.get("description", ""),
        "parameters": deepcopy(tool["parameters_schema"]),
    }} for tool in snapshot["tool_definitions"]]


def compaction_eligible_message_ids(snapshot, messages, accepted_messages=()):
    snapshot = validate_input_snapshot_v2(snapshot)
    validate_context_message_history(messages)
    require(type(accepted_messages) in (list, tuple) and len(accepted_messages) <= 4096,
            "model_execution_append_unproven", "Accepted increments require a bounded canonical sequence")
    accepted = [validate_record("agent_message", message) for message in accepted_messages]
    initial_ids = {message["message_id"] for message in snapshot["s0"]}
    current = {message["message_id"]: message for message in messages}
    accepted_ids = [message["message_id"] for message in accepted]
    require(len(accepted_ids) == len(set(accepted_ids))
            and not initial_ids.intersection(accepted_ids)
            and all(message["message_id"] not in current or current[message["message_id"]] == message
                    for message in accepted),
            "model_execution_append_unproven", "Accepted increments cannot impersonate frozen or changed messages")
    descriptor = snapshot["config"]["payload"]["context_compaction"]
    policy = descriptor["policy"]
    if policy is None:
        return []
    layout = {message["message_id"]: item for message, item in zip(snapshot["s0"], descriptor["layout"])}
    rounds = []
    for message in messages:
        item = layout.get(message["message_id"])
        if item and item["kind"] == "history" and item["round_id"] is not None and item["round_id"] not in rounds:
            rounds.append(item["round_id"])
    depth = policy["keep_depth"]
    retained = set(rounds[-depth:]) if depth else set()
    generated = {message["message_id"] for message in accepted
                 if message["role"] == "assistant" and message["source"]["kind"] == "model"
                 and is_compactable_text(message)}
    return [message["message_id"] for message in messages if is_compactable_text(message) and (
        message.get("source", {}).get("kind") == "context_checkpoint"
        or (item := layout.get(message["message_id"])) and item["kind"] == "summary"
        or message["message_id"] in generated or (
            (item := layout.get(message["message_id"])) and item["kind"] in ("history", "summary", "once")
            and item.get("compaction", "allowed") == "allowed" and item["round_id"] not in retained))]


def expected_compaction_request(snapshot, before_messages, covered_message_ids, *,
                                compaction_id, instruction_message_id, accepted_messages=()):
    snapshot = validate_input_snapshot_v2(snapshot)
    policy = snapshot["config"]["payload"]["context_compaction"]["policy"]
    require(policy is not None and policy["enabled"], "model_compaction_invalid",
            "Maintenance requires its enabled accepted policy")
    require(covered_message_ids == compaction_eligible_message_ids(snapshot, before_messages, accepted_messages)
            and bool(covered_message_ids), "model_compaction_range_mismatch",
            "Maintenance must cover exactly the permitted historical text")
    uuid4_string(compaction_id)
    uuid4_string(instruction_message_id)
    require(instruction_message_id not in {message["message_id"] for message in before_messages},
            "model_execution_duplicate_identity", "Maintenance instruction requires a fresh message identity")
    prompt = policy["summary_prompt"] if policy["summary_prompt"].strip() else DEFAULT_COMPACTION_PROMPT
    return deepcopy(before_messages) + [compaction_instruction(
        compaction_id, instruction_message_id, prompt, covered_message_ids, before_messages)]


def validate_compaction_proof(operation, *, snapshot, owner, model_facts, project_messages,
                              resolve_transport, accepted_messages=()):
    """Prove trigger, original summary, replacement and same-settings reduction."""
    validate_json_value(operation)
    _fields(operation, {
        "kind", "compaction_id", "before_message_ids", "covered_message_ids", "checkpoint",
        "after_message_ids", "request", "result", "capacity", "trigger_decision",
    }, "model_compaction_invalid")
    require(operation["kind"] == "compact", "model_compaction_invalid", "Unsupported maintenance operation")
    snapshot = validate_input_snapshot_v2(snapshot)
    owner = InvocationOwner.from_dict(owner).to_dict()
    require(snapshot["workflow_session_id"] == owner["workflow_session_id"]
            and snapshot["node_binding_id"] == owner["node_binding_id"],
            "model_execution_owner_mismatch", "Snapshot and proof belong to different Agent owners")
    binding = snapshot["config"]["payload"]["public_model_binding"]
    for field in ("before_message_ids", "covered_message_ids", "after_message_ids"):
        _ids(operation[field])
    request = validate_model_execution_request(operation["request"])
    before = request["messages"][:-1]
    ordinary_tools = snapshot_wire_tools(snapshot)
    require(operation["before_message_ids"] == [message["message_id"] for message in before]
            and request["tools"] in ([], ordinary_tools),
            "model_compaction_invalid", "Maintenance must preserve its complete view and frozen tools")
    instruction = request["messages"][-1]
    expected = expected_compaction_request(
        snapshot, before, operation["covered_message_ids"], compaction_id=operation["compaction_id"],
        instruction_message_id=instruction["message_id"], accepted_messages=accepted_messages)
    require(request["messages"] == expected, "model_compaction_invalid",
            "Maintenance request differs from its exact accepted policy instruction")
    require(type(model_facts) is dict and request["request_id"] in model_facts,
            "model_execution_fact_mismatch", "Summary requires its exact accepted model facts")
    proof = prove_model_request(
        request, model_facts[request["request_id"]], owner=owner, binding=binding,
        stream_id=model_request_stream_id(request["request_id"]),
        project_messages=project_messages, resolve_transport=resolve_transport)
    settings, result = proof["settings"], proof["result"]
    require(settings["purpose"] == "compaction" and result is not None and operation["result"] == result
            and result["finish_reason"] == "stop" and result["tool_calls"] == []
            and type(result["content"]) is str and bool(result["content"].strip()),
            "model_compaction_summary_invalid", "Maintenance requires its accepted nonempty plain-text response")
    decision = operation["trigger_decision"]
    _fields(decision, {"decision_id", "preparation", "discard"}, "model_compaction_invalid")
    uuid4_string(decision["decision_id"])
    trigger = validate_operation_payload("prepare-request", decision["preparation"], response=True)
    discard = validate_operation_payload("discard-prepared-request", decision["discard"], response=True)
    trigger_settings = trigger["settings"]
    require(trigger_settings["purpose"] == "normal" and trigger["requires_compaction"]
            and trigger_settings["binding_id"] == binding["binding_id"]
            and trigger_settings["configuration"]["reference"] == binding["reference"]
            and discard["request_id"] == trigger["request_id"]
            and trigger["request_id"] != request["request_id"]
            and not set(trigger["reserved_model_fact_ids"].values()).intersection(
                fact["fact_id"] for facts in model_facts.values() for fact in facts),
            "model_compaction_trigger_invalid", "Trigger must name its original discarded normal preparation")
    trigger_definition = _transport(trigger_settings, resolve_transport, tools=ordinary_tools)
    _, trigger_prepared = _project(
        before, ordinary_tools, trigger_settings["parameters"],
        project_messages=project_messages, definition=trigger_definition)
    trigger_measurement = measure_prepared_request(
        trigger_prepared, trigger_settings, **_estimator(trigger_definition))
    require(all(trigger[key] == trigger_measurement[key] for key in trigger_measurement),
            "model_compaction_trigger_invalid", "Trigger differs from its original ordinary wire measurement")
    checkpoint = validate_record("agent_message", operation["checkpoint"])
    require(checkpoint["schema_version"] == 4 and checkpoint["role"] == "user"
            and checkpoint["source"] == {
                "kind": "context_checkpoint", "compaction_id": operation["compaction_id"],
            } and checkpoint["blocks"] == [{"kind": "text", "text": frame_checkpoint(result["content"])}]
            and checkpoint["message_id"] not in operation["before_message_ids"]
            and checkpoint["message_id"] != instruction["message_id"],
            "model_compaction_invalid", "Checkpoint must derive from the exact summary with a fresh identity")
    after = replace_compacted_messages(before, operation["covered_message_ids"], checkpoint)
    require(operation["after_message_ids"] == [message["message_id"] for message in after],
            "model_compaction_invalid", "Maintenance must replace selected text without moving retained messages")
    _fields(operation["capacity"], {"before", "after"}, "model_compaction_invalid")
    definition = _transport(settings, resolve_transport, tools=ordinary_tools)
    parameters = {**settings["parameters"], "max_tokens": settings["budget"]["normal_output_limit_tokens"]}
    for stage, messages in (("before", before), ("after", after)):
        projected, prepared = _project(
            messages, ordinary_tools, parameters,
            project_messages=project_messages, definition=definition)
        evidence = measure_context(prepared, settings, messages=projected, tools=ordinary_tools,
                                   **_estimator(definition))
        require(operation["capacity"][stage] == evidence, "model_compaction_measurement_invalid",
                "Context proof must retain the service's exact same-settings ordinary-reserve measurement")
    validate_context_reduction(operation["capacity"]["before"], operation["capacity"]["after"], settings)
    return deepcopy(operation)


def replay_model_context_operations(snapshot, operations, *, owner, model_facts, project_messages,
                                    resolve_transport, accepted_messages=None):
    """Replay immutable historical proof; never read today's configuration."""
    snapshot = validate_input_snapshot_v2(snapshot)
    owner = InvocationOwner.from_dict(owner).to_dict()
    require(snapshot["workflow_session_id"] == owner["workflow_session_id"]
            and snapshot["node_binding_id"] == owner["node_binding_id"],
            "model_execution_owner_mismatch", "Replay requires its exact snapshot owner")
    validate_json_value(operations)
    require(type(operations) is list and len(operations) <= 4096,
            "model_execution_invalid", "Replay requires a bounded ordered operation timeline")
    messages, appended, compactions = deepcopy(snapshot["s0"]), [], []
    seen = {message["message_id"] for message in messages}
    seen_compactions = {message["source"]["compaction_id"] for message in messages
                        if message.get("source", {}).get("kind") == "context_checkpoint"}
    pending = []
    for operation in operations:
        if type(operation) is dict and operation.get("kind") == "append":
            _fields(operation, {"kind", "message"})
            message = validate_record("agent_message", operation["message"])
            require(message["message_id"] not in seen and message["source"]["kind"] not in (
                "context_checkpoint", "context_compaction_instruction"),
                "model_execution_duplicate_identity", "Ordinary appends cannot reuse or insert maintenance identities")
            messages.append(message)
            appended.append(message)
            seen.add(message["message_id"])
            pending = []
            for item in messages:
                for block in item["blocks"]:
                    if block["kind"] == "tool_call":
                        pending.append(block["tool_call_id"])
                    elif block["kind"] == "tool_result":
                        require(pending and pending[0] == block["tool_call_id"],
                                "model_execution_tool_unclosed", "Tool settlement must retain accepted call order")
                        pending.pop(0)
            if pending:
                validate_pending_context_message_history(messages, pending)
            else:
                validate_context_message_history(messages)
            continue
        require(not pending, "model_execution_tool_unclosed", "Maintenance requires a closed tool boundary")
        value = validate_compaction_proof(
            operation, snapshot=snapshot, owner=owner, model_facts=model_facts,
            project_messages=project_messages, resolve_transport=resolve_transport,
            accepted_messages=appended)
        require(value["before_message_ids"] == [message["message_id"] for message in messages]
                and value["request"]["messages"][:-1] == messages,
                "model_compaction_order_mismatch", "Maintenance must use the exact preceding effective view")
        require(value["compaction_id"] not in seen_compactions
                and value["checkpoint"]["message_id"] not in seen
                and value["request"]["messages"][-1]["message_id"] not in seen,
                "model_execution_duplicate_identity", "Historical maintenance identities cannot be reused")
        seen_compactions.add(value["compaction_id"])
        seen.update((value["checkpoint"]["message_id"], value["request"]["messages"][-1]["message_id"]))
        messages = replace_compacted_messages(messages, value["covered_message_ids"], value["checkpoint"])
        compactions.append(value["compaction_id"])
    require(not pending, "model_execution_tool_unclosed", "Replay cannot adopt an unfinished tool batch")
    require(not appended or accepted_messages is not None, "model_execution_append_unproven",
            "Replay appends require the accepted canonical executor message evidence")
    if accepted_messages is not None:
        require(appended == accepted_messages, "model_execution_append_unproven",
                "Replay appends must equal the accepted canonical executor messages")
    return {"messages": deepcopy(messages), "compaction_ids": compactions,
            "appended_message_ids": [message["message_id"] for message in appended]}
