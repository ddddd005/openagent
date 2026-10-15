"""Retired cross-workflow copies deny writes and preserve frozen receipt evidence."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.session_objects import SessionObjectStore

from test_candidate_mutation_retirement import _insert_test_only_legacy_receipt
from test_graph_application_receipt_custody import raw_database
from test_graph_receipt_http import post, serve_receipts
from test_graph_service import document, edge, service
from test_stage4_model_request_installation import _node


def _source_and_request(service):
    source = _node(service, "tools.text", "1", text="copy fixture")
    output = _node(service, "tools.output", "1")
    graph = document([source, output], [edge(source, output, 1)])
    graph.update(schema_version=3, package_lock=deepcopy(list(service.registry.execution_package_lock)))
    saved = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))
    source = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"], idempotency_key=str(uuid4()))
    target = deepcopy(saved["document"])
    target.update(schema_version=2, workflow_definition_id=str(uuid4()), revision=1)
    target.pop("edit_sequence", None)
    for node in target["nodes"]:
        node.pop("configuration_policy", None)
    return source, {
        "session_id": source["workflow_session_id"], "document": target,
        "expected_session_revision": source["revision"], "expected_data_revision": source["data_revision"],
        "expected_definition_revision": source["definition_revision"],
        "expected_head_revision": source["head_revision"], "idempotency_key": str(uuid4()),
    }


def test_copy_is_absent_from_discovery_service_and_both_http_writes(service):
    source, request = _source_and_request(service)
    application = GraphApplication(service)
    assert "session.copy" not in {row["name"] for row in application.describe()["commands"]}
    for method in ("copy_session", "_map_state", "_copied_data"):
        assert not hasattr(service, method)
    for method in ("restore_logical", "restore", "rebind"):
        assert not hasattr(SessionObjectStore, method)
    before = raw_database(service.database)

    for invoke in (
        lambda: application.command("session.copy", request),
        lambda: dispatch_graph(service, "POST", "/api/graph/commands", {
            "operation": "session.copy", "parameters": request,
        }),
        lambda: dispatch_graph(service, "POST",
                               f"/api/graph/sessions/{source['workflow_session_id']}/copy",
                               {key: value for key, value in request.items() if key != "session_id"}),
    ):
        with pytest.raises(ContractValidationError) as denied:
            invoke()
        assert denied.value.reason_code == "not_found"
    assert raw_database(service.database) == before
    assert service.get_session(source["workflow_session_id"]) == source
    assert service.list_sessions(request["document"]["workflow_definition_id"]) == []


def test_old_copy_receipt_matches_exact_request_without_runtime_or_writes(service, monkeypatch):
    source, request = _source_and_request(service)
    result = deepcopy(source)
    result.update(
        workflow_definition_id=request["document"]["workflow_definition_id"],
        workflow_session_id=str(uuid4()), revision=1, head_revision=1, head_commit_id=str(uuid4()),
        source={"kind": "copy_current", "workflow_session_id": source["workflow_session_id"],
                "workflow_definition_id": source["workflow_definition_id"],
                "definition_revision": source["definition_revision"],
                "head_commit_id": source["head_commit_id"], "data_revision": source["data_revision"],
                "state_mappings": []},
    )
    _insert_test_only_legacy_receipt(service.database, "session.copy", "management", request, result)
    before = service.database.read_bytes()
    matched = read_graph_application_receipt(service.database, "session.copy", request)
    assert matched["outcome"] == "matched", matched
    assert matched["result"] == result
    with serve_receipts(service.database, monkeypatch) as (_, port):
        assert post(port, "/api/graph/receipts/read", {
            "operation": "session.copy", "parameters": request,
        }) == (200, matched)

    changed = deepcopy(request)
    changed["document"]["name"] = "Changed request"
    unresolved = read_graph_application_receipt(service.database, "session.copy", changed)
    assert unresolved["outcome"] == "unresolved" and unresolved["reason_code"] == "request_mismatch"
    wrong_scope = read_graph_application_receipt(service.database, "session.copy", request, scope="consumer")
    assert wrong_scope["outcome"] == "unresolved" and wrong_scope["reason_code"] == "application_scope_denied"
    assert service.database.read_bytes() == before
