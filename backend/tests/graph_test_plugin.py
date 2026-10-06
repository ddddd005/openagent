"""An independently authored plugin fixture using only public graph interfaces."""

import copy

from phase1_agent.graph_contracts import NodeDefinition, NodePort, prompt_value, text_value


PROBE_COMPONENT = "test.content-probe"


def register_content_probe(registry):
    def execute(config, inputs, context):
        state = context.private_read()
        context.private_write({"count": state["count"] + 1})
        value = inputs["input"]
        if config["mode"] == "text":
            return {"left": text_value("left:" + value["text"]),
                    "right": text_value("right:" + value["text"])}
        left, right = copy.deepcopy(value["items"]), copy.deepcopy(value["items"])
        for prefix, items in (("left:", left), ("right:", right)):
            for item in items:
                if not item["protected"]:
                    item["text"] = prefix + item["text"]
        return {"left": prompt_value(left), "right": prompt_value(right)}

    variants = {mode: ((NodePort("input", family),),
                      (NodePort("left", family), NodePort("right", family)))
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
