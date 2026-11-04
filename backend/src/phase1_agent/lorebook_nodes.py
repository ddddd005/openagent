"""Read-only lorebook execution over one exact accepted scanning prompt."""

from copy import deepcopy

from .context_contract import artifact_ref
from .context_prompt_v6 import validate_native_context_prompt
from .contract_json import canonical_bytes
from .graph_contracts import require
from .lorebook_engine import evaluate_lorebook, evaluate_lorebook_content
from .variable_contract import read_variable


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
        record = read_variable(context, key)
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


def execute_lorebook_content(config, inputs, context, *, grouped=False):
    values_by_port = {port: inputs.get(port, []) for port in ("structured", "text")}
    require(any(values_by_port.values()), "lorebook_input_required",
            "Lorebook requires structured or text input")
    read_refs = {}
    for port, values in values_by_port.items():
        references = context.input_artifact_refs(port)
        require(type(values) is list and type(references) is list and len(values) == len(references),
                "lorebook_exact_artifact_required", "Each lorebook input requires an accepted artifact")
        read_refs[port] = []
        for value, raw in zip(values, references):
            require(type(raw) is dict and "output_id" in raw, "lorebook_exact_artifact_required",
                    "Each lorebook input requires an accepted artifact")
            reference = artifact_ref({"scope": "artifact", "output_id": raw["output_id"]})
            accepted = context.host_call("artifacts:read", "resolve-artifact", {"reference": reference})
            require(type(accepted) is dict and "value" in accepted
                    and canonical_bytes(accepted["value"]) == canonical_bytes(value),
                    "lorebook_artifact_mismatch", "Lorebook input differs from its accepted artifact")
            read_refs[port].append(reference)
    variables = [deepcopy(read_variable(context, key)["value"]) for key in config["object_keys"]]
    result = evaluate_lorebook_content(
        config["entries"] if grouped else [config["entry"]],
        values_by_port["structured"], values_by_port["text"],
        node_id=context.node_binding_id, variables=variables,
    )
    context.reads.append({
        "kind": "lorebook_evaluation", "input_refs": read_refs,
        "recursive_rounds": result["recursive_rounds"], "entries": deepcopy(result["diagnostics"]),
    })
    return {"output": result["materials"]}
