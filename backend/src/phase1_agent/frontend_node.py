"""Whole frontend node graph execution and accepted append settlement."""

from copy import deepcopy

from .content_contracts import object_schema
from .frontend_contracts import FRONTEND_COMMIT_TYPE, FRONTEND_STATE_TYPE, validate_frontend_commit
from .frontend_node_contracts import (
    FRONTEND_NODE_APPEND_TYPE, build_frontend_node_append, frontend_node_append_data_type,
    prepare_frontend_node_adoption,
)
from .graph_contracts import NodeDefinition, NodePort, require


FRONTEND_NODE_ENTRY = {
    "schema_version": 1, "component_id": "frontend.node", "component_version": "1",
    "page_prefix": "/frontend-node/", "api_prefix": "/api/graph/frontend-nodes/",
    "channel_suffix": "channel", "events_suffix": "events", "entry_suffix": "entry",
}


def _execute(config, inputs, context):
    record = context.object_read(config["object_key"])
    require(record["binding"].get("registrar_node_id") == context.node_binding_id,
            "frontend_node_registration_mismatch",
            "Frontend node must append to its own registered item")
    refs = context.input_artifact_refs("content")
    require(type(inputs["content"]) is list and len(inputs["content"]) == len(refs) and bool(refs),
            "frontend_exact_artifact_required", "Frontend messages require exact accepted inputs")
    for value, binding in zip(inputs["content"], refs):
        accepted = context.host_call("artifacts:read", "resolve-artifact", {
            "reference": {"scope": "artifact", "output_id": binding["output_id"]},
        })["value"]
        require(accepted == value, "frontend_artifact_mismatch",
                "Frontend message must equal its accepted source artifact")
    return {"proof": build_frontend_node_append(
        record, workflow_session_id=context.workflow_session_id, object_key=config["object_key"],
        source_output_refs=refs, role=config["role"], node_run_id=context.node_run_id,
    )}


def settle_frontend_node(context, outputs, output_refs):
    proof = deepcopy(outputs["proof"])
    key = proof["owner"]["object_key"]
    reference = {"scope": "artifact", "output_id": output_refs["proof"]}
    prepared = prepare_frontend_node_adoption(
        workflow_session_id=context.workflow_session_id, object_key=key,
        object_record=context.object_read(key), proof=proof, proof_ref=reference,
    )

    def finalize(staged, refs, receipts):
        require(type(receipts) is list and len(receipts) == 1,
                "frontend_receipt_unavailable", "Frontend append requires its accepted object receipt")
        require(type(receipts[0]) is dict
                and receipts[0].get("object_key") == key
                and receipts[0].get("revision") == proof["basis"]["head_revision"] + 1,
                "frontend_receipt_unavailable", "Frontend receipt must advance its exact object CAS basis")
        return {"proof": proof, "commit": validate_frontend_commit({
            "schema_version": 1, "kind": "workflow.frontend-commit",
            "owner": deepcopy(proof["owner"]), "view_ref": reference,
            "operation_key": prepared["operation_key"], "receipt": receipts[0],
        })}

    return {"outputs": {"proof": proof}, "object_writes": [prepared["write_intent"]],
            "finalize": finalize}


def register_frontend_node(host):
    host.register_data_type(frontend_node_append_data_type())
    key_schema = {"type": "string", "minLength": 1, "maxLength": 128}
    input_schema = {
        "type": "string", "maxLength": 36,
        "pattern": "^(?:|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$",
    }
    host.register_node(NodeDefinition(
        "frontend.node", "1", "前端节点", "Frontend",
        {"object_key": "frontend", "role": "assistant", "input_node_id": ""},
        object_schema({
            "object_key": key_schema, "role": {"enum": ["user", "assistant", "system"]},
            "input_node_id": input_schema,
        }),
        inputs=(NodePort("content", "TEXT", data_schema_version=2, multiple=True),),
        outputs=(NodePort("proof", FRONTEND_NODE_APPEND_TYPE), NodePort("commit", FRONTEND_COMMIT_TYPE)),
        is_output=True, capabilities=("objects:read", "objects:write", "artifacts:read"),
        input_storage="references",
        object_accesses=({"config_field": "object_key", "multiple": False, "access": "read_write",
                          "type_id": FRONTEND_STATE_TYPE, "schema_version": 1},),
        config_node_references=({
            "config_field": "input_node_id", "multiple": False, "allow_empty": True,
            "target_types": [{"component_id": "tools.editable-input", "component_version": "1"}],
        },),
        provided_session_item_types=({"type_id": FRONTEND_STATE_TYPE, "schema_version": 1},),
    ), _execute, settlement_builder=settle_frontend_node)
    from .frontend_node_v2 import register_frontend_chat_node
    register_frontend_chat_node(host)
