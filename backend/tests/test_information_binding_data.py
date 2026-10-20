"""D1: real node runs accept information bindings through the data management entry.

The two wiring checks execute an actual graph service run.  They wrap the new
``WorkflowDataManagement.accept_information_bindings`` entry with a spy that
still forwards to the original implementation, so a fallback to the former
direct repository write inside ``GraphInformation`` would not produce a call.
"""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.capability_packages import CapabilityPackage, PackageManifest
from phase1_agent.configuration_data import WorkflowDataManagement
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import InformationSourceDefinition, InformationSourceReference
from phase1_agent.information_binding_data import (
    InformationBindingStore, initialize_information_binding_tables,
)
from phase1_agent.runtime_hosting import InvocationOwner
from phase1_agent.storage import SqliteStore

from test_graph_service import create, document, edge, node
from test_runtime_hosting_integration import COMPONENT, graph, hosted_package, pause, resume, start


PUBLIC = InformationSourceReference("sample.binding.progress", "1")
PLAIN = InformationSourceReference("sample.binding.plain", "1")
INJECTED = "injected information binding write failure"


def page():
    return {"items": [], "next_cursor": None, "status": "ok"}


def source_definition(reference, component_id, **changes):
    return InformationSourceDefinition(
        reference, component_id, "1", reference.source_id, "sample.binding.page",
        item_schema={"type": "object"}, max_page_size=8, **changes)


def binding_definition(reference, component_id, **changes):
    """The accepted durable declaration for a registered source definition."""
    return source_definition(reference, component_id, **changes).to_dict()


def new_owner():
    return InvocationOwner(*(str(uuid4()) for _ in range(4))).to_dict()


def stored_rows(path):
    with closing(SqliteStore(path)) as store:
        return [dict(row) for row in store._connection.execute(
            "SELECT node_run_id,source_id,exact_version,generation,owner,declaration "
            "FROM workflow_information_bindings ORDER BY source_id,exact_version,generation")]


def spy_bindings(monkeypatch, management):
    original = management.accept_information_bindings
    calls = []

    def spy(owner, generation, definitions):
        calls.append((deepcopy(owner), generation, deepcopy(definitions)))
        return original(owner, generation, definitions)

    monkeypatch.setattr(management, "accept_information_bindings", spy)
    return calls


class PlainProvider:
    """Register one historical source on the existing plain text node."""

    def package(self):
        def register(host):
            host.register_information_source(
                source_definition(PLAIN, "tools.text", source_scope="history"),
                history_reader=lambda request: page())
        return CapabilityPackage(
            PackageManifest("sample.binding-provider", "1.0.0", schema_version=4), register)


class HostedProvider:
    """Keep the hosted work package and add one live-and-history source to it."""

    def __init__(self):
        self.entered, self.proceed, self.handles = Event(), Event(), []

    def package(self):
        original = hosted_package(self.entered, self.proceed, self.handles)

        def register(host):
            original.register(host)
            host.register_information_source(
                source_definition(PUBLIC, COMPONENT, source_scope="live_and_history"),
                reader_factory=lambda owner, generation, handle, context: (lambda request: page()),
                history_reader=lambda request: page())

        return CapabilityPackage(replace(original.manifest, schema_version=4), register)


