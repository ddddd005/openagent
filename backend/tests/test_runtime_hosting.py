"""Offline evidence for the public host; no Agent implementation is involved."""

import threading
from uuid import uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackage, CapabilityPackageLoader, PackageManifest
from phase1_agent.graph_contracts import NodeDefinition, NodeRegistry
from phase1_agent.host_sdk import HostContractError
from phase1_agent.runtime_executor_contracts import (
    ExecutorDefinition, ExecutorReference, ExecutorRegistry, PauseSupport,
)
from phase1_agent.runtime_hosting import InvocationOwner, RuntimeHost


REFERENCE = ExecutorReference("sample.batch", "1.0.0")


def owner():
    return InvocationOwner(*(str(uuid4()) for _ in range(4)))


class BatchHandle:
    """Retain an already-accepted element rather than restarting a batch."""

    def __init__(self, config, inputs, context):
        self.inputs = inputs
        self.context = context
        self.limit = config.get("limit", 2)
        self.cursor = 0
        self.disposes = 0
        self.callbacks = []
        self.actions = []

    def advance(self, callbacks, continuation):
        self.callbacks.append(callbacks)
        if continuation is not None:
            assert continuation is self
        while self.cursor < self.limit:
            callbacks.pause_point(self)
            callbacks.publish_fact(f"element.{self.cursor}", {"element": self.cursor})
            self.actions.append(self.cursor)
            self.cursor += 1
            callbacks.report_progress({"completed": self.cursor})
        return {"total": self.cursor, "input": self.inputs["input"]}

    def dispose(self):
        self.disposes += 1


def registry(factory=BatchHandle, *, pause=True, mode="same_process"):
    result = ExecutorRegistry()
    result.register_executor(ExecutorDefinition(
        REFERENCE, continuation_mode=mode,
        fact_schema={"type": "object", "properties": {"element": {"type": "integer"}},
                     "required": ["element"], "additionalProperties": False}), factory)
    if pause:
        result.register_pause_support(PauseSupport(REFERENCE), lambda handle, token: token is handle)
    return result


def test_independent_long_executor_safe_point_resume_and_disposal_with_owned_facts_and_result():
    facts, results, handles = [], [], []
    context = object()
    invocation = owner()

    def factory(config, inputs, current):
        handle = BatchHandle(config, inputs, current)
        handles.append(handle)
        return handle

    def accept_fact(envelope):
        facts.append(envelope)
        if envelope["sequence"] == 1:
            host.request_pause(invocation, "pause.after.first")
        return {"fact_receipt": str(uuid4())}

    def accept_result(envelope):
        results.append(envelope)
        return {"result_receipt": "accepted"}

    host = RuntimeHost(registry(factory), fact_sink=accept_fact, result_sink=accept_result)
    inputs = {"input": {"text": "frozen"}}
    host.start(invocation, REFERENCE, {"limit": 3}, inputs, context)
    inputs["input"]["text"] = "edited"
    outcome = host.drive(invocation)
    assert outcome.status == "paused"
    assert handles[0].cursor == 1 and handles[0].actions == [0]
    assert host.snapshot(invocation)["status"] == "paused"
    assert host.active_handle_count == 1
    assert host.context(invocation) is context
    assert results == []
    first = host.resume(invocation, "resume.once")
    assert host.resume(invocation, "resume.once") == first
    assert first["generation"] == 2
    outcome = host.drive(invocation)
    assert outcome.status == "succeeded"
    assert outcome.outputs == {"total": 3, "input": {"text": "frozen"}}
    assert handles[0].actions == [0, 1, 2] and handles[0].disposes == 1
    assert [fact["sequence"] for fact in facts] == [1, 2, 3]
    assert [fact["generation"] for fact in facts] == [1, 2, 2]
    assert all(fact["owner"] == invocation.to_dict() for fact in facts)
    assert results[0]["owner"] == invocation.to_dict()
    assert len(results) == 1 and host.active_handle_count == 0
    assert host.drive(invocation) == outcome
    assert len(results) == 1
    host.release(invocation)
    assert handles[0].disposes == 1 and host.context(invocation) is None


def test_pause_request_alone_is_not_acknowledgment_and_unregistered_handle_completes():
    invocation = owner()
    host = RuntimeHost(registry(pause=False, mode="none"), fact_sink=lambda fact: fact["sequence"])
    host.start(invocation, REFERENCE, {}, {"input": "value"}, None)
    snapshot = host.request_pause(invocation, "pause")
    assert snapshot["status"] == "ready" and snapshot["supported_controls"] == []
    with pytest.raises(HostContractError) as caught:
        host.resume(invocation, "resume")
    assert caught.value.reason_code == "runtime_not_paused"
    assert host.drive(invocation).status == "succeeded"


