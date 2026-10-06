"""Immutable effective context contracts, independent of any Agent kernel."""

from __future__ import annotations

from copy import deepcopy

from .contract_json import canonical_bytes, validate_json_value
from .graph_contracts import require, uuid4_string
from .host_sdk import validate_reference


EFFECTIVE_CONTEXT_TYPE = "workflow.effective-context"
MAX_ACCEPTED_DELTAS = 4096


def _fields(value, fields, *, optional=()):
    require(type(value) is dict and set(fields) <= set(value) <= set(fields) | set(optional),
            "context_invalid_contract", "Context contract fields are invalid")


def _envelope(value, kind, fields):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", *fields))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == kind, "context_invalid_contract", "Context envelope is invalid")


def artifact_ref(value):
    checked = validate_reference(value)
    require(checked["scope"] == "artifact", "context_exact_artifact_required",
            "Context provenance requires an exact immutable artifact")
    return checked


def _ids(values):
    require(type(values) is list and len(values) <= MAX_ACCEPTED_DELTAS,
            "context_delta_capacity_exceeded", "Consumed delta identities exceed the declared budget")
    for value in values:
        uuid4_string(value)
    require(len(values) == len(set(values)), "context_duplicate_delta",
            "Context cannot repeat a consumed delta identity")


def _refs(values):
    require(type(values) is list and len(values) <= 4096, "context_invalid_contract",
            "Context references must be bounded")
    for value in values:
        artifact_ref(value)
    require(len(values) == len({ref["output_id"] for ref in values}),
            "context_duplicate_reference", "Context references cannot repeat identities")


def validate_effective_context(value):
    validate_json_value(value)
    _fields(value, ("view_ref", "accepted_delta_ids"))
    if value["view_ref"] is not None:
        artifact_ref(value["view_ref"])
    _ids(value["accepted_delta_ids"])
    require(value["view_ref"] is not None or not value["accepted_delta_ids"],
            "context_consumption_mismatch", "An uninitialized context has no consumed deltas")
    return deepcopy(value)


def effective_context_references(value):
    return [] if value["view_ref"] is None else [deepcopy(value["view_ref"])]


def preserve_artifact_references(value, mapping):
    """Forking maps object ownership at read time; immutable producers stay original."""
    return deepcopy(value)


def validate_context_unit(value):
    _envelope(value, "workflow.context-unit",
              ("unit_id", "source_kind", "root", "messages", "source_refs"))
    uuid4_string(value["unit_id"])
    require(value["source_kind"] in ("accepted_execution", "manual"),
            "context_invalid_source", "Context unit requires an explicit source family")
    _fields(value["root"], ("role", "content"))
    require(value["root"]["role"] == "user" and type(value["root"]["content"]) is str,
            "context_invalid_root", "A context root is the actual frozen user input")
    _refs(value["source_refs"])
    require(bool(value["source_refs"]), "context_missing_provenance", "Units require source artifacts")
    messages = value["messages"]
    require(type(messages) is list and len(messages) <= 4096,
            "context_invalid_contract", "Unit messages must be bounded")
    if value["source_kind"] == "manual":
        require(not messages, "context_manual_protocol_forbidden",
                "Manual units cannot impersonate generated protocol messages")
        return deepcopy(value)
    pending, seen_calls, identities = [], set(), []
    for message in messages:
        _fields(message, ("message_id", "role", "content"),
                optional=("tool_calls", "tool_call_id", "status", "is_error", "outcome_reason", "result"))
        identities.append(uuid4_string(message["message_id"]))
        require(type(message["content"]) is str and message["role"] in ("assistant", "tool"),
                "context_invalid_message", "Generated units contain only assistant and tool messages")
        if message["role"] == "assistant":
            require(not pending and not ({"tool_call_id", "status", "is_error", "outcome_reason", "result"} & set(message)),
                    "context_protocol_unclosed",
                    "An assistant cannot interrupt a pending tool batch")
            calls = message.get("tool_calls", [])
            require(type(calls) is list and len(calls) <= 256
                    and ("tool_calls" not in message or bool(calls)),
                    "context_invalid_tool_call", "Tool calls require a nonempty bounded batch")
            for call in calls:
                _fields(call, ("id", "name", "arguments"))
                require(all(type(call[key]) is str and bool(call[key]) for key in ("id", "name"))
                        and type(call["arguments"]) is str and call["id"] not in seen_calls,
                        "context_invalid_tool_call", "Tool call identity or payload is invalid")
                pending.append(call["id"])
                seen_calls.add(call["id"])
        else:
            require({"tool_call_id", "status", "is_error", "outcome_reason", "result"} <= set(message)
                    and message["status"] in ("success", "error") and type(message["is_error"]) is bool
                    and message["is_error"] == (message["status"] == "error")
                    and message["outcome_reason"] in (None, "never_started", "interrupted", "outcome_unknown")
                    and (message["outcome_reason"] is None or message["is_error"]),
                    "context_invalid_tool_outcome",
                    "Historical tools preserve typed results and honest error or unknown outcomes")
            require("tool_calls" not in message and bool(pending)
                    and message.get("tool_call_id") == pending[0], "context_protocol_unclosed",
                    "Tool results must close their original batch in order")
            pending.pop(0)
    require(len(identities) == len(set(identities)), "context_duplicate_message",
            "Unit messages require unique exact identities")
    require(bool(messages) and not pending and messages[-1]["role"] == "assistant"
            and "tool_calls" not in messages[-1], "context_protocol_unclosed",
            "Only a complete execution unit enters effective history")
    return deepcopy(value)


