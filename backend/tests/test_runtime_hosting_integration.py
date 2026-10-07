"""Public non-Agent hosting through graph settlement and persistent facts."""

from contextlib import closing
from threading import Event, Thread
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.capability_packages import (
    CapabilityPackage, PackageDependency, PackageManifest,
)
from phase1_agent.content_contracts import text_content
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, HostContractError, ObjectBinding
from phase1_agent.runtime_executor_contracts import ExecutorDefinition, ExecutorReference, PauseSupport
from phase1_agent.runtime_hosting import InvocationOwner

from test_graph_service import create, document, edge, node


REFERENCE = ExecutorReference("sample.hosted-work", "1.0.0")
OBJECT_KEY = "work/progress"
COMPONENT = "sample.hosted-work.node"


class ControlledWork:
    """Simulate one in-flight operation and a retained response, without a model."""

    def __init__(self, config, inputs, context, entered, proceed):
        self.config, self.inputs, self.context = config, inputs, context
        self.entered, self.proceed = entered, proceed
        self.disposes = 0
        self.dispatched = 0
        self.received = None
        self.callbacks = []
        self.staged = False

    def advance(self, callbacks, continuation):
        self.callbacks.append(callbacks)
        assert continuation is None or continuation is self
        if not self.staged:
            old = self.context.object_read(OBJECT_KEY)
            self.context.object_write(OBJECT_KEY, {"count": old["value"]["count"] + 1},
                                      expected_revision=old["revision"])
            self.staged = True
            callbacks.publish_fact("dispatch", {"phase": "dispatch"})
            self.dispatched += 1
            self.entered.set()
            assert self.proceed.wait(5), "offline work gate timed out"
            self.received = self.inputs["input"]
            callbacks.publish_fact("received", {"phase": "received"})
        callbacks.pause_point(self)
        callbacks.publish_fact("result", {"phase": "result"})
        return {"output": self.received}

    def dispose(self):
        self.disposes += 1


def hosted_package(entered, proceed, handles):
    def register(host):
        host.register_data_type(DataTypeDefinition(
            "sample.work-state", 1,
            {"type": "object", "required": ["count"], "additionalProperties": False,
             "properties": {"count": {"type": "integer", "minimum": 0}}}, {"count": 0}))

        def factory(config, inputs, context):
            handle = ControlledWork(config, inputs, context, entered, proceed)
            handles.append(handle)
            return handle

        host.register_executor(ExecutorDefinition(
            REFERENCE, continuation_mode="same_process",
            fact_schema={"type": "object", "required": ["phase"], "additionalProperties": False,
                         "properties": {"phase": {"enum": ["dispatch", "received", "result"]}}}), factory)
        host.register_pause_support(PauseSupport(REFERENCE), lambda handle, token: handle is token)
        host.register_node(NodeDefinition(
            COMPONENT, "1", "Offline work", "Sample", {"key": OBJECT_KEY},
            {"type": "object", "required": ["key"], "additionalProperties": False,
             "properties": {"key": {"const": OBJECT_KEY}}},
            inputs=(NodePort("input", "TEXT", data_schema_version=2),),
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
            capabilities=("objects:read", "objects:write"), input_storage="references",
            object_accesses=({"config_field": "key", "multiple": False, "access": "read_write",
                              "type_id": "sample.work-state", "schema_version": 1},)),
            None, executor_ref=REFERENCE)

    return CapabilityPackage(PackageManifest(
        "sample.hosted-work", "1.0.0",
        dependencies=(PackageDependency("workflow.content", "1.0.0"),),
        schema_version=2), register)


def graph(service):
    source = node(service.registry, "tools.text", 501, text="original input")
    work = node(service.registry, COMPONENT, 502)
    output = node(service.registry, "tools.output", 503)
    doc = document([source, work, output], [edge(source, work, 1), edge(work, output, 2)])
    doc.update(schema_version=2, object_bindings=[
        ObjectBinding(OBJECT_KEY, "sample.work-state", 1, "shared",
                      readers=(work["node_binding_id"],), writers=(work["node_binding_id"],)).to_dict()],
        package_lock=list(service.registry.package_lock))
    return doc


def start(service, initial):
    view = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                         idempotency_key=str(uuid4()))
    return view["active_chain_run_id"]


def pause(service, sid):
    view = service.get_session(sid)
    return service.control(sid, action="pause", expected_revision=view["revision"],
                           idempotency_key=str(uuid4()))


def resume(service, sid):
    view = service.get_session(sid)
    request = {"action": "resume", "expected_revision": view["revision"], "idempotency_key": str(uuid4())}
    first = service.control(sid, **request)
    assert service.control(sid, **request) == first
    return first


