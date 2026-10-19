"""Unified read-only definition query over trusted registrations.

Definitions stay owned by their package or host registration.  This module
projects an exact identity, the category declaration and the contributing
package sources; it never persists a second definition authority and never
serialises executors, validators, factories or declaration hooks.

The public HTTP and frontend protocols keep their existing shapes.  Consumers
inside the host use this query, and the existing catalog routes project the
same node declarations outwards.
"""

from __future__ import annotations

import copy

from .contract_json import canonical_bytes
from .graph_contracts import NodeRegistry
from .host_sdk import HostContractError, bounded_name, ensure


DEFINITION_KINDS = ("node", "data_type")
_EXPORT_KIND = {"node": "nodes", "data_type": "data_types"}
_REFERENCE_FIELDS = {
    "node": ("component_id", "component_version"),
    "data_type": ("scope", "type_id", "schema_version"),
}


def _kind(value):
    ensure(type(value) is str and value in DEFINITION_KINDS,
           "definition_unknown_kind", "Unknown definition category")
    return value


def _reference(kind, value):
    fields = _REFERENCE_FIELDS[kind]
    ensure(type(value) is dict and set(value) == set(fields),
           "definition_invalid_reference", "Definition reference fields differ")
    if kind == "node":
        ensure(bounded_name(value["component_id"]) and bounded_name(value["component_version"]),
               "definition_invalid_reference", "Node definition requires an exact component identity")
        return {"component_id": value["component_id"], "component_version": value["component_version"]}
    ensure(value["scope"] in ("content", "session", "global")
           and bounded_name(value["type_id"])
           and type(value["schema_version"]) is int and 1 <= value["schema_version"] <= 2**53 - 1,
           "definition_invalid_reference", "Data type definition requires an exact schema identity")
    return {"scope": value["scope"], "type_id": value["type_id"],
            "schema_version": value["schema_version"]}


def _source_index(package_manifests):
    """Map exact declarations to every package that contributes them."""
    result: dict[tuple[str, bytes], list[dict]] = {}
    for manifest in package_manifests:
        package = {"package_id": manifest["package_id"], "package_version": manifest["version"]}
        for kind, export_name in _EXPORT_KIND.items():
            for declaration in manifest.get("exports", {}).get(export_name, []):
                try:
                    reference = _reference(kind, declaration)
                except HostContractError:
                    continue
                providers = result.setdefault((kind, canonical_bytes(reference)), [])
                if package not in providers:
                    providers.append(package)
    for providers in result.values():
        providers.sort(key=lambda item: (item["package_id"], item["package_version"]))
    return result


def _node_declaration(entry):
    declaration = entry.definition.to_dict(
        executable=entry.executor is not None or entry.executor_ref is not None)
    if entry.executor_ref is not None:
        declaration["executor_ref"] = entry.executor_ref.to_dict()
    return declaration


def _node_dependencies(declaration):
    result = []
    for ports in [(declaration.get("inputs", []), declaration.get("outputs", [])),
                  *((mode["inputs"], mode["outputs"])
                    for mode in declaration.get("port_modes", {}).values())]:
        for port in (*ports[0], *ports[1]):
            result.append({
                "kind": "data_type", "relation": "port",
                "registration_ref": {"scope": "content", "type_id": port["data_type"],
                                     "schema_version": port.get("data_schema_version", 1)},
            })
    for reference in declaration.get("config_node_references", []):
        for target in reference.get("target_types", []):
            result.append({
                "kind": "node", "relation": "config-node",
                "registration_ref": {"component_id": target["component_id"],
                                     "component_version": target["component_version"]},
            })
    return result


def _data_type_dependencies(declaration):
    # A JSON schema may contain ``$ref`` text, but those are not registered
    # definition identities.  Resolving them would invent a second dependency
    # rule, so the first category keeps dependencies explicit only.
    return []


class NodeDefinitionAccess:
    """Restricted rule access for trusted host consumers.

    The executor and implementation hooks remain inside the registration.  A
    consumer can validate configuration and ask for declared external inputs,
    but cannot obtain a serialisable or replaceable implementation binding.
    """

    def __init__(self, entry):
        self._entry = entry
        self.declaration = _node_declaration(entry)

    @property
    def component_id(self):
        return self._entry.definition.component_id

    @property
    def component_version(self):
        return self._entry.definition.component_version

    @property
    def can_declare_external_inputs(self):
        return self._entry.external_inputs_declaration is not None

    @property
    def has_external_inputs_validator(self):
        return self._entry.external_inputs_validator is not None

    def validate_config(self, config):
        NodeRegistry.validate_config(self._entry, config)

    def declare_external_inputs(self, config):
        callback = self._entry.external_inputs_declaration
        return [] if callback is None else callback(copy.deepcopy(config))

    def validate_external_inputs(self, config, values):
        validator = self._entry.external_inputs_validator
        if validator is not None:
            validator(copy.deepcopy(config), copy.deepcopy(values))


