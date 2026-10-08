"""Tavern display references adapted from phase1_agent.frontend_contracts.

This host-source copy retains the original proof and immutable-reference rules
with isolated tavern identities; it contains no SillyTavern source.
"""

from copy import deepcopy

from ..content_contracts import stable_item_id
from ..contract_json import content_digest, validate_json_value
from ..graph_contracts import require, uuid4_string
from ..host_sdk import WriteIntent, bounded_name, validate_reference


TAVERN_CHAT_STATE_TYPE = "workflow.tavern.chat-state"
TAVERN_CHAT_VIEW_TYPE = "TAVERN_CHAT_VIEW"
TAVERN_CHAT_COMMIT_TYPE = "TAVERN_CHAT_COMMIT"
TAVERN_CHAT_DISPLAY_TYPE = "TAVERN_CHAT_DISPLAY"
# Leave one reference slot for the accepted settlement view.
MAX_TAVERN_CHAT_ENTRIES = 4095
EMPTY_TAVERN_CHAT_STATE = {"entries": [], "view_ref": None}


def _fields(value, fields):
    require(type(value) is dict and set(value) == set(fields),
            "tavern_chat_invalid_contract", "Tavern chat contract fields differ")


def _artifact(value):
    reference = validate_reference(value)
    require(reference["scope"] == "artifact", "tavern_chat_exact_artifact_required",
            "Tavern chat entries require exact immutable artifacts")
    return reference


def _binding(value):
    _fields(value, ("edge_id", "output_id", "order"))
    uuid4_string(value["edge_id"])
    uuid4_string(value["output_id"])
    require(type(value["order"]) is int and 0 <= value["order"] <= 2**53 - 1,
            "tavern_chat_invalid_contract", "Input order must be a bounded integer")


def _validate_entries(entries):
    require(type(entries) is list and len(entries) <= MAX_TAVERN_CHAT_ENTRIES,
            "tavern_chat_capacity_exceeded", "Tavern chat entries exceed the declared limit")
    identities = []
    for entry in entries:
        _fields(entry, ("entry_id", "role", "source_ref"))
        identities.append(uuid4_string(entry["entry_id"]))
        require(entry["role"] in ("user", "assistant", "system"),
                "tavern_chat_invalid_contract", "Display role must be explicit")
        _artifact(entry["source_ref"])
    require(len(identities) == len(set(identities)), "tavern_chat_duplicate_entry",
            "Tavern chat entries require unique logical identities")


def _unique_refs(references):
    return [deepcopy(ref) for _, ref in sorted(
        {reference["output_id"]: reference for reference in references}.items())]


def validate_tavern_chat_display(value):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "entries"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow.tavern-chat-display",
            "tavern_chat_invalid_contract", "Tavern chat display envelope is invalid")
    _validate_entries(value["entries"])
    return deepcopy(value)


def tavern_chat_display_references(value):
    return _unique_refs(entry["source_ref"] for entry in value["entries"])


def validate_tavern_chat_state(value):
    validate_json_value(value)
    _fields(value, ("entries", "view_ref"))
    if value["view_ref"] is not None:
        _artifact(value["view_ref"])
    _validate_entries(value["entries"])
    require(value["view_ref"] is not None or not value["entries"], "tavern_chat_unproven_write",
            "Retained entries require their accepted settlement view")
    return deepcopy(value)


def tavern_chat_state_references(value):
    references = [entry["source_ref"] for entry in value["entries"]]
    if value["view_ref"] is not None:
        references.append(value["view_ref"])
    return _unique_refs(references)


def preserve_tavern_chat_references(value, mapping):
    """Inherited immutable sources stay fixed; a new read supplies its new owner."""
    return deepcopy(value)


