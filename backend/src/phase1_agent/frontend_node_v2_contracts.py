"""Accepted append evidence for independently stored logical chat values."""

from copy import deepcopy

from .chat_contracts import (
    CHAT_CONTEXT_SCHEMA_VERSION, CHAT_CONTEXT_TYPE, append_chat_candidate,
    validate_chat_context, validate_chat_context_write,
)
from .content_contracts import stable_item_id, text_content
from .contract_json import content_digest, validate_json_value
from .frontend_contracts import preserve_frontend_references
from .frontend_node_contracts import FRONTEND_NODE_APPEND_TYPE, FRONTEND_NODE_CHAT_COMPONENT
from .graph_contracts import require, uuid4_string
from .host_sdk import DataTypeDefinition, WriteIntent, bounded_name, validate_reference


def _fields(value, fields):
    require(type(value) is dict and set(value) == set(fields),
            "frontend_node_invalid_proof", "Frontend chat proof fields differ")


def validate_chat_append_context(value):
    _fields(value, ("round_id", "base_checkpoint_id", "submitted_input", "candidate_id"))
    uuid4_string(value["round_id"])
    uuid4_string(value["candidate_id"])
    require(value["base_checkpoint_id"] is None or type(value["base_checkpoint_id"]) is str,
            "frontend_node_invalid_proof", "Chat append checkpoint identity is invalid")
    if value["base_checkpoint_id"] is not None:
        uuid4_string(value["base_checkpoint_id"])
    require(value["submitted_input"] is None or type(value["submitted_input"]) is str,
            "frontend_node_invalid_proof", "Chat append requires the submitted input or no input")
    return deepcopy(value)


def validate_frontend_node_chat_append(value):
    validate_json_value(value)
    _fields(value, (
        "schema_version", "kind", "owner", "basis", "round",
        "source_output_refs", "role", "node_run_id", "messages",
    ))
    require(type(value["schema_version"]) is int and value["schema_version"] == 2
            and value["kind"] == "workflow.frontend-node-append",
            "frontend_node_invalid_proof", "Frontend chat append envelope is invalid")
    _fields(value["owner"], ("workflow_session_id", "object_key"))
    uuid4_string(value["owner"]["workflow_session_id"])
    require(bounded_name(value["owner"]["object_key"]), "frontend_node_invalid_proof",
            "Frontend chat proof requires an exact object owner")
    _fields(value["basis"], ("revision_id", "head_revision"))
    uuid4_string(value["basis"]["revision_id"])
    require(type(value["basis"]["head_revision"]) is int
            and 1 <= value["basis"]["head_revision"] <= 2**53 - 1,
            "frontend_node_invalid_proof", "Frontend chat proof requires an exact CAS basis")
    validate_chat_append_context(value["round"])
    uuid4_string(value["node_run_id"])
    require(value["role"] in ("user", "assistant", "system"),
            "frontend_node_invalid_proof", "Frontend chat append requires an explicit role")
    refs, messages = value["source_output_refs"], value["messages"]
    require(type(refs) is list and bool(refs)
            and type(messages) is list and len(messages) == len(refs),
            "frontend_node_invalid_proof", "Frontend chat append requires exact messages and sources")
    identities = set()
    for binding, message in zip(refs, messages):
        _fields(binding, ("edge_id", "output_id", "order"))
        uuid4_string(binding["edge_id"])
        uuid4_string(binding["output_id"])
        require(type(binding["order"]) is int and 0 <= binding["order"] <= 2**53 - 1,
                "frontend_node_invalid_proof", "Frontend input order must be a bounded integer")
        require(binding["output_id"] not in identities, "frontend_duplicate_source",
                "One frontend node invocation cannot append a source twice")
        identities.add(binding["output_id"])
        _fields(message, ("message_id", "role", "text"))
        require(message["message_id"] == stable_item_id(value["node_run_id"], binding["output_id"])
                and message["role"] == value["role"] and type(message["text"]) is str,
                "frontend_node_invalid_proof", "Frontend chat messages retain their accepted identities")
    return deepcopy(value)


def frontend_node_chat_append_data_type():
    return DataTypeDefinition(
        FRONTEND_NODE_APPEND_TYPE, 2, {"type": "object"}, scope="content",
        validator=validate_frontend_node_chat_append,
        references=lambda value: [
            {"scope": "artifact", "output_id": binding["output_id"]}
            for binding in value["source_output_refs"]],
        reference_mapper=preserve_frontend_references,
        artifact_bindings=lambda value: deepcopy(value["source_output_refs"]),
        max_bytes=4_000_000,
    )


