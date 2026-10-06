"""Application commands share historical definition and public-read authorities."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from test_graph_service import service, text_graph, uid


def run_application(app, service, session, key):
    response = app.command("run.start", {
        "session_id": session["workflow_session_id"],
        "expected_revision": session["revision"],
        "idempotency_key": key,
    })
    service.wait(response["result"]["active_chain_run_id"])
    result = app.query("session.read", {"session_id": session["workflow_session_id"]})
    assert result["status"] == "succeeded"
    return result


def candidate_request(session, candidate_id, key):
    return {
        "session_id": session["workflow_session_id"],
        "candidate_id": candidate_id,
        "expected_revision": session["revision"],
        "expected_data_revision": session["data_revision"],
        "expected_head_revision": session["head_revision"],
        "idempotency_key": key,
    }


def test_application_restore_and_next_run_use_target_definition(service):
    app = GraphApplication(service)
    original = text_graph(service.registry)
    original["nodes"][-1]["public_outputs"] = ["output"]
    app.command("definition.save", {
        "document": original, "expected_revision": 0, "idempotency_key": "publish-d1",
    })
    session = app.command("session.create", {
        "workflow_definition_id": original["workflow_definition_id"],
        "definition_revision": 1, "idempotency_key": "create",
    })["result"]
    first = run_application(app, service, session, "run-d1")
    original_chain = first["selected_chain_run_id"]

    edited = deepcopy(original)
    edited["revision"] = 2
    edited["nodes"][0]["config"]["text"] = "changed after the first round"
    app.command("definition.save", {
        "document": edited, "expected_revision": 1, "idempotency_key": "publish-d2",
    })
    rebound = app.command("session.rebind", {
        "session_id": first["workflow_session_id"], "definition_revision": 2,
        "expected_revision": first["revision"],
        "expected_data_revision": first["data_revision"],
        "expected_head_revision": first["head_revision"], "idempotency_key": "bind-d2",
    })["result"]
    second = run_application(app, service, rebound, "run-d2")
    assert second["nodes"][-1]["outputs"]["output"]["text"] == edited["nodes"][0]["config"]["text"]
    candidates = app.query("candidate.list", {"session_id": session["workflow_session_id"]})
    candidate = next(item for item in candidates["candidates"]
                     if item["chain_run_id"] == original_chain)
    request = candidate_request(second, candidate["candidate_id"], "select-d1")
    restored = app.command("candidate.select", request)
    assert restored["result"]["definition_revision"] == 1
    assert app.query("definition.read", {
        "identity": restored["result"]["workflow_definition_id"], "revision": 1,
    }) == original

    third = run_application(app, service, restored["result"], "run-after-restore")
    assert third["definition_revision"] == 1
    assert third["nodes"][-1]["outputs"]["output"]["text"] == "pear pear"
    # A later query can advance while the accepted operation receipt stays fixed.
    assert app.command("candidate.select", request) == restored
    assert app.query("session.read", {
        "session_id": session["workflow_session_id"],
    })["selected_chain_run_id"] == third["selected_chain_run_id"]


def test_application_fork_keeps_historical_public_source_and_scope(service):
    app = GraphApplication(service)
    original = text_graph(service.registry)
    original["nodes"][-1]["public_outputs"] = ["output"]
    app.command("definition.save", {
        "document": original, "expected_revision": 0, "idempotency_key": "publish",
    })
    session = app.command("session.create", {
        "workflow_definition_id": original["workflow_definition_id"],
        "definition_revision": 1, "idempotency_key": "create",
    })["result"]
    completed = run_application(app, service, session, "run-parent")
    candidates = app.query("candidate.list", {"session_id": session["workflow_session_id"]})
    candidate = candidates["candidates"][0]
    request = candidate_request(completed, candidate["candidate_id"], "fork")
    fork = app.command("candidate.fork", request)
    child = fork["result"]
    assert child["workflow_session_id"] != completed["workflow_session_id"]
    assert child["workflow_definition_id"] == original["workflow_definition_id"]
    assert child["definition_revision"] == 1
    assert app.command("candidate.fork", request) == fork

    consumer = GraphApplication(service, scope="consumer")
    port = {
        "session_id": child["workflow_session_id"],
        "workflow_definition_id": original["workflow_definition_id"],
        "definition_revision": 1, "node_id": uid(3), "port_id": "output",
    }
    inherited = consumer.query("output.read", port)["output"]
    assert inherited["payload"]["text"] == "pear pear"
    assert inherited["source"]["workflow_session_id"] == completed["workflow_session_id"]
    for operation in ("session.read", "object.list", "candidate.list"):
        with pytest.raises(ContractValidationError) as error:
            consumer.query(operation, {"session_id": child["workflow_session_id"]})
        assert error.value.reason_code == "application_scope_denied"
    with pytest.raises(ContractValidationError) as error:
        consumer.command("candidate.select", candidate_request(child, candidate["candidate_id"], str(uuid4())))
    assert error.value.reason_code == "application_scope_denied"
