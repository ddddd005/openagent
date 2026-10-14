"""Uninstalled native lineage proof within an existing formal history authority."""

from copy import deepcopy

from .context_contract import EFFECTIVE_CONTEXT_TYPE, artifact_ref, validate_effective_context
from .context_host_service import NATIVE_CONTEXT_SERVICE_REF
from .context_prompt_v6 import validate_native_context_prompt_legacy_v4
from .context_update_v2 import validate_context_update_v2
from .context_v4 import (
    merge_native_context, prepare_native_context_adoption, read_native_context,
    validate_native_context_view,
)
from .graph_records import require
from .historical_artifact_reader import HistoryArtifactAuthority, read_context_update_facts


def _authority(authority):
    require(isinstance(authority, HistoryArtifactAuthority)
            and authority.connection.in_transaction, "history_snapshot_required",
            "Native history proof requires its genuine transaction authority", 409)
    authority._transaction()
    require(authority._checkpoint_id is not None or authority._object_roots,
            "history_root_required", "Native history proof requires a formal owned root", 403)


def _input_matches(detail, port, reference):
    refs = detail["input_refs"].get(port, [])
    require(len(refs) == 1 and refs[0]["output_id"] == reference["output_id"],
            "history_context_input_mismatch", "Native lineage differs from its exact accepted input", 403)


def _service_grant(descriptor, capability, operation):
    service = NATIVE_CONTEXT_SERVICE_REF.to_dict()
    grants = [grant for grant in descriptor.get("service_requirements", [])
              if type(grant) is dict and grant.get("service_id") == service["service_id"]
              and grant.get("exact_version") == service["exact_version"]
              and grant.get("capability") == capability]
    require(capability in descriptor.get("capabilities", []) and len(grants) == 1
            and set(grants[0]) == {"service_id", "exact_version", "capability", "operations"}
            and type(grants[0]["operations"]) is list and operation in grants[0]["operations"],
            "history_context_source_denied", "Native producer lacks its exact frozen host grant", 403)


def _source(authority, reference, detail, view):
    operation = view["derivation"]["operation"]
    expected = {"read": ("context.output", "4"), "merge": ("context.merge", "4")}
    require(operation in expected
            and (detail["component_id"], detail["component_version"]) == expected[operation]
            and (detail["data_type"], detail["data_schema_version"]) == ("CONTEXT_VIEW", 4),
            "history_context_source_denied", "Native history requires its exact official accepted producer", 403)
    config, owner = detail["config"], view["owner"]
    require(set(config) == {"object_key", "agent_node_id"}
            and config["object_key"] == owner["object_key"]
            and config["agent_node_id"] == owner["agent_node_id"]
            and detail["producer"]["workflow_session_id"] == owner["workflow_session_id"],
            "history_context_owner_mismatch", "Native owner differs from the original consumed configuration", 403)
    output = authority._record("workflow_output", output_id=reference["output_id"])
    require(output["port_id"] == "output", "history_context_source_denied",
            "Native history requires its official immutable output port", 403)
    frozen = authority._details[detail["output_id"]]
    agents = [node for node in frozen["_document"]["nodes"]
              if node["node_binding_id"] == config["agent_node_id"]]
    require(len(agents) == 1
            and (agents[0]["component_id"], agents[0]["component_version"]) in authority._context_sources,
            "history_context_owner_mismatch", "Native owner has no exact frozen Agent binding", 403)
    descriptor = frozen["_descriptor"]
    accesses = [item for item in descriptor.get("object_accesses", [])
                if type(item) is dict and item.get("config_field") == "object_key"]
    require(accesses == [{"config_field": "object_key", "type_id": EFFECTIVE_CONTEXT_TYPE,
                          "schema_version": 4, "access": "read" if operation == "read" else "read_write",
                          "multiple": False}], "history_context_source_denied",
            "Native producer lacks its exact versioned object access", 403)
    if operation == "read":
        _service_grant(descriptor, "context:read", "read-context")
    else:
        require({"objects:read", "objects:write", "context:validate", "artifacts:read", "facts:read"}
                <= set(descriptor.get("capabilities", [])), "history_context_source_denied",
                "Native merge lacks its exact frozen adoption permissions", 403)
        _service_grant(descriptor, "context:validate", "validate-context")


def _basis(authority, reference, view, *, write=False):
    return authority._read_original_object_basis(
        reference, object_key=view["owner"]["object_key"],
        revision_id=view["basis"]["revision_id"], head_revision=view["basis"]["head_revision"],
        write=write)


