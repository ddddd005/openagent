"""Reject schema-invalid tool batches without accepting executable calls."""

from jsonschema import Draft202012Validator, ValidationError

from .contract_errors import ContractValidationError
from .contract_json import loads_strict


def tool_batch_argument_feedback(calls, definitions, seen_ids=()):
    tools = {item["name"]: item for item in definitions}
    seen = set(seen_ids)
    invalid = []
    for call in calls:
        if (call["type"] != "function" or type(call["id"]) is not str or not call["id"]
                or call["id"] in seen or call["name"] not in tools):
            raise ContractValidationError("Invalid tool call identity")
        seen.add(call["id"])
        arguments = loads_strict(call["raw_arguments"])
        if type(arguments) is not dict:
            raise ContractValidationError("Tool arguments must be an object")
        if call["name"] == "final_answer":
            if len(calls) != 1:
                raise ContractValidationError("Final answer must be exclusive")
            continue
        try:
            Draft202012Validator(tools[call["name"]]["parameters_schema"]).validate(arguments)
        except ValidationError:
            if call["name"] not in invalid:
                invalid.append(call["name"])
    if not invalid:
        return None
    return ("Invalid tool arguments for: " + ", ".join(invalid)
            + ". No tools in this batch were executed. Generate a new tool call batch with valid arguments.")
