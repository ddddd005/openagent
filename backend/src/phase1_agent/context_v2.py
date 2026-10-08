"""Bound context views and direct Agent round outputs; earlier schemas stay frozen."""

from copy import deepcopy

from .context_contract import (
    EFFECTIVE_CONTEXT_TYPE, MAX_ACCEPTED_DELTAS, _fields, _unique_refs, artifact_ref,
    context_view_references, validate_context_unit, validate_context_view,
    validate_effective_context,
)
from .contract_json import content_digest, validate_json_value
from .graph_contracts import require, uuid4_string
from .host_sdk import WriteIntent


def validate_bound_context_view(value):
    validate_json_value(value)
    require(type(value) is dict and type(value.get("schema_version")) is int and value["schema_version"] == 2,
            "context_invalid_contract", "Bound context requires CONTEXT_VIEW@2")
    _fields(value["owner"], ("workflow_session_id", "object_key", "agent_node_id"))
    uuid4_string(value["owner"]["agent_node_id"])
    legacy = deepcopy(value)
    legacy["schema_version"] = 1
    legacy["owner"].pop("agent_node_id")
    if legacy["derivation"]["operation"] == "merge":
        legacy["derivation"]["operation"] = "advance"
    validate_context_view(legacy)
    require(value["derivation"]["operation"] in ("read", "window", "merge"),
            "context_invalid_derivation", "Bound context operation is unsupported")
    return deepcopy(value)


def validate_agent_context(value):
    from .agent_receipt_contract import projection_id, validate_agent_receipts
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "delta_id", "owner", "basis_view_ref",
                    "current_root_ref", "unit", "receipts"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow.agent-context",
            "agent_context_invalid", "Agent context requires its exact envelope")
    uuid4_string(value["delta_id"])
    receipts = validate_agent_receipts(value["receipts"])
    require(value["owner"] == receipts["owner"], "agent_context_owner_mismatch",
            "Agent context and execution receipts require one invocation")
    artifact_ref(value["basis_view_ref"])
    artifact_ref(value["current_root_ref"])
    unit = validate_context_unit(value["unit"])
    require(unit["unit_id"] == receipts["unit_id"] and unit["source_kind"] == "accepted_execution",
            "agent_context_invalid", "Agent context requires its complete execution unit")
    require(value["delta_id"] == projection_id(value["owner"]["node_run_id"], "agent-context")
            and unit["source_refs"] == [receipts["frozen_prompt_ref"], value["current_root_ref"]],
            "agent_context_invalid", "Round identity and unit source must match the frozen invocation")
    return deepcopy(value)


def agent_context_references(value):
    return _unique_refs([value["basis_view_ref"], value["current_root_ref"],
                         value["receipts"]["frozen_prompt_ref"], value["receipts"]["model_ref"],
                         *value["unit"]["source_refs"]])


def read_bound_context(record, session_id, object_key, agent_node_id, retained=None):
    require((record["type_id"], record["schema_version"]) == (EFFECTIVE_CONTEXT_TYPE, 2),
            "context_object_type_mismatch", "Bound context requires its versioned session object")
    value = validate_effective_context(record["value"])
    if value["view_ref"] is None:
        require(retained is None, "context_invalid_read", "Empty context cannot hide history")
        units = []
    else:
        retained = validate_bound_context_view(retained)
        require(retained["accepted_delta_ids"] + retained["applied_delta_ids"] == value["accepted_delta_ids"],
                "context_consumption_mismatch", "Retained context has another consumption boundary")
        units = retained["units"]
    return validate_bound_context_view({
        "schema_version": 2, "kind": "workflow.context-view",
        "owner": {"workflow_session_id": session_id, "object_key": object_key,
                  "agent_node_id": agent_node_id},
        "basis": {"revision_id": record["revision_id"], "head_revision": record["revision"]},
        "units": deepcopy(units), "accepted_delta_ids": value["accepted_delta_ids"],
        "applied_delta_ids": [],
        "derivation": {"operation": "read", "input_view_ref": value["view_ref"], "delta_ref": None}})


def window_bound_context(view, view_ref, *, last_units):
    view = validate_bound_context_view(view)
    require(type(last_units) is int and 0 <= last_units <= 4096, "context_invalid_window",
            "Window counts complete units")
    view["units"] = view["units"][-last_units:] if last_units else []
    view["derivation"] = {"operation": "window", "input_view_ref": artifact_ref(view_ref), "delta_ref": None}
    return validate_bound_context_view(view)


