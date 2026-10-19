"""Preparation of workflow tool bindings under one trusted run basis.

The loading anchor is a real, non-working graph declaration.  This module turns
its explicit references into a frozen, per-invocation list of model tools and
real capability targets.  It only reads: no executor is called, no node run is
created, no graph edge or execution target is added.
"""

from __future__ import annotations

import re
from copy import deepcopy
from uuid import uuid4

from .contract_errors import require
from .graph_contracts import validate_config_node_references
from .tool_binding_contract import (
    PreparedToolBinding, PreparedToolFrame, PreparedToolSpec,
)
from .tools import _model_tool_schema


_ALIAS_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,54}$"
_OPERATIONS = ("read", "set", "add", "subtract")


def _node_map(document: dict) -> dict[str, dict]:
    return {node["node_binding_id"]: node for node in document["nodes"]}


def _check_spec(value, *, operations: list[str]) -> dict:
    require(type(value) is dict and set(value) == {
        "name", "operation", "access", "description", "parameters", "target", "config",
    }, "tool_binding_invalid", "Tool binding declaration fields differ")
    require(type(value["name"]) is str and value["name"]
            and type(value["description"]) is str,
            "tool_binding_invalid", "Tool binding requires an explicit name and description")
    require(value["operation"] in operations, "tool_binding_operation_unavailable",
            "Tool binding declares an operation outside its variable entry")
    require(value["access"] in ("read", "write") and
            (value["access"] == "write") == (value["operation"] != "read"),
            "tool_binding_invalid", "Tool access differs from its operation")
    _model_tool_schema(value["name"], value["parameters"])
    target = value["target"]
    require(type(target) is dict and set(target) == {"component_id", "component_version"}
            and all(type(target[key]) is str and target[key] for key in target),
            "tool_binding_invalid", "Tool binding requires an exact target identity")
    require(type(value["config"]) is dict, "tool_binding_invalid",
            "Tool binding requires an exact target configuration")
    return value


def _check_target(registry, definitions, spec: dict) -> tuple[dict, ...]:
    target = spec["target"]
    entry = registry.get(target["component_id"], target["component_version"])
    require(entry is not None, "tool_binding_target_unavailable",
            "Tool binding target implementation is unavailable")
    view = definitions.get("node", dict(target))
    require(view is not None, "tool_binding_target_unavailable",
            "Tool binding target definition source is unavailable")
    require(entry.executor is not None and entry.executor_ref is None,
            "tool_binding_target_unsupported",
            "Tool binding targets require a native executor in this batch")
    definition = entry.definition
    require(definition.execution_class == "working", "tool_binding_target_unsupported",
            "Tool binding target must be an ordinary working node")
    require(not definition.service_requirements, "tool_binding_target_unsupported",
            "Tool binding target declares a host service")
    require(not ({"resources:read", "external:read", "private:read", "private:write"}
                 & set(definition.capabilities)), "tool_binding_target_unsupported",
            "Tool binding target declares resources, external inputs or private state")
    require(not entry.resource_input_ports and entry.settlement_builder is None
            and not definition.config_node_references
            and not definition.provided_session_item_types,
            "tool_binding_target_unsupported",
            "Tool binding target requires graph wiring or special settlement")
    registry.validate_config(entry, spec["config"])
    return tuple(view["source"])


