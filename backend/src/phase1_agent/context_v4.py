"""Canonical persistent context with ordered native maintenance provenance."""

from copy import deepcopy

from .agent_receipt_contract import projection_id, validate_agent_receipts
from .context_compaction import is_compactable_text
from .context_contract import (
    EFFECTIVE_CONTEXT_TYPE, MAX_ACCEPTED_DELTAS, _fields, _ids, _unique_refs,
    artifact_ref, project_agent_context_message, project_agent_context_value,
    validate_agent_checkpoint_value, validate_effective_context,
)
from .contract_graph import validate_context_message_history
from .contract_json import content_digest, validate_json_value
from .contracts_v2 import validate_record
from .graph_contracts import require, uuid4_string
from .host_sdk import WriteIntent


def validate_native_context_view(value):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "owner", "basis", "messages", "layout",
                    "accepted_delta_ids", "applied_delta_ids",
                    "generation", "derivation"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 4
            and value["kind"] == "workflow.context-view", "context_update_invalid",
            "Persistent native context requires CONTEXT_VIEW@4")
    _fields(value["owner"], ("workflow_session_id", "object_key", "agent_node_id"))
    uuid4_string(value["owner"]["workflow_session_id"])
    uuid4_string(value["owner"]["agent_node_id"])
    require(type(value["owner"]["object_key"]) is str and 0 < len(value["owner"]["object_key"]) <= 128,
            "context_update_invalid", "Persistent context requires its exact bound object")
    _fields(value["basis"], ("revision_id", "head_revision"))
    uuid4_string(value["basis"]["revision_id"])
    require(type(value["basis"]["head_revision"]) is int
            and 1 <= value["basis"]["head_revision"] <= 2**53 - 1,
            "context_update_invalid", "Persistent context requires its exact CAS revision")
    validate_agent_checkpoint_value({
        "messages": value["messages"], "layout": value["layout"],
        "accepted_delta_ids": value["accepted_delta_ids"] + value["applied_delta_ids"],
        "generation": value["generation"],
    })
    for field in ("accepted_delta_ids", "applied_delta_ids"):
        _ids(value[field])
    require(not set(value["accepted_delta_ids"]) & set(value["applied_delta_ids"])
            and len(value["accepted_delta_ids"]) + len(value["applied_delta_ids"]) <= MAX_ACCEPTED_DELTAS,
            "context_consumption_mismatch", "Compaction cannot erase or duplicate consumed updates")
    require(type(value["generation"]) is int and 0 <= value["generation"] <= 2**53 - 1,
            "context_update_invalid", "Context generation must be monotonic")
    derivation = value["derivation"]
    _fields(derivation, ("operation", "input_view_ref", "update_ref"))
    operation = derivation["operation"]
    require(operation in ("read", "update", "merge"),
            "context_invalid_derivation", "Native context derivation is unsupported")
    for ref in (derivation["input_view_ref"], derivation["update_ref"]):
        if ref is not None:
            artifact_ref(ref)
    require((operation == "merge") == (derivation["update_ref"] is not None)
            and (operation == "read" or derivation["input_view_ref"] is not None),
            "context_invalid_derivation", "Native context lineage differs from its operation")
    return deepcopy(value)


def native_context_references(value):
    return _unique_refs([reference for reference in value["derivation"].values()
                         if type(reference) is dict])


def read_native_context(record, session_id, object_key, agent_node_id, retained=None):
    require((record["type_id"], record["schema_version"]) == (EFFECTIVE_CONTEXT_TYPE, 4)
            and not record["deleted"], "context_object_type_mismatch",
            "Native context requires its explicit versioned session object")
    current = validate_effective_context(record["value"])
    if retained is not None:
        require(validate_agent_checkpoint_value(retained) == current,
                "context_invalid_read", "Read service must return the exact inline context value")
    return validate_native_context_view({
        "schema_version": 4, "kind": "workflow.context-view",
        "owner": {"workflow_session_id": session_id, "object_key": object_key, "agent_node_id": agent_node_id},
        "basis": {"revision_id": record["revision_id"], "head_revision": record["revision"]},
        "messages": deepcopy(current["messages"]), "layout": deepcopy(current["layout"]),
        "accepted_delta_ids": deepcopy(current["accepted_delta_ids"]), "applied_delta_ids": [],
        "generation": current["generation"],
        "derivation": {"operation": "read", "input_view_ref": None, "update_ref": None},
    })


