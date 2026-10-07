"""An independently authored plugin fixture using only public graph interfaces."""

import copy
from uuid import uuid4

from phase1_agent.content_contracts import prompt_content, text_content
from phase1_agent.graph_contracts import NodeDefinition, NodePort


PROBE_COMPONENT = "test.content-probe"


def current_document(registry, nodes, edges=(), *, roots=(), controls=(), bindings=()):
    return {
        "schema_version": 2, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Current public graph", "nodes": nodes, "edges": list(edges),
        "execution_roots": [item["node_binding_id"] for item in roots],
        "control_edges": list(controls), "object_bindings": list(bindings),
        "package_lock": list(registry.execution_package_lock),
    }


def run_current_graph(service, view, *, inputs=None, idempotency_key=None):
    started = service.start(
        view["workflow_session_id"], expected_revision=view["revision"],
        inputs=inputs or {}, idempotency_key=idempotency_key or str(uuid4()),
    )
    service.wait(started["active_chain_run_id"])
    return service.get_session(view["workflow_session_id"])


def register_content_probe(registry):
    def execute(config, inputs, context):
        state = context.private_read()
        context.private_write({"count": state["count"] + 1})
        value = inputs["input"]
        if config["mode"] == "text":
            return {"left": text_content("left:" + value["text"]),
                    "right": text_content("right:" + value["text"])}
        left, right = copy.deepcopy(value["items"]), copy.deepcopy(value["items"])
        for prefix, items in (("left:", left), ("right:", right)):
            for item in items:
                if not item["protected"]:
                    item["text"] = prefix + item["text"]
        return {"left": prompt_content(left), "right": prompt_content(right)}

    variants = {mode: ((NodePort("input", family, data_schema_version=2),),
                      (NodePort("left", family, data_schema_version=2),
                       NodePort("right", family, data_schema_version=2)))
                for mode, family in (("text", "TEXT"), ("prompt", "PROMPT"))}
    registry.register(NodeDefinition(
        component_id=PROBE_COMPONENT, component_version="1", display_name="Content probe", category="Test",
        default_config={"mode": "text"}, config_schema={
            "type": "object", "required": ["mode"], "additionalProperties": False,
            "properties": {"mode": {"enum": ["text", "prompt"]}},
        }, inputs=variants["text"][0], outputs=variants["text"][1],
        capabilities=("private:read", "private:write"),
        private_state_schema={"type": "object", "required": ["count"], "additionalProperties": False,
                              "properties": {"count": {"type": "integer", "minimum": 0}}},
        private_state_default={"count": 0}, port_modes=variants,
    ), execute)
