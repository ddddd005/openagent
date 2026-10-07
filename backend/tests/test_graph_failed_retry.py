"""Confirmed B failures restart in the original chain without replaying A."""

from contextlib import closing
from copy import deepcopy
import json
from uuid import uuid4

import httpx
import pytest
from openai import APIStatusError

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_records import validate_graph_record, validate_graph_transition
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.model_service import PublicModelService

from test_agent_integration import run
from test_context_summary_integration import base_graph
from test_graph_service import copy_current, create, document, edge, node
from test_models_service_integration import ModelDatabaseFixture


def serial_graph(service):
    doc = base_graph(service)
    first = {item["component_id"]: item for item in doc["nodes"]}
    a, a_read, a_merge = first["agents.execute"], first["context.output"], first["context.merge"]
    for item in (a_read, a_merge):
        item["config"]["object_key"] = "context/a"
    b_read, b_assembly, b, b_merge, display = [
        node(service.registry, component, 2201 + index) for index, component in enumerate((
            "context.output", "context.assembly", "agents.execute", "context.merge", "tools.output"))]
    for item, version in ((b_read, "2"), (b_assembly, "3"), (b, "3"), (b_merge, "2")):
        item["component_version"] = version
        item["config"] = deepcopy(service.registry.get(item["component_id"], version).definition.default_config)
    for item in (b_read, b_merge):
        item["config"].update(object_key="context/b", agent_node_id=b["node_binding_id"])
    doc["nodes"] = [item for item in doc["nodes"] if item["component_id"] != "tools.output"]
    doc["edges"] = [item for item in doc["edges"] if item["target_node_id"] != first["tools.output"]["node_binding_id"]]
    doc["nodes"] += [b_read, b_assembly, b, b_merge, display]
    doc["edges"] += [
        edge(b_read, b_assembly, 2201, target_port="view"),
        edge(a, b_assembly, 2202, source_port="result", target_port="current_input"),
        edge(first["models.source"], b, 2203, target_port="model"),
        edge(b_assembly, b, 2204, target_port="prompt"),
        edge(b_read, b_merge, 2205, target_port="view"),
        edge(b, b_merge, 2206, source_port="context", target_port="context"),
        edge(b, display, 2207, source_port="result"),
    ]
    doc["control_edges"] = [{"edge_id": str(uuid4()), "source_node_id": b["node_binding_id"],
                             "target_node_id": a_merge["node_binding_id"]}]
    doc["execution_roots"] = [a_merge["node_binding_id"], b_merge["node_binding_id"]]
    doc["object_bindings"] = [
        ObjectBinding(key, EFFECTIVE_CONTEXT_TYPE, 3, "shared",
                      readers=(read["node_binding_id"], merge["node_binding_id"]),
                      writers=(merge["node_binding_id"],)).to_dict()
        for key, read, merge in (("context/a", a_read, a_merge), ("context/b", b_read, b_merge))]
    return doc, a["node_binding_id"], b["node_binding_id"]


class FailedBTransport:
    def __init__(self, mode):
        self.mode, self.calls, self.closes = mode, [], 0
        self.blocked = False

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append(deepcopy(messages))
                if len(fixture.calls) == 3 and fixture.mode in ("repeat_429", "429_then_unknown"):
                    if fixture.mode == "429_then_unknown":
                        raise httpx.ReadTimeout("offline second attempt result unknown")
                    response = httpx.Response(429, request=httpx.Request("POST", "https://offline.test"))
                    raise APIStatusError("offline second confirmed rejection", response=response, body=None)
                if fixture.mode == "tool_then_429" and len(fixture.calls) == 2:
                    return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                        "inspect", "inspect_text", json.dumps({"text": "known inspection"})),))
                if fixture.mode == "tool_then_429" and len(fixture.calls) == 3:
                    response = httpx.Response(429, request=httpx.Request("POST", "https://offline.test"))
                    raise APIStatusError("offline confirmed rejection after tool", response=response, body=None)
                if len(fixture.calls) == 2 and fixture.mode != "not_dispatched":
                    if fixture.mode == "unknown":
                        raise httpx.ReadTimeout("offline dispatch result unknown")
                    status = 429 if fixture.mode in ("repeat_429", "429_then_unknown") else int(fixture.mode)
                    response = httpx.Response(status, request=httpx.Request("POST", "https://offline.test"))
                    raise APIStatusError("offline confirmed rejection", response=response, body=None)
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final", "final_answer", json.dumps({"answer": {"text": f"answer {len(fixture.calls)}"}})),))

            def close(self):
                fixture.closes += 1

        return Transport()

    def install(self, monkeypatch):
        original = PublicModelService._boundary
        fixture = self

        def boundary(service, frame):
            if fixture.mode == "not_dispatched" and len(fixture.calls) == 1 and not fixture.blocked:
                fixture.blocked = True
                raise OSError("offline pre-dispatch interruption")
            return original(service, frame)

        monkeypatch.setattr(PublicModelService, "_boundary", boundary)


