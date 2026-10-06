"""Versioned declarations for optional native Agent and Chat resource nodes."""

from __future__ import annotations

from .graph_contracts import NodeDefinition, NodePort, NodeRegistry
from .graph_agent_runtime import validate_graph_model_resource


_UUID = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}
_PRIVATE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["head_turn_id"],
    "properties": {"head_turn_id": {"anyOf": [_UUID, {"type": "null"}]}},
}


def _execute_model(config: dict, inputs: dict, context) -> dict:
    value = context.host_call("model:resolve", "resolve", {"config": config})
    return {"model": validate_graph_model_resource(value)}


def _execute_agent(config: dict, inputs: dict, context) -> dict:
    return context.host_call("agent:execute", "execute", {
        "config": config, "prompt": inputs["prompt"], "model": inputs["model"],
    })


def register_graph_agent_nodes(registry: NodeRegistry) -> None:
    """Keep the v1 unavailable placeholders unchanged for frozen definitions."""
    registry.register(NodeDefinition(
        "workflow.model-provider", "2", "Chat 模型提供", "Agent",
        {"provider_id": None, "parameters": {"model": "deepseek-flash", "max_tokens": 2048}},
        {"type": "object", "additionalProperties": False,
         "required": ["provider_id", "parameters"], "properties": {
             "provider_id": {"anyOf": [_UUID, {"type": "null"}]},
             "parameters": {"type": "object", "additionalProperties": False,
                            "required": ["model"], "properties": {
                                "model": {"type": "string", "minLength": 1, "maxLength": 256},
                                "max_tokens": {"type": "integer", "minimum": 1, "maximum": 8192},
                                "temperature": {"type": "number", "minimum": 0, "maximum": 2},
                            }},
         }},
        outputs=(NodePort("model", "MODEL_RESOURCE"),), capabilities=("model:resolve",),
    ), _execute_model)
    registry.register(NodeDefinition(
        "workflow.agent", "2", "Agent 内核", "Agent",
        {"max_model_requests": 8, "max_model_attempts": 32},
        {"type": "object", "additionalProperties": False,
         "required": ["max_model_requests", "max_model_attempts"], "properties": {
             "max_model_requests": {"type": "integer", "minimum": 1, "maximum": 64},
             "max_model_attempts": {"type": "integer", "minimum": 1, "maximum": 256},
         }},
        inputs=(NodePort("prompt", "PROMPT"), NodePort("model", "MODEL_RESOURCE")),
        outputs=(NodePort("output", "TEXT"), NodePort("output_json", "TEXT"),
                 NodePort("context_delta", "PROMPT")),
        capabilities=("agent:execute", "private:read", "private:write"),
        private_state_schema=_PRIVATE_SCHEMA, private_state_default={"head_turn_id": None},
    ), _execute_agent)
