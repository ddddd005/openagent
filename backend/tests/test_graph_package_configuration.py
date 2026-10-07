"""Selection storage and pure dependency metadata without graph workers."""

from contextlib import closing
from copy import deepcopy
from threading import RLock

import pytest

from phase1_agent.capability_packages import (
    CapabilityPackage, CapabilityPackageLoader, PackageDependency, PackageManifest,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_package_selection import (
    CURRENT_EXECUTION_CONFIGURATION, GraphPackageSelectionStore,
)
from phase1_agent.graph_platform import GraphPlatform
from phase1_agent.host_sdk import DataTypeDefinition, HostContractError
from phase1_agent.storage import SqliteStore


def put_selection(store, identity, payload):
    store._connection.execute(
        "INSERT INTO graph_project_packages VALUES(?,?)", (identity, payload),
    )


def selections(store):
    return {
        row["configuration_id"]: row["payload"]
        for row in store._connection.execute(
            "SELECT configuration_id,payload FROM graph_project_packages",
        )
    }


@pytest.mark.parametrize("current,source,expected", [
    ('{}', CURRENT_EXECUTION_CONFIGURATION, {}),
    ('{"example.current":"3"}', CURRENT_EXECUTION_CONFIGURATION,
     {"example.current": "3"}),
    (' {\n "example.exact": "2"\n}\n', CURRENT_EXECUTION_CONFIGURATION,
     {"example.exact": "2"}),
    (None, None, {"example.default": "1"}),
])
def test_store_reads_only_current_payload_and_preserves_exact_selection(
    tmp_path, current, source, expected,
):
    default = {"example.default": "1"}
    with closing(SqliteStore(tmp_path / "effective.sqlite")) as store:
        for identity, payload in (
            (CURRENT_EXECUTION_CONFIGURATION, current),
            ("unknown-configuration", "{opaque invalid JSON"),
        ):
            if payload is not None:
                put_selection(store, identity, payload)
        before = selections(store)
        repository = GraphPackageSelectionStore(store)
        result = repository.read_effective(default)
        assert result.configuration_id == source
        assert result.enabled_packages == expected
        assert result.payload == before.get(source)
        result.enabled_packages["example.detached"] = "9"
        assert repository.read_effective(default).enabled_packages == expected
        assert default == {"example.default": "1"}
        assert repository.current_payload() == current
        assert selections(store) == before


@pytest.mark.parametrize("payload", [
    "{invalid", '{"example.duplicate":"1","example.duplicate":"2"}', '{"x":NaN}',
])
def test_invalid_effective_current_does_not_fall_back_to_defaults(tmp_path, payload):
    with closing(SqliteStore(tmp_path / "invalid-current.sqlite")) as store:
        put_selection(store, CURRENT_EXECUTION_CONFIGURATION, payload)
        before = selections(store)
        with pytest.raises(ContractValidationError):
            GraphPackageSelectionStore(store).read_effective({})
        assert selections(store) == before


def test_store_requires_owning_transaction_and_writes_only_current_row(tmp_path):
    with closing(SqliteStore(tmp_path / "writes.sqlite")) as store:
        put_selection(store, "unknown-configuration", "raw unknown payload")
        before = selections(store)
        repository = GraphPackageSelectionStore(store)
        with pytest.raises(ContractValidationError) as denied:
            repository.write_current({})
        assert denied.value.reason_code == "storage_contract_violation"
        assert selections(store) == before
        store._connection.execute("BEGIN IMMEDIATE")
        repository.write_current({})
        store._connection.execute("COMMIT")
        assert selections(store) == {**before, CURRENT_EXECUTION_CONFIGURATION: "{}"}
        store._connection.execute("BEGIN IMMEDIATE")
        repository.write_current({"example.current": "3"})
        store._connection.execute("ROLLBACK")
        assert selections(store) == {**before, CURRENT_EXECUTION_CONFIGURATION: "{}"}
        store._connection.execute("BEGIN IMMEDIATE")
        repository.write_current({"example.current": "3"})
        store._connection.execute("COMMIT")
        assert selections(store) == {
            **before, CURRENT_EXECUTION_CONFIGURATION: '{"example.current":"3"}',
        }


def package(identity, calls, *, version="1", dependencies=(), protocol=1, exports=None):
    def register(host):
        calls.append(identity)
    return CapabilityPackage(
        PackageManifest(identity, version, tuple(dependencies), protocol, exports or {}), register,
    )


def test_resolver_preserves_complete_closure_exact_versions_and_registration_order(monkeypatch):
    calls = []
    packages = (
        package("z.root", calls, dependencies=(
            PackageDependency("b.shared", "2"), PackageDependency("a.branch", "1"))),
        package("a.branch", calls, dependencies=(PackageDependency("c.foundation", "3"),)),
        package("b.shared", calls, version="2",
                dependencies=(PackageDependency("c.foundation", "3"),)),
        package("c.foundation", calls, version="3"),
    )
    loader = CapabilityPackageLoader(packages)
    caches = deepcopy((loader._type_contracts, loader._node_contracts, loader._executor_contracts,
                       loader._service_contracts, loader._information_contracts, loader._frontend_contracts))
    selected = {"z.root": "1"}
    resolved = loader.resolve(selected)
    expected_order = ["c.foundation", "a.branch", "b.shared", "z.root"]
    assert calls == []
    assert resolved.package_lock == (
        {"package_id": "a.branch", "version": "1"}, {"package_id": "b.shared", "version": "2"},
        {"package_id": "c.foundation", "version": "3"}, {"package_id": "z.root", "version": "1"},
    )
    assert [row.package_id for row in resolved.registration_order] == expected_order
    assert [row["package_id"] for row in resolved.package_manifests] == expected_order
    assert selected == {"z.root": "1"}
    assert caches == (loader._type_contracts, loader._node_contracts, loader._executor_contracts,
                      loader._service_contracts, loader._information_contracts, loader._frontend_contracts)
    loaded = loader.load(selected)
    assert calls == expected_order
    assert loaded.package_lock == resolved.package_lock
    assert [row["package_id"] for row in loaded.package_manifests] == expected_order

    def no_staging(*args, **kwargs):
        raise AssertionError("Pure resolution constructed a registry")

    monkeypatch.setattr(loader._base_registry, "detached", no_staging)
    assert loader.resolve(selected).package_lock == resolved.package_lock
    returned = resolved.to_dict()
    returned["package_lock"][0]["version"] = "caller edit"
    returned["package_manifests"][0]["exports"]["caller"] = []
    resolved.package_lock[0]["version"] = "caller edit"
    resolved.package_manifests[0]["exports"]["caller"] = []
    assert loader.resolve(selected).to_dict()["package_lock"][0]["version"] == "1"
    assert "caller" not in loader.resolve(selected).package_manifests[0]["exports"]


@pytest.mark.parametrize("problem,reason", [
    ("missing", "package_missing_dependency"),
    ("cycle", "package_dependency_cycle"),
    ("selected-conflict", "package_dependency_conflict"),
    ("transitive-conflict", "package_dependency_conflict"),
    ("protocol", "package_protocol_mismatch"),
    ("ambiguous-default", "package_version_required"),
    ("invalid-selection", "package_invalid_selection"),
])
def test_resolve_and_load_keep_original_resolution_failures_without_registration(problem, reason):
    calls = []
    packages, selected = [package("a", calls)], {"a": "1"}
    if problem == "missing":
        packages = [package("a", calls, dependencies=(PackageDependency("missing", "1"),))]
    elif problem == "cycle":
        packages = [package("a", calls, dependencies=(PackageDependency("b", "1"),)),
                    package("b", calls, dependencies=(PackageDependency("a", "1"),))]
    elif problem == "selected-conflict":
        packages = [package("a", calls, dependencies=(PackageDependency("b", "1"),)),
                    package("b", calls), package("b", calls, version="2")]
        selected["b"] = "2"
    elif problem == "transitive-conflict":
        packages = [
            package("a", calls, dependencies=(PackageDependency("c", "1"),)),
            package("b", calls, dependencies=(PackageDependency("c", "2"),)),
            package("c", calls), package("c", calls, version="2"),
        ]
        selected["b"] = "1"
    elif problem == "protocol":
        packages = [package("a", calls, protocol=99)]
    elif problem == "ambiguous-default":
        packages.append(package("a", calls, version="2"))
        selected = None
    else:
        selected = []
    loader = CapabilityPackageLoader(packages)
    failures = []
    for method in (loader.resolve, loader.load):
        with pytest.raises(HostContractError) as caught:
            method(selected)
        assert caught.value.reason_code == reason
        failures.append(str(caught.value))
    assert failures[0] == failures[1]
    assert calls == []


def test_resolver_returns_declarations_not_accepted_export_evidence():
    calls = []
    declared = {"nodes": [{"component_id": "example.not-registered", "component_version": "1"}]}
    loader = CapabilityPackageLoader((package("example", calls, exports=declared),))
    resolved = loader.resolve({"example": "1"})
    assert calls == [] and resolved.package_manifests[0]["exports"] == declared
    with pytest.raises(HostContractError) as mismatch:
        loader.load({"example": "1"})
    assert mismatch.value.reason_code == "package_export_mismatch"


def test_resolver_default_selection_and_explicit_empty_remain_distinct():
    calls = []
    loader = CapabilityPackageLoader((package("a", calls), package("b", calls, version="2")))
    assert loader.resolve().package_lock == (
        {"package_id": "a", "version": "1"}, {"package_id": "b", "version": "2"},
    )
    empty = loader.resolve({})
    assert empty.package_lock == empty.package_manifests == empty.registration_order == ()
    assert calls == []


class SelectionPlatform(GraphPlatform):
    def __init__(self, path, loader):
        self.path, self._package_loader = path, loader
        self.registry = loader.load({}).registry.detached()
        self._frontend_extensions, self._package_manifests = (), ()
        self._package_diagnostics = [{"reason_code": "package_missing_dependency"}]
        self._lock = RLock()
        self._pauses, self._resuming = set(), set()
        for name in (
            "_futures", "_resource_frames", "_execution_registries", "_runtime_hosts",
            "_service_runs", "_acceptance_candidates", "_failed_retry_candidates", "_information_routers",
        ):
            setattr(self, name, {})

    def _load_current_capabilities(self, selected):
        return self._package_loader.load(selected)

    def _store(self):
        return SqliteStore(self.path)

    def _check_open(self):
        return None


def type_package():
    def register(host):
        host.register_data_type(DataTypeDefinition("example.selection-value", 1, {"type": "integer"}, 0))
    return CapabilityPackage(PackageManifest("example.selection", "1"), register)


def test_configure_selection_and_type_evidence_commit_together_without_touching_unrelated_rows(tmp_path):
    path = tmp_path / "configuration.sqlite"
    with closing(SqliteStore(path)) as store:
        put_selection(store, "unknown-configuration", "opaque untouched payload")
        original = selections(store)
    platform = SelectionPlatform(path, CapabilityPackageLoader((type_package(),)))
    result = platform.configure_capability_packages({"example.selection": "1"})
    assert result["package_lock"] == [{"package_id": "example.selection", "version": "1"}]
    assert result["package_diagnostics"] == []
    with closing(SqliteStore(path)) as store:
        assert selections(store) == {
            **original, CURRENT_EXECUTION_CONFIGURATION: '{"example.selection":"1"}',
        }
        assert store._connection.execute(
            "SELECT 1 FROM registered_type_contracts WHERE type_id='example.selection-value'",
        ).fetchone() is not None


def test_configure_failed_current_write_rolls_back_type_evidence_and_published_registry(tmp_path, monkeypatch):
    path = tmp_path / "rollback.sqlite"
    with closing(SqliteStore(path)) as store:
        put_selection(store, CURRENT_EXECUTION_CONFIGURATION, "{}")
        original = selections(store)
    platform = SelectionPlatform(path, CapabilityPackageLoader((type_package(),)))
    registry, diagnostics = platform.registry, deepcopy(platform._package_diagnostics)
    write_current = GraphPackageSelectionStore.write_current

    def fail_after_current_write(self, selected):
        write_current(self, selected)
        raise RuntimeError("injected current selection commit failure")

    monkeypatch.setattr(GraphPackageSelectionStore, "write_current", fail_after_current_write)
    with pytest.raises(RuntimeError, match="injected"):
        platform.configure_capability_packages({"example.selection": "1"})
    assert platform.registry is registry
    assert platform._package_diagnostics == diagnostics
    with closing(SqliteStore(path)) as store:
        assert selections(store) == original
        assert store._connection.execute("SELECT * FROM registered_type_contracts").fetchall() == []
