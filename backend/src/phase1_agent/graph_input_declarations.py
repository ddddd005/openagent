"""Editor input discovery does not require a complete executable graph."""

from copy import deepcopy

from .graph_contracts import GraphCompiler, validate_graph_document
from .graph_records import require


def collect_input_declarations(registry, nodes):
    declarations = {}
    for node in nodes:
        node_id = node["node_binding_id"]
        entry = registry.get(node["component_id"], node["component_version"])
        require(entry is not None, "graph_missing_node_type", "Node declaration is unavailable", 409, node_id=node_id)
        registry.validate_config(entry, node["config"])
        callback = entry.external_inputs_declaration
        require(callback is not None or entry.external_inputs_validator is None,
                "consumer_input_undeclared", "External inputs have no declaration", 409, node_id=node_id)
        for value in callback(deepcopy(node["config"])) if callback else []:
            require(type(value) is dict and set(value) == {"name", "data_type", "required"}
                    and type(value["name"]) is str and 0 < len(value["name"]) <= 128
                    and value["name"] != "_workflow_frozen_resources"
                    and value["data_type"] in ("TEXT", "PROMPT") and type(value["required"]) is bool,
                    "consumer_input_invalid", "Invalid external input declaration", 409, node_id=node_id)
            previous = declarations.get(value["name"])
            require(previous is None or previous["data_type"] == value["data_type"],
                    "consumer_input_conflict", "External input declarations differ", 409, node_id=node_id)
            if previous is None:
                declarations[value["name"]] = {**value, "node_ids": [node_id]}
            else:
                previous["required"] |= value["required"]
                previous["node_ids"].append(node_id)
    return list(declarations.values())


def input_diagnostics(error, node_id=None):
    return [{**deepcopy(value), "reason_code": value.get("reason_code", value.get("code", "graph_invalid_config")),
             **({"node_id": node_id} if node_id else {})}
            for value in getattr(error, "diagnostics", [{
                "reason_code": getattr(error, "reason_code", "consumer_input_invalid"), "message": str(error),
            }])]


def editor_input_declarations(registry, document):
    document = validate_graph_document(document)
    declarations, diagnostics = {}, []
    # Resolve each draft node independently so one broken node does not hide the rest.
    for node in document["nodes"]:
        try:
            values = collect_input_declarations(registry, [node])
            for value in values:
                previous = declarations.get(value["name"])
                if previous and previous["data_type"] != value["data_type"]:
                    diagnostics.append({"reason_code": "consumer_input_conflict",
                                        "message": "External input declarations differ", "node_id": node["node_binding_id"]})
                    previous["conflict"] = True
                elif previous:
                    previous["required"] |= value["required"]
                    previous["node_ids"].extend(value["node_ids"])
                else:
                    declarations[value["name"]] = {**value, "conflict": False}
        except Exception as error:
            diagnostics.extend(input_diagnostics(error, node["node_binding_id"]))
    required = None
    try:
        plan = GraphCompiler(registry).compile(document)
        nodes = {node["node_binding_id"]: node for node in document["nodes"]}
        required = [value["name"] for value in collect_input_declarations(
            registry, [nodes[node_id] for node_id in plan.ordered_node_ids]) if value["required"]]
    except Exception as error:
        diagnostics.extend(input_diagnostics(error))
    return {"schema_version": 1, "kind": "workflow.input-declarations",
            "workflow_definition_id": document["workflow_definition_id"],
            "inputs": list(declarations.values()), "required_for_run": required, "diagnostics": diagnostics}