def validate_tavern_chat_view(value):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "owner", "basis", "entries", "derivation"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow.tavern-chat-view",
            "tavern_chat_invalid_contract", "Tavern chat view envelope is invalid")
    _fields(value["owner"], ("workflow_session_id", "object_key"))
    uuid4_string(value["owner"]["workflow_session_id"])
    require(bounded_name(value["owner"]["object_key"]), "tavern_chat_invalid_contract",
            "Tavern chat owner must identify a bound object")
    _fields(value["basis"], ("revision_id", "head_revision"))
    uuid4_string(value["basis"]["revision_id"])
    require(type(value["basis"]["head_revision"]) is int
            and 1 <= value["basis"]["head_revision"] <= 2**53 - 1,
            "tavern_chat_invalid_contract", "Tavern chat view requires an exact CAS basis")
    _validate_entries(value["entries"])
    derivation = value["derivation"]
    _fields(derivation, ("operation", "view_ref", "view_binding", "source_output_refs", "role", "node_run_id"))
    require(derivation["operation"] in ("read", "append"),
            "tavern_chat_invalid_contract", "Tavern chat operation is unsupported")
    refs = derivation["source_output_refs"]
    require(type(refs) is list and len(refs) <= MAX_TAVERN_CHAT_ENTRIES,
            "tavern_chat_capacity_exceeded", "Tavern chat inputs must be bounded")
    for reference in refs:
        _binding(reference)
    require(len({ref["output_id"] for ref in refs}) == len(refs),
            "tavern_chat_duplicate_source", "One invocation cannot append a source twice")
    if derivation["operation"] == "read":
        require(not refs and all(derivation[key] is None
                                 for key in ("view_ref", "view_binding", "role", "node_run_id")),
                "tavern_chat_invalid_contract", "Read cannot manufacture appended entries")
    else:
        _artifact(derivation["view_ref"])
        _binding(derivation["view_binding"])
        uuid4_string(derivation["node_run_id"])
        require(bool(refs) and derivation["role"] in ("user", "assistant", "system")
                and derivation["view_binding"]["output_id"] == derivation["view_ref"]["output_id"],
                "tavern_chat_invalid_contract", "Append requires exact input and invocation identities")
    return deepcopy(value)


def tavern_chat_view_references(value):
    references = [entry["source_ref"] for entry in value["entries"]]
    if value["derivation"]["view_ref"] is not None:
        references.append(value["derivation"]["view_ref"])
    return _unique_refs(references)


def tavern_chat_view_artifact_bindings(value):
    derivation = value["derivation"]
    return ([] if derivation["view_binding"] is None else [deepcopy(derivation["view_binding"])]) + deepcopy(
        derivation["source_output_refs"])


def read_tavern_chat_view(record, workflow_session_id, object_key):
    require((record["type_id"], record["schema_version"]) == (TAVERN_CHAT_STATE_TYPE, 1)
            and not record["deleted"], "tavern_chat_object_type_mismatch",
            "Tavern chat read requires its registered state object")
    state = validate_tavern_chat_state(record["value"])
    return validate_tavern_chat_view({
        "schema_version": 1, "kind": "workflow.tavern-chat-view",
        "owner": {"workflow_session_id": workflow_session_id, "object_key": object_key},
        "basis": {"revision_id": record["revision_id"], "head_revision": record["revision"]},
        "entries": state["entries"], "derivation": {
            "operation": "read", "view_ref": None, "view_binding": None,
            "source_output_refs": [], "role": None, "node_run_id": None},
    })


def append_tavern_chat_view(view, view_binding, source_output_refs, *, role, node_run_id):
    result = validate_tavern_chat_view(view)
    require(result["derivation"]["operation"] == "read", "tavern_chat_invalid_basis",
            "Append requires a fresh state read")
    _binding(view_binding)
    uuid4_string(node_run_id)
    require(role in ("user", "assistant", "system") and type(source_output_refs) is list
            and 0 < len(source_output_refs) <= MAX_TAVERN_CHAT_ENTRIES,
            "tavern_chat_invalid_contract", "Append requires bounded exact source bindings and a display role")
    for binding in source_output_refs:
        _binding(binding)
    result["entries"].extend({
        "entry_id": stable_item_id(node_run_id, reference["output_id"]), "role": role,
        "source_ref": {"scope": "artifact", "output_id": reference["output_id"]},
    } for reference in source_output_refs)
    result["derivation"] = {
        "operation": "append", "view_ref": {"scope": "artifact", "output_id": view_binding["output_id"]},
        "view_binding": deepcopy(view_binding), "source_output_refs": deepcopy(source_output_refs),
        "role": role, "node_run_id": node_run_id,
    }
    return validate_tavern_chat_view(result)


def prepare_tavern_chat_adoption(*, workflow_session_id, object_key, object_record, view, view_ref):
    view = validate_tavern_chat_view(view)
    _artifact(view_ref)
    require((object_record["type_id"], object_record["schema_version"]) == (TAVERN_CHAT_STATE_TYPE, 1)
            and not object_record["deleted"], "tavern_chat_object_type_mismatch",
            "Tavern chat adoption requires a live registered state object")
    require(view["owner"] == {"workflow_session_id": workflow_session_id, "object_key": object_key}
            and view["derivation"]["operation"] == "append",
            "tavern_chat_owner_mismatch", "Append belongs to another tavern chat object")
    require(view["basis"] == {
        "revision_id": object_record["revision_id"], "head_revision": object_record["revision"]},
        "tavern_chat_stale_basis", "Append must retain its frozen object revision")
    count = len(view["derivation"]["source_output_refs"])
    require(view["entries"][:-count] == validate_tavern_chat_state(object_record["value"])["entries"],
            "tavern_chat_state_mismatch", "Append must preserve the entire retained state")
    desired = {"entries": deepcopy(view["entries"]), "view_ref": _artifact(view_ref)}
    operation_key = "tavern-chat-append:" + content_digest({
        "owner": view["owner"], "basis": view["basis"], "view_ref": view_ref})
    return {"desired_value": desired, "operation_key": operation_key,
            "write_intent": WriteIntent(object_key, object_record["revision"], operation_key, desired).to_dict()}


