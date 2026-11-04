"""Trusted node declarations and the versioned, named-port graph boundary."""

from __future__ import annotations

import copy
import heapq
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .contract_errors import (
    ContractValidationError, DiagnosticError as GraphDiagnosticError, require,
    uuid4_string, uuid_value,
)
from .contract_json import canonical_bytes, validate_json_value
from .host_sdk import (
    DataTypeDefinition, HostContractError, HostServiceRegistry, InformationSourceRegistry,
    ObjectBinding, TypeRegistry, bounded_name,
)
from .runtime_executor_contracts import ExecutorReference, ExecutorRegistry
from .tool_binding_contract import (
    PureToolBindingAdapter, ToolBindingAdapter, WorkspaceToolBindingAdapter,
)
from .variable_contract import VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE, empty_variable_value
from .session_preloads import validate_session_preloads


MAX_GRAPH_NODES = 512
MAX_GRAPH_EDGES = 4096
MAX_CONTENT_CHARS = 1_000_000
MAX_GRAPH_BYTES = 4_000_000
SUPPORTED_GRAPH_VERSIONS = (1, 2, 3)
DOCUMENT_CONTRACT_VERSION = 2


def normalize_checkpoint_save(config_schema: dict, declaration: dict) -> dict:
    """Validate and canonically order the registered configuration paths."""
    require(type(config_schema) is dict, "graph_invalid_checkpoint_save",
            "Checkpoint-save config schema must be an object")
    require(type(declaration) is dict and set(declaration) == {"config_paths"},
            "graph_invalid_checkpoint_save", "Checkpoint-save declaration fields differ")
    paths = declaration["config_paths"]
    require(type(paths) is list, "graph_invalid_checkpoint_save",
            "Checkpoint config paths must be an array")

    normalized = []
    for path in paths:
        require(type(path) is list and bool(path)
                and all(type(segment) is str and bool(segment) for segment in path),
                "graph_invalid_checkpoint_save",
                "Checkpoint paths must be nonempty arrays of property names")
        schema = config_schema
        for segment in path:
            properties = schema.get("properties") if type(schema) is dict else None
            require(type(properties) is dict and segment in properties,
                    "graph_invalid_checkpoint_save",
                    "Checkpoint path does not follow declared config schema properties")
            schema = properties[segment]
        normalized.append(tuple(path))

    normalized.sort()
    seen = set()
    for path in normalized:
        require(path not in seen
                and all(path[:length] not in seen for length in range(1, len(path))),
                "graph_invalid_checkpoint_save",
                "Checkpoint config paths must not repeat or overlap")
        seen.add(path)
    return {"config_paths": [list(path) for path in normalized]}


def _validate_event_schema_references(schema: dict) -> None:
    """Event payload validation resolves only references within its saved schema."""
    pending = [schema]
    maps = ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas")
    singles = ("additionalProperties", "unevaluatedProperties", "propertyNames", "contains",
               "items", "additionalItems", "unevaluatedItems", "not", "if", "then", "else")
    arrays = ("allOf", "anyOf", "oneOf", "prefixItems")
    while pending:
        current = pending.pop()
        if type(current) is not dict:
            continue
        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            if keyword in current:
                require(type(current[keyword]) is str and current[keyword].startswith("#"),
                        "graph_invalid_event_binding", "Event schema references must be local fragments")
        for keyword in maps:
            if type(current.get(keyword)) is dict:
                pending.extend(current[keyword].values())
        pending.extend(current[keyword] for keyword in singles if keyword in current)
        for keyword in arrays:
            if type(current.get(keyword)) is list:
                pending.extend(current[keyword])
        # Older schema keywords may still be recognized by a declared dialect.
        if type(current.get("items")) is list:
            pending.extend(current["items"])
        if type(current.get("dependencies")) is dict:
            pending.extend(item for item in current["dependencies"].values() if type(item) is dict)


def validate_graph_document(value: Any, *, phase: str = "save") -> dict:
    """Validate storage structure without requiring dormant nodes to be runnable.

    Structure is checked here; edge endpoints must name existing nodes (Q05-1)
    while ports/types and full execution prerequisites stay compile diagnostics.
    Every structure failure carries ``phase`` and, where the check names one
    exact field, a JSON-Pointer ``path`` plus the bounded identity involved.
    """
    try:
        validate_json_value(value)
    except ContractValidationError as exc:
        raise GraphDiagnosticError("graph_invalid_document", str(exc),
                                   phase=phase, path="") from exc
    base_fields = {"schema_version", "workflow_definition_id", "revision", "name", "nodes", "edges"}
    require(type(value) is dict and type(value.get("schema_version")) is int
            and value["schema_version"] in SUPPORTED_GRAPH_VERSIONS,
            "graph_invalid_document", "Unknown graph document version",
            phase=phase, path="/schema_version",
            expected=list(SUPPORTED_GRAPH_VERSIONS), actual=value.get("schema_version"))
    require(set(value) == base_fields if value["schema_version"] == 1 else
            base_fields | {"object_bindings"} <= set(value) <= base_fields | {
                "object_bindings", "package_lock", "control_edges", "event_bindings"}
            | ({"edit_sequence", "session_preloads"} if value["schema_version"] == 3 else set()),
            "graph_invalid_document", "Graph document fields are invalid",
            phase=phase, path="")
    require(uuid_value(value["workflow_definition_id"]), "graph_invalid_document",
            "Graph identities must be canonical UUID4", phase=phase,
            path="/workflow_definition_id")
    require(type(value["revision"]) is int and 1 <= value["revision"] <= 2**53 - 1,
            "graph_invalid_document", "Graph revision must be a positive integer",
            phase=phase, path="/revision")
    if "edit_sequence" in value:
        require(type(value["edit_sequence"]) is int and 0 <= value["edit_sequence"] <= 2**53 - 1,
                "graph_invalid_document", "Graph edit sequence must be a nonnegative integer",
                phase=phase, path="/edit_sequence")
    require(type(value["name"]) is str and 0 < len(value["name"].strip()) <= 128,
            "graph_invalid_document", "Graph name must be nonempty text",
            phase=phase, path="/name")
    require(type(value["nodes"]) is list and len(value["nodes"]) <= MAX_GRAPH_NODES,
            "graph_invalid_document", "Graph node array exceeds its limit",
            phase=phase, path="/nodes", expected={"max_items": MAX_GRAPH_NODES})
    node_ids: set[str] = set()
    node_indexes: dict[str, int] = {}
    for index, node in enumerate(value["nodes"]):
        path = f"/nodes/{index}"
        policy_fields = {"configuration_policy"} if value["schema_version"] == 3 else set()
        require(type(node) is dict and {
            "node_binding_id", "component_id", "component_version", "title", "position", "config",
        } | policy_fields <= set(node) <= {
            "node_binding_id", "component_id", "component_version", "title", "position", "config",
            "public_outputs",
        } | policy_fields, "graph_invalid_document", "Graph node fields are invalid",
                phase=phase, path=path)
        if policy_fields:
            from .workflow_configuration import validate_configuration_policy
            validate_configuration_policy(node["configuration_policy"])
        require(uuid_value(node["node_binding_id"]), "graph_invalid_document",
                "Graph identities must be canonical UUID4",
                phase=phase, path=f"{path}/node_binding_id")
        identity = node["node_binding_id"]
        require(identity not in node_ids, "graph_invalid_document",
                "Duplicate node identity", phase=phase,
                path=f"{path}/node_binding_id", node_id=identity)
        node_ids.add(identity)
        node_indexes[identity] = index
        require(all(type(node[key]) is str and 0 < len(node[key]) <= 128
                    for key in ("component_id", "component_version"))
                and type(node["title"]) is str and len(node["title"]) <= 128
                and type(node["config"]) is dict,
                "graph_invalid_document", "Node type, title or config is invalid",
                phase=phase, path=path, node_id=identity)
        position = node["position"]
        require(type(position) is dict and set(position) == {"x", "y"}
                and all(type(coordinate) in (int, float) for coordinate in position.values()),
                "graph_invalid_document", "Node position is invalid",
                phase=phase, path=f"{path}/position", node_id=identity)
        public = node.get("public_outputs", [])
        require(type(public) is list and len(public) <= 64
                and all(type(port) is str and bool(port) for port in public)
                and len(public) == len(set(public)),
                "graph_invalid_document", "Public output names are invalid",
                phase=phase, path=f"{path}/public_outputs", node_id=identity)
    require(type(value["edges"]) is list and len(value["edges"]) <= MAX_GRAPH_EDGES,
            "graph_invalid_document", "Graph edge array exceeds its limit",
            phase=phase, path="/edges", expected={"max_items": MAX_GRAPH_EDGES})
    edge_ids: set[str] = set()
    for index, edge in enumerate(value["edges"]):
        path = f"/edges/{index}"
        require(type(edge) is dict and set(edge) == {
            "edge_id", "source_node_id", "source_port_id", "target_node_id", "target_port_id", "order",
        }, "graph_invalid_document", "Graph edge fields are invalid", phase=phase, path=path)
        require(uuid_value(edge["edge_id"]), "graph_invalid_document",
                "Graph identities must be canonical UUID4", phase=phase, path=f"{path}/edge_id")
        identity = edge["edge_id"]
        require(identity not in edge_ids, "graph_invalid_document", "Duplicate edge identity",
                phase=phase, path=f"{path}/edge_id", edge_id=identity)
        edge_ids.add(identity)
        for field_name in ("source_node_id", "target_node_id"):
            require(uuid_value(edge[field_name]), "graph_invalid_document",
                    "Graph identities must be canonical UUID4",
                    phase=phase, path=f"{path}/{field_name}", edge_id=identity)
            require(edge[field_name] in node_ids, "graph_missing_edge_node",
                    "Edge endpoint names a missing node", phase=phase,
                    path=f"{path}/{field_name}", edge_id=identity,
                    node_id=edge[field_name], expected={"node_id_exists": True},
                    actual={"node_id_exists": False, "node_id": edge[field_name]})
        require(all(type(edge[key]) is str and 0 < len(edge[key]) <= 128
                    for key in ("source_port_id", "target_port_id"))
                and type(edge["order"]) is int and 0 <= edge["order"] <= 2**53 - 1,
                "graph_invalid_document", "Graph edge ports or order are invalid",
                phase=phase, path=path, edge_id=identity)
    if value["schema_version"] >= 2:
        require(type(value["object_bindings"]) is list and len(value["object_bindings"]) <= 1024,
                "graph_invalid_document", "Object binding array exceeds its limit",
                phase=phase, path="/object_bindings")
        object_keys: set[str] = set()
        for index, raw_binding in enumerate(value["object_bindings"]):
            path = f"/object_bindings/{index}"
            try:
                binding = ObjectBinding.from_dict(raw_binding)
            except HostContractError as exc:
                raise GraphDiagnosticError(exc.reason_code, str(exc),
                                           phase=phase, path=path) from exc
            require(binding.object_key not in object_keys, "graph_invalid_document",
                    "Object key is duplicated", phase=phase, path=f"{path}/object_key")
            object_keys.add(binding.object_key)
            authorized_nodes = set(binding.readers) | set(binding.writers)
            if binding.owner_node_id is not None:
                authorized_nodes.add(binding.owner_node_id)
            if binding.registrar_node_id is not None:
                authorized_nodes.add(binding.registrar_node_id)
            missing_nodes = sorted(authorized_nodes - node_ids)
            require(not missing_nodes, "graph_invalid_document",
                    "Object binding names a missing owner, registrar or authorized node",
                    phase=phase, path=path, missing_node_ids=missing_nodes)
        validate_package_lock(value.get("package_lock", []))
        events = value.get("event_bindings", [])
        require(type(events) is list and len(events) <= MAX_GRAPH_NODES,
                "graph_invalid_event_binding", "Event binding array exceeds its limit",
                phase=phase, path="/event_bindings")
        event_identities: set[tuple[str, int]] = set()
        for index, event in enumerate(events):
            path = f"/event_bindings/{index}"
            require(type(event) is dict and set(event) == {
                "event_id", "schema_version", "display_name", "audience", "target_node_ids", "payload_schema",
            }, "graph_invalid_event_binding", "Event binding fields differ",
                phase=phase, path=path)
            require(bounded_name(event["event_id"]) and type(event["schema_version"]) is int
                    and 1 <= event["schema_version"] <= 2**53 - 1
                    and type(event["display_name"]) is str and bool(event["display_name"].strip())
                    and len(event["display_name"]) <= 128
                    and event["audience"] in ("management", "consumer"),
                    "graph_invalid_event_binding",
                    "Event identity, display name or audience is invalid",
                    phase=phase, path=path)
            event_identity = event["event_id"], event["schema_version"]
            require(event_identity not in event_identities, "graph_invalid_event_binding",
                    "Event identity and schema version are duplicated",
                    phase=phase, path=path)
            event_identities.add(event_identity)
            targets = event["target_node_ids"]
            require(type(targets) is list and 0 < len(targets) <= MAX_GRAPH_NODES
                    and all(type(target) is str for target in targets)
                    and len(set(targets)) == len(targets), "graph_invalid_event_binding",
                    "Event targets must be distinct node identities",
                    phase=phase, path=f"{path}/target_node_ids")
            missing_targets = sorted(target for target in targets if target not in node_ids)
            require(not missing_targets, "graph_invalid_event_binding",
                    "Event targets must name existing nodes",
                    phase=phase, path=f"{path}/target_node_ids",
                    expected={"node_id_exists": True},
                    actual={"missing_node_ids": missing_targets})
            schema = event["payload_schema"]
            require(type(schema) is dict and schema.get("type") == "object",
                    "graph_invalid_event_binding",
                    "Event payload schema must declare a JSON object",
                    phase=phase, path=f"{path}/payload_schema")
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise GraphDiagnosticError("graph_invalid_event_binding",
                                           "Event payload schema is invalid",
                                           phase=phase, path=f"{path}/payload_schema") from exc
            _validate_event_schema_references(schema)
        controls = value.get("control_edges", [])
        require(type(controls) is list and len(controls) <= MAX_GRAPH_EDGES,
                "graph_invalid_control_edge", "Control dependency array exceeds its limit",
                phase=phase, path="/control_edges")
        for index, edge in enumerate(controls):
            path = f"/control_edges/{index}"
            require(type(edge) is dict and set(edge) == {"edge_id", "source_node_id", "target_node_id"},
                    "graph_invalid_control_edge",
                    "Control dependencies contain only node identities",
                    phase=phase, path=path)
            for field_name in ("edge_id", "source_node_id", "target_node_id"):
                require(uuid_value(edge[field_name]), "graph_invalid_control_edge",
                        "Control dependency identities must be canonical UUID4",
                        phase=phase, path=f"{path}/{field_name}")
            identity = edge["edge_id"]
            require(identity not in edge_ids, "graph_invalid_control_edge",
                    "Control dependency identity is duplicated", phase=phase,
                    path=f"{path}/edge_id", edge_id=identity)
            edge_ids.add(identity)
            for field_name in ("source_node_id", "target_node_id"):
                require(edge[field_name] in node_ids, "graph_invalid_control_edge",
                        "Control dependency names a missing node", phase=phase,
                        path=f"{path}/{field_name}", edge_id=identity,
                        node_id=edge[field_name], expected={"node_id_exists": True},
                        actual={"node_id_exists": False, "node_id": edge[field_name]})
    validate_session_preloads(value)
    require(len(canonical_bytes(value)) <= MAX_GRAPH_BYTES,
            "graph_invalid_document", "Graph document exceeds its wire limit",
            phase=phase, path="", expected={"max_bytes": MAX_GRAPH_BYTES},
            actual={"bytes": len(canonical_bytes(value))})
    return copy.deepcopy(value)