def validate_native_context_view_v5(value):
    """Validate the active owner-free CONTEXT_VIEW@5 contract."""
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "owner", "basis", "messages", "layout",
                    "accepted_delta_ids", "applied_delta_ids", "generation", "derivation"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 5
            and value["kind"] == "workflow.context-view", "context_update_invalid",
            "Persistent native context requires CONTEXT_VIEW@5")
    _fields(value["owner"], ("workflow_session_id", "object_key"))
    uuid4_string(value["owner"]["workflow_session_id"])
    require(type(value["owner"]["object_key"]) is str and 0 < len(value["owner"]["object_key"]) <= 128,
            "context_update_invalid", "Persistent context requires its exact bound object")
    _fields(value["basis"], ("revision_id", "head_revision"))
    uuid4_string(value["basis"]["revision_id"])
    require(type(value["basis"]["head_revision"]) is int
            and 1 <= value["basis"]["head_revision"] <= 2**53 - 1,
            "context_update_invalid", "Persistent context requires its exact CAS revision")
    validate_agent_checkpoint_value({
        "messages": value["messages"], "layout": value["layout"],
        "accepted_delta_ids": value["accepted_delta_ids"] + value["applied_delta_ids"],
        "generation": value["generation"],
    })
    for field in ("accepted_delta_ids", "applied_delta_ids"):
        _ids(value[field])
    require(not set(value["accepted_delta_ids"]) & set(value["applied_delta_ids"])
            and len(value["accepted_delta_ids"]) + len(value["applied_delta_ids"]) <= MAX_ACCEPTED_DELTAS,
            "context_consumption_mismatch", "Compaction cannot erase or duplicate consumed updates")
    require(type(value["generation"]) is int and 0 <= value["generation"] <= 2**53 - 1,
            "context_update_invalid", "Context generation must be monotonic")
    derivation = value["derivation"]
    _fields(derivation, ("operation", "input_view_ref", "receipts_ref"))
    require(derivation["operation"] in ("read", "merge")
            and ((derivation["operation"] == "merge") == (derivation["receipts_ref"] is not None))
            and ((derivation["operation"] == "merge") == (derivation["input_view_ref"] is not None)),
            "context_invalid_derivation", "Native context lineage differs from its operation")
    for reference in (derivation["input_view_ref"], derivation["receipts_ref"]):
        if reference is not None:
            artifact_ref(reference)
    return deepcopy(value)


def native_context_references_v5(value):
    return _unique_refs([reference for reference in value["derivation"].values()
                         if type(reference) is dict])


def read_native_context_v5(record, session_id, object_key, retained=None):
    require((record["type_id"], record["schema_version"]) == (EFFECTIVE_CONTEXT_TYPE, 4)
            and not record["deleted"], "context_object_type_mismatch",
            "Native context requires its explicit versioned session object")
    current = validate_effective_context(record["value"])
    if retained is not None:
        require(validate_agent_checkpoint_value(retained) == current,
                "context_invalid_read", "Read service must return the exact inline context value")
    return validate_native_context_view_v5({
        "schema_version": 5, "kind": "workflow.context-view",
        "owner": {"workflow_session_id": session_id, "object_key": object_key},
        "basis": {"revision_id": record["revision_id"], "head_revision": record["revision"]},
        "messages": deepcopy(current["messages"]), "layout": deepcopy(current["layout"]),
        "accepted_delta_ids": deepcopy(current["accepted_delta_ids"]), "applied_delta_ids": [],
        "generation": current["generation"],
        "derivation": {"operation": "read", "input_view_ref": None, "receipts_ref": None},
    })