def merge_bound_context(view, view_ref, packet, packet_ref):
    view, packet = validate_bound_context_view(view), validate_agent_context(packet)
    view_ref, packet_ref = artifact_ref(view_ref), artifact_ref(packet_ref)
    require(packet["basis_view_ref"] == view_ref, "context_delta_basis_mismatch",
            "Merge must consume the exact view actually assembled for Agent")
    require(packet["owner"]["workflow_session_id"] == view["owner"]["workflow_session_id"]
            and packet["owner"]["node_binding_id"] == view["owner"]["agent_node_id"],
            "context_agent_binding_mismatch", "Round output belongs to another bound Agent")
    require(packet["delta_id"] not in view["accepted_delta_ids"] + view["applied_delta_ids"],
            "context_delta_already_consumed", "Agent round was already consumed")
    require(len(view["accepted_delta_ids"] + view["applied_delta_ids"]) < MAX_ACCEPTED_DELTAS,
            "context_delta_capacity_exceeded", "Consumed identities cannot be silently discarded")
    view["units"].append({"unit_ref": packet_ref, "unit": packet["unit"]})
    view["applied_delta_ids"].append(packet["delta_id"])
    view["derivation"] = {"operation": "merge", "input_view_ref": view_ref, "delta_ref": packet_ref}
    return validate_bound_context_view(view)


def validate_bound_context_artifacts(payload, resolve):
    """Resolve only exact references already granted by the public host."""
    view = validate_bound_context_view(payload["view"])
    require(resolve(artifact_ref(payload["view_ref"])) == view, "context_artifact_mismatch",
            "Bound view differs from its immutable artifact")
    for entry in view["units"]:
        packet = validate_agent_context(resolve(entry["unit_ref"]))
        require(packet["unit"] == entry["unit"], "context_artifact_mismatch",
                "Historical unit differs from its accepted Agent context")
    lineage = view["derivation"]
    ref = lineage["input_view_ref"]
    if ref is None:
        require(lineage["operation"] == "read" and not view["units"]
                and not view["accepted_delta_ids"] and not view["applied_delta_ids"],
                "context_invalid_derivation", "Empty read cannot manufacture context")
    else:
        original = validate_bound_context_view(resolve(ref))
        if lineage["operation"] == "read":
            require(view["units"] == original["units"] and not view["applied_delta_ids"]
                    and view["accepted_delta_ids"] == original["accepted_delta_ids"] + original["applied_delta_ids"],
                    "context_invalid_derivation", "Read cannot modify retained history")
        elif lineage["operation"] == "window":
            count = len(view["units"])
            require(window_bound_context(original, ref, last_units=count) == view,
                    "context_invalid_derivation", "Window must preserve the selected complete suffix")
        else:
            require(merge_bound_context(original, ref, resolve(lineage["delta_ref"]), lineage["delta_ref"]) == view,
                    "context_invalid_derivation", "Merge must use its exact accepted round")
    if "context" in payload:
        packet = validate_agent_context(payload["context"])
        require(resolve(artifact_ref(payload["context_ref"])) == packet, "context_artifact_mismatch",
                "Agent round differs from its accepted context output")
        from .context_prompt import validate_bound_context_prompt
        prompt = validate_bound_context_prompt(resolve(packet["receipts"]["frozen_prompt_ref"]))
        require(prompt["context_ref"] == packet["basis_view_ref"]
                and prompt["context"] == view and prompt["current_input_ref"] == packet["current_root_ref"]
                and prompt["current_input"] == packet["unit"]["root"],
                "context_delta_prompt_mismatch", "Agent round differs from its actual frozen input")
        root = resolve(packet["current_root_ref"])
        require(root.get("kind") == "workflow.text" and root.get("schema_version") == 2
                and root.get("text") == packet["unit"]["root"]["content"],
                "context_delta_prompt_mismatch", "Agent root differs from its accepted input")
        return merge_bound_context(view, payload["view_ref"], packet, payload["context_ref"])
    return view


def prepare_bound_context_adoption(*, workflow_session_id, object_key, object_record, view_ref, view):
    view, view_ref = validate_bound_context_view(view), artifact_ref(view_ref)
    require(view["owner"]["workflow_session_id"] == workflow_session_id
            and view["owner"]["object_key"] == object_key, "context_save_owner_mismatch",
            "View belongs to another context object")
    require((object_record["type_id"], object_record["schema_version"]) == (EFFECTIVE_CONTEXT_TYPE, 2)
            and not object_record["deleted"], "context_object_type_mismatch",
            "Merge requires its registered bound context object")
    current = validate_effective_context(object_record["value"])
    desired = {"view_ref": view_ref,
               "accepted_delta_ids": view["accepted_delta_ids"] + view["applied_delta_ids"]}
    operation_key = "context-merge:" + content_digest({
        "owner": view["owner"], "basis": view["basis"], "view_ref": view_ref,
        "adopted_delta_ids": desired["accepted_delta_ids"]})
    if current == desired:
        return {"desired_value": desired, "write_intent": None, "already_committed": True,
                "operation_key": operation_key}
    require(view["basis"] == {"revision_id": object_record["revision_id"],
                             "head_revision": object_record["revision"]},
            "context_stale_basis", "Merge requires its frozen exact revision and CAS fence")
    require(current["accepted_delta_ids"] == view["accepted_delta_ids"],
            "context_consumption_mismatch", "Merge lost the persistent consumption boundary")
    return {"desired_value": desired, "already_committed": False, "operation_key": operation_key,
            "write_intent": WriteIntent(object_key, object_record["revision"], operation_key, desired).to_dict()}


