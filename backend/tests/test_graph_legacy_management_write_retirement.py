from contextlib import closing
from copy import deepcopy
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
from test_graph_service import service, text_graph


def _legacy_definition_receipt(service):
    doc = text_graph(service.registry)
    request = {"document": deepcopy(doc), "expected_revision": 0,
               "idempotency_key": str(uuid4())}
    with bind_application_command("definition.save", "management", request):
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


def test_definition_save_is_not_discoverable_or_dispatchable_but_old_receipts_read(service):
    doc, request, result = _legacy_definition_receipt(service)
    app = GraphApplication(service)
    names = {item.name for item in COMMANDS}
    retired = {item.name for item in RETIRED_RECEIPT_COMMANDS}
    assert "definition.save" not in names and "definition.save" in retired
    assert "definition.save" not in {item["name"] for item in app.describe()["commands"]}
    assert not hasattr(service, "save_definition")

    with closing(service._store()) as store:
        before = store.read_receipt("graph.definition.save", request["idempotency_key"])
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

    frozen = read_graph_application_receipt(service.database, "definition.save", request)
    assert frozen["outcome"] == "matched" and frozen["result"] == result == doc
    assert app.query("definition.read", {"identity": doc["workflow_definition_id"]}) == doc
    with closing(service._store()) as store:
        assert store.read_receipt("graph.definition.save", request["idempotency_key"]) == before


def test_definition_update_and_session_copy_keep_internal_revision_creation(service):
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

    source_doc, _, _ = _legacy_definition_receipt(service)
    source = service.create_session(
        source_doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    target_doc = deepcopy(source_doc)
    target_doc["workflow_definition_id"] = str(uuid4())
    copied = service.copy_session(
        source["workflow_session_id"], document=target_doc,
        expected_session_revision=source["revision"], expected_data_revision=source["data_revision"],
        expected_definition_revision=source["definition_revision"],
        expected_head_revision=source["head_revision"], idempotency_key=str(uuid4()))
    assert copied["workflow_definition_id"] == target_doc["workflow_definition_id"]
    assert service.get_definition(target_doc["workflow_definition_id"]) == target_doc


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
    with pytest.raises(ContractValidationError):
        service._save_draft(**request)

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
