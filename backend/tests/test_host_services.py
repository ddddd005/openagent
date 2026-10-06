"""Exact, run-scoped public services without Agent or model implementations."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from phase1_agent.capability_packages import CapabilityPackage, CapabilityPackageLoader, PackageManifest
from phase1_agent.graph_contracts import NodeDefinition, NodeRegistry
from phase1_agent.host_sdk import (
    HostContractError, HostServiceRegistry, ServiceDefinition, ServiceOperation, ServiceReference,
)
from phase1_agent.host_services import HostServiceEnvironment, HostServiceRun

from test_graph_service import uid


REFERENCE = ServiceReference("sample.uppercase", "1")
OPERATION = ServiceOperation(
    "sample:format", "upper",
    {"type": "object", "required": ["text"], "additionalProperties": False,
     "properties": {"text": {"type": "string"}}},
    {"type": "object", "required": ["text"], "additionalProperties": False,
     "properties": {"text": {"type": "string"}}})
DEFINITION = ServiceDefinition(REFERENCE, (OPERATION,))
GRANT = {**REFERENCE.to_dict(), "capability": OPERATION.capability, "operations": [OPERATION.operation]}


class UppercaseService:
    def __init__(self, environment):
        self.environment = environment
        self.calls, self.releases = 0, 0
        self.prepared = None
        self.accepted = []
        self.fail_release_once = False

    def prepare_run(self, **run):
        self.prepared = run

    def __call__(self, context, capability, operation, payload):
        self.calls += 1
        return {"text": payload["text"].upper()}

    def accept_outputs(self, event):
        self.accepted.append(event)

    def release_run(self, sid, chain_id):
        self.releases += 1
        if self.fail_release_once and self.releases == 1:
            raise OSError("offline cleanup failure")


def definition(**changes):
    return replace(NodeDefinition(
        "sample.upper", "1", "Upper", "Sample", {}, {"type": "object"},
        capabilities=("sample:format",), service_requirements=(GRANT,)), **changes)


def make_run(registry):
    node = {"node_binding_id": uid(10), "config": {}}
    environment = HostServiceEnvironment(
        lambda *args: None, lambda *args: None, lambda *args: None, lambda *args: None, {})
    frame = HostServiceRun(registry, workflow_session_id=uid(1), chain_run_id=uid(2),
                           nodes={uid(10): node}, definitions={uid(10): definition()})
    frame.prepare(lambda reference, resources: environment, {})
    context = SimpleNamespace(workflow_session_id=uid(1), chain_run_id=uid(2),
                              node_binding_id=uid(10), node_run_id=uid(11))
    return frame, context


def test_service_declarations_reject_duplicates_protocol_and_missing_or_ambiguous_grants():
    registry = NodeRegistry()
    registry.services.register(DEFINITION, UppercaseService)
    with pytest.raises(HostContractError) as caught:
        registry.services.register(DEFINITION, UppercaseService)
    assert caught.value.reason_code == "host_duplicate_service"
    for declaration, code in [
        (replace(DEFINITION, protocol_version=2), "host_service_protocol_mismatch"),
        (replace(DEFINITION, operations=(OPERATION, OPERATION)), "host_duplicate_service_operation"),
    ]:
        with pytest.raises(HostContractError) as caught:
            declaration.to_dict()
        assert caught.value.reason_code == code
    cases = [
        (definition(service_requirements=({**GRANT, "exact_version": "missing"},)), "host_service_missing"),
        (definition(capabilities=()), "host_service_capability_denied"),
        (definition(service_requirements=({**GRANT, "operations": ["missing"]},)),
         "host_service_operation_missing"),
        (definition(service_requirements=(GRANT, GRANT)), "host_service_route_conflict"),
    ]
    for node, code in cases:
        with pytest.raises(HostContractError) as caught:
            registry.register(node, lambda *args: {})
        assert caught.value.reason_code == code


def test_frozen_run_routes_exact_operations_and_checks_both_json_boundaries():
    registry = HostServiceRegistry()
    registry.register(DEFINITION, UppercaseService)
    frame, context = make_run(registry)
    original = frame.instances[REFERENCE]
    registry.register(replace(DEFINITION, reference=ServiceReference("sample.uppercase", "2")), UppercaseService)
    assert frame.registry.get(ServiceReference("sample.uppercase", "2")) is None
    with pytest.raises(HostContractError, match="frozen"):
        frame.registry.register(DEFINITION, UppercaseService)
    assert frame.call(context, "sample:format", "upper", {"text": "ready"}) == {"text": "READY"}
    assert original.calls == 1
    for capability, operation, payload, code in [
        ("sample:other", "upper", {"text": "hi"}, "graph_capability_denied"),
        ("sample:format", "missing", {"text": "hi"}, "host_service_operation_denied"),
        ("sample:format", "upper", {"text": True}, "host_service_request_invalid"),
    ]:
        with pytest.raises(HostContractError) as caught:
            frame.call(context, capability, operation, payload)
        assert caught.value.reason_code == code
    assert original.calls == 1
    other_context = SimpleNamespace(**vars(context))
    other_context.chain_run_id = uid(3)
    with pytest.raises(HostContractError) as caught:
        frame.call(other_context, "sample:format", "upper", {"text": "hi"})
    assert caught.value.reason_code == "host_service_owner_mismatch"
    frame.instances[REFERENCE] = lambda *args: {"text": True}
    with pytest.raises(HostContractError) as caught:
        frame.call(context, "sample:format", "upper", {"text": "hi"})
    assert caught.value.reason_code == "host_service_response_invalid"


def test_instances_are_independent_per_run_and_failed_cleanup_is_retryable():
    registry = HostServiceRegistry()
    registry.register(DEFINITION, UppercaseService)
    first, context = make_run(registry)
    second, other = make_run(registry)
    assert first.instances[REFERENCE] is not second.instances[REFERENCE]
    first.call(context, "sample:format", "upper", {"text": "first"})
    assert second.instances[REFERENCE].calls == 0
    instance = first.instances[REFERENCE]
    first.accept_outputs({"node_binding_id": uid(10), "outputs": {"output": {"text": "FIRST"}}})
    assert len(instance.accepted) == 1
    instance.fail_release_once = True
    assert first.release() is False
    assert first.cleanup_errors == {REFERENCE: "host_service_release_failed"}
    assert first.release() is True and instance.releases == 2
    assert first.cleanup_errors == {} and second.release() is True


def test_invalid_factory_handle_is_disposed_and_prepare_failure_releases_instances():
    registry = HostServiceRegistry()
    disposals = []

    class Invalid:
        def dispose(self):
            disposals.append("disposed")

    registry.register(DEFINITION, lambda env: Invalid())
    with pytest.raises(HostContractError) as caught:
        make_run(registry)
    assert caught.value.reason_code == "host_invalid_service_instance"
    assert disposals == ["disposed"]
    instances = []

    class PrepareFailure(UppercaseService):
        def prepare_run(self, **run):
            raise OSError("offline prepare failed")

    def factory(env):
        instance = PrepareFailure(env)
        instances.append(instance)
        return instance

    registry = HostServiceRegistry()
    registry.register(DEFINITION, factory)
    with pytest.raises(OSError, match="prepare failed"):
        make_run(registry)
    assert instances[0].releases == 1


def test_manifest_three_stages_services_and_rejects_same_version_contract_redefinition():
    def package(request_schema):
        def register(host):
            host.register_service(
                replace(DEFINITION, operations=(replace(OPERATION, request_schema=request_schema),)), UppercaseService)
        return CapabilityPackage(PackageManifest("sample", "1", schema_version=3,
            exports={"services": [REFERENCE.to_dict()]}), register)

    loader = CapabilityPackageLoader((package(OPERATION.request_schema),))
    first = loader.load()
    assert first.registry.services.catalog() == [DEFINITION.to_dict()]
    assert first.package_manifests[0]["exports"]["services"] == [REFERENCE.to_dict()]
    loader._packages["sample", "1"] = package({"type": "object"})
    with pytest.raises(HostContractError) as caught:
        loader.load()
    assert caught.value.reason_code == "package_contract_redefined"
    assert first.registry.services.catalog() == [DEFINITION.to_dict()]
    with pytest.raises(HostContractError) as caught:
        PackageManifest("sample", "1", schema_version=2, exports={"services": [REFERENCE.to_dict()]}).to_dict()
    assert caught.value.reason_code == "package_invalid_manifest"
