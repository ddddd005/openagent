"""Explicit read, window, historical assembly, advance and CAS save nodes."""

from copy import deepcopy

from .capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from .content_contracts import object_schema, text_content
from .contract_json import canonical_bytes, content_digest
from .context_contract import (
    EFFECTIVE_CONTEXT_TYPE, MAX_ACCEPTED_DELTAS, advance_context, agent_delta_references,
    artifact_ref, context_unit_references, context_view_references, effective_context_references,
    preserve_artifact_references, read_context_view, validate_agent_delta, validate_context_unit,
    validate_context_artifacts, validate_context_view, validate_effective_context, window_context,
)
from .context_prompt import (
    assemble_context_prompt, context_prompt_references, validate_context_ready_prompt,
)
from .graph_contracts import NodeDefinition, NodePort, require
from .host_sdk import DataTypeDefinition, WriteIntent


CONTEXT_PACKAGE_ID = "workflow.context"


def _exact_input(context, port):
    refs = context.input_artifact_refs(port)
    require(len(refs) == 1, "context_exact_artifact_required",
            "Context nodes require exactly one accepted artifact per input")
    return artifact_ref({"scope": "artifact", "output_id": refs[0]["output_id"]})


def _validate_host(context, operation, view, *, delta=None):
    payload = {"operation": operation, "view": deepcopy(view),
               "view_ref": _exact_input(context, "view")}
    if delta is not None:
        payload.update({"delta": deepcopy(delta), "delta_ref": _exact_input(context, "delta")})
    context.host_call("context:validate", "validate-context", payload)
    return payload


def _read(config, inputs, context):
    record = context.object_read(config["object_key"])
    retained = context.host_call("context:read", "read-context", {"object_key": config["object_key"]})
    require(retained["object"] == record, "context_invalid_read",
            "Historical resolution must use the exact authorized frozen object")
    return {"output": read_context_view(record, context.workflow_session_id, config["object_key"],
                                        retained["view"])}


def _window(config, inputs, context):
    _validate_host(context, "window", inputs["view"])
    return {"output": window_context(inputs["view"], _exact_input(context, "view"),
                                     last_units=config["last_units"])}


def _assembly(config, inputs, context):
    _validate_host(context, "assembly", inputs["view"])
    return {"output": assemble_context_prompt(
        inputs.get("materials", []), inputs["view"], inputs["current_input"],
        context_ref=_exact_input(context, "view"), current_input_ref=_exact_input(context, "current_input"),
        source_output_refs=context.input_artifact_refs("materials"))}


def _project_text(config, inputs, context):
    view = validate_context_view(inputs["view"])
    _validate_host(context, "project", view)
    lines = []
    for entry in view["units"]:
        for message in [entry["unit"]["root"], *entry["unit"]["messages"]]:
            lines.append(message["role"] + ": " + message["content"])
            if "tool_calls" in message:
                lines.append(canonical_bytes({"tool_calls": message["tool_calls"]}).decode("utf-8"))
            if "tool_call_id" in message:
                lines.append("tool_call_id: " + message["tool_call_id"])
    return {"output": text_content("\n".join(lines))}


def _advance(config, inputs, context):
    payload = _validate_host(context, "advance", inputs["view"], delta=inputs["delta"])
    return {"output": advance_context(inputs["view"], payload["view_ref"],
                                      inputs["delta"], payload["delta_ref"])}


def _save(config, inputs, context):
    view = validate_context_view(inputs["view"])
    key = config["object_key"]
    record = context.object_read(key)
    view_ref = _exact_input(context, "view")
    _validate_host(context, "save", view)
    prepared = prepare_context_adoption(
        workflow_session_id=context.workflow_session_id, object_key=key,
        object_record=record, view_ref=view_ref, view=view)
    desired_ids = prepared["desired_value"]["accepted_delta_ids"]
    # A settled retry reads the committed pointer; no new write or model call.
    if prepared["already_committed"]:
        return {"output": _commit(context, key, view_ref, record, desired_ids, committed=True)}
    intent = prepared["write_intent"]
    context.object_write(key, intent["value"], expected_revision=intent["expected_revision"],
                         operation_key=intent["operation_key"])
    return {"output": _commit(context, key, view_ref, record, desired_ids, committed=False,
                              operation_key=intent["operation_key"])}


