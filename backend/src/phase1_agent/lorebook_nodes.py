"""Read-only lorebook execution over one exact accepted scanning prompt."""

from copy import deepcopy

from .context_contract import artifact_ref
from .context_prompt_v6 import validate_native_context_prompt
from .contract_json import canonical_bytes
from .graph_contracts import require
from .lorebook_engine import evaluate_lorebook
from .tool_package import VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE


def execute_lorebook(config, inputs, context, *, grouped=False):
    prompt = validate_native_context_prompt(inputs["input"])
    references = context.input_artifact_refs("input")
    require(type(references) is list and len(references) == 1
            and type(references[0]) is dict and "output_id" in references[0],
            "lorebook_exact_artifact_required",
            "Lorebook scanning requires exactly one accepted prompt artifact")
    reference = artifact_ref({"scope": "artifact", "output_id": references[0]["output_id"]})
    accepted = context.host_call("artifacts:read", "resolve-artifact", {"reference": reference})
    require(type(accepted) is dict and "value" in accepted
            and canonical_bytes(accepted["value"]) == canonical_bytes(prompt),
            "lorebook_artifact_mismatch", "Scanning input differs from its exact accepted prompt")

    variables = []
    for key in config["object_keys"]:
        record = context.object_read(key)
        require((record["type_id"], record["schema_version"]) == (
            VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION),
            "variable_binding_type_mismatch", "Lorebook keywords require workflow.variable@1 objects")
        variables.append(deepcopy(record["value"]))

    result = evaluate_lorebook(
        config["entries"] if grouped else [config["entry"]],
        prompt, node_id=context.node_binding_id, variables=variables,
    )
    context.reads.append({
        "kind": "lorebook_evaluation", "input_ref": reference,
        "recursive_rounds": result["recursive_rounds"],
        "entries": deepcopy(result["diagnostics"]),
    })
    return {"output": result["materials"]}