def setup(service, *, public_b=False, merge_a_first=False):
    ModelDatabaseFixture.write(service, 1)
    doc, a, b = serial_graph(service)
    if public_b:
        next(item for item in doc["nodes"] if item["node_binding_id"] == b)["public_outputs"] = ["result"]
    if merge_a_first:
        doc["control_edges"] = []
    return create(service, doc), a, b


@pytest.mark.parametrize("mode", ["429", "503", "not_dispatched"])
def test_confirmed_failure_restarts_b_with_new_identity_keeps_a_and_one_completed_checkpoint(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-failed-retry")
    transport = FailedBTransport(mode)
    transport.install(monkeypatch)
    with closing(GraphWorkflowService(tmp_path / "retry.sqlite", public_model_factory=transport.factory)) as service:
        initial, a, b = setup(service)
        failed = run(service, initial, "original input")
        assert failed["status"] == "failed"
        assert failed["available_actions"] == ["retry_failed_node", "close"], failed["chains"]
        assert failed["head_commit_id"] == initial["head_commit_id"]
        assert all(item["revision"] == 1 for item in failed["objects"].values())
        chain_id = failed["active_chain_run_id"]
        prior = service.get_run(failed["workflow_session_id"], chain_id)
        old_b = next(item for item in prior["node_runs"] if item["node_binding_id"] == b)
        old_a = next(item for item in prior["node_runs"] if item["node_binding_id"] == a)
        assert old_b["status"] == "failed" and not old_b["output_refs"]
        request = {"session_id": failed["workflow_session_id"], "action": "retry_failed_node",
                   "expected_revision": failed["revision"], "idempotency_key": str(uuid4())}
        app = GraphApplication(service)
        receipt = app.command("consumer.run.control", request)
        service.wait(chain_id)
        assert app.command("consumer.run.control", request)["receipt"] == receipt["receipt"]
        final = service.get_session(failed["workflow_session_id"])
        assert final["status"] == "succeeded", final["chains"]
        history = service.get_run(failed["workflow_session_id"], chain_id)
        assert history["chain"]["chain_run_id"] == chain_id
        assert history["chain"]["inputs"] == prior["chain"]["inputs"]
        assert history["chain"]["definition_revision"] == prior["chain"]["definition_revision"]
        assert next(item for item in history["node_runs"] if item["run_id"] == old_a["run_id"]) == old_a
        assert next(item for item in history["node_runs"] if item["run_id"] == old_b["run_id"]) == old_b
        attempts = [item for item in history["node_runs"] if item["node_binding_id"] == b]
        assert [item["status"] for item in attempts] == ["failed", "succeeded"]
        assert attempts[1]["run_id"] != old_b["run_id"]
        assert attempts[1]["input_refs"] == old_b["input_refs"]
        assert history["runtime_facts"][:len(prior["runtime_facts"])] == prior["runtime_facts"]
        assert len(transport.calls) == (2 if mode == "not_dispatched" else 3)
        assert all(item["revision"] == 2 and len(item["value"]["accepted_delta_ids"]) == 1
                   for item in final["objects"].values())
        assert final["head_commit_id"] != initial["head_commit_id"]
        assert not service._failed_retry_candidates and not service._service_runs and not service._runtime_hosts
        assert transport.closes == 1


@pytest.mark.parametrize("mode", ["unknown", "400", "tool_then_429"])
def test_unknown_and_unsupported_failure_have_no_retry_and_release_original_frame(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-failed-retry")
    transport = FailedBTransport(mode)
    with closing(GraphWorkflowService(tmp_path / "unsafe.sqlite", public_model_factory=transport.factory)) as service:
        initial, _, _ = setup(service)
        failed = run(service, initial, "original input")
        assert failed["available_actions"] == ["close"]
        assert not service._failed_retry_candidates and not service._service_runs
        with pytest.raises(ContractValidationError):
            service.control(failed["workflow_session_id"], action="retry_failed_node",
                            expected_revision=failed["revision"], idempotency_key=str(uuid4()))
        assert len(transport.calls) == (3 if mode == "tool_then_429" else 2)
        assert failed["head_commit_id"] == initial["head_commit_id"]


def test_basis_change_and_process_loss_refuse_original_failure_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-failed-retry")
    transport = FailedBTransport("429")
    database = tmp_path / "lost.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as service:
        initial, _, _ = setup(service)
        failed = run(service, initial, "original input")
        candidate = service._failed_retry_candidates[failed["active_chain_run_id"]]
        original = deepcopy(candidate["basis"])
        candidate["basis"]["objects"]["context/b"]["revision"] += 1
        assert service.get_consumer(failed["workflow_session_id"])["available_actions"] == ["close"]
        with pytest.raises(ContractValidationError) as caught:
            service.control(failed["workflow_session_id"], action="retry_failed_node",
                            expected_revision=failed["revision"], idempotency_key=str(uuid4()))
        assert caught.value.reason_code == "failed_retry_basis_changed"
        candidate["basis"] = original
    assert transport.closes == 1
    with closing(GraphWorkflowService(database, public_model_factory=transport.factory)) as reopened:
        view = reopened.get_consumer(failed["workflow_session_id"])
        assert view["available_actions"] == ["close"]
        assert next(item for item in view["nodes"] if item["status"] == "failed")["diagnostic"]["failure_retry"]["reason_code"] == "failed_retry_unavailable"
        with pytest.raises(ContractValidationError):
            reopened.control(failed["workflow_session_id"], action="retry_failed_node",
                             expected_revision=view["session_revision"], idempotency_key=str(uuid4()))
        assert len(transport.calls) == 2


def test_attempt_history_cannot_rewrite_success_or_restart_without_recorded_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-failed-retry")
    transport = FailedBTransport("429")
    with closing(GraphWorkflowService(tmp_path / "contracts.sqlite", public_model_factory=transport.factory)) as service:
        initial, _, _ = setup(service)
        failed = run(service, initial, "original input")
        prior = failed["chains"][-1]
        value = deepcopy(prior)
        value["status"], value["revision"] = "prepared", prior["revision"] + 1
        with pytest.raises(ContractValidationError):
            validate_graph_transition("chain_run", prior, value)
        index = prior["next_node_index"]
        value["node_run_attempts"][index].append(str(uuid4()))
        value["node_run_ids"][index] = value["node_run_attempts"][index][-1]
        value["diagnostic"] = None
        validate_graph_record("chain_run", value)
        validate_graph_transition("chain_run", prior, value)
        changed = deepcopy(value)
        changed["inputs"]["text"] = "changed input"
        with pytest.raises(ContractValidationError):
            validate_graph_transition("chain_run", prior, changed)
        changed = deepcopy(value)
        changed["node_run_attempts"][0].append(str(uuid4()))
        changed["node_run_ids"][0] = changed["node_run_attempts"][0][-1]
        with pytest.raises(ContractValidationError):
            validate_graph_transition("chain_run", prior, changed)


def test_old_schema_reading_and_unknown_version_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-failed-retry")
    transport = FailedBTransport("429")
    with closing(GraphWorkflowService(tmp_path / "versions.sqlite", public_model_factory=transport.factory)) as service:
        initial, _, _ = setup(service)
        failed = run(service, initial, "original input")
        current = failed["chains"][-1]
        old = deepcopy(current)
        old["schema_version"] = 4
        old.pop("node_run_attempts")
        assert validate_graph_record("chain_run", old) == old
        older = deepcopy(old)
        older["schema_version"] = 3
        for key in ("execution_kind", "event", "base_commit_id"):
            older.pop(key)
        assert validate_graph_record("chain_run", older) == older
        unknown = deepcopy(current)
        unknown["schema_version"] = 6
        with pytest.raises(ContractValidationError):
            validate_graph_record("chain_run", unknown)
        changed = deepcopy(old)
        changed["status"], changed["revision"] = "prepared", old["revision"] + 1
        with pytest.raises(ContractValidationError):
            validate_graph_transition("chain_run", old, changed)


def test_failed_attempt_information_remains_readable_after_success_and_on_copied_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-failed-retry")
    transport = FailedBTransport("429")
    with closing(GraphWorkflowService(tmp_path / "information.sqlite", public_model_factory=transport.factory)) as service:
        initial, _, b = setup(service)
        failed = run(service, initial, "original input")
        sid, chain_id = failed["workflow_session_id"], failed["active_chain_run_id"]
        bindings = service.list_registrations(session_id=sid, chain_id=chain_id, node_id=b,
                                             kind="information_binding")["items"]
        old = next(item for item in bindings if item["owner"]["node_binding_id"] == b)
        query = {"reference": old["registration_ref"], "owner": old["owner"],
                 "generation": old["generation"], "source_scope": "history"}
        before = service.read_information(sid, **query)
        service.control(sid, action="retry_failed_node", expected_revision=failed["revision"],
                        idempotency_key=str(uuid4()))
        service.wait(chain_id)
        final = service.get_session(sid)
        assert next(item for item in final["nodes"] if item["node_binding_id"] == b)["status"] == "succeeded"
        after_bindings = service.list_registrations(session_id=sid, chain_id=chain_id, node_id=b,
                                                   kind="information_binding")["items"]
        assert {item["owner"]["node_run_id"] for item in after_bindings} > {old["owner"]["node_run_id"]}
        assert service.read_information(sid, **query)["items"] == before["items"]
        doc = service.get_definition(final["workflow_definition_id"])
        doc["workflow_definition_id"] = str(uuid4())
        fork = copy_current(service, final, doc)
        assert service.read_information(fork["workflow_session_id"], **query)["items"] == before["items"]
        fork_bindings = service.list_registrations(session_id=fork["workflow_session_id"], chain_id=chain_id,
                                                  node_id=b, kind="information_binding")["items"]
        assert any(item["owner"] == old["owner"] for item in fork_bindings)
        other = service.create_session(final["workflow_definition_id"], final["definition_revision"],
                                       idempotency_key=str(uuid4()))
        with pytest.raises(ContractValidationError) as denied:
            service.read_information(other["workflow_session_id"], **query)
        assert denied.value.reason_code == "information_scope_denied"
        forged = deepcopy(query)
        forged["owner"]["node_binding_id"] = str(uuid4())
        with pytest.raises(ContractValidationError) as denied:
            service.read_information(sid, **forged)
        assert denied.value.reason_code == "information_owner_mismatch"


def test_non_agent_registered_failure_hook_can_restart_and_unregistered_ordinary_node_cannot(tmp_path):
    from phase1_agent.capability_registry import create_package_registry
    from phase1_agent.content_contracts import text_content
    from phase1_agent.graph_contracts import NodeDefinition, NodePort
    from phase1_agent.runtime_executor_contracts import ExecutorDefinition, ExecutorReference
    calls, disposals, validations = [], [], []
    reference = ExecutorReference("sample.confirmed-failure", "1")
    registry = create_package_registry().registry.detached()

    class Handle:
        def advance(self, callbacks, continuation):
            calls.append(callbacks.owner.node_run_id)
            if len(calls) == 1:
                callbacks.publish_fact("rejected", {"rejected": True})
                raise RuntimeError("known offline business failure")
            return {"output": text_content("accepted")}

        def dispose(self):
            disposals.append(True)

    def validate(config, evidence):
        validations.append(deepcopy(evidence))
        assert len(evidence["facts"]) == 1 and evidence["facts"][0]["payload"] == {"rejected": True}
        return {"classification": "confirmed_rejection", "service_ref": None, "service_evidence": {}}

    registry.executors.register_executor(ExecutorDefinition(reference), lambda config, inputs, context: Handle())
    registry.register(NodeDefinition("sample.rejected", "1", "Rejected", "Sample", {}, {"type": "object"},
                                    outputs=(NodePort("output", "TEXT", data_schema_version=2),), is_output=True),
                      None, executor_ref=reference, failed_retry_validator=validate)
    frozen = registry.detached(frozen=True)
    assert frozen.get("sample.rejected", "1").failed_retry_validator is validate
    with pytest.raises(ContractValidationError):
        registry.register(NodeDefinition("sample.bad-hook", "1", "Bad", "Sample", {}, {"type": "object"}),
                          lambda config, inputs, context: {}, failed_retry_validator=True)
    with closing(GraphWorkflowService(tmp_path / "generic.sqlite", registry=registry)) as service:
        work = node(service.registry, "sample.rejected", 3301)
        initial = create(service, document([work], []))
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        service.wait(started["active_chain_run_id"])
        failed = service.get_session(initial["workflow_session_id"])
        assert failed["available_actions"] == ["retry_failed_node", "close"]
        service.control(initial["workflow_session_id"], action="retry_failed_node",
                        expected_revision=failed["revision"], idempotency_key=str(uuid4()))
        service.wait(started["active_chain_run_id"])
        assert service.get_session(initial["workflow_session_id"])["status"] == "succeeded"
        assert len(calls) == len(disposals) == 2 and len(set(calls)) == 2 and len(validations) == 1
        service.registry.register(NodeDefinition("sample.ordinary-failure", "1", "Failure", "Sample", {},
                                                {"type": "object"},
                                                outputs=(NodePort("output", "TEXT", data_schema_version=2),),
                                                is_output=True),
                                  lambda config, inputs, context: (_ for _ in ()).throw(RuntimeError("ordinary failure")))
        ordinary = create(service, document([node(service.registry, "sample.ordinary-failure", 3302)], []))
        started = service.start(ordinary["workflow_session_id"], expected_revision=ordinary["revision"],
                                idempotency_key=str(uuid4()))
        service.wait(started["active_chain_run_id"])
        assert service.get_session(ordinary["workflow_session_id"])["available_actions"] == ["close"]


def test_public_result_selects_current_attempt_and_explicit_failed_history_keeps_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-failed-retry")
    transport = FailedBTransport("429")
    with closing(GraphWorkflowService(tmp_path / "public.sqlite", public_model_factory=transport.factory)) as service:
        initial, _, b = setup(service, public_b=True)
        failed = run(service, initial, "original input")
        sid, chain_id = failed["workflow_session_id"], failed["active_chain_run_id"]
        old_b = next(item for item in service.get_run(sid, chain_id)["node_runs"] if item["node_binding_id"] == b)
        query = {"workflow_definition_id": failed["workflow_definition_id"],
                 "definition_revision": failed["definition_revision"], "node_id": b, "port_id": "result"}
        service.control(sid, action="retry_failed_node", expected_revision=failed["revision"],
                        idempotency_key=str(uuid4()))
        service.wait(chain_id)
        final = service.get_session(sid)
        current = next(item for item in final["nodes"] if item["node_binding_id"] == b)
        expected = service.read_public_output(sid, **query)["output"]
        assert expected["run_id"] == current["run_id"] != old_b["run_id"]
        assert expected["availability"] == "produced" and expected["status"] == "succeeded"
        assert expected["payload"] == current["outputs"]["result"]
        assert next(item for item in service.get_consumer(sid)["outputs"]
                    if item["node_binding_id"] == b) == expected
        assert service.public_output_history(sid, **query)["outputs"] == [expected]
        historical = service.read_public_output(sid, **query, run_id=old_b["run_id"])["output"]
        assert historical["status"] == "failed" and historical["availability"] == "unproduced"
        assert historical["run_id"] == old_b["run_id"] and historical["payload"] is None
        doc = service.get_definition(final["workflow_definition_id"])
        doc["workflow_definition_id"] = str(uuid4())
        fork = copy_current(service, final, doc)
        fork_query = {**query, "workflow_definition_id": fork["workflow_definition_id"],
                      "definition_revision": fork["definition_revision"]}
        assert service.read_public_output(fork["workflow_session_id"], **fork_query)["output"] == expected
        assert service.read_public_output(fork["workflow_session_id"], **fork_query,
                                          run_id=old_b["run_id"])["output"] == historical
        other = service.create_session(final["workflow_definition_id"], final["definition_revision"],
                                       idempotency_key=str(uuid4()))
        with pytest.raises(ContractValidationError):
            service.read_public_output(other["workflow_session_id"], **query, run_id=old_b["run_id"])


def test_retry_preserves_already_accepted_a_context_merge_without_second_write(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-failed-retry")
    transport = FailedBTransport("429")
    with closing(GraphWorkflowService(tmp_path / "merged-prefix.sqlite", public_model_factory=transport.factory)) as service:
        initial, a, _ = setup(service, merge_a_first=True)
        failed = run(service, initial, "original input")
        sid, chain_id = failed["workflow_session_id"], failed["active_chain_run_id"]
        accepted = deepcopy(failed["objects"]["context/a"])
        assert accepted["revision"] == 2 and len(accepted["value"]["accepted_delta_ids"]) == 1
        assert failed["objects"]["context/b"]["revision"] == 1
        assert failed["head_commit_id"] == initial["head_commit_id"]
        prefix = deepcopy(failed["chains"][-1]["outputs"])
        service.control(sid, action="retry_failed_node", expected_revision=failed["revision"],
                        idempotency_key=str(uuid4()))
        service.wait(chain_id)
        final = service.get_session(sid)
        assert final["status"] == "succeeded", final["chains"]
        assert final["objects"]["context/a"] == accepted
        assert final["objects"]["context/b"]["revision"] == 2
        assert all(final["chains"][-1]["outputs"][key] == value for key, value in prefix.items())
        assert len([item for item in service.get_run(sid, chain_id)["node_runs"]
                    if item["node_binding_id"] == a]) == 1
        assert len(transport.calls) == 3


@pytest.mark.parametrize("mode", ["repeat_429", "429_then_unknown", "close"])
def test_repeated_rejection_unknown_after_retry_and_close_keep_prefix_and_release(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-failed-retry")
    transport = FailedBTransport("429" if mode == "close" else mode)
    with closing(GraphWorkflowService(tmp_path / "lifecycle.sqlite", public_model_factory=transport.factory)) as service:
        initial, a, b = setup(service)
        failed = run(service, initial, "original input")
        sid, chain_id = failed["workflow_session_id"], failed["active_chain_run_id"]
        prefix = deepcopy(failed["chains"][-1]["outputs"])
        first = service.get_run(sid, chain_id)
        assert failed["available_actions"] == ["retry_failed_node", "close"]
        if mode == "close":
            service.control(sid, action="close", expected_revision=failed["revision"],
                            idempotency_key=str(uuid4()))
            final = service.get_session(sid)
            assert final["status"] == "closed" and final["chains"][-1]["outputs"] == prefix
            assert len(transport.calls) == 2
        else:
            service.control(sid, action="retry_failed_node", expected_revision=failed["revision"],
                            idempotency_key=str(uuid4()))
            service.wait(chain_id)
            again = service.get_session(sid)
            assert again["status"] == "failed" and again["chains"][-1]["outputs"] == prefix
            assert again["head_commit_id"] == initial["head_commit_id"]
            if mode == "repeat_429":
                assert again["available_actions"] == ["retry_failed_node", "close"]
                service.control(sid, action="retry_failed_node", expected_revision=again["revision"],
                                idempotency_key=str(uuid4()))
                service.wait(chain_id)
                final = service.get_session(sid)
                assert final["status"] == "succeeded", final["chains"]
                history = service.get_run(sid, chain_id)
                assert len([item for item in history["node_runs"] if item["node_binding_id"] == a]) == 1
                assert [item["status"] for item in history["node_runs"] if item["node_binding_id"] == b] == [
                    "failed", "failed", "succeeded"]
                bindings = service.list_registrations(session_id=sid, node_id=b, kind="information_binding")["items"]
                assert len(bindings) == 3
                for binding in bindings:
                    assert service.read_information(sid, reference=binding["registration_ref"],
                        owner=binding["owner"], generation=binding["generation"], source_scope="history")["items"]
                assert len(transport.calls) == 4
            else:
                assert again["available_actions"] == ["close"]
                with pytest.raises(ContractValidationError):
                    service.control(sid, action="retry_failed_node", expected_revision=again["revision"],
                                    idempotency_key=str(uuid4()))
                assert len(transport.calls) == 3
            assert service.get_run(sid, chain_id)["runtime_facts"][:len(first["runtime_facts"])] == first["runtime_facts"]
        assert not service._failed_retry_candidates and not service._service_runs and not service._runtime_hosts
        assert transport.closes == 1
