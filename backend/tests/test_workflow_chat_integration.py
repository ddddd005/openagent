"""Real chat operations restore registered context and retain exact execution evidence."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_collection import collect_checkpoint_state
from phase1_agent.checkpoint_loading import load_checkpoint_state_in_transaction
from phase1_agent.content_contracts import text_content
from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_contracts import FRONTEND_COMMIT_TYPE
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.workflow_chat_runtime import read_chat_round_inputs
from test_checkpoint_collection import transaction
from test_checkpoint_loading import EMPTY_CONTEXT, agent_value
from frontend_node_test_support import graph as frontend_graph, save as _save
from test_graph_application_receipt_custody import raw_database
from test_graph_service import edge, node
from test_success_checkpoints import checkpoint_history


def identity():
    return str(uuid4())


@pytest.fixture
def context_chat(tmp_path):
    observed = []
    with closing(GraphWorkflowService(tmp_path / "context-chat.sqlite")) as service:
        provided_type = {"type_id": EFFECTIVE_CONTEXT_TYPE, "schema_version": 4}
        service.registry.register(NodeDefinition(
            "test.chat-context-provider", "1", "Context registrar", "Test",
            {}, {"type": "object", "additionalProperties": False},
            execution_class="non_working", provided_session_item_types=(provided_type,),
        ), lambda *args: pytest.fail("A context registrar cannot execute"))

        def probe(config, inputs, context):
            value = context.object_read(config["object_key"])["value"]
            observed.append(deepcopy(value))
            return {"output": text_content(inputs["input"]["text"] + "|" + str(value["generation"]))}

        service.registry.register(NodeDefinition(
            "test.chat-context-probe", "1", "Context probe", "Test",
            {"object_key": "context"},
            {"type": "object", "properties": {"object_key": {"type": "string"}},
             "required": ["object_key"], "additionalProperties": False},
            inputs=(NodePort("input", "TEXT", data_schema_version=2),),
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
            capabilities=("objects:read",),
            object_accesses=({
                "config_field": "object_key", "multiple": False, "access": "read", **provided_type,
            },),
        ), probe)
        graph, messages, frontends = frontend_graph(service, bound=True)
        provider = node(service.registry, "test.chat-context-provider", 23001)
        reader = node(service.registry, "test.chat-context-probe", 23002)
        for item in (provider, reader):
            item["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        graph["nodes"] = [messages[0], provider, reader, frontends[0]]
        graph["edges"] = [
            edge(messages[0], reader, 23001),
            edge(reader, frontends[0], 23002, target_port="content"),
        ]
        graph["object_bindings"].append(ObjectBinding(
            "context", EFFECTIVE_CONTEXT_TYPE, 4, "shared",
            readers=(reader["node_binding_id"],), registrar_node_id=provider["node_binding_id"],
        ).to_dict())
        yield service, graph, frontends[0], observed


def create_with_context(service, graph, value):
    document = _save(service, graph)

    def seed(repo):
        session = service._initialize_session(repo, document)
        sid = session["workflow_session_id"]
        state = collect_checkpoint_state(
            repo, document["workflow_definition_id"], document["revision"], sid,
            types=service.registry.data_types, registry=service.registry)
        if value != EMPTY_CONTEXT:
            state["agent_contexts"]["context"] = {"value": deepcopy(value)}
        current = service._objects(repo).current(sid)
        keys = set(state["chat_contexts"]) | set(state["agent_contexts"])
        cas = {key: {field: current[key][field] for field in ("revision", "revision_id")} for key in keys}
        load_checkpoint_state_in_transaction(
            repo, session, state, cas, types=service.registry.data_types, registry=service.registry)
        service._commit(repo, session, source={"kind": "session_seed", "source": None}, history=[])
        return service._view(repo, sid)

    return service._change("test.context-chat.seed", identity(), {}, seed)


def replace_fixture_context(service, session, value):
    """Install accepted inline test state through the trusted data boundary."""
    def change(repo):
        stored = repo.get("workflow_session", workflow_session_id=session["workflow_session_id"])
        document = service._chat_fixed_document(repo, stored)
        service._write_chat_values(repo, stored, document, {"context": deepcopy(value)})
        stored["revision"] += 1
        repo.put("workflow_session", stored)
        service._commit(repo, stored, source={"kind": "test_context_value"})
        return service._view(repo, stored["workflow_session_id"])

    return service._change("test.context-chat.value", identity(), {"value": value}, change)


def guards(session):
    return {
        "object_key": "chat-0",
        "expected_session_revision": session["revision"],
        "expected_data_revision": session["data_revision"],
        "expected_head_revision": session["head_revision"],
        "expected_object_revision": session["objects"]["chat-0"]["revision"],
        "idempotency_key": identity(),
    }


def settle(service, started):
    service.wait(started["active_chain_run_id"])
    return service.get_session(started["workflow_session_id"])


def submit(service, session, text="submitted"):
    return settle(service, service.chat_submit(
        session["workflow_session_id"], submitted_input=text, **guards(session)))


def reroll(service, session, round_id):
    return settle(service, service.chat_reroll(
        session["workflow_session_id"], round_id=round_id, **guards(session)))


def context_value(text, generation):
    value = agent_value(text)
    value["generation"] = generation
    return value


def test_first_round_null_basis_and_selection_restore_nonempty_accepted_agent_context(context_chat):
    service, graph, _, observed = context_chat
    initial_value = context_value("accepted initial context", 7)
    initial = create_with_context(service, graph, initial_value)
    first = submit(service, initial)
    assert first["status"] == "succeeded", first["chains"]
    floor = first["objects"]["chat-0"]["value"]["rounds"][0]
    assert floor["base_checkpoint_id"] is None
    with closing(service._store()) as store:
        with transaction(store._connection):
            frozen = read_chat_round_inputs(
                GraphRecordStore(store), first["workflow_session_id"], "chat-0", floor["round_id"])
            assert frozen["initial_compute_state"]["agent_contexts"] == {
                "context": {"value": initial_value}}
    changed = replace_fixture_context(service, first, context_value("later test state", 91))
    second = reroll(service, changed, floor["round_id"])
    assert second["status"] == "succeeded", second["chains"]
    assert observed == [initial_value, initial_value]
    assert second["objects"]["context"]["value"] == initial_value
    second_floor = second["objects"]["chat-0"]["value"]["rounds"][0]
    assert len(second_floor["candidates"]) == 2
    assert [candidate["messages"][0]["text"] for candidate in second_floor["candidates"]] == [
        "submitted|7", "submitted|7"]
    first_candidate = second_floor["candidates"][0]
    changed = replace_fixture_context(service, second, context_value("another current value", 92))
    selected = service.chat_select(
        changed["workflow_session_id"], round_id=floor["round_id"],
        candidate_id=first_candidate["candidate_id"], **guards(changed))
    assert selected["objects"]["context"]["value"] == initial_value
    assert selected["objects"]["chat-0"]["value"]["rounds"][0]["candidates"] == second_floor["candidates"]
    assert len(checkpoint_history(service, selected)["path"]) == 2


def test_reroll_resets_context_when_pre_round_checkpoint_omits_its_empty_value(context_chat):
    service, graph, _, observed = context_chat
    initial = create_with_context(service, graph, deepcopy(EMPTY_CONTEXT))
    first = submit(service, initial, "first")
    first_point = checkpoint_history(service, first)["latest"]["checkpoint_id"]
    assert checkpoint_history(service, first)["state"]["agent_contexts"] == {}
    changed_value = context_value("later accepted value", 19)
    changed = replace_fixture_context(service, first, changed_value)
    second = submit(service, changed, "second")
    round_value = second["objects"]["chat-0"]["value"]["rounds"][1]
    assert round_value["base_checkpoint_id"] == first_point
    assert observed[-1] == changed_value
    changed = replace_fixture_context(service, second, context_value("value that must not leak", 88))
    final = reroll(service, changed, round_value["round_id"])
    assert final["status"] == "succeeded", final["chains"]
    assert observed == [EMPTY_CONTEXT, changed_value, EMPTY_CONTEXT]
    assert final["objects"]["context"]["value"] == EMPTY_CONTEXT
    floor = final["objects"]["chat-0"]["value"]["rounds"][1]
    assert [candidate["messages"][0]["text"] for candidate in floor["candidates"]] == [
        "second|19", "second|0"]
    history = checkpoint_history(service, final)
    assert len(history["path"]) == 3
    assert history["state"]["agent_contexts"] == {}
    assert history["state"] == history["collected"]


def test_success_checkpoint_fault_rolls_back_candidate_binding_and_retries_without_reexecution(
        context_chat, monkeypatch):
    service, graph, frontend, observed = context_chat
    initial = create_with_context(service, graph, context_value("accepted", 3))
    failures = []

    def fail_once(point):
        if point == "after_checkpoint_write" and not failures:
            failures.append(point)
            raise OSError("injected logical chat checkpoint failure")

    monkeypatch.setattr(service, "_fault_injector", fail_once)
    pending = submit(service, initial)
    assert pending["status"] == "archive_failed"
    assert pending["head_commit_id"] == initial["head_commit_id"]
    assert checkpoint_history(service, pending)["latest"] is None
    floor = pending["objects"]["chat-0"]["value"]["rounds"][0]
    assert len(floor["candidates"]) == 1
    assert floor["candidates"][0]["result_checkpoint_id"] is None
    chain_id = pending["chains"][-1]["chain_run_id"]
    before = service.get_run(pending["workflow_session_id"], chain_id)
    retried = service.control(
        pending["workflow_session_id"], action="retry_archive",
        expected_revision=pending["revision"], idempotency_key=identity())
    final = settle(service, retried)
    assert final["status"] == "succeeded"
    history = checkpoint_history(service, final)
    assert len(history["path"]) == 1 and len(observed) == 1
    candidate = final["objects"]["chat-0"]["value"]["rounds"][0]["candidates"][0]
    assert candidate["candidate_id"] == floor["candidates"][0]["candidate_id"]
    assert candidate["messages"] == floor["candidates"][0]["messages"]
    assert candidate["result_checkpoint_id"] == history["latest"]["checkpoint_id"]
    after = service.get_run(final["workflow_session_id"], chain_id)
    assert after["node_runs"] == before["node_runs"]
    assert after["outputs"] == before["outputs"]
    assert after["runtime_facts"] == before["runtime_facts"]
    snapshot = service.get_cached_frontend_node_snapshot(
        final["workflow_definition_id"], final["definition_revision"],
        frontend["node_binding_id"], final["workflow_session_id"])
    assert snapshot["chat"] == final["objects"]["chat-0"]["value"]
    assert snapshot["basis"]["revision_id"] == final["objects"]["chat-0"]["revision_id"]


def test_downstream_failure_keeps_pending_chat_candidate_without_claiming_a_success_point(context_chat):
    service, graph, frontend, observed = context_chat
    fail_next = [True]

    def fail(config, inputs, context):
        if fail_next[0]:
            raise ValueError("later workflow output failed")
        return {}

    service.registry.register(NodeDefinition(
        "test.chat-downstream-failure", "1", "Later failure", "Test",
        {}, {"type": "object", "additionalProperties": False},
        inputs=(NodePort("commit", FRONTEND_COMMIT_TYPE),), is_output=True,
    ), fail)
    failing = node(service.registry, "test.chat-downstream-failure", 23101)
    failing["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    graph["nodes"].append(failing)
    graph["edges"].append(edge(frontend, failing, 23101, source_port="commit", target_port="commit"))
    initial = create_with_context(service, graph, deepcopy(EMPTY_CONTEXT))
    final = submit(service, initial)
    assert final["status"] == "failed" and len(observed) == 1
    floor = final["objects"]["chat-0"]["value"]["rounds"][0]
    assert len(floor["candidates"]) == 1 and floor["candidates"][0]["result_checkpoint_id"] is None
    assert checkpoint_history(service, final)["latest"] is None
    closed = service.control(
        final["workflow_session_id"], action="close",
        expected_revision=final["revision"], idempotency_key=identity())
    closed = service.get_session(closed["workflow_session_id"])
    for operation, arguments in (
        (service.chat_select, {"candidate_id": floor["candidates"][0]["candidate_id"]}),
        (service.chat_fork_floor, {}),
    ):
        with pytest.raises(ContractValidationError) as caught:
            operation(closed["workflow_session_id"], round_id=floor["round_id"],
                      **arguments, **guards(closed))
        assert caught.value.reason_code == "chat_candidate_uncommitted"
    fail_next[0] = False
    recovered = reroll(service, closed, floor["round_id"])
    assert recovered["status"] == "succeeded"
    saved = recovered["objects"]["chat-0"]["value"]["rounds"][0]
    assert len(saved["candidates"]) == 2
    assert saved["candidates"][0] == floor["candidates"][0]
    assert saved["candidates"][1]["result_checkpoint_id"] is not None
    fork = service.chat_fork_floor(
        recovered["workflow_session_id"], round_id=floor["round_id"], **guards(recovered))
    target = service.get_session(fork["target_session_id"])
    target_floor = target["objects"]["chat-0"]["value"]["rounds"][0]
    assert target_floor["candidates"][0] == floor["candidates"][0]
    assert len(target_floor["candidates"]) == 2
    assert target_floor["candidates"][1]["result_checkpoint_id"] == fork["target_checkpoint_id"]
    deleted = service.delete_session(
        recovered["workflow_session_id"], expected_revision=recovered["revision"], idempotency_key=identity())
    assert deleted["deleted"]
    changed = replace_fixture_context(service, target, context_value("target current value", 88))
    rerolled = reroll(service, changed, floor["round_id"])
    assert rerolled["status"] == "succeeded"
    current_floor = rerolled["objects"]["chat-0"]["value"]["rounds"][0]
    assert current_floor["candidates"][0] == floor["candidates"][0]
    assert len(current_floor["candidates"]) == 3
    assert current_floor["candidates"][2]["result_checkpoint_id"] is not None
    assert observed == [EMPTY_CONTEXT] * 3
    selected = service.chat_select(
        rerolled["workflow_session_id"], round_id=floor["round_id"],
        candidate_id=current_floor["candidates"][1]["candidate_id"], **guards(rerolled))
    assert selected["objects"]["chat-0"]["value"]["rounds"][0]["candidates"] == current_floor["candidates"]
    assert selected["objects"]["context"]["value"] == EMPTY_CONTEXT


def test_multi_item_reroll_rejects_mixed_bases_without_advancing_other_chat_items(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "chat-conflicting-bases.sqlite")) as service:
        graph, _, _ = frontend_graph(service, count=2, bound=True)
        graph = _save(service, graph)
        initial = service.create_session(
            graph["workflow_definition_id"], graph["revision"], idempotency_key=identity())
        first = submit(service, initial)
        assert first["status"] == "succeeded"
        first_round = first["objects"]["chat-0"]["value"]["rounds"][0]
        before = raw_database(service.database)
        registries = set(service._execution_registries)
        with pytest.raises(ContractValidationError) as caught:
            service.chat_reroll(
                first["workflow_session_id"], round_id=first_round["round_id"], **guards(first))
        assert caught.value.reason_code == "chat_computation_basis_conflict"
        assert raw_database(service.database) == before
        assert service.get_session(first["workflow_session_id"]) == first
        assert set(service._execution_registries) == registries
        second = submit(service, first, "same-basis next round")
        assert second["status"] == "succeeded"
        latest = first_round["candidates"][0]["result_checkpoint_id"]
        assert all(item["value"]["rounds"][1]["base_checkpoint_id"] == latest
                   for item in second["objects"].values())
