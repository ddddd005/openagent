"""Full checkpoint selection and frozen forks without replaying node effects."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.program_variable_store import ProgramVariableStore
from phase1_agent.storage import SqliteStore
from phase1_agent.workbench_resources import CORE_SESSION_NOTE
from phase1_agent.workflow import WorkflowService
from graph_test_plugin import PROBE_COMPONENT, register_content_probe
from test_graph_service import create, copy_current, document, edge, node, text_graph
from test_graph_agent_service import graph as agent_graph, harness, run as run_agent
from test_graph_agent_runtime import final


@pytest.fixture
def service(tmp_path):
    from phase1_agent.graph_nodes import create_default_registry
    registry = create_default_registry()
    register_content_probe(registry)
    with closing(GraphWorkflowService(tmp_path / "candidates.sqlite", registry=registry)) as instance:
        yield instance


def state_graph(registry):
    source = node(registry, "current-input", 1)
    probe = node(registry, PROBE_COMPONENT, 2)
    register = node(registry, "variable-register", 3, name="b", valueType="string")
    assign = node(registry, "variable-assign", 4, name="b")
    shared = node(registry, "session-data-write", 5)
    convert = node(registry, "json-to-text", 6)
    output = node(registry, "output", 7)
    return document([source, probe, register, assign, shared, convert, output], [
        edge(source, probe, 1), edge(probe, register, 2, "left", "text"),
        edge(register, assign, 3, "text", "text"), edge(assign, shared, 4, "text", "text"),
        edge(shared, convert, 5, "json"), edge(convert, output, 6),
    ])


def execute(service, view, text="one"):
    started = service.start(view["workflow_session_id"], expected_revision=view["revision"],
                            idempotency_key=str(uuid4()), inputs={"text": text})
    service.wait(started["active_chain_run_id"])
    final = service.get_session(view["workflow_session_id"])
    assert final["status"] == "succeeded"
    return final


def choose(service, view, candidate_id, *, fork=False, key=None):
    method = service.fork_graph_candidate if fork else service.select_graph_candidate
    return method(view["workflow_session_id"], candidate_id=candidate_id,
        expected_revision=view["revision"], expected_data_revision=view["data_revision"],
        expected_head_revision=view["head_revision"], idempotency_key=key or str(uuid4()))


def edit(service, view, value):
    return service.update_data(view["workflow_session_id"], expected_revision=view["revision"],
        expected_data_revision=view["data_revision"], idempotency_key=str(uuid4()),
        variables={"b": value}, shared={CORE_SESSION_NOTE["key"]: value})


def records(service):
    with closing(SqliteStore(service.database)) as store:
        return store.read_bundle(include_graph=True)


def test_select_restores_full_state_and_exact_old_outputs_without_replaying_effects(service):
    first = execute(service, create(service, state_graph(service.registry)))
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
    second = execute(service, edit(service, first, "between"), "two")
    probe_id = first["nodes"][1]["node_binding_id"]
    assert second["private_states"][probe_id] == {"count": 2}
    edited = edit(service, second, "after")
    before = records(service)
    key = str(uuid4())
    selected = choose(service, edited, candidate["candidate_id"], key=key)
    assert selected["data"]["values"] == first["data"]["values"]
    assert selected["data"]["data"] == first["data"]["data"]
    assert selected["private_states"] == first["private_states"]
    assert selected["nodes"][-1]["outputs"] == first["nodes"][-1]["outputs"]
    assert selected["selected_chain_run_id"] == first["selected_chain_run_id"]
    assert selected["data_revision"] > edited["data_revision"]
    assert choose(service, edited, candidate["candidate_id"], key=key) == selected
    after = records(service)
    for kind in ("node_run", "workflow_output", "chain_run"):
        assert after[kind] == before[kind]
    assert after["workflow_commit"][:len(before["workflow_commit"])] == before["workflow_commit"]
    assert service._native_runtime is None
    next_run = execute(service, selected, "three")
    assert next_run["private_states"][probe_id] == {"count": 2}
    validate_bundle(records(service))


def test_data_only_changes_restore_even_when_variables_are_identical(service):
    first = execute(service, create(service, state_graph(service.registry)))
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
    changed = service.update_data(first["workflow_session_id"], expected_revision=first["revision"],
        expected_data_revision=first["data_revision"], idempotency_key=str(uuid4()),
        shared={CORE_SESSION_NOTE["key"]: "data only"})
    assert changed["data"]["values"] == first["data"]["values"]
    restored = choose(service, changed, candidate["candidate_id"])
    assert restored["data"]["data"] == first["data"]["data"]
    assert restored["data_revision"] > changed["data_revision"]


def test_fork_uses_chosen_checkpoint_and_excludes_later_parent_histories(service):
    first = execute(service, create(service, state_graph(service.registry)))
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
    second = execute(service, edit(service, first, "between"), "two")
    parent = edit(service, second, "after")
    key = str(uuid4())
    forked = choose(service, parent, candidate["candidate_id"], fork=True, key=key)
    assert forked["workflow_session_id"] != parent["workflow_session_id"]
    assert forked["workflow_definition_id"] == parent["workflow_definition_id"]
    assert forked["data"]["values"] == first["data"]["values"]
    assert forked["data"]["data"] == first["data"]["data"]
    assert forked["private_states"] == first["private_states"]
    assert forked["history_refs"] == first["history_refs"]
    assert forked["selected_chain_run_id"] == first["selected_chain_run_id"]
    assert choose(service, parent, candidate["candidate_id"], fork=True, key=key) == forked
    assert service.get_session(parent["workflow_session_id"]) == parent
    with pytest.raises(ContractValidationError) as denied:
        service.get_run(forked["workflow_session_id"], second["selected_chain_run_id"])
    assert denied.value.reason_code == "not_found"
    later = execute(service, parent, "three")
    assert service.get_session(forked["workflow_session_id"]) == forked
    assert len(service.list_graph_candidates(forked["workflow_session_id"])["candidates"]) == 1
    child_run = execute(service, forked, "child")
    assert child_run["private_states"][first["nodes"][1]["node_binding_id"]] == {"count": 2}
    assert service.get_session(parent["workflow_session_id"]) == later


def test_candidates_scope_cas_and_unknown_receipt_replay(service):
    doc = text_graph(service.registry)
    first = execute(service, create(service, doc))
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
    other = service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    with pytest.raises(ContractValidationError) as denied:
        choose(service, other, candidate["candidate_id"])
    assert denied.value.reason_code == "graph_candidate_scope_mismatch"
    selected = choose(service, first, candidate["candidate_id"], key="selection")
    with pytest.raises(ContractValidationError) as stale:
        choose(service, first, candidate["candidate_id"])
    assert stale.value.reason_code == "stale_revision"
    assert choose(service, first, candidate["candidate_id"], key="selection") == selected
    with pytest.raises(ContractValidationError) as key_conflict:
        choose(service, selected, candidate["candidate_id"], key="selection")
    assert key_conflict.value.reason_code == "idempotency_conflict"
    with closing(SqliteStore(service.database)) as store:
        seed = next(row for row in store.list_records("workflow_commit", include_graph=True)
                    if row["source"]["kind"] == "session_seed")
    with pytest.raises(ContractValidationError) as invalid:
        choose(service, selected, seed["commit_id"])
    assert invalid.value.reason_code == "graph_candidate_invalid"


def test_copied_candidates_restore_historical_definition_and_original_node_identity(service):
    doc = state_graph(service.registry)
    first = execute(service, create(service, doc))
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    old_id = doc["nodes"][1]["node_binding_id"]
    new_id = str(uuid4())
    target["nodes"][1]["node_binding_id"] = new_id
    for link in target["edges"]:
        for endpoint in ("source_node_id", "target_node_id"):
            if link[endpoint] == old_id:
                link[endpoint] = new_id
    child = copy_current(service, first, target,
        mappings=[{"source_node_id": old_id, "target_node_id": new_id, "action": "copy"}])
    child_run = execute(service, child, "child")
    assert child_run["private_states"][new_id] == {"count": 2}
    selected = choose(service, child_run, candidate["candidate_id"])
    assert child_run["definition_revision"] == selected["definition_revision"] == 1
    assert child_run["workflow_definition_id"] != selected["workflow_definition_id"]
    assert selected["workflow_definition_id"] == doc["workflow_definition_id"]
    assert selected["private_states"][old_id] == {"count": 1}
    assert new_id not in selected["private_states"]
    assert selected["nodes"][1]["run_id"] == first["nodes"][1]["run_id"]
    assert selected["nodes"][1]["source_workflow_session_id"] == first["workflow_session_id"]
    assert service.get_consumer(selected["workflow_session_id"])["nodes"][1]["run_id"] == first["nodes"][1]["run_id"]
    again = execute(service, selected, "restored child")
    assert again["chains"][-1]["workflow_definition_id"] == doc["workflow_definition_id"]
    assert again["private_states"][old_id] == {"count": 2}
    validate_bundle(records(service))


def test_selection_and_fork_are_blocked_while_node_execution_is_active(service):
    first = execute(service, create(service, text_graph(service.registry)))
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
    entered, release = Event(), Event()
    original = service.registry.get("workflow.text", "1")
    from dataclasses import replace

    def block(config, inputs, context):
        entered.set()
        assert release.wait(10)
        return original.executor(config, inputs, context)

    service.registry._nodes[("workflow.text", "1")] = replace(original, executor=block)
    service.start(first["workflow_session_id"], expected_revision=first["revision"], idempotency_key=str(uuid4()))
    assert entered.wait(10)
    active = service.get_session(first["workflow_session_id"])
    try:
        assert not any(item["can_select"] for item in service.list_graph_candidates(first["workflow_session_id"])["candidates"])
        for fork in (False, True):
            with pytest.raises(ContractValidationError) as busy:
                choose(service, active, candidate["candidate_id"], fork=fork)
            assert busy.value.reason_code == "active_execution_not_copyable"
    finally:
        release.set()
        service.wait(active["active_chain_run_id"])


def test_native_candidate_restores_agent_context_head_without_model_replay(harness):
    instance, scripts, requests, _ = harness
    scripts.extend([[final("first checkpoint")], [final("later checkpoint")], [final("after restore")]])
    doc = agent_graph(instance, context=True)
    first = run_agent(instance, create(instance, doc))
    candidate = instance.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
    second = run_agent(instance, first)
    assert second["private_states"] != first["private_states"]
    selected = choose(instance, second, candidate["candidate_id"])
    assert selected["private_states"] == first["private_states"]
    assert len(requests) == 2
    resumed = run_agent(instance, selected)
    assert resumed["status"] == "succeeded"
    assert len(requests) == 3
    sent = str(requests[-1])
    assert "first checkpoint" in sent
    assert "later checkpoint" not in sent
    assert instance.get_run(first["workflow_session_id"], second["selected_chain_run_id"])["chain"]["status"] == "succeeded"
    validate_bundle(records(instance))


def test_candidate_restores_original_definition_without_later_duplicate_nodes(service):
    doc = state_graph(service.registry)
    first = execute(service, create(service, doc))
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    original = target["nodes"][1]["node_binding_id"]
    duplicate = deepcopy(target["nodes"][1])
    duplicate["node_binding_id"] = str(uuid4())
    target["nodes"].append(duplicate)
    mappings = [{"source_node_id": original, "target_node_id": target_id, "action": "copy"}
                for target_id in (original, duplicate["node_binding_id"])]
    copied = copy_current(service, first, target, mappings=mappings)
    selected = choose(service, copied, candidate["candidate_id"])
    assert selected["workflow_definition_id"] == doc["workflow_definition_id"]
    assert selected["private_states"][original] == {"count": 1}
    assert duplicate["node_binding_id"] not in selected["private_states"]
    assert len(selected["nodes"]) == len(doc["nodes"])
    observed = next(row for row in selected["nodes"] if row["node_binding_id"] == original)
    assert observed["run_id"] == first["nodes"][1]["run_id"]
    assert observed["source_workflow_session_id"] == first["workflow_session_id"]
    validate_bundle(records(service))


def test_candidate_restore_ignores_later_resets_rebinds_and_copy_adaptations(service):
    doc = state_graph(service.registry)
    first = execute(service, create(service, doc))
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
    copied_doc = deepcopy(doc)
    copied_doc["workflow_definition_id"] = str(uuid4())
    copied = copy_current(service, first, copied_doc)
    revised = deepcopy(copied_doc)
    revised["revision"] = 2
    probe_id = revised["nodes"][1]["node_binding_id"]
    service.save_definition(revised, expected_revision=1, idempotency_key=str(uuid4()))
    rebound = service.rebind_session(copied["workflow_session_id"], definition_revision=2,
        expected_revision=copied["revision"], expected_data_revision=copied["data_revision"],
        expected_head_revision=copied["head_revision"], idempotency_key=str(uuid4()),
        mappings=[{"target_node_id": probe_id, "action": "reset"}])
    forked = choose(service, rebound, candidate["candidate_id"], fork=True)
    assert forked["workflow_definition_id"] == doc["workflow_definition_id"]
    assert forked["definition_revision"] == 1
    assert forked["private_states"][probe_id] == {"count": 1}
    target = deepcopy(revised)
    target.update(workflow_definition_id=str(uuid4()), revision=1)
    renamed = str(uuid4())
    target["nodes"][1]["node_binding_id"] = renamed
    for link in target["edges"]:
        for endpoint in ("source_node_id", "target_node_id"):
            if link[endpoint] == probe_id:
                link[endpoint] = renamed
    grandchild = copy_current(service, forked, target,
        mappings=[{"source_node_id": probe_id, "target_node_id": renamed, "action": "copy"}])
    selected = choose(service, grandchild, candidate["candidate_id"])
    assert renamed not in selected["private_states"]
    assert selected["private_states"][probe_id] == {"count": 1}
    assert selected["nodes"][1]["run_id"] == first["nodes"][1]["run_id"]
    assert selected["nodes"][-1]["outputs"] == first["nodes"][-1]["outputs"]
    later = execute(service, first, "parent later")
    with pytest.raises(ContractValidationError):
        service.get_run(selected["workflow_session_id"], later["selected_chain_run_id"])
    completed = execute(service, selected, "new child")
    assert completed["private_states"][probe_id] == {"count": 2}
    validate_bundle(records(service))


def test_ancestor_candidate_fork_skips_later_parent_adaptations_and_preserves_public_producer(service):
    doc = text_graph(service.registry)
    original_id = doc["nodes"][-1]["node_binding_id"]
    doc["nodes"][-1]["public_outputs"] = ["output"]
    first = execute(service, create(service, doc))
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]["candidate_id"]
    rebound_doc = deepcopy(doc)
    rebound_doc["revision"] = 2
    rebound_id = str(uuid4())
    rebound_doc["nodes"][-1]["node_binding_id"] = rebound_id
    rebound_doc["edges"][-1]["target_node_id"] = rebound_id
    service.save_definition(rebound_doc, expected_revision=1, idempotency_key=str(uuid4()))
    rebound = service.rebind_session(first["workflow_session_id"], definition_revision=2,
        expected_revision=first["revision"], expected_data_revision=first["data_revision"],
        expected_head_revision=first["head_revision"], idempotency_key=str(uuid4()),
        mappings=[{"source_node_id": original_id, "target_node_id": rebound_id, "action": "copy"}])
    later = execute(service, rebound)
    copied_doc = deepcopy(rebound_doc)
    copied_doc.update(workflow_definition_id=str(uuid4()), revision=1)
    copied_id = str(uuid4())
    copied_doc["nodes"][-1]["node_binding_id"] = copied_id
    copied_doc["edges"][-1]["target_node_id"] = copied_id
    copied = copy_current(service, later, copied_doc,
        mappings=[{"source_node_id": rebound_id, "target_node_id": copied_id, "action": "copy"}])
    parent = execute(service, copied)
    forked = choose(service, parent, candidate, fork=True)
    assert forked["source"]["workflow_session_id"] == parent["workflow_session_id"]
    assert forked["workflow_definition_id"] == doc["workflow_definition_id"]
    assert forked["definition_revision"] == 1
    assert forked["nodes"][-1]["run_id"] == first["nodes"][-1]["run_id"]
    assert forked["nodes"][-1]["outputs"] == first["nodes"][-1]["outputs"]
    public = service.read_public_output(forked["workflow_session_id"],
        workflow_definition_id=doc["workflow_definition_id"], definition_revision=1,
        node_id=original_id, port_id="output")["output"]
    assert public["availability"] == "produced"
    assert public["source"]["workflow_session_id"] == first["workflow_session_id"]
    assert public["source"]["node_binding_id"] == original_id
    assert public["run_id"] == first["nodes"][-1]["run_id"]
    assert forked["history_refs"] == first["history_refs"]
    for chain_id in (later["selected_chain_run_id"], parent["selected_chain_run_id"]):
        with pytest.raises(ContractValidationError) as denied:
            service.get_run(forked["workflow_session_id"], chain_id)
        assert denied.value.reason_code == "not_found"
    again = execute(service, forked)
    assert again["chains"][-1]["workflow_definition_id"] == doc["workflow_definition_id"]
    assert again["chains"][-1]["definition_revision"] == 1
    assert again["nodes"][-1]["outputs"] == first["nodes"][-1]["outputs"]
    assert service.get_session(parent["workflow_session_id"]) == parent
    validate_bundle(records(service))


def test_legacy_explicit_selection_restores_data_only_changes_with_unchanged_variables(tmp_path):
    from test_workflow_preparation_program import publish, reroll
    from test_workflow_prompt_selection import choice

    path = tmp_path / "legacy-data-only.sqlite"
    with closing(WorkflowService(path)) as legacy:
        sid = legacy.create_session()["workflow_session_id"]
        note = CORE_SESSION_NOTE
        legacy.write_session_data(sid, definition_id=note["definition_id"], revision=1,
            value="before", expected_revision=0, expected_session_revision=1, idempotency_key=str(uuid4()))
        publish(legacy)
        first = legacy.submit(sid, "input", str(uuid4()), prompt_selection=choice("A", "B"))
        legacy.wait_for_idle(sid)
        assert legacy.get_session(sid)["error"] is None
        reroll(legacy, sid)
        with closing(SqliteStore(path)) as store:
            current = ProgramVariableStore(store).current(sid)
            candidate = next(row for row in store.list_records("workflow_candidate")
                             if row["chain_run_id"] == first["chain_run_id"])
        before = legacy.get_session(sid)
        legacy.write_session_data(sid, definition_id=note["definition_id"], revision=1, value="data-only edit",
            expected_revision=current["revision"], expected_session_revision=before["revision"], idempotency_key=str(uuid4()))
        view = legacy.get_session(sid)
        legacy.select_candidate(sid, candidate["candidate_id"], idempotency_key=str(uuid4()),
            expected_session_revision=view["revision"], expected_ref_revision=view["ref_revision"],
            expected_head_commit_id=view["head_commit_id"])
        with closing(SqliteStore(path)) as store:
            restored = ProgramVariableStore(store).current(sid)
            assert restored["values"] == current["values"]
            assert restored["data"][note["key"]]["value"] == "before"
