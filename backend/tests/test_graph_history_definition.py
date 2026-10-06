"""Exact historical definitions remain the default recovery authority."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.storage import SqliteStore, _record_id
from test_graph_candidates import choose, execute, records, service
from test_graph_service import create, text_graph


def rebind(service, view, document):
    service.save_definition(document, expected_revision=document["revision"] - 1, idempotency_key=str(uuid4()))
    return service.rebind_session(view["workflow_session_id"], definition_revision=document["revision"],
        expected_revision=view["revision"], expected_data_revision=view["data_revision"],
        expected_head_revision=view["head_revision"], idempotency_key=str(uuid4()))


def revisions(service):
    first_doc = text_graph(service.registry)
    first = execute(service, create(service, first_doc))
    second_doc = deepcopy(first_doc)
    second_doc["revision"] = 2
    second_doc["nodes"][0]["config"]["text"] = "D2"
    rebound = rebind(service, first, second_doc)
    second = execute(service, rebound)
    candidates = service.list_graph_candidates(first["workflow_session_id"])["candidates"]
    return first_doc, first, second_doc, second, candidates


@pytest.mark.parametrize("fork", [False, True])
@pytest.mark.parametrize("revision", [1, 2])
def test_restore_and_fork_use_the_exact_definition_of_the_selected_completed_run(service, fork, revision):
    first_doc, first, second_doc, second, candidates = revisions(service)
    candidate = next(row for row in candidates if row["source_definition_revision"] == revision)
    expected, expected_doc = (first, first_doc) if revision == 1 else (second, second_doc)
    restored = choose(service, second, candidate["candidate_id"], fork=fork)
    assert restored["definition_revision"] == revision
    assert service.get_definition(restored["workflow_definition_id"], revision) == expected_doc
    assert restored["nodes"][-1]["outputs"] == expected["nodes"][-1]["outputs"]
    actual = execute(service, restored)
    assert actual["chains"][-1]["definition_revision"] == revision
    assert actual["nodes"][-1]["outputs"] == expected["nodes"][-1]["outputs"]
    if fork:
        assert service.get_session(second["workflow_session_id"]) == second


@pytest.mark.parametrize("failure", ["missing", "invalid", "implementation_missing", "implementation_changed",
                                     "declaration_missing"])
@pytest.mark.parametrize("fork", [False, True])
def test_unavailable_history_fails_without_rebuilding_or_using_current_definition(service, failure, fork):
    first_doc, _, _, current, candidates = revisions(service)
    candidate = next(row for row in candidates if row["source_definition_revision"] == 1)
    if failure in ("missing", "invalid", "declaration_missing"):
        with closing(SqliteStore(service.database)) as store:
            if failure == "declaration_missing":
                identity = _record_id("node_definition", {"component_id": "workflow.regex", "component_version": "1"})
                store._connection.execute("DELETE FROM records WHERE record_type='node_definition' AND record_id=?",
                                          (identity,))
            else:
                identity = _record_id("workflow_definition_revision", {
                    "workflow_definition_id": first_doc["workflow_definition_id"], "revision": 1})
                if failure == "missing":
                    store._connection.execute(
                        "DELETE FROM records WHERE record_type='workflow_definition_revision' AND record_id=?", (identity,))
                else:
                    saved = store._get("workflow_definition_revision", identity)
                    saved["document"]["edges"][0]["target_node_id"] = str(uuid4())
                    store._connection.execute(
                        "UPDATE records SET payload=? WHERE record_type='workflow_definition_revision' AND record_id=?",
                        (canonical_bytes(saved).decode("utf-8"), identity))
    elif failure == "implementation_missing":
        service.registry._nodes.pop(("workflow.regex", "1"))
    else:
        old = service.registry.get("workflow.regex", "1")
        service.registry._nodes[("workflow.regex", "1")] = replace(
            old, definition=replace(old.definition, display_name="replacement"))
    before = records(service)
    with pytest.raises(ContractValidationError) as error:
        choose(service, current, candidate["candidate_id"], fork=fork)
    assert error.value.reason_code == "historical_definition_unavailable"
    assert error.value.diagnostics[0]["workflow_definition_id"] == first_doc["workflow_definition_id"]
    assert error.value.diagnostics[0]["definition_revision"] == 1
    assert records(service) == before
    listed = next(row for row in service.list_graph_candidates(current["workflow_session_id"])["candidates"]
                  if row["candidate_id"] == candidate["candidate_id"])
    assert not listed["can_select"]
    assert listed["diagnostic"]["reason_code"] == "historical_definition_unavailable"


def test_candidate_save_failure_retries_the_same_result_and_definition_without_node_reexecution(service):
    _, _, _, current, candidates = revisions(service)
    candidate = next(row for row in candidates if row["source_definition_revision"] == 1)
    before = records(service)
    key = str(uuid4())

    def fault(stage):
        if stage == "before_commit":
            raise RuntimeError("checkpoint restore save failed")

    service._fault_injector = fault
    with pytest.raises(RuntimeError, match="checkpoint restore save failed"):
        choose(service, current, candidate["candidate_id"], key=key)
    service._fault_injector = None
    assert records(service) == before
    restored = choose(service, current, candidate["candidate_id"], key=key)
    assert restored["definition_revision"] == 1
    assert choose(service, current, candidate["candidate_id"], key=key) == restored
    after = records(service)
    for kind in ("node_run", "workflow_output", "chain_run"):
        assert after[kind] == before[kind]
    assert len(after["workflow_commit"]) == len(before["workflow_commit"]) + 1


def test_checkpoint_uses_started_definition_when_new_revision_is_saved_during_execution(service):
    entered, release = Event(), Event()
    original = service.registry.get("workflow.text", "1")

    def wait(config, inputs, context):
        entered.set()
        assert release.wait(5)
        return original.executor(config, inputs, context)

    service.registry._nodes[("workflow.text", "1")] = replace(original, executor=wait)
    doc = text_graph(service.registry)
    initial = create(service, doc)
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                            idempotency_key=str(uuid4()))
    try:
        assert entered.wait(5)
        live = deepcopy(doc)
        live["revision"] = 2
        live["nodes"][0]["config"]["text"] = "future definition"
        service.save_definition(live, expected_revision=1, idempotency_key=str(uuid4()))
    finally:
        release.set()
        service.wait(started["active_chain_run_id"])
    completed = service.get_session(initial["workflow_session_id"])
    candidate = service.list_graph_candidates(completed["workflow_session_id"])["candidates"][0]
    assert candidate["source_definition_revision"] == 1
    assert completed["nodes"][-1]["outputs"]["output"]["text"] == "pear pear"
    restored = choose(service, completed, candidate["candidate_id"], fork=True)
    assert restored["definition_revision"] == 1
    assert service.get_definition(doc["workflow_definition_id"])["revision"] == 2
    assert service.get_definition(restored["workflow_definition_id"], restored["definition_revision"]) == doc
