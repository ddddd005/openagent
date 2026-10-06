"""Public contracts for trusted packages and persistent objects.

The host validates identities, permissions and registered schemas. Algorithms
and semantic validation belong to the registered implementation. Package
release, component, data schema and envelope versions are independent.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from jsonschema import Draft202012Validator, ValidationError

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, validate_json_value


HOST_PROTOCOL_VERSION = 1
MAX_OBJECT_BYTES = 1_000_000
_MISSING = object()


class HostContractError(ContractValidationError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.reason_code, self.status_code = code, 400


def ensure(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise HostContractError(code, message)


def bounded_name(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 128 and value == value.strip()


def canonical_uuid(value: Any) -> bool:
    try:
        parsed = UUID(value) if type(value) is str else None
    except (ValueError, TypeError):
        parsed = None
    return parsed is not None and parsed.version == 4 and str(parsed) == value


@dataclass(frozen=True)
class InformationSourceReference:
    source_id: str
    exact_version: str

    def to_dict(self) -> dict:
        ensure(bounded_name(self.source_id) and bounded_name(self.exact_version),
               "information_invalid_source", "Information sources require exact identities and versions")
        return dict(vars(self))

    @classmethod
    def from_dict(cls, value: Any) -> InformationSourceReference:
        ensure(type(value) is dict and set(value) == {"source_id", "exact_version"},
               "information_invalid_source", "Information source reference fields differ")
        result = cls(**value)
        result.to_dict()
        return result


@dataclass(frozen=True)
class InformationSourceDefinition:
    reference: InformationSourceReference
    component_id: str
    component_version: str
    channel_id: str
    format_id: str
    format_version: int = 1
    item_schema: dict = field(default_factory=dict)
    discover_public: bool = False
    read_public: bool = False
    source_scope: str = "live"
    max_page_size: int = 100
    max_page_bytes: int = 1_000_000

    def to_dict(self) -> dict:
        ensure(isinstance(self.reference, InformationSourceReference)
               and all(bounded_name(value) for value in (
                   self.component_id, self.component_version, self.channel_id, self.format_id))
               and type(self.format_version) is int and 1 <= self.format_version <= 2**53 - 1
               and type(self.discover_public) is bool and type(self.read_public) is bool
               and self.source_scope in ("live", "history", "live_and_history")
               and type(self.max_page_size) is int and 1 <= self.max_page_size <= 1000
               and type(self.max_page_bytes) is int and 1 <= self.max_page_bytes <= 4_000_000,
               "information_invalid_source", "Information source declarations are invalid")
        validate_json_value(self.item_schema)
        ensure(type(self.item_schema) is dict, "information_invalid_source", "Item schema must be an object")
        Draft202012Validator.check_schema(self.item_schema)
        return {"source_ref": self.reference.to_dict(), **{
            key: copy.deepcopy(getattr(self, key)) for key in (
                "component_id", "component_version", "channel_id", "format_id", "format_version",
                "item_schema", "discover_public", "read_public", "source_scope",
                "max_page_size", "max_page_bytes",
            )}}


@dataclass(frozen=True)
class RegisteredInformationSource:
    definition: InformationSourceDefinition
    reader_factory: Callable | None
    history_reader: Callable | None


class InformationSourceRegistry:
    """Trusted read-only providers; declarations never hold provider content."""

    def __init__(self):
        self._sources: dict[InformationSourceReference, RegisteredInformationSource] = {}
        self._frozen = False

    def register(self, definition: InformationSourceDefinition,
                 reader_factory: Callable | None = None, history_reader: Callable | None = None) -> None:
        ensure(not self._frozen, "host_registry_frozen", "Information source registry is frozen")
        ensure(isinstance(definition, InformationSourceDefinition)
               and all(value is None or callable(value) for value in (reader_factory, history_reader)),
               "information_invalid_source", "Information readers must be callable")
        definition.to_dict()
        ensure(definition.reference not in self._sources,
               "information_duplicate_source", "Exact information source is already registered")
        ensure(not any((item.definition.component_id, item.definition.component_version,
                        item.definition.channel_id) ==
                       (definition.component_id, definition.component_version, definition.channel_id)
                       for item in self._sources.values()),
               "information_duplicate_channel", "Node information channel is already registered")
        ensure(history_reader is None or definition.source_scope in ("history", "live_and_history"),
               "information_invalid_source", "Historical reader needs a historical source declaration")
        ensure(reader_factory is None or definition.source_scope in ("live", "live_and_history"),
               "information_invalid_source", "Live reader needs a live source declaration")
        self._sources[definition.reference] = RegisteredInformationSource(
            copy.deepcopy(definition), reader_factory, history_reader)

    def get(self, reference: InformationSourceReference) -> RegisteredInformationSource | None:
        reference.to_dict()
        item = self._sources.get(reference)
        return None if item is None else RegisteredInformationSource(
            copy.deepcopy(item.definition), item.reader_factory, item.history_reader)

    def registrations(self) -> tuple[RegisteredInformationSource, ...]:
        return tuple(self.get(ref) for ref in sorted(
            self._sources, key=lambda ref: (ref.source_id, ref.exact_version)))

    def catalog(self) -> list[dict]:
        return [item.definition.to_dict() for item in self.registrations()]

    def detached(self, *, frozen: bool = False) -> InformationSourceRegistry:
        result = InformationSourceRegistry()
        result._sources = {key: RegisteredInformationSource(
            copy.deepcopy(item.definition), item.reader_factory, item.history_reader)
            for key, item in self._sources.items()}
        result._frozen = frozen
        return result


@dataclass(frozen=True)
class ServiceReference:
    service_id: str
    exact_version: str

    def to_dict(self) -> dict:
        ensure(bounded_name(self.service_id) and bounded_name(self.exact_version),
               "host_invalid_service", "Service references require an exact identity and version")
        return {"service_id": self.service_id, "exact_version": self.exact_version}

    @classmethod
    def from_dict(cls, value: Any) -> ServiceReference:
        ensure(type(value) is dict and set(value) == {"service_id", "exact_version"},
               "host_invalid_service", "Service reference fields differ")
        result = cls(**value)
        result.to_dict()
        return result


@dataclass(frozen=True)
class ServiceOperation:
    capability: str
    operation: str
    request_schema: dict = field(default_factory=lambda: {"type": "object"})
    response_schema: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        ensure(bounded_name(self.capability) and bounded_name(self.operation),
               "host_invalid_service", "Service operations require bounded names")
        for schema in (self.request_schema, self.response_schema):
            validate_json_value(schema)
            ensure(type(schema) is dict, "host_invalid_service", "Service schemas require objects")
            Draft202012Validator.check_schema(schema)
        return {"capability": self.capability, "operation": self.operation,
                "request_schema": copy.deepcopy(self.request_schema),
                "response_schema": copy.deepcopy(self.response_schema)}


@dataclass(frozen=True)
class ServiceDefinition:
    reference: ServiceReference
    operations: tuple[ServiceOperation, ...]
    protocol_version: int = HOST_PROTOCOL_VERSION

    def to_dict(self) -> dict:
        ensure(isinstance(self.reference, ServiceReference)
               and type(self.protocol_version) is int
               and self.protocol_version == HOST_PROTOCOL_VERSION,
               "host_service_protocol_mismatch", "Service protocol is not supported")
        ensure(type(self.operations) is tuple and 0 < len(self.operations) <= 256
               and all(isinstance(item, ServiceOperation) for item in self.operations),
               "host_invalid_service", "Service operations require public declarations")
        operations = [item.to_dict() for item in self.operations]
        ensure(len({(item["capability"], item["operation"]) for item in operations}) == len(operations),
               "host_duplicate_service_operation", "Service operations must be distinct")
        return {"service_ref": self.reference.to_dict(), "protocol_version": self.protocol_version,
                "operations": operations}


@dataclass(frozen=True)
class RegisteredService:
    definition: ServiceDefinition
    factory: Callable


class HostServiceRegistry:
    """Exact implementations and per-node operation grants, frozen per run."""

    def __init__(self):
        self._services: dict[ServiceReference, RegisteredService] = {}
        self._frozen = False

    def register(self, definition: ServiceDefinition, factory: Callable) -> None:
        ensure(not self._frozen, "host_registry_frozen", "The service registry is frozen")
        ensure(isinstance(definition, ServiceDefinition) and callable(factory),
               "host_invalid_service", "Service registration requires a definition and factory")
        definition.to_dict()
        ensure(definition.reference not in self._services, "host_duplicate_service",
               "Exact service version is already registered")
        self._services[definition.reference] = RegisteredService(copy.deepcopy(definition), factory)

    def get(self, reference: ServiceReference) -> RegisteredService | None:
        item = self._services.get(reference)
        return None if item is None else RegisteredService(copy.deepcopy(item.definition), item.factory)

    def grants(self, requirements: tuple[dict, ...], capabilities: tuple[str, ...]) -> dict:
        ensure(type(requirements) is tuple and len(requirements) <= 256,
               "host_invalid_service_requirement", "Service requirements must be a bounded tuple")
        result = {}
        for grant in requirements:
            ensure(type(grant) is dict and set(grant) == {
                "service_id", "exact_version", "capability", "operations",
            } and bounded_name(grant["capability"])
                and type(grant["operations"]) is list and 0 < len(grant["operations"]) <= 256
                and all(bounded_name(item) for item in grant["operations"])
                and len(set(grant["operations"])) == len(grant["operations"]),
                "host_invalid_service_requirement", "Service requirement fields are invalid")
            reference = ServiceReference.from_dict({key: grant[key] for key in ("service_id", "exact_version")})
            ensure(grant["capability"] in capabilities, "host_service_capability_denied",
                   "Node service grants require the declared capability")
            entry = self.get(reference)
            ensure(entry is not None, "host_service_missing", "Exact host service is unavailable")
            available = {(item.capability, item.operation): item for item in entry.definition.operations}
            for operation in grant["operations"]:
                key = grant["capability"], operation
                ensure(key in available, "host_service_operation_missing",
                       "Service does not declare this capability operation")
                ensure(key not in result, "host_service_route_conflict",
                       "Node declares more than one route for a capability operation")
                result[key] = (reference, available[key])
        return result

    def catalog(self) -> list[dict]:
        return [entry.definition.to_dict() for _, entry in sorted(
            self._services.items(), key=lambda item: (item[0].service_id, item[0].exact_version))]

    def registrations(self) -> tuple[RegisteredService, ...]:
        return tuple(RegisteredService(copy.deepcopy(item.definition), item.factory)
                     for item in self._services.values())

    def detached(self, *, frozen: bool = False) -> HostServiceRegistry:
        result = HostServiceRegistry()
        result._services = {ref: RegisteredService(copy.deepcopy(item.definition), item.factory)
                            for ref, item in self._services.items()}
        result._frozen = frozen
        return result


@dataclass(frozen=True)
class DataTypeDefinition:
    type_id: str
    schema_version: int
    schema: dict
    default_value: Any = field(default_factory=lambda: _MISSING)
    scope: str = "session"
    validator: Callable[[Any], Any] | None = None
    references: Callable[[Any], list[dict]] | None = None
    reference_mapper: Callable[[Any, dict], Any] | None = None
    migrator: Callable[[Any, int], Any] | None = None
    max_bytes: int = MAX_OBJECT_BYTES
    content_transformer: Callable[[Any, Callable, str], Any] | None = None
    artifact_bindings: Callable[[Any], list[dict]] | None = None
    write_validator: Callable[[Any, dict], Any] | None = None
    public_references: Callable[[Any], list[dict]] | None = None

    def to_dict(self) -> dict:
        value = {
            "type_id": self.type_id, "schema_version": self.schema_version,
            "scope": self.scope, "schema": copy.deepcopy(self.schema),
            "max_bytes": self.max_bytes, "has_default": self.default_value is not _MISSING,
            "has_validator": self.validator is not None,
            "has_references": self.references is not None,
            "has_reference_mapper": self.reference_mapper is not None,
            "has_migrator": self.migrator is not None,
        }
        if self.default_value is not _MISSING:
            value["default_value"] = copy.deepcopy(self.default_value)
        if self.content_transformer is not None:
            value["has_content_transformer"] = True
        if self.artifact_bindings is not None:
            value["has_artifact_bindings"] = True
        if self.write_validator is not None:
            value["has_write_validator"] = True
        if self.public_references is not None:
            value["has_public_references"] = True
        return value


def _copy_definition(value: DataTypeDefinition) -> DataTypeDefinition:
    # deepcopy(object()) changes identity; preserve the omitted-default sentinel.
    return DataTypeDefinition(
        value.type_id, value.schema_version, copy.deepcopy(value.schema),
        _MISSING if value.default_value is _MISSING else copy.deepcopy(value.default_value),
        value.scope, value.validator, value.references, value.reference_mapper,
        value.migrator, value.max_bytes, value.content_transformer, value.artifact_bindings,
        value.write_validator, value.public_references,
    )


class TypeRegistry:
    """Exact schema registration; no implicit 'latest' or schema conversion."""

    def __init__(self) -> None:
        self._types: dict[tuple[str, str, int], DataTypeDefinition] = {}
        self._frozen = False

    def register(self, definition: DataTypeDefinition) -> None:
        ensure(not self._frozen, "host_registry_frozen", "The active type registry is frozen")
        ensure(isinstance(definition, DataTypeDefinition) and bounded_name(definition.type_id)
               and type(definition.schema_version) is int and 1 <= definition.schema_version <= 2**53 - 1
               and definition.scope in ("content", "session", "global")
               and type(definition.max_bytes) is int and 0 < definition.max_bytes <= 4_000_000,
               "host_invalid_type", "Data type identity, scope or limit is invalid")
        key = definition.scope, definition.type_id, definition.schema_version
        ensure(key not in self._types, "host_duplicate_type", "Data type version is already registered")
        ensure(all(callback is None or callable(callback) for callback in (
            definition.validator, definition.references, definition.reference_mapper, definition.migrator,
            definition.content_transformer,
            definition.artifact_bindings,
            definition.write_validator,
            definition.public_references,
        )), "host_invalid_type", "Type implementation hooks must be callable")
        validate_json_value(definition.schema)
        ensure(type(definition.schema) is dict, "host_invalid_type", "Data schema must be an object")
        Draft202012Validator.check_schema(definition.schema)
        detached = _copy_definition(definition)
        if detached.default_value is not _MISSING:
            self._validate_definition(detached, detached.default_value)
        self._types[key] = detached

    def get(self, type_id: str, schema_version: int, *, scope: str | None = None) -> DataTypeDefinition | None:
        ensure(bounded_name(type_id) and type(schema_version) is int and 1 <= schema_version <= 2**53 - 1
               and (scope is None or scope in ("content", "session", "global")),
               "host_invalid_type", "Type lookup requires an exact identity, schema version and scope")
        if scope is not None:
            value = self._types.get((scope, type_id, schema_version))
        else:
            matches = [item for (_, identity, version), item in self._types.items()
                       if identity == type_id and version == schema_version]
            ensure(len(matches) <= 1, "host_ambiguous_type", "Type exists in multiple scopes; choose a scope")
            value = matches[0] if matches else None
        return _copy_definition(value) if value is not None else None

    def _required(self, type_id: str, schema_version: int, scope: str) -> DataTypeDefinition:
        definition = self.get(type_id, schema_version, scope=scope)
        ensure(definition is not None, "host_unknown_type", "Required data type version is unavailable")
        return definition

    @staticmethod
    def _validate_definition(definition: DataTypeDefinition, value: Any) -> Any:
        validate_json_value(value)
        ensure(len(canonical_bytes(value)) <= definition.max_bytes,
               "host_value_too_large", "Data value exceeds the registered limit")
        if definition.scope == "content":
            ensure(type(value) is dict and type(value.get("schema_version")) is int
                   and value["schema_version"] == definition.schema_version,
                   "host_invalid_content", "Content envelope version does not match its port declaration")
        try:
            Draft202012Validator(definition.schema).validate(value)
        except ValidationError as exc:
            raise HostContractError("host_schema_mismatch", "Data value does not match its registered schema") from exc
        if definition.validator is not None:
            definition.validator(copy.deepcopy(value))
        return copy.deepcopy(value)

    def validate(self, type_id: str, schema_version: int, value: Any, *, scope: str = "session") -> Any:
        return self._validate_definition(self._required(type_id, schema_version, scope), value)

    def validate_write(self, type_id: str, schema_version: int, value: Any, write_context: dict) -> Any:
        """Validate an object transition without letting a hook rewrite its value."""
        definition = self._required(type_id, schema_version, "session")
        ensure(type(write_context) is dict and set(write_context) == {
            "workflow_session_id", "object_key", "current_record", "operation", "resolve_artifact",
        } and canonical_uuid(write_context["workflow_session_id"])
            and bounded_name(write_context["object_key"])
            and write_context["operation"] in ("put", "delete", "initialize")
            and callable(write_context["resolve_artifact"])
            and (write_context["current_record"] is None
                 if write_context["operation"] == "initialize"
                 else type(write_context["current_record"]) is dict),
            "host_invalid_write_context", "Object write validation context is invalid")
        if write_context["operation"] == "delete":
            ensure(value is None, "host_invalid_write", "Deleted object values must be null")
            checked = None
        else:
            checked = self._validate_definition(definition, value)
        if definition.write_validator is not None:
            detached = {key: (item if key == "resolve_artifact" else copy.deepcopy(item))
                        for key, item in write_context.items()}
            definition.write_validator(copy.deepcopy(checked), detached)
        return copy.deepcopy(checked)

    def transform_content(self, type_id: str, schema_version: int, value: Any,
                          operation: Callable, *, scope: str) -> Any:
        ensure(scope in ("body", "all") and callable(operation), "host_invalid_transform",
               "Content transformation requires a declared scope and callable")
        definition = self._required(type_id, schema_version, "content")
        ensure(definition.content_transformer is not None, "host_transform_unavailable",
               "Content type does not declare editable fields")
        original = self._validate_definition(definition, value)
        derived = definition.content_transformer(original, operation, scope)
        return self._validate_definition(definition, derived)

    def default(self, type_id: str, schema_version: int, *, scope: str = "session") -> Any:
        definition = self._required(type_id, schema_version, scope)
        ensure(definition.default_value is not _MISSING,
               "host_default_missing", "Data type does not declare an initialization default")
        return self._validate_definition(definition, definition.default_value)

    def references(self, type_id: str, schema_version: int, value: Any, *, scope: str = "session") -> list[dict]:
        definition = self._required(type_id, schema_version, scope)
        checked = self._validate_definition(definition, value)
        return self._extract_references(definition, checked)

    @staticmethod
    def _extract_references(definition: DataTypeDefinition, checked: Any) -> list[dict]:
        refs = [] if definition.references is None else definition.references(copy.deepcopy(checked))
        validate_json_value(refs)
        ensure(type(refs) is list and len(refs) <= 4096 and all(type(ref) is dict for ref in refs),
               "host_invalid_references", "Registered reference declaration must return bounded JSON objects")
        return [validate_reference(ref) for ref in refs]

    def validate_and_references(self, type_id: str, schema_version: int, value: Any,
                                *, scope: str = "content") -> tuple[Any, list[dict]]:
        definition = self._required(type_id, schema_version, scope)
        checked = self._validate_definition(definition, value)
        return checked, self._extract_references(definition, checked)

    def public_artifact_references(self, type_id: str, schema_version: int, value: Any) -> list[dict]:
        """Only explicitly exported direct references grant consumer reads.

        Ordinary type references describe retention/provenance, never disclosure.
        Missing implementations fail closed, including disabled historical packages.
        """
        definition = self.get(type_id, schema_version, scope="content")
        ensure(definition is not None and definition.public_references is not None,
               "output_reference_denied", "Output type does not export public artifact references")
        checked = self._validate_definition(definition, value)
        refs = definition.public_references(copy.deepcopy(checked))
        ensure(type(refs) is list and len(refs) <= 4096,
               "host_invalid_references", "Public artifact references must be bounded")
        refs = [validate_reference(ref) for ref in refs]
        retained = self._extract_references(definition, checked)
        ensure(all(ref["scope"] == "artifact" and ref in retained for ref in refs),
               "host_invalid_references", "Public references must be direct retained artifacts")
        return refs

    def content_artifact_bindings(self, type_id: str, schema_version: int, checked_value: Any) -> list[dict]:
        """Inspect binding evidence after validation at the execution boundary."""
        definition = self._required(type_id, schema_version, "content")
        result = ([] if definition.artifact_bindings is None
                  else definition.artifact_bindings(copy.deepcopy(checked_value)))
        validate_json_value(result)
        ensure(type(result) is list and len(result) <= 4096
               and all(type(ref) is dict and set(ref) == {"edge_id", "output_id", "order"}
                       and canonical_uuid(ref["edge_id"]) and canonical_uuid(ref["output_id"])
                       and type(ref["order"]) is int and 0 <= ref["order"] <= 2**53 - 1 for ref in result),
               "host_invalid_artifact_bindings", "Content artifact bindings require exact input evidence")
        return copy.deepcopy(result)

    def remap(self, type_id: str, schema_version: int, value: Any, mapping: dict,
              *, scope: str = "session") -> Any:
        definition = self._required(type_id, schema_version, scope)
        checked = self._validate_definition(definition, value)
        validate_json_value(mapping)
        ensure(type(mapping) is dict, "host_invalid_references", "Reference mapping must be an object")
        if not self.references(type_id, schema_version, checked, scope=scope):
            return checked
        ensure(definition.reference_mapper is not None, "host_reference_mapping_missing",
               "Referenced data needs an explicit mapping implementation")
        return self._validate_definition(definition, definition.reference_mapper(checked, copy.deepcopy(mapping)))

    def migrate(self, type_id: str, source_version: int, target_version: int, value: Any,
                *, scope: str = "session") -> Any:
        checked = self.validate(type_id, source_version, value, scope=scope)
        if source_version == target_version:
            return checked
        target = self._required(type_id, target_version, scope)
        ensure(target.migrator is not None, "host_migration_missing", "No explicit schema migration is registered")
        return self._validate_definition(target, target.migrator(checked, source_version))

    def catalog(self, *, scope: str | None = None) -> list[dict]:
        ensure(scope is None or scope in ("content", "session", "global"),
               "host_invalid_type", "Type catalog scope is invalid")
        return [definition.to_dict() for key, definition in sorted(self._types.items())
                if scope is None or key[0] == scope]

    def definitions(self) -> tuple[DataTypeDefinition, ...]:
        return tuple(_copy_definition(value) for _, value in sorted(self._types.items()))

    def detached(self, *, frozen: bool = False) -> TypeRegistry:
        registry = TypeRegistry()
        registry._types = {key: _copy_definition(value) for key, value in self._types.items()}
        registry._frozen = frozen
        return registry


@dataclass(frozen=True)
class ObjectBinding:
    object_key: str
    type_id: str
    schema_version: int
    scope: str
    readers: tuple[str, ...] = ()
    writers: tuple[str, ...] = ()
    owner_node_id: str | None = None
    default_value: Any = field(default_factory=lambda: _MISSING)

    @classmethod
    def from_dict(cls, value: Any) -> ObjectBinding:
        validate_json_value(value)
        required = {"object_key", "type_id", "schema_version", "scope", "readers", "writers", "owner_node_id"}
        ensure(type(value) is dict and required <= set(value) <= required | {"default_value"},
               "host_invalid_binding", "Object binding fields are invalid")
        ensure(bounded_name(value["object_key"]) and bounded_name(value["type_id"])
               and type(value["schema_version"]) is int and 1 <= value["schema_version"] <= 2**53 - 1
               and value["scope"] in ("shared", "private"),
               "host_invalid_binding", "Object binding identity, type or scope is invalid")
        for field_name in ("readers", "writers"):
            members = value[field_name]
            ensure(type(members) is list and len(members) <= 512
                   and all(canonical_uuid(member) for member in members) and len(set(members)) == len(members),
                   "host_invalid_binding", "Object permissions require unique node identities")
        owner = value["owner_node_id"]
        ensure(owner is None if value["scope"] == "shared" else canonical_uuid(owner),
               "host_invalid_binding", "Shared and private object ownership is invalid")
        if value["scope"] == "private":
            ensure(set(value["readers"]) <= {owner} and set(value["writers"]) <= {owner},
                   "host_invalid_binding", "Only the owner may access a private object")
        return cls(value["object_key"], value["type_id"], value["schema_version"], value["scope"],
                   tuple(value["readers"]), tuple(value["writers"]), owner,
                   copy.deepcopy(value["default_value"]) if "default_value" in value else _MISSING)

    def to_dict(self) -> dict:
        value = {
            "object_key": self.object_key, "type_id": self.type_id, "schema_version": self.schema_version,
            "scope": self.scope, "readers": list(self.readers), "writers": list(self.writers),
            "owner_node_id": self.owner_node_id,
        }
        if self.default_value is not _MISSING:
            value["default_value"] = copy.deepcopy(self.default_value)
        return value

    @property
    def has_default(self) -> bool:
        return self.default_value is not _MISSING


@dataclass(frozen=True)
class WriteIntent:
    object_key: str
    expected_revision: int
    operation_key: str
    value: Any = None
    operation: str = "put"

    def to_dict(self) -> dict:
        value = {"object_key": self.object_key, "expected_revision": self.expected_revision,
                 "operation_key": self.operation_key, "operation": self.operation,
                 "value": copy.deepcopy(self.value)}
        self.from_dict(value)
        return value

    @classmethod
    def from_dict(cls, value: Any) -> WriteIntent:
        validate_json_value(value)
        ensure(type(value) is dict and set(value) == {
            "object_key", "expected_revision", "operation_key", "operation", "value",
        } and bounded_name(value["object_key"]) and bounded_name(value["operation_key"])
            and type(value["expected_revision"]) is int and 0 <= value["expected_revision"] <= 2**53 - 1
            and value["operation"] in ("put", "delete")
            and (value["operation"] != "delete" or value["value"] is None),
            "host_invalid_write", "Object write intent is invalid")
        return cls(value["object_key"], value["expected_revision"], value["operation_key"],
                   copy.deepcopy(value["value"]), value["operation"])


@dataclass(frozen=True)
class ResourceIdentity:
    scope: str
    type_id: str
    resource_id: str

    def to_dict(self) -> dict:
        value = {"envelope_version": 1, "scope": self.scope,
                 "type_id": self.type_id, "resource_id": self.resource_id}
        self.from_dict(value)
        return value

    @classmethod
    def from_dict(cls, value: Any) -> ResourceIdentity:
        validate_json_value(value)
        ensure(type(value) is dict and set(value) == {"envelope_version", "scope", "type_id", "resource_id"}
               and type(value["envelope_version"]) is int and value["envelope_version"] == 1
               and bounded_name(value["scope"]) and value["scope"] not in ("session", "artifact")
               and bounded_name(value["type_id"])
               and canonical_uuid(value["resource_id"]),
               "host_invalid_resource_identity", "Resource identity requires a stable scope, type and UUID")
        return cls(value["scope"], value["type_id"], value["resource_id"])


def validate_reference(value: Any) -> dict:
    """References name immutable session/artifact values or current global IDs."""
    validate_json_value(value)
    ensure(type(value) is dict, "host_invalid_references", "Reference must be a JSON object")
    if value.get("scope") == "session":
        ensure(set(value) == {"scope", "workflow_session_id", "object_key", "revision_id"}
               and canonical_uuid(value["workflow_session_id"]) and bounded_name(value["object_key"])
               and canonical_uuid(value["revision_id"]),
               "host_invalid_references", "Session references require an exact revision and session identity")
    elif value.get("scope") == "artifact":
        ensure(set(value) == {"scope", "output_id"} and canonical_uuid(value["output_id"]),
               "host_invalid_references", "Artifact references require an exact output identity")
    else:
        ResourceIdentity.from_dict(value)
    return copy.deepcopy(value)