def final_from_context_operations(operations):
    accepted = [operation["message"] for operation in operations if operation["kind"] == "append"]
    require(len(accepted) >= 2, "context_update_final_unproven", "A completed Agent requires its accepted final pair")
    control, settled = accepted[-2:]
    calls = [block for block in control["blocks"] if block["kind"] == "tool_call"]
    require(control["role"] == "assistant" and len(calls) == 1 and calls[0]["tool_name"] == "final_answer"
            and settled["role"] == "tool" and len(settled["blocks"]) == 1
            and settled["blocks"][0]["tool_call_id"] == calls[0]["tool_call_id"]
            and settled["blocks"][0]["status"] == "success"
            and settled["blocks"][0]["content"] == calls[0]["parsed_arguments"].get("answer"),
            "context_update_final_unproven", "A final answer requires the actual successful final tool settlement")
    value = settled["blocks"][0]["content"]
    require(type(value) is dict and set(value) == {"text"} and type(value["text"]) is str
            and bool(value["text"]), "context_update_final_unproven", "Final output must match the Agent answer schema")
    return {"message_id": control["message_id"], "value": deepcopy(value)}


def build_context_update_view(prompt, operations, receipts, final):
    """Project a proven native timeline explicitly; never reappend its raw round."""
    from .context_update import replay_context_operations, validate_context_operations
    from .context_prompt_v6 import validate_native_context_prompt_legacy_v4

    prompt, receipts = validate_native_context_prompt_legacy_v4(prompt), validate_agent_receipts(receipts)
    initial = validate_native_context_view(prompt["context"])
    operations = validate_context_operations(operations)
    require(initial["owner"]["workflow_session_id"] == receipts["owner"]["workflow_session_id"]
            and initial["owner"]["agent_node_id"] == receipts["owner"]["node_binding_id"],
            "context_agent_binding_mismatch", "Context updates belong to their explicitly bound Agent")
    update_id = projection_id(receipts["owner"]["node_run_id"], "context-update")
    identities = initial["accepted_delta_ids"] + initial["applied_delta_ids"]
    require(update_id not in identities and len(identities) < MAX_ACCEPTED_DELTAS,
            "context_update_already_consumed", "An Agent update cannot be consumed twice")
    eligible = [message["message_id"] for message, item in zip(prompt["messages"], prompt["layout"])
                if item["kind"] in ("history", "summary", "once")
                and item["compaction"] == "allowed" and is_compactable_text(message)]
    replayed = replay_context_operations(prompt["messages"], operations, eligible_message_ids=eligible)
    return _project_context_update_view(prompt, operations, receipts, final, replayed)


