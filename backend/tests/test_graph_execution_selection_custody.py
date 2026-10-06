"""Execution configuration custody in isolated, model-free service databases."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from threading import Event
from uuid import UUID

import pytest

from phase1_agent import builtin_packages, graph_nodes
from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.capability_packages import (
    CapabilityPackage, PackageDependency, PackageManifest,
)
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_package_selection import (
    CURRENT_EXECUTION_CONFIGURATION, HISTORICAL_PROJECT_CONFIGURATION,
)
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, TypeRegistry
from phase1_agent.storage import SqliteStore
from phase1_agent.type_contract_store import TypeContractStore


TOOLS = {"workflow.tools": "1.0.0"}
PROMPTS = {"workflow.prompts": "1.0.0"}
HISTORICAL_RAW = ' \n{ "workflow.tools" : "1.0.0" }\n '
CURRENT_RAW = ' \n{ "workflow.tools" : "1.0.0" }\n '


def uid(number):
    return str(UUID(int=number, version=4))


def encoded(value):
    return canonical_bytes(value).decode("utf-8")


def no_model(*args, **kwargs):
    pytest.fail("Configuration custody attempted to construct a model")


@pytest.fixture(autouse=True)
def no_compatibility_registration(monkeypatch):
    def reject():
        pytest.fail("Configuration custody constructed the compatibility registry")

    monkeypatch.setattr(graph_nodes, "create_default_registry", reject)


def service(path, **options):
    return GraphWorkflowService(
        path, model_factory=no_model, public_model_factory=no_model, **options,
    )


def seed_selection(path, *, historical=None, current=None):
    with closing(SqliteStore(path)) as store:
        for identity, payload in (
            (HISTORICAL_PROJECT_CONFIGURATION, historical),
            (CURRENT_EXECUTION_CONFIGURATION, current),
        ):
            if payload is not None:
                store._connection.execute(
                    "INSERT INTO graph_project_packages VALUES(?,?)",
                    (identity, payload),
                )


def raw_selections(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return dict(connection.execute(
            "SELECT configuration_id,payload FROM graph_project_packages ORDER BY configuration_id",
        ))


def raw_database(path):
    """Read complete original rows without running storage initialization."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        tables = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name",
        ).fetchall()
        result = {}
        for name, declaration in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            cursor = connection.execute("SELECT * FROM " + quoted + " ORDER BY rowid")
            result[name] = (
                declaration,
                tuple(column[0] for column in cursor.description),
                tuple(cursor.fetchall()),
            )
        return result


def lock(instance):
    return {row["package_id"]: row["version"] for row in instance.registry.package_lock}


def expected_lock(selection):
    return {**selection, **({"workflow.content": "1.0.0"} if selection else {})}


@pytest.mark.parametrize("selection", [None, {}, TOOLS], ids=["default", "empty", "tools"])
def test_fresh_service_writes_only_current_and_reopen_keeps_exact_selection(
    tmp_path, monkeypatch, selection,
):
    path = tmp_path / "fresh.sqlite"
    selected = deepcopy(DEFAULT_PACKAGES if selection is None else selection)
    with closing(service(path, enabled_packages=selection)) as instance:
        assert lock(instance) == expected_lock(selected)
        assert instance._native_runtime is None
        assert raw_selections(path) == {CURRENT_EXECUTION_CONFIGURATION: encoded(selected)}
    before = raw_selections(path)
    monkeypatch.setattr(builtin_packages, "DEFAULT_PACKAGES", {"unavailable.new-default": "9"})
    with closing(service(path)) as reopened:
        assert lock(reopened) == expected_lock(selected)
        assert reopened.platform_capabilities()["package_diagnostics"] == []
        assert reopened._native_runtime is None
    assert raw_selections(path) == before


@pytest.mark.parametrize("raw,selected", [
    (HISTORICAL_RAW, TOOLS),
    (" \n{}\n ", {}),
], ids=["tools", "explicit-empty"])
def test_historical_fallback_is_exact_and_is_not_rewritten_or_promoted(
    tmp_path, monkeypatch, raw, selected,
):
    path = tmp_path / "historical-only.sqlite"
    seed_selection(path, historical=raw)
    monkeypatch.setattr(builtin_packages, "DEFAULT_PACKAGES", {"unavailable.new-default": "9"})
    with closing(service(path)) as instance:
        assert lock(instance) == expected_lock(selected)
        assert instance.platform_capabilities()["package_diagnostics"] == []
        assert raw_selections(path) == {HISTORICAL_PROJECT_CONFIGURATION: raw}
    assert raw_selections(path) == {HISTORICAL_PROJECT_CONFIGURATION: raw}


