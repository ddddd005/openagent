"""Offline acceptance of the installed model/Agent/update chain in fresh stores."""

from contextlib import closing
from copy import deepcopy
import json
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.checkpoint_repository import CheckpointRepository
from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.frontend_contracts import FRONTEND_STATE_TYPE
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.model_request_contracts import default_model_runtime
from phase1_agent.session_objects import SessionObjectStore

from credential_test_support import TestProtector, seed_service_credential


def _identity(type_id):
    return {"envelope_version": 1, "scope": "workspace",
            "type_id": type_id, "resource_id": str(uuid4())}


def _save_resource(service, identity, value, schema, *, sequence=1):
    record = {**identity, "data_schema_version": schema,
              "update_sequence": sequence, "value": deepcopy(value)}
    service.save_global_resource(
        record, expected_sequence=sequence - 1, idempotency_key=str(uuid4()))
    return record


def _resources(service, *, threshold=8500):
    provider, profile = _identity("workflow.chat-provider"), _identity("workflow.model-profile")
    _save_resource(service, provider, {
        "name": "Offline provider", "protocol": "chat", "base_url": "https://first.invalid",
        "credential_ref": seed_service_credential(service, secret="stage4-private-test-key"),
        "enabled": True,
    }, 3)
    runtime = default_model_runtime()
    runtime["compaction_threshold_bps"] = threshold
    _save_resource(service, profile, {
        "name": "Offline model", "provider": provider,
        "model": {"id": "deepseek-chat", "capacity": {
            "context_window_tokens": 400000, "input_limit_tokens": 400000,
            "max_output_tokens": 64000}},
        "runtime": runtime, "enabled": True,
    }, 2)
    return provider, profile


def _node(service, component, version, **config):
    definition = service.registry.get(component, version).definition
    return {
        "node_binding_id": str(uuid4()), "component_id": component, "component_version": version,
        "title": component, "position": {"x": 0, "y": 0},
        "config": {**deepcopy(definition.default_config), **config},
        "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
    }


def _edge(source, target, *, source_port="output", target_port="input"):
    return {
        "edge_id": str(uuid4()), "source_node_id": source["node_binding_id"],
        "target_node_id": target["node_binding_id"], "source_port_id": source_port,
        "target_port_id": target_port, "order": 0,
    }


def _save_graph(service, nodes, edges, bindings):
    graph = {
        "schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Installed stage 4 acceptance", "nodes": nodes, "edges": edges,
        "object_bindings": bindings,
        "package_lock": deepcopy(list(service.registry.execution_package_lock)),
    }
    result = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))
    assert result["outcome"] == "saved"
    assert service.get_definition(graph["workflow_definition_id"]) == result["document"]
    return service.create_session(
        graph["workflow_definition_id"], 1, idempotency_key=str(uuid4()))


def _agent_graph(service, profile, *, count=2, writes=True, compaction=False):
    current = _node(service, "tools.current-input", "1")
    model = _node(service, "models.source", "7", reference=profile)
    agents = [_node(service, "agents.execute", "10") for _ in range(count)]
    nodes, edges, bindings, branches = [current, model, *agents], [], [], []
    for index, agent in enumerate(agents):
        key = "context-" + str(index)
        read = _node(service, "context.output", "5", object_key=key)
        assembly = _node(service, "context.assembly", "5")
        material = _node(service, "prompts.item", "2", text="RULE_" + str(index))
        output = _node(service, "tools.output", "2")
        nodes += [read, assembly, material, output]
        edges += [
            _edge(read, assembly, target_port="view"),
            _edge(current, assembly, target_port="current_input"),
            _edge(material, assembly, target_port="materials"),
            _edge(assembly, agent, target_port="prompt"),
            _edge(model, agent, target_port="model"),
            _edge(agent, output, source_port="result"),
        ]
        readers, writers = [read["node_binding_id"]], []
        branch = {"key": key, "read": read, "agent": agent, "output": output}
        if compaction:
            policy = _node(service, "agents.compaction-policy", "2", keep_depth=0)
            nodes.append(policy)
            edges.append(_edge(policy, agent, target_port="compaction_policy"))
            branch["policy"] = policy
        if writes:
            merge = _node(service, "context.merge", "6", object_key=key)
            nodes.append(merge)
            edges += [
                _edge(read, merge, target_port="view"),
                _edge(agent, merge, source_port="receipts", target_port="receipts"),
            ]
            readers.append(merge["node_binding_id"])
            writers.append(merge["node_binding_id"])
            branch["merge"] = merge
        bindings += [
            ObjectBinding(
                key, EFFECTIVE_CONTEXT_TYPE, 4, "shared", readers=tuple(readers),
                writers=tuple(writers),
                registrar_node_id=agents[(index + 1) % count]["node_binding_id"]).to_dict(),
            ObjectBinding(
                "display-" + str(index), FRONTEND_STATE_TYPE, 1, "shared",
                registrar_node_id=output["node_binding_id"]).to_dict(),
        ]
        branches.append(branch)
    return _save_graph(service, nodes, edges, bindings), branches


