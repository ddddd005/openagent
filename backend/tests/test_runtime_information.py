"""Provider-owned information routes, independent of Agent internals."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
import threading

import pytest

from phase1_agent.capability_packages import CapabilityPackage, CapabilityPackageLoader, PackageManifest
from phase1_agent.graph_contracts import NodeDefinition, NodeRegistry
from phase1_agent.host_sdk import (
    HostContractError, InformationSourceDefinition, InformationSourceReference,
    InformationSourceRegistry,
)
from phase1_agent.runtime_executor_contracts import ExecutorDefinition, ExecutorReference, ExecutorRegistry, PauseSupport
from phase1_agent.runtime_hosting import InvocationOwner, RuntimeHost
from phase1_agent.runtime_information import InformationRouter, paginate_registrations, registration_catalog


SOURCE = InformationSourceReference("sample.counter.progress", "1")
NODE = NodeDefinition("sample.counter", "1", "Counter", "Sample", {}, {"type": "object"})


def owner():
    return InvocationOwner(*(str(uuid4()) for _ in range(4)))


def source(**changes):
    return replace(InformationSourceDefinition(
        SOURCE, "sample.counter", "1", "progress", "sample.counter.page",
        item_schema={"type": "integer"}, source_scope="live_and_history"), **changes)


def page(items=None, **changes):
    return {"items": [] if items is None else items, "next_cursor": None, "status": "ok", **changes}


def assert_error(code, operation):
    with pytest.raises(HostContractError) as caught:
        operation()
    assert caught.value.reason_code == code


def test_aggregate_catalog_never_calls_providers_and_nodes_without_logs_remain_visible():
    calls = []
    registry = NodeRegistry()
    registry.register(NODE, lambda config, inputs, context: {})
    registry.register(replace(NODE, component_id="sample.quiet"), None)
    registry.information_sources.register(source(), lambda *args: calls.append(args))
    entries = registration_catalog(registry)
    nodes = [item for item in entries if item["kind"] == "node"]
    assert calls == []
    assert [item["has_information_sources"] for item in nodes] == [True, False]
    assert next(item for item in entries if item["kind"] == "information_source")["availability"] == "unbound"
    assert registry.information_sources.detached(frozen=True).catalog() == registry.information_sources.catalog()


def test_directory_consumer_filters_private_references_and_pagination_is_query_bound():
    registry = NodeRegistry()
    registry.register(NODE, None)
    registry.information_sources.register(source(discover_public=False, read_public=True))
    entries = registration_catalog(registry)
    result = paginate_registrations(entries, audience="consumer")
    assert all(item["kind"] != "information_source" for item in result["items"])
    node = next(item for item in result["items"] if item["kind"] == "node")
    assert node["information_sources"] == []
    first = paginate_registrations(entries, limit=1)
    second = paginate_registrations(entries, limit=1, cursor=first["next_cursor"])
    assert second["items"] != first["items"]
    assert_error("information_invalid_cursor", lambda: paginate_registrations(
        entries, limit=2, cursor=first["next_cursor"]))
    assert_error("information_invalid_cursor", lambda: paginate_registrations(
        entries, limit=1, cursor=first["next_cursor"][:-3] + "bad"))
    assert_error("information_invalid_cursor", lambda: paginate_registrations(
        entries, limit=1, cursor=first["next_cursor"], audience="consumer"))


def test_discovery_and_read_permissions_are_separate_and_response_is_not_retained():
    registry = InformationSourceRegistry()
    provider_content = [2]
    registry.register(source(discover_public=True), history_reader=lambda request: page(provider_content))
    router = InformationRouter(registry)
    invocation = owner()
    router.bind(SOURCE, invocation, 1, reader=lambda request: page(provider_content))
    assert router.catalog(audience="consumer")
    assert_error("information_read_denied", lambda: router.read(SOURCE, invocation, 1, audience="consumer"))
    response = router.read(SOURCE, invocation, 1)
    response["items"].append(99)
    provider_content[:] = [3]
    assert router.read(SOURCE, invocation, 1)["items"] == [3]
    assert all("items" not in vars(binding) for binding in router._bindings.values())
    assert all("payload" not in vars(binding) for binding in router._bindings.values())


def test_exact_owner_generation_release_and_historical_binding():
    registry = InformationSourceRegistry()
    registry.register(source(read_public=True), history_reader=lambda request: page([request["generation"]]))
    router = InformationRouter(registry)
    invocation = owner()
    router.bind(SOURCE, invocation, 1, reader=lambda request: page([1]))
    assert_error("information_unbound", lambda: router.read(SOURCE, owner(), 1))
    forged = replace(invocation, workflow_session_id=str(uuid4()))
    assert_error("information_owner_mismatch", lambda: router.bind(SOURCE, forged, 2))
    router.bind(SOURCE, invocation, 2, reader=lambda request: page([2]))
    assert_error("information_stale_generation", lambda: router.read(SOURCE, invocation, 1))
    assert_error("information_duplicate_binding", lambda: router.bind(SOURCE, invocation, 1))
    assert router.read(SOURCE, invocation, 1, source_scope="history")["items"] == [1]
    router.release(invocation)
    assert_error("information_live_unavailable", lambda: router.read(SOURCE, invocation, 2))
    assert router.read(SOURCE, invocation, 2, source_scope="history")["items"] == [2]
    reopened = InformationRouter(registry)
    assert_error("information_unbound", lambda: reopened.read(SOURCE, invocation, 2, source_scope="history"))
    reopened.restore(SOURCE, invocation, 2)
    assert reopened.read(SOURCE, invocation, 2, source_scope="history")["items"] == [2]
    assert_error("information_stale_generation", lambda: reopened.read(SOURCE, invocation, 2))


@pytest.mark.parametrize("response", [
    {"items": [1], "next_cursor": None, "status": "ok", "other": 1},
    page(["wrong format"]), page([1, 2]), page([1], next_cursor=1),
    page([1], status="complete"),
])
def test_read_envelope_and_registered_page_bounds(response):
    registry = InformationSourceRegistry()
    registry.register(source(max_page_size=1))
    router, invocation = InformationRouter(registry), owner()
    router.bind(SOURCE, invocation, 1, reader=lambda request: response)
    assert_error("information_invalid_response", lambda: router.read(SOURCE, invocation, 1, limit=1))
    assert_error("information_invalid_limit", lambda: router.read(SOURCE, invocation, 1, limit=True))


def test_reader_owns_opaque_cursor_gap_and_reset_without_runtime_business_interpretation():
    requests = []
    registry = InformationSourceRegistry()
    registry.register(source())
    router, invocation = InformationRouter(registry), owner()
    def reader(request):
        requests.append(request)
        return page([1], status="gap", next_cursor="provider:opaque:next")
    router.bind(SOURCE, invocation, 1, reader=reader)
    result = router.read(SOURCE, invocation, 1, cursor="provider:opaque:old")
    assert result["status"] == "gap" and result["next_cursor"] == "provider:opaque:next"
    assert requests == [{"owner": invocation.to_dict(), "generation": 1,
                         "limit": 100, "cursor": "provider:opaque:old"}]
    assert_error("information_invalid_cursor", lambda: router.read(SOURCE, invocation, 1, cursor=2))


def test_release_while_provider_reads_does_not_block_and_does_not_return_stale_content():
    entered, leave = threading.Event(), threading.Event()
    registry = InformationSourceRegistry()
    registry.register(source())
    router, invocation = InformationRouter(registry), owner()
    errors = []
    def reader(request):
        entered.set()
        assert leave.wait(3)
        return page([1])
    def read():
        try:
            router.read(SOURCE, invocation, 1)
        except HostContractError as error:
            errors.append(error.reason_code)
    router.bind(SOURCE, invocation, 1, reader=reader)
    worker = threading.Thread(target=read)
    worker.start()
    assert entered.wait(3)
    router.release(invocation)
    leave.set()
    worker.join(3)
    assert not worker.is_alive()
    assert errors == ["information_live_unavailable"]


def test_manifest_four_stages_information_and_preserves_older_strict_export_versions():
    definition = source(source_scope="history")
    manifest = PackageManifest("sample.counter", "1", schema_version=4, exports={
        "nodes": [{"component_id": "sample.counter", "component_version": "1"}],
        "information_sources": [SOURCE.to_dict()],
    })
    def register(host):
        host.register_node(NODE, None)
        host.register_information_source(definition, history_reader=lambda request: page([1]))
    loaded = CapabilityPackageLoader((CapabilityPackage(manifest, register),)).load()
    entry = next(item for item in registration_catalog(
        loaded.registry, package_manifests=loaded.package_manifests) if item["kind"] == "information_source")
    assert (entry["package_id"], entry["package_version"]) == ("sample.counter", "1")
    assert_error("host_registry_frozen", lambda: loaded.registry.information_sources.register(definition))
    for version in (1, 2, 3):
        assert_error("package_invalid_manifest", lambda: replace(manifest, schema_version=version).to_dict())
def test_conflicting_source_channel_and_declared_manifest_exports_are_rejected():
    registry = InformationSourceRegistry()
    registry.register(source())
    assert_error("information_duplicate_source", lambda: registry.register(source()))
    assert_error("information_duplicate_channel", lambda: registry.register(
        source(reference=InformationSourceReference("sample.other.progress", "1"))))
    manifest = PackageManifest("sample.counter", "1", schema_version=4, exports={"information_sources": []})
    assert_error("package_export_mismatch", lambda: CapabilityPackageLoader((
        CapabilityPackage(manifest, lambda host: host.register_information_source(source())),
    )).load())


def test_hosted_non_agent_sources_survive_pause_rebind_resume_and_release_after_success():
    invocation = owner()
    reference = ExecutorReference("sample.counter", "1")
    handles, bindings = [], []
    class Handle:
        def __init__(self):
            self.count = 0
        def advance(self, callbacks, continuation):
            callbacks.pause_point(self)
            self.count += 1
            return {"count": self.count}
        def dispose(self):
            pass
    def factory(config, inputs, context):
        result = Handle()
        handles.append(result)
        return result
    registry = ExecutorRegistry()
    registry.register_executor(ExecutorDefinition(reference, continuation_mode="same_process"), factory)
    registry.register_pause_support(PauseSupport(reference), lambda handle, continuation: handle is continuation)
    sources = InformationSourceRegistry()
    sources.register(source(), lambda current, generation, handle, context: lambda request: page([handle.count]),
                     lambda request: page([request["generation"]]))
    router = InformationRouter(sources)
    def sink(current, generation, definitions):
        unlocked = threading.Event()
        def inspect():
            host.snapshot(invocation)
            unlocked.set()
        worker = threading.Thread(target=inspect)
        worker.start()
        assert unlocked.wait(3), "Metadata sink was called under host lock"
        worker.join(3)
        bindings.append((current, generation, definitions))
    host = RuntimeHost(registry, information_router=router, information_binding_sink=sink)
    host.start(invocation, reference, {}, {}, SimpleNamespace(definition=NODE))
    host.request_pause(invocation, "pause")
    assert host.drive(invocation).status == "paused"
    assert router.read(SOURCE, invocation, 1)["items"] == [0]
    host.resume(invocation, "resume")
    assert_error("information_stale_generation", lambda: router.read(SOURCE, invocation, 1))
    assert router.read(SOURCE, invocation, 2)["items"] == [0]
    assert host.drive(invocation).status == "succeeded"
    assert handles[0].count == 1
    assert_error("information_live_unavailable", lambda: router.read(SOURCE, invocation, 2))
    assert router.read(SOURCE, invocation, 2, source_scope="history")["items"] == [2]
    assert [item[1] for item in bindings] == [1, 2]


def test_plain_node_history_binding_does_not_create_a_hosted_invocation_or_call_executor():
    invocation = owner()
    sources = InformationSourceRegistry()
    sources.register(source(source_scope="history"), history_reader=lambda request: page([1]))
    router, bindings = InformationRouter(sources), []
    host = RuntimeHost(ExecutorRegistry(), information_router=router,
                       information_binding_sink=lambda *args: bindings.append(args))
    context = SimpleNamespace(definition=NODE, **invocation.to_dict())
    host.bind_node_information(context)
    assert host.active_handle_count == 0
    assert host._invocations == {}
    assert len(bindings) == 1
    assert router.read(SOURCE, invocation, 1, source_scope="history")["items"] == [1]


def test_release_during_reader_factory_does_not_reactivate_a_live_route():
    entered, leave = threading.Event(), threading.Event()
    invocation = owner()
    reference = ExecutorReference("sample.counter", "1")
    executor_registry = ExecutorRegistry()
    executor_registry.register_executor(
        ExecutorDefinition(reference),
        lambda config, inputs, context: SimpleNamespace(advance=lambda *args: {}, dispose=lambda: None))
    source_registry = InformationSourceRegistry()
    def reader_factory(current, generation, handle, context):
        entered.set()
        assert leave.wait(3)
        return lambda request: page([1])
    source_registry.register(source(), reader_factory)
    router, errors, bindings = InformationRouter(source_registry), [], []
    host = RuntimeHost(executor_registry, information_router=router,
                       information_binding_sink=lambda *args: bindings.append(args))
    def start():
        try:
            host.start(invocation, reference, {}, {}, SimpleNamespace(definition=NODE))
        except HostContractError as error:
            errors.append(error.reason_code)
    worker = threading.Thread(target=start)
    worker.start()
    assert entered.wait(3)
    host.release(invocation)
    leave.set()
    worker.join(3)
    assert not worker.is_alive() and errors == ["information_live_unavailable"]
    assert bindings == []
    assert_error("information_live_unavailable", lambda: router.read(SOURCE, invocation, 1))
