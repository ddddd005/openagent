"""Tavern business nodes adapted from phase1_agent.frontend_business.

This host-source copy retains accepted-artifact settlement with isolated tavern
identities; it contains no SillyTavern source or browser initialization.
"""

from copy import deepcopy

from ..content_contracts import object_schema
from ..graph_contracts import NodeDefinition, NodePort, require
from ..host_sdk import DataTypeDefinition
from .chat_contracts import (
    EMPTY_TAVERN_CHAT_STATE, MAX_TAVERN_CHAT_ENTRIES, TAVERN_CHAT_COMMIT_TYPE,
    TAVERN_CHAT_DISPLAY_TYPE, TAVERN_CHAT_STATE_TYPE, TAVERN_CHAT_VIEW_TYPE,
    append_tavern_chat_view, preserve_tavern_chat_references, prepare_tavern_chat_adoption,
    read_tavern_chat_view, tavern_chat_display_references, tavern_chat_state_references,
    tavern_chat_view_artifact_bindings, tavern_chat_view_references,
    validate_tavern_chat_commit, validate_tavern_chat_display, validate_tavern_chat_state,
    validate_tavern_chat_state_write, validate_tavern_chat_view,
)


TAVERN_CHAT_COMPONENTS = ("tavern.chat.output", "tavern.chat.append", "tavern.chat.presentation")


def _read(config, inputs, context):
    return {"view": read_tavern_chat_view(
        context.object_read(config["object_key"]), context.workflow_session_id, config["object_key"])}


def _append(config, inputs, context):
    record = context.object_read(config["object_key"])
    view = validate_tavern_chat_view(inputs["view"])
    require(view == read_tavern_chat_view(record, context.workflow_session_id, config["object_key"]),
            "tavern_chat_stale_basis", "Tavern chat append requires the current object read")
    bindings = context.input_artifact_refs("view")
    require(len(bindings) == 1, "tavern_chat_exact_artifact_required",
            "Tavern chat append requires exactly one view artifact")
    refs = context.input_artifact_refs("content")
    require(type(inputs["content"]) is list and len(inputs["content"]) == len(refs) and bool(refs),
            "tavern_chat_exact_artifact_required", "Tavern chat inputs require exact accepted artifacts")
    for value, binding in zip(inputs["content"], refs):
        accepted = context.host_call("artifacts:read", "resolve-artifact", {
            "reference": {"scope": "artifact", "output_id": binding["output_id"]}})["value"]
        require(accepted == value, "tavern_chat_artifact_mismatch",
                "Display input must equal its exact accepted source artifact")
    return {"view": append_tavern_chat_view(
        view, bindings[0], refs, role=config["role"], node_run_id=context.node_run_id)}


def _present(config, inputs, context):
    view = validate_tavern_chat_view(inputs["view"])
    bindings = context.input_artifact_refs("view")
    require(len(bindings) == 1, "tavern_chat_exact_artifact_required",
            "Tavern presentation requires one exact accepted view")
    source = context.host_call("artifacts:read", "resolve-artifact", {
        "reference": {"scope": "artifact", "output_id": bindings[0]["output_id"]}})
    origin = context.host_call("artifacts:read", "describe-input-origin", {"port": "view"})
    require(source["value"] == view
            and (origin["component_id"], origin["component_version"])
            in (("tavern.chat.output", "1"), ("tavern.chat.append", "1")),
            "tavern_chat_unproven_presentation", "Presentation requires a registered accepted tavern chat view")
    return {"display": validate_tavern_chat_display({
        "schema_version": 1, "kind": "workflow.tavern-chat-display", "entries": view["entries"]})}