def test_other_runs_do_not_share_control_and_old_callbacks_are_fenced():
    first, second = owner(), owner()
    handles = []

    def factory(config, inputs, context):
        handle = BatchHandle(config, inputs, context)
        handles.append(handle)
        return handle

    host = RuntimeHost(registry(factory), fact_sink=lambda fact: fact["sequence"])
    for invocation in (first, second):
        host.start(invocation, REFERENCE, {}, {"input": "same"}, None)
    host.request_pause(first, "pause")
    assert host.drive(first).status == "paused"
    old = handles[0].callbacks[0]
    assert host.drive(second).status == "succeeded"
    host.resume(first, "resume")
    with pytest.raises(HostContractError) as caught:
        old.publish_fact("late", {"element": 99})
    assert caught.value.reason_code == "runtime_stale_callback"
    assert host.drive(first).status == "succeeded"
    with pytest.raises(HostContractError):
        handles[0].callbacks[-1].report_progress({"late": True})
    assert host.active_handle_count == 0


def test_fact_acceptance_failure_blocks_action_and_disposes_handle():
    handles = []

    def factory(config, inputs, context):
        handle = BatchHandle(config, inputs, context)
        handles.append(handle)
        return handle

    def failing_sink(envelope):
        raise OSError("fact store unavailable")

    host = RuntimeHost(registry(factory), fact_sink=failing_sink)
    invocation = owner()
    host.start(invocation, REFERENCE, {}, {"input": 1}, None)
    with pytest.raises(OSError, match="unavailable"):
        host.drive(invocation)
    assert handles[0].actions == [] and handles[0].disposes == 1
    assert host.snapshot(invocation)["status"] == "failed"
    assert host.snapshot(invocation)["fact_sequence"] == 0
    assert host.active_handle_count == 0


def test_result_sink_failure_retains_candidate_and_only_retries_acceptance():
    results, handles = [], []

    def factory(config, inputs, context):
        handle = BatchHandle(config, inputs, context)
        handles.append(handle)
        return handle

    def sink(result):
        results.append(result)
        if len(results) < 3:
            raise OSError("result unavailable")
        return {"receipt": "accepted"}

    host = RuntimeHost(registry(factory), fact_sink=lambda fact: fact["sequence"], result_sink=sink)
    invocation = owner()
    host.start(invocation, REFERENCE, {}, {"input": 1}, None)
    with pytest.raises(OSError, match="result unavailable"):
        host.drive(invocation)
    assert host.snapshot(invocation)["status"] == "acceptance_pending"
    assert host.active_handle_count == 1
    callback = handles[0].callbacks[-1]
    with pytest.raises(HostContractError, match="active generation"):
        callback.publish_fact("late", {"element": 10})
    with pytest.raises(OSError):
        host.retry_acceptance(invocation)
    outcome = host.retry_acceptance(invocation)
    assert outcome.status == "succeeded" and outcome.receipt == {"receipt": "accepted"}
    assert handles[0].actions == [0, 1] and len(handles[0].callbacks) == 1
    assert len(results) == 3 and host.active_handle_count == 0
    assert results[0] == results[1] == results[2]
    with pytest.raises(HostContractError):
        host.retry_acceptance(invocation)


def test_fact_duplicate_receipt_is_stable_and_conflicting_payload_is_rejected():
    receipts = []

    class ReportingHandle(BatchHandle):
        def advance(self, callbacks, continuation):
            first = callbacks.publish_fact("first", {"element": 1})
            assert callbacks.publish_fact("first", {"element": 1}) == first
            with pytest.raises(HostContractError) as caught:
                callbacks.publish_fact("first", {"element": 2})
            assert caught.value.reason_code == "runtime_fact_conflict"
            with pytest.raises(HostContractError) as caught:
                callbacks.publish_fact("wrong.schema", {"element": "one"})
            assert caught.value.reason_code == "runtime_invalid_fact"
            return {}

    def sink(envelope):
        receipts.append(envelope)
        return {"receipt": "one"}

    host = RuntimeHost(registry(ReportingHandle), fact_sink=sink)
    invocation = owner()
    host.start(invocation, REFERENCE, {}, {}, None)
    assert host.drive(invocation).status == "succeeded"
    assert len(receipts) == 1 and host.snapshot(invocation)["fact_sequence"] == 1
    # Only a digest and a receipt remain in the host, not a second mutable fact payload.
    assert len(host._invocations[invocation].facts["first"][0]) == 32


