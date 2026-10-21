from contextlib import closing
from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import COMMANDS, QUERIES, RETIRED_RECEIPT_COMMANDS
from phase1_agent.graph_application_identity import application_command_receipt, bind_application_command
from phase1_agent.graph_drafts import LocalDraftStore, validate_draft_address, validate_draft_value
from phase1_agent.graph_records import require
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_receipts import read_graph_application_receipt
from test_graph_application_receipt_custody import raw_database
from test_graph_service import service, text_graph


def _legacy_definition_receipt(service):
    doc = text_graph(service.registry)
    request = {"document": deepcopy(doc), "expected_revision": 0,
               "idempotency_key": str(uuid4())}
    result = service._change(
        "graph.definition.save", request["idempotency_key"],
        {"document": request["document"], "expected_revision": request["expected_revision"]},
        lambda repo: service._save_definition(repo, request["document"], 0))
    return doc, request, result


def _legacy_management_draft(service, request):
    address = validate_draft_address(request["address"], "management")
    validate_draft_value(address, request["format_version"], request["value"])
    require(type(request["expected_sequence"]) is int and 0 <= request["expected_sequence"] <= 2**53 - 1,
            "invalid_request", "Draft expected sequence is invalid")
    native_request = {key: value for key, value in request.items() if key != "idempotency_key"}
    with bind_application_command("draft.save", "management", request):
        with closing(service._store()) as store:
            result = LocalDraftStore(store).save(
                "graph.draft.save", native_request, request["idempotency_key"],
                lambda address, value: service._draft_owner(store, address, value))
    receipt = application_command_receipt(
        "draft.save", "management", request, result, authority="service_receipt")
    return {
        "schema_version": 1, "kind": "workflow.application-command",
        "receipt": receipt, "result": result,
    }


def _schema3_fixed(document):
    result = deepcopy(document)
    result["schema_version"] = 3
    for node_record in result["nodes"]:
        node_record["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    return result


def test_definition_save_receipt_read_is_retired_but_stored_rows_remain(service):
    doc, request, _ = _legacy_definition_receipt(service)
    app = GraphApplication(service)
    names = {item.name for item in COMMANDS}
    retired = {item.name for item in RETIRED_RECEIPT_COMMANDS}
    assert "definition.save" not in names and "definition.save" not in retired
    assert "definition.save" not in {item["name"] for item in app.describe()["commands"]}
    assert not hasattr(service, "save_definition")

    with closing(service._store()) as store:
        before = store.read_receipt("graph.definition.save", request["idempotency_key"])
    before_database = raw_database(service.database)
    with pytest.raises(ContractValidationError) as direct:
        app.command("definition.save", request)
    assert direct.value.reason_code == "not_found"
    with pytest.raises(ContractValidationError) as named_http:
        dispatch_graph(service, "POST", "/api/graph/commands",
                       {"operation": "definition.save", "parameters": request})
    assert named_http.value.reason_code == "not_found"
    with pytest.raises(ContractValidationError) as route:
        dispatch_graph(service, "POST", "/api/graph/definitions", {
            **request, "document": _schema3_fixed(doc),
        })
    assert route.value.reason_code == "not_found"

    # Receipt reads return the existing unsupported outcome; no new rejection protocol.
    frozen = read_graph_application_receipt(service.database, "definition.save", request)
    assert frozen == {"schema_version": 1, "kind": "workflow.application-receipt-read",
                      "outcome": "unresolved", "reason_code": "unsupported_operation"}
    assert app.query("receipt.read", {
        "operation": "definition.save", "parameters": request}) == frozen
    assert raw_database(service.database) == before_database
    with closing(service._store()) as store:
        assert store.read_receipt("graph.definition.save", request["idempotency_key"]) == before
    assert app.query("definition.read", {"identity": doc["workflow_definition_id"]}) == doc


def test_retired_definition_save_receipt_read_never_opens_a_database(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A retired definition.save receipt read must not open storage")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    request = {"document": {"workflow_definition_id": str(uuid4()), "revision": 1, "nodes": []},
               "expected_revision": 0, "idempotency_key": str(uuid4())}
    assert read_graph_application_receipt(
        tmp_path / "not-created.sqlite", "definition.save", request) == {
            "schema_version": 1, "kind": "workflow.application-receipt-read",
            "outcome": "unresolved", "reason_code": "unsupported_operation",
        }
    assert not (tmp_path / "not-created.sqlite").exists()


def test_definition_update_keeps_internal_revision_creation(service):
    app = GraphApplication(service)
    doc = _schema3_fixed(text_graph(service.registry))
    created = app.command("definition.update", {
        "workflow_definition_id": doc["workflow_definition_id"], "document": doc,
        "expected_edit_sequence": 0, "idempotency_key": str(uuid4()),
    })["result"]
    assert created["outcome"] == "saved" and created["definition_revision"] == 1
    changed = deepcopy(created["document"])
    changed["nodes"][0]["config"]["text"] = "Updated through configuration"
    updated = app.command("definition.update", {
        "workflow_definition_id": doc["workflow_definition_id"], "document": changed,
        "expected_edit_sequence": 1, "idempotency_key": str(uuid4()),
    })["result"]
    assert updated["outcome"] == "saved" and updated["definition_revision"] == 2


def test_management_draft_writer_retires_but_evidence_readers_and_receipts_remain(service):
    target = {"scope": "management", "kind": "intent", "key": "legacy-intent:v1",
              "workflow_definition_id": None, "workflow_session_id": None}
    request = {"address": target, "format_version": 1,
               "value": {"pending": {"operation": "definition.update", "body": {"idempotency_key": "kept"}}},
               "expected_sequence": 0, "idempotency_key": str(uuid4())}
    accepted = _legacy_management_draft(service, request)
    app = GraphApplication(service)
    names = {item.name for item in COMMANDS}
    retired = {item.name for item in RETIRED_RECEIPT_COMMANDS}
    query_names = {item.name for item in QUERIES}
    assert "draft.save" not in names and "draft.save" in retired
    assert {"draft.read", "draft.list"} <= query_names
    assert not hasattr(service, "save_draft")
    assert not hasattr(service, "_save_draft")

    with pytest.raises(ContractValidationError) as direct:
        app.command("draft.save", request)
    assert direct.value.reason_code == "not_found"
    with pytest.raises(ContractValidationError) as named_http:
        dispatch_graph(service, "POST", "/api/graph/commands",
                       {"operation": "draft.save", "parameters": request})
    assert named_http.value.reason_code == "not_found"
    assert app.query("draft.read", {"address": target}) == accepted["result"]
    assert app.query("draft.list", {"kind": "intent"})["records"] == [accepted["result"]]

    frozen = read_graph_application_receipt(service.database, "draft.save", request)
    assert frozen["outcome"] == "matched" and frozen["result"] == accepted["result"]
    assert frozen["receipt"] == accepted["receipt"]