def build_frontend_node_chat_append(
        record, *, workflow_session_id, object_key, source_output_refs,
        contents, role, node_run_id, chat_append_context):
    require((record["type_id"], record["schema_version"]) == (
        CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION) and not record["deleted"],
        "frontend_node_registration_mismatch", "Frontend chat append requires its live logical chat item")
    validate_chat_context(record["value"])
    metadata = validate_chat_append_context(chat_append_context)
    require(type(contents) is list and len(contents) == len(source_output_refs),
            "frontend_exact_artifact_required", "Frontend messages require exact accepted source values")
    messages = []
    for binding, content in zip(source_output_refs, contents):
        require(type(content) is dict and set(content) == {"schema_version", "kind", "text"}
                and type(content["schema_version"]) is int and content["schema_version"] == 2
                and content["kind"] == "workflow.text" and type(content["text"]) is str,
                "frontend_source_type_mismatch", "Frontend chat sources must be TEXT@2")
        messages.append({
            "message_id": stable_item_id(node_run_id, binding["output_id"]),
            "role": role, "text": content["text"],
        })
    return validate_frontend_node_chat_append({
        "schema_version": 2, "kind": "workflow.frontend-node-append",
        "owner": {"workflow_session_id": workflow_session_id, "object_key": object_key},
        "basis": {"revision_id": record["revision_id"], "head_revision": record["revision"]},
        "round": metadata, "source_output_refs": deepcopy(source_output_refs),
        "role": role, "node_run_id": node_run_id, "messages": messages,
    })


def prepare_frontend_node_chat_adoption(
        *, workflow_session_id, object_key, object_record, proof, proof_ref):
    proof = validate_frontend_node_chat_append(proof)
    proof_ref = validate_reference(proof_ref)
    require(proof_ref["scope"] == "artifact", "frontend_exact_artifact_required",
            "Frontend chat adoption requires its exact append artifact")
    require(proof["owner"] == {"workflow_session_id": workflow_session_id, "object_key": object_key},
            "frontend_owner_mismatch", "Frontend chat append belongs to another object")
    require(proof["basis"] == {
        "revision_id": object_record["revision_id"], "head_revision": object_record["revision"]},
        "frontend_stale_basis", "Frontend chat append must retain its exact object revision")
    expected = build_frontend_node_chat_append(
        object_record, workflow_session_id=workflow_session_id, object_key=object_key,
        source_output_refs=proof["source_output_refs"],
        contents=[text_content(message["text"]) for message in proof["messages"]],
        role=proof["role"], node_run_id=proof["node_run_id"], chat_append_context=proof["round"])
    require(expected == proof, "frontend_state_mismatch",
            "Frontend chat append cannot manufacture source identities")
    desired = append_chat_candidate(
        object_record["value"], **proof["round"], messages=proof["messages"])
    operation_key = "frontend-chat-append:" + content_digest({
        "owner": proof["owner"], "basis": proof["basis"], "proof_ref": proof_ref})
    return {
        "desired_value": desired, "operation_key": operation_key,
        "write_intent": WriteIntent(object_key, object_record["revision"], operation_key, desired).to_dict(),
        "write_evidence": {"proof_ref": proof_ref},
    }


def validate_frontend_node_chat_write(value, context):
    if context["operation"] == "initialize":
        return validate_chat_context_write(value, context)
    require(context["operation"] == "put", "frontend_unproven_write",
            "Frontend chat updates require an accepted append")
    evidence = context.get("write_evidence")
    require(type(evidence) is dict and set(evidence) == {"proof_ref"},
            "frontend_unproven_write", "Frontend chat append requires transient accepted evidence")
    reference = validate_reference(evidence["proof_ref"])
    require(reference["scope"] == "artifact", "frontend_exact_artifact_required",
            "Frontend chat evidence requires an accepted append artifact")
    source = context["resolve_artifact"](reference)
    proof = validate_frontend_node_chat_append(source["value"])
    producer, current = source["producer"], context["current_record"]
    config, binding = source["consumed_config"], current["binding"]
    require((source["component_id"], source["component_version"]) == FRONTEND_NODE_CHAT_COMPONENT
            and config["object_key"] == context["object_key"] and config["role"] == proof["role"]
            and producer["workflow_session_id"] == context["workflow_session_id"]
            and producer["node_run_id"] == proof["node_run_id"]
            and binding.get("registrar_node_id") == producer["node_binding_id"]
            and context.get("writer_node_id") == producer["node_binding_id"]
            and source["input_refs"].get("content") == proof["source_output_refs"]
            and source.get("chat_append_context") == proof["round"],
            "frontend_unproven_write",
            "Frontend chat append requires its own item and actual frozen invocation")
    expected_read = {
        "kind": "object_read", "object_key": context["object_key"],
        "revision": current["revision"], "revision_id": current["revision_id"],
    }
    require(type(source.get("reads")) is list and expected_read in source["reads"],
            "frontend_unproven_write", "Frontend chat append requires its actual authorized read")
    contents = [
        context["resolve_artifact"]({"scope": "artifact", "output_id": item["output_id"]})["value"]
        for item in proof["source_output_refs"]]
    expected = build_frontend_node_chat_append(
        current, workflow_session_id=context["workflow_session_id"], object_key=context["object_key"],
        source_output_refs=proof["source_output_refs"], contents=contents, role=config["role"],
        node_run_id=producer["node_run_id"], chat_append_context=source["chat_append_context"])
    require(proof == expected, "frontend_artifact_mismatch",
            "Frontend chat messages must equal their actual accepted source values")
    prepared = prepare_frontend_node_chat_adoption(
        workflow_session_id=context["workflow_session_id"], object_key=context["object_key"],
        object_record=current, proof=proof, proof_ref=reference)
    require(value == prepared["desired_value"], "frontend_state_mismatch",
            "Frontend chat state must equal its proven append")
