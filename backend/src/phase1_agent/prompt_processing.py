"""Pure, serial prompt/text processing; no history, storage or model dispatch."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import re
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import content_digest, validate_json_value
from .prompt_errors import PromptProcessingError
from .prompt_macro import MACRO_SYNTAX, MacroLimits, macro_replace
from .prompt_regex import (
    REGEX_SYNTAX, RegexLimits, regex_replace_many, regex_rule_from_dict, validate_regex_limits,
)
from .prompt_values import validate_prompt_collection, validate_prompt_item
from .prompt_variables import validate_variable_snapshot


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


@dataclass(frozen=True)
class PromptProcessingLimits:
    max_nodes: int = 32
    max_items: int = 1024
    max_total_chars: int = 1_000_000
    macro: MacroLimits = MacroLimits(1_000_000, 1_000_000, 10_000)
    regex: RegexLimits = RegexLimits()

    def __post_init__(self) -> None:
        for value in (self.max_nodes, self.max_items, self.max_total_chars):
            if type(value) is not int or value < 0:
                raise ContractValidationError("Prompt pipeline limits must be nonnegative integers")
        if type(self.macro) is not MacroLimits or type(self.regex) is not RegexLimits:
            raise ContractValidationError("Prompt pipeline requires explicit processing limits")
        validate_regex_limits(self.regex)


def _uuid(value: Any, *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (
        type(value) is str and _UUID.fullmatch(value) is not None
    )


def validate_prompt_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validate_json_value(steps)
    if type(steps) is not list:
        raise ContractValidationError("Prompt processing steps must be a JSON array")
    seen: set[str] = set()
    for step in steps:
        if type(step) is not dict:
            raise ContractValidationError("Prompt processing step must be an object")
        fields = {"schema_version", "node_id", "kind", "enabled", "select"}
        kind = step.get("kind")
        if kind == "regex":
            fields.add("rule")
        if (
            set(step) != fields or kind not in ("macro", "regex")
            or type(step["schema_version"]) is not int or step["schema_version"] != 1
            or type(step["enabled"]) is not bool or not _uuid(step["node_id"])
        ):
            raise ContractValidationError("Prompt processing step is invalid")
        if step["node_id"] in seen:
            raise ContractValidationError("Duplicate prompt processing node")
        seen.add(step["node_id"])
        selector = step["select"]
        if type(selector) is not dict:
            raise ContractValidationError("Prompt selector must be an object")
        if selector == {"mode": "all"}:
            pass
        elif set(selector) == {"mode", "instances"} and selector["mode"] == "instances":
            if type(selector["instances"]) is not list:
                raise ContractValidationError("Prompt selector instances must be an array")
            identities = set()
            for identity in selector["instances"]:
                if (
                    type(identity) is not dict or set(identity) != {
                        "group_instance_id", "item_instance_id",
                    }
                    or not _uuid(identity["group_instance_id"], nullable=True)
                    or not _uuid(identity["item_instance_id"])
                ):
                    raise ContractValidationError("Prompt selector identity is invalid")
                key = identity["group_instance_id"], identity["item_instance_id"]
                if key in identities:
                    raise ContractValidationError("Duplicate prompt selector instance")
                identities.add(key)
        else:
            raise ContractValidationError("Unknown prompt selector")
        if kind == "regex":
            regex_rule_from_dict(step["rule"])
    return copy.deepcopy(steps)


def _check_size(items: list[dict[str, Any]], limits: PromptProcessingLimits, node_id=None) -> None:
    if len(items) > limits.max_items or sum(len(item["text"]) for item in items) > limits.max_total_chars:
        raise PromptProcessingError(
            "prompt_pipeline_limit", "Prompt processing exceeds its item or total text limit",
            node_id=node_id,
        )


def _selected(items, step):
    selector = step["select"]
    if selector["mode"] == "all":
        return list(range(len(items)))
    available = {
        (item["group_instance_id"], item["item_instance_id"]): index
        for index, item in enumerate(items)
    }
    requested = {
        (identity["group_instance_id"], identity["item_instance_id"])
        for identity in selector["instances"]
    }
    if not requested <= available.keys():
        raise PromptProcessingError(
            "prompt_selector_missing", "Prompt selector references an unavailable instance",
            node_id=step["node_id"],
        )
    return [index for index, item in enumerate(items) if (
        item["group_instance_id"], item["item_instance_id"]
    ) in requested]


def _run(
    value: Any, value_kind: str, steps: list[dict[str, Any]],
    variables: dict[str, Any] | None, limits: PromptProcessingLimits,
) -> dict[str, Any]:
    if type(limits) is not PromptProcessingLimits:
        raise ContractValidationError("Prompt processing requires explicit limits")
    steps = validate_prompt_steps(steps)
    if len(steps) > limits.max_nodes:
        raise PromptProcessingError("prompt_pipeline_limit", "Prompt processing exceeds its node limit")
    collection_version = 1
    if value_kind == "text":
        validate_json_value(value)
        if type(value) is not str:
            raise ContractValidationError("Text processing input must be text")
        items = [{"text": value, "interpolation": "variables"}]
        if any(step["select"] != {"mode": "all"} for step in steps):
            raise ContractValidationError("Text processing cannot select prompt instances")
    elif value_kind == "prompt_item":
        items = [validate_prompt_item(value)]
    else:
        collection = validate_prompt_collection(value)
        collection_version = collection["schema_version"]
        items = collection["items"]
    _check_size(items, limits)
    variables = None if variables is None else validate_variable_snapshot(variables)
    trace = []
    for step in steps:
        node_id = step["node_id"]
        indexes = list(range(len(items))) if value_kind == "text" else _selected(items, step)
        before = content_digest(items)
        executed = []
        if step["enabled"]:
            if step["kind"] == "macro":
                executed = [index for index in indexes if items[index]["interpolation"] != "literal"]
                if executed and variables is None:
                    raise PromptProcessingError(
                        "prompt_variables_missing", "Macro processing requires a variable snapshot",
                        node_id=node_id,
                    )
                output_chars = 0
                for index in executed:
                    item = items[index]
                    try:
                        item["text"] = macro_replace(
                            item["text"], variables, node_id=node_id, limits=limits.macro,
                        )
                    except PromptProcessingError as exc:
                        exc.item_instance_id = item.get("item_instance_id")
                        exc.group_instance_id = item.get("group_instance_id")
                        raise
                    output_chars += len(item["text"])
                    if output_chars > limits.max_total_chars:
                        raise PromptProcessingError(
                            "prompt_pipeline_limit", "Prompt processing exceeds its total text limit",
                            node_id=node_id,
                        )
            else:
                executed = indexes
                outputs = regex_replace_many(
                    [items[index]["text"] for index in indexes], regex_rule_from_dict(step["rule"]),
                    limits=limits.regex, node_id=node_id,
                )
                for index, output in zip(indexes, outputs):
                    items[index]["text"] = output
        _check_size(items, limits, node_id)
        trace.append({
            "step": copy.deepcopy(step), "input_digest": before, "output_digest": content_digest(items),
            "processed_instances": [] if value_kind == "text" else [{
                "group_instance_id": items[index]["group_instance_id"],
                "item_instance_id": items[index]["item_instance_id"],
            } for index in executed],
            "processed_text": value_kind == "text" and bool(executed),
            "variable_snapshot_digest": content_digest(variables) if (
                step["enabled"] and step["kind"] == "macro" and executed
            ) else None,
        })
    output = items[0]["text"] if value_kind == "text" else (
        items[0] if value_kind == "prompt_item" else {
            "schema_version": collection_version, "kind": "prompt_collection", "items": items,
        }
    )
    return {
        "schema_version": 1, "kind": "prompt_processing_result", "value_kind": value_kind,
        "value": output, "trace": trace, "limits": asdict(limits),
        "syntax": {"macro": MACRO_SYNTAX, "regex": REGEX_SYNTAX},
    }


def process_prompt_collection(
    collection: dict[str, Any], steps: list[dict[str, Any]], *,
    variables: dict[str, Any] | None = None, limits: PromptProcessingLimits = PromptProcessingLimits(),
) -> dict[str, Any]:
    return _run(collection, "prompt_collection", steps, variables, limits)


def process_prompt_item(
    item: dict[str, Any], steps: list[dict[str, Any]], *,
    variables: dict[str, Any] | None = None, limits: PromptProcessingLimits = PromptProcessingLimits(),
) -> dict[str, Any]:
    return _run(item, "prompt_item", steps, variables, limits)


def process_text(
    text: str, steps: list[dict[str, Any]], *,
    variables: dict[str, Any] | None = None, limits: PromptProcessingLimits = PromptProcessingLimits(),
) -> dict[str, Any]:
    return _run(text, "text", steps, variables, limits)
