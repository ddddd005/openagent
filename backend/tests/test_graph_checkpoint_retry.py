"""Final checkpoint retries reuse only proven completed execution results."""

from contextlib import closing
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_records import graph_error
from phase1_agent.graph_service import GraphWorkflowService
from test_graph_candidates import records, service
from test_graph_service import create, document, edge, node, text_graph
from test_models_service_integration import ModelDatabaseFixture


def fail_checkpoint_twice(service):
    original = service._commit
    attempts = []

    def save(repo, session, *, source, **kwargs):
        original(repo, session, source=source, **kwargs)
        if source["kind"] == "completed_execution":
            attempts.append((session["workflow_definition_id"], session["definition_revision"]))
            if len(attempts) <= 2:
                raise OSError("checkpoint transaction failed")

    service._commit = save
    return attempts


def retry_archive(service, view):
    started = service.control(view["workflow_session_id"], action="retry_archive",
                              expected_revision=view["revision"], idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    return service.get_session(view["workflow_session_id"])


def complete_after_two_checkpoint_failures(service, initial):
    attempts = fail_checkpoint_twice(service)
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                            idempotency_key=str(uuid4()))
    chain_id = started["active_chain_run_id"]
    service.wait(chain_id)
    pending = service.get_session(initial["workflow_session_id"])
    assert pending["status"] == "archive_failed"
    assert pending["chains"][-1]["diagnostic"]["reason_code"] == "checkpoint_commit_pending"
    assert pending["available_actions"] == ["retry_archive", "close"]
    assert pending["head_commit_id"] == initial["head_commit_id"]
    assert service.list_graph_candidates(initial["workflow_session_id"])["candidates"] == []
    assert len(records(service)["workflow_commit"]) == 1
    assert len(records(service)["state_snapshot"]) == 1
    evidence = service.get_run(initial["workflow_session_id"], chain_id)
    retained_key = service._acceptance_candidates[chain_id]["completed_execution"]["finish_key"]
    pending_again = retry_archive(service, pending)
    assert pending_again["status"] == "archive_failed"
    assert pending_again["head_commit_id"] == initial["head_commit_id"]
    assert pending_again["available_actions"] == ["retry_archive", "close"]
    assert len(records(service)["workflow_commit"]) == 1
    assert service._acceptance_candidates[chain_id]["completed_execution"]["finish_key"] == retained_key
    completed = retry_archive(service, pending_again)
    assert completed["status"] == "succeeded" and completed["active_chain_run_id"] is None
    assert len(records(service)["workflow_commit"]) == 2
    assert len(records(service)["state_snapshot"]) == 2
    assert len(service.list_graph_candidates(initial["workflow_session_id"])["candidates"]) == 1
    after = service.get_run(initial["workflow_session_id"], chain_id)
    assert after["node_runs"] == evidence["node_runs"]
    assert after["outputs"] == evidence["outputs"]
    assert after["runtime_facts"] == evidence["runtime_facts"]
    assert attempts == [(initial["workflow_definition_id"], initial["definition_revision"])] * 3
    assert not service._acceptance_candidates
    return completed


def test_two_checkpoint_save_failures_retry_original_success_without_executing_nodes(service, monkeypatch):
    import phase1_agent.graph_execution as execution

    calls = []
    original = execution.execute_graph

    def execute(*args, **kwargs):
        calls.append(kwargs["chain_run_id"])
        return original(*args, **kwargs)

    monkeypatch.setattr(execution, "execute_graph", execute)
    completed = complete_after_two_checkpoint_failures(service, create(service, text_graph(service.registry)))
    assert calls == [completed["selected_chain_run_id"]]


def test_checkpoint_retry_preserves_single_mock_model_request_and_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-secret")
    fixture = ModelDatabaseFixture()
    with closing(GraphWorkflowService(tmp_path / "checkpoint-model.sqlite",
                                      public_model_factory=fixture.factory)) as instance:
        fixture.write(instance, 1)
        initial = fixture.create(instance, fixture.document(instance))
        completed = complete_after_two_checkpoint_failures(instance, initial)
        assert len(fixture.calls) == 1 and fixture.closes == 1
        assert fixture.result(completed)["content"] == "https://original.test"


@pytest.mark.parametrize("failure", ["unknown", "notification"])
def test_all_accepted_nodes_without_returned_success_do_not_gain_checkpoint_retry(service, monkeypatch, failure):
    import phase1_agent.graph_execution as execution

    original = execution.execute_graph

    def execute(*args, **kwargs):
        original(*args, **kwargs)
        if failure == "notification":
            raise graph_error("result_committed_notification_failed", "completion notification lost", 500)
        raise OSError("execution outcome lost")

    monkeypatch.setattr(execution, "execute_graph", execute)
    initial = create(service, text_graph(service.registry))
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                            idempotency_key=str(uuid4()))
    with pytest.raises(Exception):
        service.wait(started["active_chain_run_id"])
    failed = service.get_session(initial["workflow_session_id"])
    assert failed["chains"][-1]["next_node_index"] == len(failed["chains"][-1]["ordered_nodes"])
    assert failed["status"] == "recovery_unavailable"
    assert failed["available_actions"] == ["close"]
    assert service.list_graph_candidates(initial["workflow_session_id"])["candidates"] == []
    with pytest.raises(ContractValidationError):
        retry_archive(service, failed)


def test_business_failure_does_not_gain_checkpoint_retry(service):
    def execute(config, inputs, context):
        raise ValueError("business failure")

    service.registry.register(NodeDefinition(
        "test.business-failure", "1", "Failure", "test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),)), execute)
    failing, output = node(service.registry, "test.business-failure", 1), node(service.registry, "output", 2)
    initial = create(service, document([failing, output], [edge(failing, output, 1)]))
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                            idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    failed = service.get_session(initial["workflow_session_id"])
    assert failed["status"] == "failed" and failed["available_actions"] == ["close"]
    assert service.list_graph_candidates(initial["workflow_session_id"])["candidates"] == []
    with pytest.raises(ContractValidationError):
        retry_archive(service, failed)
