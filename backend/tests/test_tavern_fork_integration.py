"""Consumer forks inherit exact completed tavern rounds, never display indexes."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_service import GraphWorkflowService

from workflow_test_support import run
from test_context_native_integration import NativeTransport, native_graph, node_output, rebind
from test_graph_service import copy_current, create
from test_models_service_integration import ModelDatabaseFixture
from test_tavern_chat_integration import add_chat, chat_graph


def candidates(service, view):
    return GraphApplication(service).for_consumer().query("consumer.candidate.list", {
        "session_id": view["workflow_session_id"],
        "workflow_definition_id": view["workflow_definition_id"],
        "definition_revision": view["definition_revision"],
    })


def fork_request(view, candidate_id, *, key=None):
    return {
        "session_id": view["workflow_session_id"], "candidate_id": candidate_id,
        "expected_revision": view["revision"], "expected_data_revision": view["data_revision"],
        "expected_head_revision": view["head_revision"], "idempotency_key": key or str(uuid4()),
    }


def fork(service, request):
    return GraphApplication(service).for_consumer().command("consumer.candidate.fork", request)


def transcript(service, view, doc, chat):
    parameters = {
        "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": doc["revision"],
        "node_id": chat["presentation"]["node_binding_id"], "port_id": "display",
    }
    public = service.read_public_output(view["workflow_session_id"], **parameters)["output"]
    artifacts = [service.read_public_artifact(
        view["workflow_session_id"], **parameters, run_id=public["run_id"],
        reference=entry["source_ref"]) for entry in public["payload"]["entries"]]
    return public, artifacts


def test_native_tavern_fork_uses_old_completed_definition_context_and_display_then_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-tavern-fork")
    transport = NativeTransport()
    database = tmp_path / "native-fork.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, native = native_graph(service, policy=False, once=False)
        current = next(node for node in doc["nodes"] if node["component_id"] == "tools.current-input")
        chat = add_chat(service, doc, current, native["a"]["agent"], after=native["a"]["merge"])
        first = run(service, create(service, doc), "First checkpoint input.")
        assert first["status"] == "succeeded", first["chains"]
        first_snapshot = deepcopy(first)
        first_public, first_artifacts = transcript(service, first, doc, chat)
        first_chain = first["selected_chain_run_id"]
        initial_candidates = candidates(service, first)
        first_candidate = next(row for row in initial_candidates["candidates"] if row["chain_run_id"] == first_chain)
        assert initial_candidates["can_fork"]
        assert set(first_candidate) == {
            "candidate_id", "chain_run_id", "source_session_id",
            "source_workflow_definition_id", "source_definition_revision",
        }
        assert [entry["role"] for entry in first_public["payload"]["entries"]] == ["user", "assistant"]
        assert {artifact["producer"]["chain_run_id"] for artifact in first_artifacts} == {first_chain}
        assert {artifact["producer"]["workflow_session_id"] for artifact in first_artifacts} == {first["workflow_session_id"]}
        assert [artifact["value"]["text"] for artifact in first_artifacts] == [
            "First checkpoint input.", "Native accepted answer"]
        newer = deepcopy(doc)
        next(node for node in newer["nodes"] if node["node_binding_id"] == native["a"]["fixed"]["node_binding_id"])[
            "config"]["text"] = "SECOND_DEFINITION_RULE"
        rebound, newer = rebind(service, first, newer)
        second = run(service, rebound, "Second parent-only input.")
        assert second["status"] == "succeeded", second["chains"]
        assert len(transport.calls) == 2 and "SECOND_DEFINITION_RULE" in str(transport.calls[-1])
        before_parent = deepcopy(service.get_session(second["workflow_session_id"]))
        before_consumer = deepcopy(service.get_consumer(second["workflow_session_id"]))
        request = fork_request(second, first_candidate["candidate_id"])
        result = fork(service, request)["result"]
        receipt = result["receipt"]
        child_sid = receipt["workflow_session_id"]
        child = service.get_session(child_sid)
        assert child_sid != second["workflow_session_id"]
        assert child["revision"] == 1 and child["definition_revision"] == 1
        assert child["source"]["candidate_commit_id"] == first_candidate["candidate_id"]
        assert receipt["operation"] == "fork" and receipt["chain_run_id"] is None
        assert receipt["fork_source"] == {
            "workflow_session_id": second["workflow_session_id"], "candidate_id": first_candidate["candidate_id"],
            "chain_run_id": first_chain, "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1,
        }
        assert service.get_session(second["workflow_session_id"]) == before_parent
        assert service.get_consumer(second["workflow_session_id"]) == before_consumer
        assert child["history_refs"] == [first_chain]
        assert [row["chain_run_id"] for row in child["inherited_history"]] == [first_chain]
        for key in ("context-a", "tavern-chat"):
            assert child["objects"][key]["value"] == first_snapshot["objects"][key]["value"]
            assert child["objects"][key]["binding"] == first_snapshot["objects"][key]["binding"]
        inherited_public, inherited_artifacts = transcript(service, child, doc, chat)
        assert inherited_public["payload"] == first_public["payload"]
        assert inherited_artifacts == [{**artifact, "workflow_session_id": child_sid, "session_revision": 1}
                                      for artifact in first_artifacts]
        assert inherited_public["source"]["workflow_session_id"] == second["workflow_session_id"]
        assert [row["chain_run_id"] for row in result["consumer"]["history"]] == [first_chain]
        assert all(row["inherited"] for row in result["consumer"]["history"])
        assert len(transport.calls) == 2
        child_next = run(service, child, "Child continuation.")
        assert child_next["status"] == "succeeded", child_next["chains"]
        assert len(transport.calls) == 3
        child_prompt = node_output(child_next, native["a"]["assembly"])
        assert "First checkpoint input." in str(child_prompt["messages"])
        assert "Second parent-only input." not in str(child_prompt["messages"])
        assert "SECOND_DEFINITION_RULE" not in str(child_prompt["messages"])
        _, child_artifacts = transcript(service, child_next, doc, chat)
        assert [artifact["value"]["text"] for artifact in child_artifacts] == [
            "First checkpoint input.", "Native accepted answer", "Child continuation.", "Native accepted answer"]
        assert service.get_session(second["workflow_session_id"]) == before_parent
        third_parent = run(service, second, "Third parent-only input.")
        assert third_parent["status"] == "succeeded"
        repeated = fork(service, request)["result"]
        assert repeated["receipt"] == receipt
        assert repeated["consumer"]["workflow_session_id"] == child_sid
        assert len(service.list_sessions(doc["workflow_definition_id"])) == 2
        assert len(transport.calls) == 4
        cold_request, cold_receipt = deepcopy(request), deepcopy(receipt)
    readonly = read_graph_application_receipt(
        database, "consumer.candidate.fork", cold_request, scope="consumer")
    assert readonly["outcome"] == "matched", readonly
    assert readonly["result"] == {"receipt": cold_receipt}
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as reopened:
        cold_child = reopened.get_session(child_sid)
        assert cold_child["objects"]["tavern-chat"]["value"] == child_next["objects"]["tavern-chat"]["value"]
        assert len(transport.calls) == 4


@pytest.mark.parametrize("field", ["expected_revision", "expected_data_revision", "expected_head_revision"])
def test_consumer_fork_rejects_stale_cas_without_creating_a_child(tmp_path, field):
    with closing(GraphWorkflowService(tmp_path / (field + ".sqlite"))) as service:
        doc, _ = chat_graph(service)
        final = run(service, create(service, doc), "First.")
        candidate = candidates(service, final)["candidates"][0]
        request = fork_request(final, candidate["candidate_id"])
        request[field] += 1
        before = deepcopy(service.get_session(final["workflow_session_id"]))
        with pytest.raises(ContractValidationError) as rejected:
            fork(service, request)
        assert rejected.value.reason_code in {
            "stale_revision", "stale_data_revision", "stale_head_revision",
        }
        assert service.get_session(final["workflow_session_id"]) == before
        assert len(service.list_sessions(doc["workflow_definition_id"])) == 1
        assert read_graph_application_receipt(
            service.database, "consumer.candidate.fork", request, scope="consumer")["outcome"] == "unresolved"


def test_consumer_fork_rejects_foreign_missing_seed_and_direct_management_operations(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "scope.sqlite")) as service:
        doc, _ = chat_graph(service)
        initial = create(service, doc)
        first = run(service, initial, "Own.")
        foreign = run(service, service.create_session(doc["workflow_definition_id"], 1,
                      idempotency_key=str(uuid4())), "Foreign.")
        other = candidates(service, foreign)["candidates"][0]
        app = GraphApplication(service).for_consumer()
        before = deepcopy(service.get_session(first["workflow_session_id"]))
        for candidate_id in (other["candidate_id"], str(uuid4()), initial["head_commit_id"]):
            with pytest.raises(ContractValidationError):
                fork(service, fork_request(first, candidate_id))
        for operation, parameters, command in (
            ("candidate.select", fork_request(first, candidates(service, first)["candidates"][0]["candidate_id"]), True),
            ("candidate.fork", fork_request(first, candidates(service, first)["candidates"][0]["candidate_id"]), True),
            ("object.list", {"session_id": first["workflow_session_id"]}, False),
            ("candidate.list", {"session_id": first["workflow_session_id"]}, False),
        ):
            with pytest.raises(ContractValidationError) as denied:
                (app.command if command else app.query)(operation, parameters)
            assert denied.value.reason_code == "application_scope_denied"
        with pytest.raises(ContractValidationError):
            app.query("consumer.candidate.list", {
                "session_id": first["workflow_session_id"], "workflow_definition_id": str(uuid4()), "definition_revision": 1,
            })
        assert service.get_session(first["workflow_session_id"]) == before
        assert len(service.list_sessions(doc["workflow_definition_id"])) == 2


def test_consumer_filters_inherited_foreign_workflow_identity_but_management_fork_remains(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "identity-boundary.sqlite")) as service:
        doc, _ = chat_graph(service)
        parent = run(service, create(service, doc), "Parent in original workflow.")
        candidate = candidates(service, parent)["candidates"][0]
        copied_doc = deepcopy(doc)
        copied_doc.update(workflow_definition_id=str(uuid4()), revision=1)
        copied = copy_current(service, parent, copied_doc)
        assert parent["selected_chain_run_id"] in copied["history_refs"]
        projected = candidates(service, copied)
        assert projected["workflow_definition_id"] == copied_doc["workflow_definition_id"]
        assert projected["candidates"] == []
        before = deepcopy(service.get_session(copied["workflow_session_id"]))
        with pytest.raises(ContractValidationError) as rejected:
            fork(service, fork_request(copied, candidate["candidate_id"]))
        assert rejected.value.reason_code == "consumer_candidate_scope_mismatch"
        assert service.get_session(copied["workflow_session_id"]) == before
        assert len(service.list_sessions(doc["workflow_definition_id"])) == 1
        assert len(service.list_sessions(copied_doc["workflow_definition_id"])) == 1
        trusted = GraphApplication(service).command("candidate.fork", fork_request(copied, candidate["candidate_id"]))["result"]
        assert trusted["workflow_definition_id"] == doc["workflow_definition_id"]
        assert trusted["history_refs"] == [parent["selected_chain_run_id"]]
        assert service.get_session(copied["workflow_session_id"]) == before


def test_active_paused_partial_and_failed_rounds_cannot_become_fork_targets(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "unstable.sqlite")) as service:
        doc, chat = chat_graph(service)
        first = run(service, create(service, doc), "Completed first.")
        candidate = candidates(service, first)["candidates"][0]
        original = service.registry.get("tavern.chat.append", "1")
        entered, release = Event(), Event()

        def block_assistant(config, inputs, context):
            if config["role"] == "assistant":
                entered.set()
                assert release.wait(10)
                raise RuntimeError("Offline assistant failure")
            return original.executor(config, inputs, context)

        service.registry._nodes[("tavern.chat.append", "1")] = replace(original, executor=block_assistant)
        started = service.start(first["workflow_session_id"], expected_revision=first["revision"],
                                idempotency_key=str(uuid4()), inputs={"text": "Partial second."})
        try:
            assert entered.wait(5)
            partial = service.get_session(first["workflow_session_id"])
            assert [entry["role"] for entry in partial["objects"]["tavern-chat"]["value"]["entries"]] == [
                "user", "assistant", "user"]
            active_candidates = candidates(service, partial)
            assert active_candidates["can_fork"] is False
            assert active_candidates["candidates"] == []
            with pytest.raises(ContractValidationError):
                fork(service, fork_request(partial, candidate["candidate_id"]))
            paused_request = service.control(partial["workflow_session_id"], action="pause",
                                             expected_revision=partial["revision"], idempotency_key=str(uuid4()))
            paused_candidates = candidates(service, paused_request)
            assert paused_candidates["can_fork"] is False
        finally:
            release.set()
            service.wait(started["active_chain_run_id"])
        failed = service.get_session(first["workflow_session_id"])
        assert failed["status"] == "failed"
        assert candidates(service, failed)["can_fork"] is False
        with pytest.raises(ContractValidationError):
            fork(service, fork_request(failed, candidate["candidate_id"]))
        stable = service.control(failed["workflow_session_id"], action="close",
                                 expected_revision=failed["revision"], idempotency_key=str(uuid4()))
        stable_candidates = candidates(service, stable)
        assert stable_candidates["can_fork"] is True
        assert {row["chain_run_id"] for row in stable_candidates["candidates"]} == {first["selected_chain_run_id"]}
        with pytest.raises(ContractValidationError):
            fork(service, fork_request(stable, started["active_chain_run_id"]))
        child = fork(service, fork_request(stable, candidate["candidate_id"]))["result"]
        child_view = service.get_session(child["receipt"]["workflow_session_id"])
        assert len(child_view["objects"]["tavern-chat"]["value"]["entries"]) == 2
        assert len(service.list_sessions(doc["workflow_definition_id"])) == 2