def validate_bound_context_write(value, context):
    if context["operation"] == "delete":
        require(value is None, "context_invalid_write", "Deletion requires a tombstone")
        return
    value = validate_effective_context(value)
    current = context["current_record"]
    if context["operation"] == "initialize":
        require(current is None and value == {"view_ref": None, "accepted_delta_ids": []},
                "context_initialization_invalid", "Bound context starts empty")
        return
    if current is not None and not current["deleted"] and value == current["value"]:
        return
    require(value["view_ref"] is not None, "context_invalid_write", "Context cannot discard consumption")
    resolver = context["resolve_artifact"]
    source = resolver(value["view_ref"])
    require(source["component_id"] == "context.merge" and source["component_version"] == "1",
            "context_adoption_source_invalid", "Only the official merge produces writable bound context")
    view = validate_bound_context_adoption_proof(value["view_ref"], resolver)
    require(view["owner"]["agent_node_id"] == source["config"]["agent_node_id"]
            and view["owner"]["object_key"] == source["config"]["object_key"],
            "context_agent_binding_mismatch", "Merge candidate differs from its bound writer")
    packet_source = resolver(view["derivation"]["delta_ref"])
    packet = validate_agent_context(packet_source["value"])
    require(packet_source["component_id"] == "agents.execute" and packet_source["component_version"] in ("2", "6"),
            "context_adoption_source_invalid", "Merge requires the official Agent direct output")
    require(packet_source.get("producer", packet["owner"]) == packet["owner"],
            "context_agent_binding_mismatch", "Agent receipt owner differs from accepted producer")
    prepared = prepare_bound_context_adoption(
        workflow_session_id=context["workflow_session_id"], object_key=context["object_key"],
        object_record=current, view_ref=value["view_ref"], view=view)
    require(prepared["desired_value"] == value, "context_consumption_mismatch",
            "Pointer and consumed identities must equal the proven candidate")


def validate_bound_context_adoption_proof(view_ref, resolve_detail):
    """Every transform must come from its exact frozen official implementation."""
    visited = set()
    reference = artifact_ref(view_ref)
    result = None
    while reference is not None:
        reference = artifact_ref(reference)
        identity = reference["output_id"]
        require(identity not in visited, "context_invalid_derivation", "Context lineage cannot cycle")
        visited.add(identity)
        source = resolve_detail(reference)
        view = validate_bound_context_view(source["value"])
        if result is None:
            result = deepcopy(view)
        operation = view["derivation"]["operation"]
        expected_source = {"read": ("context.output", "1"), "window": ("context.window", "2"),
                           "merge": ("context.merge", "1")}[operation]
        require((source["component_id"], source["component_version"]) == expected_source,
                "context_adoption_source_invalid", "Bound context requires its official frozen producer")
        if operation in ("read", "merge"):
            require(source["config"]["object_key"] == view["owner"]["object_key"]
                    and source["config"]["agent_node_id"] == view["owner"]["agent_node_id"],
                    "context_agent_binding_mismatch", "View ownership differs from its producer binding")
        original_ref = view["derivation"]["input_view_ref"]
        if original_ref is not None:
            original = validate_bound_context_view(resolve_detail(original_ref)["value"])
            if operation == "window":
                require(window_bound_context(original, original_ref,
                                             last_units=source["config"]["last_units"]) == view,
                        "context_adoption_source_invalid", "Window differs from its frozen configuration")
        if operation == "merge":
            packet_source = resolve_detail(view["derivation"]["delta_ref"])
            require((packet_source["component_id"], packet_source["component_version"]) in (
                ("agents.execute", "2"), ("agents.execute", "6")),
                    "context_adoption_source_invalid", "Merge requires the official direct Agent output")
        validate_bound_context_artifacts(
            {"view_ref": reference, "view": view}, lambda ref: resolve_detail(ref)["value"])
        reference = original_ref
    return result
