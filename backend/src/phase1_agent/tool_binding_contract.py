"""Trusted declaration of how one capability exposes bound Agent tools.

The package owns the business semantics of its tools (names, parameter schemas,
target component identities, config and result projection).  The execution layer
owns registry resolution, ACL checks, limited contexts, invocation and
acceptance.  This module stays free of data access and of package imports so it
can be shared by the registry and the execution host without a cycle.
"""

from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolBindingAdapter:
    """Business-side adapter for one tool loading node type.

    A declaration-only node may omit ``prompt_output``.  An executing loading
    node must declare its single structured prompt port so the registry can
    reject arbitrary working nodes that merely claim to load tools.
    """

    normalize: Callable[[dict[str, Any]], list[dict[str, Any]]]
    bind: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]
    call: Callable[[dict[str, Any], dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]
    result: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]
    prompt_output: tuple[str, str, int] | None = None
    agent_config_field: str = "agent_node_id"

    def __post_init__(self) -> None:
        if not all(callable(getattr(self, name, None)) for name in (
                "normalize", "bind", "call", "result")):
            raise TypeError("A tool binding adapter requires four callable stages")
        if not (type(self.agent_config_field) is str and self.agent_config_field):
            raise TypeError("A tool binding adapter requires its Agent config field")
        if self.prompt_output is not None and not (
                type(self.prompt_output) is tuple and len(self.prompt_output) == 3
                and type(self.prompt_output[0]) is str and self.prompt_output[0]
                and type(self.prompt_output[1]) is str and self.prompt_output[1]
                and type(self.prompt_output[2]) is int and self.prompt_output[2] > 0):
            raise TypeError("A tool prompt output requires an exact port, type and schema version")