def test_public_hosted_graph_safe_point_defers_writes_facts_are_owned_and_resume_reuses_response(tmp_path):
    entered, proceed, handles = Event(), Event(), []
    packages = {**DEFAULT_PACKAGES, "sample.hosted-work": "1.0.0"}
    with closing(GraphWorkflowService(
            tmp_path / "hosted.sqlite", capability_packages=[hosted_package(entered, proceed, handles)],
            enabled_packages=packages)) as service:
        initial = create(service, graph(service))
        sid = initial["workflow_session_id"]
        chain_id = start(service, initial)
        try:
            assert entered.wait(5)
            host = service._runtime_hosts[chain_id]
            runtime_owner = InvocationOwner(sid, chain_id, handles[0].context.node_binding_id,
                                            handles[0].context.node_run_id)
            response = pause(service, sid)
            assert response["status"] == "running"
            consumer = service.get_consumer(sid)
            assert consumer["status"] == "pausing" and consumer["available_actions"] == []
            assert service.get_session(sid)["status"] == "running"
            assert host.snapshot(runtime_owner)["requested"] is True
            assert host.snapshot(runtime_owner)["status"] == "running"
            assert service.get_session(sid)["objects"][OBJECT_KEY]["value"] == {"count": 0}
        finally:
            proceed.set()
        service.wait(chain_id)
        paused = service.get_session(sid)
        assert paused["status"] == "paused" and paused["chains"][-1]["next_node_index"] == 1
        consumer = service.get_consumer(sid)
        assert consumer["status"] == "paused" and consumer["available_actions"] == ["resume", "close"]
        assert paused["objects"][OBJECT_KEY]["value"] == {"count": 0}
        assert paused["objects"][OBJECT_KEY]["revision"] == 1
        assert handles[0].dispatched == 1 and handles[0].received == text_content("original input")
        evidence = service.get_run(sid, chain_id)
        assert [fact["payload"]["phase"] for fact in evidence["runtime_facts"]] == ["dispatch", "received"]
        assert all(fact["owner"] == runtime_owner.to_dict() for fact in evidence["runtime_facts"])
        assert evidence["node_runs"][1]["output_refs"] == {}
        assert evidence["node_runs"][1]["input_values"] == {}
        input_ref = evidence["node_runs"][1]["input_refs"]["input"][0]["output_id"]
        assert input_ref == evidence["node_runs"][0]["output_refs"]["output"]
        assert host.active_handle_count == 1
        old_callbacks = handles[0].callbacks[0]
        resume(service, sid)
        service.wait(chain_id)
        final = service.get_session(sid)
        assert final["status"] == "succeeded"
        assert final["objects"][OBJECT_KEY]["value"] == {"count": 1}
        assert final["objects"][OBJECT_KEY]["revision"] == 2
        assert final["nodes"][-1]["outputs"]["output"] == text_content("original input")
        assert handles[0].dispatched == 1 and handles[0].disposes == 1
        assert host.active_handle_count == 0 and chain_id not in service._runtime_hosts
        completed = service.get_run(sid, chain_id)
        assert [fact["sequence"] for fact in completed["runtime_facts"]] == [1, 2, 3]
        assert [fact["generation"] for fact in completed["runtime_facts"]] == [1, 1, 2]
        assert completed["node_runs"][1]["input_refs"]["input"][0]["output_id"] == input_ref
        with pytest.raises(HostContractError) as caught:
            old_callbacks.publish_fact("late", {"phase": "result"})
        assert caught.value.reason_code == "runtime_stale_callback"
        assert len(service.get_run(sid, chain_id)["runtime_facts"]) == 3


def test_paused_run_does_not_pause_an_independent_session_and_close_releases_original_handle(tmp_path):
    entered, proceed, handles = Event(), Event(), []
    with closing(GraphWorkflowService(
            tmp_path / "isolation.sqlite", capability_packages=[hosted_package(entered, proceed, handles)],
            enabled_packages={**DEFAULT_PACKAGES, "sample.hosted-work": "1.0.0"})) as service:
        doc = graph(service)
        first = create(service, doc)
        sid, first_chain = first["workflow_session_id"], start(service, first)
        try:
            assert entered.wait(5)
            pause(service, sid)
        finally:
            proceed.set()
        service.wait(first_chain)
        assert service.get_session(sid)["status"] == "paused"
        original_host = service._runtime_hosts[first_chain]
        second = service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
        second_chain = start(service, second)
        service.wait(second_chain)
        assert service.get_session(second["workflow_session_id"])["status"] == "succeeded"
        assert service.get_session(sid)["status"] == "paused"
        assert handles[1].dispatched == 1 and handles[1].disposes == 1
        first_facts = service.get_run(sid, first_chain)["runtime_facts"]
        second_facts = service.get_run(second["workflow_session_id"], second_chain)["runtime_facts"]
        assert {fact["owner"]["chain_run_id"] for fact in first_facts} == {first_chain}
        assert {fact["owner"]["chain_run_id"] for fact in second_facts} == {second_chain}
        paused = service.get_session(sid)
        closed = service.control(sid, action="close", expected_revision=paused["revision"],
                                 idempotency_key=str(uuid4()))
        assert closed["status"] == "closed" and handles[0].disposes == 1
        assert closed["objects"][OBJECT_KEY]["value"] == {"count": 0}
        assert original_host.active_handle_count == 0 and first_chain not in service._runtime_hosts