def _merge(authority, reference, detail, view, original):
    lineage = view["derivation"]
    _input_matches(detail, "view", lineage["input_view_ref"])
    _input_matches(detail, "context", lineage["update_ref"])
    require(original["owner"] == view["owner"] and original["basis"] == view["basis"],
            "history_context_owner_mismatch", "Native merge cannot change its exact input owner or basis", 403)
    origin = authority.resolve_detail(lineage["update_ref"])
    require((origin["component_id"], origin["component_version"]) in authority._context_sources
            and (origin["data_type"], origin["data_schema_version"]) == ("AGENT_CONTEXT_UPDATE", 2),
            "history_context_update_unproven", "Historical native merge requires complete schema-2 update proof", 403)
    update = validate_context_update_v2(origin["value"])
    require(origin["producer"] == update["owner"]
            and update["owner"]["workflow_session_id"] == view["owner"]["workflow_session_id"]
            and update["owner"]["node_binding_id"] == view["owner"]["agent_node_id"]
            and update["owner"]["chain_run_id"] == detail["producer"]["chain_run_id"],
            "history_context_owner_mismatch", "Native merge differs from its actual bound Agent invocation", 403)
    _input_matches(origin, "prompt", update["receipts"]["frozen_prompt_ref"])
    _input_matches(origin, "model", update["receipts"]["model_ref"])
    prompt_source = authority.resolve_detail(update["receipts"]["frozen_prompt_ref"])
    require((prompt_source["data_type"], prompt_source["data_schema_version"]) == ("PROMPT", 6),
            "history_context_update_unproven", "Historical native updates require the original PROMPT@6", 403)
    prompt = validate_native_context_prompt_legacy_v4(prompt_source["value"])
    require(prompt["context_ref"] == lineage["input_view_ref"] and prompt["context"] == original,
            "history_context_input_mismatch", "Native merge must retain the exact view frozen in its prompt", 403)
    record = _basis(authority, reference, view, write=True)
    # Candidate replay is not proof: every merge verifies its original executor/M2 facts.
    read_context_update_facts(authority.connection, authority, lineage["update_ref"])
    expected = merge_native_context(original, lineage["input_view_ref"], update, lineage["update_ref"], prompt)
    require(expected == view, "history_context_derivation_mismatch",
            "Native merge does not reproduce its fully proven original update", 403)
    prepared = prepare_native_context_adoption(
        workflow_session_id=view["owner"]["workflow_session_id"], object_key=view["owner"]["object_key"],
        object_record=record, view_ref=reference, view=view)
    require(not prepared["already_committed"], "history_context_derivation_mismatch",
            "An original accepted merge must advance its actual read consumption boundary", 403)


def prove_historical_native_context(authority, view_ref):
    """Return only a detached view after proving every original read and merge."""
    _authority(authority)
    reference, lineage, visited = artifact_ref(view_ref), [], set()
    while reference is not None:
        require(reference["output_id"] not in visited, "history_context_lineage_cycle",
                "Historical native lineage cannot cycle", 403)
        visited.add(reference["output_id"])
        detail = authority.resolve_detail(reference)
        view = validate_native_context_view(detail["value"])
        _source(authority, reference, detail, view)
        lineage.append((reference, detail, view))
        reference = view["derivation"]["input_view_ref"]
    original = None
    for reference, detail, view in reversed(lineage):
        if view["derivation"]["operation"] == "read":
            expected = read_native_context(
                _basis(authority, reference, view), view["owner"]["workflow_session_id"],
                view["owner"]["object_key"], view["owner"]["agent_node_id"], original)
            require(expected == view, "history_context_derivation_mismatch",
                    "Native read does not reproduce its actual immutable object basis", 403)
        else:
            require(original is not None, "history_context_derivation_mismatch",
                    "A native merge requires a completely proven original view", 403)
            _merge(authority, reference, detail, view, original)
        original = view
    return deepcopy(original)


def validate_historical_context_object(authority, value, *, object_key=None):
    """Prove an adopted pointer, without rewriting its source owner or CAS basis."""
    _authority(authority)
    value = validate_effective_context(value)
    if value["view_ref"] is None:
        authority._verify_roots()
        return value
    view = prove_historical_native_context(authority, value["view_ref"])
    require(view["derivation"]["operation"] == "merge"
            and (object_key is None or view["owner"]["object_key"] == object_key),
            "history_context_owner_mismatch", "An adopted context requires its exact original merge object", 403)
    require(value["accepted_delta_ids"] == view["accepted_delta_ids"] + view["applied_delta_ids"],
            "context_consumption_mismatch", "Historical context must retain its complete exact consumed boundary")
    return deepcopy(value)
