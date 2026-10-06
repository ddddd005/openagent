"""Metadata-only classification; genuine fixture setup may register builtin packages."""

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from phase1_agent.capability_packages import (
    CapabilityPackage, CapabilityPackageLoader, HostRegistration, PackageDependency, PackageManifest,
)
from phase1_agent.graph_record_classification import (
    COMPAT_NODES, COMPAT_TYPES, classify_graph_document,
)


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def document(lock, *identities):
    return {
        "schema_version": 2, "workflow_definition_id": uid(1), "revision": 1,
        "name": "Pure classification", "object_bindings": [], "edges": [],
        "package_lock": deepcopy(list(lock)),
        "nodes": [{
            "node_binding_id": uid(10 + index), "component_id": component,
            "component_version": version, "title": component,
            "position": {"x": index * 100, "y": 0}, "config": {},
        } for index, (component, version) in enumerate(identities)],
    }


def manifest(identity="example.current", version="1", *, dependencies=(), exports=None, schema=1):
    return PackageManifest(
        identity, version, tuple(PackageDependency(*item) for item in dependencies),
        exports={} if exports is None else exports, schema_version=schema,
    ).to_dict()


def accepted(declaration, *, nodes=(), types=()):
    value = deepcopy(declaration)
    value["exports"] = {
        "nodes": [{"component_id": component, "component_version": version} for component, version in nodes],
        "data_types": [{"scope": scope, "type_id": identity, "schema_version": version}
                       for scope, identity, version in types],
        "frontend_extensions": [],
    }
    if declaration["schema_version"] >= 2:
        value["exports"].update(executors=[], pause_support=[])
    if declaration["schema_version"] >= 3:
        value["exports"]["services"] = []
    if declaration["schema_version"] >= 4:
        value["exports"]["information_sources"] = []
    return value


def resolve(*declarations):
    def forbidden_registration(host):
        raise AssertionError("Classification must not register packages")
    packages = [CapabilityPackage(PackageManifest.from_dict(value), forbidden_registration)
                for value in declarations]
    loader = CapabilityPackageLoader(packages)
    selection = {value["package_id"]: value["version"] for value in declarations}
    return loader.resolve(selection)


def current_fixture(*identities):
    declaration = manifest("workflow.agents", "1.0.0")
    resolved = resolve(declaration)
    actual = accepted(declaration, nodes=identities)
    return document(resolved.package_lock, *identities), resolved, [actual], {
        ("workflow.agents", "1.0.0"): "builtin",
    }


def compat_evidence():
    identities = [
        "workflow.text@1", "workflow.current-input@1", "workflow.output@1",
        "workflow.regex@1", "workflow.text-to-prompt@1", "workflow.prompt-to-text@1",
        "workflow.json-to-text@1", "workflow.prompt-item@1", "workflow.prompt-group@1",
        "workflow.prompt-source@1", "workflow.prompt-summary@1", "workflow.prompt-summary@2",
        "workflow.tool@1", "workflow.tool-summary@1", "workflow.global-content@1",
        "workflow.global-content@2", "workflow.variable-register@1", "workflow.variable-assign@1",
        "workflow.variable-replace@1", "workflow.session-data-read@1", "workflow.session-data-write@1",
        "workflow.object-read@1", "workflow.object-write@1", "workflow.object-delete@1",
        "workflow.agent@1", "workflow.agent@2", "workflow.model-provider@1", "workflow.model-provider@2",
        "workflow.context@1", "workflow.prompt-assembly@1",
    ]
    declaration = manifest("workflow.compat", "1.0.0")
    actual = accepted(declaration, nodes=[tuple(value.split("@")) for value in identities], types=[
        ("content", "GLOBAL_RESOURCE_REF", 1), ("global", "workflow.global-content", 1),
        ("session", "workflow.json-object", 1),
    ])
    return declaration, actual


def classify(fixture):
    doc, resolved, actual, sources = fixture
    return classify_graph_document(doc, resolved, accepted_manifests=actual, package_sources=sources)


def prohibit_loading_and_registration(monkeypatch, loader):
    def forbidden(*args, **kwargs):
        raise AssertionError("Classification must not load or register packages")
    monkeypatch.setattr(CapabilityPackageLoader, "load", forbidden)
    monkeypatch.setattr(HostRegistration, "__init__", forbidden)
    for key, package in tuple(loader._packages.items()):
        monkeypatch.setitem(loader._packages, key, replace(package, register=forbidden))