def test_unregistered_ordinary_node_finishes_before_boundary_pause_and_is_not_reexecuted(tmp_path):
    entered, proceed, calls = Event(), Event(), []
    with closing(GraphWorkflowService(tmp_path / "ordinary.sqlite")) as service:
        def execute(config, inputs, context):
            calls.append("ordinary")
            entered.set()
            assert proceed.wait(5), "ordinary gate timed out"
            return {"output": inputs["input"]}

        service.registry.register(NodeDefinition(
            "sample.ordinary", "1", "Ordinary", "Sample", {}, {"type": "object"},
            inputs=(NodePort("input", "TEXT", data_schema_version=2),),
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
            input_storage="references"), execute)
        source = node(service.registry, "tools.text", 601, text="finished current call")
        ordinary = node(service.registry, "sample.ordinary", 602)
        output = node(service.registry, "tools.output", 603)
        initial = create(service, document(
            [source, ordinary, output], [edge(source, ordinary, 1), edge(ordinary, output, 2)]))
        sid, chain_id = initial["workflow_session_id"], start(service, initial)
        try:
            assert entered.wait(5)
            requested = pause(service, sid)
            assert requested["status"] == "running"
        finally:
            proceed.set()
        service.wait(chain_id)
        paused = service.get_session(sid)
        assert paused["status"] == "paused" and paused["chains"][-1]["next_node_index"] == 2
        records = service.get_run(sid, chain_id)["node_runs"]
        assert [record["status"] for record in records] == ["succeeded", "succeeded", "prepared"]
        assert service._runtime_hosts[chain_id].active_handle_count == 0
        resume(service, sid)
        service.wait(chain_id)
        assert service.get_session(sid)["status"] == "succeeded"
        assert calls == ["ordinary"]
        assert service.get_run(sid, chain_id)["runtime_facts"] == []


def test_pending_pause_on_last_node_does_not_invent_resumable_work(tmp_path):
    entered, proceed = Event(), Event()
    with closing(GraphWorkflowService(tmp_path / "last.sqlite")) as service:
        def execute(config, inputs, context):
            entered.set()
            assert proceed.wait(5)
            return {"output": text_content("done")}

        service.registry.register(NodeDefinition(
            "sample.last", "1", "Last", "Sample", {}, {"type": "object"}, is_output=True,
            outputs=(NodePort("output", "TEXT", data_schema_version=2),)), execute)
        initial = create(service, document([node(service.registry, "sample.last", 701)], []))
        sid, chain_id = initial["workflow_session_id"], start(service, initial)
        try:
            assert entered.wait(5)
            pause(service, sid)
        finally:
            proceed.set()
        service.wait(chain_id)
        final = service.get_session(sid)
        assert final["status"] == "succeeded" and final["active_chain_run_id"] is None
        assert final["chains"][-1]["next_node_index"] == 1


def test_service_shutdown_routes_pause_to_a_cooperative_active_handle_and_releases_it(tmp_path):
    from phase1_agent.capability_registry import create_package_registry

    entered, finished, tick = Event(), Event(), Event()
    handles = []
    reference = ExecutorReference("sample.shutdown", "1")
    registry = create_package_registry().registry.detached()

    class ShutdownHandle:
        def __init__(self):
            self.disposes = 0
            self.owner = None

        def advance(self, callbacks, continuation):
            self.owner = callbacks.owner
            entered.set()
            while True:
                callbacks.pause_point(self)
                tick.wait(0.02)

        def dispose(self):
            self.disposes += 1

    def factory(config, inputs, context):
        handle = ShutdownHandle()
        handles.append(handle)
        return handle

    registry.executors.register_executor(ExecutorDefinition(
        reference, continuation_mode="same_process"), factory)
    registry.executors.register_pause_support(PauseSupport(reference), lambda handle, token: handle is token)
    registry.register(NodeDefinition(
        "sample.shutdown.node", "1", "Shutdown", "Sample", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),), is_output=True),
        None, executor_ref=reference)
    service = GraphWorkflowService(tmp_path / "shutdown.sqlite", registry=registry)
    initial = create(service, document([node(service.registry, "sample.shutdown.node", 801)], []))
    chain_id = start(service, initial)
    assert entered.wait(5)
    host = service._runtime_hosts[chain_id]

    def close():
        try:
            service.close()
        finally:
            finished.set()

    worker = Thread(target=close)
    worker.start()
    notified = finished.wait(3)
    if not notified:
        # Ensure a failed regression test cannot leave its executor thread alive.
        host.request_pause(handles[0].owner, "test.cleanup")
        worker.join(5)
    else:
        worker.join(1)
    assert not worker.is_alive()
    assert notified, "Service shutdown did not notify its active hosted invocation"
    assert handles[0].disposes == 1 and host.active_handle_count == 0