def build_context_execution_view_v5(prompt, operations, receipts, final, *,
                                    view_ref, receipts_ref, projection_replay):
    """Rebuild a durable candidate from a loaded view and proven execution facts."""
    from .context_update_v2 import validate_context_operations_v2
    from .context_prompt_v6 import validate_native_context_prompt

    prompt = validate_native_context_prompt(prompt)
    receipts = validate_agent_receipts(receipts)
    require(receipts["schema_version"] == 2
            and prompt["context"]["owner"]["workflow_session_id"]
            == receipts["owner"]["workflow_session_id"],
            "context_execution_owner_mismatch", "Schema 2 execution receipts must target the loaded session")
    operations = validate_context_operations_v2(operations)
    require(type(projection_replay) is dict and set(projection_replay) == {"messages", "generation_delta"}
            and type(projection_replay["messages"]) is list
            and type(projection_replay["generation_delta"]) is int
            and projection_replay["generation_delta"] >= 0,
            "context_update_invalid", "Merge requires the exact schema 2 proof replay")
    initial = validate_native_context_view_v5(prompt["context"])
    update_id = projection_id(receipts["owner"]["node_run_id"], "context-update")
    identities = initial["accepted_delta_ids"] + initial["applied_delta_ids"]
    require(update_id not in identities and len(identities) < MAX_ACCEPTED_DELTAS,
            "context_update_already_consumed", "An execution update cannot be consumed twice")
    replayed = {
        "messages": deepcopy(projection_replay["messages"]),
        "generation_delta": projection_replay["generation_delta"],
    }
    validate_context_message_history(replayed["messages"])
    require(replayed["generation_delta"] == sum(operation["kind"] == "compact" for operation in operations),
            "context_update_invalid", "Generation delta must count only proven compaction operations")
    require(final == final_from_context_operations(operations),
            "context_update_final_unproven", "Final answer must equal its accepted tool settlement")

    accepted = [operation["message"] for operation in operations if operation["kind"] == "append"]
    preserve_final_protocol = accepted[-2].get("provider_metadata") is not None
    removed_final_ids = set() if preserve_final_protocol else {
        accepted[-2]["message_id"], accepted[-1]["message_id"],
    }
    layouts = {}
    for message, item in zip(prompt["messages"], prompt["layout"]):
        if item["kind"] == "fixed":
            continue
        persistent = deepcopy(item)
        if persistent["kind"] == "current":
            persistent.update(kind="history", round_id=update_id, compaction="allowed")
        elif persistent["kind"] == "once":
            persistent["round_id"] = persistent["round_id"] or update_id
        layouts[message["message_id"]] = persistent
    for operation in operations:
        if operation["kind"] == "compact":
            layouts[operation["checkpoint"]["message_id"]] = {
                "kind": "summary", "round_id": None, "compaction": "allowed"}
        else:
            message = operation["message"]
            if message["source"]["kind"] != "protocol_feedback":
                layouts[message["message_id"]] = {
                    "kind": "history", "round_id": update_id,
                    "compaction": "allowed" if is_compactable_text(message) else "never"}
    retained = [project_agent_context_message(message) for message in replayed["messages"]
                if message["message_id"] in layouts and message["message_id"] not in removed_final_ids]
    answer_id = projection_id(receipts["snapshot_id"], "final-answer-projection")
    from .contracts_v2 import validate_context_history_message
    answer = validate_context_history_message({
        "message_id": answer_id, "role": "assistant",
        "blocks": [{"kind": "text", "text": final["value"]["text"]}],
    })
    if not preserve_final_protocol:
        retained.append(answer)
        layouts[answer_id] = {"kind": "history", "round_id": update_id, "compaction": "allowed"}
    return validate_native_context_view_v5({
        "schema_version": 5, "kind": "workflow.context-view",
        "owner": deepcopy(initial["owner"]), "basis": deepcopy(initial["basis"]),
        "messages": retained,
        "layout": [deepcopy(layouts[message["message_id"]]) for message in retained],
        "accepted_delta_ids": deepcopy(initial["accepted_delta_ids"]),
        "applied_delta_ids": initial["applied_delta_ids"] + [update_id],
        "generation": initial["generation"] + replayed["generation_delta"],
        "derivation": {"operation": "merge", "input_view_ref": artifact_ref(view_ref),
                       "receipts_ref": artifact_ref(receipts_ref)},
    })