def test_owner_mismatch_reused_node_run_and_terminal_control_are_rejected():
    first = owner()
    other = InvocationOwner(str(uuid4()), str(uuid4()), first.node_binding_id, first.node_run_id)
    host = RuntimeHost(registry(), fact_sink=lambda fact: fact["sequence"])
    host.start(first, REFERENCE, {}, {"input": 1}, None)
    with pytest.raises(HostContractError, match="another owner"):
        host.start(other, REFERENCE, {}, {"input": 1}, None)
    with pytest.raises(HostContractError) as caught:
        host.request_pause(other)
    assert caught.value.reason_code == "runtime_owner_mismatch"
    host.drive(first)
    with pytest.raises(HostContractError) as caught:
        host.request_pause(first)
    assert caught.value.reason_code == "runtime_terminal_invocation"


def test_repeated_resume_cannot_resume_a_new_pause_or_reuse_pause_command_identity():
    invocation = owner()
    host = RuntimeHost(registry(), fact_sink=lambda fact: fact["sequence"])
    host.start(invocation, REFERENCE, {}, {"input": 1}, None)
    host.request_pause(invocation, "first.pause")
    host.drive(invocation)
    first = host.resume(invocation, "first.resume")
    host.request_pause(invocation, "second.pause")
    assert host.drive(invocation).status == "paused"
    assert host.resume(invocation, "first.resume") == first
    assert host.snapshot(invocation)["status"] == "paused"
    with pytest.raises(HostContractError) as caught:
        host.resume(invocation, "second.pause")
    assert caught.value.reason_code == "runtime_command_conflict"
    host.resume(invocation, "second.resume")
    assert host.drive(invocation).status == "succeeded"


def test_pause_adapter_rejects_invalid_retained_token_without_false_acknowledgment():
    class InvalidHandle(BatchHandle):
        def advance(self, callbacks, continuation):
            callbacks.pause_point(object())
            return {}

    host = RuntimeHost(registry(InvalidHandle))
    invocation = owner()
    host.start(invocation, REFERENCE, {}, {}, None)
    host.request_pause(invocation)
    with pytest.raises(HostContractError) as caught:
        host.drive(invocation)
    assert caught.value.reason_code == "runtime_invalid_safe_point"
    assert host.snapshot(invocation)["status"] == "failed" and host.active_handle_count == 0


def test_release_running_handle_waits_for_natural_boundary_without_killing_thread():
    entered, proceed = threading.Event(), threading.Event()
    handles, outcomes = [], []

    class WaitingHandle(BatchHandle):
        def advance(self, callbacks, continuation):
            entered.set()
            assert proceed.wait(3)
            return {"finished": True}

    def factory(config, inputs, context):
        handle = WaitingHandle(config, inputs, context)
        handles.append(handle)
        return handle

    host = RuntimeHost(registry(factory))
    invocation = owner()
    host.start(invocation, REFERENCE, {}, {}, None)
    worker = threading.Thread(target=lambda: outcomes.append(host.drive(invocation)))
    worker.start()
    try:
        assert entered.wait(3)
        host.release(invocation)
        assert handles[0].disposes == 0 and host.snapshot(invocation)["status"] == "running"
    finally:
        proceed.set()
        worker.join(3)
    assert not worker.is_alive()
    assert outcomes[0].status == "succeeded" and handles[0].disposes == 1
    assert host.active_handle_count == 0


def test_disposal_failure_preserves_accepted_result_and_retains_cleanup_handle_for_retry():
    handles = []

    class CleanupHandle(BatchHandle):
        def dispose(self):
            self.disposes += 1
            if self.disposes == 1:
                raise OSError("cleanup unavailable")

    def factory(config, inputs, context):
        handle = CleanupHandle(config, inputs, context)
        handles.append(handle)
        return handle

    host = RuntimeHost(registry(factory), fact_sink=lambda fact: fact["sequence"],
                       result_sink=lambda result: {"receipt": "accepted"})
    invocation = owner()
    host.start(invocation, REFERENCE, {}, {"input": 1}, None)
    result = host.drive(invocation)
    assert result.status == "succeeded" and result.receipt == {"receipt": "accepted"}
    assert host.snapshot(invocation)["disposal_error"] == "runtime_dispose_failed"
    assert host.active_handle_count == 1
    assert host.drive(invocation) == result
    assert handles[0].actions == [0, 1]
    host.release(invocation)
    assert handles[0].disposes == 2 and host.active_handle_count == 0
    assert host.snapshot(invocation)["disposal_error"] is None


