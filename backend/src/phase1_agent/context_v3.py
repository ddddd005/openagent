"""Summary-aware effective contexts; CONTEXT_VIEW@2 remains unchanged."""

from copy import deepcopy

from .context_contract import (
    EFFECTIVE_CONTEXT_TYPE, MAX_ACCEPTED_DELTAS, _fields, _unique_refs, artifact_ref,
    validate_effective_context,
)
from .context_summary import (
    accept_summary_candidate, entry_coverage, entry_ref, plan_compression,
    replace_context_prefix, validate_compression_plan, validate_context_summary,
)
from .context_v2 import validate_agent_context, validate_bound_context_view
from .contract_json import content_digest, validate_json_value
from .graph_contracts import require
from .host_sdk import WriteIntent


def validate_effective_view(value):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "owner", "basis", "units",
                    "accepted_delta_ids", "applied_delta_ids", "derivation"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 3,
            "context_invalid_contract", "Summary-aware context requires CONTEXT_VIEW@3")
    require(type(value["units"]) is list and len(value["units"]) <= 4096,
            "context_invalid_contract", "Effective units must be bounded")
    round_units, identities, unit_ids, coverage = [], [], [], []
    for entry in value["units"]:
        if type(entry) is dict and set(entry) == {"unit_ref", "unit"}:
            round_units.append(entry)
            unit_ids.append(entry["unit"].get("unit_id"))
        else:
            _fields(entry, ("summary_ref", "summary"))
            validate_context_summary(entry["summary"])
            unit_ids.append(entry["summary"]["summary_id"])
        identities.append(artifact_ref(entry_ref(entry))["output_id"])
        coverage.extend(ref["output_id"] for ref in entry_coverage(entry))
    require(len(identities) == len(set(identities)), "context_duplicate_unit",
            "An effective unit cannot appear twice")
    require(len(unit_ids) == len(set(unit_ids)), "context_duplicate_unit",
            "Effective rounds and summaries require distinct logical identities")
    require(len(coverage) == len(set(coverage)), "context_summary_overlap",
            "Effective summaries and rounds cannot overlap in original coverage")
    derivation = value["derivation"]
    _fields(derivation, ("operation", "input_view_ref", "round_ref", "plan_ref", "summary_ref"))
    operation = derivation["operation"]
    require(operation in ("read", "window", "merge", "replace"), "context_invalid_derivation",
            "Effective view operation is unsupported")
    for reference in (derivation[key] for key in ("input_view_ref", "round_ref", "plan_ref", "summary_ref")):
        if reference is not None:
            artifact_ref(reference)
    require((derivation["round_ref"] is not None) == (operation == "merge")
            and (derivation["plan_ref"] is not None) == (operation == "replace")
            and (derivation["summary_ref"] is not None) == (operation == "replace")
            and (operation == "read" or derivation["input_view_ref"] is not None),
            "context_invalid_derivation", "Effective view lineage differs from its operation")
    legacy = {**deepcopy(value), "schema_version": 2, "units": deepcopy(round_units),
              "derivation": {"operation": "window" if operation == "replace" else operation,
                             "input_view_ref": derivation["input_view_ref"],
                             "delta_ref": derivation["round_ref"]}}
    validate_bound_context_view(legacy)
    return deepcopy(value)


def effective_view_references(value):
    return _unique_refs([entry_ref(entry) for entry in value["units"]]
                        + [ref for ref in value["derivation"].values() if type(ref) is dict])


def read_effective_view(record, sid, key, agent_node_id, retained=None):
    require((record["type_id"], record["schema_version"]) == (EFFECTIVE_CONTEXT_TYPE, 3)
            and not record["deleted"], "context_object_type_mismatch",
            "Summary-aware context requires its registered session object")
    value = validate_effective_context(record["value"])
    units = []
    if value["view_ref"] is not None:
        retained = validate_effective_view(retained)
        require(retained["accepted_delta_ids"] + retained["applied_delta_ids"] == value["accepted_delta_ids"],
                "context_consumption_mismatch", "Retained view differs from its consumed round boundary")
        units = retained["units"]
    else:
        require(retained is None, "context_invalid_read", "Empty object cannot hide history")
    return validate_effective_view({
        "schema_version": 3, "kind": "workflow.context-view",
        "owner": {"workflow_session_id": sid, "object_key": key, "agent_node_id": agent_node_id},
        "basis": {"revision_id": record["revision_id"], "head_revision": record["revision"]},
        "units": deepcopy(units), "accepted_delta_ids": value["accepted_delta_ids"],
        "applied_delta_ids": [], "derivation": {
            "operation": "read", "input_view_ref": value["view_ref"],
            "round_ref": None, "plan_ref": None, "summary_ref": None}})