def _project_context_update_view(prompt, operations, receipts, final, replayed):
    """Share persistent lifecycle projection after the caller proves its timeline."""
    initial = validate_native_context_view(prompt["context"])
    require(initial["owner"]["workflow_session_id"] == receipts["owner"]["workflow_session_id"]
            and initial["owner"]["agent_node_id"] == receipts["owner"]["node_binding_id"],
            "context_agent_binding_mismatch", "Context updates belong to their explicitly bound Agent")
    update_id = projection_id(receipts["owner"]["node_run_id"], "context-update")
    identities = initial["accepted_delta_ids"] + initial["applied_delta_ids"]
    require(update_id not in identities and len(identities) < MAX_ACCEPTED_DELTAS,
            "context_update_already_consumed", "An Agent update cannot be consumed twice")
    require(final == final_from_context_operations(operations), "context_update_final_unproven",
            "The derived final answer must equal its accepted canonical control pair")
    accepted = [operation["message"] for operation in operations if operation["kind"] == "append"]
    preserve_final_protocol = accepted[-2].get("provider_metadata") is not None
    removed_final_ids = set() if preserve_final_protocol else {
        accepted[-2]["message_id"], accepted[-1]["message_id"],
    }
    layouts = {}
    for message, item in zip(prompt["messages"], prompt["layout"]):
        if item["kind"] == "fixed":
            continue
        persistent = deepcopy(item)
        if persistent["kind"] == "current":
            persistent.update(kind="history", round_id=update_id, compaction="allowed")
        elif persistent["kind"] == "once":
            persistent["round_id"] = persistent["round_id"] or update_id
        layouts[message["message_id"]] = persistent
    for operation in operations:
        if operation["kind"] == "compact":
            layouts[operation["checkpoint"]["message_id"]] = {
                "kind": "summary", "round_id": None, "compaction": "allowed"}
        else:
            message = operation["message"]
            if message["source"]["kind"] != "protocol_feedback":
                layouts[message["message_id"]] = {
                    "kind": "history", "round_id": update_id,
                    "compaction": "allowed" if is_compactable_text(message) else "never"}
    retained = [project_agent_context_message(message) for message in replayed["messages"]
                if message["message_id"] in layouts and message["message_id"] not in removed_final_ids]
    answer_id = projection_id(receipts["snapshot_id"], "final-answer-projection")
    from .contracts_v2 import validate_context_history_message
    answer = validate_context_history_message({
        "message_id": answer_id, "role": "assistant",
        "blocks": [{"kind": "text", "text": final["value"]["text"]}],
    })
    if not preserve_final_protocol:
        retained.append(answer)
        layouts[answer_id] = {"kind": "history", "round_id": update_id, "compaction": "allowed"}
    return validate_native_context_view({
        **deepcopy(initial), "messages": retained,
        "layout": [deepcopy(layouts[message["message_id"]]) for message in retained],
        "applied_delta_ids": initial["applied_delta_ids"] + [update_id],
        "generation": initial["generation"] + replayed["generation_delta"],
        "derivation": {"operation": "update", "input_view_ref": prompt["context_ref"], "update_ref": None},
    })


def merge_native_context(view, view_ref, update, update_ref, prompt):
    from .context_update import validate_context_update

    view, update = validate_native_context_view(view), validate_context_update(update)
    view_ref = artifact_ref(view_ref)
    require(update["basis_view_ref"] == view_ref and update["basis_revision"] == view["basis"]
            and prompt["context_ref"] == view_ref and prompt["context"] == view,
            "context_update_basis_mismatch", "Merge must use the exact view actually frozen for this Agent")
    final = final_from_context_operations(update["operations"])
    if update["schema_version"] == 2:
        from .context_update_v2 import _candidate_replay, make_context_update_v2
        # This projection checks shape only; the consumer must separately prove
        # accepted executor/M2 facts before adopting a schema 2 artifact.
        replayed = _candidate_replay(prompt, update["operations"])
        expected = make_context_update_v2(
            prompt, update["operations"], update["receipts"], final,
            effective_messages=replayed["messages"],
            policy_ref=update["compaction_policy_ref"])["next_view"]
    else:
        expected = build_context_update_view(prompt, update["operations"], update["receipts"], final)
    require(update["next_view"] == expected, "context_update_candidate_mismatch",
            "Persistent context must derive from ordered execution and explicit projection")
    result = deepcopy(expected)
    result["derivation"] = {"operation": "merge", "input_view_ref": view_ref,
                            "update_ref": artifact_ref(update_ref)}
    return validate_native_context_view(result)


