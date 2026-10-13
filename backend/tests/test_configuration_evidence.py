"""Configuration commands and independent original evidence share formal authority."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore
from test_configuration_application import configuration_document, prompt_registry


def parameters(request):
    return {"operation": "definition.update", "idempotency_key": request["idempotency_key"],
            "owner_kind": "workflow-configuration", "owner_id": request["workflow_definition_id"],
            "request": deepcopy(request)}


def request(document, sequence):
    return {"workflow_definition_id": document["workflow_definition_id"],
            "document": deepcopy(document), "expected_edit_sequence": sequence,
            "idempotency_key": str(uuid4())}


def test_formal_configuration_evidence_reopen_and_original_receipt_do_not_read_today_current(tmp_path):
    path, registry = tmp_path / "configuration-evidence.sqlite", prompt_registry()
    with closing(GraphWorkflowService(path, registry=registry)) as service:
        app = GraphApplication(service)
        original = request(configuration_document(registry), 0)
        saved = app.command("request-evidence.write", parameters(original))["result"]
        assert saved["request"] == original and saved["resolution"] == "pending"
        assert service.list_definitions() == []
        service.close()
        with closing(GraphWorkflowService(path, registry=registry)) as reopened:
            other = GraphApplication(reopened)
            query = {"owner_kind": saved["owner_kind"], "owner_id": saved["owner_id"]}
            assert other.query("request-evidence.read", query) == [saved]
            created = other.command("definition.update", original)["result"]
            changed = deepcopy(created["document"])
            changed["nodes"][0]["config"]["text"] = "Today's current"
            current = other.command("definition.update", request(changed, 1))["result"]
            receipt = other.query("receipt.read", {
                "operation": "definition.update", "parameters": saved["request"]})
            assert receipt["outcome"] == "matched" and receipt["result"] == created
            assert current["document"] != created["document"]
            assert reopened.get_definition(original["workflow_definition_id"]) == current["document"]
            verified = other.command("request-evidence.resolve", {
                "operation": saved["operation"], "idempotency_key": saved["idempotency_key"],
                "request_digest": saved["request_digest"], "resolution": "verified",
            })["result"]
            assert verified == {**saved, "resolution": "verified"}
            assert other.query("request-evidence.read", query) == [verified]
        with closing(SqliteStore(path)) as store:
            assert store._connection.execute("SELECT COUNT(*) FROM local_editor_drafts").fetchone()[0] == 0


def test_unknown_configuration_evidence_locks_only_its_owner_and_never_dispatches(tmp_path):
    registry = prompt_registry()
    with closing(GraphWorkflowService(tmp_path / "unknown-evidence.sqlite", registry=registry)) as service:
        app = GraphApplication(service)
        original = request(configuration_document(registry), 0)
        evidence = app.command("request-evidence.write", parameters(original))["result"]
        with pytest.raises(ContractValidationError) as error:
            app.command("request-evidence.write", parameters({**original, "idempotency_key": str(uuid4())}))
        assert error.value.reason_code == "request_evidence_pending"
        receipt = app.query("receipt.read", {"operation": "definition.update", "parameters": original})
        assert receipt["outcome"] != "matched"
        assert service.list_definitions() == []
        assert app.query("request-evidence.read", {
            "owner_kind": evidence["owner_kind"], "owner_id": evidence["owner_id"],
        }) == [evidence]
        unrelated = request(configuration_document(registry), 0)
        other = app.command("request-evidence.write", parameters(unrelated))["result"]
        assert other["owner_id"] != evidence["owner_id"] and other["resolution"] == "pending"
        assert service.list_definitions() == []