def prepare_context_adoption(*, workflow_session_id, object_key, object_record, view_ref, view):
    """Prepare one business write after the platform proves artifact authority.

    This helper does not resolve references, authorize a writer, or commit.
    The caller must supply an authorized frozen/current object record and a
    proven exact immutable view; public object storage settles the intent.
    """
    view = validate_context_view(view)
    view_ref = artifact_ref(view_ref)
    record = object_record
    require(view["owner"] == {"workflow_session_id": workflow_session_id, "object_key": object_key},
            "context_save_owner_mismatch", "View belongs to another context object")
    require((record["type_id"], record["schema_version"]) == (EFFECTIVE_CONTEXT_TYPE, 1)
            and record["binding"]["object_key"] == object_key and not record["deleted"],
            "context_object_type_mismatch", "Save requires its registered effective context object")
    value = validate_effective_context(record["value"])
    desired_ids = view["accepted_delta_ids"] + view["applied_delta_ids"]
    desired = {"view_ref": view_ref, "accepted_delta_ids": desired_ids}
    if value == desired:
        return {"desired_value": desired, "write_intent": None, "already_committed": True}
    require(view["basis"] == {"revision_id": record["revision_id"], "head_revision": record["revision"]},
            "context_stale_basis", "Both object revision identity and CAS fence must match")
    require(value["accepted_delta_ids"] == view["accepted_delta_ids"],
            "context_consumption_mismatch", "View lost or changed its persistent consumption boundary")
    require(not (set(value["accepted_delta_ids"]) & set(view["applied_delta_ids"])),
            "context_delta_already_consumed", "Committed deltas cannot be applied again")
    require(len(desired_ids) <= MAX_ACCEPTED_DELTAS, "context_delta_capacity_exceeded",
            "Consumed delta identities cannot be silently discarded")
    operation_key = "context-save:" + content_digest({
        "owner": view["owner"], "basis": view["basis"], "view_ref": view_ref,
        "adopted_delta_ids": desired_ids,
    })
    return {"desired_value": desired, "already_committed": False,
            "write_intent": WriteIntent(object_key, record["revision"], operation_key, desired).to_dict()}


def validate_context_adoption_proof(view_ref, view, resolve, producer_component_id, producer_config):
    """Prove the business operation of an already accepted official view.

    The platform supplies the exact frozen producer declaration/configuration
    and restricts ``resolve`` to its authorized immutable reference closure.
    """
    require(producer_component_id in ("context.read", "context.window", "context.advance"),
            "context_adoption_source_invalid", "Only official context view producers can be adopted")
    checked = validate_context_artifacts({"view_ref": view_ref, "view": view}, resolve)
    derivation = checked["derivation"]
    operation = producer_component_id.removeprefix("context.")
    require(derivation["operation"] == operation, "context_adoption_source_invalid",
            "View derivation differs from its frozen producer")
    if operation == "read":
        require(producer_config.get("object_key") == checked["owner"]["object_key"],
                "context_adoption_source_invalid", "Read projection belongs to another bound object")
    elif operation == "window":
        original = resolve(derivation["input_view_ref"])
        expected = window_context(original, derivation["input_view_ref"],
                                  last_units=producer_config["last_units"])
        require(expected == checked, "context_adoption_source_invalid",
                "Window differs from its exact frozen configuration")
    else:
        original = resolve(derivation["input_view_ref"])
        delta = resolve(derivation["delta_ref"])
        expected = validate_context_artifacts({
            "view": original, "view_ref": derivation["input_view_ref"],
            "delta": delta, "delta_ref": derivation["delta_ref"],
        }, resolve)
        require(expected == checked, "context_adoption_source_invalid",
                "Advance differs from its exact frozen execution proof")
    return deepcopy(checked)


