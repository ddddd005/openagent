"""Frontend reference state and explicit public presentation business nodes."""

from copy import deepcopy

from .content_contracts import object_schema
from .frontend_contracts import (
    EMPTY_FRONTEND_STATE, FRONTEND_COMMIT_TYPE, FRONTEND_STATE_TYPE, FRONTEND_VIEW_TYPE, MAX_FRONTEND_ENTRIES,
    FRONTEND_DISPLAY_TYPE, frontend_display_references, validate_frontend_display,
    append_frontend_view, frontend_state_references, frontend_view_artifact_bindings,
    frontend_view_references, prepare_frontend_adoption, preserve_frontend_references,
    read_frontend_view, validate_frontend_commit, validate_frontend_state,
    validate_frontend_state_write, validate_frontend_view,
)
from .graph_contracts import NodeDefinition, NodePort, require
from .host_sdk import DataTypeDefinition


def _read(config, inputs, context):
    return {"view": read_frontend_view(
        context.object_read(config["object_key"]), context.workflow_session_id, config["object_key"])}


def _append(config, inputs, context):
    record = context.object_read(config["object_key"])
    view = validate_frontend_view(inputs["view"])
    require(view == read_frontend_view(record, context.workflow_session_id, config["object_key"]),
            "frontend_stale_basis", "Frontend append requires the current object read")
    bindings = context.input_artifact_refs("view")
    require(len(bindings) == 1, "frontend_exact_artifact_required",
            "Frontend append requires exactly one view artifact")
    refs = context.input_artifact_refs("content")
    require(type(inputs["content"]) is list and len(inputs["content"]) == len(refs) and bool(refs),
            "frontend_exact_artifact_required", "Frontend display inputs require exact accepted artifacts")
    for value, binding in zip(inputs["content"], refs):
        accepted = context.host_call("artifacts:read", "resolve-artifact", {
            "reference": {"scope": "artifact", "output_id": binding["output_id"]}})["value"]
        require(accepted == value, "frontend_artifact_mismatch",
                "Display input must equal its exact accepted source artifact")
    return {"view": append_frontend_view(
        view, bindings[0], refs, role=config["role"], node_run_id=context.node_run_id)}


def _present(config, inputs, context):
    view = validate_frontend_view(inputs["view"])
    bindings = context.input_artifact_refs("view")
    require(len(bindings) == 1, "frontend_exact_artifact_required",
            "Presentation requires one exact accepted view")
    source = context.host_call("artifacts:read", "resolve-artifact", {
        "reference": {"scope": "artifact", "output_id": bindings[0]["output_id"]}})
    origin = context.host_call("artifacts:read", "describe-input-origin", {"port": "view"})
    require(source["value"] == view
            and (origin["component_id"], origin["component_version"])
            in (("frontend.state.output", "1"), ("frontend.state.append", "1")),
            "frontend_unproven_presentation", "Presentation requires a registered accepted frontend view")
    return {"display": validate_frontend_display({
        "schema_version": 1, "kind": "workflow.frontend-display", "entries": view["entries"]})}


def settle_frontend_append(context, outputs, output_refs):
    view = validate_frontend_view(outputs["view"])
    key = view["owner"]["object_key"]
    reference = {"scope": "artifact", "output_id": output_refs["view"]}
    prepared = prepare_frontend_adoption(
        workflow_session_id=context.workflow_session_id, object_key=key,
        object_record=context.object_read(key), view=view, view_ref=reference)

    def finalize(staged, refs, receipts):
        require(type(receipts) is list and len(receipts) == 1,
                "frontend_receipt_unavailable", "Append requires its actual accepted object receipt")
        require(type(receipts[0]) is dict
                and receipts[0].get("revision") == view["basis"]["head_revision"] + 1,
                "frontend_receipt_unavailable", "Append receipt must advance its exact frozen CAS basis")
        return {"view": view, "commit": validate_frontend_commit({
            "schema_version": 1, "kind": "workflow.frontend-commit", "owner": deepcopy(view["owner"]),
            "view_ref": reference, "operation_key": prepared["operation_key"], "receipt": receipts[0],
        })}

    return {"outputs": {"view": view}, "object_writes": [prepared["write_intent"]], "finalize": finalize}


def register_frontend_business(host):
    """Register business capabilities; the complete UI package host is separate."""
    host.register_data_type(DataTypeDefinition(
        FRONTEND_STATE_TYPE, 1, object_schema({
            "entries": {"type": "array", "maxItems": MAX_FRONTEND_ENTRIES},
            "view_ref": {"type": ["object", "null"]}}),
        EMPTY_FRONTEND_STATE, validator=validate_frontend_state, references=frontend_state_references,
        reference_mapper=preserve_frontend_references, write_validator=validate_frontend_state_write))
    host.register_data_type(DataTypeDefinition(
        FRONTEND_VIEW_TYPE, 1, {"type": "object"}, scope="content", validator=validate_frontend_view,
        references=frontend_view_references, reference_mapper=preserve_frontend_references,
        artifact_bindings=frontend_view_artifact_bindings, max_bytes=4_000_000))
    host.register_data_type(DataTypeDefinition(
        FRONTEND_COMMIT_TYPE, 1, {"type": "object"}, scope="content", validator=validate_frontend_commit,
        references=lambda value: [deepcopy(value["view_ref"])],
        reference_mapper=preserve_frontend_references))
    host.register_data_type(DataTypeDefinition(
        FRONTEND_DISPLAY_TYPE, 1, {"type": "object"}, scope="content",
        validator=validate_frontend_display, references=frontend_display_references,
        reference_mapper=preserve_frontend_references,
        public_references=frontend_display_references, max_bytes=4_000_000))
    key_schema = {"type": "string", "minLength": 1, "maxLength": 128}
    access = {"config_field": "object_key", "type_id": FRONTEND_STATE_TYPE,
              "schema_version": 1, "multiple": False}
    host.register_node(NodeDefinition(
        "frontend.state.output", "1", "Read frontend business state", "Frontend",
        {"object_key": "frontend"}, object_schema({"object_key": key_schema}),
        outputs=(NodePort("view", FRONTEND_VIEW_TYPE),), capabilities=("objects:read",),
        input_storage="references", object_accesses=({**access, "access": "read"},)), _read)
    host.register_node(NodeDefinition(
        "frontend.state.append", "1", "Append frontend display references", "Frontend",
        {"object_key": "frontend", "role": "assistant"},
        object_schema({"object_key": key_schema, "role": {"enum": ["user", "assistant", "system"]}}),
        inputs=(NodePort("view", FRONTEND_VIEW_TYPE),
                NodePort("content", "TEXT", data_schema_version=2, multiple=True)),
        outputs=(NodePort("view", FRONTEND_VIEW_TYPE), NodePort("commit", FRONTEND_COMMIT_TYPE)),
        capabilities=("objects:read", "objects:write", "artifacts:read"),
        input_storage="references", object_accesses=({**access, "access": "read_write"},)),
        _append, settlement_builder=settle_frontend_append)
    host.register_node(NodeDefinition(
        "frontend.presentation", "1", "Present frontend display references", "Frontend",
        {}, object_schema({}), inputs=(NodePort("view", FRONTEND_VIEW_TYPE),),
        outputs=(NodePort("display", FRONTEND_DISPLAY_TYPE),),
        capabilities=("artifacts:read",), input_storage="references"), _present)


def create_frontend_business_package():
    from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
    return CapabilityPackage(PackageManifest(
        "workflow.frontend-business", "1.0.0",
        dependencies=(PackageDependency("workflow.content", "1.0.0"),)), register_frontend_business)