@pytest.mark.parametrize("version", ["1", "2", "3"])
def test_all_current_agent_versions_use_exact_accepted_ownership(version):
    fixture = current_fixture(("agents.execute", version))
    result = classify(fixture)
    assert result.classification == "current"
    assert result.diagnostics == ()
    assert result.node_owners[0]["owners"] == [
        {"package_id": "workflow.agents", "version": "1.0.0", "source": "builtin"},
    ]


@pytest.mark.parametrize("version", ["1", "2", "3"])
def test_real_default_execution_exports_classify_current_without_loading_or_registration(monkeypatch, version):
    from phase1_agent.builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages

    loader = CapabilityPackageLoader(builtin_capability_packages())
    loaded = loader.load(DEFAULT_PACKAGES)
    selection = {row["package_id"]: row["version"] for row in loaded.registry.execution_package_lock}
    resolved = loader.resolve(selection)
    keys = {(row["package_id"], row["version"]) for row in resolved.package_lock}
    actual = [deepcopy(row) for row in loaded.package_manifests
              if (row["package_id"], row["version"]) in keys]
    assert resolved.package_lock == loaded.registry.execution_package_lock
    assert not {"workflow.frontend", "workflow.compat"} & set(selection)
    assert "workflow.frontend-business" in selection
    sources = {key: "builtin" for key in keys}
    fixture = document(resolved.package_lock, ("agents.execute", version)), resolved, actual, sources
    before = deepcopy((fixture[0], actual, sources, resolved.to_dict()))

    prohibit_loading_and_registration(monkeypatch, loader)
    result = classify(fixture)

    assert result.classification == "current"
    assert result.diagnostics == ()
    assert result.node_owners[0]["owners"] == [
        {"package_id": "workflow.agents", "version": "1.0.0", "source": "builtin"},
    ]
    assert (fixture[0], actual, sources, resolved.to_dict()) == before


def test_real_compat_accepted_exports_match_policy_and_classify_frozen_without_registration(monkeypatch):
    from phase1_agent.builtin_packages import builtin_capability_packages

    loader = CapabilityPackageLoader(builtin_capability_packages())
    selection = {"workflow.compat": "1.0.0"}
    loaded = loader.load(selection)
    resolved = loader.resolve(selection)
    assert len(loaded.package_manifests) == 1
    actual = deepcopy(list(loaded.package_manifests))
    exports = actual[0]["exports"]
    nodes = {(row["component_id"], row["component_version"]) for row in exports["nodes"]}
    types = {(row["scope"], row["type_id"], row["schema_version"]) for row in exports["data_types"]}
    assert nodes == COMPAT_NODES
    assert types == COMPAT_TYPES
    assert len(exports["nodes"]) == len(nodes) == 30
    assert len(exports["data_types"]) == len(types) == 3
    assert exports["frontend_extensions"] == []
    sources = {("workflow.compat", "1.0.0"): "builtin"}
    fixture = document(resolved.package_lock, ("workflow.agent", "2")), resolved, actual, sources
    before = deepcopy((fixture[0], actual, sources, resolved.to_dict()))

    prohibit_loading_and_registration(monkeypatch, loader)
    result = classify(fixture)

    assert result.classification == "frozen"
    assert result.diagnostics[0]["reason_code"] == "graph_record_compat_frozen"
    assert result.node_owners[0]["owners"] == [
        {"package_id": "workflow.compat", "version": "1.0.0", "source": "builtin"},
    ]
    assert (fixture[0], actual, sources, resolved.to_dict()) == before


def test_trusted_extension_with_workflow_prefix_is_not_guessed_legacy():
    declaration = manifest("example.trusted")
    resolved = resolve(declaration)
    doc = document(resolved.package_lock, ("workflow.future", "1"))
    result = classify_graph_document(doc, resolved,
        accepted_manifests=[accepted(declaration, nodes=[("workflow.future", "1")])],
        package_sources={("example.trusted", "1"): "trusted_extension"})
    assert result.classification == "current"