def merge_native_context_v5(view, view_ref, receipts, receipts_ref, prompt, execution):
    from .context_prompt_v6 import validate_native_context_prompt
    view, receipts = validate_native_context_view_v5(view), validate_agent_receipts(receipts)
    view_ref, receipts_ref = artifact_ref(view_ref), artifact_ref(receipts_ref)
    prompt = validate_native_context_prompt(prompt)
    require(prompt["context_ref"] == view_ref and prompt["context"] == view
            and prompt["context"]["owner"]["workflow_session_id"]
            == receipts["owner"]["workflow_session_id"],
            "context_update_basis_mismatch", "Merge requires the exact loaded view used by this execution")
    candidate = build_context_execution_view_v5(
        prompt, execution["operations"], receipts, execution["final"],
        view_ref=view_ref, receipts_ref=receipts_ref,
        projection_replay=execution["projection_replay"])
    require(execution["snapshot_id"] == receipts["snapshot_id"],
            "agent_snapshot_mismatch", "Execution proof must retain the exact accepted snapshot")
    return candidate


def validate_native_context_artifacts(payload, resolve):
    from .context_update import validate_context_update
    from .context_prompt_v6 import validate_native_context_prompt_legacy_v4

    view = validate_native_context_view(payload["view"])
    require(resolve(artifact_ref(payload["view_ref"])) == view, "context_artifact_mismatch",
            "Native context must equal its accepted immutable artifact")
    derivation = view["derivation"]
    previous = derivation["input_view_ref"]
    if previous is None:
        require(derivation["operation"] == "read" and derivation["update_ref"] is None,
                "context_invalid_derivation", "Only an object read may begin native context lineage")
    else:
        original = validate_native_context_view(resolve(previous))
        if derivation["operation"] == "read":
            require(all(view[field] == original[field]
                        for field in ("messages", "layout", "generation"))
                    and view["accepted_delta_ids"] == original["accepted_delta_ids"] + original["applied_delta_ids"]
                    and not view["applied_delta_ids"],
                    "context_invalid_derivation", "A read cannot restore compacted original messages")
        else:
            require(derivation["operation"] == "merge", "context_adoption_source_invalid",
                    "An unaccepted Agent candidate is not a writable context artifact")
            packet = validate_context_update(resolve(derivation["update_ref"]))
            prompt = validate_native_context_prompt_legacy_v4(
                resolve(packet["receipts"]["frozen_prompt_ref"]))
            expected = merge_native_context(original, previous, packet, derivation["update_ref"], prompt)
            require(expected == view, "context_invalid_derivation",
                    "Merge must reproduce its exact accepted Agent update")
    if "context" in payload:
        packet = validate_context_update(payload["context"])
        require(resolve(artifact_ref(payload["context_ref"])) == packet,
                "context_artifact_mismatch", "The update must equal its accepted Agent artifact")
        prompt = validate_native_context_prompt_legacy_v4(
            resolve(packet["receipts"]["frozen_prompt_ref"]))
        return merge_native_context(view, payload["view_ref"], packet, payload["context_ref"], prompt)
    return view


def validate_native_context_artifacts_v5(payload, resolve):
    view = validate_native_context_view_v5(payload["view"])
    require(resolve(artifact_ref(payload["view_ref"])) == view,
            "context_artifact_mismatch", "Native context must equal its accepted immutable artifact")
    if view["derivation"]["operation"] == "read":
        require(view["derivation"]["input_view_ref"] is None
                and view["derivation"]["receipts_ref"] is None,
                "context_invalid_derivation", "A read cannot inherit another context artifact")
    else:
        original = validate_native_context_view_v5(resolve(view["derivation"]["input_view_ref"]))
        receipts = validate_agent_receipts(resolve(view["derivation"]["receipts_ref"]))
        require(receipts["schema_version"] == 2
                and original["owner"] == view["owner"]
                and original["basis"] == view["basis"]
                and receipts["owner"]["workflow_session_id"] == view["owner"]["workflow_session_id"]
                and projection_id(receipts["owner"]["node_run_id"], "context-update")
                == view["applied_delta_ids"][-1],
                "context_invalid_derivation", "Merged context must retain its exact loaded basis and producer")
    return view


