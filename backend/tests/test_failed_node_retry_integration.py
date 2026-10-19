"""Explicit failed-node retry through the public control entry and a real host."""

from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.capability_packages import (
    CapabilityPackage, PackageDependency, PackageManifest,
)
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.execution_data import ExecutionRecords
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.program_variable_store import ProgramVariableStore
from phase1_agent.runtime_executor_contracts import ExecutorDefinition, ExecutorReference

from test_graph_service import create, document, edge, node


REFERENCE = ExecutorReference("sample.failed-retry-executor", "1.0.0")
SOURCE = "sample.failed-retry.source"
TARGET = "sample.failed-retry.node"
SINK = "sample.failed-retry.sink"


class FailOnceHandle:
    """First hosted attempt fails, a fresh handle from the second call succeeds."""

    def __init__(self, state, calls, disposes, config, inputs):
        self.state, self.calls, self.disposes = state, calls, disposes
        self.config, self.inputs = config, inputs

    def advance(self, callbacks, continuation):
        self.calls["B"] += 1
        self.state["attempts"] += 1
        if self.state["attempts"] == 1:
            callbacks.publish_fact("attempt", {"phase": "attempt"})
            raise ValueError("controlled hosted failure")
        return {"output": text_content("retry: " + self.inputs["input"]["text"])}

    def dispose(self):
        self.disposes.append(self.state["attempts"])


def retry_package(calls, disposes, state, validator):
    def register(host):
        def factory(config, inputs, context):
            return FailOnceHandle(state, calls, disposes, config, inputs)

        host.register_executor(ExecutorDefinition(
            REFERENCE, continuation_mode="same_process",
            fact_schema={"type": "object", "required": ["phase"],
                         "additionalProperties": False,
                         "properties": {"phase": {"enum": ["attempt"]}}}), factory)

        host.register_node(NodeDefinition(
            SOURCE, "1", "Counted source", "Sample",
            {"text": "prefix"}, {"type": "object", "required": ["text"],
                                 "additionalProperties": False,
                                 "properties": {"text": {"type": "string"}}},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        ), source)

        host.register_node(NodeDefinition(
            TARGET, "1", "Controlled failing host", "Sample", {},
            {"type": "object", "additionalProperties": False},
            inputs=(NodePort("input", "TEXT", data_schema_version=2),),
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        ), None, executor_ref=REFERENCE, failed_retry_validator=validator)

        host.register_node(NodeDefinition(
            SINK, "1", "Counted sink", "Sample", {},
            {"type": "object", "additionalProperties": False},
            inputs=(NodePort("input", "TEXT", data_schema_version=2),),
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
            is_output=True,
        ), sink)

    def source(config, inputs, context):
        calls["A"] += 1
        return {"output": text_content(config["text"])}

    def sink(config, inputs, context):
        calls["C"] += 1
        return {"output": inputs["input"]}

    return CapabilityPackage(PackageManifest(
        "sample.failed-retry", "1.0.0",
        dependencies=(PackageDependency("workflow.content", "1.0.0"),),
        schema_version=2), register)


def retry_validator(config, evidence):
    run = evidence["run"]
    if (run["status"] != "failed"
            or (run.get("diagnostic") or {}).get("code") != "graph_node_failed"
            or config != {}):
        raise ValueError("retry policy does not cover this failure")
    return {"classification": "controlled-failed-invocation",
            "service_ref": None, "service_evidence": {}}


def graph(service):
    source = node(service.registry, SOURCE, 901)
    target = node(service.registry, TARGET, 902)
    sink = node(service.registry, SINK, 903)
    doc = document([source, target, sink], [
        edge(source, target, 1), edge(target, sink, 2)])
    doc["package_lock"] = list(service.registry.package_lock)
    return doc


def make_service(path, **options):
    return GraphWorkflowService(
        path, enabled_packages={**DEFAULT_PACKAGES, "sample.failed-retry": "1.0.0"},
        **options)


