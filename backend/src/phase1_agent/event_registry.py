"""Exact behavior declarations frozen before a run, never mutable event history."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping
from uuid import UUID

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, validate_json_value
from .contracts_v2 import validate_record
from .event_contracts import (
    CORE_EVENT_TYPES, CORE_PAYLOAD_SCHEMAS, RESERVED_EVENT_TYPES, core_payload_schema_ref,
    validate_core_payload_correspondence, validate_event_type,
    validate_payload, validate_payload_schema, validate_schema_ref,
)


class UnsupportedEventDeclaration(ContractValidationError):
    """A required exact event or schema version is not available."""

    reason_code = "unsupported"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def _uuid(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _names(value: Any, description: str) -> list[str]:
    _require(isinstance(value, (tuple, list, frozenset, set))
             and all(type(item) is str and bool(item.strip()) for item in value),
             f"{description} must be nonempty names")
    _require(len(value) == len(set(value)), f"{description} must not contain duplicates")
    return sorted(value)


def _producer(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {
        "kind", "component_id", "component_version", "contract_version", "capabilities",
    }, "Event producer must be an exact component descriptor")
    _require(type(value["kind"]) is str and bool(value["kind"].strip())
             and _uuid(value["component_id"])
             and type(value["component_version"]) is str and bool(value["component_version"].strip()),
             "Event producer component identity is invalid")
    _require(type(value["contract_version"]) is int and value["contract_version"] == 1,
             "Unsupported event producer contract version")
    _require(type(value["capabilities"]) is list, "Producer capabilities must be a JSON array")
    result = deepcopy(value)
    result["capabilities"] = _names(value["capabilities"], "Producer capabilities")
    return result


def _event_ref(value: Mapping[str, Any]) -> tuple[str, int]:
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {"event_type", "event_version"},
             "Behavior reference requires an exact type and event version")
    name = validate_event_type(value["event_type"])
    _require(type(value["event_version"]) is int and value["event_version"] >= 1,
             "Event version must be a positive integer")
    return name, value["event_version"]


def _declaration(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {
        "event_type", "event_version", "payload_schema_ref", "payload_schema",
        "producer", "required_capabilities",
    }, "Behavior declaration fields must be exact")
    name, version = _event_ref({
        "event_type": value["event_type"], "event_version": value["event_version"],
    })
    _require(name not in RESERVED_EVENT_TYPES, "Custom behavior cannot declare a core event")
    ref = validate_schema_ref(value["payload_schema_ref"])
    _require(not ref["schema_id"].startswith("run-event."),
             "Custom behavior cannot use a core payload schema identity")
    schema = validate_payload_schema(value["payload_schema"])
    producer = _producer(value["producer"])
    _require(type(value["required_capabilities"]) is list,
             "Required event capabilities must be a JSON array")
    required = _names(value["required_capabilities"], "Required event capabilities")
    _require({"events", *required} <= set(producer["capabilities"]),
             "Event producer is missing declared capabilities")
    return {
        "event_type": name, "event_version": version,
        "payload_schema_ref": ref, "payload_schema": schema,
        "producer": producer, "required_capabilities": required,
    }


def _core_declaration(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_json_value(value)
    _require(type(value) is dict and set(value) == {
        "event_type", "event_version", "payload_schema_ref", "payload_schema",
    }, "Frozen core event declaration fields must be exact")
    name = validate_event_type(value["event_type"])
    if (name not in CORE_EVENT_TYPES or type(value["event_version"]) is not int
            or value["event_version"] != 1
            or validate_schema_ref(value["payload_schema_ref"]) != core_payload_schema_ref(name)):
        raise UnsupportedEventDeclaration("Unsupported exact core event declaration version")
    schema = validate_payload_schema(value["payload_schema"])
    _require(canonical_bytes(schema) == canonical_bytes(CORE_PAYLOAD_SCHEMAS[name]),
             "Frozen core schema differs from its exact supported version")
    return {
        "event_type": name, "event_version": 1,
        "payload_schema_ref": core_payload_schema_ref(name), "payload_schema": schema,
    }


class FrozenEventDeclarations:
    """JSON-only schema basis selected once; reports cannot read registry latest."""

    def __init__(self, value: Mapping[str, Any]) -> None:
        validate_json_value(value)
        _require(type(value) is dict and set(value) == {
            "schema_version", "kind", "producer", "core_events", "behaviors",
        }, "Frozen event declaration fields must be exact")
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != "frozen_run_event_declarations"):
            raise UnsupportedEventDeclaration("Unsupported frozen event declaration version")
        producer = _producer(value["producer"])
        _require(type(value["core_events"]) is list, "Frozen core events must be a JSON array")
        core = [_core_declaration(item) for item in value["core_events"]]
        core_names = [item["event_type"] for item in core]
        _require(len(core_names) == len(set(core_names)), "Duplicate frozen core event selection")
        _require(type(value["behaviors"]) is list, "Frozen behaviors must be a JSON array")
        declarations = [_declaration(item) for item in value["behaviors"]]
        names = [item["event_type"] for item in declarations]
        _require(len(names) == len(set(names)), "One run cannot select multiple versions of one event type")
        _require(all(item["producer"] == producer for item in declarations),
                 "Frozen behavior producer differs from the selected component")
        self._value = {
            "schema_version": 1, "kind": "frozen_run_event_declarations",
            "producer": producer, "core_events": sorted(core, key=lambda item: item["event_type"]),
            "behaviors": sorted(declarations, key=lambda item: item["event_type"]),
        }
        self._behaviors = {item["event_type"]: item for item in declarations}
        self._core_events = {item["event_type"]: item for item in core}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> FrozenEventDeclarations:
        """Parse fixed evidence; registry.restore separately verifies availability."""
        return cls(value)

    def to_json(self) -> dict[str, Any]:
        return deepcopy(self._value)

    @property
    def producer(self) -> dict[str, Any]:
        return deepcopy(self._value["producer"])

    @property
    def core_event_types(self) -> frozenset[str]:
        return frozenset(item["event_type"] for item in self._value["core_events"])

    @property
    def behavior_types(self) -> frozenset[str]:
        return frozenset(self._behaviors)

    def validate_core(
        self, event_type: str, payload_schema_ref: Mapping[str, Any], payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        _require(event_type in self.core_event_types, "Core event is not declared for this run")
        declaration = self._core_events[event_type]
        _require(validate_schema_ref(payload_schema_ref) == declaration["payload_schema_ref"],
                 "Core event payload schema differs from the frozen exact version")
        value = validate_payload(declaration["payload_schema"], payload)
        return validate_core_payload_correspondence(event_type, value)

    def validate_report(
        self, event_type: str, payload_schema_ref: Mapping[str, Any], payload: Mapping[str, Any],
        *, producer: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Producer-owned custom data cannot claim coordinator state or success."""
        validate_event_type(event_type)
        _require(event_type not in RESERVED_EVENT_TYPES, "Behavior report cannot impersonate a core event")
        declaration = self._behaviors.get(event_type)
        if declaration is None:
            raise UnsupportedEventDeclaration("Behavior event is not frozen for this run")
        _require(validate_schema_ref(payload_schema_ref) == declaration["payload_schema_ref"],
                 "Behavior payload schema differs from the frozen exact version")
        if producer is not None:
            _require(_producer(producer) == self._value["producer"],
                     "Behavior report producer differs from the frozen component")
        return validate_payload(declaration["payload_schema"], payload)


