"""Read-only registration projections and exact invocation information routes.

Providers own content, retention and cursors. This module retains declarations,
invocation identities and reader routes only.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import dataclass
from typing import Callable

from jsonschema import Draft202012Validator, ValidationError

from .contract_json import canonical_bytes, validate_json_value
from .host_sdk import (
    HostContractError, InformationSourceReference, InformationSourceRegistry, ensure,
)
from .runtime_hosting import InvocationOwner


_CURSOR_KEY = secrets.token_bytes(32)
_KINDS = {
    "nodes": "node", "data_types": "data_type", "executors": "executor",
    "pause_support": "pause_support", "services": "service",
    "frontend_extensions": "frontend_extension", "information_sources": "information_source",
}


def _reference(kind: str, value: dict) -> dict:
    if kind == "node":
        return {key: value[key] for key in ("component_id", "component_version")}
    if kind == "data_type":
        return {key: value[key] for key in ("scope", "type_id", "schema_version")}
    if kind in ("executor", "pause_support"):
        return copy.deepcopy(value.get("executor_ref", value))
    if kind == "service":
        return copy.deepcopy(value.get("service_ref", value))
    if kind == "information_source":
        return copy.deepcopy(value.get("source_ref", value))
    return {"extension_id": value["extension_id"]}


def registration_catalog(registry, *, frontend_extensions=(), package_manifests=()) -> list[dict]:
    """Aggregate existing registries without introducing writable duplicates."""
    packages = {}
    for manifest in package_manifests:
        for exported, declarations in manifest["exports"].items():
            kind = _KINDS[exported]
            for declaration in declarations:
                key = kind, canonical_bytes(declaration)
                package = (manifest["package_id"], manifest["version"])
                previous = packages.get(key)
                ensure(previous is None or previous == package or kind == "data_type",
                       "information_package_conflict", "Registration has conflicting package ownership")
                packages.setdefault(key, package)
    sources = registry.information_sources.catalog()
    catalogs = (
        ("node", registry.catalog()), ("data_type", registry.data_types.catalog()),
        ("executor", registry.executors.catalog()), ("pause_support", registry.executors.pause_catalog()),
        ("service", registry.services.catalog()), ("frontend_extension", frontend_extensions),
        ("information_source", sources),
    )
    result = []
    for kind, declarations in catalogs:
        for declaration in declarations:
            reference = _reference(kind, declaration)
            package = packages.get((kind, canonical_bytes(reference)))
            value = {
                "kind": kind, "registration_ref": reference,
                "package_id": None if package is None else package[0],
                "package_version": None if package is None else package[1],
                "declaration": copy.deepcopy(declaration),
                "discover_public": declaration.get("discover_public", kind != "information_source"),
                "read_public": declaration.get("read_public", False),
                "availability": "unbound" if kind == "information_source" else "registered",
            }
            if kind == "node":
                value["information_sources"] = [
                    copy.deepcopy(item["source_ref"]) for item in sources
                    if (item["component_id"], item["component_version"]) ==
                       (declaration["component_id"], declaration["component_version"])
                ]
                value["has_information_sources"] = bool(value["information_sources"])
            result.append(value)
    return sorted(result, key=lambda item: (item["kind"], canonical_bytes(item["registration_ref"])))


def _audience(value: str) -> None:
    ensure(value in ("management", "consumer"), "information_invalid_audience",
           "Information access requires a management or consumer audience")


def paginate_registrations(entries: list[dict], *, limit: int = 100,
                           cursor: str | None = None, filters: dict | None = None,
                           audience: str = "management", query_scope: dict | None = None) -> dict:
    _audience(audience)
    ensure(type(limit) is int and 1 <= limit <= 1000,
           "information_invalid_limit", "Directory page size must be between 1 and 1000")
    filters = {} if filters is None else filters
    ensure(type(filters) is dict and set(filters) <= {
        "kind", "package_id", "node_id", "chain_id", "session_id", "channel_id"},
        "information_invalid_filter", "Registration filters contain unsupported fields")
    validate_json_value(filters)
    ensure(all(type(value) is str and 0 < len(value) <= 128 for value in filters.values()),
           "information_invalid_filter", "Registration filters require bounded identities")
    # Invocation query coordinates bind cursors without filtering out the static
    # registrations that remain visible beside session-scoped bindings.
    query_scope = {} if query_scope is None else query_scope
    ensure(type(query_scope) is dict and set(query_scope) <= {"session_id", "chain_id", "node_id"}
           and all(type(value) is str and 0 < len(value) <= 128 for value in query_scope.values()),
           "information_invalid_filter", "Registration query scope requires bounded identities")
    validate_json_value(query_scope)
    public_refs = {canonical_bytes(entry["registration_ref"]) for entry in entries
                   if entry["kind"] == "information_source" and entry["discover_public"]}
    items = []
    for original in entries:
        if audience == "consumer" and not original["discover_public"]:
            continue
        item = copy.deepcopy(original)
        if audience == "consumer":
            # A public node type must not reveal its private channel references.
            if "information_sources" in item:
                item["information_sources"] = [ref for ref in item["information_sources"]
                                               if canonical_bytes(ref) in public_refs]
                item["has_information_sources"] = bool(item["information_sources"])
        owner = item.get("owner", {})
        values = {**item, "node_id": owner.get("node_binding_id"),
                  "chain_id": owner.get("chain_run_id"),
                  "session_id": owner.get("workflow_session_id"),
                  "channel_id": item.get("declaration", {}).get("channel_id")}
        if all(values.get(key) == value for key, value in filters.items()):
            items.append(item)
    signature = hashlib.sha256(canonical_bytes({
        "audience": audience, "filters": filters, "query_scope": query_scope,
        "items": items, "limit": limit,
    })).hexdigest()
    offset = 0
    if cursor is not None:
        ensure(type(cursor) is str and 0 < len(cursor) <= 4096,
               "information_invalid_cursor", "Directory cursor must be bounded opaque text")
        try:
            decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
            payload, supplied = decoded[:-32], decoded[-32:]
            ensure(hmac.compare_digest(hmac.digest(_CURSOR_KEY, payload, "sha256"), supplied),
                   "information_invalid_cursor", "Directory cursor is invalid")
            position = json.loads(payload)
            ensure(type(position) is dict and set(position) == {"signature", "offset"}
                   and position["signature"] == signature
                   and type(position["offset"]) is int and 0 <= position["offset"] <= len(items),
                   "information_invalid_cursor", "Directory cursor no longer matches this query")
            offset = position["offset"]
        except (ValueError, TypeError, UnicodeError) as exc:
            raise HostContractError("information_invalid_cursor", "Directory cursor is invalid") from exc
    next_offset = offset + limit
    next_cursor = None
    if next_offset < len(items):
        payload = canonical_bytes({"signature": signature, "offset": next_offset})
        next_cursor = base64.urlsafe_b64encode(
            payload + hmac.digest(_CURSOR_KEY, payload, "sha256")).decode("ascii")
    return {"items": items[offset:next_offset], "next_cursor": next_cursor}


@dataclass
class _Binding:
    owner: InvocationOwner
    reference: InformationSourceReference
    generation: int
    reader: Callable | None
    history_reader: Callable | None
    released: bool = False


class InformationRouter:
    """Route reads to provider-owned live or historical data, never cache data."""

    def __init__(self, registry: InformationSourceRegistry):
        self.registry = registry.detached(frozen=True)
        self._bindings: dict[tuple, _Binding] = {}
        self._active: dict[tuple, int] = {}
        self._node_owners: dict[str, InvocationOwner] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _identities(reference, owner, generation):
        ref = (InformationSourceReference.from_dict(reference) if type(reference) is dict else reference)
        invocation = InvocationOwner.from_dict(owner) if type(owner) is dict else owner
        ensure(isinstance(ref, InformationSourceReference) and isinstance(invocation, InvocationOwner),
               "information_invalid_binding", "Binding requires exact source and invocation identities")
        ref.to_dict()
        invocation.to_dict()
        ensure(type(generation) is int and 1 <= generation <= 2**53 - 1,
               "information_invalid_generation", "Information binding requires a positive generation")
        return ref, invocation

    def bind(self, reference, owner, generation: int, *, reader=None, history_reader=None,
             handle=None, context=None) -> dict:
        ref, invocation = self._identities(reference, owner, generation)
        registered = self.registry.get(ref)
        ensure(registered is not None, "information_source_missing", "Exact information source is unavailable")
        definition = registered.definition
        with self._lock:
            self._require_new_binding(invocation, ref, generation)
        if reader is None and registered.reader_factory is not None:
            reader = registered.reader_factory(invocation.to_dict(), generation, handle, context)
        history_reader = registered.history_reader if history_reader is None else history_reader
        ensure(all(callback is None or callable(callback) for callback in (reader, history_reader)),
               "information_invalid_reader", "Provider must return a read-only callable")
        ensure(reader is None or definition.source_scope in ("live", "live_and_history"),
               "information_scope_mismatch", "Source does not declare live reads")
        ensure(history_reader is None or definition.source_scope in ("history", "live_and_history"),
               "information_scope_mismatch", "Source does not declare historical reads")
        with self._lock:
            key, active_key = (invocation, ref, generation), (invocation, ref)
            self._require_new_binding(invocation, ref, generation)
            previous_generation = self._active.get(active_key)
            if previous_generation is not None:
                previous = self._bindings[(invocation, ref, previous_generation)]
                previous.reader, previous.released = None, True
            self._bindings[key] = _Binding(invocation, ref, generation, reader, history_reader)
            self._active[active_key] = generation
            self._node_owners[invocation.node_run_id] = invocation
            return definition.to_dict()

    def _require_new_binding(self, invocation, ref, generation):
        ensure((invocation, ref, generation) not in self._bindings,
               "information_duplicate_binding", "Information binding already exists")
        existing_owner = self._node_owners.get(invocation.node_run_id)
        ensure(existing_owner is None or existing_owner == invocation,
               "information_owner_mismatch", "Node run belongs to a different information owner")
        ensure(generation > self._active.get((invocation, ref), 0),
               "information_stale_generation", "Information binding generation is obsolete")

    def bind_node(self, owner, generation: int, component_id: str, component_version: str,
                  *, handle=None, context=None) -> list[dict]:
        definitions = [item.definition for item in self.registry.registrations()
                       if (item.definition.component_id, item.definition.component_version) ==
                          (component_id, component_version)]
        return [self.bind(item.reference, owner, generation, handle=handle, context=context)
                for item in definitions]

    def restore(self, reference, owner, generation: int) -> None:
        """Restore a proven historical binding; callers supply durable evidence."""
        ref, invocation = self._identities(reference, owner, generation)
        with self._lock:
            registered = self.registry.get(ref)
            ensure(registered is not None and registered.definition.source_scope in ("history", "live_and_history"),
                   "information_history_unavailable", "Historical source is not registered")
            existing_owner = self._node_owners.get(invocation.node_run_id)
            ensure(existing_owner is None or existing_owner == invocation,
                   "information_owner_mismatch", "Node run belongs to a different information owner")
            key = invocation, ref, generation
            if key not in self._bindings:
                self._bindings[key] = _Binding(
                    invocation, ref, generation, None, registered.history_reader, True)
            self._node_owners[invocation.node_run_id] = invocation

    def release(self, owner, generation: int | None = None) -> None:
        invocation = InvocationOwner.from_dict(owner) if type(owner) is dict else owner
        ensure(isinstance(invocation, InvocationOwner),
               "information_invalid_binding", "Release requires an exact invocation identity")
        invocation.to_dict()
        ensure(generation is None or type(generation) is int and generation > 0,
               "information_invalid_generation", "Release generation is invalid")
        with self._lock:
            for binding in self._bindings.values():
                if binding.owner == invocation and (generation is None or binding.generation == generation):
                    binding.reader, binding.released = None, True

    def catalog(self, *, audience: str = "management", owner=None) -> list[dict]:
        _audience(audience)
        invocation = InvocationOwner.from_dict(owner) if type(owner) is dict else owner
        ensure(invocation is None or isinstance(invocation, InvocationOwner),
               "information_invalid_binding", "Catalog owner is invalid")
        if invocation is not None:
            invocation.to_dict()
        with self._lock:
            result = []
            for binding in self._bindings.values():
                if invocation is not None and binding.owner != invocation:
                    continue
                declaration = self.registry.get(binding.reference).definition.to_dict()
                if audience == "consumer" and not declaration["discover_public"]:
                    continue
                result.append({
                    "kind": "information_binding", "registration_ref": binding.reference.to_dict(),
                    "owner": binding.owner.to_dict(), "generation": binding.generation,
                    "declaration": declaration,
                    "discover_public": declaration["discover_public"], "read_public": declaration["read_public"],
                    "availability": "live" if binding.reader is not None and not binding.released else
                                    "history" if binding.history_reader is not None else "unavailable",
                    "package_id": None, "package_version": None,
                })
            return sorted(result, key=canonical_bytes)

    def read(self, reference, owner, generation: int, *, audience: str = "management",
             source_scope: str = "live", limit: int = 100, cursor: str | None = None) -> dict:
        _audience(audience)
        ref, invocation = self._identities(reference, owner, generation)
        ensure(source_scope in ("live", "history"),
               "information_scope_mismatch", "Read requires a live or history source scope")
        ensure(cursor is None or type(cursor) is str and 0 < len(cursor) <= 4096,
               "information_invalid_cursor", "Provider cursor must be bounded opaque text")
        with self._lock:
            registered = self.registry.get(ref)
            ensure(registered is not None, "information_source_missing", "Exact information source is unavailable")
            declaration = registered.definition
            ensure(audience == "management" or declaration.read_public,
                   "information_read_denied", "Source is not readable by consumers")
            ensure(type(limit) is int and 1 <= limit <= declaration.max_page_size,
                   "information_invalid_limit", "Read page size exceeds the registered bound")
            binding = self._bindings.get((invocation, ref, generation))
            ensure(binding is not None, "information_unbound", "Source has no binding for this exact invocation")
            if source_scope == "live":
                ensure(self._active.get((invocation, ref)) == generation,
                       "information_stale_generation", "Live reader generation is obsolete")
                ensure(not binding.released and binding.reader is not None,
                       "information_live_unavailable", "Live information source has been released or is unavailable")
                reader = binding.reader
            else:
                ensure(binding.history_reader is not None,
                       "information_history_unavailable", "No historical reader is registered for this binding")
                reader = binding.history_reader
        request = {"owner": invocation.to_dict(), "generation": generation, "limit": limit, "cursor": cursor}
        response = reader(copy.deepcopy(request))
        validate_json_value(response)
        ensure(type(response) is dict and set(response) == {"items", "next_cursor", "status"}
               and type(response["items"]) is list and len(response["items"]) <= limit
               and response["status"] in ("ok", "gap", "reset")
               and (response["next_cursor"] is None or type(response["next_cursor"]) is str
                    and 0 < len(response["next_cursor"]) <= 4096)
               and len(canonical_bytes(response)) <= declaration.max_page_bytes,
               "information_invalid_response", "Reader response exceeds its declared envelope or bounds")
        try:
            validator = Draft202012Validator(declaration.item_schema)
            for item in response["items"]:
                validator.validate(item)
        except ValidationError as exc:
            raise HostContractError("information_invalid_response", "Reader item schema mismatch") from exc
        with self._lock:
            if source_scope == "live":
                ensure(not binding.released and binding.reader is reader
                       and self._active.get((invocation, ref)) == generation,
                       "information_live_unavailable", "Information source changed while it was being read")
        return {"schema_version": 1, "source_ref": ref.to_dict(), "owner": invocation.to_dict(),
                "generation": generation, "source_scope": source_scope,
                "format_id": declaration.format_id, "format_version": declaration.format_version,
                **copy.deepcopy(response)}