def context_unit_references(value):
    return deepcopy(value["source_refs"])


def validate_context_view(value):
    _envelope(value, "workflow.context-view",
              ("owner", "basis", "units", "accepted_delta_ids", "applied_delta_ids", "derivation"))
    _fields(value["owner"], ("workflow_session_id", "object_key"))
    uuid4_string(value["owner"]["workflow_session_id"])
    require(type(value["owner"]["object_key"]) is str and 0 < len(value["owner"]["object_key"]) <= 128,
            "context_invalid_owner", "Context owner requires a bound object key")
    _fields(value["basis"], ("revision_id", "head_revision"))
    uuid4_string(value["basis"]["revision_id"])
    require(type(value["basis"]["head_revision"]) is int and 1 <= value["basis"]["head_revision"] <= 2**53 - 1,
            "context_invalid_basis", "Context basis requires the exact monotonic CAS fence")
    require(type(value["units"]) is list and len(value["units"]) <= 4096,
            "context_invalid_contract", "Context units must be bounded")
    unit_ids, output_ids = [], []
    for entry in value["units"]:
        _fields(entry, ("unit_ref", "unit"))
        output_ids.append(artifact_ref(entry["unit_ref"])["output_id"])
        unit_ids.append(validate_context_unit(entry["unit"])["unit_id"])
    require(len(unit_ids) == len(set(unit_ids)) and len(output_ids) == len(set(output_ids)),
            "context_duplicate_unit", "Context cannot repeat a unit or its immutable artifact")
    _ids(value["accepted_delta_ids"])
    _ids(value["applied_delta_ids"])
    require(not (set(value["accepted_delta_ids"]) & set(value["applied_delta_ids"])),
            "context_delta_already_consumed", "New deltas cannot already be committed")
    derivation = value["derivation"]
    _fields(derivation, ("operation", "input_view_ref", "delta_ref"))
    require(derivation["operation"] in ("read", "window", "advance"),
            "context_invalid_derivation", "Context operation version is unsupported")
    for key in ("input_view_ref", "delta_ref"):
        if derivation[key] is not None:
            artifact_ref(derivation[key])
    require((derivation["operation"] == "advance") == (derivation["delta_ref"] is not None)
            and (derivation["operation"] == "read" or derivation["input_view_ref"] is not None),
            "context_invalid_derivation", "View lineage differs from its operation")
    return deepcopy(value)