def validate_effective_context_write(value, write_context):
    """Apply package business invariants to every generic public object write."""
    operation = write_context["operation"]
    require(operation in ("put", "delete", "initialize"), "context_invalid_write",
            "Effective context write operation is unsupported")
    if operation == "delete":
        require(value is None, "context_invalid_write", "Context deletion requires a tombstone")
        return
    value = validate_effective_context(value)
    record = write_context["current_record"]
    if operation == "initialize":
        require(record is None and value == {"view_ref": None, "accepted_delta_ids": []},
                "context_initialization_invalid", "New context objects start with an empty pointer")
        return
    require(record is not None, "context_invalid_write", "Context update requires an authorized current head")
    if not record["deleted"] and value == record["value"]:
        return
    require(value["view_ref"] is not None, "context_invalid_write",
            "Context edits cannot silently discard the persistent consumption boundary")
    resolver = write_context["resolve_artifact"]
    source = resolver(value["view_ref"])
    require(source["component_version"] == "1", "context_adoption_source_invalid",
            "Context view producer implementation version is unsupported")

    def resolve(reference):
        return resolver(reference)["value"]

    view = validate_context_adoption_proof(
        value["view_ref"], source["value"], resolve, source["component_id"], source["config"])
    prepared = prepare_context_adoption(
        workflow_session_id=write_context["workflow_session_id"], object_key=write_context["object_key"],
        object_record=record, view_ref=value["view_ref"], view=view)
    require(value == prepared["desired_value"], "context_consumption_mismatch",
            "Object pointer and consumed delta identities must match the proven view")


def _commit(context, key, view_ref, record, ids, *, committed, operation_key=None):
    # Actual new revision IDs live in the public object write receipt, never guessed.
    return {"schema_version": 1, "kind": "workflow.context-commit",
            "owner": {"workflow_session_id": context.workflow_session_id, "object_key": key},
            "view_ref": deepcopy(view_ref), "basis": {
                "revision_id": record["revision_id"], "head_revision": record["revision"]},
            "adopted_delta_ids": deepcopy(ids), "operation_key": operation_key,
            "status": "already_committed" if committed else "write_intent"}


def validate_context_commit(value):
    from .context_contract import _envelope, _ids
    _envelope(value, "workflow.context-commit",
              ("owner", "view_ref", "basis", "adopted_delta_ids", "operation_key", "status"))
    artifact_ref(value["view_ref"])
    _ids(value["adopted_delta_ids"])
    # Reuse the exact owner and basis checks with an empty legitimate read view.
    validate_context_view({"schema_version": 1, "kind": "workflow.context-view",
                           "owner": value["owner"], "basis": value["basis"], "units": [],
                           "accepted_delta_ids": [], "applied_delta_ids": [],
                           "derivation": {"operation": "read", "input_view_ref": None, "delta_ref": None}})
    require(value["status"] in ("write_intent", "already_committed")
            and (type(value["operation_key"]) is str and value["operation_key"].startswith("context-save:")
                 if value["status"] == "write_intent" else value["operation_key"] is None),
            "context_invalid_commit", "Commit describes a public write intent or settled replay")
    return deepcopy(value)


