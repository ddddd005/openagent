"""Exact append evidence for the frontend node's own registered session item."""

from copy import deepcopy

from .content_contracts import stable_item_id
from .contract_json import content_digest, validate_json_value
from .frontend_contracts import (
    MAX_FRONTEND_ENTRIES, preserve_frontend_references, read_frontend_view, validate_frontend_display,
)
from .graph_contracts import require, uuid4_string
from .host_sdk import DataTypeDefinition, WriteIntent, bounded_name, validate_reference


FRONTEND_NODE_APPEND_TYPE = "FRONTEND_NODE_APPEND"
FRONTEND_NODE_COMPONENT = ("frontend.node", "1")
FRONTEND_NODE_CHAT_COMPONENT = ("frontend.node", "2")
FRONTEND_NODE_COMPONENTS = frozenset((FRONTEND_NODE_COMPONENT, FRONTEND_NODE_CHAT_COMPONENT))


def _fields(value, fields):
    require(type(value) is dict and set(value) == set(fields),
            "frontend_node_invalid_proof", "Frontend node proof fields differ")


def validate_frontend_node_append(value):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "owner", "basis", "entries",
                    "source_output_refs", "role", "node_run_id"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow.frontend-node-append",
            "frontend_node_invalid_proof", "Frontend node append envelope is invalid")
    _fields(value["owner"], ("workflow_session_id", "object_key"))
    uuid4_string(value["owner"]["workflow_session_id"])
    require(bounded_name(value["owner"]["object_key"]), "frontend_node_invalid_proof",
            "Frontend node proof requires an exact object owner")
    _fields(value["basis"], ("revision_id", "head_revision"))
    uuid4_string(value["basis"]["revision_id"])
    require(type(value["basis"]["head_revision"]) is int
            and 1 <= value["basis"]["head_revision"] <= 2**53 - 1,
            "frontend_node_invalid_proof", "Frontend node proof requires an exact CAS basis")
    validate_frontend_display({
        "schema_version": 1, "kind": "workflow.frontend-display", "entries": value["entries"]})
    refs = value["source_output_refs"]
    require(type(refs) is list and 0 < len(refs) <= MAX_FRONTEND_ENTRIES,
            "frontend_node_invalid_proof", "Frontend node append requires exact source bindings")
    for binding in refs:
        _fields(binding, ("edge_id", "output_id", "order"))
        uuid4_string(binding["edge_id"])
        uuid4_string(binding["output_id"])
        require(type(binding["order"]) is int and 0 <= binding["order"] <= 2**53 - 1,
                "frontend_node_invalid_proof", "Frontend node input order must be a bounded integer")
    require(len({binding["output_id"] for binding in refs}) == len(refs),
            "frontend_duplicate_source", "One frontend node invocation cannot append a source twice")
    require(value["role"] in ("user", "assistant", "system"), "frontend_node_invalid_proof",
            "Frontend node append requires an explicit role")
    uuid4_string(value["node_run_id"])
    return deepcopy(value)


def frontend_node_append_references(value):
    return [deepcopy(reference) for _, reference in sorted(
        {entry["source_ref"]["output_id"]: entry["source_ref"] for entry in value["entries"]}.items())]


def frontend_node_append_artifact_bindings(value):
    return deepcopy(value["source_output_refs"])


def frontend_node_append_data_type():
    return DataTypeDefinition(
        FRONTEND_NODE_APPEND_TYPE, 1, {"type": "object"}, scope="content",
        validator=validate_frontend_node_append, references=frontend_node_append_references,
        reference_mapper=preserve_frontend_references,
        artifact_bindings=frontend_node_append_artifact_bindings, max_bytes=4_000_000)