def test_plain_node_run_reaches_data_management_entry_and_is_exactly_reread(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(
            tmp_path / "plain-binding.sqlite",
            capability_packages=[PlainProvider().package()],
            enabled_packages={**DEFAULT_PACKAGES, "sample.binding-provider": "1.0.0"})) as service:
        calls = spy_bindings(monkeypatch, service._data_management)
        plain = node(service.registry, "tools.text", 801, text="plain")
        output = node(service.registry, "tools.output", 802)
        initial = create(service, document([plain, output], [edge(plain, output, 1)]))
        sid = initial["workflow_session_id"]
        chain_id = start(service, initial)
        service.wait(chain_id)
        final = service.get_session(sid)
        assert final["status"] == "succeeded", final["chains"]
        assert len(calls) == 1
        accepted_owner, generation, definitions = calls[0]
        expected = service.registry.information_sources.get(PLAIN).definition.to_dict()
        assert generation == 1
        assert definitions == [expected]
        run = next(item for item in service.get_run(sid, chain_id)["node_runs"]
                   if item["node_binding_id"] == plain["node_binding_id"])
        assert accepted_owner == {
            "workflow_session_id": sid, "chain_run_id": chain_id,
            "node_binding_id": plain["node_binding_id"], "node_run_id": run["run_id"]}
        with closing(service._store()) as store:
            bindings = InformationBindingStore(store)
            assert bindings.get(PLAIN, accepted_owner, generation) == expected
            assert bindings.list([run["run_id"]]) == [
                {"owner": accepted_owner, "generation": 1, "declaration": expected}]


def test_hosted_node_run_and_resume_generation_reach_data_management_entry(tmp_path, monkeypatch):
    provider = HostedProvider()
    with closing(GraphWorkflowService(
            tmp_path / "hosted-binding.sqlite",
            capability_packages=[provider.package()],
            enabled_packages={**DEFAULT_PACKAGES, "sample.hosted-work": "1.0.0"})) as service:
        calls = spy_bindings(monkeypatch, service._data_management)
        initial = create(service, graph(service))
        sid = initial["workflow_session_id"]
        chain_id = start(service, initial)
        try:
            assert provider.entered.wait(5), "hosted node did not reach its controlled gate"
            pause(service, sid)
        finally:
            provider.proceed.set()
        service.wait(chain_id)
        assert service.get_session(sid)["status"] == "paused"
        resume(service, sid)
        service.wait(chain_id)
        final = service.get_session(sid)
        assert final["status"] == "succeeded", final["chains"]
        expected = service.registry.information_sources.get(PUBLIC).definition.to_dict()
        assert [generation for _, generation, _ in calls] == [1, 2]
        accepted_owner, _, definitions = calls[0]
        assert definitions == [expected]
        assert calls[1] == (accepted_owner, 2, [expected])
        run = next(item for item in service.get_run(sid, chain_id)["node_runs"]
                   if item["node_binding_id"] == provider.handles[0].context.node_binding_id)
        assert accepted_owner["node_run_id"] == run["run_id"]
        with closing(service._store()) as store:
            bindings = InformationBindingStore(store)
            assert bindings.get(PUBLIC, accepted_owner, 1) == expected
            assert bindings.get(PUBLIC, accepted_owner, 2) == expected
            assert bindings.list([run["run_id"]]) == [
                {"owner": accepted_owner, "generation": 1, "declaration": expected},
                {"owner": accepted_owner, "generation": 2, "declaration": expected},
            ]


def test_repeated_acceptance_of_the_same_binding_does_not_add_a_row(tmp_path):
    path = tmp_path / "repeat-binding.sqlite"
    with closing(SqliteStore(path)) as store:
        initialize_information_binding_tables(store._connection)
    management = WorkflowDataManagement(lambda: SqliteStore(path))
    accepted_owner = new_owner()
    definition = binding_definition(PUBLIC, "sample.binding.node", source_scope="live_and_history")
    management.accept_information_bindings(accepted_owner, 1, [definition])
    management.accept_information_bindings(accepted_owner, 1, [deepcopy(definition)])
    assert len(stored_rows(path)) == 1
    with closing(SqliteStore(path)) as store:
        assert InformationBindingStore(store).get(PUBLIC, accepted_owner, 1) == definition


def test_empty_definitions_open_no_store_and_accept_nothing():
    def fail():
        raise AssertionError("empty information bindings must not open a store")

    management = WorkflowDataManagement(fail)
    assert management.accept_information_bindings(new_owner(), 1, []) is None