class EventBehaviorRegistry:
    """Register exact event versions; only selected immutable declarations reach runs."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[tuple[str, int], dict[str, Any]] = {}

    def register(
        self, event_type: str, event_version: int, *, payload_schema_ref: Mapping[str, Any],
        payload_schema: Mapping[str, Any], producer: Mapping[str, Any],
        required_capabilities: Sequence[str] = (),
    ) -> None:
        required = _names(required_capabilities, "Required event capabilities")
        declaration = _declaration({
            "event_type": event_type, "event_version": event_version,
            "payload_schema_ref": payload_schema_ref, "payload_schema": payload_schema,
            "producer": producer, "required_capabilities": required,
        })
        key = event_type, event_version
        with self._lock:
            _require(key not in self._entries, "Duplicate or conflicting behavior event declaration")
            for existing in self._entries.values():
                if existing["payload_schema_ref"] == declaration["payload_schema_ref"]:
                    _require(canonical_bytes(existing["payload_schema"]) == canonical_bytes(declaration["payload_schema"]),
                             "Payload schema identity was reused with conflicting content")
            self._entries[key] = declaration

    def unregister(self, event_type: str, event_version: int) -> None:
        key = _event_ref({"event_type": event_type, "event_version": event_version})
        with self._lock:
            if key not in self._entries:
                raise UnsupportedEventDeclaration("Behavior event version is not registered")
            del self._entries[key]

    def freeze(
        self, *, producer: Mapping[str, Any],
        event_refs: Sequence[Mapping[str, Any]] = (),
        core_event_types: Sequence[str] = tuple(sorted(CORE_EVENT_TYPES)),
    ) -> FrozenEventDeclarations:
        producer = _producer(producer)
        _require(isinstance(event_refs, (tuple, list)), "Event selections must be a list or tuple")
        keys = [_event_ref(ref) for ref in event_refs]
        _require(len(keys) == len(set(keys)), "Duplicate frozen behavior selection")
        _require(len({key[0] for key in keys}) == len(keys),
                 "One run cannot select multiple versions of one event type")
        core = _names(core_event_types, "Core event selections")
        _require(set(core) <= CORE_EVENT_TYPES, "Unknown frozen core event type")
        with self._lock:
            declarations = []
            for key in keys:
                declaration = self._entries.get(key)
                if declaration is None:
                    raise UnsupportedEventDeclaration("Selected exact behavior event version is unavailable")
                _require(declaration["producer"] == producer,
                         "Selected event producer version or capabilities differ")
                declarations.append(deepcopy(declaration))
        return FrozenEventDeclarations({
            "schema_version": 1, "kind": "frozen_run_event_declarations",
            "producer": producer, "core_events": [
                {
                    "event_type": name, "event_version": 1,
                    "payload_schema_ref": core_payload_schema_ref(name),
                    "payload_schema": deepcopy(CORE_PAYLOAD_SCHEMAS[name]),
                }
                for name in core
            ],
            "behaviors": declarations,
        })

    def restore(self, value: Mapping[str, Any]) -> FrozenEventDeclarations:
        """Re-resolve exact frozen declarations; absent versions never use latest."""
        frozen = FrozenEventDeclarations.from_json(value)
        with self._lock:
            for declaration in frozen.to_json()["behaviors"]:
                key = declaration["event_type"], declaration["event_version"]
                current = self._entries.get(key)
                if current is None:
                    raise UnsupportedEventDeclaration("Frozen exact behavior event version is unavailable")
                _require(canonical_bytes(current) == canonical_bytes(declaration),
                         "Frozen behavior declaration differs from its registered version")
        return frozen

    def detached(self) -> EventBehaviorRegistry:
        registry = EventBehaviorRegistry()
        with self._lock:
            registry._entries = deepcopy(self._entries)
        return registry


@dataclass(frozen=True)
class ReportScope:
    """Actual run ownership from fixed records, not names or editor locations."""

    workflow_session_id: str
    node_binding_id: str
    run_id: str
    chain_run_id: str
    workflow_definition_id: str
    workflow_definition_revision: int

    def __post_init__(self) -> None:
        _require(all(_uuid(getattr(self, name)) for name in (
            "workflow_session_id", "node_binding_id", "run_id", "chain_run_id",
            "workflow_definition_id",
        )), "Event report scope identities must be canonical UUID4")
        _require(type(self.workflow_definition_revision) is int
                 and self.workflow_definition_revision > 0,
                 "Event report scope requires a fixed definition revision")

    @classmethod
    def from_records(
        cls, session: Mapping[str, Any], binding: Mapping[str, Any],
        run: Mapping[str, Any], chain: Mapping[str, Any] | None = None,
    ) -> ReportScope:
        session = validate_record("workflow_session", session)
        binding = validate_record("node_binding", binding)
        _require(type(run) is dict and run.get("profile") in {"agent", "node"},
                 "Event reporting requires an actual run record")
        run = validate_record("run_record" if run["profile"] == "agent" else "node_run", run)
        _require(
            run["workflow_session_id"] == session["workflow_session_id"]
            and run["node_binding_id"] == binding["node_binding_id"]
            and binding["workflow_definition_id"] == session["workflow_definition_id"]
            and binding["workflow_definition_revision"] == session["definition_revision"],
            "Event report run, binding, and fixed workflow definition differ",
        )
        if chain is not None:
            chain = validate_record("chain_run", chain)
            _require(chain["chain_run_id"] == run["chain_run_id"]
                     and chain["workflow_session_id"] == run["workflow_session_id"]
                     and run["run_id"] in chain["node_run_ids"],
                     "Event report run does not belong to this chain")
        return cls(
            run["workflow_session_id"], run["node_binding_id"], run["run_id"],
            run["chain_run_id"], session["workflow_definition_id"], session["definition_revision"],
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "workflow_session_id": self.workflow_session_id,
            "node_binding_id": self.node_binding_id, "run_id": self.run_id,
            "chain_run_id": self.chain_run_id,
            "workflow_definition_id": self.workflow_definition_id,
            "workflow_definition_revision": self.workflow_definition_revision,
        }

    def validate_event(self, value: Mapping[str, Any]) -> dict[str, Any]:
        event = validate_record("run_event", value)
        _require(event["schema_version"] == 2, "Legacy v1 events cannot be published as live v2 events")
        _require(all(event[name] == getattr(self, name) for name in (
            "workflow_session_id", "node_binding_id", "run_id",
        )), "Event source ownership differs from the actual run scope")
        return event