def settle_tavern_chat_append(context, outputs, output_refs):
    view = validate_tavern_chat_view(outputs["view"])
    key = view["owner"]["object_key"]
    reference = {"scope": "artifact", "output_id": output_refs["view"]}
    prepared = prepare_tavern_chat_adoption(
        workflow_session_id=context.workflow_session_id, object_key=key,
        object_record=context.object_read(key), view=view, view_ref=reference)

    def finalize(staged, refs, receipts):
        require(type(receipts) is list and len(receipts) == 1,
                "tavern_chat_receipt_unavailable", "Append requires its actual accepted object receipt")
        require(type(receipts[0]) is dict
                and receipts[0].get("revision") == view["basis"]["head_revision"] + 1,
                "tavern_chat_receipt_unavailable", "Append receipt must advance its exact frozen CAS basis")
        return {"view": view, "commit": validate_tavern_chat_commit({
            "schema_version": 1, "kind": "workflow.tavern-chat-commit", "owner": deepcopy(view["owner"]),
            "view_ref": reference, "operation_key": prepared["operation_key"], "receipt": receipts[0],
        })}

    return {"outputs": {"view": view}, "object_writes": [prepared["write_intent"]], "finalize": finalize}


def register_tavern_chat(host):
    """Register tavern-only contracts and the exact display provenance chain."""
    host.register_data_type(DataTypeDefinition(
        TAVERN_CHAT_STATE_TYPE, 1, object_schema({
            "entries": {"type": "array", "maxItems": MAX_TAVERN_CHAT_ENTRIES},
            "view_ref": {"type": ["object", "null"]}}),
        EMPTY_TAVERN_CHAT_STATE, validator=validate_tavern_chat_state, references=tavern_chat_state_references,
        reference_mapper=preserve_tavern_chat_references, write_validator=validate_tavern_chat_state_write))
    host.register_data_type(DataTypeDefinition(
        TAVERN_CHAT_VIEW_TYPE, 1, {"type": "object"}, scope="content", validator=validate_tavern_chat_view,
        references=tavern_chat_view_references, reference_mapper=preserve_tavern_chat_references,
        artifact_bindings=tavern_chat_view_artifact_bindings, max_bytes=4_000_000))
    host.register_data_type(DataTypeDefinition(
        TAVERN_CHAT_COMMIT_TYPE, 1, {"type": "object"}, scope="content", validator=validate_tavern_chat_commit,
        references=lambda value: [deepcopy(value["view_ref"])],
        reference_mapper=preserve_tavern_chat_references))
    host.register_data_type(DataTypeDefinition(
        TAVERN_CHAT_DISPLAY_TYPE, 1, {"type": "object"}, scope="content",
        validator=validate_tavern_chat_display, references=tavern_chat_display_references,
        reference_mapper=preserve_tavern_chat_references,
        public_references=tavern_chat_display_references, max_bytes=4_000_000))
    key_schema = {"type": "string", "minLength": 1, "maxLength": 128}
    access = {"config_field": "object_key", "type_id": TAVERN_CHAT_STATE_TYPE,
              "schema_version": 1, "multiple": False}
    host.register_node(NodeDefinition(
        "tavern.chat.output", "1", "Read tavern chat display state", "Tavern",
        {"object_key": "tavern-chat"}, object_schema({"object_key": key_schema}),
        outputs=(NodePort("view", TAVERN_CHAT_VIEW_TYPE),), capabilities=("objects:read",),
        input_storage="references", object_accesses=({**access, "access": "read"},)), _read)
    host.register_node(NodeDefinition(
        "tavern.chat.append", "1", "Append tavern chat display references", "Tavern",
        {"object_key": "tavern-chat", "role": "assistant"},
        object_schema({"object_key": key_schema, "role": {"enum": ["user", "assistant", "system"]}}),
        inputs=(NodePort("view", TAVERN_CHAT_VIEW_TYPE),
                NodePort("content", "TEXT", data_schema_version=2, multiple=True)),
        outputs=(NodePort("view", TAVERN_CHAT_VIEW_TYPE), NodePort("commit", TAVERN_CHAT_COMMIT_TYPE)),
        capabilities=("objects:read", "objects:write", "artifacts:read"),
        input_storage="references", object_accesses=({**access, "access": "read_write"},)),
        _append, settlement_builder=settle_tavern_chat_append)
    host.register_node(NodeDefinition(
        "tavern.chat.presentation", "1", "Present tavern chat display references", "Tavern",
        {}, object_schema({}), inputs=(NodePort("view", TAVERN_CHAT_VIEW_TYPE),),
        outputs=(NodePort("display", TAVERN_CHAT_DISPLAY_TYPE),),
        capabilities=("artifacts:read",), input_storage="references"), _present)
