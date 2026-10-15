"""Current Tavern identity retains Lorebook and excludes retired chat authority."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES, builtin_capability_packages
from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.frontend_package import FRONTEND_EXTENSIONS
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ResourceIdentity
from phase1_agent.lorebook_engine import default_lorebook_entry
from phase1_agent.tavern.lorebook_resources import LOREBOOK_RESOURCE_TYPE
from phase1_agent.tavern.package import (
    CURRENT_TAVERN_FRONTEND_EXTENSIONS, GLOBAL_TAVERN_FRONTEND_EXTENSIONS,
    create_current_tavern_package,
)

from test_graph_service import create, text_graph


RETIRED_COMPONENTS = ("tavern.chat.output", "tavern.chat.append", "tavern.chat.presentation")
RETIRED_TYPES = (
    ("session", "workflow.tavern.chat-state"),
    ("content", "TAVERN_CHAT_VIEW"),
    ("content", "TAVERN_CHAT_COMMIT"),
    ("content", "TAVERN_CHAT_DISPLAY"),
)
LOREBOOK_COMPONENTS = (
    "lorebook.item", "lorebook.group", "lorebook.global-reference", "lorebook.global-activate",
)


def loader():
    return CapabilityPackageLoader(builtin_capability_packages())


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "1.2.0"])
def test_exact_tavern_versions_have_no_old_chat_nodes_types_or_ui(version):
    loaded = loader().load({"workflow.tavern": version})
    assert {"package_id": "workflow.tavern", "version": version} in loaded.package_lock
    for component in RETIRED_COMPONENTS:
        assert loaded.registry.get(component, "1") is None
    for scope, type_id in RETIRED_TYPES:
        assert loaded.registry.data_types.get(type_id, 1, scope=scope) is None
    extension_ids = {row["extension_id"] for row in loaded.frontend_extensions}
    assert "workflow.tavern.chat-launch" not in extension_ids
    assert "workflow.tavern.presentation-fields" not in extension_ids
    assert loaded.registry.get("lorebook.item", "1") is not None
    assert loaded.registry.get("lorebook.group", "1") is not None
    assert bool(loaded.registry.get("lorebook.global-activate", "1")) == (version != "1.0.0")


def test_current_identity_and_lorebook_contracts_are_not_downgraded_or_redefined():
    installed = loader()
    inline = installed.load({"workflow.tavern": "1.0.0"})
    global_version = installed.load({"workflow.tavern": "1.1.0"})
    current = installed.load({"workflow.tavern": "1.2.0"})
    for component in LOREBOOK_COMPONENTS:
        previous = inline if component in ("lorebook.item", "lorebook.group") else global_version
        assert previous.registry.get(component, "1").definition.to_dict() == (
            current.registry.get(component, "1").definition.to_dict())
    assert current.registry.data_types.get(LOREBOOK_RESOURCE_TYPE, 1, scope="global").to_dict() == (
        global_version.registry.data_types.get(LOREBOOK_RESOURCE_TYPE, 1, scope="global").to_dict())
    assert CURRENT_TAVERN_FRONTEND_EXTENSIONS == tuple({
        **deepcopy(row), "extension_id": row["extension_id"] + "-v1-2",
        "entrypoint": row["entrypoint"] + "-v1-2",
    } for row in GLOBAL_TAVERN_FRONTEND_EXTENSIONS)
    declarations = [row for row in current.frontend_extensions if row["package_id"] == "workflow.tavern"]
    assert declarations == sorted([{
        "component_id": None, "component_version": None, **deepcopy(row),
        "schema_version": 3 if row["binding"]["slot"] == "node-fields" else 2,
        "host_protocol_version": 2 if row["binding"]["slot"] == "node-fields" else 1,
        "package_id": "workflow.tavern", "package_version": "1.2.0",
    } for row in CURRENT_TAVERN_FRONTEND_EXTENSIONS], key=lambda row: row["extension_id"])
    exports = create_current_tavern_package().manifest.to_dict()["exports"]
    assert exports["nodes"] == [
        {"component_id": identity, "component_version": "1"} for identity in LOREBOOK_COMPONENTS]
    assert exports["data_types"] == [
        {"scope": "global", "type_id": LOREBOOK_RESOURCE_TYPE, "schema_version": 1}]


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "1.2.0"])
def test_ordinary_saved_graph_selection_and_session_reopen_without_migration(tmp_path, version):
    database = tmp_path / "selection.sqlite"
    selection = {**DEFAULT_PACKAGES, "workflow.tavern": version}
    with closing(GraphWorkflowService(database, enabled_packages=selection)) as service:
        document = text_graph(service.registry)
        session = create(service, document)
        saved_document = service.get_definition(document["workflow_definition_id"], 1)
        catalog, lock = service.registry.catalog(), service.registry.package_lock
    with closing(GraphWorkflowService(database)) as reopened:
        assert reopened.registry.catalog() == catalog
        assert reopened.registry.package_lock == lock
        assert {"package_id": "workflow.tavern", "version": version} in lock
        assert reopened.get_definition(document["workflow_definition_id"], 1) == saved_document
        assert reopened.get_session(session["workflow_session_id"]) == session
        assert reopened.platform_capabilities()["package_diagnostics"] == []
        assert not hasattr(reopened, "_native_runtime")


def test_default_keeps_current_lorebook_and_logical_chat_node(tmp_path):
    assert DEFAULT_PACKAGES["workflow.tavern"] == "1.2.0"
    assert "workflow.frontend.workbench-panel" not in {
        row["extension_id"] for row in FRONTEND_EXTENSIONS}
    with closing(GraphWorkflowService(tmp_path / "fresh.sqlite")) as service:
        assert {"package_id": "workflow.tavern", "version": "1.2.0"} in service.registry.package_lock
        for component in LOREBOOK_COMPONENTS:
            assert service.registry.get(component, "1") is not None
        assert service.registry.get("frontend.node", "2") is not None
        assert service.registry.get("frontend.node", "1") is None
        for component in ("frontend.state.output", "frontend.state.append", "frontend.presentation"):
            assert service.registry.get(component, "1") is None
        assert not hasattr(service, "_native_runtime")


def test_current_lorebook_resource_save_read_and_reopen_are_preserved(tmp_path):
    database = tmp_path / "lorebook.sqlite"
    identity = ResourceIdentity("workspace", LOREBOOK_RESOURCE_TYPE, str(uuid4())).to_dict()
    record = {
        **identity, "data_schema_version": 1, "update_sequence": 1,
        "value": {"name": "Retained Lorebook", "enabled": True, "entries": [
            {**default_lorebook_entry(), "text": "retained content"}]},
    }
    with closing(GraphWorkflowService(database)) as service:
        app = GraphApplication(service)
        saved = app.command("resource.save", {
            "record": record, "expected_sequence": 0, "idempotency_key": str(uuid4())})
        assert saved["receipt"]["accepted"] == saved["result"]
        assert app.query("resource.read", {"identity": identity}) == record
        assert app.query("resource.list", {"type_id": LOREBOOK_RESOURCE_TYPE}) == [record]
        assert not hasattr(service, "_native_runtime")
    with closing(GraphWorkflowService(database)) as reopened:
        assert GraphApplication(reopened).query("resource.read", {"identity": identity}) == record
        assert reopened.registry.get("lorebook.global-activate", "1") is not None
        assert not hasattr(reopened, "_native_runtime")