def _execute(service, view, text="offline input"):
    accepted = service.start(
        view["workflow_session_id"], inputs={"text": text},
        expected_revision=view["revision"], idempotency_key=str(uuid4()))
    service.wait(accepted["active_chain_run_id"])
    return service.get_session(view["workflow_session_id"])


def _output(view, node, port):
    return next(entry["outputs"][port] for entry in view["nodes"]
                if entry["node_binding_id"] == node["node_binding_id"])


def _checkpoint(service, view):
    with closing(service._store()) as store:
        store._connection.execute("BEGIN")
        try:
            repository = CheckpointRepository(
                store._connection, workflow_definition_id=view["workflow_definition_id"],
                workflow_session_id=view["workflow_session_id"],
                fixed_definition_revision=view["definition_revision"])
            point = repository.latest()
            return point, repository.materialize(point["checkpoint_id"]) if point else None
        finally:
            store._connection.execute("ROLLBACK")


class Answers:
    def __init__(self, *, after_first=None, unknown=False, steps=0):
        self.parameters, self.messages, self.addresses, self.closes = [], [], [], 0
        self.after_first, self.unknown = after_first, unknown
        self.steps = steps

    def factory(self, *, provider, parameters, api_key):
        assert api_key == "stage4-private-test-key"
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate_prepared(self, prepared):
                wire = prepared["wire_request"]
                assert wire["max_tokens"] == parameters["max_tokens"]
                return self.generate(wire["messages"], wire.get("tools", []))

            def generate(self, messages, tools):
                fixture.parameters.append(deepcopy(parameters))
                fixture.addresses.append(provider["base_url"])
                fixture.messages.append(deepcopy(messages))
                if fixture.unknown:
                    raise RuntimeError("stage4-private-test-key")
                if len(fixture.messages) == 1 and fixture.after_first is not None:
                    fixture.after_first()
                if not tools:
                    return ModelResponse("stop", content="offline chat result")
                if len(fixture.messages) <= fixture.steps:
                    return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                        "inspect-" + str(len(fixture.messages)), "inspect_text",
                        json.dumps({"text": "offline tool input"}),
                    ),))
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final.offline", "final_answer",
                    json.dumps({"answer": {"text": "answer-" + str(len(fixture.messages))}}),
                ),))

            def close(self):
                fixture.closes += 1

        return Transport()


