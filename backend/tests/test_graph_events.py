"""Declared events use ordinary graph acceptance without completion commits."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackage, PackageManifest
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort, text_value
from phase1_agent.graph_records import graph_record, validate_graph_bundle, validate_graph_record, validate_graph_transition
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding
from phase1_agent.storage import SqliteStore

from test_graph_public import rebind
from test_graph_service import copy_current, create, document, edge, node, run


class EventHarness:
    def __init__(self):
        self.calls = []
        self.entered, self.release = Event(), Event()
        self.gated = False

    def source(self, config, inputs, context):
        self.calls.append(("source", context.node_run_id))
        if self.gated:
            self.entered.set()
            assert self.release.wait(10)
        return {"output": text_value(str(context.external_input("delta")))}

    def apply(self, config, inputs, context):
        self.calls.append(("apply", context.node_run_id))
        current = context.object_read(config["object_key"])
        count = current["value"]["count"] + int(inputs["input"]["text"])
        context.object_write(config["object_key"], {"count": count}, expected_revision=current["revision"])
        if config["fail"]:
            raise ValueError("private event payload must not escape")
        return {"output": text_value(str(count))}

    def unrelated(self, config, inputs, context):
        pytest.fail("Event traversed an unrelated ordinary output")

    def register(self, host):
        host.register_data_type(DataTypeDefinition(
            "event.counter", 1, {"type": "object", "properties": {"count": {"type": "integer"}},
                                "required": ["count"], "additionalProperties": False}, {"count": 0}))
        host.register_node(NodeDefinition(
            "event.source", "1", "Event input", "Events", {}, {"type": "object", "additionalProperties": False},
            outputs=(NodePort("output", "TEXT"),), capabilities=("external:read",)),
            lambda config, inputs, context: self.source(config, inputs, context))
        host.register_node(NodeDefinition(
            "event.apply", "1", "Apply event", "Events", {"object_key": "main", "fail": False},
            {"type": "object", "properties": {"object_key": {"type": "string"}, "fail": {"type": "boolean"}},
             "required": ["object_key", "fail"], "additionalProperties": False},
            inputs=(NodePort("input", "TEXT"),), outputs=(NodePort("output", "TEXT"),),
            capabilities=("objects:read", "objects:write"),
            object_accesses=({"config_field": "object_key", "type_id": "event.counter", "schema_version": 1,
                              "multiple": False, "access": "read_write"},)),
            lambda config, inputs, context: self.apply(config, inputs, context))
        host.register_node(NodeDefinition(
            "event.unrelated", "1", "Unrelated output", "Events", {}, {"type": "object"},
            outputs=(NodePort("output", "TEXT"),), is_output=True),
            lambda config, inputs, context: self.unrelated(config, inputs, context))

    def package(self):
        return CapabilityPackage(PackageManifest("test.events", "1"), self.register)


@pytest.fixture
def event_service(tmp_path):
    harness = EventHarness()

    def no_model(*args, **kwargs):
        pytest.fail("Event attempted to construct a model")

    with closing(GraphWorkflowService(tmp_path / "events.sqlite", capability_packages=(harness.package(),),
        enabled_packages={"test.events": "1"}, model_factory=no_model, public_model_factory=no_model)) as service:
        yield service, harness
        harness.release.set()


def event_document(service, *, audience="consumer", fail=False, allow=True):
    ordinary = node(service.registry, "text", 14101, text="ordinary round")
    output = node(service.registry, "output", 14102)
    source = node(service.registry, "event.source", 14103)
    apply = node(service.registry, "event.apply", 14104, fail=fail)
    event_output = node(service.registry, "output", 14105)
    unrelated = node(service.registry, "event.unrelated", 14106)
    doc = document([ordinary, output, source, apply, event_output, unrelated], [
        edge(ordinary, output, 14101), edge(source, apply, 14102), edge(apply, event_output, 14103),
    ])
    doc.update(schema_version=2, package_lock=deepcopy(list(service.registry.execution_package_lock)),
        execution_roots=[output["node_binding_id"]], object_bindings=[
            ObjectBinding("main", "event.counter", 1, "shared", readers=(apply["node_binding_id"],),
                          writers=(apply["node_binding_id"],) if allow else ()).to_dict(),
        ], event_bindings=[{
            "event_id": "counter.increment", "schema_version": 1, "display_name": "Increment",
            "audience": audience, "target_node_ids": [event_output["node_binding_id"]],
            "payload_schema": {"type": "object", "properties": {"delta": {"type": "integer", "minimum": 1}},
                               "required": ["delta"], "additionalProperties": False},
        }])
    return doc


def parameters(session, doc, *, key="event-once", payload=None):
    return {"workflow_definition_id": doc["workflow_definition_id"], "definition_revision": doc["revision"],
            "event_id": "counter.increment", "event_schema_version": 1,
            "payload": {"delta": 2} if payload is None else payload,
            "expected_revision": session["revision"], "idempotency_key": key}


def submit(service, session, doc, *, consumer=False, **overrides):
    method = service.submit_consumer_event if consumer else service.submit_event
    return method(session["workflow_session_id"], **{**parameters(session, doc), **overrides})


def records(service):
    with closing(SqliteStore(service.database)) as store:
        return store.read_bundle(include_graph=True)


def test_event_accepts_local_closure_and_object_write_without_replacing_round_candidate(event_service):
    service, harness = event_service
    doc = event_document(service)
    initial = create(service, doc)
    round_view = run(service, initial)
    sid = initial["workflow_session_id"]
    before = records(service)
    accepted = submit(service, round_view, doc)
    chain_id = accepted["active_chain_run_id"]
    service.wait(chain_id)
    current = service.get_session(sid)
    event = service.get_run(sid, chain_id)
    assert event["chain"]["schema_version"] == 5 and event["chain"]["execution_kind"] == "event"
    assert event["chain"]["ordered_nodes"] == [item["node_binding_id"] for item in doc["nodes"][2:5]]
    assert [entry["status"] for entry in event["node_runs"]] == ["succeeded"] * 3
    assert current["objects"]["main"]["value"] == {"count": 2}
    assert current["head_commit_id"] == round_view["head_commit_id"]
    assert current["head_revision"] == round_view["head_revision"]
    assert current["selected_chain_run_id"] == round_view["selected_chain_run_id"]
    assert current["nodes"][1]["outputs"] == round_view["nodes"][1]["outputs"]
    assert len(service.list_graph_candidates(sid)["candidates"]) == 1
    assert [entry[0] for entry in harness.calls] == ["source", "apply"]
    after = records(service)
    assert before["workflow_commit"] == after["workflow_commit"]
    assert before["state_snapshot"] == after["state_snapshot"]
    assert event["state_manifests"]["start"]["objects"]["main"]["revision_id"] == round_view["objects"]["main"]["revision_id"]
    assert event["state_manifests"]["end"]["objects"]["main"]["revision_id"] == current["objects"]["main"]["revision_id"]
    assert chain_id in current["history_refs"]
    next_round = run(service, current)
    assert next_round["head_revision"] == round_view["head_revision"] + 1
    manifest = service.get_run(sid, next_round["selected_chain_run_id"])["state_manifests"]["end"]
    assert manifest["objects"]["main"]["revision_id"] == current["objects"]["main"]["revision_id"]
    assert chain_id in next_round["history_refs"]
    validate_graph_bundle(after)


def test_event_before_any_round_keeps_head_seed_and_selection_empty(event_service):
    service, _ = event_service
    doc = event_document(service)
    initial = create(service, doc)
    event = submit(service, initial, doc)
    service.wait(event["active_chain_run_id"])
    current = service.get_session(initial["workflow_session_id"])
    assert current["head_commit_id"] == initial["head_commit_id"]
    assert current["selected_chain_run_id"] is None
    assert current["status"] == "idle" and current["can_submit"]
    assert service.list_graph_candidates(initial["workflow_session_id"])["candidates"] == []


def test_event_only_graph_with_empty_round_roots_executes_and_has_no_candidates(event_service):
    service, harness = event_service
    doc = event_document(service)
    doc["execution_roots"] = []
    initial = create(service, doc)
    sid = initial["workflow_session_id"]
    assert not service.get_consumer(sid)["can_submit"]
    assert service.list_consumer_event_bindings(sid, workflow_definition_id=doc["workflow_definition_id"],
                                               definition_revision=1)["can_submit"]
    accepted = submit(service, initial, doc, consumer=True)
    chain_id = accepted["receipt"]["chain_run_id"]
    service.wait(chain_id)
    assert service.get_session(sid)["objects"]["main"]["value"] == {"count": 2}
    assert service.read_consumer_event(sid, chain_id=chain_id)["status"] == "succeeded"
    assert service.list_graph_candidates(sid)["candidates"] == []
    assert len(harness.calls) == 2


def test_round_candidate_restore_and_fork_isolate_later_event_facts_and_state(event_service):
    service, harness = event_service
    doc = event_document(service)
    first = run(service, create(service, doc))
    sid = first["workflow_session_id"]
    candidate = service.list_graph_candidates(sid)["candidates"][0]["candidate_id"]
    accepted = submit(service, first, doc)
    event_id = accepted["active_chain_run_id"]
    service.wait(event_id)
    current = service.get_session(sid)

    def choose(method, view, identity, key):
        return method(view["workflow_session_id"], candidate_id=identity,
            expected_revision=view["revision"], expected_data_revision=view["data_revision"],
            expected_head_revision=view["head_revision"], idempotency_key=key)

    fork = choose(service.fork_graph_candidate, current, candidate, "fork-before-event")
    assert fork["objects"]["main"]["value"] == {"count": 0}
    assert event_id not in fork["history_refs"]
    with pytest.raises(ContractValidationError):
        service.read_consumer_event(fork["workflow_session_id"], chain_id=event_id)
    after = run(service, current)
    after_candidate = next(row["candidate_id"] for row in service.list_graph_candidates(sid)["candidates"]
                          if row["chain_run_id"] == after["selected_chain_run_id"])
    second = submit(service, after, doc, payload={"delta": 3}, idempotency_key="event-second")
    second_id = second["active_chain_run_id"]
    service.wait(second_id)
    current = service.get_session(sid)
    after_fork = choose(service.fork_graph_candidate, current, after_candidate, "fork-after-first-event")
    assert after_fork["objects"]["main"]["value"] == {"count": 2}
    assert event_id in after_fork["history_refs"] and second_id not in after_fork["history_refs"]
    assert service.read_consumer_event(after_fork["workflow_session_id"], chain_id=event_id)["status"] == "succeeded"
    restored = choose(service.select_graph_candidate, current, candidate, "restore-before-event")
    assert restored["objects"]["main"]["value"] == {"count": 0}
    assert restored["selected_chain_run_id"] == first["selected_chain_run_id"]
    assert service.read_event(sid, chain_id=second_id)["status"] == "succeeded"
    assert len(harness.calls) == 4


def test_inherited_event_overview_uses_current_copy_scope_and_explicit_original_source(event_service):
    service, _ = event_service
    doc = event_document(service)
    initial = create(service, doc)
    accepted = submit(service, initial, doc)
    chain_id = accepted["active_chain_run_id"]
    service.wait(chain_id)
    parent = service.get_session(initial["workflow_session_id"])
    target = deepcopy(doc)
    target["workflow_definition_id"] = str(uuid4())
    child = copy_current(service, parent, target)
    overview = service.read_consumer_event(child["workflow_session_id"], chain_id=chain_id)
    assert overview["workflow_definition_id"] == target["workflow_definition_id"]
    assert overview["workflow_session_id"] == child["workflow_session_id"]
    assert overview["definition_revision"] == target["revision"]
    assert overview["session_revision"] == child["revision"]
    assert overview["source"] == {
        "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": doc["revision"],
        "workflow_session_id": parent["workflow_session_id"]}
    assert {"inputs", "outputs", "objects", "state_manifests"}.isdisjoint(overview)
    unrelated = service.create_session(doc["workflow_definition_id"], 1, idempotency_key="isolated-session")
    with pytest.raises(ContractValidationError):
        service.read_consumer_event(unrelated["workflow_session_id"], chain_id=chain_id)
    child_event = submit(service, child, target, idempotency_key="child-event")
    service.wait(child_event["active_chain_run_id"])
    assert service.get_session(child["workflow_session_id"])["objects"]["main"]["value"] == {"count": 4}
    assert service.get_session(parent["workflow_session_id"])["objects"]["main"]["value"] == {"count": 2}


def test_event_request_replay_is_frozen_and_reopen_never_reexecutes(event_service):
    service, harness = event_service
    doc = event_document(service)
    initial = create(service, doc)
    request = parameters(initial, doc)
    accepted = service.submit_event(initial["workflow_session_id"], **request)
    service.wait(accepted["active_chain_run_id"])
    assert service.submit_event(initial["workflow_session_id"], **request) == accepted
    assert len(harness.calls) == 2
    database = service.database
    service.close()
    with closing(GraphWorkflowService(database, capability_packages=(harness.package(),))) as restored:
        assert restored.submit_event(initial["workflow_session_id"], **request) == accepted
        assert len(restored.get_session(initial["workflow_session_id"])["chains"]) == 1
        assert restored._futures == {}
        assert len(harness.calls) == 2


def test_event_idempotency_includes_session_and_definition_identity(event_service):
    service, harness = event_service
    doc = event_document(service)
    initial = create(service, doc)
    accepted = submit(service, initial, doc)
    service.wait(accepted["active_chain_run_id"])
    other = service.create_session(doc["workflow_definition_id"], 1, idempotency_key="other-session")
    with pytest.raises(ContractValidationError) as failure:
        submit(service, other, doc)
    assert failure.value.reason_code == "idempotency_conflict"
    current = service.get_session(initial["workflow_session_id"])
    revised = deepcopy(doc)
    revised["revision"] = 2
    rebound = rebind(service, current, revised)
    with pytest.raises(ContractValidationError) as failure:
        submit(service, rebound, revised)
    assert failure.value.reason_code == "idempotency_conflict"
    assert len(harness.calls) == 2


@pytest.mark.parametrize("changed", [
    {"payload": {"delta": 3}}, {"expected_revision": 99},
    {"event_id": "counter.other"}, {"event_schema_version": 2},
])
def test_same_event_key_cannot_change_original_request(event_service, changed):
    service, harness = event_service
    doc = event_document(service)
    for identity, version in (("counter.other", 1), ("counter.increment", 2)):
        doc["event_bindings"].append({**deepcopy(doc["event_bindings"][0]),
                                      "event_id": identity, "schema_version": version})
    initial = create(service, doc)
    accepted = submit(service, initial, doc)
    service.wait(accepted["active_chain_run_id"])
    with pytest.raises(ContractValidationError) as failure:
        submit(service, initial, doc, **changed)
    assert failure.value.reason_code == "idempotency_conflict"
    assert len(harness.calls) == 2


@pytest.mark.parametrize("payload", [{}, {"delta": "bad"}, {"delta": 0}, {"delta": 1, "private": 2},
                                     {"_workflow_frozen_resources": {}, "delta": 1}])
def test_invalid_payload_and_reserved_inputs_are_rejected_before_preparation(event_service, payload):
    service, harness = event_service
    doc = event_document(service)
    initial = create(service, doc)
    with pytest.raises(ContractValidationError):
        submit(service, initial, doc, payload=payload)
    assert harness.calls == []
    assert service.get_session(initial["workflow_session_id"])["chains"] == []
    assert service._execution_registries == {} and service._resource_frames == {}


def test_event_scope_permissions_and_current_revision_are_checked_before_writes(event_service):
    service, harness = event_service
    doc = event_document(service, allow=False)
    initial = create(service, doc)
    with pytest.raises(ContractValidationError) as failure:
        submit(service, initial, doc)
    assert failure.value.reason_code == "session_object_access_denied"
    assert harness.calls == []
    assert service.get_session(initial["workflow_session_id"])["objects"]["main"]["value"] == {"count": 0}
    with pytest.raises(ContractValidationError) as failure:
        submit(service, initial, doc, definition_revision=2)
    assert failure.value.reason_code == "consumer_definition_mismatch"
    with pytest.raises(ContractValidationError) as failure:
        submit(service, initial, doc, expected_revision=100)
    assert failure.value.reason_code == "stale_revision"


def test_consumer_binding_filter_receipt_and_event_overview_never_return_private_state(event_service):
    service, harness = event_service
    doc = event_document(service)
    management = deepcopy(doc["event_bindings"][0])
    management.update(event_id="counter.private", audience="management")
    doc["event_bindings"].append(management)
    initial = create(service, doc)
    sid = initial["workflow_session_id"]
    scope = {"workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1}
    assert len(service.list_event_bindings(sid, **scope)["bindings"]) == 2
    assert len(service.list_consumer_event_bindings(sid, **scope)["bindings"]) == 1
    result = submit(service, initial, doc, consumer=True)
    assert result["kind"] == "workflow.consumer.receipt"
    assert result["receipt"]["operation"] == "event"
    chain_id = result["receipt"]["chain_run_id"]
    service.wait(chain_id)
    replay = submit(service, initial, doc, consumer=True)
    assert replay["receipt"] == result["receipt"]
    overview = service.read_consumer_event(sid, chain_id=chain_id)
    assert overview == service.read_event(sid, chain_id=chain_id)
    assert overview["status"] == "succeeded" and overview["execution_kind"] == "event"
    assert {"inputs", "outputs", "objects", "node_runs", "state_manifests", "private_states"}.isdisjoint(overview)
    assert {"objects", "private_states", "data", "chains", "inputs_values"}.isdisjoint(replay["consumer"])
    assert len(harness.calls) == 2
    current = service.get_session(sid)
    with pytest.raises(ContractValidationError) as failure:
        submit(service, current, doc, consumer=True, event_id="counter.private", idempotency_key="private-denied")
    assert failure.value.reason_code == "graph_event_not_public"
    private = submit(service, current, doc, event_id="counter.private", idempotency_key="private-management")
    service.wait(private["active_chain_run_id"])
    with pytest.raises(ContractValidationError) as failure:
        service.read_consumer_event(sid, chain_id=private["active_chain_run_id"])
    assert failure.value.reason_code == "graph_event_not_public"


def test_current_consumer_authorization_is_required_even_when_replaying_old_receipt(event_service):
    service, _ = event_service
    doc = event_document(service)
    initial = create(service, doc)
    accepted = submit(service, initial, doc, consumer=True)
    chain_id = accepted["receipt"]["chain_run_id"]
    service.wait(chain_id)
    current = service.get_session(initial["workflow_session_id"])
    revised = deepcopy(doc)
    revised["revision"] = 2
    revised["event_bindings"][0]["audience"] = "management"
    rebind(service, current, revised)
    with pytest.raises(ContractValidationError):
        submit(service, initial, doc, consumer=True)
    with pytest.raises(ContractValidationError) as failure:
        service.read_consumer_event(initial["workflow_session_id"], chain_id=chain_id)
    assert failure.value.reason_code == "graph_event_not_public"
    assert service.read_event(initial["workflow_session_id"], chain_id=chain_id)["status"] == "succeeded"


def test_event_failure_and_close_never_create_completion_checkpoint(event_service):
    service, _ = event_service
    doc = event_document(service, fail=True)
    initial = create(service, doc)
    accepted = submit(service, initial, doc)
    chain_id = accepted["active_chain_run_id"]
    service.wait(chain_id)
    failed = service.get_session(initial["workflow_session_id"])
    assert failed["status"] == "failed" and not failed["can_submit"]
    assert failed["head_commit_id"] == initial["head_commit_id"]
    assert failed["objects"]["main"]["value"] == {"count": 0}
    assert failed["available_actions"] == ["close"]
    with pytest.raises(ContractValidationError):
        submit(service, failed, doc, idempotency_key="blocked-failed")
    overview = service.read_consumer_event(initial["workflow_session_id"], chain_id=chain_id)
    assert "private event payload" not in str(overview)
    closed = service.control(initial["workflow_session_id"], action="close",
                             expected_revision=failed["revision"], idempotency_key="close-event")
    assert closed["head_commit_id"] == initial["head_commit_id"]
    assert closed["can_submit"]
    assert service.get_run(initial["workflow_session_id"], chain_id)["chain"]["status"] == "closed"
    assert len(records(service)["workflow_commit"]) == 1


def test_active_event_status_and_controls_keep_round_selection_and_block_new_events(event_service):
    service, harness = event_service
    doc = event_document(service)
    round_view = run(service, create(service, doc))
    harness.gated = True
    accepted = submit(service, round_view, doc)
    chain_id = accepted["active_chain_run_id"]
    assert harness.entered.wait(5)
    try:
        current = service.get_session(round_view["workflow_session_id"])
        assert current["status"] == "running"
        assert current["selected_chain_run_id"] == round_view["selected_chain_run_id"]
        assert current["nodes"][1]["outputs"] == round_view["nodes"][1]["outputs"]
        with pytest.raises(ContractValidationError):
            submit(service, current, doc, idempotency_key="blocked-running")
        scope = {"workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1}
        assert not service.list_consumer_event_bindings(current["workflow_session_id"], **scope)["can_submit"]
        service.control(current["workflow_session_id"], action="pause",
                        expected_revision=current["revision"], idempotency_key="pause-event")
    finally:
        harness.release.set()
    service.wait(chain_id)
    paused = service.get_session(round_view["workflow_session_id"])
    assert paused["status"] == "paused"
    assert paused["selected_chain_run_id"] == round_view["selected_chain_run_id"]
    with pytest.raises(ContractValidationError):
        submit(service, paused, doc, idempotency_key="blocked-paused")
    resumed = service.control(paused["workflow_session_id"], action="resume",
                              expected_revision=paused["revision"], idempotency_key="resume-event")
    service.wait(resumed["active_chain_run_id"])
    complete = service.get_session(round_view["workflow_session_id"])
    assert complete["head_commit_id"] == round_view["head_commit_id"]
    assert complete["objects"]["main"]["value"] == {"count": 2}
    assert [name for name, _ in harness.calls] == ["source", "apply"]


def test_event_acceptance_retry_uses_original_result_and_blocks_parallel_submission(event_service, monkeypatch):
    service, harness = event_service
    doc = event_document(service)
    initial = create(service, doc)
    original = service._save_effects
    attempts = []

    def interrupted(repo, sid, document, event):
        result = original(repo, sid, document, event)
        if event["node_binding_id"] == doc["nodes"][3]["node_binding_id"]:
            attempts.append(deepcopy(event["object_writes"]))
            if len(attempts) == 1:
                raise OSError("accepted event object transaction interrupted")
        return result

    monkeypatch.setattr(service, "_save_effects", interrupted)
    accepted = submit(service, initial, doc)
    service.wait(accepted["active_chain_run_id"])
    pending = service.get_session(initial["workflow_session_id"])
    assert pending["status"] == "archive_failed"
    assert pending["available_actions"] == ["retry_acceptance", "close"]
    assert pending["objects"]["main"]["value"] == {"count": 0}
    with pytest.raises(ContractValidationError):
        submit(service, pending, doc, idempotency_key="blocked-acceptance")
    resumed = service.control(pending["workflow_session_id"], action="retry_acceptance",
                              expected_revision=pending["revision"], idempotency_key="retry-event")
    service.wait(resumed["active_chain_run_id"])
    complete = service.get_session(initial["workflow_session_id"])
    assert complete["objects"]["main"]["value"] == {"count": 2}
    assert complete["head_commit_id"] == initial["head_commit_id"]
    assert len(harness.calls) == 2 and attempts[0] == attempts[1]


def test_chain_v3_is_round_and_event_metadata_is_immutable(event_service):
    service, _ = event_service
    doc = event_document(service)
    completed = run(service, create(service, doc))
    chain = service.get_run(completed["workflow_session_id"], completed["selected_chain_run_id"])["chain"]
    assert chain["execution_kind"] == "round" and chain["event"] is None
    legacy = {key: deepcopy(value) for key, value in chain.items()
              if key not in {"execution_kind", "event", "base_commit_id", "node_run_attempts"}}
    legacy["schema_version"] = 3
    assert validate_graph_record("chain_run", legacy) == legacy
    malformed = deepcopy(chain)
    malformed["event"] = {"event_id": "forged"}
    with pytest.raises(ContractValidationError):
        validate_graph_record("chain_run", malformed)
    accepted = submit(service, completed, doc)
    service.wait(accepted["active_chain_run_id"])
    event = service.get_run(completed["workflow_session_id"], accepted["active_chain_run_id"])["chain"]
    changed = deepcopy(event)
    changed["revision"] += 1
    changed["status"] = "closed"
    changed["event"]["idempotency_key"] = "altered"
    with pytest.raises(ContractValidationError):
        validate_graph_transition("chain_run", event, changed)


def test_event_records_cannot_claim_other_binding_or_completion_checkpoint(event_service):
    service, _ = event_service
    doc = event_document(service)
    initial = create(service, doc)
    accepted = submit(service, initial, doc)
    service.wait(accepted["active_chain_run_id"])
    bundle = records(service)
    forged = deepcopy(bundle)
    event = next(row for row in forged["chain_run"] if row["execution_kind"] == "event")
    event["event"]["audience"] = "management"
    with pytest.raises(ContractValidationError):
        validate_graph_bundle(forged)
    forged = deepcopy(bundle)
    event = next(row for row in forged["chain_run"] if row["execution_kind"] == "event")
    event["status"] = "prepared"
    event["next_node_index"] = 0
    event["completed_nodes"] = []
    event["outputs"] = {}
    event["ordered_nodes"].append(doc["nodes"][0]["node_binding_id"])
    extra_id = str(uuid4())
    event["node_run_ids"].append(extra_id)
    event["node_run_attempts"].append([extra_id])
    forged["node_run"].append(graph_record(
        "node_run", profile="node", run_id=extra_id, workflow_session_id=initial["workflow_session_id"],
        node_binding_id=doc["nodes"][0]["node_binding_id"], chain_run_id=event["chain_run_id"],
        status="prepared", revision=1, input_refs={}, input_values={}, output_refs={}, reads=[], effects=[],
        config=deepcopy(doc["nodes"][0]["config"]), diagnostic=None))
    with pytest.raises(ContractValidationError, match="declared dependency closure"):
        validate_graph_bundle(forged)
    forged = deepcopy(bundle)
    forged["workflow_commit"][0]["source"] = {
        "kind": "completed_execution", "chain_run_id": accepted["active_chain_run_id"]}
    with pytest.raises(ContractValidationError):
        validate_graph_bundle(forged)


def test_paused_event_recovery_never_reexecutes_original_request_and_close_keeps_round_head(event_service):
    service, harness = event_service
    doc = event_document(service)
    round_view = run(service, create(service, doc))
    harness.gated = True
    accepted = submit(service, round_view, doc)
    chain_id = accepted["active_chain_run_id"]
    assert harness.entered.wait(5)
    try:
        current = service.get_session(round_view["workflow_session_id"])
        service.control(current["workflow_session_id"], action="pause",
                        expected_revision=current["revision"], idempotency_key="pause-reopen")
    finally:
        harness.release.set()
    service.wait(chain_id)
    database = service.database
    service.close()
    with closing(GraphWorkflowService(database, capability_packages=(harness.package(),))) as restored:
        failed = restored.get_session(round_view["workflow_session_id"])
        assert failed["status"] == "recovery_unavailable"
        assert failed["selected_chain_run_id"] == round_view["selected_chain_run_id"]
        assert restored.submit_event(round_view["workflow_session_id"], **parameters(round_view, doc)) == accepted
        assert restored._futures == {}
        assert len(harness.calls) == 1
        closed = restored.control(failed["workflow_session_id"], action="close",
                                  expected_revision=failed["revision"], idempotency_key="close-recovered-event")
        assert closed["head_commit_id"] == round_view["head_commit_id"]
        assert closed["selected_chain_run_id"] == round_view["selected_chain_run_id"]


def test_event_public_history_applies_resets_only_after_its_starting_head(event_service):
    service, _ = event_service
    doc = event_document(service)
    event_output = doc["nodes"][4]
    event_output["public_outputs"] = ["output"]
    first = run(service, create(service, doc))
    revised = deepcopy(doc)
    revised["revision"] = 2
    reset = [{"target_node_id": event_output["node_binding_id"], "action": "reset"}]
    rebound = rebind(service, first, revised, mappings=reset)
    accepted = submit(service, rebound, revised)
    chain_id = accepted["active_chain_run_id"]
    service.wait(chain_id)
    history = service.get_run(first["workflow_session_id"], chain_id)
    event_run_id = history["chain"]["node_run_ids"][-1]
    assert history["chain"]["base_commit_id"] == rebound["head_commit_id"]
    query = {"workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 2,
             "node_id": event_output["node_binding_id"], "port_id": "output", "run_id": event_run_id}
    public = service.read_public_output(first["workflow_session_id"], **query)["output"]
    assert public["payload"] == text_value("2")
    copied_doc = deepcopy(revised)
    copied_doc.update(workflow_definition_id=str(uuid4()), revision=1)
    copied = copy_current(service, service.get_session(first["workflow_session_id"]), copied_doc)
    inherited = service.read_public_output(copied["workflow_session_id"], **{
        **query, "workflow_definition_id": copied_doc["workflow_definition_id"], "definition_revision": 1,
    })["output"]
    assert inherited["payload"] == text_value("2")
    assert inherited["source"]["workflow_session_id"] == first["workflow_session_id"]
    later = deepcopy(revised)
    later["revision"] = 3
    rebind(service, service.get_session(first["workflow_session_id"]), later, mappings=reset)
    with pytest.raises(ContractValidationError) as denied:
        service.read_public_output(first["workflow_session_id"], **{**query, "definition_revision": 3})
    assert denied.value.reason_code == "not_found"
    assert service.read_public_output(copied["workflow_session_id"], **{
        **query, "workflow_definition_id": copied_doc["workflow_definition_id"], "definition_revision": 1,
    })["output"]["payload"] == text_value("2")


def test_event_baseline_cannot_be_replaced_with_another_sessions_head(event_service):
    service, _ = event_service
    doc = event_document(service)
    initial = create(service, doc)
    other = service.create_session(doc["workflow_definition_id"], 1, idempotency_key="other-baseline")
    accepted = submit(service, initial, doc)
    service.wait(accepted["active_chain_run_id"])
    forged = records(service)
    event = next(row for row in forged["chain_run"] if row["execution_kind"] == "event")
    event["base_commit_id"] = other["head_commit_id"]
    with pytest.raises(ContractValidationError, match="baseline has another session owner"):
        validate_graph_bundle(forged)