def validate_tavern_chat_state_write(value, context):
    if context["operation"] == "initialize":
        require(value == EMPTY_TAVERN_CHAT_STATE, "tavern_chat_initialization_invalid",
                "Tavern chat state starts empty")
        return
    require(context["operation"] == "put", "tavern_chat_invalid_write",
            "Tavern chat state cannot discard its accepted entries")
    value = validate_tavern_chat_state(value)
    current = context["current_record"]
    require(current is not None and not current["deleted"], "tavern_chat_invalid_write",
            "Tavern chat adoption requires a live object")
    if value == current["value"]:
        return
    require(value["view_ref"] is not None, "tavern_chat_unproven_write",
            "Tavern chat writes require their exact accepted settlement view")
    resolve = context["resolve_artifact"]
    source = resolve(value["view_ref"])
    require((source["component_id"], source["component_version"]) == ("tavern.chat.append", "1"),
            "tavern_chat_unproven_write", "Only the registered tavern append node may advance chat state")
    view = validate_tavern_chat_view(source["value"])
    derivation = view["derivation"]
    require(source["config"]["object_key"] == context["object_key"]
            and source["config"]["role"] == derivation["role"]
            and source["producer"]["workflow_session_id"] == context["workflow_session_id"]
            and source["producer"]["node_run_id"] == derivation["node_run_id"]
            and source["input_refs"].get("view") == [derivation["view_binding"]]
            and source["input_refs"].get("content") == derivation["source_output_refs"],
            "tavern_chat_unproven_write", "Append must use its actual frozen config and input bindings")
    original = resolve(derivation["view_ref"])
    require((original["component_id"], original["component_version"]) == ("tavern.chat.output", "1")
            and original["config"]["object_key"] == context["object_key"]
            and original["producer"]["workflow_session_id"] == context["workflow_session_id"],
            "tavern_chat_unproven_write", "Append requires the exact registered tavern object read")
    original_view = validate_tavern_chat_view(original["value"])
    require(original_view == read_tavern_chat_view(
        current, context["workflow_session_id"], context["object_key"]),
        "tavern_chat_state_mismatch", "Append input must be the current authorized object read")
    for binding in derivation["source_output_refs"]:
        content = resolve({"scope": "artifact", "output_id": binding["output_id"]})["value"]
        require(type(content) is dict and set(content) == {"schema_version", "kind", "text"}
                and type(content["schema_version"]) is int and content["schema_version"] == 2
                and content["kind"] == "workflow.text" and type(content["text"]) is str,
                "tavern_chat_source_type_mismatch", "Display sources must be accepted TEXT@2 artifacts")
    require(append_tavern_chat_view(
        original_view, derivation["view_binding"], derivation["source_output_refs"],
        role=derivation["role"], node_run_id=derivation["node_run_id"]) == view,
        "tavern_chat_unproven_write", "Append cannot alter retained entries or manufacture source identities")
    prepared = prepare_tavern_chat_adoption(
        workflow_session_id=context["workflow_session_id"], object_key=context["object_key"],
        object_record=current, view=view, view_ref=value["view_ref"])
    require(prepared["desired_value"] == value, "tavern_chat_state_mismatch",
            "Tavern chat state must equal its proven accepted output")


def validate_tavern_chat_commit(value):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "owner", "view_ref", "operation_key", "receipt"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow.tavern-chat-commit", "tavern_chat_invalid_contract",
            "Tavern chat commit envelope is invalid")
    _fields(value["owner"], ("workflow_session_id", "object_key"))
    uuid4_string(value["owner"]["workflow_session_id"])
    require(bounded_name(value["owner"]["object_key"]) and bounded_name(value["operation_key"]),
            "tavern_chat_invalid_contract", "Tavern chat commit identities are invalid")
    _artifact(value["view_ref"])
    receipt = value["receipt"]
    _fields(receipt, ("object_key", "revision", "revision_id", "deleted"))
    uuid4_string(receipt["revision_id"])
    require(receipt["object_key"] == value["owner"]["object_key"]
            and type(receipt["revision"]) is int and 1 <= receipt["revision"] <= 2**53 - 1
            and receipt["deleted"] is False, "tavern_chat_invalid_contract",
            "Tavern chat commit must contain its actual accepted object receipt")
    return deepcopy(value)