def create_context_package():
    def register(host):
        from .context_host_service import (
            CONTEXT_SERVICE_DEFINITION, create_context_host_service, service_requirement,
            SUMMARY_CONTEXT_SERVICE_DEFINITION, create_summary_context_host_service,
            NATIVE_CONTEXT_SERVICE_DEFINITION, create_native_context_host_service,
        )
        host.register_service(CONTEXT_SERVICE_DEFINITION, create_context_host_service)
        host.register_service(SUMMARY_CONTEXT_SERVICE_DEFINITION, create_summary_context_host_service)
        host.register_service(NATIVE_CONTEXT_SERVICE_DEFINITION, create_native_context_host_service)
        host.register_data_type(DataTypeDefinition(
            EFFECTIVE_CONTEXT_TYPE, 1, object_schema({
                "view_ref": {"type": ["object", "null"]}, "accepted_delta_ids": {"type": "array"},
            }), {"view_ref": None, "accepted_delta_ids": []},
            validator=validate_effective_context, references=effective_context_references,
            reference_mapper=preserve_artifact_references,
            write_validator=validate_effective_context_write))
        for identity, version, validator, references, bindings in (
            ("CONTEXT_UNIT", 1, validate_context_unit, context_unit_references, None),
            ("CONTEXT_VIEW", 1, validate_context_view, context_view_references, None),
            ("AGENT_DELTA", 1, validate_agent_delta, agent_delta_references, None),
            ("CONTEXT_COMMIT", 1, validate_context_commit, lambda value: [value["view_ref"]], None),
            ("PROMPT", 3, validate_context_ready_prompt, context_prompt_references,
             lambda value: value["source_output_refs"]),
        ):
            host.register_data_type(DataTypeDefinition(
                identity, version, {"type": "object"}, scope="content", validator=validator,
                references=references, reference_mapper=preserve_artifact_references,
                artifact_bindings=bindings, max_bytes=4_000_000))

        def node(name, config, config_schema, execute, *, inputs=(), output="CONTEXT_VIEW",
                 version=1, capabilities=("context:validate",), access=None):
            host.register_node(NodeDefinition(
                "context." + name, "1", "Context " + name, "Context", config, config_schema,
                inputs=inputs, outputs=(NodePort("output", output, data_schema_version=version),),
                capabilities=capabilities, input_storage="references",
                object_accesses=() if access is None else ({
                    "config_field": "object_key", "type_id": EFFECTIVE_CONTEXT_TYPE,
                    "schema_version": 1, "access": access, "multiple": False},),
                service_requirements=tuple(
                    service_requirement(capability, "read-context" if capability == "context:read" else "validate-context")
                    for capability in capabilities if capability in ("context:read", "context:validate")),
            ), execute)

        object_config = {"object_key": "context"}
        object_schema_config = object_schema({"object_key": {"type": "string", "minLength": 1, "maxLength": 128}})
        view = NodePort("view", "CONTEXT_VIEW")
        node("read", object_config, object_schema_config, _read,
             capabilities=("objects:read", "context:read"), access="read")
        node("window", {"last_units": 16}, object_schema({
            "last_units": {"type": "integer", "minimum": 0, "maximum": 4096}}), _window, inputs=(view,))
        node("project-text", {}, object_schema({}), _project_text, inputs=(view,), output="TEXT", version=2)
        node("assembly", {}, object_schema({}), _assembly, output="PROMPT", version=3,
             inputs=(view, NodePort("materials", "PROMPT", data_schema_version=2, multiple=True, required=False),
                     NodePort("current_input", "TEXT", data_schema_version=2)))
        node("advance", {}, object_schema({}), _advance,
             inputs=(view, NodePort("delta", "AGENT_DELTA")))
        node("save", object_config, object_schema_config, _save, inputs=(view,),
             output="CONTEXT_COMMIT", capabilities=("objects:read", "objects:write", "context:validate"),
             access="read_write")
        from .context_v2_nodes import register_bound_context
        register_bound_context(host)
        from .context_v3_nodes import register_summary_context
        register_summary_context(host)
        from .context_v4_nodes import register_native_context
        register_native_context(host)

    from .context_host_service import CONTEXT_SERVICE_REF, SUMMARY_CONTEXT_SERVICE_REF, NATIVE_CONTEXT_SERVICE_REF
    return CapabilityPackage(PackageManifest(
        CONTEXT_PACKAGE_ID, "1.0.0", (PackageDependency("workflow.content", "1.0.0"),
                                    PackageDependency("workflow.prompts", "1.0.0")),
        exports={"services": [CONTEXT_SERVICE_REF.to_dict(), SUMMARY_CONTEXT_SERVICE_REF.to_dict(),
                              NATIVE_CONTEXT_SERVICE_REF.to_dict()]},
        schema_version=3), register)
