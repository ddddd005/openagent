"""Exact public prompt choices materialized without reading catalog heads."""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Protocol

from .contract_errors import ContractValidationError
from .contract_json import validate_json_value
from .prompt_config import validate_prompt_record
from .prompt_errors import PromptProcessingError
from .prompt_preparation import make_prompt_context_config, validate_prompt_context_config
from .prompt_values import collection_from_config
from .prompt_variables import assign_variable, create_variable_registry, create_variable_snapshot
from .preparation_program import program_definitions


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
MAX_PROMPT_REVISION = 2**63 - 1


class PromptRevisionReader(Protocol):
    def get_revision(self, kind: str, definition_id: str, revision: int) -> dict | None: ...


def _error(message: str, *, status: int = 400, reason: str = "invalid_request") -> ContractValidationError:
    result = ContractValidationError(message)
    result.status_code, result.reason_code = status, reason
    return result


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _error(message)


def _uuid(value: Any, message: str) -> None:
    _require(type(value) is str and _UUID.fullmatch(value) is not None, message)


def validate_prompt_selection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Missing stages retain their configured default; no implicit latest."""
    try:
        validate_json_value(value)
    except ContractValidationError as exc:
        raise _error("Prompt selection must contain strict JSON") from exc
    _require(
        type(value) is dict and set(value) == {"schema_version", "kind", "nodes"},
        "Prompt selection fields must be exact",
    )
    _require(
        type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "workflow_prompt_selection",
        "Unknown workflow prompt selection version or kind",
    )
    nodes = value["nodes"]
    _require(type(nodes) is dict and set(nodes) <= {"A", "B"}, "Prompt selection nodes must be A or B")
    for reference in nodes.values():
        _require(
            type(reference) is dict and set(reference) == {"config_id", "revision"},
            "Prompt selection references must be exact",
        )
        _uuid(reference["config_id"], "Prompt selection config identity must be a canonical UUID4")
        _require(
            type(reference["revision"]) is int and 1 <= reference["revision"] <= MAX_PROMPT_REVISION,
            "Prompt selection requires a positive exact revision",
        )
    return copy.deepcopy(value)


def validate_prompt_selection_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_json_value(value)
    _require(
        type(value) is dict and set(value) == {"schema_version", "kind", "nodes"},
        "Frozen prompt selection plan fields must be exact",
    )
    _require(
        type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "workflow_prompt_selection_plan",
        "Unknown frozen prompt selection plan version or kind",
    )
    _require(
        type(value["nodes"]) is dict and set(value["nodes"]) <= {"A", "B"},
        "Frozen prompt selection plan nodes must be A or B",
    )
    for config in value["nodes"].values():
        validate_prompt_context_config(config)
    return copy.deepcopy(value)


def make_prompt_selection_plan(nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return validate_prompt_selection_plan({
        "schema_version": 1, "kind": "workflow_prompt_selection_plan", "nodes": nodes,
    })


def compose_selected_prompt_context(
    selected_config: Mapping[str, Any],
    default_context_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace exact prompt material without discarding configured preparation."""
    selected = validate_prompt_context_config(selected_config)
    if selected["schema_version"] == 3:
        return selected
    if default_context_config is None:
        return selected
    combined = validate_prompt_context_config(default_context_config)
    combined["collection"] = selected["collection"]
    combined["prompt_config"] = selected["prompt_config"]
    combined = validate_prompt_context_config(combined)
    available = {
        (item["group_instance_id"], item["item_instance_id"])
        for item in combined["collection"]["items"]
    }
    available.update(
        (None, instance["item_instance_id"])
        for entry in combined["lorebooks"] for instance in entry["request"]["entry_instances"]
    )
    for step in combined["steps"]:
        selector = step["select"]
        if selector["mode"] == "instances":
            _require({
                (identity["group_instance_id"], identity["item_instance_id"])
                for identity in selector["instances"]
            } <= available, "Selected prompt collection is incompatible with a configured selector")
    variables = combined["variables"]
    if variables is not None and selected["variables"] is not None:
        _require(all(
            variables["registry"][field] == selected["variables"]["registry"][field]
            for field in ("workflow_id", "revision")
        ), "Selected prompt configuration has another variable registry owner")
    if combined["schema_version"] == 2:
        for assignment in combined["variable_plan"]["assignments"]:
            source, name = assignment["source"], assignment["name"]
            if source["kind"] == "constant":
                assign_variable(
                    variables, name, source["value"], node_id=assignment["node_id"],
                )
            else:
                _require(name in variables["values"] and variables["values"][name]["type"] == "string",
                         "Root input assignment requires a registered string variable")
    return combined


