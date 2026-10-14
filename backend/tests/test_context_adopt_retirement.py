"""The old context-adoption command is unavailable; frozen receipts remain readable."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import COMMANDS, RETIRED_RECEIPT_COMMANDS
from phase1_agent.graph_application_identity import (
    APPLICATION_IDENTITY_OPERATION, application_command_identity, application_origin_key,
)
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.server import management_request

from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture
from workflow_test_support import AgentTransportFixture, graph as context_graph, run as run_agent


def _output(session, node_id, port):
    return next(node["outputs"][port] for node in session["nodes"]
                if node["node_binding_id"] == node_id)


def _seed_historical_adoption_receipt(database, parameters, result):
    identity = application_command_identity("context.adopt", "management", parameters)
    native = identity["native_receipt"]
    origin_digest = "workflow-op-v1:" + identity["request_sha256"]
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        connection.execute(
            "INSERT INTO idempotency(operation,key,digest,result_refs,result_payload,request_digest) "
            "VALUES(?,?,?,?,?,?)",
            (APPLICATION_IDENTITY_OPERATION, application_origin_key(identity), origin_digest, "[]",
             canonical_bytes([{"application_identity": identity}]).decode(), origin_digest),
        )
        connection.execute(
            "INSERT INTO idempotency(operation,key,digest,result_refs,result_payload,request_digest) "
            "VALUES(?,?,?,?,?,?)",
            (native["operation"], native["key"], native["request_digest"], "[]",
             canonical_bytes([{"graph_result": result}]).decode(), native["request_digest"]),
        )


def test_context_adopt_entrypoints_are_retired_without_changing_live_merge(tmp_path):
    transport = AgentTransportFixture()
    with closing(GraphWorkflowService(
            tmp_path / "context-adopt-retirement.sqlite",
            public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document = context_graph(service)
        merge = next(node for node in document["nodes"] if node["component_id"] == "context.merge")
        completed = run_agent(service, create(service, document), "Persist through the graph merge.")
        assert completed["status"] == "succeeded", completed["chains"]

        commit = _output(completed, merge["node_binding_id"], "commit")
        current = completed["objects"]["context"]
        assert current["revision"] == 2
        assert commit["receipt"]["object_key"] == "context"
        assert commit["receipt"]["revision"] == current["revision"]
        assert current["value"]["messages"]

        session_id = completed["workflow_session_id"]
        retired_path = f"/api/graph/sessions/{session_id}/context/adopt"
        request = {
            "session_id": session_id,
            "node_id": merge["node_binding_id"],
            "view_output_id": commit["view_ref"]["output_id"],
            "expected_revision": completed["revision"],
            "idempotency_key": str(uuid4()),
        }
        before = service.database.read_bytes()
        assert "context.adopt" not in {item.name for item in COMMANDS}
        assert "context.adopt" in {item.name for item in RETIRED_RECEIPT_COMMANDS}
        assert "context.adopt" not in {
            item["name"] for item in GraphApplication(service).describe()["commands"]
        }
        assert hasattr(service, "adopt_context_view")
        assert not management_request("POST", retired_path, {})
        assert management_request("POST", "/api/graph/commands", {
            "operation": "context.adopt", "parameters": request,
        })

        for invoke in (
            lambda: GraphApplication(service).command("context.adopt", request),
            lambda: dispatch_graph(service, "POST", "/api/graph/commands", {
                "operation": "context.adopt", "parameters": request,
            }),
            lambda: dispatch_graph(
                service, "POST", retired_path,
                {key: value for key, value in request.items() if key != "session_id"},
            ),
        ):
            with pytest.raises(ContractValidationError) as denied:
                invoke()
            assert denied.value.reason_code == "not_found"

        with pytest.raises(ContractValidationError) as direct:
            service.adopt_context_view(
                session_id,
                node_id=request["node_id"],
                view_output_id=request["view_output_id"],
                expected_revision=request["expected_revision"],
                idempotency_key=request["idempotency_key"],
            )
        assert direct.value.reason_code == "context_adoption_retired"
        assert service.database.read_bytes() == before
        assert service.get_session(session_id)["objects"]["context"] == current
        assert len(transport.calls) == 1


@pytest.mark.parametrize("write_result", [False, True], ids=["already-committed", "object-write-receipt"])
def test_exact_historical_context_adopt_receipt_is_read_only_and_bad_receipt_unresolved(
        tmp_path, write_result):
    transport = AgentTransportFixture()
    with closing(GraphWorkflowService(
            tmp_path / "context-adopt-historical-receipt.sqlite",
            public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        document = context_graph(service)
        merge = next(node for node in document["nodes"] if node["component_id"] == "context.merge")
        completed = run_agent(service, create(service, document), "Freeze the old request evidence.")
        assert completed["status"] == "succeeded", completed["chains"]
        commit = _output(completed, merge["node_binding_id"], "commit")
        parameters = {
            "session_id": completed["workflow_session_id"],
            "node_id": merge["node_binding_id"],
            "view_output_id": commit["view_ref"]["output_id"],
            "expected_revision": completed["revision"],
            "idempotency_key": "historical-context-adopt",
        }
        prior_result = {
            "results": [deepcopy(commit["receipt"])] if write_result else [],
            "session": deepcopy(completed),
        }
        prior_result["session"]["revision"] += 1

        _seed_historical_adoption_receipt(service.database, parameters, prior_result)
        before = service.database.read_bytes()
        matched = read_graph_application_receipt(
            service.database, "context.adopt", parameters, scope="management")
        assert matched["outcome"] == "matched"
        assert matched["receipt"]["operation"] == "context.adopt"
        assert matched["result"] == prior_result
        application_read = GraphApplication(service).query("receipt.read", {
            "operation": "context.adopt", "parameters": parameters,
        })
        assert application_read == matched
        assert service.database.read_bytes() == before

        changed_request = {**parameters, "expected_revision": parameters["expected_revision"] + 1}
        mismatch = read_graph_application_receipt(
            service.database, "context.adopt", changed_request, scope="management")
        assert mismatch["outcome"] == "unresolved"
        assert mismatch["reason_code"] == "request_mismatch"
        assert service.database.read_bytes() == before

        with closing(sqlite3.connect(service.database, isolation_level=None)) as connection:
            row = connection.execute(
                "SELECT result_payload FROM idempotency WHERE operation=? AND key=?",
                ("graph.context.adopt", parameters["idempotency_key"]),
            ).fetchone()
            original = row[0]
            mutations = [{"arbitrary": "receipt"}]
            if write_result:
                valid = deepcopy(commit["receipt"])
                mutations.extend([
                    {**valid, "revision": valid["revision"] + 1},
                    {**valid, "revision_id": str(uuid4())},
                ])
            for malformed in mutations:
                payload = loads_strict(original)
                payload[0]["graph_result"]["results"] = [malformed]
                connection.execute(
                    "UPDATE idempotency SET result_payload=? WHERE operation=? AND key=?",
                    (canonical_bytes(payload).decode(), "graph.context.adopt", parameters["idempotency_key"]),
                )
                damaged_database = service.database.read_bytes()
                damaged = read_graph_application_receipt(
                    service.database, "context.adopt", parameters, scope="management")
                assert damaged["outcome"] == "unresolved"
                assert damaged["reason_code"] == "receipt_invalid"
                assert service.database.read_bytes() == damaged_database
