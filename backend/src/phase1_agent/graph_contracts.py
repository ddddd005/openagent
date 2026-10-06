"""Trusted node declarations and the versioned, named-port graph boundary."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, validate_json_value
from .host_sdk import (
    DataTypeDefinition, HostContractError, HostServiceRegistry, InformationSourceRegistry,
    ObjectBinding, TypeRegistry, bounded_name,
)
from .runtime_executor_contracts import ExecutorReference, ExecutorRegistry


MAX_GRAPH_NODES = 512
MAX_GRAPH_EDGES = 4096
MAX_CONTENT_CHARS = 1_000_000
MAX_GRAPH_BYTES = 4_000_000


class GraphDiagnosticError(ContractValidationError):
    def __init__(self, code: str, message: str, *, node_id: str | None = None,
                 port_id: str | None = None, edge_id: str | None = None,
                 dependent_outputs: list[str] | None = None):
        super().__init__(message)
        self.reason_code, self.status_code = code, 400
        self.diagnostics = [{
            "code": code, "message": message, "node_id": node_id,
            "port_id": port_id, "edge_id": edge_id,
            "dependent_outputs": dependent_outputs or [],
        }]


def require(condition: bool, code: str, message: str, **location: Any) -> None:
    if not condition:
        raise GraphDiagnosticError(code, message, **location)


def uuid4_string(value: Any) -> str:
    try:
        parsed = UUID(value) if type(value) is str else None
    except (ValueError, TypeError):
        parsed = None
    require(parsed is not None and parsed.version == 4 and str(parsed) == value,
            "graph_invalid_document", "Graph identities must be canonical UUID4")
    return value


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


def validate_graph_document(value: Any) -> dict:
    """Validate storage structure without requiring dormant nodes to be runnable."""
    validate_json_value(value)
    base_fields = {"schema_version", "workflow_definition_id", "revision", "name", "nodes", "edges"}
    require(type(value) is dict and type(value.get("schema_version")) is int
            and value["schema_version"] in (1, 2),
            "graph_invalid_document", "Unknown graph document version")
    require(set(value) == base_fields if value["schema_version"] == 1 else
            base_fields | {"object_bindings"} <= set(value) <= base_fields | {
                "object_bindings", "package_lock", "execution_roots", "control_edges", "event_bindings"},
            "graph_invalid_document", "Graph document fields are invalid")
    uuid4_string(value["workflow_definition_id"])
    require(type(value["revision"]) is int and 1 <= value["revision"] <= 2**53 - 1,
            "graph_invalid_document", "Graph revision must be a positive integer")
    require(type(value["name"]) is str and 0 < len(value["name"].strip()) <= 128,
            "graph_invalid_document", "Graph name must be nonempty text")
    require(type(value["nodes"]) is list and len(value["nodes"]) <= MAX_GRAPH_NODES,
            "graph_invalid_document", "Graph node array exceeds its limit")
    node_ids: set[str] = set()
    for node in value["nodes"]:
        require(type(node) is dict and {
            "node_binding_id", "component_id", "component_version", "title", "position", "config",
        } <= set(node) <= {
            "node_binding_id", "component_id", "component_version", "title", "position", "config",
            "public_outputs",
        }, "graph_invalid_document", "Graph node fields are invalid")
        identity = uuid4_string(node["node_binding_id"])
        require(identity not in node_ids, "graph_invalid_document", "Duplicate node identity")
        node_ids.add(identity)
        require(all(type(node[key]) is str and 0 < len(node[key]) <= 128
                    for key in ("component_id", "component_version"))
                and type(node["title"]) is str and len(node["title"]) <= 128
                and type(node["config"]) is dict,
                "graph_invalid_document", "Node type, title or config is invalid", node_id=identity)
        position = node["position"]
        require(type(position) is dict and set(position) == {"x", "y"}
                and all(type(coordinate) in (int, float) for coordinate in position.values()),
                "graph_invalid_document", "Node position is invalid", node_id=identity)
        public = node.get("public_outputs", [])
        require(type(public) is list and len(public) <= 64
                and all(type(port) is str and bool(port) for port in public)
                and len(public) == len(set(public)),
                "graph_invalid_document", "Public output names are invalid", node_id=identity)
    require(type(value["edges"]) is list and len(value["edges"]) <= MAX_GRAPH_EDGES,
            "graph_invalid_document", "Graph edge array exceeds its limit")
    edge_ids: set[str] = set()
    for edge in value["edges"]:
        require(type(edge) is dict and set(edge) == {
            "edge_id", "source_node_id", "source_port_id", "target_node_id", "target_port_id", "order",
        }, "graph_invalid_document", "Graph edge fields are invalid")
        identity = uuid4_string(edge["edge_id"])
        require(identity not in edge_ids, "graph_invalid_document", "Duplicate edge identity")
        edge_ids.add(identity)
        uuid4_string(edge["source_node_id"])
        uuid4_string(edge["target_node_id"])
        require(all(type(edge[key]) is str and 0 < len(edge[key]) <= 128
                    for key in ("source_port_id", "target_port_id"))
                and type(edge["order"]) is int and 0 <= edge["order"] <= 2**53 - 1,
                "graph_invalid_document", "Graph edge ports or order are invalid", edge_id=identity)
    if value["schema_version"] == 2:
        require(type(value["object_bindings"]) is list and len(value["object_bindings"]) <= 1024,
                "graph_invalid_document", "Object binding array exceeds its limit")
        object_keys: set[str] = set()
        for raw_binding in value["object_bindings"]:
            try:
                binding = ObjectBinding.from_dict(raw_binding)
            except HostContractError as exc:
                raise GraphDiagnosticError(exc.reason_code, str(exc)) from exc
            require(binding.object_key not in object_keys, "graph_invalid_document", "Object key is duplicated")
            object_keys.add(binding.object_key)
            authorized_nodes = set(binding.readers) | set(binding.writers)
            if binding.owner_node_id is not None:
                authorized_nodes.add(binding.owner_node_id)
            require(authorized_nodes <= node_ids, "graph_invalid_document", "Object permission names a missing node")
        validate_package_lock(value.get("package_lock", []))
        roots = value.get("execution_roots", [])
        require(type(roots) is list and len(roots) <= MAX_GRAPH_NODES
                and all(root in node_ids for root in roots if type(root) is str)
                and all(type(root) is str for root in roots) and len(set(roots)) == len(roots),
                "graph_invalid_execution_roots", "Execution roots must name distinct existing nodes")
        events = value.get("event_bindings", [])
        require(type(events) is list and len(events) <= MAX_GRAPH_NODES,
                "graph_invalid_event_binding", "Event binding array exceeds its limit")
        require(not events or "execution_roots" in value, "graph_invalid_execution_roots",
                "Graphs with events must explicitly declare ordinary execution roots")
        event_identities: set[tuple[str, int]] = set()
        for event in events:
            require(type(event) is dict and set(event) == {
                "event_id", "schema_version", "display_name", "audience", "target_node_ids", "payload_schema",
            }, "graph_invalid_event_binding", "Event binding fields differ")
            require(bounded_name(event["event_id"]) and type(event["schema_version"]) is int
                    and 1 <= event["schema_version"] <= 2**53 - 1
                    and type(event["display_name"]) is str and bool(event["display_name"].strip())
                    and len(event["display_name"]) <= 128
                    and event["audience"] in ("management", "consumer"),
                    "graph_invalid_event_binding", "Event identity, display name or audience is invalid")
            identity = event["event_id"], event["schema_version"]
            require(identity not in event_identities, "graph_invalid_event_binding",
                    "Event identity and schema version are duplicated")
            event_identities.add(identity)
            targets = event["target_node_ids"]
            require(type(targets) is list and 0 < len(targets) <= MAX_GRAPH_NODES
                    and all(type(target) is str and target in node_ids for target in targets)
                    and len(set(targets)) == len(targets), "graph_invalid_event_binding",
                    "Event targets must name distinct existing nodes")
            schema = event["payload_schema"]
            require(type(schema) is dict and schema.get("type") == "object",
                    "graph_invalid_event_binding", "Event payload schema must declare a JSON object")
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise GraphDiagnosticError("graph_invalid_event_binding", "Event payload schema is invalid") from exc
            _validate_event_schema_references(schema)
        controls = value.get("control_edges", [])
        require(type(controls) is list and len(controls) <= MAX_GRAPH_EDGES,
                "graph_invalid_control_edge", "Control dependency array exceeds its limit")
        for edge in controls:
            require(type(edge) is dict and set(edge) == {"edge_id", "source_node_id", "target_node_id"},
                    "graph_invalid_control_edge", "Control dependencies contain only node identities")
            identity = uuid4_string(edge["edge_id"])
            uuid4_string(edge["source_node_id"])
            uuid4_string(edge["target_node_id"])
            require(identity not in edge_ids and edge["source_node_id"] in node_ids
                    and edge["target_node_id"] in node_ids,
                    "graph_invalid_control_edge", "Control dependency is duplicated or names a missing node")
            edge_ids.add(identity)
    require(len(canonical_bytes(value)) <= MAX_GRAPH_BYTES,
            "graph_invalid_document", "Graph document exceeds its wire limit")
    return copy.deepcopy(value)


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
    """Standalone legacy helpers use built-ins; runtime uses its frozen registry."""
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
            from .graph_prompt import validate_ready_prompt
            validate_ready_prompt(value)
    elif data_type == "MODEL_RESOURCE":
        require(set(value) == {"schema_version", "kind", "binding"}
                and value["kind"] == "workflow.model-resource",
                "graph_invalid_output", "Model resource must be a frozen reference")
        from .model_selection import validate_model_binding
        validate_model_binding(value["binding"])
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

    def to_dict(self) -> dict:
        value = {"port_id": self.port_id, "data_type": self.data_type,
                 "required": self.required, "multiple": self.multiple}
        if self.data_schema_version != 1 or self.data_type not in {"TEXT", "PROMPT", "JSON", "MODEL_RESOURCE"}:
            value["data_schema_version"] = self.data_schema_version
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

    def ports(self, config: dict) -> tuple[tuple[NodePort, ...], tuple[NodePort, ...]]:
        if self.port_modes:
            mode = config.get("mode")
            require(type(mode) is str and mode in self.port_modes,
                    "graph_invalid_config", "Node content mode is invalid")
            return self.port_modes[mode]
        return self.inputs, self.outputs

    def to_dict(self, *, executable: bool = True) -> dict:
        value = {key: copy.deepcopy(getattr(self, key)) for key in (
            "component_id", "component_version", "display_name", "category", "default_config",
            "config_schema", "is_output", "private_state_schema", "private_state_default",
        )}
        value.update({"inputs": [port.to_dict() for port in self.inputs],
                      "outputs": [port.to_dict() for port in self.outputs],
                      "capabilities": list(self.capabilities), "executable": executable})
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
        return value


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


class NodeRegistry:
    """Only trusted application/plugin code may associate a declaration with code."""

    def __init__(self, data_types: TypeRegistry | None = None) -> None:
        self._nodes: dict[tuple[str, str], RegisteredNode] = {}
        self.executors = ExecutorRegistry()
        self.services = HostServiceRegistry()
        self.information_sources = InformationSourceRegistry()
        self.data_types = data_types if data_types is not None else TypeRegistry()
        self.package_lock: tuple[dict, ...] = ()
        self.execution_package_lock: tuple[dict, ...] = ()
        self._frozen = False
        for data_type in ("TEXT", "PROMPT", "JSON", "MODEL_RESOURCE"):
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
                 failed_retry_validator: Callable[[dict, dict], dict] | None = None) -> None:
        key = definition.component_id, definition.component_version
        require(not self._frozen, "host_registry_frozen", "The active node registry is frozen")
        require(key not in self._nodes, "graph_duplicate_type", "Node implementation is already registered")
        require(executor is None or callable(executor), "graph_invalid_type", "Node executor must be callable")
        require(executor_ref is None or isinstance(executor_ref, ExecutorReference),
                "runtime_invalid_executor", "Hosted node requires an exact executor reference")
        if executor_ref is not None:
            require(executor is None, "runtime_executor_conflict", "Node cannot declare two execution paths")
            require(self.executors.get(executor_ref) is not None,
                    "runtime_missing_executor", "Hosted node requires its exact executor registration")
        require(definition.input_storage in ("inline", "references"),
                "graph_invalid_type", "Unknown input storage policy")
        self.services.grants(definition.service_requirements, definition.capabilities)
        require(settlement_builder is None or callable(settlement_builder), "graph_invalid_type",
                "Settlement builder must be callable")
        require(failed_retry_validator is None or callable(failed_retry_validator), "graph_invalid_type",
                "Failed retry validator must be callable")
        declared_fields = set()
        for reference in definition.config_node_references:
            require(type(reference) is dict and set(reference) == {"config_field", "multiple", "target_types"}
                    and bounded_name(reference["config_field"]) and type(reference["multiple"]) is bool
                    and reference["config_field"] not in declared_fields
                    and type(reference["target_types"]) is list and 0 < len(reference["target_types"]) <= 256
                    and all(type(target) is dict and set(target) == {"component_id", "component_version"}
                            and bounded_name(target["component_id"]) and bounded_name(target["component_version"])
                            for target in reference["target_types"]),
                    "graph_invalid_type", "Config node reference declarations are invalid")
            declared_fields.add(reference["config_field"])
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
        Draft202012Validator.check_schema(definition.private_state_schema)
        self.validate_private_state(definition, definition.private_state_default)
        for inputs, outputs in [(definition.inputs, definition.outputs), *definition.port_modes.values()]:
            for ports in (inputs, outputs):
                require(len({port.port_id for port in ports}) == len(ports)
                        and all(bounded_name(port.port_id) and bounded_name(port.data_type)
                                and type(port.data_schema_version) is int and port.data_schema_version > 0
                                and type(port.required) is bool and type(port.multiple) is bool
                                and self.data_types.get(port.data_type, port.data_schema_version, scope="content") is not None
                                for port in ports),
                        "graph_invalid_type", "Node port declarations are invalid")
        self._nodes[key] = RegisteredNode(copy.deepcopy(definition), executor, config_validator, state_migrator,
                                          external_inputs_validator, external_inputs_declaration,
                                          resource_dependencies_declaration, resource_preflight_validator,
                                          resource_input_ports, executor_ref, settlement_builder, failed_retry_validator)

    def get(self, component_id: str, component_version: str) -> RegisteredNode | None:
        return self._nodes.get((component_id, component_version))

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


class GraphCompiler:
    def __init__(self, registry: NodeRegistry):
        self.registry = registry

    def compile(self, value: Any, *, external_inputs: dict | None = None) -> GraphPlan:
        document = validate_graph_document(value)
        return self._compile(document, external_inputs=external_inputs)

    def compile_event(self, value: Any, event_id: str, schema_version: int,
                      external_inputs: dict | None = None) -> GraphPlan:
        document = validate_graph_document(value)
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
        available_packages = {item["package_id"]: item["version"] for item in self.registry.package_lock}
        require(all(available_packages.get(item["package_id"]) == item["version"]
                    for item in document.get("package_lock", [])),
                "graph_missing_package", "The graph requires an unavailable package version")
        for raw_binding in document.get("object_bindings", []):
            binding = ObjectBinding.from_dict(raw_binding)
            try:
                if binding.has_default:
                    self.registry.data_types.validate(binding.type_id, binding.schema_version,
                                                      binding.default_value, scope="session")
                else:
                    self.registry.data_types.default(binding.type_id, binding.schema_version, scope="session")
            except HostContractError as exc:
                raise GraphDiagnosticError(exc.reason_code, str(exc)) from exc
        if external_inputs is not None:
            validate_json_value(external_inputs)
            require(type(external_inputs) is dict, "graph_invalid_external_inputs", "External inputs must be named values")
        nodes = {node["node_binding_id"]: node for node in document["nodes"]}
        if targets is None:
            targets = ([] if document.get("event_bindings") else
                       [identity for identity, node in nodes.items()
                        if (entry := self.registry.get(node["component_id"], node["component_version"]))
                        and entry.definition.is_output])
            targets = [*targets, *document.get("execution_roots", [])]
        targets = sorted(set(targets))
        require(bool(targets), "graph_no_outputs", "Workflow has no execution targets")
        incoming: dict[str, list[dict]] = {}
        for edge in document["edges"]:
            incoming.setdefault(edge["target_node_id"], []).append(edge)
        control_incoming: dict[str, list[dict]] = {}
        for edge in document.get("control_edges", []):
            control_incoming.setdefault(edge["target_node_id"], []).append(edge)
        affected_outputs: dict[str, list[str]] = {}
        for target in targets:
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
        order: list[str] = []
        definitions: dict[str, NodeDefinition] = {}
        bindings: dict[str, dict[str, list[dict]]] = {}

        def visit(identity: str, target: str) -> None:
            require(identity not in visiting, "graph_dependency_cycle", "Graph has a dependency cycle",
                    node_id=identity, dependent_outputs=[target])
            if identity in closure:
                return
            require(identity in nodes, "graph_missing_node", "Dependency node is missing",
                    node_id=identity, dependent_outputs=[target])
            node = nodes[identity]
            entry = self.registry.get(node["component_id"], node["component_version"])
            require(entry is not None and (entry.executor is not None or entry.executor_ref is not None
                    and self.registry.executors.get(entry.executor_ref) is not None),
                    "graph_missing_node_type", "Node implementation is missing or unavailable",
                    node_id=identity, dependent_outputs=[target])
            try:
                self.registry.validate_config(entry, node["config"])
                for reference in entry.definition.config_node_references:
                    value = node["config"].get(reference["config_field"])
                    identities = value if reference["multiple"] else [value]
                    require(type(identities) is list and bool(identities),
                            "graph_invalid_config_reference", "Node binding must name explicit node identities")
                    allowed_types = {(item["component_id"], item["component_version"])
                                     for item in reference["target_types"]}
                    for referenced_id in identities:
                        uuid4_string(referenced_id)
                        referenced_node = nodes.get(referenced_id)
                        require(referenced_node is not None, "graph_missing_config_reference",
                                "Configured node binding names a missing node")
                        require((referenced_node["component_id"], referenced_node["component_version"])
                                in allowed_types, "graph_config_reference_type_mismatch",
                                "Configured node binding differs from its declared target type")
                    require(len(set(identities)) == len(identities), "graph_invalid_config_reference",
                            "Configured node bindings must not repeat an identity")
                input_ports, output_ports = entry.definition.ports(node["config"])
                if external_inputs is not None and entry.external_inputs_validator is not None:
                    entry.external_inputs_validator(copy.deepcopy(node["config"]), copy.deepcopy(external_inputs))
            except Exception as exc:
                code = getattr(exc, "reason_code", getattr(exc, "code", "graph_invalid_config"))
                raise GraphDiagnosticError(code, str(exc), node_id=identity, dependent_outputs=[target]) from exc
            require(set(node.get("public_outputs", [])) <= {port.port_id for port in output_ports},
                    "graph_unknown_port", "Public output is not declared", node_id=identity)
            visiting.add(identity)
            for edge in sorted(control_incoming.get(identity, []), key=lambda edge: edge["edge_id"]):
                visit(edge["source_node_id"], target)
            port_by_id = {port.port_id: port for port in input_ports}
            node_bindings: dict[str, list[dict]] = {}
            for edge in sorted(incoming.get(identity, []), key=lambda edge: (edge["order"], edge["edge_id"])):
                port_id = edge["target_port_id"]
                require(port_id in port_by_id, "graph_unknown_port", "Target input port is not declared",
                        node_id=identity, port_id=port_id, edge_id=edge["edge_id"], dependent_outputs=[target])
                visit(edge["source_node_id"], target)
                source = nodes[edge["source_node_id"]]
                source_entry = self.registry.get(source["component_id"], source["component_version"])
                source_ports = {port.port_id: port for port in source_entry.definition.ports(source["config"])[1]}
                require(edge["source_port_id"] in source_ports,
                        "graph_unknown_port", "Source output port is not declared", node_id=identity,
                        port_id=port_id, edge_id=edge["edge_id"], dependent_outputs=[target])
                require((source_ports[edge["source_port_id"]].data_type,
                         source_ports[edge["source_port_id"]].data_schema_version)
                        == (port_by_id[port_id].data_type, port_by_id[port_id].data_schema_version),
                        "graph_type_mismatch", "Linked content types differ", node_id=identity,
                        port_id=port_id, edge_id=edge["edge_id"], dependent_outputs=[target])
                node_bindings.setdefault(port_id, []).append(copy.deepcopy(edge))
            for port in input_ports:
                edges = node_bindings.get(port.port_id, [])
                require(not port.required or bool(edges), "graph_required_input_missing",
                        "Required input is not connected", node_id=identity, port_id=port.port_id,
                        dependent_outputs=[target])
                require(port.multiple or len(edges) <= 1, "graph_input_cardinality",
                        "Input accepts only one source", node_id=identity, port_id=port.port_id,
                        dependent_outputs=[target])
                require(len({edge["order"] for edge in edges}) == len(edges), "graph_input_order_conflict",
                        "Input source order is repeated", node_id=identity, port_id=port.port_id,
                        dependent_outputs=[target])
            definitions[identity], bindings[identity] = entry.definition, node_bindings
            visiting.remove(identity)
            closure.add(identity)
            order.append(identity)

        try:
            for target in targets:
                visit(target, target)
        except GraphDiagnosticError as exc:
            for diagnostic in exc.diagnostics:
                diagnostic["dependent_outputs"] = affected_outputs.get(
                    diagnostic["node_id"], diagnostic["dependent_outputs"],
                )
            raise
        object_bindings = {raw["object_key"]: ObjectBinding.from_dict(raw)
                           for raw in document.get("object_bindings", [])}
        object_accesses: dict[str, dict[str, bool]] = {}
        for identity in order:
            node = nodes[identity]
            for access in definitions[identity].object_accesses:
                value = node["config"].get(access["config_field"])
                keys = value if access["multiple"] else [value]
                require(type(keys) is list and all(bounded_name(key) for key in keys),
                        "graph_invalid_object_access", "Object access config must name explicit object keys",
                        node_id=identity)
                for key in keys:
                    binding = object_bindings.get(key)
                    require(binding is not None, "session_object_unbound", "Declared object is not bound",
                            node_id=identity)
                    require((binding.type_id, binding.schema_version) == (
                        access["type_id"], access["schema_version"]),
                        "session_object_type_mismatch", "Object binding differs from its node declaration",
                        node_id=identity)
                    require((access["access"] == "write" or identity in binding.readers)
                            and (access["access"] == "read" or identity in binding.writers),
                            "session_object_access_denied", "Node lacks declared object access", node_id=identity)
                    writes = access["access"] != "read"
                    accesses = object_accesses.setdefault(key, {})
                    accesses[identity] = accesses.get(identity, False) or writes
        ancestors: dict[str, set[str]] = {}
        for identity in order:
            predecessors = [edge["source_node_id"] for edge in incoming.get(identity, [])]
            predecessors += [edge["source_node_id"] for edge in control_incoming.get(identity, [])]
            ancestors[identity] = set(predecessors)
            for predecessor in predecessors:
                ancestors[identity].update(ancestors[predecessor])
        for key, accesses in object_accesses.items():
            members = list(accesses)
            for index, identity in enumerate(members):
                for previous in members[:index]:
                    require(not (accesses[identity] or accesses[previous])
                            or previous in ancestors[identity] or identity in ancestors[previous],
                            "graph_unordered_object_access",
                            f"Object {key} has unordered read/write nodes; add an explicit dependency",
                            node_id=identity, dependent_outputs=affected_outputs.get(identity, []))
        return GraphPlan(document, tuple(targets), tuple(order), bindings, definitions)