def window_effective_view(view, view_ref, *, last_units):
    view = validate_effective_view(view)
    require(type(last_units) is int and 0 <= last_units <= 4096,
            "context_invalid_window", "Window selects complete effective units")
    view["units"] = view["units"][-last_units:] if last_units else []
    view["derivation"] = {"operation": "window", "input_view_ref": artifact_ref(view_ref),
                          "round_ref": None, "plan_ref": None, "summary_ref": None}
    return validate_effective_view(view)


def merge_effective_view(view, view_ref, packet, packet_ref):
    view, packet = validate_effective_view(view), validate_agent_context(packet)
    view_ref = artifact_ref(view_ref)
    require(packet["basis_view_ref"] == view_ref, "context_delta_basis_mismatch",
            "Merge must consume the exact summary-aware view used by Agent")
    require(packet["owner"]["workflow_session_id"] == view["owner"]["workflow_session_id"]
            and packet["owner"]["node_binding_id"] == view["owner"]["agent_node_id"],
            "context_agent_binding_mismatch", "Round belongs to another bound Agent")
    identities = view["accepted_delta_ids"] + view["applied_delta_ids"]
    require(packet["delta_id"] not in identities, "context_delta_already_consumed", "Round was already consumed")
    require(len(identities) < MAX_ACCEPTED_DELTAS, "context_delta_capacity_exceeded",
            "Consumed identities cannot be dropped by compression")
    view["units"].append({"unit_ref": artifact_ref(packet_ref), "unit": packet["unit"]})
    view["applied_delta_ids"].append(packet["delta_id"])
    view["derivation"] = {"operation": "merge", "input_view_ref": view_ref,
                          "round_ref": artifact_ref(packet_ref), "plan_ref": None, "summary_ref": None}
    return validate_effective_view(view)


def validate_effective_view_artifacts(payload, resolve):
    view = validate_effective_view(payload["view"])
    require(resolve(artifact_ref(payload["view_ref"])) == view,
            "context_artifact_mismatch", "Effective view differs from its exact immutable artifact")
    for entry in view["units"]:
        if "unit" in entry:
            packet = validate_agent_context(resolve(entry["unit_ref"]))
            require(packet["unit"] == entry["unit"], "context_artifact_mismatch",
                    "Round differs from its accepted Agent source")
        else:
            summary = validate_context_summary(resolve(entry["summary_ref"]))
            require(summary == entry["summary"], "context_artifact_mismatch",
                    "Summary differs from its accepted candidate")
            plan = validate_compression_plan(resolve(summary["plan_ref"]))
            expected = accept_summary_candidate(
                plan, summary["plan_ref"], resolve(summary["generation"]["prompt_ref"]),
                summary["generation"]["prompt_ref"], resolve(summary["generation"]["result_ref"]),
                summary["generation"]["result_ref"], summary["summary_id"])
            require(summary == expected, "context_summary_plan_mismatch",
                    "Summary differs from the exact model result and declared task gates")
    derivation = view["derivation"]
    ref, operation = derivation["input_view_ref"], derivation["operation"]
    if ref is None:
        require(operation == "read" and not view["units"] and not view["accepted_delta_ids"]
                and not view["applied_delta_ids"], "context_invalid_derivation",
                "Empty read cannot manufacture effective history")
    else:
        original = validate_effective_view(resolve(ref))
        if operation == "read":
            require(view["units"] == original["units"] and not view["applied_delta_ids"]
                    and view["accepted_delta_ids"] == original["accepted_delta_ids"] + original["applied_delta_ids"],
                    "context_invalid_derivation", "Read cannot restore replaced source rounds")
        elif operation == "window":
            count = len(view["units"])
            require(window_effective_view(original, ref, last_units=count) == view,
                    "context_invalid_derivation", "Window must preserve its complete effective suffix")
        elif operation == "merge":
            require(merge_effective_view(original, ref, resolve(derivation["round_ref"]),
                                         derivation["round_ref"]) == view,
                    "context_invalid_derivation", "Merge must append only its actual accepted Agent round")
        else:
            require(replace_context_prefix(
                original, ref, resolve(derivation["plan_ref"]), derivation["plan_ref"],
                resolve(derivation["summary_ref"]), derivation["summary_ref"]) == view,
                "context_invalid_derivation", "Replacement must consume its exact prefix plan and summary")
    if "context" in payload:
        packet = validate_agent_context(payload["context"])
        require(resolve(artifact_ref(payload["context_ref"])) == packet,
                "context_artifact_mismatch", "Round differs from its accepted Agent context")
        from .context_prompt import validate_summary_context_prompt
        prompt = validate_summary_context_prompt(resolve(packet["receipts"]["frozen_prompt_ref"]))
        require(prompt["context_ref"] == packet["basis_view_ref"] and prompt["context"] == view
                and prompt["current_input_ref"] == packet["current_root_ref"]
                and prompt["current_input"] == packet["unit"]["root"],
                "context_delta_prompt_mismatch", "Agent must actually use this exact replaced effective view")
        root = resolve(packet["current_root_ref"])
        require(root.get("kind") == "workflow.text" and root.get("schema_version") == 2
                and root.get("text") == packet["unit"]["root"]["content"],
                "context_delta_prompt_mismatch", "Agent root differs from its accepted input")
        return merge_effective_view(view, payload["view_ref"], packet, payload["context_ref"])
    return view


