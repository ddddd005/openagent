"""Shared fixtures for the current logical-chat frontend node."""

from copy import deepcopy
from uuid import uuid4

from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE

from test_graph_service import document, edge, node, run, uid


def ref(number):
    return {"scope": "artifact", "output_id": uid(number)}


def binding(number):
    return {"edge_id": uid(number + 100), "output_id": uid(number), "order": 0}


def graph(service, *, count=1, bound=False, readers=True, writers=True):
    messages = [
        node(service.registry, "tools.editable-input", 19001 + index, text="message " + str(index))
        for index in range(count)
    ]
    frontends = []
    for index in range(count):
        entry = service.registry.get("frontend.node", "2")
        node_id = uid(19011 + index)
        config = {
            **deepcopy(entry.definition.default_config),
            "object_key": "chat-" + str(index),
            "input_node_id": messages[index]["node_binding_id"] if bound else "",
        }
        frontends.append({
            "node_binding_id": node_id, "component_id": "frontend.node", "component_version": "2",
            "title": "frontend.node", "position": {"x": 19011 + index, "y": 0}, "config": config,
        })
    result = document([*messages, *frontends], [
        edge(message, frontend, 19001 + index, target_port="content")
        for index, (message, frontend) in enumerate(zip(messages, frontends))
    ])
    result.update(
        schema_version=3, package_lock=list(service.registry.execution_package_lock),
        object_bindings=[
            {
                "object_key": entry["config"]["object_key"],
                "type_id": CHAT_CONTEXT_TYPE, "schema_version": 1, "scope": "shared",
                "readers": [entry["node_binding_id"]] if readers else [],
                "writers": [entry["node_binding_id"]] if writers else [],
                "owner_node_id": None, "registrar_node_id": entry["node_binding_id"],
            }
            for entry in frontends
        ],
    )
    for entry in result["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"])
    return result, messages, frontends


def save(service, value):
    saved = service.update_definition(
        value, workflow_definition_id=value["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()),
    )
    assert saved["outcome"] == "saved"
    return saved["document"]


def create(service, value):
    return service.create_session(
        value["workflow_definition_id"], value["revision"], idempotency_key=str(uuid4()),
    )


def output(session, entry, port):
    return next(item["outputs"][port] for item in session["nodes"]
                if item["node_binding_id"] == entry["node_binding_id"])


def owner(graph_value, frontend, session):
    return (graph_value["workflow_definition_id"], graph_value["revision"],
            frontend["node_binding_id"], session["workflow_session_id"])
