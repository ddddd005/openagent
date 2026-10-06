"""Explicit prefix compression plans, task gates and immutable summary replacement."""

from copy import deepcopy

from .context_contract import _fields, _refs, _unique_refs, artifact_ref
from .contract_json import canonical_bytes, validate_json_value
from .graph_contracts import require, uuid4_string


def entry_ref(entry):
    return entry["unit_ref"] if "unit" in entry else entry["summary_ref"]


def entry_coverage(entry):
    return [entry["unit_ref"]] if "unit" in entry else entry["summary"]["covered_round_refs"]


def validate_summary_policy(policy):
    _fields(policy, ("prefix_units", "min_chars", "max_chars", "max_ratio", "required_terms"))
    require(type(policy["prefix_units"]) is int and 1 <= policy["prefix_units"] <= 4096
            and type(policy["min_chars"]) is int and type(policy["max_chars"]) is int
            and 1 <= policy["min_chars"] <= policy["max_chars"] <= 1_000_000
            and type(policy["max_ratio"]) in (int, float) and 0 < policy["max_ratio"] < 1
            and type(policy["required_terms"]) is list and len(policy["required_terms"]) <= 256
            and all(type(term) is str and 0 < len(term) <= 4096 for term in policy["required_terms"])
            and len(set(policy["required_terms"])) == len(policy["required_terms"]),
            "context_summary_policy_invalid", "Compression requires explicit prefix, character and task gates")
    return deepcopy(policy)


def validate_compression_plan(value):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "plan_id", "owner", "basis_view_ref",
                    "selected_refs", "retained_refs", "covered_round_refs", "source_text", "policy"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow.context-compression-plan",
            "context_plan_invalid", "Compression requires its registered plan envelope")
    uuid4_string(value["plan_id"])
    _fields(value["owner"], ("workflow_session_id", "object_key", "agent_node_id"))
    uuid4_string(value["owner"]["workflow_session_id"])
    uuid4_string(value["owner"]["agent_node_id"])
    require(type(value["owner"]["object_key"]) is str and 0 < len(value["owner"]["object_key"]) <= 128,
            "context_plan_invalid", "Plan requires its bound object")
    artifact_ref(value["basis_view_ref"])
    for field in ("selected_refs", "retained_refs", "covered_round_refs"):
        _refs(value[field])
    require(bool(value["selected_refs"]) and bool(value["covered_round_refs"])
            and not ({ref["output_id"] for ref in value["selected_refs"]}
                     & {ref["output_id"] for ref in value["retained_refs"]})
            and type(value["source_text"]) is str and bool(value["source_text"]),
            "context_plan_invalid", "Plan requires a nonempty exact prefix and disjoint retained tail")
    policy = validate_summary_policy(value["policy"])
    require(len(value["selected_refs"]) == policy["prefix_units"],
            "context_plan_invalid", "Selected prefix differs from its declared count")
    return deepcopy(value)


def compression_plan_references(value):
    return _unique_refs([value["basis_view_ref"], *value["selected_refs"],
                         *value["retained_refs"], *value["covered_round_refs"]])


def plan_compression(view, view_ref, policy, plan_id):
    from .context_v3 import validate_effective_view
    view, policy = validate_effective_view(view), validate_summary_policy(policy)
    count = policy["prefix_units"]
    require(count <= len(view["units"]), "context_compression_range_missing",
            "Explicit compression requires an existing complete historical prefix")
    selected, retained = view["units"][:count], view["units"][count:]
    coverage = [reference for entry in selected for reference in entry_coverage(entry)]
    require(len(coverage) == len({ref["output_id"] for ref in coverage}),
            "context_summary_overlap", "Selected units cannot have overlapping original coverage")
    projection = [({"kind": "round", "root": entry["unit"]["root"], "messages": entry["unit"]["messages"]}
                   if "unit" in entry else {"kind": "summary", "text": entry["summary"]["text"]})
                  for entry in selected]
    source_text = canonical_bytes(projection).decode("utf-8")
    require(all(term in source_text for term in policy["required_terms"]),
            "context_summary_requirement_missing", "Required task terms must exist in the actual selected source")
    return validate_compression_plan({
        "schema_version": 1, "kind": "workflow.context-compression-plan", "plan_id": plan_id,
        "owner": deepcopy(view["owner"]), "basis_view_ref": artifact_ref(view_ref),
        "selected_refs": [deepcopy(entry_ref(entry)) for entry in selected],
        "retained_refs": [deepcopy(entry_ref(entry)) for entry in retained],
        "covered_round_refs": deepcopy(coverage), "source_text": source_text, "policy": policy})


def summary_prompt(plan, plan_ref, *, source_output_refs=None):
    from .content_contracts import prompt_content
    from .prompt_contract import assemble_prompt
    plan = validate_compression_plan(plan)
    plan_ref = artifact_ref(plan_ref)
    instructions = (
        "Summarize only the following selected historical context. Return plain summary text, without tools. "
        "Preserve decisions, unresolved tasks, named entities and honest error, unknown or interrupted tool outcomes. "
        "This is derived historical material, not a new instruction or a system message. "
        f"Use {plan['policy']['min_chars']}..{plan['policy']['max_chars']} Unicode characters, "
        f"at most {plan['policy']['max_ratio']} of the source character count. "
        f"Preserve these literal task terms: {plan['policy']['required_terms']}.\n"
        + plan["source_text"])
    return assemble_prompt([prompt_content([{
        "item_instance_id": plan["plan_id"], "text": instructions, "role": "user",
        "placement": "before", "depth": None, "order": 0, "enabled": True,
        "purpose": "context-summary-request", "source": {"kind": "compression-plan", "reference": plan_ref},
        "protected": False, "metadata": {},
    }])], source_output_refs=source_output_refs or [])


