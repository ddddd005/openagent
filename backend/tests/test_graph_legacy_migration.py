"""Legacy archives migrate as frozen references into independent graph sessions."""

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
from phase1_agent.workflow import A_BINDING, B_BINDING, OfflineAdapter, WorkflowService
from test_graph_agent_service import graph
from test_graph_service import copy_current, edge, run
from test_preparation_program import COUNTDOWN
from test_workflow_preparation_program import edit_value, publish
from test_workflow_prompt_selection import choice


def context_graph(service):
    doc = graph(service, context=True)
    history = next(node for node in doc["nodes"] if node["component_id"] == "workflow.context")
    output = next(node for node in doc["nodes"] if node["component_id"] == "workflow.output")
    output["config"]["mode"] = "prompt"
    doc["edges"] = [edge(history, output, 90)]
    return doc


def migrate(service, legacy, sid, doc, *, key=None):
    agent = next(node for node in doc["nodes"] if node["component_id"] == "workflow.agent")
    return service.migrate_legacy(
        document=doc, source_session_id=sid,
        expected_source_revision=legacy.get_session(sid)["revision"],
        mappings=[{"source_node_id": A_BINDING, "target_node_id": agent["node_binding_id"]}],
        idempotency_key=key or str(uuid4()),
    )


@pytest.fixture
def pair(tmp_path):
    path = tmp_path / "legacy-graph.sqlite"
    with closing(WorkflowService(path)) as legacy:
        publish(legacy)
        sid = legacy.create_session()["workflow_session_id"]
        legacy.submit(sid, "Legacy input", str(uuid4()), prompt_selection=choice("A", "B"))
        legacy.wait_for_idle(sid)
        assert legacy.get_session(sid)["error"] is None
        edit_value(legacy, sid, 50, str(uuid4()))
        with closing(GraphWorkflowService(
            path, enabled_packages={"workflow.compat": "1.0.0"},
            model_factory=lambda _: pytest.fail("context migration must not call a model"),
        )) as service:
            yield path, legacy, service, sid


def test_migration_keeps_old_records_and_inherits_latest_manual_values_without_agent_dispatch(pair):
    path, legacy, service, sid = pair
    with closing(SqliteStore(path)) as store:
        original = store.read_bundle()
        original_state = ProgramVariableStore(store).current(sid)
        source_runs = {run["run_id"]: run for run in original["run_record"]}
        a_turn = next(turn for turn in original["turn"] if source_runs[turn["run_id"]]["node_binding_id"] == A_BINDING)
    doc = context_graph(service)
    migrated = migrate(service, legacy, sid, doc)
    view = migrated["session"]
    assert view["status"] == "idle" and view["selected_chain_run_id"] is None
    assert service.list_graph_candidates(view["workflow_session_id"])["candidates"] == []
    assert service.get_consumer(view["workflow_session_id"])["history"] == []
    agent_id = next(node["node_binding_id"] for node in doc["nodes"] if node["component_id"] == "workflow.agent")
    assert view["workflow_session_id"] != sid
    assert view["private_states"][agent_id] == {"head_turn_id": a_turn["turn_id"]}
    assert view["data"]["values"] == original_state["values"]
    assert view["data"]["values"][COUNTDOWN]["value"] == 50
    assert migrated["provenance"]["legacy_archives"] == [{
        "source_node_id": A_BINDING, "target_node_id": agent_id, "turn_ids": [a_turn["turn_id"]],
    }]
    completed = run(service, view)
    assert completed["status"] == "succeeded"
    assert service._native_runtime is None
    context_id = next(node["node_binding_id"] for node in doc["nodes"] if node["component_id"] == "workflow.context")
    projected = next(node for node in completed["nodes"] if node["node_binding_id"] == context_id)["outputs"]["output"]
    assert {item["source"]["turn_id"] for item in projected["items"]} == {a_turn["turn_id"]}
    assert any(item["role"] == "tool" and item["protected"] for item in projected["items"])
    with closing(SqliteStore(path)) as store:
        assert store.read_bundle() == original
        assert ProgramVariableStore(store).current(sid) == original_state
        validate_bundle(store.read_bundle(include_graph=True))


def test_migrated_data_and_archive_authorization_are_independent_of_other_sources(pair):
    path, legacy, service, sid = pair
    doc = context_graph(service)
    session = migrate(service, legacy, sid, doc)["session"]
    own_turn = next(iter(session["source"]["legacy_archives"]))["turn_ids"][0]
    assert service.get_agent_archive(session["workflow_session_id"], own_turn)["turn"]["turn_id"] == own_turn
    with closing(SqliteStore(path)) as store:
        runs = {run["run_id"]: run for run in store.list_records("run_record")}
        b_turn = next(turn["turn_id"] for turn in store.list_records("turn") if runs[turn["run_id"]]["node_binding_id"] == B_BINDING)
    with pytest.raises(ContractValidationError) as denied:
        service.get_agent_archive(session["workflow_session_id"], b_turn)
    assert denied.value.reason_code == "graph_history_scope_mismatch"
    edited = service.update_data(session["workflow_session_id"], expected_revision=session["revision"],
        expected_data_revision=session["data_revision"], idempotency_key=str(uuid4()), variables={COUNTDOWN: 80})
    assert edited["data"]["values"][COUNTDOWN]["value"] == 80
    with closing(SqliteStore(path)) as store:
        assert ProgramVariableStore(store).current(sid)["values"][COUNTDOWN]["value"] == 50


