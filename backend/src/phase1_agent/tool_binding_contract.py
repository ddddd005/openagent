"""Trusted declaration of how one capability exposes bound Agent tools.

The package owns the business semantics of its tools (names, parameter schemas,
target component identities, config and result projection).  The execution layer
owns registry resolution, ACL checks, limited contexts, invocation and
acceptance.  This module stays free of data access and of package imports so it
can be shared by the registry and the execution host without a cycle.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolBindingAdapter:
    """Business-side adapter for one load-anchor node type."""

    normalize: Callable[[dict[str, Any]], list[dict[str, Any]]]
    bind: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]
    call: Callable[[dict[str, Any], dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]
    result: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]

    def __post_init__(self) -> None:
        if not all(callable(getattr(self, name, None)) for name in (
                "normalize", "bind", "call", "result")):
            raise TypeError("A tool binding adapter requires four callable stages")


@dataclass(frozen=True)
class PreparedToolSpec:
    """One model-facing tool fixed for this invocation."""

    name: str
    operation: str
    access: str
    description: str
    parameters: dict[str, Any]
    target_component_id: str
    target_component_version: str
    config: dict[str, Any]
    binding_id: str
    agent_node_id: str
    object_key: str
    source: tuple[dict[str, Any], ...] = ()

    def adapter_spec(self) -> dict[str, Any]:
        return deepcopy({
            "name": self.name, "operation": self.operation, "access": self.access,
            "description": self.description, "parameters": self.parameters,
            "target": {"component_id": self.target_component_id,
                       "component_version": self.target_component_version},
            "config": self.config, "binding_id": self.binding_id,
            "agent_node_id": self.agent_node_id, "object_key": self.object_key,
        })

    def public(self) -> dict[str, Any]:
        return deepcopy({"binding_id": self.binding_id, "name": self.name,
                         "operation": self.operation, "description": self.description,
                         "parameters": self.parameters})


@dataclass(frozen=True)
class PreparedToolBinding:
    """One alias/object pair prepared for one Agent node."""

    binding_id: str
    anchor_node_id: str
    anchor_component_id: str
    anchor_component_version: str
    agent_node_id: str
    alias: str
    object_key: str
    operations: tuple[str, ...]
    tools: tuple[PreparedToolSpec, ...]
    anchor_source: tuple[dict[str, Any], ...] = ()

    def spec(self, operation: str) -> PreparedToolSpec | None:
        return next((tool for tool in self.tools if tool.operation == operation), None)


@dataclass(frozen=True)
class PreparedToolFrame:
    """All tool bindings prepared under one run basis."""

    agent_bindings: dict[str, tuple[PreparedToolBinding, ...]]
    descriptors: dict[str, str]
    anchors: tuple[str, ...]
    target_count: int

    def bindings_for(self, agent_node_id: str) -> tuple[PreparedToolBinding, ...]:
        return self.agent_bindings.get(agent_node_id, ())

    def find(self, agent_node_id: str, binding_id: str, operation: str):
        for binding in self.bindings_for(agent_node_id):
            if binding.binding_id == binding_id:
                return binding, binding.spec(operation)
        return None, None