@dataclass(frozen=True)
class PureToolBindingAdapter:
    """A trusted package exposes synchronous tools without business state.

    ``normalize`` returns exact name/description/parameters/target/config specs.
    ``call`` projects validated model arguments into target config and inputs;
    ``result`` projects validated target outputs into a JSON object.
    """

    normalize: Callable[[dict[str, Any]], list[dict[str, Any]]]
    call: Callable[[dict[str, Any], dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]
    result: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    prompt_output: tuple[str, str, int] | None = None
    agent_config_field: str = "agent_node_id"

    def __post_init__(self) -> None:
        if not all(callable(getattr(self, name, None)) for name in (
                "normalize", "call", "result")):
            raise TypeError("A pure tool binding adapter requires three callable stages")
        if not (type(self.agent_config_field) is str and self.agent_config_field):
            raise TypeError("A tool binding adapter requires its Agent config field")
        if self.prompt_output is not None and not (
                type(self.prompt_output) is tuple and len(self.prompt_output) == 3
                and type(self.prompt_output[0]) is str and self.prompt_output[0]
                and type(self.prompt_output[1]) is str and self.prompt_output[1]
                and type(self.prompt_output[2]) is int and self.prompt_output[2] > 0):
            raise TypeError("A tool prompt output requires an exact port, type and schema version")


@dataclass(frozen=True)
class WorkspaceToolBindingAdapter:
    """A trusted package keeps one running workspace for one invocation.

    ``normalize`` returns exact name/description/parameters/config specs for the
    tools sharing the instance.  ``open`` creates or attaches the running
    instance and returns an opaque handle (any Python object; the host neither
    inspects nor persists it).  ``invoke`` serves one model call against that
    handle, ``finish`` lets the package save and export its final text, and
    ``close``/``abort`` release the resources after (or instead of) a normal
    finish.  Only synchronous stages are supported in this first batch.
    """

    normalize: Callable[[dict[str, Any]], list[dict[str, Any]]]
    open: Callable[[dict[str, Any], dict[str, Any]], Any]
    invoke: Callable[[Any, dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]
    finish: Callable[[Any, dict[str, Any]], dict[str, Any] | None]
    close: Callable[[Any], None]
    abort: Callable[[Any], None]
    prompt_output: tuple[str, str, int] | None = None
    agent_config_field: str = "agent_node_id"
    data_config_field: str | None = None

    def __post_init__(self) -> None:
        stages = ("normalize", "open", "invoke", "finish", "close", "abort")
        if not all(callable(getattr(self, name, None)) for name in stages):
            raise TypeError("A workspace tool binding adapter requires six callable stages")
        if not (type(self.agent_config_field) is str and self.agent_config_field):
            raise TypeError("A tool binding adapter requires its Agent config field")
        if self.data_config_field is not None and not (
                type(self.data_config_field) is str and self.data_config_field):
            raise TypeError("A workspace data config field must be a nonempty string")
        if self.prompt_output is not None and not (
                type(self.prompt_output) is tuple and len(self.prompt_output) == 3
                and type(self.prompt_output[0]) is str and self.prompt_output[0]
                and type(self.prompt_output[1]) is str and self.prompt_output[1]
                and type(self.prompt_output[2]) is int and self.prompt_output[2] > 0):
            raise TypeError("A tool prompt output requires an exact port, type and schema version")
        for name in stages:
            stage = getattr(self, name)
            forms = (stage, getattr(type(stage), "__call__", None))
            if any(check(form) for form in forms if form is not None for check in (
                    inspect.iscoroutinefunction, inspect.isgeneratorfunction,
                    inspect.isasyncgenfunction)):
                raise TypeError("A workspace tool binding adapter requires synchronous stages")


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
class PreparedPureToolSpec:
    """An exact computation target with no object, access or variable operation."""

    name: str
    description: str
    parameters: dict[str, Any]
    target_component_id: str
    target_component_version: str
    config: dict[str, Any]
    binding_id: str
    agent_node_id: str
    source: tuple[dict[str, Any], ...] = ()

    @property
    def operation(self) -> str:
        return "invoke"

    def adapter_spec(self) -> dict[str, Any]:
        return deepcopy({
            "name": self.name, "description": self.description,
            "parameters": self.parameters,
            "target": {"component_id": self.target_component_id,
                       "component_version": self.target_component_version},
            "config": self.config, "binding_id": self.binding_id,
            "agent_node_id": self.agent_node_id,
        })

    def public(self) -> dict[str, Any]:
        return deepcopy({"binding_id": self.binding_id, "name": self.name,
                         "operation": self.operation, "description": self.description,
                         "parameters": self.parameters})


@dataclass(frozen=True)
class PreparedPureToolBinding:
    binding_id: str
    anchor_node_id: str
    anchor_component_id: str
    anchor_component_version: str
    agent_node_id: str
    tools: tuple[PreparedPureToolSpec, ...]
    anchor_source: tuple[dict[str, Any], ...] = ()

    def spec(self, operation: str) -> PreparedPureToolSpec | None:
        return next((tool for tool in self.tools if tool.operation == operation), None)


@dataclass(frozen=True)
class PreparedWorkspaceToolSpec:
    """One model-facing tool served by this invocation's workspace instance."""

    name: str
    description: str
    parameters: dict[str, Any]
    config: dict[str, Any]
    binding_id: str
    agent_node_id: str
    source: tuple[dict[str, Any], ...] = ()

    @property
    def operation(self) -> str:
        return "invoke"

    def adapter_spec(self) -> dict[str, Any]:
        return deepcopy({
            "name": self.name, "description": self.description,
            "parameters": self.parameters, "config": self.config,
            "binding_id": self.binding_id, "agent_node_id": self.agent_node_id,
        })

    def public(self) -> dict[str, Any]:
        return deepcopy({"binding_id": self.binding_id, "name": self.name,
                         "operation": self.operation, "description": self.description,
                         "parameters": self.parameters})


@dataclass(frozen=True)
class PreparedWorkspaceToolBinding:
    """One prepared workspace instance and the tools it serves for one Agent.

    ``data_objects`` freezes the declared session object keys (empty when the
    adapter declares no data capability); ``writable`` freezes the subset the
    Agent may save at finish, derived from the document's object bindings.
    """

    binding_id: str
    anchor_node_id: str
    anchor_component_id: str
    anchor_component_version: str
    agent_node_id: str
    anchor_config: dict[str, Any]
    tools: tuple[PreparedWorkspaceToolSpec, ...]
    anchor_source: tuple[dict[str, Any], ...] = ()
    data_objects: tuple[str, ...] = ()
    writable: tuple[str, ...] = ()

    def spec(self, operation: str) -> PreparedWorkspaceToolSpec | None:
        return next((tool for tool in self.tools if tool.operation == operation), None)


PreparedBinding = PreparedToolBinding | PreparedPureToolBinding | PreparedWorkspaceToolBinding


@dataclass(frozen=True)
class PreparedToolFrame:
    """All tool bindings prepared under one run basis."""

    agent_bindings: dict[str, tuple[PreparedBinding, ...]]
    descriptors: dict[str, str]
    anchors: tuple[str, ...]
    target_count: int

    def bindings_for(self, agent_node_id: str) -> tuple[PreparedBinding, ...]:
        return self.agent_bindings.get(agent_node_id, ())

    def find(self, agent_node_id: str, binding_id: str, operation: str):
        for binding in self.bindings_for(agent_node_id):
            if binding.binding_id == binding_id:
                return binding, binding.spec(operation)
        return None, None