def test_batch_conflict_rolls_back_new_rows_and_keeps_the_existing_one(tmp_path):
    path = tmp_path / "atomic-binding.sqlite"
    with closing(SqliteStore(path)) as store:
        initialize_information_binding_tables(store._connection)
    management = WorkflowDataManagement(lambda: SqliteStore(path))
    accepted_owner = new_owner()
    existing = binding_definition(PUBLIC, "sample.binding.node", read_public=False)
    added = binding_definition(PLAIN, "sample.binding.node", read_public=False)
    conflicting = binding_definition(PUBLIC, "sample.binding.node", read_public=True)
    management.accept_information_bindings(accepted_owner, 1, [existing])
    with pytest.raises(ContractValidationError) as caught:
        management.accept_information_bindings(accepted_owner, 1, [added, conflicting])
    assert caught.value.reason_code == "information_binding_conflict"
    rows = stored_rows(path)
    assert len(rows) == 1
    assert rows[0]["source_id"] == PUBLIC.source_id
    with closing(SqliteStore(path)) as store:
        bindings = InformationBindingStore(store)
        assert bindings.get(PUBLIC, accepted_owner, 1) == existing
        with pytest.raises(ContractValidationError) as missing:
            bindings.get(PLAIN, accepted_owner, 1)
        assert missing.value.reason_code == "information_unbound"


def test_binding_write_failure_releases_live_route_and_does_not_retry(tmp_path, monkeypatch):
    provider = HostedProvider()
    provider.proceed.set()
    path = tmp_path / "binding-failure.sqlite"
    with closing(SqliteStore(path)) as store:
        initialize_information_binding_tables(store._connection)
    with closing(GraphWorkflowService(
            path, capability_packages=[provider.package()],
            enabled_packages={**DEFAULT_PACKAGES, "sample.hosted-work": "1.0.0"})) as service:
        calls, hosts = [], []

        def broken_accept(_store, owner, generation, definitions):
            calls.append((deepcopy(owner), generation, deepcopy(definitions)))
            hosts.append(service._runtime_hosts.get(owner["chain_run_id"]))
            raise OSError(INJECTED)

        # The real data entry opens the store, begins and rolls back; only the
        # domain write is forced to fail so the host failure path runs unchanged.
        monkeypatch.setattr(InformationBindingStore, "accept", broken_accept)
        initial = create(service, graph(service))
        sid = initial["workflow_session_id"]
        chain_id = start(service, initial)
        service.wait(chain_id)
        assert len(calls) == 1, "binding write failure must not be implicitly retried"
        accepted_owner = calls[0][0]
        assert service.get_session(sid)["status"] == "failed"
        run = service.get_run(sid, chain_id)
        assert run["chain"]["status"] == "failed"
        binding_id = provider.handles[0].context.node_binding_id
        failed = next(item for item in run["node_runs"] if item["node_binding_id"] == binding_id)
        assert failed["status"] == "failed"
        assert failed["diagnostic"]["code"] == "graph_node_failed"
        assert failed["diagnostic"]["message"] == INJECTED
        assert failed["diagnostic"]["node_id"] == binding_id
        assert len(provider.handles) == 1
        assert provider.handles[0].disposes == 1
        assert provider.handles[0].dispatched == 0
        host = hosts[0]
        snapshot = host.snapshot(InvocationOwner.from_dict(accepted_owner))
        assert snapshot["status"] == "closed"
        assert snapshot["disposed"] is True
        assert snapshot["disposal_error"] is None
        assert host.active_handle_count == 0
        assert chain_id not in service._runtime_hosts
        assert chain_id not in service._information_routers
        with closing(service._store()) as store:
            count = store._connection.execute(
                "SELECT COUNT(*) FROM workflow_information_bindings").fetchone()[0]
        assert count == 0
        with pytest.raises(ContractValidationError) as caught:
            service.read_information(sid, reference=PUBLIC.to_dict(),
                                     owner=accepted_owner, generation=1)
        assert caught.value.reason_code == "information_unbound"