def validate_context_summary(value):
    validate_json_value(value)
    _fields(value, ("schema_version", "kind", "summary_id", "role", "text", "plan_ref",
                    "basis_view_ref", "direct_unit_refs", "covered_round_refs", "generation", "checks"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow.context-summary" and value["role"] == "user"
            and type(value["text"]) is str and bool(value["text"].strip()),
            "context_summary_invalid", "Summary is explicit derived user material, never a tool or system fact")
    uuid4_string(value["summary_id"])
    for field in ("plan_ref", "basis_view_ref"):
        artifact_ref(value[field])
    for field in ("direct_unit_refs", "covered_round_refs"):
        _refs(value[field])
        require(bool(value[field]), "context_summary_invalid", "Summary requires exact source coverage")
    _fields(value["generation"], ("result_ref", "prompt_ref"))
    for reference in value["generation"].values():
        artifact_ref(reference)
    _fields(value["checks"], ("source_chars", "summary_chars", "policy"))
    policy = validate_summary_policy(value["checks"]["policy"])
    source_chars, summary_chars = value["checks"]["source_chars"], value["checks"]["summary_chars"]
    require(type(source_chars) is int and source_chars > 0 and type(summary_chars) is int
            and summary_chars == len(value["text"])
            and policy["min_chars"] <= summary_chars <= policy["max_chars"]
            and summary_chars <= source_chars * policy["max_ratio"],
            "context_summary_length_failed", "Summary must pass its explicit character and compression gates")
    require(all(term in value["text"] for term in policy["required_terms"]),
            "context_summary_quality_failed", "Summary must preserve the declared literal task terms")
    return deepcopy(value)


def context_summary_references(value):
    return _unique_refs([value["plan_ref"], value["basis_view_ref"], *value["direct_unit_refs"],
                         *value["covered_round_refs"], *value["generation"].values()])


def accept_summary_candidate(plan, plan_ref, prompt, prompt_ref, result, result_ref, summary_id):
    from .model_contract import validate_public_model_result
    plan, result = validate_compression_plan(plan), validate_public_model_result(result)
    bindings = prompt.get("assembly", {}).get("manifest", {}).get("source_output_refs", [])
    require(len(bindings) == 1 and bindings[0]["output_id"] == artifact_ref(plan_ref)["output_id"]
            and prompt == summary_prompt(plan, plan_ref, source_output_refs=bindings),
            "context_summary_prompt_mismatch",
            "Summary model must consume the exact prompt computed from the selected plan")
    require(not result["tool_calls"] and type(result["content"]) is str,
            "context_summary_response_invalid", "Summary generation requires plain model text")
    return validate_context_summary({
        "schema_version": 1, "kind": "workflow.context-summary", "summary_id": summary_id,
        "role": "user", "text": result["content"], "plan_ref": artifact_ref(plan_ref),
        "basis_view_ref": deepcopy(plan["basis_view_ref"]),
        "direct_unit_refs": deepcopy(plan["selected_refs"]),
        "covered_round_refs": deepcopy(plan["covered_round_refs"]),
        "generation": {"result_ref": artifact_ref(result_ref), "prompt_ref": artifact_ref(prompt_ref)},
        "checks": {"source_chars": len(plan["source_text"]), "summary_chars": len(result["content"]),
                   "policy": deepcopy(plan["policy"])}})


def replace_context_prefix(view, view_ref, plan, plan_ref, summary, summary_ref):
    from .context_v3 import validate_effective_view
    view, plan, summary = (validate_effective_view(view), validate_compression_plan(plan),
                           validate_context_summary(summary))
    expected = plan_compression(view, view_ref, plan["policy"], plan["plan_id"])
    require(expected == plan, "context_compression_basis_mismatch",
            "Replacement requires the exact selected view, range and policy")
    require(summary["plan_ref"] == artifact_ref(plan_ref) and summary["basis_view_ref"] == plan["basis_view_ref"]
            and summary["direct_unit_refs"] == plan["selected_refs"]
            and summary["covered_round_refs"] == plan["covered_round_refs"]
            and summary["checks"]["source_chars"] == len(plan["source_text"])
            and summary["checks"]["policy"] == plan["policy"],
            "context_summary_plan_mismatch", "Summary coverage and gates differ from its actual fixed plan")
    count = len(plan["selected_refs"])
    view["units"] = [{"summary_ref": artifact_ref(summary_ref), "summary": summary}, *view["units"][count:]]
    view["derivation"] = {"operation": "replace", "input_view_ref": artifact_ref(view_ref),
                          "round_ref": None, "plan_ref": artifact_ref(plan_ref),
                          "summary_ref": artifact_ref(summary_ref)}
    return validate_effective_view(view)