def context_view_references(value):
    refs = [entry["unit_ref"] for entry in value["units"]]
    refs += [ref for ref in value["derivation"].values() if type(ref) is dict]
    return _unique_refs(refs)


def _unique_refs(refs):
    return [deepcopy(ref) for _, ref in sorted({ref["output_id"]: ref for ref in refs}.items())]


def validate_agent_delta(value):
    _envelope(value, "workflow.agent-delta",
              ("delta_id", "owner", "frozen_prompt_ref", "basis_view_ref", "current_root_ref",
               "unit_ref", "unit", "fact_receipt_refs"))
    uuid4_string(value["delta_id"])
    _fields(value["owner"], ("workflow_session_id", "node_binding_id", "chain_run_id", "node_run_id"))
    for identity in value["owner"].values():
        uuid4_string(identity)
    for key in ("frozen_prompt_ref", "current_root_ref", "unit_ref"):
        artifact_ref(value[key])
    if value["basis_view_ref"] is not None:
        artifact_ref(value["basis_view_ref"])
    validate_context_unit(value["unit"])
    require(value["unit"]["source_kind"] == "accepted_execution", "context_invalid_source",
            "Execution delta cannot contain manual material")
    _refs(value["fact_receipt_refs"])
    require(bool(value["fact_receipt_refs"]), "context_missing_provenance",
            "Execution delta requires accepted fact receipts")
    return deepcopy(value)


def agent_delta_references(value):
    return _unique_refs([value[key] for key in
                         ("frozen_prompt_ref", "basis_view_ref", "current_root_ref", "unit_ref")
                         if value[key] is not None] + value["fact_receipt_refs"])


def read_context_view(object_record, session_id, object_key, retained=None):
    value = validate_effective_context(object_record["value"])
    require((object_record["type_id"], object_record["schema_version"]) == (EFFECTIVE_CONTEXT_TYPE, 1),
            "context_object_type_mismatch", "Context read requires its registered session object")
    if value["view_ref"] is None:
        require(retained is None, "context_invalid_read", "Empty objects cannot carry hidden history")
        units = []
    else:
        validate_context_view(retained)
        require(set(retained["accepted_delta_ids"] + retained["applied_delta_ids"])
                == set(value["accepted_delta_ids"]), "context_consumption_mismatch",
                "Retained view differs from the object's consumed delta boundary")
        units = retained["units"]
    return validate_context_view({
        "schema_version": 1, "kind": "workflow.context-view",
        "owner": {"workflow_session_id": session_id, "object_key": object_key},
        "basis": {"revision_id": object_record["revision_id"], "head_revision": object_record["revision"]},
        "units": deepcopy(units), "accepted_delta_ids": deepcopy(value["accepted_delta_ids"]),
        "applied_delta_ids": [],
        "derivation": {"operation": "read", "input_view_ref": deepcopy(value["view_ref"]), "delta_ref": None},
    })


def window_context(view, view_ref, *, last_units):
    checked = validate_context_view(view)
    require(type(last_units) is int and 0 <= last_units <= 4096,
            "context_invalid_window", "Context window counts complete units")
    checked["units"] = checked["units"][-last_units:] if last_units else []
    checked["derivation"] = {"operation": "window", "input_view_ref": artifact_ref(view_ref), "delta_ref": None}
    return validate_context_view(checked)


def advance_context(view, view_ref, delta, delta_ref):
    checked, delta = validate_context_view(view), validate_agent_delta(delta)
    view_ref, delta_ref = artifact_ref(view_ref), artifact_ref(delta_ref)
    require(delta["basis_view_ref"] == view_ref, "context_delta_basis_mismatch",
            "Delta must consume the exact selected view, including its window")
    require(delta["owner"]["workflow_session_id"] == checked["owner"]["workflow_session_id"],
            "context_delta_owner_mismatch", "Delta belongs to another session")
    require(delta["delta_id"] not in checked["accepted_delta_ids"] + checked["applied_delta_ids"],
            "context_delta_already_consumed", "Delta has already been consumed by this context")
    checked["units"].append({"unit_ref": delta["unit_ref"], "unit": delta["unit"]})
    checked["applied_delta_ids"].append(delta["delta_id"])
    checked["derivation"] = {"operation": "advance", "input_view_ref": view_ref, "delta_ref": delta_ref}
    return validate_context_view(checked)