def test_verified_compatibility_in_full_lock_freezes_even_unused_compatibility():
    doc, _, actual, sources = current_fixture(("agents.execute", "3"))
    declaration, compat = compat_evidence()
    resolved = resolve(declaration, manifest("workflow.agents", "1.0.0"))
    doc["package_lock"] = list(reversed(deepcopy(list(resolved.package_lock))))
    sources[("workflow.compat", "1.0.0")] = "builtin"
    before = deepcopy((doc, actual, sources, resolved.to_dict()))
    result = classify_graph_document(doc, resolved, accepted_manifests=[*actual, compat], package_sources=sources)
    assert result.classification == "frozen"
    assert list(result.package_lock) == doc["package_lock"]
    assert result.diagnostics[0]["reason_code"] == "graph_record_compat_frozen"
    assert len(compat["exports"]["nodes"]) == 30
    assert len(compat["exports"]["data_types"]) == 3
    assert (doc, actual, sources, resolved.to_dict()) == before
    detached = result.to_dict()
    detached["package_lock"][0]["version"] = "changed"
    detached["node_owners"][0]["owners"][0]["source"] = "changed"
    assert result.to_dict()["package_lock"] == doc["package_lock"]
    assert result.node_owners[0]["owners"][0]["source"] == "builtin"


def test_dormant_unknown_node_blocks_even_a_verified_compatibility_document():
    declaration, actual = compat_evidence()
    resolved = resolve(declaration)
    doc = document(resolved.package_lock, ("workflow.text", "1"), ("unknown.dormant", "1"))
    doc["execution_roots"] = [doc["nodes"][0]["node_binding_id"]]
    result = classify_graph_document(doc, resolved, accepted_manifests=[actual],
                                    package_sources={("workflow.compat", "1.0.0"): "builtin"})
    assert result.classification == "blocked"
    assert result.diagnostics[0]["reason_code"] == "graph_record_owner_unknown"


def test_known_legacy_node_with_foreign_owner_is_not_relabelled_current_or_frozen():
    declaration = manifest()
    resolved = resolve(declaration)
    result = classify_graph_document(document(resolved.package_lock, ("workflow.agent", "2")), resolved,
        accepted_manifests=[accepted(declaration, nodes=[("workflow.agent", "2")])],
        package_sources={("example.current", "1"): "trusted_extension"})
    assert result.classification == "blocked"
    assert result.diagnostics[0]["reason_code"] == "graph_record_owner_conflict"


@pytest.mark.parametrize("defect", ["missing", "wrong-version", "extra", "duplicate"])
def test_full_original_lock_must_match_exact_resolution(defect):
    fixture = current_fixture(("agents.execute", "3"))
    doc = fixture[0]
    if defect == "missing":
        doc["package_lock"] = []
    elif defect == "wrong-version":
        doc["package_lock"][0]["version"] = "2.0.0"
    elif defect == "extra":
        doc["package_lock"].append({"package_id": "not.installed", "version": "1"})
    else:
        doc["package_lock"].append(deepcopy(doc["package_lock"][0]))
    result = classify(fixture)
    assert result.classification == "blocked"
    assert result.diagnostics[0]["reason_code"] in ("graph_record_lock_incomplete", "graph_invalid_package_lock")


def test_exact_dependency_and_topological_evidence_cannot_be_omitted_or_forged():
    dependency = manifest("example.dependency")
    declaration = manifest(dependencies=[("example.dependency", "1")])
    resolved = resolve(declaration, dependency)
    actual = [accepted(declaration, nodes=[("example.node", "1")]), accepted(dependency)]
    sources = {("example.current", "1"): "trusted_extension", ("example.dependency", "1"): "trusted_extension"}
    doc = document(resolved.package_lock, ("example.node", "1"))
    assert classify_graph_document(doc, resolved, accepted_manifests=actual,
                                   package_sources=sources).classification == "current"
    for bad in (
        replace(resolved, registration_order=tuple(reversed(resolved.registration_order))),
        replace(resolved, registration_order=resolved.registration_order[:-1]),
        replace(resolved, package_manifests=resolved.package_manifests[:-1]),
    ):
        assert classify_graph_document(doc, bad, accepted_manifests=actual,
                                       package_sources=sources).classification == "blocked"
    doc["package_lock"] = [row for row in doc["package_lock"] if row["package_id"] != "example.dependency"]
    assert classify_graph_document(doc, resolved, accepted_manifests=actual,
                                   package_sources=sources).classification == "blocked"


