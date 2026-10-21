"""Configuration functional and data interfaces are on the real production paths."""

from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from phase1_agent.configuration_data import ConfigurationDataSession
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService

from test_configuration_mutations import document
from test_frontend_node_operations import prepared


@pytest.fixture
def service(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "configuration-interface.sqlite")) as instance:
        yield instance


def record_interfaces(service, monkeypatch, calls):
    interface = service.configuration_service

    original_update = interface.update_definition

    def update(*args, **kwargs):
        calls.append("interface.update_definition")
        return original_update(*args, **kwargs)

    monkeypatch.setattr(interface, "update_definition", update)

    original_patch = interface.patch_exact_configuration

    def patch(data, **kwargs):
        assert isinstance(data, ConfigurationDataSession)
        calls.append("interface.patch_exact_configuration")
        return original_patch(data, **kwargs)

    monkeypatch.setattr(interface, "patch_exact_configuration", patch)

    original_business_patch = service._patch_node_configuration

    def business_patch(data, **kwargs):
        assert isinstance(data, ConfigurationDataSession)
        calls.append("business.patch_node_configuration")
        return original_business_patch(data, **kwargs)

    monkeypatch.setattr(service, "_patch_node_configuration", business_patch)

    original_business_save = service._save_configuration_update

    def business_save(data, *args, **kwargs):
        assert isinstance(data, ConfigurationDataSession)
        calls.append("business.save_configuration_update")
        return original_business_save(data, *args, **kwargs)

    monkeypatch.setattr(service, "_save_configuration_update", business_save)

    original_current = ConfigurationDataSession.save_current_configuration

    def save_current(self, *args, **kwargs):
        calls.append("data.save_current_configuration")
        return original_current(self, *args, **kwargs)

    monkeypatch.setattr(ConfigurationDataSession, "save_current_configuration", save_current)

    original_fixed = ConfigurationDataSession.save_fixed_revision

    def save_fixed(self, *args, **kwargs):
        calls.append("data.save_fixed_revision")
        return original_fixed(self, *args, **kwargs)

    monkeypatch.setattr(ConfigurationDataSession, "save_fixed_revision", save_fixed)


def test_workbench_command_uses_functional_business_and_data_interfaces(service, monkeypatch):
    calls = []
    record_interfaces(service, monkeypatch, calls)
    value = document()
    key = str(uuid4())
    result = GraphApplication(service).command("definition.update", {
        "workflow_definition_id": value["workflow_definition_id"],
        "document": value, "expected_edit_sequence": 0, "idempotency_key": key,
    })["result"]

    assert result["outcome"] == "saved"
    assert calls == [
        "interface.update_definition", "business.save_configuration_update",
        "data.save_fixed_revision", "data.save_current_configuration",
    ]
    with closing(service._store()) as store:
        assert store.read_receipt("graph.definition.update", key) is not None

    with closing(GraphManagementService(service.database)) as reader:
        assert reader.get_definition(value["workflow_definition_id"])["nodes"][0]["config"]["text"] == "A"
        assert reader.get_definition(value["workflow_definition_id"], 1)["nodes"][0]["config"]["text"] == "A"


def test_frontend_node_patch_uses_same_business_and_data_interfaces(service, monkeypatch):
    calls = []
    record_interfaces(service, monkeypatch, calls)
    bound, graph, _, _ = prepared(service)
    calls.clear()

    result = bound.patch_input("B", expected_edit_sequence=1, idempotency_key="interface-patch")

    assert result["outcome"] == "saved" and result["edit_sequence"] == 2
    assert calls == [
        "interface.patch_exact_configuration", "business.patch_node_configuration",
        "business.save_configuration_update", "data.save_current_configuration",
    ]
    with closing(service._store()) as store:
        assert store.read_receipt("graph.frontend-node.patch-input", "interface-patch") is not None
        assert store.read_receipt("graph.configuration.patch-node", "interface-patch") is None

    with closing(GraphManagementService(service.database)) as reader:
        current = reader.get_definition(graph["workflow_definition_id"])
        assert next(node["config"]["text"] for node in current["nodes"]
                    if node["component_id"] == "tools.editable-input") == "B"
        fixed = reader.get_definition(graph["workflow_definition_id"], 1)
        assert next(node["config"]["text"] for node in fixed["nodes"]
                    if node["component_id"] == "tools.editable-input") == "message 0"


def test_configuration_business_and_interface_have_no_direct_storage_access():
    source = Path(__file__).resolve().parents[1] / "src" / "phase1_agent"
    for name in ("workflow_configuration.py", "configuration_service.py"):
        text = (source / name).read_text(encoding="utf-8")
        for token in ("GraphRecordStore", "SqliteStore", "self._store(", "self._change(",
                      ".rows(", ".put(", "repo."):
            assert token not in text, (name, token)

    data = (source / "configuration_data.py").read_text(encoding="utf-8")
    assert "class ConfigurationRepository" in data
    assert "class WorkflowDataManagement" in data
    assert "GraphRecordStore" in data
    assert "initialize_configuration_tables" in data
    assert "from .configuration_data import initialize_configuration_tables" in (
        source / "storage.py").read_text(encoding="utf-8")

    operations = (source / "frontend_node_operations.py").read_text(encoding="utf-8")
    for token in ("GraphRecordStore", "self._store(", "self._document(", "self._change("):
        assert token not in operations, ("frontend_node_operations.py", token)