def validate_context_artifacts(payload, resolve):
    """Check immutable business evidence after the host grants exact references.

    ``resolve(ref)`` returns the authorized immutable artifact *value*. It must
    never query arbitrary IDs on behalf of package supplied JSON.
    """
    view = validate_context_view(payload["view"])
    require(canonical_bytes(resolve(artifact_ref(payload["view_ref"]))) == canonical_bytes(view),
            "context_artifact_mismatch", "View differs from its exact accepted artifact")
    for entry in view["units"]:
        require(canonical_bytes(resolve(entry["unit_ref"])) == canonical_bytes(entry["unit"]),
                "context_artifact_mismatch", "Unit differs from its immutable source")
    _validate_view_lineage(view, resolve)
    if "delta" in payload:
        delta = validate_agent_delta(payload["delta"])
        require(canonical_bytes(resolve(artifact_ref(payload["delta_ref"]))) == canonical_bytes(delta),
                "context_artifact_mismatch", "Delta differs from its exact accepted execution artifact")
        require(canonical_bytes(resolve(delta["unit_ref"])) == canonical_bytes(delta["unit"]),
                "context_artifact_mismatch", "Delta unit differs from its accepted artifact")
        prompt = resolve(delta["frozen_prompt_ref"])
        from .context_prompt import validate_context_ready_prompt
        validate_context_ready_prompt(prompt)
        require(prompt["context_ref"] == delta["basis_view_ref"]
                and prompt["current_input_ref"] == delta["current_root_ref"]
                and prompt["current_input"] == delta["unit"]["root"],
                "context_delta_prompt_mismatch", "Delta does not match the actual frozen input")
        root = resolve(delta["current_root_ref"])
        require(type(root) is dict and root.get("schema_version") == 2
                and root.get("kind") == "workflow.text" and root.get("text") == delta["unit"]["root"]["content"],
                "context_delta_prompt_mismatch", "Delta root differs from its actual input projection")
        return advance_context(view, payload["view_ref"], delta, payload["delta_ref"])
    return deepcopy(view)


def _validate_view_lineage(view, resolve):
    derivation = view["derivation"]
    ref = derivation["input_view_ref"]
    if ref is None:
        require(derivation["operation"] == "read" and not view["units"]
                and not view["accepted_delta_ids"] and not view["applied_delta_ids"],
                "context_invalid_derivation", "An empty read cannot manufacture history")
        return
    original = validate_context_view(resolve(ref))
    if derivation["operation"] == "read":
        require(view["units"] == original["units"] and not view["applied_delta_ids"]
                and set(view["accepted_delta_ids"]) == set(
                    original["accepted_delta_ids"] + original["applied_delta_ids"]),
                "context_invalid_derivation", "Object read cannot change retained history")
    elif derivation["operation"] == "window":
        for key in ("owner", "basis", "accepted_delta_ids", "applied_delta_ids"):
            require(view[key] == original[key], "context_invalid_derivation",
                    "Window cannot change ownership, basis or consumption")
        count = len(view["units"])
        require(view["units"] == (original["units"][-count:] if count else []),
                "context_invalid_derivation", "Window preserves complete unit order")
    else:
        delta = validate_agent_delta(resolve(derivation["delta_ref"]))
        expected = advance_context(original, ref, delta, derivation["delta_ref"])
        require(canonical_bytes(expected) == canonical_bytes(view), "context_invalid_derivation",
                "Advance must append only the exact accepted delta to the selected view")
