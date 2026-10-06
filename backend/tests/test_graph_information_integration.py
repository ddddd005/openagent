"""Application readers observe exact non-Agent calls without advancing them."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.capability_packages import CapabilityPackage
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import InformationSourceDefinition, InformationSourceReference

from test_graph_service import create
from test_runtime_hosting_integration import COMPONENT, OBJECT_KEY, graph, hosted_package, pause, resume, start


PUBLIC = InformationSourceReference("sample.work.public", "1")
DISCOVER_ONLY = InformationSourceReference("sample.work.discover", "1")
PRIVATE = InformationSourceReference("sample.work.private", "1")
PLAIN = InformationSourceReference("sample.work.plain", "1")


class InformationProvider:
    """Project provider handles and the existing fact store without copied logs."""

    def __init__(self):
        self.entered, self.proceed = Event(), Event()
        self.handles, self.factories, self.reads = [], [], []
        self.service = None

    def live_factory(self, owner, generation, handle, context):
        self.factories.append((deepcopy(owner), generation))

        def read(request):
            self.reads.append(deepcopy(request))
            return {"items": [{"dispatched": handle.dispatched}],
                    "next_cursor": None, "status": "ok"}

        return read

    def history(self, request):
        self.reads.append(deepcopy(request))
        if request["cursor"] == "discarded-provider-cursor":
            return {"items": [], "next_cursor": None, "status": "gap"}
        owner = request["owner"]
        history = self.service.get_run(owner["workflow_session_id"], owner["chain_run_id"])
        facts = [deepcopy(item["payload"]) for item in history["runtime_facts"]
                 if item["owner"] == owner and item["generation"] == request["generation"]]
        return {"items": facts[:request["limit"]], "next_cursor": None, "status": "ok"}

    def package(self):
        original = hosted_package(self.entered, self.proceed, self.handles)

        def register(host):
            original.register(host)
            for reference, discover, read in (
                (PUBLIC, True, True), (DISCOVER_ONLY, True, False), (PRIVATE, False, False),
            ):
                host.register_information_source(InformationSourceDefinition(
                    reference, COMPONENT, "1", reference.source_id, "sample.work.page",
                    source_scope="live_and_history", discover_public=discover, read_public=read,
                    item_schema={"type": "object"}, max_page_size=8,
                ), self.live_factory, self.history)
            host.register_information_source(InformationSourceDefinition(
                PLAIN, "tools.text", "1", "plain", "sample.work.page",
                source_scope="history", item_schema={"type": "object"}, max_page_size=8,
            ), history_reader=self.history)

        return CapabilityPackage(replace(original.manifest, schema_version=4), register)


@pytest.fixture
def information_service(tmp_path):
    provider = InformationProvider()
    with closing(GraphWorkflowService(
        tmp_path / "information.sqlite", capability_packages=[provider.package()],
        enabled_packages={**DEFAULT_PACKAGES, "sample.hosted-work": "1.0.0"},
    )) as service:
        provider.service = service
        yield service, provider, GraphApplication(service)


def bindings(app, session, *, chain=None):
    parameters = {"session_id": session["workflow_session_id"],
                  "kind": "information_binding", "limit": 100}
    if chain is not None:
        parameters["chain_id"] = chain
    return app.query("registration.list", parameters)["items"]


def read(app, session, binding, *, reference=None, generation=None, **parameters):
    return app.query("information.read", {
        "session_id": session["workflow_session_id"],
        "reference": binding["registration_ref"] if reference is None else reference.to_dict(),
        "owner": deepcopy(binding["owner"]),
        "generation": binding["generation"] if generation is None else generation,
        "limit": 8, **parameters,
    })


def assert_error(code, callback):
    with pytest.raises(ContractValidationError) as caught:
        callback()
    assert caught.value.reason_code == code


def test_application_directory_is_metadata_only_and_ordinary_nodes_need_no_reader(information_service):
    service, provider, app = information_service
    initial = create(service, graph(service))
    page = app.query("registration.list", {"limit": 1000})
    sources = [item for item in page["items"] if item["kind"] == "information_source"
               and item["package_id"] == "sample.hosted-work"]
    assert {item["registration_ref"]["source_id"] for item in sources} == {
        PUBLIC.source_id, PRIVATE.source_id, DISCOVER_ONLY.source_id, PLAIN.source_id,
    }
    assert all(item["availability"] == "unbound" for item in sources)
    assert all(item["package_id"] == "sample.hosted-work" for item in sources)
    output = next(item for item in page["items"] if item["kind"] == "node"
                  and item["registration_ref"]["component_id"] == "tools.output")
    assert output["has_information_sources"] is False
    assert bindings(app, initial) == []
    assert provider.factories == provider.reads == provider.handles == []
    assert service.get_session(initial["workflow_session_id"]) == initial
    consumer = app.for_consumer()
    public_page = consumer.query("registration.list", {"limit": 1000})
    assert all(item["registration_ref"].get("source_id") != PRIVATE.source_id
               for item in public_page["items"])
    work_node = next(item for item in public_page["items"] if item["kind"] == "node"
                     and item["registration_ref"]["component_id"] == COMPONENT)
    assert PRIVATE.to_dict() not in work_node["information_sources"]


def test_non_agent_live_read_and_discovery_never_advance_work_or_publish_state(information_service):
    service, provider, app = information_service
    initial = create(service, graph(service))
    chain_id = start(service, initial)
    try:
        assert provider.entered.wait(5)
        current = service.get_session(initial["workflow_session_id"])
        before = service.get_run(initial["workflow_session_id"], chain_id)
        routes = bindings(app, initial, chain=chain_id)
        route = next(item for item in routes if item["registration_ref"] == PUBLIC.to_dict())
        assert route["availability"] == "live"
        assert provider.reads == []
        result = read(app.for_consumer(), initial, route)
        assert result["items"] == [{"dispatched": 1}]
        assert result["owner"] == route["owner"]
        result["items"][0]["dispatched"] = 999
        assert read(app, initial, route)["items"] == [{"dispatched": 1}]
        assert service.get_session(initial["workflow_session_id"]) == current
        assert service.get_run(initial["workflow_session_id"], chain_id) == before
        assert current["objects"][OBJECT_KEY]["value"] == {"count": 0}
        assert provider.handles[0].dispatched == 1
        private = next(item for item in routes if item["registration_ref"] == PRIVATE.to_dict())
        discover = next(item for item in routes if item["registration_ref"] == DISCOVER_ONLY.to_dict())
        assert_error("information_read_denied", lambda: read(app.for_consumer(), initial, private))
        assert_error("information_read_denied", lambda: read(app.for_consumer(), initial, discover))
        consumer_routes = bindings(app.for_consumer(), initial, chain=chain_id)
        assert all(item["registration_ref"] != PRIVATE.to_dict() for item in consumer_routes)
        assert any(item["registration_ref"] == DISCOVER_ONLY.to_dict() for item in consumer_routes)
        assert_error("information_invalid_limit", lambda: read(app, initial, route, limit=9))
        assert_error("information_unbound", lambda: read(app, initial, route, generation=999))
        forged = deepcopy(route)
        forged["owner"]["workflow_session_id"] = str(uuid4())
        assert_error("information_owner_mismatch", lambda: read(app, initial, forged))
    finally:
        provider.proceed.set()
    service.wait(chain_id)
    final = service.get_session(initial["workflow_session_id"])
    assert final["status"] == "succeeded", final["chains"]
    assert provider.handles[0].dispatched == 1
    assert provider.handles[0].disposes == 1
    assert_error("information_live_unavailable", lambda: read(app, initial, route))
    history = read(app, initial, route, source_scope="history")
    assert [item["phase"] for item in history["items"]] == ["dispatch", "received", "result"]
    assert read(app, initial, route, source_scope="history",
                cursor="discarded-provider-cursor")["status"] == "gap"


def test_pause_keeps_readers_and_resume_does_not_mix_generations(information_service):
    service, provider, app = information_service
    initial = create(service, graph(service))
    chain_id = start(service, initial)
    try:
        assert provider.entered.wait(5)
        route = next(item for item in bindings(app, initial, chain=chain_id)
                     if item["registration_ref"] == PUBLIC.to_dict())
        pause(service, initial["workflow_session_id"])
    finally:
        provider.proceed.set()
    service.wait(chain_id)
    paused = service.get_session(initial["workflow_session_id"])
    assert paused["status"] == "paused"
    before = deepcopy(paused)
    assert read(app, initial, route)["items"] == [{"dispatched": 1}]
    assert service.get_session(initial["workflow_session_id"]) == before
    assert [item["phase"] for item in read(app, initial, route, source_scope="history")["items"]] == [
        "dispatch", "received",
    ]
    resumed_entered, resumed_proceed = Event(), Event()
    original_advance = provider.handles[0].advance

    def delayed_resume(callbacks, continuation):
        resumed_entered.set()
        assert resumed_proceed.wait(5), "Test did not release the resumed non-Agent call"
        return original_advance(callbacks, continuation)

    provider.handles[0].advance = delayed_resume
    resume(service, initial["workflow_session_id"])
    try:
        assert resumed_entered.wait(5)
        current = next(item for item in bindings(app, initial, chain=chain_id)
                       if item["registration_ref"] == PUBLIC.to_dict() and item["generation"] == 2)
        assert current["owner"] == route["owner"]
        before = service.get_session(initial["workflow_session_id"])
        assert read(app, initial, current)["items"] == [{"dispatched": 1}]
        assert_error("information_stale_generation", lambda: read(app, initial, route))
        assert service.get_session(initial["workflow_session_id"]) == before
    finally:
        resumed_proceed.set()
    service.wait(chain_id)
    completed = service.get_session(initial["workflow_session_id"])
    assert completed["status"] == "succeeded", completed["chains"]
    assert provider.handles[0].dispatched == 1
    assert [item["phase"] for item in read(app, initial, route, source_scope="history")["items"]] == [
        "dispatch", "received",
    ]
    assert read(app, initial, current, source_scope="history")["items"] == [{"phase": "result"}]
    assert_error("information_live_unavailable", lambda: read(app, initial, current))
    assert_error("information_live_unavailable", lambda: read(app, initial, route))


def test_fork_and_reopen_read_original_history_without_latest_run_fallback(information_service):
    service, provider, app = information_service
    provider.proceed.set()
    initial = create(service, graph(service))
    first_chain = start(service, initial)
    service.wait(first_chain)
    first = service.get_session(initial["workflow_session_id"])
    assert first["status"] == "succeeded", first["chains"]
    route = next(item for item in bindings(app, first, chain=first_chain)
                 if item["registration_ref"] == PUBLIC.to_dict())
    candidate = service.list_graph_candidates(first["workflow_session_id"])["candidates"][0]
    child = service.fork_graph_candidate(
        first["workflow_session_id"], candidate_id=candidate["candidate_id"],
        expected_revision=first["revision"], expected_data_revision=first["data_revision"],
        expected_head_revision=first["head_revision"], idempotency_key=str(uuid4()),
    )
    original = read(app, child, route, source_scope="history")
    assert original["owner"]["workflow_session_id"] == first["workflow_session_id"]
    second_chain = start(service, first)
    service.wait(second_chain)
    second = service.get_session(initial["workflow_session_id"])
    second_route = next(item for item in bindings(app, second, chain=second_chain)
                        if item["registration_ref"] == PUBLIC.to_dict())
    assert_error("information_scope_denied", lambda: read(app, child, second_route, source_scope="history"))
    assert read(app, child, route, source_scope="history") == original
    other = service.create_session(first["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    assert_error("information_scope_denied", lambda: read(app, other, route, source_scope="history"))
    service.close()
    with closing(GraphWorkflowService(
        service.database, capability_packages=[provider.package()],
    )) as reopened:
        provider.service = reopened
        reopened_app = GraphApplication(reopened)
        assert read(reopened_app, child, route, source_scope="history") == original
        assert_error("information_live_unavailable", lambda: read(reopened_app, child, route))
        assert_error("information_scope_denied", lambda: read(
            reopened_app, child, second_route, source_scope="history"))


def test_plain_node_history_binding_is_optional_and_reader_does_not_execute(information_service):
    service, provider, app = information_service
    provider.proceed.set()
    initial = create(service, graph(service))
    chain_id = start(service, initial)
    service.wait(chain_id)
    final = service.get_session(initial["workflow_session_id"])
    assert final["status"] == "succeeded", final["chains"]
    plain = next(item for item in bindings(app, final, chain=chain_id)
                 if item["registration_ref"] == PLAIN.to_dict())
    assert plain["availability"] == "history"
    before = service.get_run(final["workflow_session_id"], chain_id)
    assert read(app, final, plain, source_scope="history")["items"] == []
    assert service.get_run(final["workflow_session_id"], chain_id) == before
    assert len(provider.handles) == 1 and provider.handles[0].dispatched == 1
    assert_error("information_live_unavailable", lambda: read(app, final, plain))
