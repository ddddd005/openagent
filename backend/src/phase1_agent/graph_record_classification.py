"""Pure package-evidence classification, not an execution or freezing authority.

Accepted manifests and their sources are evidence supplied by the trusted host,
not fields accepted from a graph document. This evaluates full locks and export
ownership only. It does not prove historical fact closure, live executor
availability, configuration validity, or persistent type-contract digests.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from .capability_packages import PackageDependency, PackageManifest, ResolvedPackageSelection
from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, loads_strict
from .graph_contracts import validate_graph_document, validate_package_lock
from .host_sdk import HOST_PROTOCOL_VERSION, ensure


COMPAT_PACKAGE = ("workflow.compat", "1.0.0")
COMPAT_NODES = frozenset({
    ("workflow.text", "1"), ("workflow.current-input", "1"), ("workflow.output", "1"),
    ("workflow.regex", "1"), ("workflow.text-to-prompt", "1"), ("workflow.prompt-to-text", "1"),
    ("workflow.json-to-text", "1"), ("workflow.prompt-item", "1"), ("workflow.prompt-group", "1"),
    ("workflow.prompt-source", "1"), ("workflow.prompt-summary", "1"), ("workflow.prompt-summary", "2"),
    ("workflow.tool", "1"), ("workflow.tool-summary", "1"), ("workflow.global-content", "1"),
    ("workflow.global-content", "2"), ("workflow.variable-register", "1"),
    ("workflow.variable-assign", "1"), ("workflow.variable-replace", "1"),
    ("workflow.session-data-read", "1"), ("workflow.session-data-write", "1"),
    ("workflow.object-read", "1"), ("workflow.object-write", "1"), ("workflow.object-delete", "1"),
    ("workflow.agent", "1"), ("workflow.agent", "2"), ("workflow.model-provider", "1"),
    ("workflow.model-provider", "2"), ("workflow.context", "1"), ("workflow.prompt-assembly", "1"),
})
COMPAT_TYPES = frozenset({
    ("content", "GLOBAL_RESOURCE_REF", 1),
    ("global", "workflow.global-content", 1),
    ("session", "workflow.json-object", 1),
})


@dataclass(frozen=True)
class GraphRecordClassification:
    classification: Literal["current", "frozen", "blocked"]
    package_lock: tuple[dict, ...]
    diagnostics: tuple[dict, ...] = ()
    node_owners: tuple[dict, ...] = ()
    data_type_owners: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        return deepcopy({
            "classification": self.classification, "package_lock": list(self.package_lock),
            "diagnostics": list(self.diagnostics), "node_owners": list(self.node_owners),
            "data_type_owners": list(self.data_type_owners),
        })


def _export_kinds(schema_version):
    kinds = {"data_types", "nodes", "frontend_extensions"}
    if schema_version >= 2:
        kinds |= {"executors", "pause_support"}
    if schema_version >= 3:
        kinds.add("services")
    if schema_version >= 4:
        kinds.add("information_sources")
    return kinds


def _identities(lock):
    return {(item["package_id"], item["version"]) for item in lock}


def _resolution_manifests(resolved, lock):
    ensure(type(resolved) is ResolvedPackageSelection
           and type(resolved.package_lock) is tuple and type(resolved.package_manifests) is tuple
           and type(resolved.registration_order) is tuple
           and len(resolved.package_manifests) <= 256 and len(resolved.registration_order) <= 256,
           "graph_record_resolution_unverified", "Exact dependency resolution evidence is required")
    resolved_lock = validate_package_lock(list(resolved.package_lock))
    ensure(_identities(lock) == _identities(resolved_lock),
           "graph_record_lock_incomplete", "The original full lock differs from the resolved dependency closure")
    manifests = {}
    for raw in resolved.package_manifests:
        manifest = PackageManifest.from_dict(raw).to_dict()
        key = manifest["package_id"], manifest["version"]
        ensure(key not in manifests, "graph_record_manifest_conflict", "Package manifest evidence is duplicated")
        ensure(manifest["host_protocol_version"] == HOST_PROTOCOL_VERSION,
               "package_protocol_mismatch", "Package host protocol is not supported")
        manifests[key] = manifest
    ensure(set(manifests) == _identities(lock),
           "graph_record_manifest_unverified", "Every exact locked package requires its installed manifest")
    ensure(all(type(item) is PackageDependency for item in resolved.registration_order),
           "graph_record_resolution_unverified", "Registration order requires exact package identities")
    checked_order = [item.to_dict() for item in resolved.registration_order]
    order = [(item["package_id"], item["version"]) for item in checked_order]
    ensure(len(order) == len(set(order)) and set(order) == set(manifests),
           "graph_record_resolution_unverified", "Resolution order differs from the exact locked packages")
    ranks = {key: index for index, key in enumerate(order)}
    for key, manifest in manifests.items():
        for dependency in manifest["dependencies"]:
            required = dependency["package_id"], dependency["version"]
            ensure(required in ranks and ranks[required] < ranks[key],
                   "graph_record_lock_incomplete", "The lock must include an exact acyclic dependency closure")
    return manifests


def _accepted_exports(manifests, accepted_manifests, package_sources):
    ensure(type(accepted_manifests) in (list, tuple) and len(accepted_manifests) <= 256,
           "graph_record_exports_unverified", "Accepted export evidence must be a bounded manifest array")
    ensure(type(package_sources) is dict and set(package_sources) == set(manifests)
           and all(source in ("builtin", "trusted_extension") for source in package_sources.values()),
           "graph_record_source_unverified", "Each exact package requires its trusted installation source")
    accepted = {}
    ownership = {}
    for raw in accepted_manifests:
        manifest = PackageManifest.from_dict(raw).to_dict()
        key = manifest["package_id"], manifest["version"]
        ensure(key in manifests and key not in accepted,
               "graph_record_manifest_conflict", "Accepted manifests must match each exact package once")
        declaration = manifests[key]
        ensure(all(canonical_bytes(manifest[field]) == canonical_bytes(declaration[field])
                   for field in declaration if field != "exports"),
               "graph_record_manifest_conflict", "Accepted manifest metadata differs from the installed declaration")
        exports = manifest["exports"]
        ensure(set(exports) == _export_kinds(manifest["schema_version"]),
               "graph_record_exports_unverified", "Accepted evidence must include every actual export category")
        for kind, expected in declaration["exports"].items():
            ensure(sorted(map(canonical_bytes, expected)) == sorted(map(canonical_bytes, exports[kind])),
                   "package_export_mismatch", "Accepted exports differ from the explicit package declaration")
        if key[0] == COMPAT_PACKAGE[0]:
            ensure(key == COMPAT_PACKAGE and package_sources[key] == "builtin"
                   and declaration["schema_version"] == 1 and declaration["dependencies"] == []
                   and declaration["exports"] == {},
                   "graph_record_compat_unverified", "Compatibility requires its exact builtin source and manifest")
            ensure({(row["component_id"], row["component_version"]) for row in exports["nodes"]} == COMPAT_NODES
                   and {(row["scope"], row["type_id"], row["schema_version"])
                        for row in exports["data_types"]} == COMPAT_TYPES
                   and exports["frontend_extensions"] == [],
                   "graph_record_compat_unverified", "Compatibility requires the adjudicated accepted export identities")
        for kind, rows in exports.items():
            for row in rows:
                owners = ownership.setdefault((kind, canonical_bytes(row)), set())
                owners.add(key)
                ensure(kind == "data_types" or len(owners) == 1,
                       "graph_record_owner_conflict", "A nonshared accepted export has conflicting package owners")
        accepted[key] = manifest
    ensure(set(accepted) == set(manifests),
           "graph_record_exports_unverified", "Every locked package requires separately accepted export evidence")
    return ownership


def classify_graph_document(
    document: dict, resolved: ResolvedPackageSelection, *,
    accepted_manifests=(), package_sources=None,
) -> GraphRecordClassification:
    """Evaluate host-supplied evidence without loading packages or changing a record.

    ``resolved`` is the metadata-only result of ``CapabilityPackageLoader.resolve``.
    ``accepted_manifests`` must instead contain actual exports after successful
    registration/finish. Source keys are exact ``(package_id, version)`` tuples.
    A result is preparation for later policy wiring, never proof that the runtime
    or frozen-record write boundary has been implemented.
    """
    lock = ()
    try:
        checked = validate_graph_document(document)
        ensure("package_lock" in checked, "graph_record_lock_unverified",
               "Classification requires an explicit original full package lock")
        lock = tuple(deepcopy(checked["package_lock"]))
        manifests = _resolution_manifests(resolved, lock)
        sources = {} if package_sources is None else deepcopy(package_sources)
        ownership = _accepted_exports(manifests, accepted_manifests, sources)

        def owners(kind, identity):
            keys = ownership.get((kind, canonical_bytes(identity)), set())
            return [{"package_id": key[0], "version": key[1], "source": sources[key]}
                    for key in sorted(keys)]

        nodes = []
        for node in checked["nodes"]:
            identity = {key: node[key] for key in ("component_id", "component_version")}
            actual = owners("nodes", identity)
            ensure(bool(actual), "graph_record_owner_unknown", "A node has no verified exact package owner")
            if (node["component_id"], node["component_version"]) in COMPAT_NODES:
                ensure(actual == [{"package_id": COMPAT_PACKAGE[0], "version": COMPAT_PACKAGE[1],
                                   "source": "builtin"}],
                       "graph_record_owner_conflict", "A compatibility identity has a different accepted owner")
            nodes.append({"node_binding_id": node["node_binding_id"], **identity, "owners": actual})
        types = []
        for (kind, raw_identity), _ in sorted(ownership.items()):
            if kind == "data_types":
                identity = loads_strict(raw_identity.decode("utf-8"))
                types.append({**identity, "owners": owners(kind, identity)})
        for binding in checked.get("object_bindings", []):
            ensure(bool(owners("data_types", {"scope": "session", "type_id": binding["type_id"],
                                            "schema_version": binding["schema_version"]})),
                   "graph_record_type_owner_unknown", "An object type has no verified exact package owner")
        frozen = COMPAT_PACKAGE in manifests
        diagnostic = ({"reason_code": "graph_record_compat_frozen",
                       "message": "The complete original lock retains the verified compatibility package"},) if frozen else ()
        return GraphRecordClassification("frozen" if frozen else "current", lock, diagnostic,
                                         tuple(nodes), tuple(types))
    except ContractValidationError as error:
        return GraphRecordClassification("blocked", lock, ({
            "reason_code": getattr(error, "reason_code", "graph_record_evidence_invalid"),
            "message": str(error),
        },))