def build_frontend_node_append(record, *, workflow_session_id, object_key, source_output_refs, role, node_run_id):
    original = read_frontend_view(record, workflow_session_id, object_key)
    entries = deepcopy(original["entries"])
    proof = {
        "schema_version": 1, "kind": "workflow.frontend-node-append",
        "owner": deepcopy(original["owner"]), "basis": deepcopy(original["basis"]),
        "entries": entries, "source_output_refs": deepcopy(source_output_refs),
        "role": role, "node_run_id": node_run_id,
    }
    # Validate identities before using them to derive stable entry IDs.
    validate_frontend_node_append(proof)
    entries.extend({
        "entry_id": stable_item_id(node_run_id, binding["output_id"]), "role": role,
        "source_ref": {"scope": "artifact", "output_id": binding["output_id"]},
    } for binding in source_output_refs)
    return validate_frontend_node_append(proof)


def prepare_frontend_node_adoption(*, workflow_session_id, object_key, object_record, proof, proof_ref):
    proof = validate_frontend_node_append(proof)
    proof_ref = validate_reference(proof_ref)
    require(proof_ref["scope"] == "artifact", "frontend_exact_artifact_required",
            "Frontend node adoption requires its exact append artifact")
    require(proof["owner"] == {"workflow_session_id": workflow_session_id, "object_key": object_key},
            "frontend_owner_mismatch", "Frontend node append belongs to another object")
    require(proof["basis"] == {
        "revision_id": object_record["revision_id"], "head_revision": object_record["revision"]},
        "frontend_stale_basis", "Frontend node append must retain its exact object revision")
    expected = build_frontend_node_append(
        object_record, workflow_session_id=workflow_session_id, object_key=object_key,
        source_output_refs=proof["source_output_refs"], role=proof["role"], node_run_id=proof["node_run_id"])
    require(proof == expected, "frontend_state_mismatch",
            "Frontend node append cannot alter retained entries or manufacture source identities")
    desired = {"entries": deepcopy(proof["entries"]), "view_ref": proof_ref}
    operation_key = "frontend-node-append:" + content_digest({
        "owner": proof["owner"], "basis": proof["basis"], "proof_ref": proof_ref})
    return {
        "desired_value": desired, "operation_key": operation_key,
        "write_intent": WriteIntent(object_key, object_record["revision"], operation_key, desired).to_dict(),
    }


def validate_frontend_node_state_write(value, context, source):
    proof = validate_frontend_node_append(source["value"])
    producer = source["producer"]
    current = context["current_record"]
    binding = current["binding"]
    config = source["consumed_config"]
    require((source["component_id"], source["component_version"]) == FRONTEND_NODE_COMPONENT
            and config["object_key"] == context["object_key"]
            and config["role"] == proof["role"]
            and producer["workflow_session_id"] == context["workflow_session_id"]
            and producer["node_run_id"] == proof["node_run_id"]
            and binding.get("registrar_node_id") == producer["node_binding_id"]
            and context.get("writer_node_id") == producer["node_binding_id"]
            and source["input_refs"].get("content") == proof["source_output_refs"],
            "frontend_unproven_write",
            "Frontend node append must use its own registered item and actual frozen invocation")
    expected_read = {
        "kind": "object_read", "object_key": context["object_key"],
        "revision": current["revision"], "revision_id": current["revision_id"],
    }
    require(type(source.get("reads")) is list and expected_read in source["reads"], "frontend_unproven_write",
            "Frontend node append requires its actual authorized object read")
    for binding in proof["source_output_refs"]:
        content = context["resolve_artifact"]({"scope": "artifact", "output_id": binding["output_id"]})["value"]
        require(type(content) is dict and set(content) == {"schema_version", "kind", "text"}
                and type(content["schema_version"]) is int and content["schema_version"] == 2
                and content["kind"] == "workflow.text" and type(content["text"]) is str,
                "frontend_source_type_mismatch", "Frontend node sources must be accepted TEXT@2 artifacts")
    prepared = prepare_frontend_node_adoption(
        workflow_session_id=context["workflow_session_id"], object_key=context["object_key"],
        object_record=current, proof=proof, proof_ref=value["view_ref"])
    require(prepared["desired_value"] == value, "frontend_state_mismatch",
            "Frontend state must equal its proven frontend node output")