RETIRED_DOCUMENT_FIELDS = ("execution_roots",)


def drop_retired_document_fields(value: Any) -> Any:
    """Remove document fields retired from the contract before re-validating stored evidence."""
    if type(value) is not dict:
        return value
    return {key: item for key, item in value.items() if key not in RETIRED_DOCUMENT_FIELDS}


def validate_package_lock(value: Any) -> list[dict]:
    require(type(value) is list and len(value) <= 256, "graph_invalid_package_lock", "Package lock is invalid")
    identities: set[str] = set()
    for item in value:
        require(type(item) is dict and set(item) == {"package_id", "version"}
                and bounded_name(item["package_id"]) and bounded_name(item["version"])
                and item["package_id"] not in identities,
                "graph_invalid_package_lock", "Package lock requires unique exact package versions")
        identities.add(item["package_id"])
    return copy.deepcopy(value)


def text_value(text: str) -> dict:
    return validate_content_value({"schema_version": 1, "kind": "workflow.text", "text": text}, "TEXT")


def prompt_value(items: list[dict]) -> dict:
    return validate_content_value({
        "schema_version": 1, "kind": "workflow.prompt", "stage": "materials",
        "items": items, "assembly": None,
    }, "PROMPT")


def json_value(value: Any) -> dict:
    return validate_content_value({"schema_version": 1, "kind": "workflow.json", "value": value}, "JSON")


def validate_content_value(value: Any, data_type: str, schema_version: int = 1,
                           *, registry: TypeRegistry | None = None) -> dict:
    """Standalone content helpers use built-ins; runtime uses its frozen registry."""
    if registry is not None:
        try:
            return registry.validate(data_type, schema_version, value, scope="content")
        except HostContractError as exc:
            raise GraphDiagnosticError(exc.reason_code, str(exc)) from exc
    require(schema_version == 1, "graph_unknown_data_type", "Content schema is not registered")
    return _validate_builtin_content_value(value, data_type)


def _validate_builtin_content_value(value: Any, data_type: str) -> dict:
    """Content families are independent of the semantic purpose of a prompt item."""
    validate_json_value(value)
    require(type(value) is dict and type(value.get("schema_version")) is int
            and value["schema_version"] == 1,
            "graph_invalid_output", "Content must have an explicit versioned envelope")
    if data_type == "TEXT":
        require(set(value) == {"schema_version", "kind", "text"}
                and value["kind"] == "workflow.text" and type(value["text"]) is str
                and len(value["text"]) <= MAX_CONTENT_CHARS,
                "graph_invalid_output", "TEXT output must contain bounded text")
    elif data_type == "JSON":
        require(set(value) == {"schema_version", "kind", "value"}
                and value["kind"] == "workflow.json" and len(canonical_bytes(value)) <= MAX_CONTENT_CHARS,
                "graph_invalid_output", "JSON management output is invalid")
    elif data_type == "PROMPT":
        require(set(value) == {"schema_version", "kind", "stage", "items", "assembly"}
                and value["kind"] == "workflow.prompt"
                and value["stage"] in ("materials", "assembled")
                and (value["stage"] == "assembled" or value["assembly"] is None)
                and type(value["items"]) is list and len(value["items"]) <= 1024,
                "graph_invalid_output", "PROMPT requires ordered materials and explicit assembly state")
        identities: set[str] = set()
        for item in value["items"]:
            require(type(item) is dict and set(item) == {
                "item_instance_id", "text", "role", "placement", "depth", "order", "enabled",
                "purpose", "source", "protected", "metadata",
            }, "graph_invalid_output", "PROMPT item fields are invalid")
            identity = uuid4_string(item["item_instance_id"])
            require(identity not in identities, "graph_duplicate_material", "Repeated material instance")
            identities.add(identity)
            require(type(item["text"]) is str and item["role"] in ("system", "user", "assistant", "tool")
                    and item["placement"] in ("before", "middle", "after")
                    and type(item["order"]) is int and type(item["enabled"]) is bool
                    and type(item["protected"]) is bool
                    and type(item["purpose"]) is str and 0 < len(item["purpose"]) <= 128
                    and type(item["source"]) is dict and bool(item["source"])
                    and type(item["metadata"]) is dict,
                    "graph_invalid_output", "PROMPT metadata or presentation is invalid")
            require(type(item["depth"]) is int and item["depth"] >= 0
                    if item["placement"] == "middle" else item["depth"] is None,
                    "graph_invalid_output", "PROMPT depth is invalid")
            require(item["role"] != "tool" or item["protected"],
                    "graph_invalid_output", "Tool protocol facts must remain protected")
        require(sum(len(item["text"]) for item in value["items"]) <= MAX_CONTENT_CHARS
                and len(canonical_bytes(value)) <= MAX_GRAPH_BYTES,
                "graph_invalid_output", "PROMPT output exceeds its resource limit")
        if value["stage"] == "assembled":
            raise GraphDiagnosticError("graph_legacy_prompt_retired", "PROMPT@1 assembly is no longer supported")
    else:
        raise GraphDiagnosticError("graph_unknown_data_type", "Data type is not registered")
    return copy.deepcopy(value)


@dataclass(frozen=True)
class NodePort:
    port_id: str
    data_type: str
    required: bool = True
    multiple: bool = False
    data_schema_version: int = 1
    accepted_types: tuple[tuple[str, int], ...] = ()

    def accepts(self, output_port: "NodePort") -> bool:
        identity = (output_port.data_type, output_port.data_schema_version)
        return identity == (self.data_type, self.data_schema_version) or identity in self.accepted_types

    def to_dict(self) -> dict:
        value = {"port_id": self.port_id, "data_type": self.data_type,
                 "required": self.required, "multiple": self.multiple}
        if self.data_schema_version != 1 or self.data_type not in {"TEXT", "PROMPT", "JSON"}:
            value["data_schema_version"] = self.data_schema_version
        if self.accepted_types:
            value["accepted_types"] = [{"data_type": data_type, "data_schema_version": version}
                                       for data_type, version in self.accepted_types]
        return value


@dataclass(frozen=True)
class NodeDefinition:
    component_id: str
    component_version: str
    display_name: str
    category: str
    default_config: dict
    config_schema: dict
    inputs: tuple[NodePort, ...] = ()
    outputs: tuple[NodePort, ...] = ()
    is_output: bool = False
    capabilities: tuple[str, ...] = ()
    private_state_schema: dict = field(default_factory=lambda: {"type": "object", "additionalProperties": False})
    private_state_default: Any = field(default_factory=dict)
    port_modes: dict[str, tuple[tuple[NodePort, ...], tuple[NodePort, ...]]] = field(default_factory=dict)
    input_storage: str = "inline"
    object_accesses: tuple[dict, ...] = ()
    service_requirements: tuple[dict, ...] = ()
    config_node_references: tuple[dict, ...] = ()
    adjustable_configuration: dict | None = None
    checkpoint_save: dict = field(default_factory=lambda: {"config_paths": []})
    execution_class: str = "working"
    provided_session_item_types: tuple[dict, ...] = ()

    def ports(self, config: dict) -> tuple[tuple[NodePort, ...], tuple[NodePort, ...]]:
        if self.port_modes:
            mode = config.get("mode")
            require(type(mode) is str and mode in self.port_modes,
                    "graph_invalid_config", "Node content mode is invalid")
            return self.port_modes[mode]
        return self.inputs, self.outputs

    def to_dict(self, *, executable: bool = True) -> dict:
        from .workflow_configuration import default_configuration_policy

        value = {key: copy.deepcopy(getattr(self, key)) for key in (
            "component_id", "component_version", "display_name", "category", "default_config",
            "config_schema", "is_output", "private_state_schema", "private_state_default",
            "execution_class",
        )}
        value.update({"inputs": [port.to_dict() for port in self.inputs],
                      "outputs": [port.to_dict() for port in self.outputs],
                      "capabilities": list(self.capabilities), "executable": executable})
        value["checkpoint_save"] = normalize_checkpoint_save(self.config_schema, self.checkpoint_save)
        value["default_configuration_policy"] = default_configuration_policy(
            self.component_id, self.component_version, self.adjustable_configuration)
        if self.port_modes:
            value["port_modes"] = {
                mode: {"inputs": [port.to_dict() for port in ports[0]],
                       "outputs": [port.to_dict() for port in ports[1]]}
                for mode, ports in self.port_modes.items()
            }
        if self.input_storage != "inline":
            value["input_storage"] = self.input_storage
        if self.object_accesses:
            value["object_accesses"] = copy.deepcopy(list(self.object_accesses))
        if self.service_requirements:
            value["service_requirements"] = copy.deepcopy(list(self.service_requirements))
        if self.config_node_references:
            value["config_node_references"] = copy.deepcopy(list(self.config_node_references))
        if self.adjustable_configuration is not None:
            value["adjustable_configuration"] = copy.deepcopy(self.adjustable_configuration)
        if self.provided_session_item_types:
            value["provided_session_item_types"] = copy.deepcopy(list(self.provided_session_item_types))
        return value