def test_installed_two_agents_receipts_updates_checkpoints_and_fork(tmp_path):
    answers = Answers()
    with closing(GraphWorkflowService(
            tmp_path / "installed.sqlite", credential_protector=TestProtector(),
            public_model_factory=answers.factory)) as service:
        _, profile = _resources(service)
        initial, branches = _agent_graph(service, profile)
        final = _execute(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        assert len(answers.messages) == answers.closes == 2
        assert [item["max_tokens"] for item in answers.parameters] == [32000, 32000]
        for branch in branches:
            receipts = _output(final, branch["agent"], "receipts")
            assert receipts["schema_version"] == 2
            assert len(receipts["model_requests"]) == 1
            assert not {"object_key", "next_view", "basis_revision"}.intersection(receipts)
            assert _output(final, branch["read"], "output")["messages"] == []
            current = final["objects"][branch["key"]]
            assert current["revision"] == 2 and current["value"]["messages"]
        point, state = _checkpoint(service, final)
        assert set(state["agent_contexts"]) == {"context-0", "context-1"}
        result = service.fork_checkpoint(
            final["workflow_session_id"], checkpoint_id=point["checkpoint_id"],
            expected_session_revision=final["revision"],
            expected_data_revision=final["data_revision"], expected_head_revision=final["head_revision"],
            idempotency_key=str(uuid4()))
        target = service.get_session(result["target_session_id"])
        assert len(answers.messages) == 2
        assert all(target["objects"][key]["value"] == final["objects"][key]["value"]
                   for key in ("context-0", "context-1"))
        assert not service._service_runs
        facts = service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])["runtime_facts"]
        assert len([fact for fact in facts if fact.get("kind") == "workflow.model-fact"]) == 6
        assert "stage4-private-test-key" not in json.dumps(facts)
        continued = _execute(service, target, text="child input")
        assert continued["status"] == "succeeded", continued["chains"]
        assert len(answers.messages) == 4
        assert "offline input" in json.dumps(continued["objects"]["context-0"]["value"])
        assert "child input" in json.dumps(continued["objects"]["context-0"]["value"])
        assert service.get_session(final["workflow_session_id"])["objects"] == final["objects"]