@pytest.mark.parametrize("historical", [
    "{",
    '["not-a-selection"]',
    '{"workflow.compat":"1.0.0"}',
    '{ "missing.historical-package" : "9" }\n',
], ids=["invalid-json", "wrong-shape", "compatibility", "missing-package"])
@pytest.mark.parametrize("current,selected", [
    (CURRENT_RAW, TOOLS),
    (" \n{}\n ", {}),
], ids=["tools", "explicit-empty"])
def test_current_precedes_unused_historical_data_without_parsing_or_rewriting_it(
    tmp_path, monkeypatch, historical, current, selected,
):
    path = tmp_path / "priority.sqlite"
    seed_selection(path, historical=historical, current=current)
    before = raw_selections(path)
    monkeypatch.setattr(builtin_packages, "DEFAULT_PACKAGES", {"unavailable.new-default": "9"})
    with closing(service(path)) as instance:
        assert lock(instance) == expected_lock(selected)
        assert instance.platform_capabilities()["package_diagnostics"] == []
        assert instance._native_runtime is None
        assert raw_selections(path) == before
    assert raw_selections(path) == before


@pytest.mark.parametrize("historical", ["{", HISTORICAL_RAW], ids=["malformed", "valid"])
@pytest.mark.parametrize("selected", [{}, PROMPTS], ids=["explicit-empty", "prompts"])
def test_explicit_constructor_updates_only_current_and_skips_malformed_saved_data(
    tmp_path, historical, selected,
):
    path = tmp_path / "explicit.sqlite"
    seed_selection(path, historical=historical, current="{")
    with closing(service(path, enabled_packages=selected)) as instance:
        assert lock(instance) == expected_lock(selected)
        assert instance.platform_capabilities()["package_diagnostics"] == []
        assert raw_selections(path) == {
            HISTORICAL_PROJECT_CONFIGURATION: historical,
            CURRENT_EXECUTION_CONFIGURATION: encoded(selected),
        }
    with closing(service(path)) as reopened:
        assert lock(reopened) == expected_lock(selected)
    assert raw_selections(path)[HISTORICAL_PROJECT_CONFIGURATION] == historical


@pytest.mark.parametrize("selected", [{}, PROMPTS], ids=["explicit-empty", "prompts"])
def test_configure_updates_only_current_and_reopen_uses_its_exact_value(tmp_path, selected):
    path = tmp_path / "configure.sqlite"
    seed_selection(path, historical="{", current=CURRENT_RAW)
    with closing(service(path)) as instance:
        configured = instance.configure_capability_packages(selected)
        assert configured["package_diagnostics"] == []
        assert lock(instance) == expected_lock(selected)
        assert raw_selections(path) == {
            HISTORICAL_PROJECT_CONFIGURATION: "{",
            CURRENT_EXECUTION_CONFIGURATION: encoded(selected),
        }
    with closing(service(path)) as reopened:
        assert lock(reopened) == expected_lock(selected)
        assert reopened.platform_capabilities()["package_diagnostics"] == []
    assert raw_selections(path)[HISTORICAL_PROJECT_CONFIGURATION] == "{"


def test_explicit_missing_constructor_does_not_replace_any_original_rows(tmp_path):
    path = tmp_path / "missing-constructor.sqlite"
    seed_selection(path, historical="{", current=CURRENT_RAW)
    with closing(service(path)):
        pass
    before = raw_database(path)
    with pytest.raises(ContractValidationError) as caught:
        service(path, enabled_packages={"missing.requested-package": "9"})
    assert caught.value.reason_code == "package_missing_dependency"
    assert raw_database(path) == before
    with closing(service(path)) as reopened:
        assert lock(reopened) == expected_lock(TOOLS)