@pytest.mark.parametrize("identity", [([], "1"), ("example.current", {})])
def test_malformed_dependency_order_fields_are_blocked_without_unhashable_errors(identity):
    fixture = current_fixture(("agents.execute", "3"))
    doc, resolved, actual, sources = fixture
    damaged = replace(resolved, registration_order=(PackageDependency(*identity),))
    result = classify_graph_document(doc, damaged, accepted_manifests=actual, package_sources=sources)
    assert result.classification == "blocked"
    assert result.diagnostics[0]["reason_code"] == "package_invalid_manifest"


@pytest.mark.parametrize("defect", ["none", "wrong-key-version", "unknown", "extra"])
def test_sources_must_name_every_exact_installed_package(defect):
    doc, resolved, actual, sources = current_fixture(("agents.execute", "3"))
    if defect == "none":
        sources = None
    elif defect == "wrong-key-version":
        sources = {("workflow.agents", "other"): "builtin"}
    elif defect == "unknown":
        sources[("workflow.agents", "1.0.0")] = "unverified"
    else:
        sources[("extra", "1")] = "trusted_extension"
    result = classify_graph_document(doc, resolved, accepted_manifests=actual, package_sources=sources)
    assert result.classification == "blocked"
    assert result.diagnostics[0]["reason_code"] == "graph_record_source_unverified"


@pytest.mark.parametrize("defect", ["missing", "declared-only", "metadata", "duplicate", "foreign"])
def test_empty_manifest_declarations_do_not_prove_actual_accepted_exports(defect):
    doc, resolved, actual, sources = current_fixture(("agents.execute", "3"))
    if defect == "missing":
        actual = []
    elif defect == "declared-only":
        actual = list(resolved.package_manifests)
    elif defect == "metadata":
        actual[0]["dependencies"] = [{"package_id": "not.installed", "version": "1"}]
    elif defect == "duplicate":
        actual.append(deepcopy(actual[0]))
    else:
        actual.append(accepted(manifest("foreign")))
    result = classify_graph_document(doc, resolved, accepted_manifests=actual, package_sources=sources)
    assert result.classification == "blocked"
    assert result.diagnostics[0]["reason_code"] in (
        "graph_record_exports_unverified", "graph_record_manifest_conflict",
    )


def test_explicit_manifest_exports_must_match_accepted_exports():
    declaration = manifest(exports={"nodes": [{"component_id": "example.node", "component_version": "1"}]})
    resolved = resolve(declaration)
    result = classify_graph_document(document(resolved.package_lock, ("example.node", "1")), resolved,
        accepted_manifests=[accepted(declaration, nodes=[("example.node", "2")])],
        package_sources={("example.current", "1"): "trusted_extension"})
    assert result.classification == "blocked"
    assert result.diagnostics[0]["reason_code"] == "package_export_mismatch"


@pytest.mark.parametrize("defect", ["node", "type", "frontend", "source", "version", "schema"])
def test_compatibility_requires_original_source_manifest_and_complete_accepted_profile(defect):
    declaration, actual = compat_evidence()
    source = "builtin"
    if defect == "node":
        actual["exports"]["nodes"].pop()
    elif defect == "type":
        actual["exports"]["data_types"].pop()
    elif defect == "frontend":
        actual["exports"]["frontend_extensions"] = [{"extension_id": "unexpected"}]
    elif defect == "source":
        source = "trusted_extension"
    elif defect == "version":
        declaration["version"] = actual["version"] = "2.0.0"
    else:
        declaration["schema_version"] = actual["schema_version"] = 2
        actual["exports"].update(executors=[], pause_support=[])
    resolved = resolve(declaration)
    result = classify_graph_document(document(resolved.package_lock), resolved, accepted_manifests=[actual],
        package_sources={(declaration["package_id"], declaration["version"]): source})
    assert result.classification == "blocked"
    assert result.diagnostics[0]["reason_code"] == "graph_record_compat_unverified"