def test_invalid_factory_handle_is_disposed_before_registration_fails():
    calls = []

    class InvalidHandle:
        def dispose(self):
            calls.append("disposed")

    host = RuntimeHost(registry(lambda config, inputs, context: InvalidHandle()))
    invocation = owner()
    with pytest.raises(HostContractError) as caught:
        host.start(invocation, REFERENCE, {}, {}, None)
    assert caught.value.reason_code == "runtime_invalid_handle"
    assert calls == ["disposed"] and not host.contains(invocation)


@pytest.mark.parametrize("support,reason", [
    (PauseSupport(ExecutorReference("missing", "1")), "runtime_missing_executor"),
    (PauseSupport(REFERENCE, protocol_version=2), "runtime_protocol_mismatch"),
    (PauseSupport(REFERENCE, continuation_mode="durable"), "runtime_unsupported_continuation"),
])
def test_pause_registration_rejects_missing_version_protocol_and_durable(support, reason):
    result = registry(pause=False)
    with pytest.raises(HostContractError) as caught:
        result.register_pause_support(support, lambda handle, token: True)
    assert caught.value.reason_code == reason


def test_duplicate_registration_and_capability_upgrade_rejected_and_detached_registry_frozen():
    result = registry(pause=False, mode="none")
    with pytest.raises(HostContractError) as caught:
        result.register_pause_support(PauseSupport(REFERENCE), lambda handle, token: True)
    assert caught.value.reason_code == "runtime_pause_capability_mismatch"
    with pytest.raises(HostContractError) as caught:
        result.register_executor(ExecutorDefinition(REFERENCE), BatchHandle)
    assert caught.value.reason_code == "runtime_duplicate_executor"
    frozen = registry().detached(frozen=True)
    with pytest.raises(HostContractError) as caught:
        frozen.register_pause_support(PauseSupport(REFERENCE), lambda handle, token: True)
    assert caught.value.reason_code == "host_registry_frozen"


def test_schema_two_package_registers_exact_executor_node_and_pause_exports():
    definition = NodeDefinition("sample.batch.node", "1", "Batch", "Sample", {}, {"type": "object"},
                                is_output=True)

    def register(host):
        host.register_executor(ExecutorDefinition(REFERENCE, continuation_mode="same_process"), BatchHandle)
        host.register_pause_support(PauseSupport(REFERENCE), lambda handle, token: handle is token)
        host.register_node(definition, None, executor_ref=REFERENCE)

    exports = {"executors": [REFERENCE.to_dict()], "pause_support": [REFERENCE.to_dict()],
               "nodes": [{"component_id": definition.component_id, "component_version": "1"}]}
    package = CapabilityPackage(PackageManifest("sample", "1", exports=exports, schema_version=2), register)
    active = CapabilityPackageLoader([package]).load()
    assert active.registry.get("sample.batch.node", "1").executor_ref == REFERENCE
    assert active.package_manifests[0]["schema_version"] == 2
    assert active.registry.executors.supported_controls(REFERENCE) == ("pause", "resume")
    assert active.registry.catalog()[0]["executable"] is True
    assert active.registry.catalog()[0]["executor_ref"] == REFERENCE.to_dict()


def test_schema_one_export_strictness_and_failed_executor_staging_preserve_base():
    with pytest.raises(HostContractError) as caught:
        PackageManifest("sample", "1", exports={"executors": [REFERENCE.to_dict()]}).to_dict()
    assert caught.value.reason_code == "package_invalid_manifest"
    base = NodeRegistry()

    def register(host):
        host.register_executor(ExecutorDefinition(REFERENCE), BatchHandle)
        host.register_pause_support(PauseSupport(REFERENCE), lambda handle, token: True)

    package = CapabilityPackage(PackageManifest("sample", "1", schema_version=2), register)
    loader = CapabilityPackageLoader([package], base_registry=base)
    with pytest.raises(HostContractError) as caught:
        loader.load()
    assert caught.value.reason_code == "runtime_pause_capability_mismatch"
    assert base.executors.get(REFERENCE) is None
    assert loader.load({}).registry.executors.get(REFERENCE) is None


def test_exact_executor_contract_cannot_be_redefined_by_package_upgrade():
    def package(version, mode):
        return CapabilityPackage(PackageManifest("sample", version, schema_version=2),
                                 lambda host: host.register_executor(
                                     ExecutorDefinition(REFERENCE, continuation_mode=mode), BatchHandle))

    loader = CapabilityPackageLoader([package("1", "none"), package("2", "same_process")])
    first = loader.load({"sample": "1"})
    with pytest.raises(HostContractError) as caught:
        loader.load({"sample": "2"})
    assert caught.value.reason_code == "package_contract_redefined"
    assert first.registry.executors.get(REFERENCE).definition.continuation_mode == "none"