def test_missing_saved_current_never_falls_back_to_valid_historical_selection(tmp_path):
    path = tmp_path / "missing-current.sqlite"
    unavailable = '{"missing.current-package":"9"}'
    seed_selection(path, historical=HISTORICAL_RAW, current=unavailable)
    before = raw_selections(path)
    with closing(service(path)) as instance:
        diagnostics = instance.platform_capabilities()["package_diagnostics"]
        assert diagnostics[0]["reason_code"] == "package_missing_dependency"
        assert diagnostics[0]["enabled_packages"] == {"missing.current-package": "9"}
        assert instance.registry.catalog() == []
        assert lock(instance) == {}
        assert instance._native_runtime is None and instance._futures == {}
        assert raw_selections(path) == before
    assert raw_selections(path) == before


def data_package(*, conflict=False):
    declarations = [
        DataTypeDefinition("custody.a.new", 1, {"type": "string"}, ""),
    ]
    if conflict:
        declarations.append(DataTypeDefinition(
            "custody.z.existing", 1, {"type": "integer"}, 0,
        ))

    def register(host):
        for declaration in declarations:
            host.register_data_type(declaration)

    return CapabilityPackage(PackageManifest(
        "custody.data", "1", exports={"data_types": [
            {"scope": declaration.scope, "type_id": declaration.type_id,
             "schema_version": declaration.schema_version}
            for declaration in declarations
        ]},
    ), register)


def failing_package():
    def register(host):
        host.register_data_type(DataTypeDefinition("custody.partial", 1, {"type": "string"}, ""))
        raise RuntimeError("custody registration failed")

    return CapabilityPackage(PackageManifest("custody.failing", "1"), register)


@pytest.mark.parametrize("selected,reason", [
    ({"missing.requested-package": "9"}, "package_missing_dependency"),
    ([], "package_invalid_selection"),
    ({**TOOLS, "custody.failing": "1"}, "package_registration_failed"),
], ids=["missing", "invalid", "registration"])
def test_failed_configure_preserves_raw_configuration_evidence_and_published_registry(
    tmp_path, selected, reason,
):
    path = tmp_path / "failed-configure.sqlite"
    seed_selection(path, historical=HISTORICAL_RAW, current=CURRENT_RAW)
    with closing(service(path, capability_packages=(failing_package(),))) as instance:
        before, published = raw_database(path), deepcopy(instance.platform_capabilities())
        with pytest.raises(ContractValidationError) as caught:
            instance.configure_capability_packages(selected)
        assert caught.value.reason_code == reason
        assert raw_database(path) == before
        assert instance.platform_capabilities() == published
        assert instance._native_runtime is None and instance._futures == {}


@pytest.mark.parametrize("operation", ["constructor", "configure"])
def test_configuration_write_failure_rolls_back_new_type_evidence_as_well_as_current(
    tmp_path, operation,
):
    path = tmp_path / "write-rollback.sqlite"
    seed_selection(path, historical=HISTORICAL_RAW, current=CURRENT_RAW)
    package = data_package()
    with closing(service(path, capability_packages=(package,))) as instance:
        with closing(SqliteStore(path)) as store:
            store._connection.execute(
                "CREATE TRIGGER custody_reject_current_write BEFORE INSERT ON graph_project_packages "
                "WHEN NEW.configuration_id='current-execution' "
                "BEGIN SELECT RAISE(ABORT,'custody selection write failed'); END",
            )
        before, published = raw_database(path), deepcopy(instance.platform_capabilities())
        selected = {**TOOLS, "custody.data": "1"}
        if operation == "constructor":
            instance.close()
        with pytest.raises(sqlite3.IntegrityError, match="custody selection write failed"):
            if operation == "constructor":
                service(path, capability_packages=(package,), enabled_packages=selected)
            else:
                instance.configure_capability_packages(selected)
        assert raw_database(path) == before
        if operation == "configure":
            assert instance.platform_capabilities() == published
    with closing(service(path)) as reopened:
        assert lock(reopened) == expected_lock(TOOLS)
    assert raw_database(path) == before