class _NodeCategory:
    kind = "node"

    @staticmethod
    def reference(declaration):
        return _reference("node", {key: declaration[key] for key in _REFERENCE_FIELDS["node"]})

    @staticmethod
    def get(registry, reference):
        entry = registry.get(reference["component_id"], reference["component_version"])
        return None if entry is None else _node_declaration(entry)

    @staticmethod
    def list(registry):
        return registry.catalog()

    @staticmethod
    def dependencies(declaration):
        return _node_dependencies(declaration)


class _DataTypeCategory:
    kind = "data_type"

    @staticmethod
    def reference(declaration):
        return _reference("data_type", {key: declaration[key] for key in _REFERENCE_FIELDS["data_type"]})

    @staticmethod
    def get(registry, reference):
        definition = registry.data_types.get(
            reference["type_id"], reference["schema_version"], scope=reference["scope"])
        return None if definition is None else definition.to_dict()

    @staticmethod
    def list(registry):
        return registry.data_types.catalog()

    @staticmethod
    def dependencies(declaration):
        return _data_type_dependencies(declaration)


_CATEGORIES = {"node": _NodeCategory(), "data_type": _DataTypeCategory()}


def definition_reference(kind, value):
    """Parse one exact reference without querying a registry."""
    return _reference(_kind(kind), value)


class WorkflowDefinitionCatalog:
    """Exact definition query with package provenance over one trusted registry."""

    def __init__(self, registry: NodeRegistry, package_manifests=()):
        ensure(isinstance(registry, NodeRegistry), "definition_invalid_registry",
               "Definition query requires the trusted node registry")
        self._registry = registry
        self._sources = _source_index(tuple(package_manifests))

    @property
    def registry(self):
        """The trusted registry, for compiler paths that are not definition reads."""
        return self._registry

    def _sources_for(self, kind, reference):
        return copy.deepcopy(self._sources.get((kind, canonical_bytes(reference)), []))

    def _view(self, kind, reference, declaration):
        return {
            "kind": kind,
            "registration_ref": copy.deepcopy(reference),
            "declaration": copy.deepcopy(declaration),
            "source": self._sources_for(kind, reference),
            "availability": "registered",
        }

    def get(self, kind, reference):
        """Return one definition view, or ``None`` when the exact version is absent."""
        kind = _kind(kind)
        reference = _reference(kind, reference)
        declaration = _CATEGORIES[kind].get(self._registry, reference)
        return None if declaration is None else self._view(kind, reference, declaration)

    def require(self, kind, reference):
        view = self.get(kind, reference)
        ensure(view is not None, "definition_missing", "Exact definition is unavailable")
        return view

    def list(self, kind=None):
        kinds = DEFINITION_KINDS if kind is None else (_kind(kind),)
        result = []
        for current in kinds:
            category = _CATEGORIES[current]
            for declaration in category.list(self._registry):
                reference = category.reference(declaration)
                result.append(self._view(current, reference, declaration))
        return result

    def dependencies(self, kind, reference):
        view = self.require(kind, reference)
        return copy.deepcopy(_CATEGORIES[kind].dependencies(view["declaration"]))

    def node_access(self, component_id, component_version):
        reference = _reference("node", {"component_id": component_id,
                                        "component_version": component_version})
        entry = self._registry.get(reference["component_id"], reference["component_version"])
        return None if entry is None else NodeDefinitionAccess(entry)

    def node_catalog(self):
        """Project node declarations in the exact shape of the existing catalog."""
        return [copy.deepcopy(self.require("node", self._node_reference(declaration))["declaration"])
                for declaration in self._registry.catalog()]

    @staticmethod
    def _node_reference(declaration):
        return _reference("node", {key: declaration[key] for key in _REFERENCE_FIELDS["node"]})

    def workflow_references(self, document):
        """Resolve exact definitions used by one fixed workflow document.

        The caller reads the fixed document through the configuration data
        port; this method only resolves the already-persisted references and
        never re-composes current configuration.
        """
        nodes = []
        data_types = {}
        for node in document.get("nodes", []):
            reference = {"component_id": node["component_id"],
                         "component_version": node["component_version"]}
            view = self.get("node", reference)
            if view is None:
                nodes.append({"kind": "node", "registration_ref": copy.deepcopy(reference),
                              "source": self._sources_for("node", reference),
                              "availability": "unavailable"})
                continue
            nodes.append({"kind": "node", "registration_ref": copy.deepcopy(reference),
                          "source": view["source"], "availability": view["availability"]})
            for dependency in self.dependencies("node", reference):
                if dependency["kind"] != "data_type":
                    continue
                identity = canonical_bytes(dependency["registration_ref"])
                view = self.get("data_type", dependency["registration_ref"])
                data_types[identity] = view if view is not None else {
                    "kind": "data_type",
                    "registration_ref": copy.deepcopy(dependency["registration_ref"]),
                    "source": self._sources_for("data_type", dependency["registration_ref"]),
                    "availability": "unavailable",
                }
        return {
            "workflow_definition_id": document["workflow_definition_id"],
            "revision": document["revision"],
            "package_lock": copy.deepcopy(document.get("package_lock", [])),
            "nodes": nodes,
            "data_types": [data_types[key] for key in sorted(data_types)],
        }
