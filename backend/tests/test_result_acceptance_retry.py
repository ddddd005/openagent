"""Exact same-process result reuse; no model/tool business reexecution."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_records import graph_error
from phase1_agent.capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from phase1_agent.content_contracts import text_content
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.host_sdk import ServiceDefinition, ServiceOperation, ServiceReference

from test_model_package import uid
from test_models_service_integration import ModelDatabaseFixture
from workflow_test_support import AgentTransportFixture, graph
from test_graph_service import create
from test_graph_service import document, node


def execute(service, fixture):
    initial = fixture.create(service, fixture.document(service))
    started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                            idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    return service.get_session(initial["workflow_session_id"]), started["active_chain_run_id"]


def retry(service, view, key=None):
    result = service.control(view["workflow_session_id"], action="retry_acceptance",
                             expected_revision=view["revision"], idempotency_key=key or str(uuid4()))
    service.wait(result["active_chain_run_id"])
    return service.get_session(view["workflow_session_id"])


@pytest.mark.parametrize("failure", ["outcome", "output"])
def test_chat_acceptance_failure_reuses_response_and_exact_node_identity(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-secret")
    fixture = ModelDatabaseFixture()
    with closing(GraphWorkflowService(tmp_path / "chat.sqlite", public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        if failure == "outcome":
            original = service._accept_runtime_fact
            failures = []

            def fail(owner, fact, stream_id):
                if fact.get("stage") == "outcome" and not failures:
                    failures.append(True)
                    raise OSError("outcome store unavailable")
                return original(owner, fact, stream_id)

            service._accept_runtime_fact = fail
        else:
            original = service._node_event
            failures = []

            def fail(sid, chain, event):
                if event["node_binding_id"] == uid(13) and event["event"] == "succeeded" and not failures:
                    failures.append(True)
                    raise OSError("output store unavailable")
                return original(sid, chain, event)

            service._node_event = fail
        pending, chain_id = execute(service, fixture)
        assert pending["status"] == "archive_failed", pending
        assert pending["available_actions"] == ["retry_acceptance", "close"]
        prior = service.get_run(pending["workflow_session_id"], chain_id)
        final = retry(service, pending)
        assert final["status"] == "succeeded", final
        after = service.get_run(final["workflow_session_id"], chain_id)
        assert [run["run_id"] for run in prior["node_runs"]] == [run["run_id"] for run in after["node_runs"]]
        assert len(fixture.calls) == 1 and fixture.closes == 1
        assert [fact["stage"] for fact in after["runtime_facts"]] == ["request", "attempt", "outcome"]
        assert fixture.result(final)["content"] == "https://original.test"
        assert not service._acceptance_candidates


def test_unknown_result_is_not_offered_acceptance_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-secret")
    fixture = ModelDatabaseFixture()
    fixture.fail = True
    with closing(GraphWorkflowService(tmp_path / "unknown.sqlite", public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        failed, _ = execute(service, fixture)
        assert failed["status"] == "failed" and failed["available_actions"] == ["close"]
        with pytest.raises(ContractValidationError):
            retry(service, failed)
        assert len(fixture.calls) == 1


def test_lost_process_candidate_refuses_retry_and_close_releases_retained_result(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-secret")
    fixture = ModelDatabaseFixture()
    path = tmp_path / "lost.sqlite"
    with closing(GraphWorkflowService(path, public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        original = service._node_event

        def fail(sid, chain, event):
            if event["node_binding_id"] == uid(13) and event["event"] == "succeeded":
                raise OSError("output store unavailable")
            return original(sid, chain, event)

        service._node_event = fail
        pending, _ = execute(service, fixture)
        assert pending["status"] == "archive_failed"
    assert fixture.closes == 1
    with closing(GraphWorkflowService(path, public_model_factory=fixture.factory)) as reopened:
        lost = reopened.get_session(pending["workflow_session_id"])
        assert lost["status"] == "recovery_unavailable"
        assert lost["available_actions"] == ["close"]
        with pytest.raises(ContractValidationError):
            retry(reopened, lost)
        assert len(fixture.calls) == 1


@pytest.mark.parametrize("failure", ["terminal_fact", "terminal_fact_committed", "output"])
def test_agent_completed_result_retry_does_not_repeat_model_or_tools(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-secret")
    fixture = AgentTransportFixture(batch=True)
    with closing(GraphWorkflowService(tmp_path / "agent.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        initial = create(service, graph(service))
        failures = []
        if failure.startswith("terminal_fact"):
            original = service._accept_runtime_fact

            def fail(owner, fact, stream_id):
                if fact.get("payload", {}).get("kind") == "agent_result" and len(failures) < 2:
                    failures.append(deepcopy(fact))
                    if failure == "terminal_fact_committed":
                        original(owner, fact, stream_id)
                    raise OSError("terminal fact acknowledgment unavailable")
                return original(owner, fact, stream_id)

            service._accept_runtime_fact = fail
        else:
            original = service._node_event

            def fail(sid, chain, event):
                if event["event"] == "succeeded" and "context" in event["outputs"] and not failures:
                    failures.append(True)
                    raise OSError("agent output commit unavailable")
                return original(sid, chain, event)

            service._node_event = fail
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()), inputs={"text": "question"})
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        pending = service.get_session(initial["workflow_session_id"])
        assert pending["status"] == "archive_failed", pending["chains"]
        assert pending["available_actions"] == ["retry_acceptance", "close"]
        if failure.startswith("terminal_fact"):
            pending = retry(service, pending)
            assert pending["status"] == "archive_failed"
            assert failures[0] == failures[1]
        final = retry(service, pending)
        assert final["status"] == "succeeded", final["chains"]
        assert len(fixture.calls) == 2 and fixture.closes == 1
        facts = service.get_run(final["workflow_session_id"], chain_id)["runtime_facts"]
        assert sum(fact.get("payload", {}).get("kind") == "agent_result" for fact in facts) == 1
        assert sum(fact.get("payload", {}).get("kind") == "tool_dispatch" for fact in facts) == 3
        assert final["objects"]["context"]["revision"] == 2
        assert not service._acceptance_candidates and not service._runtime_hosts and not service._service_runs


def test_retry_requires_original_state_fence_and_close_discards_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-secret")
    fixture = ModelDatabaseFixture()
    with closing(GraphWorkflowService(tmp_path / "fence.sqlite", public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        original = service._node_event

        def fail(sid, chain, event):
            if event["node_binding_id"] == uid(13) and event["event"] == "succeeded":
                raise OSError("output unavailable")
            return original(sid, chain, event)

        service._node_event = fail
        pending, chain_id = execute(service, fixture)
        candidates = service._acceptance_candidates[chain_id]
        saved_basis = deepcopy(next(iter(candidates.values()))["basis"])
        next(iter(candidates.values()))["basis"]["state"]["revision"] += 1
        with pytest.raises(ContractValidationError) as caught:
            retry(service, pending)
        assert caught.value.reason_code == "graph_acceptance_basis_changed"
        assert service.get_session(pending["workflow_session_id"]) == pending
        next(iter(candidates.values()))["basis"] = saved_basis
        consumer = service.get_consumer(pending["workflow_session_id"])
        assert consumer["available_actions"] == ["retry_acceptance", "close"]
        closed = service.control(pending["workflow_session_id"], action="close",
                                 expected_revision=pending["revision"], idempotency_key=str(uuid4()))
        assert closed["status"] == "closed"
        assert not service._acceptance_candidates and not service._service_runs
        assert len(fixture.calls) == 1 and fixture.closes == 1


@pytest.mark.parametrize("failure", ["protocol", "capacity"])
def test_actual_model_fact_validation_error_is_not_a_retry_boundary(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-secret")
    fixture = ModelDatabaseFixture()
    with closing(GraphWorkflowService(tmp_path / "invalid.sqlite", public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        original = service._accept_runtime_fact

        def reject(owner, fact, stream_id):
            if fact.get("stage") == "outcome":
                raise graph_error("runtime_fact_too_large" if failure == "capacity" else "runtime_fact_conflict",
                                  "Actual persistence contract rejection")
            return original(owner, fact, stream_id)

        service._accept_runtime_fact = reject
        failed, _ = execute(service, fixture)
        assert failed["status"] == "failed" and failed["available_actions"] == ["close"]
        assert not service._acceptance_candidates and not service._service_runs
        assert len(fixture.calls) == 1


def test_public_http_retry_action_is_idempotent_and_state_revision_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-secret")
    fixture = ModelDatabaseFixture()
    with closing(GraphWorkflowService(tmp_path / "http.sqlite", public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        original = service._node_event
        failed_once = []

        def fail(sid, chain, event):
            if event["node_binding_id"] == uid(13) and event["event"] == "succeeded" and not failed_once:
                failed_once.append(True)
                raise OSError("output unavailable")
            return original(sid, chain, event)

        service._node_event = fail
        pending, chain_id = execute(service, fixture)
        path = "/api/graph/sessions/" + pending["workflow_session_id"] + "/consumer/control"
        request = {"action": "retry_acceptance", "expected_revision": pending["revision"],
                   "idempotency_key": str(uuid4())}
        status, response = dispatch_graph(service, "POST", path, request)
        assert status == 200
        service.wait(chain_id)
        repeated_status, repeated = dispatch_graph(service, "POST", path, request)
        assert repeated_status == status and repeated["receipt"] == response["receipt"]
        final = service.get_consumer(pending["workflow_session_id"])
        assert final["status"] == "succeeded" and not final["available_actions"]
        with pytest.raises(ContractValidationError):
            dispatch_graph(service, "POST", path, {**request, "idempotency_key": str(uuid4())})
        assert len(fixture.calls) == 1


@pytest.mark.parametrize("failure", ["hook", "lost_ack"])
def test_committed_result_notification_failure_keeps_accepted_outputs_and_cannot_replay(
        tmp_path, monkeypatch, failure):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-secret")
    fixture = ModelDatabaseFixture()
    with closing(GraphWorkflowService(tmp_path / "committed.sqlite", public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        if failure == "hook":
            original = service._accept_public_outputs

            def injected(sid, chain_id, event):
                original(sid, chain_id, event)
                if event["node_binding_id"] == uid(13) and event["event"] == "succeeded":
                    raise OSError("capability notification unavailable after commit")

            service._accept_public_outputs = injected
        else:
            original = service._node_event

            def injected(sid, chain_id, event):
                original(sid, chain_id, event)
                if event["node_binding_id"] == uid(13) and event["event"] == "succeeded":
                    raise OSError("local acknowledgment lost after actual commit")

            service._node_event = injected
        initial = fixture.create(service, fixture.document(service))
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        with pytest.raises(ContractValidationError) as caught:
            service.wait(started["active_chain_run_id"])
        assert caught.value.reason_code == "result_committed_notification_failed"
        failed = service.get_session(initial["workflow_session_id"])
        assert failed["status"] == "recovery_unavailable" and failed["available_actions"] == ["close"]
        chain = failed["chains"][-1]
        assert chain["diagnostic"]["reason_code"] == "result_committed_notification_failed"
        chat = next(node for node in failed["nodes"] if node["node_binding_id"] == uid(13))
        assert chat["status"] == "succeeded" and chat["outputs"]["output"]["content"] == "https://original.test"
        assert chain["next_node_index"] == len(chain["ordered_nodes"])
        assert not service._acceptance_candidates and not service._service_runs
        with pytest.raises(ContractValidationError):
            retry(service, failed)
        assert len(fixture.calls) == 1 and fixture.closes == 1


def test_two_declared_services_with_pending_results_refuse_ambiguous_node_recovery(tmp_path):
    """The first retained service response cannot impersonate the whole node result."""
    instances, executions = [], []
    references = [ServiceReference("sample.result." + suffix, "1") for suffix in ("a", "b")]
    capabilities = ["sample:a", "sample:b"]

    class PendingService:
        def __init__(self, label):
            self.label, self.owner = label, None
            self.dispatches = self.recoveries = self.releases = 0

        def __call__(self, context, capability, operation, payload):
            self.owner = (context.chain_run_id, context.node_run_id)
            self.dispatches += 1
            raise OSError("Result received but local acceptance unavailable")

        def has_pending_acceptance(self, context):
            return self.owner == (context.chain_run_id, context.node_run_id)

        def recover_acceptance(self, context):
            self.recoveries += 1
            return {"output": text_content(self.label)}

        def release_run(self, workflow_session_id, chain_run_id):
            self.releases += 1

    def register(host):
        for reference, capability in zip(references, capabilities):
            def factory(environment, label=reference.service_id):
                service = PendingService(label)
                instances.append(service)
                return service
            host.register_service(ServiceDefinition(reference, (ServiceOperation(capability, "complete"),)), factory)

        def execute(config, inputs, context):
            executions.append(context.node_run_id)
            try:
                context.host_call(capabilities[0], "complete", {})
            except OSError:
                pass
            context.host_call(capabilities[1], "complete", {})
            raise AssertionError("Both results require explicit local acceptance")

        host.register_node(NodeDefinition(
            "sample.two-results", "1", "Two pending services", "Sample", {},
            {"type": "object", "additionalProperties": False},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),), is_output=True,
            capabilities=tuple(capabilities), service_requirements=tuple({
                **reference.to_dict(), "capability": capability, "operations": ["complete"]}
                for reference, capability in zip(references, capabilities))), execute)

    package = CapabilityPackage(PackageManifest(
        "sample.two-results", "1", dependencies=(PackageDependency("workflow.content", "1.0.0"),),
        schema_version=3, exports={"services": [reference.to_dict() for reference in references],
                                  "nodes": [{"component_id": "sample.two-results", "component_version": "1"}]}),
        register)
    with closing(GraphWorkflowService(tmp_path / "ambiguous.sqlite", capability_packages=(package,),
            enabled_packages={"sample.two-results": "1"})) as service:
        work = node(service.registry, "sample.two-results", 911)
        initial = create(service, document([work], []))
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        with pytest.raises(ContractValidationError) as caught:
            service.wait(started["active_chain_run_id"])
        assert caught.value.reason_code == "host_result_recovery_ambiguous"
        failed = service.get_session(initial["workflow_session_id"])
        assert failed["status"] == "recovery_unavailable" and failed["available_actions"] == ["close"]
        assert failed["chains"][-1]["diagnostic"]["reason_code"] == "host_result_recovery_ambiguous"
        assert failed["nodes"][0]["outputs"] == {}
        assert len(executions) == 1
        assert len(instances) == 2 and all(instance.dispatches == instance.releases == 1 for instance in instances)
        assert all(instance.recoveries == 0 for instance in instances)
        with pytest.raises(ContractValidationError):
            retry(service, failed)
        assert len(executions) == 1 and all(instance.dispatches == 1 for instance in instances)
        assert not service._acceptance_candidates and not service._service_runs