def normalize_provided_session_item_types(value: Any, data_types: TypeRegistry) -> tuple[dict, ...]:
    require(type(value) is tuple and len(value) <= 128,
            "graph_invalid_session_item_declaration",
            "Provided session item declarations must be a bounded tuple")
    normalized = []
    identities = set()
    for item in value:
        require(type(item) is dict and set(item) == {"type_id", "schema_version"}
                and bounded_name(item["type_id"]) and type(item["schema_version"]) is int
                and 1 <= item["schema_version"] <= 2**53 - 1,
                "graph_invalid_session_item_declaration",
                "Provided session item declarations require exact type identities")
        identity = item["type_id"], item["schema_version"]
        require(identity not in identities,
                "graph_invalid_session_item_declaration",
                "Provided session item type is duplicated")
        require(data_types.get(*identity, scope="session") is not None,
                "graph_missing_session_item_type",
                "Provided session item type is not registered in the session scope")
        identities.add(identity)
        normalized.append({"type_id": item["type_id"], "schema_version": item["schema_version"]})
    return tuple(normalized)


@dataclass(frozen=True)
class RegisteredNode:
    definition: NodeDefinition
    executor: Callable | None
    config_validator: Callable[[dict], None] | None = None
    state_migrator: Callable[[Any, NodeDefinition], Any] | None = None
    external_inputs_validator: Callable[[dict, dict], None] | None = None
    external_inputs_declaration: Callable[[dict], list[dict]] | None = None
    resource_dependencies_declaration: Callable[[dict], list[dict]] | None = None
    resource_preflight_validator: Callable[[dict, list[dict]], None] | None = None
    resource_input_ports: tuple[str, ...] = ()
    executor_ref: ExecutorReference | None = None
    settlement_builder: Callable | None = None
    failed_retry_validator: Callable[[dict, dict], dict] | None = None
    tool_binding: ToolBindingAdapter | PureToolBindingAdapter | WorkspaceToolBindingAdapter | None = None


class NodeRegistry:
    """Only trusted application/plugin code may associate a declaration with code."""

    def __init__(self, data_types: TypeRegistry | None = None) -> None:
        self._nodes: dict[tuple[str, str], RegisteredNode] = {}
        self._trusted_scopes: dict[tuple[str, str], str] = {}
        self.executors = ExecutorRegistry()
        self.services = HostServiceRegistry()
        self.information_sources = InformationSourceRegistry()
        self.data_types = data_types if data_types is not None else TypeRegistry()
        self.package_lock: tuple[dict, ...] = ()
        self.execution_package_lock: tuple[dict, ...] = ()
        self._frozen = False
        for data_type in ("TEXT", "PROMPT", "JSON"):
            if self.data_types.get(data_type, 1, scope="content") is None:
                self.data_types.register(DataTypeDefinition(
                    data_type, 1, {}, scope="content", max_bytes=MAX_GRAPH_BYTES,
                    validator=lambda value, identity=data_type: _validate_builtin_content_value(value, identity),
                ))

    def register(self, definition: NodeDefinition, executor: Callable | None,
                 config_validator: Callable[[dict], None] | None = None,
                 state_migrator: Callable[[Any, NodeDefinition], Any] | None = None,
                 external_inputs_validator: Callable[[dict, dict], None] | None = None,
                 external_inputs_declaration: Callable[[dict], list[dict]] | None = None,
                 resource_dependencies_declaration: Callable[[dict], list[dict]] | None = None,
                 resource_preflight_validator: Callable[[dict, list[dict]], None] | None = None,
                 resource_input_ports: tuple[str, ...] = (),
                 executor_ref: ExecutorReference | None = None,
                 settlement_builder: Callable | None = None,
                 failed_retry_validator: Callable[[dict, dict], dict] | None = None,
                 tool_binding: ToolBindingAdapter | PureToolBindingAdapter
                 | WorkspaceToolBindingAdapter | None = None,
                 *, trusted_scope: str = "host") -> None:
        require(isinstance(definition, NodeDefinition), "graph_invalid_type",
                "Node registration requires a node definition")
        require(type(trusted_scope) is str and bounded_name(trusted_scope),
                "graph_invalid_registration_scope", "Node registration requires a trusted scope")
        key = definition.component_id, definition.component_version
        require(not self._frozen, "host_registry_frozen", "The active node registry is frozen")
        require(key not in self._nodes, "graph_duplicate_type", "Node implementation is already registered")
        require(type(definition.execution_class) is str
                and definition.execution_class in {"working", "non_working"},
                "graph_invalid_execution_class", "Node execution class must be working or non_working")
        require(executor is None or callable(executor), "graph_invalid_type", "Node executor must be callable")
        require(executor_ref is None or isinstance(executor_ref, ExecutorReference),
                "runtime_invalid_executor", "Hosted node requires an exact executor reference")
        if executor_ref is not None:
            require(executor is None, "runtime_executor_conflict", "Node cannot declare two execution paths")
            require(self.executors.get(executor_ref) is not None,
                    "runtime_missing_executor", "Hosted node requires its exact executor registration")
        require(definition.input_storage in ("inline", "references"),
                "graph_invalid_type", "Unknown input storage policy")
        from .workflow_configuration import validate_adjustable_configuration
        validate_adjustable_configuration(definition.component_id, definition.component_version,
                                          definition.adjustable_configuration)
        self.services.grants(definition.service_requirements, definition.capabilities)
        require(settlement_builder is None or callable(settlement_builder), "graph_invalid_type",
                "Settlement builder must be callable")
        require(failed_retry_validator is None or callable(failed_retry_validator), "graph_invalid_type",
                "Failed retry validator must be callable")
        require(tool_binding is None or isinstance(
                    tool_binding,
                    (ToolBindingAdapter, PureToolBindingAdapter, WorkspaceToolBindingAdapter)),
                "graph_invalid_type",
                "Tool binding declaration must be a trusted adapter")
        declared_fields = set()
        for reference in definition.config_node_references:
            require(type(reference) is dict
                    and {"config_field", "multiple", "target_types"} <= set(reference)
                    and set(reference) <= {"config_field", "multiple", "target_types", "allow_empty"}
                    and bounded_name(reference["config_field"]) and type(reference["multiple"]) is bool
                    and ("allow_empty" not in reference or type(reference["allow_empty"]) is bool)
                    and reference["config_field"] not in declared_fields
                    and type(reference["target_types"]) is list and 0 < len(reference["target_types"]) <= 256
                    and all(type(target) is dict and set(target) == {"component_id", "component_version"}
                            and bounded_name(target["component_id"]) and bounded_name(target["component_version"])
                            for target in reference["target_types"]),
                    "graph_invalid_type", "Config node reference declarations are invalid")
            declared_fields.add(reference["config_field"])
        if tool_binding is not None:
            require(any(reference["config_field"] == tool_binding.agent_config_field
                        and reference["multiple"] is False
                        for reference in definition.config_node_references),
                    "graph_invalid_type",
                    "A tool loader requires one exact Agent configuration reference")
            if executor is None and executor_ref is None:
                require(definition.execution_class == "non_working"
                        and not definition.inputs and not definition.outputs
                        and not definition.port_modes
                        and not definition.service_requirements
                        and not definition.object_accesses,
                        "graph_invalid_type",
                        "A tool binding anchor is a non-working declaration without ports or state")
            else:
                require(definition.execution_class == "working" and executor is not None
                        and executor_ref is None,
                        "graph_invalid_type",
                        "An executing tool loader requires its native executor")
                require(tool_binding.prompt_output is not None, "graph_invalid_type",
                        "An executing tool loader requires one structured prompt output")
                port_id, data_type, schema_version = tool_binding.prompt_output
                require(definition.outputs == (NodePort(
                            port_id, data_type, data_schema_version=schema_version),)
                        and not definition.inputs and not definition.port_modes
                        and not definition.service_requirements
                        and not definition.object_accesses
                        and not definition.capabilities
                        and settlement_builder is None
                        and not definition.provided_session_item_types
                        and external_inputs_declaration is None
                        and resource_dependencies_declaration is None
                        and not resource_input_ports,
                        "graph_invalid_type",
                        "An executing tool loader exposes one structured prompt output only")
        for access in definition.object_accesses:
            require(type(access) is dict and set(access) == {
                "config_field", "multiple", "access", "type_id", "schema_version",
            } and bounded_name(access["config_field"]) and type(access["multiple"]) is bool
                and access["access"] in ("read", "write", "read_write")
                and bounded_name(access["type_id"]) and type(access["schema_version"]) is int
                and access["schema_version"] > 0,
                "graph_invalid_type", "Object access declaration is invalid")
            require((access["access"] == "write" or "objects:read" in definition.capabilities)
                    and (access["access"] == "read" or "objects:write" in definition.capabilities),
                    "graph_invalid_type", "Object access requires matching capabilities")
        require(external_inputs_declaration is None or callable(external_inputs_declaration),
                "graph_invalid_type", "External input declaration must be callable")
        require(resource_dependencies_declaration is None or callable(resource_dependencies_declaration),
                "graph_invalid_type", "Resource dependency declaration must be callable")
        require(resource_preflight_validator is None or callable(resource_preflight_validator),
                "graph_invalid_type", "Resource preflight validator must be callable")
        require(type(resource_input_ports) is tuple and len(set(resource_input_ports)) == len(resource_input_ports)
                and all(bounded_name(port) for port in resource_input_ports),
                "graph_invalid_type", "Resource input ports must be distinct names")
        Draft202012Validator.check_schema(definition.config_schema)
        definition = replace(
            definition,
            checkpoint_save=normalize_checkpoint_save(definition.config_schema, definition.checkpoint_save),
            provided_session_item_types=normalize_provided_session_item_types(
                definition.provided_session_item_types, self.data_types),
        )
        Draft202012Validator.check_schema(definition.private_state_schema)
        self.validate_private_state(definition, definition.private_state_default)
        for inputs, outputs in [(definition.inputs, definition.outputs), *definition.port_modes.values()]:
            for ports, is_input in ((inputs, True), (outputs, False)):
                require(len({port.port_id for port in ports}) == len(ports)
                        and all(bounded_name(port.port_id) and bounded_name(port.data_type)
                                and type(port.data_schema_version) is int and port.data_schema_version > 0
                                and type(port.required) is bool and type(port.multiple) is bool
                                and self.data_types.get(port.data_type, port.data_schema_version, scope="content") is not None
                                and type(port.accepted_types) is tuple
                                and (is_input or not port.accepted_types)
                                and all(type(item) is tuple and len(item) == 2
                                        and bounded_name(item[0]) and type(item[1]) is int and item[1] > 0
                                        and self.data_types.get(item[0], item[1], scope="content") is not None
                                        for item in port.accepted_types)
                                and len(set(port.accepted_types)) == len(port.accepted_types)
                                and (port.data_type, port.data_schema_version) not in port.accepted_types
                                for port in ports),
                        "graph_invalid_type", "Node port declarations are invalid")
        self._nodes[key] = RegisteredNode(copy.deepcopy(definition), executor, config_validator, state_migrator,
                                          external_inputs_validator, external_inputs_declaration,
                                          resource_dependencies_declaration, resource_preflight_validator,
                                          resource_input_ports, executor_ref, settlement_builder,
                                          failed_retry_validator, tool_binding)
        self._trusted_scopes[key] = trusted_scope

    def update_declarations(self, definition: NodeDefinition, *, trusted_scope: str = "host") -> None:
        """Replace only checkpoint and execution declarations within their trusted scope."""
        require(not self._frozen, "host_registry_frozen", "The active node registry is frozen")
        require(isinstance(definition, NodeDefinition), "graph_invalid_type",
                "Declaration update requires a node definition")
        require(type(trusted_scope) is str and bounded_name(trusted_scope),
                "graph_invalid_registration_scope", "Declaration update requires a trusted scope")
        require(type(definition.execution_class) is str
                and definition.execution_class in {"working", "non_working"},
                "graph_invalid_execution_class", "Node execution class must be working or non_working")
        key = definition.component_id, definition.component_version
        current = self._nodes.get(key)
        require(current is not None, "graph_missing_node_type", "Declaration update requires a registered node")
        require(self._trusted_scopes.get(key) == trusted_scope,
                "graph_declaration_update_scope", "Node declarations can only be updated by their trusted scope")
        normalized = replace(
            definition,
            checkpoint_save=normalize_checkpoint_save(definition.config_schema, definition.checkpoint_save),
            provided_session_item_types=normalize_provided_session_item_types(
                definition.provided_session_item_types, self.data_types),
        )
        executable = current.executor is not None or current.executor_ref is not None
        previous_contract = current.definition.to_dict(executable=executable)
        candidate_contract = normalized.to_dict(executable=executable)
        for field_name in ("checkpoint_save", "execution_class"):
            previous_contract.pop(field_name, None)
            candidate_contract.pop(field_name, None)
        require(canonical_bytes(previous_contract) == canonical_bytes(candidate_contract),
                "graph_declaration_update_contract_changed",
                "Declaration updates cannot change other node contracts")
        self._nodes[key] = replace(current, definition=copy.deepcopy(normalized))

    def get(self, component_id: str, component_version: str) -> RegisteredNode | None:
        value = self._nodes.get((component_id, component_version))
        return replace(value, definition=copy.deepcopy(value.definition)) if value is not None else None

    def get_definition(self, component_id: str, component_version: str) -> NodeDefinition | None:
        value = self._nodes.get((component_id, component_version))
        return copy.deepcopy(value.definition) if value is not None else None

    def catalog(self) -> list[dict]:
        result = []
        for _, entry in sorted(self._nodes.items()):
            value = entry.definition.to_dict(executable=entry.executor is not None or entry.executor_ref is not None)
            if entry.executor_ref is not None:
                value["executor_ref"] = entry.executor_ref.to_dict()
            result.append(value)
        return result

    def registrations(self) -> tuple[RegisteredNode, ...]:
        return tuple(copy.deepcopy(value) for _, value in sorted(self._nodes.items()))

    def validate_content(self, value: Any, data_type: str, schema_version: int = 1) -> dict:
        return validate_content_value(value, data_type, schema_version, registry=self.data_types)

    def detached(self, *, frozen: bool = False) -> NodeRegistry:
        registry = NodeRegistry.__new__(NodeRegistry)
        registry._nodes = copy.deepcopy(self._nodes)
        registry._trusted_scopes = copy.deepcopy(self._trusted_scopes)
        registry.executors = self.executors.detached(frozen=frozen)
        registry.services = self.services.detached(frozen=frozen)
        registry.information_sources = self.information_sources.detached(frozen=frozen)
        registry.data_types = self.data_types.detached(frozen=frozen)
        registry.package_lock = tuple(copy.deepcopy(self.package_lock))
        registry.execution_package_lock = tuple(copy.deepcopy(self.execution_package_lock))
        registry._frozen = frozen
        return registry

    def migrate_private_state(self, value: Any, source: NodeDefinition, target: NodeDefinition,
                              *, policy: str = "strict") -> Any:
        """Changing type/schema never reuses a same-ID state by assumption."""
        require(policy in ("strict", "reset"), "graph_private_state_incompatible", "Unknown state copy policy")
        self.validate_private_state(source, value)
        if policy == "reset":
            return self.validate_private_state(target, target.private_state_default)
        if ((source.component_id, source.component_version) == (target.component_id, target.component_version)
                and canonical_bytes(source.private_state_schema) == canonical_bytes(target.private_state_schema)):
            return self.validate_private_state(target, value)
        entry = self.get(target.component_id, target.component_version)
        require(entry is not None and entry.state_migrator is not None,
                "graph_private_state_incompatible", "Private state has no declared compatible migration")
        return self.validate_private_state(target, entry.state_migrator(copy.deepcopy(value), source))

    @staticmethod
    def validate_private_state(definition: NodeDefinition, value: Any) -> Any:
        validate_json_value(value)
        require(len(canonical_bytes(value)) <= MAX_CONTENT_CHARS,
                "graph_private_state_invalid", "Node private state exceeds its resource limit")
        try:
            Draft202012Validator(definition.private_state_schema).validate(value)
        except ValidationError as exc:
            raise GraphDiagnosticError("graph_private_state_invalid", "Node private state schema mismatch") from exc
        return copy.deepcopy(value)

    @staticmethod
    def validate_config(entry: RegisteredNode, config: dict) -> None:
        try:
            Draft202012Validator(entry.definition.config_schema).validate(config)
        except ValidationError as exc:
            raise GraphDiagnosticError("graph_invalid_config", exc.message) from exc
        if entry.config_validator is not None:
            entry.config_validator(copy.deepcopy(config))