def prepare_native_context_adoption(*, workflow_session_id, object_key, object_record, view_ref, view):
    view = validate_native_context_view(view)
    require(view["owner"]["workflow_session_id"] == workflow_session_id
            and view["owner"]["object_key"] == object_key, "context_save_owner_mismatch",
            "Native context belongs to another object")
    require((object_record["type_id"], object_record["schema_version"]) == (EFFECTIVE_CONTEXT_TYPE, 4)
            and object_record["binding"]["object_key"] == object_key and not object_record["deleted"],
            "context_object_type_mismatch", "Native context requires its exact registered object version")
    current = validate_effective_context(object_record["value"])
    view_ref = artifact_ref(view_ref)
    desired = project_agent_context_value(view)
    operation_key = "context-merge:" + content_digest({
        "owner": view["owner"], "basis": view["basis"], "desired": desired, "view_schema": 4})
    if current == desired:
        return {"desired_value": desired, "write_intent": None, "already_committed": True,
                "operation_key": operation_key}
    require(view["basis"] == {"revision_id": object_record["revision_id"],
                             "head_revision": object_record["revision"]},
            "context_stale_basis", "Native context must retain its exact revision and CAS fence")
    require(current["accepted_delta_ids"] == view["accepted_delta_ids"],
            "context_consumption_mismatch", "Adoption cannot erase the persistent consumption boundary")
    return {"desired_value": desired, "already_committed": False, "operation_key": operation_key,
            "write_intent": WriteIntent(object_key, object_record["revision"], operation_key, desired).to_dict()}


def prepare_native_context_adoption_v5(*, workflow_session_id, object_key, object_record, view_ref, view):
    view = validate_native_context_view_v5(view)
    require(view["owner"] == {"workflow_session_id": workflow_session_id, "object_key": object_key},
            "context_save_owner_mismatch", "Native context belongs to another object")
    require((object_record["type_id"], object_record["schema_version"]) == (EFFECTIVE_CONTEXT_TYPE, 4)
            and object_record["binding"]["object_key"] == object_key and not object_record["deleted"],
            "context_object_type_mismatch", "Native context requires its exact registered object version")
    current = validate_effective_context(object_record["value"])
    view_ref = artifact_ref(view_ref)
    desired = project_agent_context_value(view)
    operation_key = "context-merge:" + content_digest({
        "owner": view["owner"], "basis": view["basis"], "desired": desired, "view_schema": 5})
    if current == desired:
        return {"desired_value": desired, "write_intent": None, "already_committed": True,
                "operation_key": operation_key}
    require(view["basis"] == {"revision_id": object_record["revision_id"],
                             "head_revision": object_record["revision"]},
            "context_stale_basis", "Native context must retain its exact revision and CAS fence")
    require(current["accepted_delta_ids"] == view["accepted_delta_ids"],
            "context_consumption_mismatch", "Adoption cannot erase consumed updates")
    return {"desired_value": desired, "already_committed": False, "operation_key": operation_key,
            "write_intent": WriteIntent(object_key, object_record["revision"], operation_key, desired).to_dict()}