def test_public_retry_after_hosted_failure_completes_and_preserves_prefix(
        tmp_path, monkeypatch):
    calls = {"A": 0, "B": 0, "C": 0}
    disposes, state = [], {"attempts": 0}
    package = retry_package(calls, disposes, state, retry_validator)
    with closing(make_service(tmp_path / "retry.sqlite",
                              capability_packages=[package])) as service:
        view = create(service, graph(service))
        sid = view["workflow_session_id"]
        started = service.start(
            sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        failed = service.get_session(sid)
        assert failed["status"] == "failed", failed["chains"]
        assert failed["available_actions"] == ["retry_failed_node", "close"]
        assert calls == {"A": 1, "B": 1, "C": 0}

        before = service.get_run(sid, chain_id)
        assert [run["status"] for run in before["node_runs"]] == [
            "succeeded", "failed", "prepared"]
        assert [fact["payload"]["phase"] for fact in before["runtime_facts"]] == ["attempt"]
        old_fact = before["runtime_facts"][0]
        assert old_fact["owner"]["node_run_id"] == before["node_runs"][1]["run_id"]
        old_target = before["node_runs"][1]
        old_source = before["node_runs"][0]
        source_output_id = old_source["output_refs"]["output"]
        assert before["outputs"][0]["output_id"] == source_output_id

        ports = {}

        def track(name, method):
            def wrapper(*args, **kwargs):
                ports[name] = ports.get(name, 0) + 1
                return method(*args, **kwargs)
            return wrapper

        for name in ("put_node_run", "put_chain", "put_session"):
            monkeypatch.setattr(ExecutionRecords, name, track(
                name, getattr(ExecutionRecords, name)))

        response = service.control(
            sid, action="retry_failed_node", expected_revision=failed["revision"],
            idempotency_key="controlled-retry")
        assert response["status"] == "prepared", response
        service.wait(chain_id)
        final = service.get_session(sid)
        assert final["status"] == "succeeded", final["chains"]
        assert calls == {"A": 1, "B": 2, "C": 1}
        assert next(item["outputs"]["output"] for item in final["nodes"]
                    if item["node_binding_id"] == old_target["node_binding_id"]
                    ) == text_content("retry: prefix")
        assert ports.get("put_node_run", 0) > 0
        assert ports.get("put_chain", 0) > 0
        assert ports.get("put_session", 0) > 0

        after = service.get_run(sid, chain_id)
        runs = after["node_runs"]
        assert [run["status"] for run in runs] == [
            "succeeded", "failed", "succeeded", "succeeded"]
        assert runs[0]["run_id"] == old_source["run_id"]
        assert runs[0]["output_refs"]["output"] == source_output_id
        assert runs[2]["node_binding_id"] == old_target["node_binding_id"]
        assert runs[2]["run_id"] != old_target["run_id"]
        assert runs[2]["input_refs"]["input"][0]["output_id"] == source_output_id
        assert after["chain"]["node_run_ids"][1] == runs[2]["run_id"]
        assert after["chain"]["node_run_attempts"][1] == [
            old_target["run_id"], runs[2]["run_id"]]
        assert after["outputs"][0]["output_id"] == source_output_id
        assert after["runtime_facts"] == [old_fact]
        assert after["state_manifests"]["start"]["status"] == "prepared"
        assert after["state_manifests"]["end"]["status"] == "succeeded"
        assert disposes == [1, 2]
        assert chain_id not in service._runtime_hosts
        assert not service._failed_retry_candidates

        replay = service.control(
            sid, action="retry_failed_node", expected_revision=failed["revision"],
            idempotency_key="controlled-retry")
        assert replay == response
        assert calls == {"A": 1, "B": 2, "C": 1}

        with service._data_management.read_execution(registry=service.registry) as data:
            stored_session = data.records.session(sid)
            chain = data.records.chain(chain_id)
            stored_old = data.records.node_run(old_target["run_id"])
            stored_new = data.records.node_run(runs[2]["run_id"])
            outputs = data.records.outputs_for_chain(chain_id)
            head = data.records.head(sid)
            commit = data.records.commit(head["head_commit_id"])
            checkpoint = data.settlement.latest_checkpoint(stored_session)
            end = data.settlement.manifest("chain_end", chain_id)
        assert stored_session["active_chain_run_id"] is None
        assert chain["status"] == "succeeded"
        assert chain["node_run_attempts"][1] == [
            old_target["run_id"], runs[2]["run_id"]]
        assert stored_old["status"] == "failed"
        assert stored_new["status"] == "succeeded"
        assert stored_new["config"] == old_target["config"]
        assert {row["output_id"] for row in outputs} >= {source_output_id}
        assert commit["source"] == {
            "kind": "completed_execution", "chain_run_id": chain_id}
        assert checkpoint is not None
        assert end["status"] == "succeeded"


def test_retry_without_registered_policy_is_not_offered_or_dispatched(tmp_path):
    calls = {"A": 0, "B": 0, "C": 0}
    disposes, state = [], {"attempts": 0}
    package = retry_package(calls, disposes, state, None)
    with closing(make_service(tmp_path / "no-policy.sqlite",
                              capability_packages=[package])) as service:
        view = create(service, graph(service))
        sid = view["workflow_session_id"]
        started = service.start(
            sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        failed = service.get_session(sid)
        assert failed["status"] == "failed", failed["chains"]
        assert failed["available_actions"] == ["close"]
        before = service.get_run(sid, chain_id)
        assert len(before["node_runs"]) == 3
        with pytest.raises(ContractValidationError) as caught:
            service.control(
                sid, action="retry_failed_node",
                expected_revision=failed["revision"],
                idempotency_key=str(uuid4()))
        assert caught.value.reason_code == "failed_retry_unsupported"
        assert calls == {"A": 1, "B": 1, "C": 0}
        after = service.get_run(sid, chain_id)
        assert [run["run_id"] for run in after["node_runs"]] == [
            run["run_id"] for run in before["node_runs"]]
        assert chain_id not in service._runtime_hosts


def test_retry_without_retained_run_basis_is_rejected_without_a_new_handle(tmp_path):
    calls = {"A": 0, "B": 0, "C": 0}
    disposes, state = [], {"attempts": 0}
    package = retry_package(calls, disposes, state, retry_validator)
    with closing(make_service(tmp_path / "no-basis.sqlite",
                              capability_packages=[package])) as service:
        view = create(service, graph(service))
        sid = view["workflow_session_id"]
        started = service.start(
            sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        failed = service.get_session(sid)
        assert failed["status"] == "failed"
        assert "retry_failed_node" in failed["available_actions"]
        service._execution_registries.pop(chain_id)
        with pytest.raises(ContractValidationError) as caught:
            service.control(
                sid, action="retry_failed_node",
                expected_revision=failed["revision"],
                idempotency_key=str(uuid4()))
        assert caught.value.reason_code == "failed_retry_unavailable"
        assert calls == {"A": 1, "B": 1, "C": 0}
        stored = service.get_run(sid, chain_id)
        assert [run["status"] for run in stored["node_runs"]] == [
            "succeeded", "failed", "prepared"]


def test_retry_rejects_changed_state_basis_without_a_new_handle(tmp_path):
    calls = {"A": 0, "B": 0, "C": 0}
    disposes, state = [], {"attempts": 0}
    package = retry_package(calls, disposes, state, retry_validator)
    with closing(make_service(tmp_path / "basis-changed.sqlite",
                              capability_packages=[package])) as service:
        view = create(service, graph(service))
        sid = view["workflow_session_id"]
        started = service.start(
            sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        failed = service.get_session(sid)
        assert failed["status"] == "failed"

        def mutate(repo):
            variables = ProgramVariableStore(repo.store)
            current = variables.current(sid)
            state = {"revision": current["revision"], "values": {
                **current["values"],
                "probe": {"type": "string", "source": "assignment",
                          "value": "changed"}}}
            variables.write_in_transaction(
                sid, state, expected_revision=current["revision"],
                key="test.failed-retry.basis", request={})
        service._change("test.failed-retry.basis", "basis-change", {}, mutate)
        assert service.get_session(sid)["data"]["values"]["probe"]["value"] == "changed"

        with pytest.raises(ContractValidationError) as caught:
            service.control(
                sid, action="retry_failed_node",
                expected_revision=failed["revision"],
                idempotency_key=str(uuid4()))
        assert caught.value.reason_code == "failed_retry_basis_changed"
        assert calls == {"A": 1, "B": 1, "C": 0}
        stored = service.get_run(sid, chain_id)
        assert [run["status"] for run in stored["node_runs"]] == [
            "succeeded", "failed", "prepared"]


def test_retry_precheck_reads_with_the_retained_run_basis(tmp_path, monkeypatch):
    calls = {"A": 0, "B": 0, "C": 0}
    disposes, state = [], {"attempts": 0}
    package = retry_package(calls, disposes, state, retry_validator)
    with closing(make_service(tmp_path / "precheck-basis.sqlite",
                              capability_packages=[package])) as service:
        view = create(service, graph(service))
        sid = view["workflow_session_id"]
        started = service.start(
            sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        failed = service.get_session(sid)
        basis = service._execution_registries[chain_id]
        seen = []
        original = service._data_management.read_execution

        def tracked(*args, registry=None, **kwargs):
            seen.append(registry)
            return original(*args, registry=registry, **kwargs)

        monkeypatch.setattr(service._data_management, "read_execution", tracked)
        service.control(
            sid, action="retry_failed_node",
            expected_revision=failed["revision"],
            idempotency_key=str(uuid4()))
        service.wait(chain_id)
        assert service.get_session(sid)["status"] == "succeeded"
        assert seen and seen[0] is service.registry
        assert seen[1] is basis.registry


def test_capture_uses_persisted_invocation_config_for_the_policy(tmp_path):
    received = []
    calls = {"A": 0, "B": 0, "C": 0}
    disposes, state = [], {"attempts": 0}

    def capture_validator(config, evidence):
        received.append(deepcopy(config))
        if (evidence["run"].get("diagnostic") or {}).get("code") != "graph_node_failed":
            raise ValueError("not the controlled failure boundary")
        return {"classification": "controlled-failed-invocation",
                "service_ref": None, "service_evidence": {}}

    package = retry_package(calls, disposes, state, capture_validator)
    with closing(make_service(tmp_path / "capture-config.sqlite",
                              capability_packages=[package])) as service:
        view = create(service, graph(service))
        sid = view["workflow_session_id"]
        started = service.start(
            sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        assert service.get_session(sid)["status"] == "failed"
        assert received == [{}]

        with service._data_management.read_execution(registry=service.registry) as data:
            chain = data.records.chain(chain_id)
            run_id = chain["node_run_ids"][chain["next_node_index"]]
            real = data.records.node_run(run_id)
            document = data.configuration.fixed_document(
                chain["workflow_definition_id"], chain["definition_revision"])

            class Records:
                def node_run(self, identity):
                    value = data.records.node_run(identity)
                    if identity == run_id:
                        value = {**value, "config": {"label": "persisted"}}
                    return value

                def __getattr__(self, name):
                    return getattr(data.records, name)

            class Configuration:
                def fixed_document(self, *args, **kwargs):
                    return document

                def __getattr__(self, name):
                    return getattr(data.configuration, name)

            wrapped = SimpleNamespace(
                records=Records(), state=data.state,
                configuration=Configuration(), registry=data.registry)
            service._resource_frames[chain_id] = {real["node_binding_id"]: []}
            service._runtime_hosts[chain_id] = SimpleNamespace(
                contains=lambda owner: True,
                snapshot=lambda owner: {"status": "failed", "disposed": True,
                                        "disposal_error": None},
                release=lambda owner: None,
                active_handle_count=0,
            )
            captured = service._capture_failed_retry(wrapped, chain)
        assert captured["allowed"] is True
        assert received[-1] == {"label": "persisted"}
