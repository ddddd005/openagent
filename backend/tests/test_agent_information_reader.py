"""Registered default Agent facts are original storage reads, never log copies."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.runtime_fact_store import RuntimeFactStore
from phase1_agent.storage import SqliteStore

from workflow_test_support import run
from workflow_test_support import SummaryTransport, base_graph
from test_graph_service import create
from test_models_service_integration import ModelDatabaseFixture


def test_default_agent_source_pages_original_facts_and_reopens_exact_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-information-test")
    path = tmp_path / "agent-information.sqlite"
    transport = SummaryTransport()
    with closing(GraphWorkflowService(path, public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        final = run(service, create(service, base_graph(service)), "ORION information question")
        assert final["status"] == "succeeded", final["chains"]
        sid, chain_id = final["workflow_session_id"], final["selected_chain_run_id"]
        app = GraphApplication(service)
        source = app.query("registration.list", {
            "session_id": sid, "chain_id": chain_id, "kind": "information_binding",
            "channel_id": "execution-facts", "limit": 100,
        })["items"]
        assert len(source) == 1 and source[0]["availability"] == "history"
        source = source[0]
        parameters = {"session_id": sid, "reference": source["registration_ref"],
                      "owner": source["owner"], "generation": source["generation"],
                      "source_scope": "history", "limit": 2}
        first = app.query("information.read", parameters)
        assert first["next_cursor"] is not None and first["format_id"] == "workflow.executor-facts"
        cursor = first["next_cursor"]
        actual = deepcopy(first["items"])
        while cursor is not None:
            page = app.query("information.read", {**parameters, "cursor": cursor})
            actual.extend(page["items"])
            cursor = page["next_cursor"]
        expected = [item for item in service.get_run(sid, chain_id)["runtime_facts"]
                    if item.get("owner") == source["owner"] and item.get("generation") == source["generation"]]
        assert actual == expected and len(transport.calls) == 1
        assert app.query("session.read", {"session_id": sid}) == final
        with pytest.raises(ContractValidationError) as denied:
            app.for_consumer().query("information.read", parameters)
        assert denied.value.reason_code == "information_read_denied"
        assert app.for_consumer().query("registration.list", {
            "session_id": sid, "kind": "information_binding",
        })["items"] == []
        with pytest.raises(ContractValidationError) as unavailable:
            app.query("information.read", {**parameters, "source_scope": "live"})
        assert unavailable.value.reason_code == "information_live_unavailable"
    with closing(GraphWorkflowService(path, public_model_factory=transport.factory)) as service:
        second = GraphApplication(service).query("information.read", {
            **parameters, "cursor": first["next_cursor"],
        })
        assert second["items"] == expected[2:4]
        assert len(transport.calls) == 1


def fact_owner():
    return dict(zip(("workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"),
                    (str(uuid4()) for _ in range(4))))


def test_fact_reader_generation_cursor_and_byte_pages_are_bounded(tmp_path):
    owner = fact_owner()
    reference = {"executor_id": "sample.work", "exact_version": "1"}
    stream = "executor:sample.work@1"
    with closing(SqliteStore(tmp_path / "reader.sqlite")) as store:
        facts = RuntimeFactStore(store)
        store._connection.execute("BEGIN IMMEDIATE")
        for sequence in range(1, 6):
            facts.accept(owner=owner, stream_id=stream, fact={
                "fact_id": str(sequence), "sequence": sequence, "owner": owner,
                "generation": 1 if sequence < 5 else 2,
                "payload": {"text": "x" * 1_500_000},
            })
        store._connection.execute("COMMIT")
        page = facts.read_executor_page(owner, reference, 1, limit=100)
        assert [item["sequence"] for item in page["items"]] == [1, 2]
        assert page["next_cursor"] is not None
        second = facts.read_executor_page(owner, reference, 1, limit=100, cursor=page["next_cursor"])
        assert [item["sequence"] for item in second["items"]] == [3, 4]
        assert second["next_cursor"] is None
        assert [item["sequence"] for item in facts.read_executor_page(owner, reference, 2)["items"]] == [5]
        with pytest.raises(ContractValidationError) as denied:
            facts.read_executor_page(owner, reference, 2, cursor=page["next_cursor"])
        assert denied.value.reason_code == "information_invalid_cursor"
        with pytest.raises(ContractValidationError) as denied:
            facts.read_executor_page(owner, reference, 1, cursor="invalid")
        assert denied.value.reason_code == "information_invalid_cursor"
        # Missing cursor position is an explicit gap, never a pretend full history.
        store._connection.execute("DELETE FROM workflow_runtime_facts WHERE sequence=2")
        assert facts.read_executor_page(owner, reference, 1, cursor=page["next_cursor"]) == {
            "items": [], "next_cursor": None, "status": "gap",
        }