def prepare_tool_frame(*, data, sid: str, document: dict, plan, registry,
                       definitions) -> PreparedToolFrame:
    """Freeze every explicit binding that targets an Agent participating in this plan."""
    nodes = _node_map(document)
    ordered = set(plan.ordered_node_ids)
    document_bindings = {value["object_key"]: value
                         for value in document.get("object_bindings", [])}
    agent_bindings: dict[str, list[PreparedToolBinding]] = {}
    anchors = []
    target_count = 0
    for anchor in document["nodes"]:
        entry = registry.get(anchor["component_id"], anchor["component_version"])
        if entry is None or entry.tool_binding is None:
            continue
        anchor_view = definitions.get("node", {
            "component_id": anchor["component_id"],
            "component_version": anchor["component_version"]})
        require(anchor_view is not None, "tool_binding_target_unavailable",
                "Tool binding anchor definition source is unavailable", 409)
        anchor_source = tuple(deepcopy(anchor_view["source"]))
        anchors.append(anchor["node_binding_id"])
        registry.validate_config(entry, anchor["config"])
        validate_config_node_references(entry, anchor, nodes)
        agent_node_id = anchor["config"]["agent_node_id"]
        if agent_node_id not in ordered:
            continue
        normalized = entry.tool_binding.normalize(anchor["config"])
        require(type(normalized) is list and 0 < len(normalized) <= 64,
                "tool_binding_invalid", "Tool bindings require a bounded nonempty declaration")
        prepared: list[PreparedToolBinding] = []
        seen_aliases = {binding.alias for binding in agent_bindings.get(agent_node_id, [])}
        seen_names = {tool.name for binding in agent_bindings.get(agent_node_id, [])
                      for tool in binding.tools}
        for declaration in normalized:
            require(type(declaration) is dict and set(declaration) == {
                "alias", "object_key", "operations"},
                "tool_binding_invalid", "Variable tool declaration fields differ")
            alias, object_key = declaration["alias"], declaration["object_key"]
            operations = declaration["operations"]
            require(type(alias) is str and re.fullmatch(_ALIAS_PATTERN, alias) is not None
                    and alias not in seen_aliases and alias != "final_answer",
                    "tool_binding_alias_conflict", "Variable tool alias is invalid or repeated")
            require(type(operations) is list and bool(operations)
                    and all(type(operation) is str and operation in _OPERATIONS
                            for operation in operations)
                    and len(set(operations)) == len(operations),
                    "tool_binding_operation_unavailable",
                    "Variable tool operations must be a unique nonempty subset")
            require(type(object_key) is str and object_key in document_bindings,
                    "tool_binding_variable_missing",
                    "Variable tool target is not bound in this session")
            declared = document_bindings[object_key]
            require(agent_node_id in declared["readers"], "tool_binding_access_denied",
                    "The Agent is not an explicit reader of the bound object")
            if any(operation != "read" for operation in operations):
                require(agent_node_id in declared["writers"], "tool_binding_access_denied",
                        "The Agent is not an explicit writer of the bound object")
            item = data.state.read_object(sid, object_key, node_id=agent_node_id)
            specs = entry.tool_binding.bind(declaration, item)
            require(type(specs) is list and bool(specs), "tool_binding_invalid",
                    "Variable tool binding produced no exact tool")
            binding_id = str(uuid4())
            tools = []
            for value in specs:
                value = _check_spec(value, operations=operations)
                require(value["name"] not in seen_names, "tool_binding_alias_conflict",
                        "Model tool name is repeated for one Agent")
                require(value["operation"] in operations, "tool_binding_operation_unavailable",
                        "Variable tool produced an undeclared operation")
                source = _check_target(registry, definitions, value)
                target = value["target"]
                tools.append(PreparedToolSpec(
                    name=value["name"], operation=value["operation"], access=value["access"],
                    description=value["description"],
                    parameters=value["parameters"],
                    target_component_id=target["component_id"],
                    target_component_version=target["component_version"],
                    config=value["config"], binding_id=binding_id,
                    agent_node_id=agent_node_id, object_key=object_key, source=source))
                seen_names.add(value["name"])
                require(len(seen_names) <= 120, "tool_binding_invalid",
                        "Agent tool bindings exceed the bounded model tool budget")
            require({tool.operation for tool in tools} == set(operations),
                    "tool_binding_invalid",
                    "Variable tool set differs from its declared operations")
            prepared.append(PreparedToolBinding(
                binding_id=binding_id, anchor_node_id=anchor["node_binding_id"],
                anchor_component_id=anchor["component_id"],
                anchor_component_version=anchor["component_version"],
                agent_node_id=agent_node_id, alias=alias, object_key=object_key,
                operations=tuple(operations), tools=tuple(tools),
                anchor_source=anchor_source))
            seen_aliases.add(alias)
            target_count += len(tools)
        agent_bindings.setdefault(agent_node_id, []).extend(prepared)
    descriptors: dict[str, str] = {}
    for node in document["nodes"]:
        if node["node_binding_id"] not in ordered:
            continue
        entry = registry.get(node["component_id"], node["component_version"])
        if entry is None:
            continue
        referenced = []
        for reference in entry.definition.config_node_references:
            value = node["config"].get(reference["config_field"])
            if reference.get("allow_empty", False) and value == "":
                continue
            values = value if reference["multiple"] else [value]
            referenced.extend(item for item in values if item in agent_bindings)
        identities = set(referenced)
        if not identities:
            continue
        require(len(identities) == 1, "tool_binding_descriptor_ambiguous",
                "A tool material node must reference one Agent binding set")
        descriptors[node["node_binding_id"]] = identities.pop()
    return PreparedToolFrame(
        agent_bindings={key: tuple(value) for key, value in agent_bindings.items()},
        descriptors=descriptors, anchors=tuple(anchors), target_count=target_count)