@dataclass(frozen=True)
class GraphPlan:
    document: dict
    target_node_ids: tuple[str, ...]
    ordered_node_ids: tuple[str, ...]
    input_edges: dict[str, dict[str, list[dict]]]
    definitions: dict[str, NodeDefinition]


def _reference_path(node_index, field_name: str) -> str:
    """JSON Pointer to one declared configuration field of one node."""
    prefix = f"/nodes/{node_index}" if node_index is not None else "/nodes"
    token = str(field_name).replace("~", "~0").replace("/", "~1")
    return f"{prefix}/config/{token}"


def validate_session_item_registrations(document: dict, registry: NodeRegistry, *,
                                        phase: str = "compile") -> None:
    """Require one explicit object binding for each declared provider instance."""
    nodes = {node["node_binding_id"]: node for node in document["nodes"]}
    node_indexes = {node["node_binding_id"]: index for index, node in enumerate(document["nodes"])}
    expected = set()
    for identity, node in nodes.items():
        definition = registry.get_definition(node["component_id"], node["component_version"])
        if definition is None:
            continue
        expected.update((identity, item["type_id"], item["schema_version"])
                        for item in definition.provided_session_item_types)

    registered = set()
    for index, raw in enumerate(document.get("object_bindings", [])):
        path = f"/object_bindings/{index}"
        binding = ObjectBinding.from_dict(raw)
        registrar = binding.registrar_node_id
        if registrar is None:
            continue
        require(registrar in nodes, "session_object_registrar_missing",
                "Object registrar must name an existing node instance",
                phase=phase, path=path, node_id=registrar)
        node = nodes[registrar]
        definition = registry.get_definition(node["component_id"], node["component_version"])
        require(definition is not None, "session_object_registrar_unavailable",
                "Object registrar node definition is unavailable",
                phase=phase, path=path, node_id=registrar)
        identity = registrar, binding.type_id, binding.schema_version
        require(identity in expected, "session_object_registration_mismatch",
                "Registered object type is not declared by its registrar node",
                phase=phase, path=path, node_id=registrar,
                expected={"declared_by_registrar": True},
                actual={"type_id": binding.type_id, "schema_version": binding.schema_version})
        require(identity not in registered, "session_object_registration_duplicate",
                "Registrar instance already provides this exact session item type",
                phase=phase, path=path, node_id=registrar)
        if (definition.component_id, definition.component_version) == ("tools.variable-register", "2"):
            require((binding.type_id, binding.schema_version) == (VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION)
                    and node["config"].get("object_key") == binding.object_key,
                    "variable_registration_target_mismatch",
                    "Variable registration must target its own provided item",
                    phase=phase, path=path, node_id=registrar,
                    expected={"type_id": VARIABLE_TYPE, "schema_version": VARIABLE_SCHEMA_VERSION},
                    actual={"type_id": binding.type_id, "schema_version": binding.schema_version,
                            "object_key": binding.object_key})
            require(not binding.has_default or binding.default_value == empty_variable_value(),
                    "variable_registration_default_invalid",
                    "Variable item configuration may only initialize an unregistered shell",
                    phase=phase, path=path, node_id=registrar)
        registered.add(identity)

    for identity in expected:
        require(identity in registered, "session_object_registration_missing",
                "Provider node is missing its declared session item registration",
                phase=phase, path=f"/nodes/{node_indexes[identity[0]]}", node_id=identity[0],
                expected={"type_id": identity[1], "schema_version": identity[2]},
                actual={"registered": False})


def validate_config_node_references(entry: RegisteredNode, node: dict,
                                    nodes: dict[str, dict], *, phase: str = "compile",
                                    node_index: int | None = None) -> None:
    """Resolve one declaration's explicit node references against this document.

    Unconfigured values (absent, ``None``, ``allow_empty`` empty string, empty
    multiple list) are allowed; a configured value that no longer resolves is a
    dangling declared reference and is rejected.  The same rule serves the save
    boundary and the compiler.
    """
    node_id = node["node_binding_id"]
    for reference in entry.definition.config_node_references:
        field_name = reference["config_field"]
        path = _reference_path(node_index, field_name)
        value = node["config"].get(field_name)
        if value is None:
            continue
        if not reference["multiple"]:
            if value == "" and reference.get("allow_empty", False):
                continue
            identities = [value]
        else:
            if value == []:
                continue
            require(type(value) is list, "graph_invalid_config_reference",
                    "Node binding must name explicit node identities",
                    phase=phase, path=path, node_id=node_id,
                    expected={"kind": "array"}, actual={"kind": type(value).__name__})
            identities = value
        require(bool(identities), "graph_invalid_config_reference",
                "Node binding must name explicit node identities",
                phase=phase, path=path, node_id=node_id,
                expected={"kind": "array", "nonempty": True})
        allowed_types = [{"component_id": item["component_id"],
                          "component_version": item["component_version"]}
                         for item in reference["target_types"]]
        allowed_set = {(item["component_id"], item["component_version"])
                       for item in reference["target_types"]}
        for referenced_id in identities:
            require(uuid_value(referenced_id), "graph_invalid_config_reference",
                    "Node binding must name explicit node identities",
                    phase=phase, path=path, node_id=node_id,
                    expected={"kind": "canonical_uuid4"},
                    actual={"kind": type(referenced_id).__name__})
            referenced_node = nodes.get(referenced_id)
            require(referenced_node is not None, "graph_missing_config_reference",
                    "Configured node binding names a missing node",
                    phase=phase, path=path, node_id=node_id,
                    expected={"target_types": allowed_types, "node_id_exists": True},
                    actual={"node_id": referenced_id, "node_id_exists": False})
            require((referenced_node["component_id"], referenced_node["component_version"])
                    in allowed_set, "graph_config_reference_type_mismatch",
                    "Configured node binding differs from its declared target type",
                    phase=phase, path=path, node_id=node_id,
                    expected={"target_types": allowed_types},
                    actual={"component_id": referenced_node["component_id"],
                            "component_version": referenced_node["component_version"]})
        require(len(set(identities)) == len(identities), "graph_invalid_config_reference",
                "Configured node bindings must not repeat an identity",
                phase=phase, path=path, node_id=node_id)


