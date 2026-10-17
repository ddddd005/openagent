"""The current graph merge remains independent from retired context adoption."""

from contextlib import closing
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import COMMANDS, RETIRED_RECEIPT_COMMANDS
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.server import management_request

from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture
from workflow_test_support import AgentTransportFixture, graph as context_graph, run as run_agent


def _output(session, node_id, port):
    return next(node["outputs"][port] for node in session["nodes"]
                if node["node_binding_id"] == node_id)


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