def prove_native_context_view(view_ref, resolve_detail):
    def input_matches(source, port, reference):
        refs = source.get("input_refs", {}).get(port, [])
        return len(refs) == 1 and refs[0]["output_id"] == reference["output_id"]

    visited, reference, result = set(), artifact_ref(view_ref), None
    while reference is not None:
        require(reference["output_id"] not in visited, "context_invalid_derivation",
                "Native context lineage cannot cycle")
        visited.add(reference["output_id"])
        source = resolve_detail(reference)
        view = validate_native_context_view(source["value"])
        result = result or deepcopy(view)
        operation, lineage = view["derivation"]["operation"], view["derivation"]
        expected = {"read": ("context.output", "4"), "merge": ("context.merge", "4")}
        require(operation in expected and (source["component_id"], source["component_version"]) == expected[operation],
                "context_adoption_source_invalid", "Native context requires its official frozen producer")
        require(source["config"]["object_key"] == view["owner"]["object_key"]
                and source["config"]["agent_node_id"] == view["owner"]["agent_node_id"]
                and source["producer"]["workflow_session_id"] == view["owner"]["workflow_session_id"],
                "context_agent_binding_mismatch", "Context owner must match the actual frozen producer")
        if operation == "merge":
            from .context_update import validate_context_update
            origin = resolve_detail(lineage["update_ref"])
            update = validate_context_update(origin["value"])
            require((origin["component_id"], origin["component_version"]) in (
                ("agents.execute", "4"), ("agents.execute", "8"))
                    and origin["producer"] == update["owner"]
                    and input_matches(origin, "prompt", update["receipts"]["frozen_prompt_ref"])
                    and input_matches(origin, "model", update["receipts"]["model_ref"])
                    and input_matches(source, "view", lineage["input_view_ref"])
                    and input_matches(source, "context", lineage["update_ref"]),
                    "context_adoption_source_invalid", "Update provenance must retain its exact official Agent inputs")
            policy_refs = origin.get("input_refs", {}).get("compaction_policy", [])
            policy_ref = update["compaction_policy_ref"]
            require(len(policy_refs) == (0 if policy_ref is None else 1)
                    and (not policy_refs or policy_refs[0]["output_id"] == policy_ref["output_id"]),
                    "context_policy_binding_mismatch",
                    "Update provenance must retain the actual accepted compaction policy input")
            from .context_compaction_policy import effective_summary_prompt, validate_compaction_policy
            from .context_compaction import compaction_instruction, eligible_compaction_ids, should_compact

            policy = None if policy_ref is None else validate_compaction_policy(resolve_detail(policy_ref)["value"])
            prompt = resolve_detail(update["receipts"]["frozen_prompt_ref"])["value"]
            accepted = []
            settings = {"policy": policy, "layout": prompt["layout"]}
            for context_operation in update["operations"]:
                if context_operation["kind"] == "append":
                    accepted.append(context_operation["message"])
                    continue
                request = context_operation["request"]["messages"]
                require(policy is not None and policy["enabled"]
                        and should_compact(context_operation["capacity"]["before"], policy)
                        and context_operation["covered_message_ids"] == eligible_compaction_ids(
                            prompt["messages"], request[:-1], settings, accepted)
                        and request[-1] == compaction_instruction(
                            context_operation["compaction_id"], request[-1]["message_id"],
                            effective_summary_prompt(policy), context_operation["covered_message_ids"], request[:-1]),
                        "context_policy_binding_mismatch",
                        "Accepted compactions must retain their actual policy, prompt and permitted range")
        validate_native_context_artifacts(
            {"view": view, "view_ref": reference}, lambda ref: resolve_detail(ref)["value"])
        reference = lineage["input_view_ref"]
    return result


def validate_native_context_write(value, context):
    if context["operation"] == "delete":
        require(value is None, "context_invalid_write", "Deletion requires a tombstone")
        return
    value = validate_effective_context(value)
    current = context["current_record"]
    if context["operation"] == "initialize":
        require(current is None and value == {
            "messages": [], "layout": [], "accepted_delta_ids": [], "generation": 0,
        }, "context_initialization_invalid", "Native context begins with its formal empty value")
        return
    if current is not None and not current["deleted"] and current["value"] == value:
        return
    require(current is not None and not current["deleted"],
            "context_invalid_write", "Native context requires an initialized live object")
    previous = validate_effective_context(current["value"])
    require(value["accepted_delta_ids"][:len(previous["accepted_delta_ids"])]
            == previous["accepted_delta_ids"]
            and value["generation"] >= previous["generation"],
            "context_consumption_mismatch", "Native context cannot discard consumed updates or compaction")
