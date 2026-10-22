"""Configuration-only declarations for one-time session initialization."""

from copy import deepcopy

from jsonschema import Draft202012Validator, SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012, UnknownDialect

from .contract_errors import DiagnosticError, require
from .contract_json import validate_json_value
from .host_sdk import HostContractError, ObjectBinding, TypeRegistry, bounded_name
from .variable_contract import VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE, validate_variable


MAX_SESSION_PRELOADS = 1024
_SPECIAL_CALLBACKS = (
    "validator", "references", "reference_mapper", "migrator", "content_transformer",
    "artifact_bindings", "write_validator", "public_references",
)


def _validate_local_schema_references(schema, *, object_key):
    resolver = Registry().resolver_with_root(DRAFT202012.create_resource(schema))
    pending = [(schema, resolver, DRAFT202012, False)]
    checked = set()
    try:
        while pending:
            current, scope, specification, referenced = pending.pop()
            require(type(current) in (dict, bool), "session_preload_type_unsupported",
                    "Session preload references must resolve to JSON schemas", object_key=object_key)
            if type(current) is bool:
                continue
            if referenced:
                Draft202012Validator.check_schema(current)
            identity = id(current), id(scope.lookup("#").contents), specification.name
            if identity in checked:
                continue
            checked.add(identity)
            for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                if keyword in current:
                    reference = current[keyword]
                    require(type(reference) is str and reference.startswith("#"),
                            "session_preload_type_unsupported",
                            "Session preload schema references must be local fragments",
                            object_key=object_key, keyword=keyword)
                    resolved = scope.lookup(reference)
                    Draft202012Validator.check_schema(resolved.contents)
                    pending.append((resolved.contents, resolved.resolver,
                                    specification.detect(resolved.contents), True))
            for child in specification.subresources_of(current):
                if type(child) in (dict, bool):
                    child_scope = scope.in_subresource(specification.create_resource(child))
                    pending.append((child, child_scope, specification.detect(child), False))
    except DiagnosticError:
        raise
    except (Unresolvable, SchemaError, UnknownDialect, ValueError) as exc:
        raise DiagnosticError(
            "session_preload_schema_reference_invalid",
            "Session preload schema contains an invalid or unresolved local reference",
            object_key=object_key) from exc


def session_preload_bindings(document: dict, types: TypeRegistry | None = None) -> dict:
    """Resolve admitted declarations without reading configuration initial values."""
    require(type(document) is dict, "session_preload_invalid",
            "Session preloads require a workflow document")
    if "session_preloads" not in document:
        return {}
    require(type(document.get("schema_version")) is int and document["schema_version"] == 3,
            "session_preload_invalid", "Session preloads require graph document schema 3")
    entries = document["session_preloads"]
    require(type(entries) is list and len(entries) <= MAX_SESSION_PRELOADS,
            "session_preload_invalid", "Session preloads must be a bounded array")
    require(types is None or isinstance(types, TypeRegistry), "session_preload_invalid",
            "Session preload validation requires the exact type registry")
    from .chat_contracts import CHAT_CONTEXT_TYPE
    from .context_contract import EFFECTIVE_CONTEXT_TYPE

    bindings = {}
    try:
        for raw in document.get("object_bindings", []):
            binding = ObjectBinding.from_dict(raw)
            require(binding.object_key not in bindings, "session_preload_invalid",
                    "Session preload bindings must have unique object keys")
            bindings[binding.object_key] = binding
        selected = {}
        for entry in entries:
            require(type(entry) is dict and set(entry) == {"object_key", "value"}
                    and bounded_name(entry["object_key"]),
                    "session_preload_invalid", "Session preload fields must be object_key and value")
            key = entry["object_key"]
            require(key not in selected, "session_preload_duplicate",
                    "Session preload object key is duplicated", object_key=key)
            binding = bindings.get(key)
            require(binding is not None, "session_preload_binding_missing",
                    "Session preload requires its exact object binding", object_key=key)
            require(not binding.has_default and binding.registrar_node_id is None,
                    "session_preload_source_conflict",
                    "Session preload cannot also have a binding default or registrar", object_key=key)
            variable = (binding.type_id, binding.schema_version) == (
                VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION)
            require(variable or binding.type_id not in (
                VARIABLE_TYPE, CHAT_CONTEXT_TYPE, EFFECTIVE_CONTEXT_TYPE),
                "session_preload_type_unsupported",
                "Special session type identities cannot be ordinary preloads",
                object_key=key, type_id=binding.type_id, schema_version=binding.schema_version)
            if types is not None:
                definition = types.get(binding.type_id, binding.schema_version, scope="session")
                require(definition is not None, "session_preload_type_unavailable",
                        "The exact session preload type is unavailable", object_key=key,
                        type_id=binding.type_id, schema_version=binding.schema_version)
                require(definition.checkpoint_admission, "session_preload_type_not_admitted",
                        "Session preload type must admit checkpoint restoration", object_key=key)
                unsupported = [
                    field for field in _SPECIAL_CALLBACKS
                    if getattr(definition, field) is not None and not (variable and field == "validator")
                ]
                require(not unsupported, "session_preload_type_unsupported",
                        "Session preload type requires unsupported special behavior",
                        object_key=key, callbacks=unsupported)
                _validate_local_schema_references(definition.schema, object_key=key)
            selected[key] = binding
        return selected
    except HostContractError as exc:
        raise DiagnosticError(exc.reason_code, str(exc)) from exc


def validate_session_preloads(document: dict, types: TypeRegistry | None = None) -> dict:
    """Return detached initial values, without changing the saved document.

    Structural readers may omit ``types``. Configuration and session writers
    supply the exact registry to enforce admission and validate the real value.
    Historical readers use ``session_preload_bindings`` instead.
    """
    bindings = session_preload_bindings(document, types)
    values = {}
    try:
        for entry in document.get("session_preloads", []):
            key, value = entry["object_key"], entry["value"]
            binding = bindings[key]
            validate_json_value(value)
            if (binding.type_id, binding.schema_version) == (VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION):
                validate_variable(value)
                require(value["registered"] is True, "session_preload_variable_unregistered",
                        "A preloaded variable must already be registered", object_key=key)
            if types is not None:
                value = types.validate(binding.type_id, binding.schema_version, value, scope="session")
            values[key] = deepcopy(value)
        return values
    except HostContractError as exc:
        raise DiagnosticError(exc.reason_code, str(exc)) from exc
    except (Unresolvable, RecursionError) as exc:
        raise DiagnosticError(
            "session_preload_schema_reference_invalid",
            "Session preload schema contains an unresolved local reference") from exc