def prepare_effective_view_adoption(*, workflow_session_id, object_key, object_record, view_ref, view):
    view = validate_effective_view(view)
    require((object_record["type_id"], object_record["schema_version"]) == (EFFECTIVE_CONTEXT_TYPE, 3)
            and not object_record["deleted"], "context_object_type_mismatch", "Merge requires its versioned object")
    require(view["owner"]["workflow_session_id"] == workflow_session_id
            and view["owner"]["object_key"] == object_key, "context_save_owner_mismatch",
            "View belongs to another context object")
    current = validate_effective_context(object_record["value"])
    desired = {"view_ref": artifact_ref(view_ref),
               "accepted_delta_ids": view["accepted_delta_ids"] + view["applied_delta_ids"]}
    operation_key = "context-merge:" + content_digest({
        "owner": view["owner"], "basis": view["basis"], "desired": desired, "view_schema": 3})
    if current == desired:
        return {"desired_value": desired, "write_intent": None, "already_committed": True,
                "operation_key": operation_key}
    require(view["basis"] == {"revision_id": object_record["revision_id"], "head_revision": object_record["revision"]},
            "context_stale_basis", "Merge retains its exact frozen revision and CAS fence")
    require(current["accepted_delta_ids"] == view["accepted_delta_ids"], "context_consumption_mismatch",
            "Compression cannot modify the persistent consumed round boundary")
    return {"desired_value": desired, "already_committed": False, "operation_key": operation_key,
            "write_intent": WriteIntent(object_key, object_record["revision"], operation_key, desired).to_dict()}


def validate_effective_view_write(value, context):
    if context["operation"] == "delete":
        require(value is None, "context_invalid_write", "Deletion requires a tombstone")
        return
    value = validate_effective_context(value)
    current = context["current_record"]
    if context["operation"] == "initialize":
        require(current is None and value == {"view_ref": None, "accepted_delta_ids": []},
                "context_initialization_invalid", "Summary-aware context starts empty")
        return
    if current is not None and not current["deleted"] and value == current["value"]:
        return
    require(value["view_ref"] is not None, "context_invalid_write", "Context cannot discard consumed identities")
    resolver = context["resolve_artifact"]
    source = resolver(value["view_ref"])
    require((source["component_id"], source["component_version"]) == ("context.merge", "2"),
            "context_adoption_source_invalid", "Only the summary-aware merge writes its context object")
    view = prove_effective_view(value["view_ref"], resolver)
    prepared = prepare_effective_view_adoption(
        workflow_session_id=context["workflow_session_id"], object_key=context["object_key"],
        object_record=current, view_ref=value["view_ref"], view=view)
    require(prepared["desired_value"] == value, "context_consumption_mismatch",
            "Object pointer and consumption must equal the proven effective view")