@pytest.mark.parametrize("operation", ["constructor", "configure"])
def test_persistent_type_conflict_rolls_back_preceding_new_declarations_and_configuration(
    tmp_path, operation,
):
    path = tmp_path / "type-rollback.sqlite"
    seed_selection(path, historical=HISTORICAL_RAW, current=CURRENT_RAW)
    package = data_package(conflict=True)
    with closing(service(path, capability_packages=(package,))) as instance:
        types = TypeRegistry()
        types.register(DataTypeDefinition("custody.z.existing", 1, {"type": "string"}, "original"))
        with closing(SqliteStore(path)) as store:
            TypeContractStore(store).register(types, "custody.z.existing", 1, "session")
        before, published = raw_database(path), deepcopy(instance.platform_capabilities())
        selected = {**TOOLS, "custody.data": "1"}
        if operation == "constructor":
            instance.close()
        with pytest.raises(ContractValidationError) as caught:
            if operation == "constructor":
                service(path, capability_packages=(package,), enabled_packages=selected)
            else:
                instance.configure_capability_packages(selected)
        assert caught.value.reason_code == "type_contract_redefined"
        assert raw_database(path) == before
        if operation == "configure":
            assert instance.platform_capabilities() == published
    with closing(service(path)) as reopened:
        assert lock(reopened) == expected_lock(TOOLS)
    assert raw_database(path) == before


def gate_package(entered, release):
    def execute(config, inputs, context):
        entered.set()
        assert release.wait(10)
        return {"output": text_content("released")}

    def register(host):
        host.register_node(NodeDefinition(
            "custody.gate", "1", "Gate", "Custody", {},
            {"type": "object", "additionalProperties": False},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        ), execute)

    return CapabilityPackage(PackageManifest(
        "custody.active", "1", dependencies=(PackageDependency("workflow.content", "1.0.0"),),
        exports={"nodes": [{"component_id": "custody.gate", "component_version": "1"}]},
    ), register)


def test_running_and_paused_current_execution_still_block_package_changes_without_writes(tmp_path):
    path = tmp_path / "active.sqlite"
    seed_selection(path, historical=HISTORICAL_RAW)
    entered, release = Event(), Event()
    selected = {**TOOLS, "custody.active": "1"}
    with closing(service(
        path, capability_packages=(gate_package(entered, release),), enabled_packages=selected,
    )) as instance:
        output_config = deepcopy(instance.registry.get("tools.output", "1").definition.default_config)
        document = {
            "schema_version": 2, "workflow_definition_id": uid(1), "revision": 1,
            "name": "Current execution custody", "object_bindings": [],
            "package_lock": deepcopy(list(instance.registry.execution_package_lock)),
            "nodes": [
                {"node_binding_id": uid(2), "component_id": "custody.gate", "component_version": "1",
                 "title": "Gate", "position": {"x": 0, "y": 0}, "config": {}},
                {"node_binding_id": uid(3), "component_id": "tools.output", "component_version": "1",
                 "title": "Output", "position": {"x": 200, "y": 0}, "config": output_config},
            ],
            "edges": [{"edge_id": uid(4), "source_node_id": uid(2), "source_port_id": "output",
                       "target_node_id": uid(3), "target_port_id": "input", "order": 0}],
        }
        instance.save_definition(document, expected_revision=0, idempotency_key="active-definition")
        initial = instance.create_session(uid(1), 1, idempotency_key="active-session")
        started = instance.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                 idempotency_key="active-start")
        try:
            assert entered.wait(5)
            before = raw_database(path)
            with pytest.raises(ContractValidationError) as caught:
                instance.configure_capability_packages({})
            assert caught.value.reason_code == "package_change_during_execution"
            assert raw_database(path) == before
            current = instance.get_session(initial["workflow_session_id"])
            instance.control(initial["workflow_session_id"], action="pause",
                             expected_revision=current["revision"], idempotency_key="active-pause")
        finally:
            release.set()
        instance.wait(started["active_chain_run_id"])
        paused = instance.get_session(initial["workflow_session_id"])
        assert paused["status"] == "paused"
        before = raw_database(path)
        with pytest.raises(ContractValidationError) as caught:
            instance.configure_capability_packages({})
        assert caught.value.reason_code == "package_change_during_execution"
        assert raw_database(path) == before
        assert instance.get_session(initial["workflow_session_id"]) == paused
        assert raw_selections(path) == {
            HISTORICAL_PROJECT_CONFIGURATION: HISTORICAL_RAW,
            CURRENT_EXECUTION_CONFIGURATION: encoded(selected),
        }
        assert instance._native_runtime is None