def resolve_prompt_selection(
    selection: Mapping[str, Any], catalog: PromptRevisionReader, *,
    workflow_definition_id: str, workflow_session_id: str,
    node_bindings: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Resolve immutable definitions into complete, detached Context configs."""
    selection = validate_prompt_selection(selection)
    _uuid(workflow_definition_id, "Workflow definition identity must be a canonical UUID4")
    _uuid(workflow_session_id, "Workflow session identity must be a canonical UUID4")
    _require(
        type(node_bindings) is dict and set(node_bindings) == {"A", "B"},
        "Prompt selection requires explicit A/B node bindings",
    )
    for binding in node_bindings.values():
        _uuid(binding, "Node binding identity must be a canonical UUID4")
    _require(len(set(node_bindings.values())) == 2, "Agent prompt consumers must have distinct bindings")
    _require(callable(getattr(catalog, "get_revision", None)), "Prompt selection requires an exact revision reader")
    cache: dict[tuple[str, str, int], dict[str, Any]] = {}

    def read(kind: str, identity: str, revision: int, *, root: bool = False) -> dict[str, Any]:
        key = kind, identity, revision
        if key not in cache:
            value = catalog.get_revision(kind, identity, revision)
            if value is None:
                raise _error(
                    "Selected prompt configuration was not found" if root else
                    "Stored prompt configuration has a missing exact reference",
                    status=404 if root else 500,
                    reason="not_found" if root else "storage_contract_violation",
                )
            try:
                value = validate_prompt_record(kind, value)
            except ContractValidationError as exc:
                raise _error(
                    "Stored prompt selection definition is invalid",
                    status=500, reason="storage_contract_violation",
                ) from exc
            field = {"item": "item_id", "group": "group_id", "config": "config_id"}[kind]
            if value[field] != identity or value["revision"] != revision:
                raise _error(
                    "Stored prompt selection exact identity differs",
                    status=500, reason="storage_contract_violation",
                )
            cache[key] = value
        return copy.deepcopy(cache[key])

    result = {}
    for stage in ("A", "B"):
        if stage not in selection["nodes"]:
            continue
        reference = selection["nodes"][stage]
        config = read("config", reference["config_id"], reference["revision"], root=True)
        items: dict[tuple[str, int], dict[str, Any]] = {}
        groups: dict[tuple[str, int], dict[str, Any]] = {}
        for entry in config["inputs"]:
            if entry["kind"] == "item":
                key = entry["item_id"], entry["revision"]
                items.setdefault(key, read("item", *key))
            else:
                key = entry["group_id"], entry["revision"]
                group = read("group", *key)
                groups.setdefault(key, group)
                for member in group["members"]:
                    key = member["item_id"], member["revision"]
                    items.setdefault(key, read("item", *key))
        try:
            collection = collection_from_config(
                config, lambda identity, revision: items[(identity, revision)],
                lambda identity, revision: groups[(identity, revision)],
            )
            registry = create_variable_registry(
                workflow_id=workflow_definition_id, revision=1, definitions=[],
            )
            variables = create_variable_snapshot(
                registry, workflow_session_id=workflow_session_id,
                node_binding_id=node_bindings[stage],
            )
            result[stage] = make_prompt_context_config(
                collection, prompt_config={
                    "config": config, "items": list(items.values()), "groups": list(groups.values()),
                },
                variables=variables,
                preparation=config.get("preparation"),
            )
        except PromptProcessingError as exc:
            exc.status_code, exc.reason_code = 400, "invalid_request"
            raise
        except (ContractValidationError, KeyError) as exc:
            raise _error(
                "Stored prompt configuration references cannot be materialized",
                status=500, reason="storage_contract_violation",
            ) from exc
    definitions = {}
    for context_config in result.values():
        if context_config["schema_version"] == 3:
            for definition in program_definitions(context_config["preparation"]):
                previous = definitions.get(definition["name"])
                _require(previous is None or previous == definition,
                         "A/B variable declarations differ")
                definitions[definition["name"]] = definition
    return result