def validate_object_access_references(node: dict, definition: NodeDefinition,
                                      object_bindings: dict, *, phase: str,
                                      node_index: int | None = None) -> None:
    """Resolve declared object keys against the document bindings (Q05-2).

    Unconfigured values are allowed; a configured key that no longer resolves,
    changes type or loses its declared access is rejected.  Shared by the save
    boundary and the compiler so both apply one rule.
    """
    node_id = node["node_binding_id"]
    for access in definition.object_accesses:
        field_name = access["config_field"]
        path = _reference_path(node_index, field_name)
        value = node["config"].get(field_name)
        if access["multiple"]:
            if value is None or value == []:
                continue
            require(type(value) is list and all(type(key) is str and bool(key) for key in value),
                    "graph_invalid_object_access", "Object access config must name explicit object keys",
                    phase=phase, path=path, node_id=node_id,
                    expected={"kind": "array", "multiple": True},
                    actual={"kind": type(value).__name__})
            keys = value
        else:
            if value is None or value == "":
                continue
            require(type(value) is str and bool(value) and bounded_name(value),
                    "graph_invalid_object_access", "Object access config must name an explicit object key",
                    phase=phase, path=path, node_id=node_id,
                    expected={"kind": "object_key", "multiple": False},
                    actual={"kind": type(value).__name__})
            keys = [value]
        for key in keys:
            binding = object_bindings.get(key)
            require(binding is not None, "session_object_unbound", "Declared object is not bound",
                    phase=phase, path=path, node_id=node_id,
                    expected={"object_key": key, "type_id": access["type_id"],
                              "schema_version": access["schema_version"]},
                    actual={"object_key": key, "bound": False})
            require((binding.type_id, binding.schema_version) == (access["type_id"], access["schema_version"]),
                    "session_object_type_mismatch", "Object binding differs from its node declaration",
                    phase=phase, path=path, node_id=node_id,
                    expected={"type_id": access["type_id"], "schema_version": access["schema_version"]},
                    actual={"type_id": binding.type_id, "schema_version": binding.schema_version})
            require((access["access"] == "write" or node_id in binding.readers)
                    and (access["access"] == "read" or node_id in binding.writers),
                    "session_object_access_denied", "Node lacks declared object access",
                    phase=phase, path=path, node_id=node_id,
                    expected={"access": access["access"]},
                    actual={"read": node_id in binding.readers, "write": node_id in binding.writers})


def validate_document_references(document: dict, registry: NodeRegistry, *,
                                 phase: str = "save") -> None:
    """Save-time declared reference check shared with the compiler (Q05-2).

    Declarations come from the registered node definitions; nothing here treats
    a field name as a guess, clears a configured value or migrates an object.
    """
    nodes = {node["node_binding_id"]: node for node in document["nodes"]}
    indexes = {node["node_binding_id"]: index for index, node in enumerate(document["nodes"])}
    object_bindings = {raw["object_key"]: ObjectBinding.from_dict(raw)
                       for raw in document.get("object_bindings", [])}
    for identity, node in nodes.items():
        entry = registry.get(node["component_id"], node["component_version"])
        index = indexes[identity]
        require(entry is not None, "graph_missing_node_type",
                "Node implementation is missing or unavailable",
                phase=phase, path=f"/nodes/{index}", node_id=identity)
        validate_config_node_references(entry, node, nodes, phase=phase, node_index=index)
        validate_object_access_references(node, entry.definition, object_bindings,
                                          phase=phase, node_index=index)


class GraphCompiler:
    def __init__(self, registry: NodeRegistry):
        self.registry = registry

    def compile(self, value: Any, *, external_inputs: dict | None = None) -> GraphPlan:
        document = validate_graph_document(value, phase="compile")
        return self._compile(document, external_inputs=external_inputs)

    def compile_event(self, value: Any, event_id: str, schema_version: int,
                      external_inputs: dict | None = None) -> GraphPlan:
        document = validate_graph_document(value, phase="compile")
        require(bounded_name(event_id) and type(schema_version) is int and 1 <= schema_version <= 2**53 - 1,
                "graph_event_not_declared", "An exact declared event identity and schema version are required")
        event = next((binding for binding in document.get("event_bindings", [])
                      if (binding["event_id"], binding["schema_version"]) == (event_id, schema_version)), None)
        require(event is not None, "graph_event_not_declared", "Event identity and schema version are not declared")
        if external_inputs is not None:
            try:
                validate_json_value(external_inputs)
            except ContractValidationError as exc:
                raise GraphDiagnosticError("graph_event_payload_invalid", "Event payload must contain only JSON values") from exc
            require(type(external_inputs) is dict and "_workflow_frozen_resources" not in external_inputs,
                    "graph_event_payload_invalid", "Event payload must be named nonreserved JSON inputs")
            try:
                Draft202012Validator(event["payload_schema"]).validate(external_inputs)
            except Exception as exc:
                raise GraphDiagnosticError("graph_event_payload_invalid", "Event payload differs from its declared schema") from exc
        return self._compile(document, external_inputs=external_inputs, targets=event["target_node_ids"])

    def _compile(self, document: dict, *, external_inputs: dict | None = None,
                 targets: list[str] | None = None) -> GraphPlan:
        validate_session_item_registrations(document, self.registry, phase="compile")
        preloads = validate_session_preloads(document, self.registry.data_types)
        available_packages = {item["package_id"]: item["version"] for item in self.registry.package_lock}
        require(all(available_packages.get(item["package_id"]) == item["version"]
                    for item in document.get("package_lock", [])),
                "graph_missing_package", "The graph requires an unavailable package version",
                phase="compile", path="/package_lock")
        for index, raw_binding in enumerate(document.get("object_bindings", [])):
            binding = ObjectBinding.from_dict(raw_binding)
            if binding.object_key in preloads:
                continue
            try:
                if binding.has_default:
                    self.registry.data_types.validate(binding.type_id, binding.schema_version,
                                                      binding.default_value, scope="session")
                else:
                    self.registry.data_types.default(binding.type_id, binding.schema_version, scope="session")
            except HostContractError as exc:
                raise GraphDiagnosticError(exc.reason_code, str(exc), phase="compile",
                                           path=f"/object_bindings/{index}") from exc
        if external_inputs is not None:
            validate_json_value(external_inputs)
            require(type(external_inputs) is dict, "graph_invalid_external_inputs",
                    "External inputs must be named values", phase="compile", path="")
        nodes = {node["node_binding_id"]: node for node in document["nodes"]}
        node_indexes = {node["node_binding_id"]: index for index, node in enumerate(document["nodes"])}
        # The compiler checks every edge endpoint independently of the storage
        # validator: unsaved or historical documents cannot bypass the rule and
        # an edge with a missing target is never silently ignored.
        for index, edge in enumerate(document["edges"]):
            for field_name in ("source_node_id", "target_node_id"):
                require(edge[field_name] in nodes, "graph_missing_edge_node",
                        "Edge endpoint names a missing node", phase="compile",
                        path=f"/edges/{index}/{field_name}", edge_id=edge["edge_id"],
                        node_id=edge[field_name], expected={"node_id_exists": True},
                        actual={"node_id_exists": False, "node_id": edge[field_name]})
        edge_indexes = {edge["edge_id"]: index for index, edge in enumerate(document["edges"])}
        ordinary = targets is None
        if ordinary:
            connected = {
                edge[key]
                for edge in (*document["edges"], *document.get("control_edges", []))
                for key in ("source_node_id", "target_node_id")
            }
            targets = [
                identity for identity, node in nodes.items()
                if identity in connected
                and ((definition := self.registry.get_definition(
                    node["component_id"], node["component_version"])) is None
                     or definition.execution_class == "working")
            ]
            require(any(
                (definition := self.registry.get_definition(
                    nodes[identity]["component_id"], nodes[identity]["component_version"]))
                is not None and definition.is_output
                for identity in targets
            ), "graph_no_outputs", "Workflow has no connected working output node",
                    phase="compile", path="/edges")
        targets = list(dict.fromkeys(targets))
        require(bool(targets), "graph_no_outputs", "Workflow has no execution targets",
                phase="compile", path="/edges")
        incoming: dict[str, list[dict]] = {}
        for edge in document["edges"]:
            incoming.setdefault(edge["target_node_id"], []).append(edge)
        control_incoming: dict[str, list[dict]] = {}
        for edge in document.get("control_edges", []):
            control_incoming.setdefault(edge["target_node_id"], []).append(edge)
        affected_outputs: dict[str, list[str]] = {}
        diagnostic_targets = [
            identity for identity in targets
            if not ordinary or (
                (definition := self.registry.get_definition(
                    nodes[identity]["component_id"], nodes[identity]["component_version"]))
                is not None and definition.is_output
            )
        ]
        for target in diagnostic_targets:
            pending, reached = [target], set()
            while pending:
                identity = pending.pop()
                if identity in reached:
                    continue
                reached.add(identity)
                affected_outputs.setdefault(identity, []).append(target)
                pending.extend(edge["source_node_id"] for edge in incoming.get(identity, []))
                pending.extend(edge["source_node_id"] for edge in control_incoming.get(identity, []))
        closure: set[str] = set()
        visiting: set[str] = set()
        definitions: dict[str, NodeDefinition] = {}
        bindings: dict[str, dict[str, list[dict]]] = {}

        def visit(identity: str, target: str) -> None:
            node_path = f"/nodes/{node_indexes.get(identity, 0)}"
            require(identity not in visiting, "graph_dependency_cycle", "Graph has a dependency cycle",
                    node_id=identity, dependent_outputs=[target], phase="compile", path=node_path)
            if identity in closure:
                return
            require(identity in nodes, "graph_missing_node", "Dependency node is missing",
                    node_id=identity, dependent_outputs=[target], phase="compile", path="/nodes")
            node = nodes[identity]
            entry = self.registry.get(node["component_id"], node["component_version"])
            require(entry is not None and (entry.executor is not None or entry.executor_ref is not None
                    and self.registry.executors.get(entry.executor_ref) is not None),
                    "graph_missing_node_type", "Node implementation is missing or unavailable",
                    node_id=identity, dependent_outputs=[target], phase="compile", path=node_path)
            require(not ordinary or entry.definition.execution_class == "working",
                    "graph_non_working_dependency",
                    "An ordinary working node depends on a non-working node",
                    node_id=identity, dependent_outputs=[target], phase="compile", path=node_path)
            try:
                self.registry.validate_config(entry, node["config"])
                validate_config_node_references(entry, node, nodes, phase="compile",
                                                node_index=node_indexes[identity])
                input_ports, output_ports = entry.definition.ports(node["config"])
                if external_inputs is not None and entry.external_inputs_validator is not None:
                    entry.external_inputs_validator(copy.deepcopy(node["config"]), copy.deepcopy(external_inputs))
            except GraphDiagnosticError as exc:
                diagnostic = exc.diagnostics[0]
                if not diagnostic.get("node_id"):
                    diagnostic["node_id"] = identity
                diagnostic["dependent_outputs"] = [target]
                if "phase" not in diagnostic:
                    diagnostic["phase"] = "compile"
                if "path" not in diagnostic:
                    diagnostic["path"] = f"{node_path}/config"
                raise
            except Exception as exc:
                raise GraphDiagnosticError(
                    getattr(exc, "reason_code", getattr(exc, "code", "graph_invalid_config")),
                    str(exc), node_id=identity, dependent_outputs=[target],
                    phase="compile", path=f"{node_path}/config") from exc
            declared_outputs = [port.port_id for port in output_ports]
            require(set(node.get("public_outputs", [])) <= set(declared_outputs),
                    "graph_unknown_port", "Public output is not declared", node_id=identity,
                    phase="compile", path=f"{node_path}/public_outputs",
                    expected={"declared_outputs": declared_outputs},
                    actual={"public_outputs": list(node.get("public_outputs", []))})
            visiting.add(identity)
            for edge in sorted(control_incoming.get(identity, []), key=lambda edge: edge["edge_id"]):
                visit(edge["source_node_id"], target)
            port_by_id = {port.port_id: port for port in input_ports}
            node_bindings: dict[str, list[dict]] = {}
            for edge in sorted(incoming.get(identity, []), key=lambda edge: (edge["order"], edge["edge_id"])):
                port_id = edge["target_port_id"]
                edge_path = f"/edges/{edge_indexes[edge['edge_id']]}"
                require(port_id in port_by_id, "graph_unknown_port", "Target input port is not declared",
                        node_id=identity, port_id=port_id, edge_id=edge["edge_id"],
                        dependent_outputs=[target], phase="compile",
                        path=f"{edge_path}/target_port_id",
                        expected={"declared_input_ports": sorted(port_by_id)},
                        actual={"port_id": port_id})
                visit(edge["source_node_id"], target)
                source = nodes[edge["source_node_id"]]
                source_entry = self.registry.get(source["component_id"], source["component_version"])
                source_ports = {port.port_id: port for port in source_entry.definition.ports(source["config"])[1]}
                require(edge["source_port_id"] in source_ports,
                        "graph_unknown_port", "Source output port is not declared", node_id=identity,
                        port_id=port_id, edge_id=edge["edge_id"], dependent_outputs=[target],
                        phase="compile", path=f"{edge_path}/source_port_id",
                        expected={"declared_output_ports": sorted(source_ports)},
                        actual={"port_id": edge["source_port_id"]})
                require(port_by_id[port_id].accepts(source_ports[edge["source_port_id"]]),
                        "graph_type_mismatch", "Linked content types differ", node_id=identity,
                        port_id=port_id, edge_id=edge["edge_id"], dependent_outputs=[target],
                        phase="compile", path=f"{edge_path}/target_port_id",
                        expected=port_by_id[port_id].to_dict(),
                        actual=source_ports[edge["source_port_id"]].to_dict())
                node_bindings.setdefault(port_id, []).append(copy.deepcopy(edge))
            for port in input_ports:
                edges = node_bindings.get(port.port_id, [])
                require(not port.required or bool(edges), "graph_required_input_missing",
                        "Required input is not connected", node_id=identity, port_id=port.port_id,
                        dependent_outputs=[target], phase="compile", path=node_path,
                        expected={"required": True}, actual={"connected_sources": 0})
                require(port.multiple or len(edges) <= 1, "graph_input_cardinality",
                        "Input accepts only one source", node_id=identity, port_id=port.port_id,
                        dependent_outputs=[target], phase="compile", path=node_path,
                        expected={"multiple": port.multiple},
                        actual={"connected_sources": len(edges)})
                require(len({edge["order"] for edge in edges}) == len(edges), "graph_input_order_conflict",
                        "Input source order is repeated", node_id=identity, port_id=port.port_id,
                        dependent_outputs=[target], phase="compile", path=node_path,
                        expected={"distinct_orders": True},
                        actual={"orders": sorted(edge["order"] for edge in edges)})
            definitions[identity], bindings[identity] = copy.deepcopy(entry.definition), node_bindings
            visiting.remove(identity)
            closure.add(identity)

        try:
            for target in targets:
                visit(target, target)
        except GraphDiagnosticError as exc:
            for diagnostic in exc.diagnostics:
                diagnostic["dependent_outputs"] = affected_outputs.get(
                    diagnostic["node_id"], diagnostic["dependent_outputs"],
                )
            raise
        creation_order = {
            node["node_binding_id"]: index for index, node in enumerate(document["nodes"])
        }
        predecessors = {
            identity: {
                edge["source_node_id"]
                for edge in (*incoming.get(identity, []), *control_incoming.get(identity, []))
            }
            for identity in closure
        }
        successors = {identity: set() for identity in closure}
        for identity, sources in predecessors.items():
            for source in sources:
                successors[source].add(identity)
        ready = [
            (creation_order[identity], identity)
            for identity, sources in predecessors.items() if not sources
        ]
        heapq.heapify(ready)
        ordered: list[str] = []
        remaining = {identity: len(sources) for identity, sources in predecessors.items()}
        while ready:
            _, identity = heapq.heappop(ready)
            ordered.append(identity)
            for dependent in successors[identity]:
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    heapq.heappush(ready, (creation_order[dependent], dependent))
        require(len(ordered) == len(closure), "graph_dependency_cycle",
                "Graph has a dependency cycle", phase="compile", path="/nodes")

        object_bindings = {raw["object_key"]: ObjectBinding.from_dict(raw)
                           for raw in document.get("object_bindings", [])}
        for identity in ordered:
            node = nodes[identity]
            validate_object_access_references(node, definitions[identity], object_bindings,
                                              phase="compile", node_index=node_indexes[identity])
        return GraphPlan(document, tuple(targets), tuple(ordered), bindings, definitions)