def test_next_agent_reads_new_saved_profile_and_new_provider_dependency(tmp_path):
    answers = Answers()
    with closing(GraphWorkflowService(
            tmp_path / "live-config.sqlite", credential_protector=TestProtector(),
            public_model_factory=answers.factory)) as service:
        provider, profile = _resources(service)
        initial, _ = _agent_graph(service, profile)

        def change():
            current_provider = service.get_global_resource(provider)
            new_provider = _identity("workflow.chat-provider")
            _save_resource(service, new_provider, {
                **current_provider["value"], "name": "New offline provider",
                "base_url": "https://second.invalid",
            }, 3)
            current = service.get_global_resource(profile)
            current["value"]["provider"] = new_provider
            current["value"]["runtime"]["output_limit_tokens"] = 16000
            _save_resource(service, profile, current["value"], 2, sequence=2)

        answers.after_first = change
        final = _execute(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        assert [item["max_tokens"] for item in answers.parameters] == [32000, 16000]
        assert answers.addresses == ["https://first.invalid", "https://second.invalid"]


def test_same_agent_exceeds_old_eight_calls_and_reads_new_runtime_per_request(tmp_path):
    answers = Answers(steps=9)
    with closing(GraphWorkflowService(
            tmp_path / "no-quota.sqlite", credential_protector=TestProtector(),
            public_model_factory=answers.factory)) as service:
        _, profile = _resources(service)
        initial, branches = _agent_graph(service, profile, count=1)

        def change():
            current = service.get_global_resource(profile)
            current["value"]["runtime"]["output_limit_tokens"] = 16000
            _save_resource(service, profile, current["value"], 2, sequence=2)

        answers.after_first = change
        final = _execute(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        assert len(answers.messages) == 10
        assert [item["max_tokens"] for item in answers.parameters] == [32000] + [16000] * 9
        receipts = _output(final, branches[0]["agent"], "receipts")
        assert len(receipts["model_requests"]) == 10
        assert final["objects"]["context-0"]["revision"] == 2
        assert "extend_budget" not in final["available_actions"]


def test_installed_compaction_uses_global_output_and_has_no_second_low_water_target(tmp_path):
    answers = Answers()
    with closing(GraphWorkflowService(
            tmp_path / "compaction.sqlite", credential_protector=TestProtector(),
            public_model_factory=answers.factory)) as service:
        _, profile = _resources(service)
        initial, branches = _agent_graph(service, profile, count=1, compaction=True)
        first = _execute(service, initial, text="retained original " + "x" * 8000)
        assert first["status"] == "succeeded", first["chains"]
        first_point, first_state = _checkpoint(service, first)
        current = service.get_global_resource(profile)
        current["value"]["runtime"]["compaction_threshold_bps"] = 1000
        _save_resource(service, profile, current["value"], 2, sequence=2)
        final = _execute(service, first, text="new compacted input")
        assert final["status"] == "succeeded", final["chains"]
        assert len(answers.messages) == 3
        assert [item["max_tokens"] for item in answers.parameters] == [32000, 32000, 32000]
        receipts = _output(final, branches[0]["agent"], "receipts")
        assert len(receipts["model_requests"]) == 2
        assert final["objects"]["context-0"]["value"]["generation"] == 1
        assert "retained original" not in json.dumps(final["objects"]["context-0"]["value"])
        with closing(service._store()) as store:
            store._connection.execute("BEGIN")
            try:
                repository = CheckpointRepository(
                    store._connection, workflow_definition_id=first["workflow_definition_id"],
                    workflow_session_id=first["workflow_session_id"],
                    fixed_definition_revision=first["definition_revision"])
                assert repository.materialize(first_point["checkpoint_id"]) == first_state
            finally:
                store._connection.execute("ROLLBACK")


def test_without_policy_low_trigger_does_not_fail_and_without_merge_does_not_write(tmp_path):
    answers = Answers()
    with closing(GraphWorkflowService(
            tmp_path / "read-only.sqlite", credential_protector=TestProtector(),
            public_model_factory=answers.factory)) as service:
        _, profile = _resources(service, threshold=1)
        initial, branches = _agent_graph(service, profile, count=1, writes=False)
        before = deepcopy(initial["objects"])
        final = _execute(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        assert final["objects"] == before
        assert _output(final, branches[0]["output"], "output")["text"] == "answer-1"
        assert _checkpoint(service, final)[1]["agent_contexts"] == {}


def test_pause_resume_keeps_original_request_and_reads_config_only_for_next_request(tmp_path):
    entered, proceed = Event(), Event()

    def gate():
        entered.set()
        assert proceed.wait(10), "Offline model gate was not released"

    answers = Answers(steps=1, after_first=gate)
    with closing(GraphWorkflowService(
            tmp_path / "pause.sqlite", credential_protector=TestProtector(),
            public_model_factory=answers.factory)) as service:
        _, profile = _resources(service)
        initial, branches = _agent_graph(service, profile, count=1)
        sid = initial["workflow_session_id"]
        started = service.start(
            sid, inputs={"text": "paused continuation"},
            expected_revision=initial["revision"], idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        try:
            assert entered.wait(5)
            current = service.get_session(sid)
            service.control(sid, action="pause", expected_revision=current["revision"],
                            idempotency_key=str(uuid4()))
        finally:
            proceed.set()
        service.wait(chain_id)
        paused = service.get_session(sid)
        assert paused["status"] == "paused", paused["chains"]
        assert paused["objects"] == initial["objects"] and len(answers.messages) == 1
        assert _checkpoint(service, paused)[0] is None
        current = service.get_global_resource(profile)
        current["value"]["runtime"]["output_limit_tokens"] = 16000
        _save_resource(service, profile, current["value"], 2, sequence=2)
        service.control(sid, action="resume", expected_revision=paused["revision"],
                        idempotency_key=str(uuid4()))
        service.wait(chain_id)
        final = service.get_session(sid)
        assert final["status"] == "succeeded", final["chains"]
        assert len(answers.messages) == 2
        assert [item["max_tokens"] for item in answers.parameters] == [32000, 16000]
        assert len(_output(final, branches[0]["agent"], "receipts")["model_requests"]) == 2
        assert _checkpoint(service, final)[0] is not None


def test_unknown_request_is_not_replayed_and_does_not_save_success_point(tmp_path):
    answers = Answers(unknown=True)
    with closing(GraphWorkflowService(
            tmp_path / "unknown.sqlite", credential_protector=TestProtector(),
            public_model_factory=answers.factory)) as service:
        _, profile = _resources(service)
        initial, _ = _agent_graph(service, profile, count=1)
        final = _execute(service, initial)
        assert final["status"] == "failed", final["chains"]
        assert len(answers.messages) == 1
        assert "extend_budget" not in final["available_actions"]
        assert "retry_failed_node" not in final["available_actions"]
        assert _checkpoint(service, final)[0] is None
        assert final["objects"] == initial["objects"]
        assert "stage4-private-test-key" not in json.dumps(final)
        with pytest.raises(Exception) as error:
            service.control(final["workflow_session_id"], action="extend_budget",
                            expected_revision=final["revision"], idempotency_key=str(uuid4()))
        assert getattr(error.value, "reason_code", None) == "invalid_request"
        assert len(answers.messages) == 1


def test_explicit_merge_settlement_rolls_back_and_reuses_accepted_result(tmp_path, monkeypatch):
    answers = Answers()
    with closing(GraphWorkflowService(
            tmp_path / "settlement.sqlite", credential_protector=TestProtector(),
            public_model_factory=answers.factory)) as service:
        _, profile = _resources(service)
        initial, branches = _agent_graph(service, profile, count=1)
        apply, fail = SessionObjectStore.apply, [True]

        def fail_once(store, sid, *, node_id, writes):
            result = apply(store, sid, node_id=node_id, writes=writes)
            if writes and fail[0]:
                fail[0] = False
                raise OSError("offline settlement fault")
            return result

        monkeypatch.setattr(SessionObjectStore, "apply", fail_once)
        pending = _execute(service, initial)
        assert pending["status"] == "archive_failed", pending["chains"]
        assert pending["objects"] == initial["objects"]
        assert _checkpoint(service, pending)[0] is None
        original = deepcopy(_output(pending, branches[0]["agent"], "receipts"))
        chain_id = pending["active_chain_run_id"]
        service.control(
            pending["workflow_session_id"], action="retry_acceptance",
            expected_revision=pending["revision"], idempotency_key=str(uuid4()))
        service.wait(chain_id)
        final = service.get_session(pending["workflow_session_id"])
        assert final["status"] == "succeeded", final["chains"]
        assert len(answers.messages) == 1
        assert _output(final, branches[0]["agent"], "receipts") == original
        assert final["objects"]["context-0"]["revision"] == 2
        assert _checkpoint(service, final)[0] is not None


def test_installed_chat_publishes_one_response_without_agent_compaction(tmp_path):
    answers = Answers()
    with closing(GraphWorkflowService(
            tmp_path / "chat.sqlite", credential_protector=TestProtector(),
            public_model_factory=answers.factory)) as service:
        _, profile = _resources(service, threshold=1)
        source = _node(service, "models.source", "7", reference=profile)
        current = _node(service, "tools.current-input", "1")
        prompt = _node(service, "prompts.assembly", "2")
        chat = _node(service, "models.chat", "5")
        # Chat results are MODEL_RESULT, so inspect the Chat's public artifact directly.
        chat["public_outputs"] = ["output"]
        initial = _save_graph(service, [source, current, prompt, chat], [
            _edge(source, chat, target_port="model"),
            _edge(current, prompt, target_port="current_input"),
            _edge(prompt, chat, target_port="prompt"),
        ], [])
        final = _execute(service, initial)
        assert final["status"] == "succeeded", final["chains"]
        result = _output(final, chat, "output")
        assert result["schema_version"] == 3 and result["content"] == "offline chat result"
        assert len(answers.messages) == 1 and answers.parameters[0]["max_tokens"] == 32000