def prove_effective_view(view_ref, resolve_detail):
    def input_matches(source, port, reference):
        refs = source.get("input_refs", {}).get(port, [])
        return len(refs) == 1 and refs[0]["output_id"] == reference["output_id"]

    visited, reference, result = set(), artifact_ref(view_ref), None
    while reference is not None:
        identity = reference["output_id"]
        require(identity not in visited, "context_invalid_derivation", "Effective lineage cannot cycle")
        visited.add(identity)
        source = resolve_detail(reference)
        view = validate_effective_view(source["value"])
        result = result or deepcopy(view)
        operation, derivation = view["derivation"]["operation"], view["derivation"]
        expected = {"read": ("context.output", "2"), "window": ("context.window", "3"),
                    "merge": ("context.merge", "2"), "replace": ("context.replace", "1")}[operation]
        require((source["component_id"], source["component_version"]) == expected,
                "context_adoption_source_invalid", "Effective view requires its official frozen producer")
        if operation in ("read", "merge"):
            require(source["config"]["object_key"] == view["owner"]["object_key"]
                    and source["config"]["agent_node_id"] == view["owner"]["agent_node_id"],
                    "context_agent_binding_mismatch", "Effective ownership differs from its frozen producer")
        if operation == "window":
            require(input_matches(source, "view", derivation["input_view_ref"])
                    and window_effective_view(resolve_detail(derivation["input_view_ref"])["value"],
                                          derivation["input_view_ref"],
                                          last_units=source["config"]["last_units"]) == view,
                    "context_adoption_source_invalid", "Window differs from its exact frozen configuration")
        if operation == "merge":
            origin = resolve_detail(derivation["round_ref"])
            require((origin["component_id"], origin["component_version"]) in (
                ("agents.execute", "3"), ("agents.execute", "7")),
                    "context_adoption_source_invalid", "Summary-aware merge requires its direct Agent version")
            packet = validate_agent_context(origin["value"])
            require(origin.get("producer") == packet["owner"]
                    and input_matches(origin, "prompt", packet["receipts"]["frozen_prompt_ref"])
                    and input_matches(origin, "model", packet["receipts"]["model_ref"])
                    and input_matches(source, "context", derivation["round_ref"])
                    and input_matches(source, "view", derivation["input_view_ref"]),
                    "context_agent_binding_mismatch", "Round proof requires its actual producer and frozen input identities")
            proven_merge = validate_effective_view_artifacts({
                "view_ref": derivation["input_view_ref"],
                "view": resolve_detail(derivation["input_view_ref"])["value"],
                "context_ref": derivation["round_ref"], "context": packet,
            }, lambda ref: resolve_detail(ref)["value"])
            require(proven_merge == view, "context_invalid_derivation",
                    "Persisted merge must use its Agent's actual frozen prompt and current input")
        if operation == "replace":
            from .context_artifacts import projection_id
            for field, component in (("plan_ref", "context.plan"), ("summary_ref", "context.summary")):
                origin = resolve_detail(derivation[field])
                require((origin["component_id"], origin["component_version"]) == (component, "1"),
                        "context_adoption_source_invalid", "Replacement requires its official plan and candidate")
            plan_source = resolve_detail(derivation["plan_ref"])
            plan = validate_compression_plan(plan_source["value"])
            require(plan.get("plan_id") == projection_id(
                plan_source["producer"]["node_run_id"], "compression-plan"),
                "context_adoption_source_invalid", "Plan identity must derive from its actual accepted invocation")
            require(plan_compression(resolve_detail(plan["basis_view_ref"])["value"], plan["basis_view_ref"],
                                     plan_source["config"], plan["plan_id"]) == plan,
                    "context_adoption_source_invalid", "Plan differs from its frozen policy")
            summary_source = resolve_detail(derivation["summary_ref"])
            summary = validate_context_summary(summary_source["value"])
            require(summary["summary_id"] == projection_id(
                summary_source["producer"]["node_run_id"], "context-summary"),
                "context_adoption_source_invalid", "Summary identity must derive from its actual accepted invocation")
            prompt_ref, result_ref = summary["generation"]["prompt_ref"], summary["generation"]["result_ref"]
            prompt_source, result_source = resolve_detail(prompt_ref), resolve_detail(result_ref)
            require(input_matches(source, "view", derivation["input_view_ref"])
                    and input_matches(source, "plan", derivation["plan_ref"])
                    and input_matches(source, "summary", derivation["summary_ref"])
                    and input_matches(plan_source, "view", plan["basis_view_ref"])
                    and input_matches(summary_source, "plan", summary["plan_ref"])
                    and input_matches(summary_source, "prompt", prompt_ref)
                    and input_matches(summary_source, "result", result_ref)
                    and (prompt_source["component_id"], prompt_source["component_version"]) == ("context.summary-prompt", "1")
                    and input_matches(prompt_source, "plan", summary["plan_ref"])
                    and (result_source["component_id"], result_source["component_version"]) == ("models.chat", "1")
                    and input_matches(result_source, "prompt", prompt_ref),
                    "context_summary_generation_unproven",
                    "Summary provenance requires its official producers and exact accepted input bindings")
        validate_effective_view_artifacts({"view_ref": reference, "view": view},
                                         lambda ref: resolve_detail(ref)["value"])
        reference = derivation["input_view_ref"]
    return result