_UUID4_PATTERN = ("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-"
                  "[0-9a-f]{12}$")


def _document_json_schema() -> dict:
    """Structural GraphDocument schema; execution constraints stay in code."""
    port = {
        "type": "object",
        "required": ["port_id", "data_type"],
        "properties": {
            "port_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "data_type": {"type": "string", "minLength": 1, "maxLength": 128},
            "required": {"type": "boolean"},
            "multiple": {"type": "boolean"},
            "data_schema_version": {"type": "integer", "minimum": 1},
            "accepted_types": {"type": "array", "items": {
                "type": "object",
                "required": ["data_type", "data_schema_version"],
                "properties": {"data_type": {"type": "string", "minLength": 1, "maxLength": 128},
                               "data_schema_version": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            }},
        },
        "additionalProperties": False,
    }
    node_common = {
        "type": "object",
        "required": ["node_binding_id", "component_id", "component_version", "title",
                     "position", "config"],
        "properties": {
            "node_binding_id": {"type": "string", "pattern": _UUID4_PATTERN},
            "component_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "component_version": {"type": "string", "minLength": 1, "maxLength": 128},
            "title": {"type": "string", "maxLength": 128},
            "position": {"type": "object", "required": ["x", "y"],
                         "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                         "additionalProperties": False},
            "config": {"type": "object"},
            "public_outputs": {"type": "array", "maxItems": 64,
                               "items": {"type": "string", "minLength": 1}},
        },
        "additionalProperties": False,
    }
    node_v3 = copy.deepcopy(node_common)
    node_v3["required"] = [*node_common["required"], "configuration_policy"]
    node_v3["properties"]["configuration_policy"] = {
        "type": "object", "required": ["schema_version", "prompt_mode"],
        "properties": {"schema_version": {"const": 1},
                       "prompt_mode": {"enum": ["fixed", "adjustable"]}},
        "additionalProperties": False,
    }
    edge = {
        "type": "object",
        "required": ["edge_id", "source_node_id", "source_port_id",
                     "target_node_id", "target_port_id", "order"],
        "properties": {
            "edge_id": {"type": "string", "pattern": _UUID4_PATTERN},
            "source_node_id": {"type": "string", "pattern": _UUID4_PATTERN},
            "source_port_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "target_node_id": {"type": "string", "pattern": _UUID4_PATTERN},
            "target_port_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "order": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": False,
        "x-node-existence": "source_node_id and target_node_id must name nodes in this document",
    }
    control_edge = {
        "type": "object",
        "required": ["edge_id", "source_node_id", "target_node_id"],
        "properties": {
            "edge_id": {"type": "string", "pattern": _UUID4_PATTERN},
            "source_node_id": {"type": "string", "pattern": _UUID4_PATTERN},
            "target_node_id": {"type": "string", "pattern": _UUID4_PATTERN},
        },
        "additionalProperties": False,
    }
    object_binding = {
        "type": "object",
        "required": ["object_key", "type_id", "schema_version", "scope", "readers", "writers"],
        "properties": {
            "object_key": {"type": "string", "minLength": 1, "maxLength": 128},
            "type_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "schema_version": {"type": "integer", "minimum": 1},
            "scope": {"enum": ["shared", "private"]},
            "readers": {"type": "array", "maxItems": 512,
                        "items": {"type": "string", "pattern": _UUID4_PATTERN}},
            "writers": {"type": "array", "maxItems": 512,
                        "items": {"type": "string", "pattern": _UUID4_PATTERN}},
            "owner_node_id": {"type": ["string", "null"], "pattern": _UUID4_PATTERN},
            "registrar_node_id": {"type": ["string", "null"], "pattern": _UUID4_PATTERN},
            "default_value": {},
        },
        "additionalProperties": False,
    }
    event_binding = {
        "type": "object",
        "required": ["event_id", "schema_version", "display_name", "audience",
                     "target_node_ids", "payload_schema"],
        "properties": {
            "event_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "schema_version": {"type": "integer", "minimum": 1},
            "display_name": {"type": "string", "minLength": 1, "maxLength": 128},
            "audience": {"enum": ["management", "consumer"]},
            "target_node_ids": {"type": "array", "minItems": 1, "maxItems": MAX_GRAPH_NODES,
                                "items": {"type": "string", "pattern": _UUID4_PATTERN}},
            "payload_schema": {"type": "object"},
        },
        "additionalProperties": False,
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:openagent:workflow.graph-document:1",
        "title": "workflow.graph-document",
        "description": ("Structural document contract for workflow definitions. "
                        "Node/port availability, package locks, compilation and runtime "
                        "constraints are validated by the node catalog and compiler, not "
                        "by this schema."),
        "type": "object",
        "required": ["schema_version", "workflow_definition_id", "revision", "name",
                     "nodes", "edges"],
        "properties": {
            "schema_version": {"enum": list(SUPPORTED_GRAPH_VERSIONS)},
            "workflow_definition_id": {"type": "string", "pattern": _UUID4_PATTERN},
            "revision": {"type": "integer", "minimum": 1, "maximum": 2**53 - 1},
            "name": {"type": "string", "minLength": 1, "maxLength": 128},
            "nodes": {"type": "array", "maxItems": MAX_GRAPH_NODES,
                      "items": {"anyOf": [{"$ref": "#/$defs/node"},
                                          {"$ref": "#/$defs/node_v3"}]}},
            "edges": {"type": "array", "maxItems": MAX_GRAPH_EDGES,
                      "items": {"$ref": "#/$defs/edge"}},
            "object_bindings": {"type": "array", "maxItems": 1024,
                                "items": {"$ref": "#/$defs/object_binding"}},
            "package_lock": {"type": "array", "items": {
                "type": "object", "required": ["package_id", "version"],
                "properties": {"package_id": {"type": "string", "minLength": 1, "maxLength": 128},
                               "version": {"type": "string", "minLength": 1, "maxLength": 128}},
                "additionalProperties": False}},
            "control_edges": {"type": "array", "maxItems": MAX_GRAPH_EDGES,
                              "items": {"$ref": "#/$defs/control_edge"}},
            "event_bindings": {"type": "array", "maxItems": MAX_GRAPH_NODES,
                               "items": {"$ref": "#/$defs/event_binding"}},
            "edit_sequence": {"type": "integer", "minimum": 0, "maximum": 2**53 - 1},
            "session_preloads": {"type": "array", "items": {
                "type": "object", "required": ["object_key", "value"],
                "properties": {"object_key": {"type": "string", "minLength": 1, "maxLength": 128},
                               "value": {}},
                "additionalProperties": False}},
        },
        "additionalProperties": False,
        "allOf": [
            {"if": {"properties": {"schema_version": {"const": 3}}},
             "then": {"required": ["object_bindings"],
                      "properties": {"nodes": {"items": {"$ref": "#/$defs/node_v3"}}}}},
            {"if": {"properties": {"schema_version": {"enum": [1, 2]}}},
             "then": {"properties": {"nodes": {"items": {"$ref": "#/$defs/node"}}}}},
            {"if": {"properties": {"schema_version": {"const": 2}}},
             "then": {"required": ["object_bindings"]}},
            {"if": {"properties": {"schema_version": {"const": 1}}},
             "then": {"not": {"anyOf": [
                 {"required": ["object_bindings"]}, {"required": ["package_lock"]},
                 {"required": ["control_edges"]}, {"required": ["event_bindings"]},
                 {"required": ["edit_sequence"]}, {"required": ["session_preloads"]},
             ]}}},
        ],
        "$defs": {
            "node": node_common,
            "node_v3": node_v3,
            "edge": edge,
            "control_edge": control_edge,
            "object_binding": object_binding,
            "event_binding": event_binding,
        },
        "x-version-notes": {
            "1": "nodes/edges only; object_bindings and later arrays are not allowed",
            "2": "object_bindings required; package_lock/control_edges/event_bindings optional",
            "3": "adds node configuration_policy; edit_sequence/session_preloads optional",
        },
    }
    return schema


_SAVE_ERRORS = (
    ("graph_invalid_document", 400, "Document structure violates the versioned contract", "save"),
    ("graph_missing_edge_node", 400, "An ordinary edge endpoint names a missing node", "save"),
    ("graph_invalid_control_edge", 400, "A control dependency is duplicated or names a missing node", "save"),
    ("graph_invalid_event_binding", 400, "An event binding identity, audience, target or schema is invalid", "save"),
    ("graph_invalid_config_reference", 400, "A declared node reference field has an invalid value", "save"),
    ("graph_missing_config_reference", 400, "A configured declared node reference names a missing node", "save"),
    ("graph_config_reference_type_mismatch", 400, "A configured declared node reference names the wrong node type", "save"),
    ("graph_invalid_object_access", 400, "A declared object access field has an invalid value", "save"),
    ("session_object_unbound", 400, "A configured declared object key has no object binding", "save"),
    ("session_object_type_mismatch", 400, "An object binding differs from its node declaration", "save"),
    ("session_object_access_denied", 400, "A node lacks its declared object access", "save"),
    ("session_object_registrar_missing", 400, "An object registrar names a missing node", "save"),
    ("session_object_registrar_unavailable", 400, "An object registrar node definition is unavailable", "save"),
    ("session_object_registration_mismatch", 400, "A registration is not declared by its registrar node", "save"),
    ("session_object_registration_duplicate", 400, "A registrar instance declares one session item twice", "save"),
    ("session_object_registration_missing", 400, "A provider node is missing its session item registration", "save"),
    ("variable_registration_target_mismatch", 400, "Variable registration does not target its provided item", "save"),
    ("variable_registration_default_invalid", 400, "Variable registration default is not a legal shell", "save"),
    ("graph_missing_node_type", 409, "A node implementation is missing or unavailable", "save"),
    ("graph_configuration_missing", 409, "An existing definition has no current schema 3 configuration", "save"),
    ("stale_edit_sequence", 409, "The editing basis differs from the durable edit sequence", "save"),
    ("stale_revision", 409, "The submitted document fixed revision differs from its basis", "save"),
    ("configuration_run_locked", 409, "G21: user configuration is frozen while a run is unfinished", "save"),
    ("workflow_archived", 409, "The workflow is archived and read-only", "save"),
    ("workflow_modification_busy", 409, "Another modification of this workflow is in flight", "save"),
    ("graph_storage_busy", 409, "SQLite refused the write lock", "storage"),
    ("idempotency_conflict", 409, "The idempotency key belongs to a different request", "save"),
    # S3-05: save-side diagnostics of this step's configuration channel.
    ("graph_configuration_not_adjustable", 400,
     "The node has no trusted adjustable configuration declaration", "save"),
    ("graph_invalid_adjustable_declaration", 400,
     "A node adjustable configuration declaration is invalid", "save"),
    ("graph_definition_basis_mismatch", 400,
     "Configuration composition requires one definition basis", "save"),
    ("graph_definition_conflict", 409,
     "The derived workflow definition identity already exists", "save"),
    ("graph_configuration_target_mismatch", 409,
     "A configuration patch targets a different node contract", "save"),
    ("graph_edit_sequence_exhausted", 409,
     "The workflow configuration edit sequence is exhausted", "save"),
    ("graph_invalid_configuration_patch", 400,
     "Configuration patch fields are not permitted", "save"),
    ("graph_configuration_mutation_not_allowed", 400,
     "The exact node contract has no configuration mutation permission", "save"),
)

_COMPILE_ERRORS = (
    ("graph_missing_edge_node", 400, "An ordinary edge endpoint names a missing node", "compile"),
    ("graph_unknown_port", 400, "A declared port does not exist", "compile"),
    ("graph_type_mismatch", 400, "Linked content types differ", "compile"),
    ("graph_required_input_missing", 400, "A required input is not connected", "compile"),
    ("graph_input_cardinality", 400, "An input accepts only one source", "compile"),
    ("graph_input_order_conflict", 400, "An input source order is repeated", "compile"),
    ("graph_missing_node", 400, "A dependency node is missing", "compile"),
    ("graph_dependency_cycle", 400, "The graph has a dependency cycle", "compile"),
    ("graph_non_working_dependency", 400, "A working node depends on a non-working node", "compile"),
    ("graph_no_outputs", 400, "The workflow has no connected working output node", "compile"),
    ("graph_missing_package", 400, "The graph requires an unavailable package version", "compile"),
    ("graph_invalid_config", 400, "A node configuration violates its declared schema", "compile"),
    ("graph_missing_config_reference", 400, "A configured declared node reference names a missing node", "compile"),
    ("graph_config_reference_type_mismatch", 400, "A configured declared node reference names the wrong node type", "compile"),
    ("session_object_unbound", 400, "A configured declared object key has no object binding", "compile"),
    ("session_object_type_mismatch", 400, "An object binding differs from its node declaration", "compile"),
    ("session_object_access_denied", 400, "A node lacks its declared object access", "compile"),
    ("graph_invalid_external_inputs", 400, "External inputs are not named JSON values", "compile"),
)

_PREPARE_ERRORS = (
    ("package_missing_dependency", 409, "Restore the saved packages before running workflows", "prepare"),
    ("execution_authority_required", 403, "The invocation does not hold execution authority", "prepare"),
    ("graph_event_not_declared", 400, "The requested event identity or schema version is not declared", "prepare"),
    ("graph_event_payload_invalid", 400, "The event payload differs from its declared schema", "prepare"),
    ("package_retired", 409, "A selected compatibility package is no longer supported", "prepare"),
)

_RUN_ERRORS = (
    ("graph_node_failed", 500, "Node execution raised an unclassified failure", "run"),
    ("graph_linked_output_missing", 400, "A declared linked input has no published output", "run"),
    ("graph_node_output_invalid", 400, "A node output differs from its declared port", "run"),
    # S3-05: node-execution diagnostics raised by graph_execution/graph_service.
    ("graph_artifact_binding_mismatch", 400,
     "Content provenance differs from the graph's accepted input binding", "run"),
    ("graph_capability_denied", 400,
     "A node used a state capability it did not declare", "run"),
    ("graph_external_input_missing", 400, "A required external input is missing", "run"),
    ("graph_host_unavailable", 400,
     "A node requires an unavailable workflow host", "run"),
    ("graph_implementation_changed", 400,
     "A node implementation changed after planning", "run"),
    ("graph_invalid_resume", 400,
     "A saved execution boundary is invalid or differs from completed results", "run"),
    ("graph_node_run_missing", 400,
     "A participating node has no assigned run identity", "run"),
    ("graph_object_access_undeclared", 400,
     "Object access is outside the node declaration", "run"),
    ("graph_settlement_unavailable", 400,
     "A node requires an unavailable atomic settlement host", "run"),
    ("graph_duplicate_material", 400, "A prompt material instance is repeated", "run"),
    ("graph_legacy_prompt_retired", 400,
     "PROMPT@1 assembly is no longer supported", "run"),
    ("host_unknown_type", 400,
     "Content or object types are unavailable at this host", "run"),
    ("runtime_host_unavailable", 400,
     "A hosted executor requires the public runtime host", "run"),
    ("runtime_invocation_unavailable", 400,
     "A hosted invocation produced no successful result", "run"),
    ("session_data_definition_conflict", 400,
     "Session data belongs to a changed definition", "run"),
    ("session_data_read_only", 400, "Session data is read-only", "run"),
    ("session_data_unassigned", 400, "Session data has not been assigned", "run"),
    ("session_object_deleted", 400, "A session object was deleted", "run"),
    ("session_object_duplicate_write", 400,
     "One settle wrote the same object twice", "run"),
    ("stale_object_revision", 400,
     "A session object changed since it was read", "run"),
    ("tool_binding_not_prepared", 400,
     "This invocation has no prepared tool bridge", "run"),
    ("variable_not_registered", 400,
     "A variable assignment target is not registered", "run"),
    ("variable_type_mismatch", 400,
     "A variable update does not match its assigned type", "run"),
    ("private_state_not_declared", 409, "A node has no declared private state", "run"),
    ("private_state_owner_mismatch", 409,
     "Private state names an unknown owner", "run"),
    ("private_state_schema_mismatch", 409,
     "Private state does not match its schema", "run"),
    ("graph_private_state_invalid", 400,
     "Node private state exceeds its resource limit", "run"),
    ("graph_private_state_incompatible", 400,
     "Private state has no compatible migration", "run"),
    ("execution_owner_changed", 409,
     "The execution callback owner is no longer active", "run"),
    ("execution_dependency_unaccepted", 409,
     "A control predecessor has not completed", "run"),
    ("execution_stop_unconfirmed", 409,
     "An active execution has no coordinator to acknowledge stopping", "run"),
    ("invalid_state", 409,
     "The execution is not in a state that admits this action", "run"),
    ("stale_data_revision", 409, "Session data changed concurrently", "run"),
    ("stale_head_revision", 409, "Session Head changed concurrently", "run"),
)

_SETTLEMENT_ERRORS = (
    ("result_acceptance_pending", 409, "A completed result still awaits acceptance", "settlement"),
    ("checkpoint_commit_pending", 409, "A completed execution awaits checkpoint commit", "settlement"),
    ("result_committed_notification_failed", 409, "A committed result could not signal local completion", "settlement"),
    ("host_result_recovery_ambiguous", 503, "Several declared services retain the result; node recovery is unavailable", "settlement"),
    ("execution_result_unknown", 503, "Execution could not preserve its completion boundary", "settlement"),
    ("run_basis_unavailable", 410, "The invocation has no retained run basis", "settlement"),
    ("graph_invalid_settlement", 400,
     "The settlement builder returned an invalid result", "settlement"),
)

# S3-05: codes reachable in more than one stage of this step's workflow chain.
# ``phases`` lists every stage where the code is reachable from this chain; for
# legacy raise sites that do not set a phase, the stage is the enclosing save/
# compile/prepare/run/settlement/storage boundary.  A code whose status differs
# between raise sites records its settlement/control status and names the other
# status in its summary.
_SHARED_ERRORS = (
    ("graph_invalid_package_lock", 400,
     "Package lock lists an invalid or repeated exact package version", ("save", "compile")),
    ("graph_invalid_configuration_policy", 400,
     "Node configuration policy is invalid", ("save", "compile")),
    ("session_preload_type_unsupported", 400,
     "A session preload data type cannot be used as an ordinary preload", ("save", "compile")),
    ("session_preload_schema_reference_invalid", 400,
     "A session preload schema has an invalid or unresolved local reference", ("save", "compile")),
    ("session_preload_invalid", 400,
     "Session preloads violate the document's preload structure", ("save", "compile")),
    ("session_preload_duplicate", 400,
     "Session preload object keys are duplicated", ("save", "compile")),
    ("session_preload_binding_missing", 400,
     "A session preload has no exact object binding", ("save", "compile")),
    ("session_preload_source_conflict", 400,
     "A session preload conflicts with its binding default or registrar", ("save", "compile")),
    ("session_preload_type_unavailable", 400,
     "The exact session preload data type is unavailable", ("save", "compile")),
    ("session_preload_type_not_admitted", 400,
     "A session preload type does not admit checkpoint restoration", ("save", "compile")),
    ("session_preload_variable_unregistered", 400,
     "A preloaded variable is not registered", ("save", "compile")),
    ("package_publication_failed", 503,
     "The committed package selection is unavailable in this workflow service",
     ("save", "prepare", "run")),
    ("graph_artifact_reference_denied", 400,
     "A settled output references an unauthorized artifact", ("run", "settlement")),
    ("graph_invalid_output", 400,
     "A node output violates its declared content contract", ("run", "settlement")),
    ("graph_unknown_data_type", 400,
     "A content schema is not registered", ("run", "settlement")),
    ("graph_acceptance_basis_changed", 409,
     "The retained result or fixed configuration basis changed (node execution "
     "reports the same condition as 400)", ("run", "settlement")),
    ("failed_retry_unavailable", 409,
     "No retained failed invocation can be retried", ("run", "settlement")),
    ("recovery_unavailable", 409,
     "The execution has no retained continuation", ("run", "settlement")),
    ("request_abandoned", 409,
     "The request was explicitly terminated and cannot execute", ("save", "run")),
    ("storage_contract_violation", 500,
     "A durable record or run-order declaration violates its storage contract",
     ("save", "run", "settlement", "storage")),
    ("graph_type_mismatch", 400,
     "Linked content types differ", ("run",)),
)


def _catalogue_row(catalogue: dict, code: str, status: int, summary: str,
                   phases: tuple[str, ...]) -> dict:
    row = catalogue.setdefault(code, {
        "code": code, "status": status, "summary": summary, "phases": [],
        "location_fields": ["node_id", "port_id", "edge_id", "dependent_outputs",
                            "path", "expected", "actual"],
    })
    for phase in phases:
        if phase not in row["phases"]:
            row["phases"].append(phase)
    return row


def _error_catalogue() -> dict:
    catalogue = {}
    for code, status, summary, phase in (
            *_SAVE_ERRORS, *_COMPILE_ERRORS, *_PREPARE_ERRORS,
            *_RUN_ERRORS, *_SETTLEMENT_ERRORS):
        _catalogue_row(catalogue, code, status, summary, (phase,))
    for code, status, summary, phases in _SHARED_ERRORS:
        _catalogue_row(catalogue, code, status, summary, phases)
    return catalogue


def document_contract() -> dict:
    """Versioned machine-readable document/save/diagnostic contract (Q06-3).

    Constants and rules come from this module's actual validators; the JSON
    Schema describes structure only and explicitly does not claim to cover
    catalog/node availability or compilation/runtime constraints.
    """
    example_empty = {
        "schema_version": 3,
        "workflow_definition_id": "00000000-0000-4000-8000-000000000001",
        "revision": 1, "name": "Empty schema 3 document",
        "nodes": [], "edges": [], "object_bindings": [], "package_lock": [],
    }
    example_edge = {
        "schema_version": 3,
        "workflow_definition_id": "00000000-0000-4000-8000-000000000002",
        "revision": 1, "name": "One node",
        "nodes": [{
            "node_binding_id": "00000000-0000-4000-8000-000000000010",
            "component_id": "tools.text", "component_version": "1",
            "title": "Text", "position": {"x": 0, "y": 0}, "config": {"text": "hello"},
            "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
        }],
        "edges": [], "object_bindings": [], "package_lock": [],
    }
    return {
        "schema_version": 1,
        "kind": "workflow.document-contract",
        "contract_version": DOCUMENT_CONTRACT_VERSION,
        "discovery": {
            "application": "GET /api/graph/application",
            "document_contract_query": "document.contract",
            "preview_query": "definition.preview",
            "node_catalog": "catalog.node-types (protocol_version 2)",
            "editor_inputs": "definition.inputs",
        },
        "document": {
            "json_schema": _document_json_schema(),
            "supported_versions": list(SUPPORTED_GRAPH_VERSIONS),
            "current_version": 3,
            "limits": {
                "max_nodes": MAX_GRAPH_NODES,
                "max_edges": MAX_GRAPH_EDGES,
                "max_bytes": MAX_GRAPH_BYTES,
                "max_content_chars": MAX_CONTENT_CHARS,
            },
            "structure_rules": [
                "ordinary edge source_node_id/target_node_id must name nodes in this document",
                "control_edges, event target_node_ids and object binding authorized nodes must name nodes in this document",
                "unconfigured declared references (absent, null, allow_empty empty string, empty multiple list) are allowed",
                "configured declared references must resolve; the backend rejects dangling references at save",
            ],
            "examples": [example_empty, example_edge],
        },
        "save": {
            "operation": "definition.update",
            "narrow_patch_operation": "graph.frontend-node.patch-input",
            "baseline": ["expected_edit_sequence", "document.revision"],
            "changed_categories": ["display", "adjustable", "fixed"],
            "outcomes": {
                "saved": "the modification was accepted and written",
                "unchanged": "the proposed document equals the accepted document",
                "confirmation_required": "a fixed change with an unarchived session on this workflow needs confirm_derivation; nothing is written",
                "derived": "the confirmed fixed change was written to a new workflow identity",
            },
            "checks": {
                "structure": "validate_graph_document (this contract schema)",
                "declared_references": "config_node_references / object_accesses / provided session items / preloads",
                "fixed_config_schema": "checked by the compiler, not a save gate; an incomplete but structurally legal graph remains savable",
                "concurrency": "workflow_modification_busy before the transaction; CAS inside the transaction",
                "storage": "graph_storage_busy when SQLite refuses the write lock",
            },
            "mutex_release": "successful commit or explicit rollback; not frontend acceptance or evidence closure",
            "preview": "definition.preview evaluates structure/references/compile/classification "
                       "without writing or taking the modification entitlement",
        },
        "preview": {
            "query": "definition.preview",
            "kind": "workflow.configuration-preview",
            "required": ["workflow_definition_id", "document", "expected_edit_sequence"],
            "read_only": True,
            "returns": {
                "structure": "validate_graph_document result (same rule as save)",
                "declared_references": "declared node/object/preload reference checks",
                "compilation": "GraphCompiler result and target node identities",
                "baseline": "current edit sequence/revision, archived/run-lock flags and occupying sessions (read snapshot)",
                "changes": "display/adjustable/fixed categories and the predicted would_outcome",
                "run_conditions": "which conditions were and were not checked",
            },
            "guarantees": [
                "writes no business, evidence or idempotency record",
                "does not take the modification entitlement and does not hold it",
                "does not reserve resources and does not call models or tools",
            ],
            "limits": [
                "a successful preview does not guarantee that a later submit or run succeeds",
                "submission re-runs the entitlement, CAS, G21, idempotency and resource checks",
            ],
        },
        "run": {
            "start_command": "run.start", "control_command": "run.control",
            "failure_phases": {
                "prepare": "the start call fails before the accepted prepare commit; no chain/node_run exists, so callers must retain the original error response",
                "run": "node execution failed; persisted in the chain and node_run diagnostics",
                "settlement": "result acceptance/checkpoint commit failed; retryable diagnostics persist with failure_stage",
                "storage": "SQLite refused the write lock; nothing was processed",
            },
            "readable_after_failure": {
                "chain": "session.read chains[].diagnostic; run.read chain diagnostic",
                "node": "session.read nodes[].diagnostic; run.read node_runs[].diagnostic",
            },
            "available_actions": {
                "running/prepared": ["pause"],
                "paused": ["resume", "close"],
                "archive_failed": ["retry_acceptance or retry_archive", "close"],
                "failed/recovery_unavailable/budget_exhausted": ["close"],
                "idle": [],
            },
            "close": {
                "action": "close",
                "requires_pause_first": ["running", "prepared"],
                "scope": "closes the active chain of this session and clears its active_chain_run_id",
                "effects": ["releases this chain's in-memory frames and accepted capabilities",
                            "writes a chain_end manifest with status closed"],
                "limits": ["does not close other sessions' active chains",
                           "does not archive the session and does not clear all occupancy",
                           "does not automatically free G21 for other unfinished sessions",
                           "a fixed edit with an unarchived session still needs derivation confirmation"],
            },
            "g21": {
                "code": "configuration_run_locked", "status": 409, "phase": "save",
                "occupying_sessions_row": {"workflow_session_id": "uuid", "active_chain_run_id": "uuid"},
                "meaning": "every persisted session with a non-null active_chain_run_id",
                "source": "read in the caller's configuration-data transaction; no extra storage read",
                "not_bypassed_by": ["preview", "close of another session", "successful request for another workflow"],
            },
        },
        "diagnostics": {
            "fields": [
                {"name": "code", "required": True, "description": "diagnostic code; equals the outer reason_code for DiagnosticError"},
                {"name": "message", "required": True, "description": "bounded public message"},
                {"name": "node_id", "required": False, "description": "node binding identity"},
                {"name": "port_id", "required": False, "description": "declared port identity"},
                {"name": "edge_id", "required": False, "description": "edge identity"},
                {"name": "dependent_outputs", "required": False, "description": "output nodes affected by this failure"},
                {"name": "phase", "required": False, "description": "save/compile/prepare/run/settlement/storage"},
                {"name": "path", "required": False, "description": "RFC 6901 JSON Pointer into the document being checked; empty string is the root"},
                {"name": "expected", "required": False, "description": "bounded expected type/value summary when known"},
                {"name": "actual", "required": False, "description": "bounded actual type/value summary when known"},
            ],
            "phases": ["save", "compile", "prepare", "run", "settlement", "storage"],
            "reason_code_mapping": ("DiagnosticError: outer reason_code == diagnostics[0].code == "
                                    "HTTP error reason_code. Legacy graph errors carry reason_code in "
                                    "the diagnostic instead of code."),
            "information_policy": ("unavailable information stays absent; diagnostics never contain "
                                   "full request bodies, secrets or unbounded payloads"),
        },
        "errors": _error_catalogue(),
        "notes": [
            "contract_version 2 adds the run/prepare/close/G21 semantics and the read-only preview query",
            "errors is a PARTIAL catalogue, not the complete set of possible failures: it covers the "
            "error codes this step's save/compile/prepare/run/settlement/storage chain can actually "
            "raise from graph_contracts, workflow_configuration, configuration_mutations, "
            "graph_service, graph_execution, graph_store, storage and the session-preload validation "
            "delegated by validate_graph_document; the S3-05 coverage fix added 64 reachable chain "
            "codes and one existing row gained its run phase, without changing the document schema, "
            "preview/run semantics or query parameters, so contract_version stays 2",
            "errors does not enumerate everything outside that chain: generic request-contract codes "
            "(invalid_request, not_found, service_closed), declaration-registration codes raised only "
            "when host packages register or update declarations (graph_invalid_type, "
            "graph_invalid_registration_scope, graph_duplicate_type, graph_invalid_execution_class, "
            "host_registry_frozen, runtime_invalid_executor/runtime_missing_executor/"
            "runtime_executor_conflict, graph_invalid_checkpoint_save, "
            "graph_invalid_session_item_declaration, graph_missing_session_item_type, "
            "graph_declaration_update_scope, graph_declaration_update_contract_changed), delegated "
            "sub-protocol vocabularies (frontend_node_*, request_evidence_*, session-object/"
            "checkpoint/chat record codes, model_*/prompt_*/tool_* package, transport and model "
            "execution codes), definition.inputs consumer-input declaration diagnostics "
            "(consumer_input_*, returned inside that query's diagnostics instead of being raised), "
            "workflow management commands (including session.copy's active_execution_not_copyable) "
            "and node/package implementation codes are not listed here",
            "phases on a row list every stage where the code is reachable from this chain; for "
            "legacy raise sites that do not set a phase, the stage comes from the enclosing "
            "save/compile/prepare/run/settlement/storage boundary rather than from the diagnostic "
            "object itself",
            "a few codes use different HTTP statuses at different raise sites (for example "
            "graph_acceptance_basis_changed is 409 at settlement/run control and 400 at node "
            "execution); the row records the settlement/control status and the summary names the "
            "exception",
            "extension: add reachable codes to the matching tuple in this module and keep each row's "
            "status/summary/phases tied to an actual raise site; consumers must not treat the errors "
            "key set as the full list of possible errors",
            "JSON Schema covers document structure only; node availability, package locks, "
            "compilation and runtime constraints are not covered by this schema",
            "concise examples are illustrative; limits and rules above come from the actual constants",
        ],
    }