def test_shared_type_ownership_retains_all_exact_owners_not_a_single_overwrite():
    first, second = manifest("example.first"), manifest("example.second")
    resolved = resolve(first, second)
    shared = [("session", "example.state", 1)]
    actual = [accepted(first, nodes=[("example.node", "1")], types=shared), accepted(second, types=shared)]
    doc = document(resolved.package_lock, ("example.node", "1"))
    doc["object_bindings"] = [{
        "object_key": "state", "type_id": "example.state", "schema_version": 1, "scope": "shared",
        "owner_node_id": None, "readers": [uid(10)], "writers": [uid(10)],
    }]
    sources = {(row["package_id"], row["version"]): "trusted_extension" for row in resolved.package_lock}
    result = classify_graph_document(doc, resolved, accepted_manifests=actual, package_sources=sources)
    assert result.classification == "current"
    assert {row["package_id"] for row in result.data_type_owners[0]["owners"]} == {"example.first", "example.second"}
    actual[1]["exports"]["nodes"] = deepcopy(actual[0]["exports"]["nodes"])
    assert classify_graph_document(doc, resolved, accepted_manifests=actual,
                                   package_sources=sources).diagnostics[0]["reason_code"] == "graph_record_owner_conflict"


def test_shared_global_reference_keeps_compat_and_current_content_owners():
    declaration, compat = compat_evidence()
    content = manifest("workflow.content", "1.0.0")
    resolved = resolve(declaration, content)
    actual = [compat, accepted(content, types=[("content", "GLOBAL_RESOURCE_REF", 1)])]
    result = classify_graph_document(document(resolved.package_lock), resolved, accepted_manifests=actual,
        package_sources={("workflow.compat", "1.0.0"): "builtin", ("workflow.content", "1.0.0"): "builtin"})
    assert result.classification == "frozen"
    reference = next(row for row in result.data_type_owners if row["type_id"] == "GLOBAL_RESOURCE_REF")
    assert {owner["package_id"] for owner in reference["owners"]} == {"workflow.compat", "workflow.content"}


def test_missing_object_type_owner_and_missing_original_lock_are_blocked_without_age_heuristics():
    fixture = current_fixture(("agents.execute", "1"))
    doc = fixture[0]
    doc["object_bindings"] = [{
        "object_key": "state", "type_id": "unknown.state", "schema_version": 1, "scope": "shared",
        "owner_node_id": None, "readers": [uid(10)], "writers": [],
    }]
    assert classify(fixture).diagnostics[0]["reason_code"] == "graph_record_type_owner_unknown"
    doc["schema_version"] = 1
    doc.pop("object_bindings")
    doc.pop("package_lock")
    assert classify(fixture).diagnostics[0]["reason_code"] == "graph_record_lock_unverified"


def test_invalid_document_and_resolution_produce_blocked_without_mutating_evidence():
    fixture = current_fixture(("agents.execute", "3"))
    original = deepcopy(fixture[0])
    fixture[0]["nodes"][0]["unexpected"] = True
    assert classify(fixture).classification == "blocked"
    fixture[0].clear()
    fixture[0].update(original)
    assert classify_graph_document(fixture[0], fixture[1].to_dict(),
        accepted_manifests=fixture[2], package_sources=fixture[3]).diagnostics[0]["reason_code"] == "graph_record_resolution_unverified"
    assert fixture[0] == original


def test_fresh_process_classification_blocks_execution_storage_and_compat_registration_imports(tmp_path):
    program = r"""
import importlib.abc
import sys
blocked = (
    "phase1_agent.graph_nodes", "phase1_agent.graph_agent_runtime", "phase1_agent.graph_service",
    "phase1_agent.workflow", "phase1_agent.kernel", "phase1_agent.storage",
    "phase1_agent.builtin_packages",
)
class NoExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Classification imported execution/storage: " + fullname)
sys.meta_path.insert(0, NoExecution())
from test_graph_record_classification import (
    classify, current_fixture, compat_evidence, document, resolve, classify_graph_document,
)
assert classify(current_fixture(("agents.execute", "3"))).classification == "current"
declaration, actual = compat_evidence()
resolved = resolve(declaration)
assert classify_graph_document(document(resolved.package_lock, ("workflow.agent", "2")), resolved,
    accepted_manifests=[actual], package_sources={("workflow.compat", "1.0.0"): "builtin"}).classification == "frozen"
assert not any(name == item or name.startswith(item + ".") for name in sys.modules for item in blocked)
print("classification remains metadata-only")
"""
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    environment["PYTHONPATH"] = os.pathsep.join([
        str(root / "src"), str(root / "tests"), environment.get("PYTHONPATH", ""),
    ])
    result = subprocess.run([sys.executable, "-c", program], text=True, capture_output=True,
                            env=environment, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "classification remains metadata-only"