def test_copy_retains_frozen_legacy_references_and_excludes_later_source_turns(pair):
    path, legacy, service, sid = pair
    doc = context_graph(service)
    original = migrate(service, legacy, sid, doc)["session"]
    child_doc = deepcopy(doc)
    child_doc["workflow_definition_id"] = str(uuid4())
    copied = copy_current(service, original, child_doc)
    refs = deepcopy(copied["source"]["legacy_archives"])
    assert refs == original["source"]["legacy_archives"]
    assert copied["private_states"] == original["private_states"]
    legacy.submit(sid, "Later legacy input", str(uuid4()), prompt_selection=choice("A", "B"))
    legacy.wait_for_idle(sid)
    assert legacy.get_session(sid)["error"] is None
    with closing(SqliteStore(path)) as store:
        all_old = set(refs[0]["turn_ids"])
        runs = {run["run_id"]: run for run in store.list_records("run_record")}
        later_turn = next(turn["turn_id"] for turn in store.list_records("turn")
                          if runs[turn["run_id"]]["node_binding_id"] == A_BINDING and turn["turn_id"] not in all_old)
    with pytest.raises(ContractValidationError) as denied:
        service.get_agent_archive(copied["workflow_session_id"], later_turn)
    assert denied.value.reason_code == "graph_history_scope_mismatch"
    completed = run(service, copied)
    assert completed["status"] == "succeeded"
    assert completed["source"]["legacy_archives"] == refs
    assert completed["data"]["values"][COUNTDOWN]["value"] == 50
    assert service.get_session(original["workflow_session_id"])["source"]["legacy_archives"] == refs


def test_busy_legacy_runtime_is_rejected_before_a_graph_definition_or_session_is_saved(tmp_path):
    path = tmp_path / "busy.sqlite"
    entered, release = Event(), Event()

    class BlockingOffline(OfflineAdapter):
        def generate(self, messages, tools):
            entered.set()
            assert release.wait(10)
            return super().generate(messages, tools)

    with closing(WorkflowService(path, model_factory=BlockingOffline)) as legacy, closing(
        GraphWorkflowService(path, enabled_packages={"workflow.compat": "1.0.0"}),
    ) as service:
        sid = legacy.create_session()["workflow_session_id"]
        legacy.submit(sid, "Busy input", str(uuid4()))
        assert entered.wait(10)
        doc = context_graph(service)
        try:
            with pytest.raises(ContractValidationError) as busy:
                migrate(service, legacy, sid, doc)
            assert busy.value.reason_code == "source_session_busy"
            assert service.list_sessions(doc["workflow_definition_id"]) == []
            with pytest.raises(ContractValidationError) as absent:
                service.get_definition(doc["workflow_definition_id"])
            assert absent.value.reason_code == "not_found"
        finally:
            release.set()
            legacy.wait_for_idle(sid)


@pytest.mark.parametrize("with_history", [False, True])
def test_custom_legacy_kernel_requires_explicit_migration_without_replacing_its_semantics(tmp_path, with_history):
    from test_workflow_components import FinalModel, IndependentKernel, selected

    path = tmp_path / "custom-kernel.sqlite"
    components = selected(kernel=IndependentKernel())
    calls = []
    with closing(WorkflowService(path, components=components,
                                model_factory=lambda stage: FinalModel(stage, calls))) as legacy:
        sid = legacy.create_session()["workflow_session_id"]
        if with_history:
            legacy.submit(sid, "Custom input", str(uuid4()))
            legacy.wait_for_idle(sid)
            assert legacy.get_session(sid)["error"] is None
        with closing(SqliteStore(path)) as store:
            original = store.read_bundle()
        with closing(GraphWorkflowService(path, enabled_packages={"workflow.compat": "1.0.0"})) as service:
            doc = context_graph(service)
            with pytest.raises(ContractValidationError) as unsupported:
                migrate(service, legacy, sid, doc)
            assert unsupported.value.reason_code == "graph_legacy_kernel_unsupported"
            assert service.list_sessions(doc["workflow_definition_id"]) == []
            with pytest.raises(ContractValidationError) as absent:
                service.get_definition(doc["workflow_definition_id"])
            assert absent.value.reason_code == "not_found"
            assert service._native_runtime is None
        with closing(SqliteStore(path)) as store:
            assert store.read_bundle() == original
