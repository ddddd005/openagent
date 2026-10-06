"""Version-frozen behavior reports without Agent-only prerequisites."""

from copy import deepcopy
import json

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.event_contracts import CORE_EVENT_TYPES, CORE_PAYLOAD_SCHEMAS, core_payload_schema_ref
from phase1_agent.event_registry import (
    EventBehaviorRegistry, FrozenEventDeclarations, ReportScope, UnsupportedEventDeclaration,
)


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def producer(kind="transform", version="1.0.0"):
    return {
        "kind": kind, "component_id": uid(1), "component_version": version,
        "contract_version": 1, "capabilities": ["events", "progress"],
    }


def payload_schema():
    return {
        "type": "object",
        "properties": {"chapter": {"type": "integer", "minimum": 1}, "complete": {"type": "boolean"}},
        "required": ["chapter", "complete"], "additionalProperties": False,
    }


def register(registry, event_version=1, schema_version=2, component=None):
    registry.register(
        "chapter.check.progress", event_version,
        payload_schema_ref={"schema_id": "chapter-progress", "version": schema_version},
        payload_schema=payload_schema(), producer=component or producer(),
        required_capabilities=("progress",),
    )


def freeze(registry, event_version=1, component=None):
    return registry.freeze(
        producer=component or producer(),
        event_refs=[{"event_type": "chapter.check.progress", "event_version": event_version}],
    )


def test_event_version_and_payload_schema_version_are_exact_independent_frozen_values():
    registry = EventBehaviorRegistry()
    register(registry, event_version=3, schema_version=7)
    frozen = freeze(registry, event_version=3)
    serialized = frozen.to_json()
    assert serialized["behaviors"][0]["event_version"] == 3
    assert serialized["behaviors"][0]["payload_schema_ref"]["version"] == 7
    assert frozen.validate_report(
        "chapter.check.progress", {"schema_id": "chapter-progress", "version": 7},
        {"chapter": 2, "complete": False},
    ) == {"chapter": 2, "complete": False}
    with pytest.raises(ContractValidationError, match="frozen exact version"):
        frozen.validate_report(
            "chapter.check.progress", {"schema_id": "chapter-progress", "version": 3},
            {"chapter": 2, "complete": False},
        )


def test_frozen_json_contains_all_core_and_behavior_schema_basis_but_no_dynamic_run_identity():
    registry = EventBehaviorRegistry()
    register(registry)
    frozen = freeze(registry)
    serialized = json.loads(json.dumps(frozen.to_json()))
    assert set(serialized) == {"schema_version", "kind", "producer", "core_events", "behaviors"}
    assert len(serialized["core_events"]) == len(CORE_EVENT_TYPES)
    assert all(set(item) == {"event_type", "event_version", "payload_schema_ref", "payload_schema"}
               for item in serialized["core_events"])
    restored = registry.restore(serialized)
    assert restored.to_json() == serialized
    assert FrozenEventDeclarations.from_json(serialized).to_json() == serialized
    assert {"run_id", "chain_run_id", "generation", "node_binding_id"}.isdisjoint(serialized)
    serialized["behaviors"][0]["payload_schema"]["properties"]["chapter"]["minimum"] = 10
    assert frozen.to_json()["behaviors"][0]["payload_schema"]["properties"]["chapter"]["minimum"] == 1
    with pytest.raises(ContractValidationError, match="registered version"):
        registry.restore(serialized)


def test_registry_upgrades_or_external_mutation_do_not_change_active_run_and_no_latest_restore():
    registry = EventBehaviorRegistry()
    register(registry)
    frozen = freeze(registry)
    old_json = frozen.to_json()
    register(registry, event_version=2, schema_version=3)
    assert freeze(registry, 2).to_json()["behaviors"][0]["payload_schema_ref"]["version"] == 3
    assert frozen.to_json() == old_json
    registry.unregister("chapter.check.progress", 1)
    assert frozen.validate_report(
        "chapter.check.progress", {"schema_id": "chapter-progress", "version": 2},
        {"chapter": 1, "complete": True},
    )["complete"]
    with pytest.raises(UnsupportedEventDeclaration, match="unavailable") as refused:
        registry.restore(old_json)
    assert refused.value.reason_code == "unsupported"
    with pytest.raises(UnsupportedEventDeclaration, match="unavailable"):
        freeze(registry)


def test_registry_detach_retains_exact_old_versions_without_live_registry_aliases():
    registry = EventBehaviorRegistry()
    register(registry)
    detached = registry.detached()
    registry.unregister("chapter.check.progress", 1)
    assert freeze(detached).behavior_types == {"chapter.check.progress"}
    with pytest.raises(UnsupportedEventDeclaration):
        freeze(registry)


@pytest.mark.parametrize("conflict", [False, True])
def test_repeat_declaration_always_rejects_even_identical_versions(conflict):
    registry = EventBehaviorRegistry()
    register(registry)
    candidate = payload_schema()
    if conflict:
        candidate["properties"]["chapter"]["minimum"] = 100
    with pytest.raises(ContractValidationError, match="Duplicate or conflicting"):
        registry.register(
            "chapter.check.progress", 1,
            payload_schema_ref={"schema_id": "chapter-progress", "version": 2},
            payload_schema=candidate, producer=producer(), required_capabilities=("progress",),
        )


def test_reused_payload_schema_identity_with_different_shape_rejects():
    registry = EventBehaviorRegistry()
    register(registry)
    changed = payload_schema()
    changed["properties"]["chapter"]["type"] = "string"
    with pytest.raises(ContractValidationError, match="conflicting content"):
        registry.register(
            "chapter.other.progress", 1,
            payload_schema_ref={"schema_id": "chapter-progress", "version": 2},
            payload_schema=changed, producer=producer(),
        )


@pytest.mark.parametrize("event_type", sorted(CORE_EVENT_TYPES | {"registered_behavior", "progress"}))
def test_behavior_cannot_declare_or_report_coordinator_core_semantics(event_type):
    registry = EventBehaviorRegistry()
    with pytest.raises(ContractValidationError, match="core event"):
        registry.register(
            event_type, 1, payload_schema_ref={"schema_id": "impostor", "version": 1},
            payload_schema=payload_schema(), producer=producer(),
        )
    frozen = registry.freeze(producer=producer())
    with pytest.raises(ContractValidationError, match="impersonate"):
        frozen.validate_report(event_type, {"schema_id": "impostor", "version": 1}, {"complete": True})


@pytest.mark.parametrize("change", [
    lambda p: p.update(component_id="not-a-component"),
    lambda p: p.update(component_version=""),
    lambda p: p.update(contract_version=2),
    lambda p: p.update(capabilities=["progress"]),
    lambda p: p.update(capabilities=["events"]),
    lambda p: p.update(capabilities=["events", "progress", "progress"]),
])
def test_registration_requires_exact_producer_version_and_declared_capabilities(change):
    registry = EventBehaviorRegistry()
    component = producer()
    change(component)
    with pytest.raises(ContractValidationError):
        register(registry, component=component)


@pytest.mark.parametrize("event_version,schema_version", [(0, 1), (True, 1), (1.0, 1), (1, 0), (1, True)])
def test_declaration_versions_are_strict_positive_integers(event_version, schema_version):
    with pytest.raises(ContractValidationError):
        register(EventBehaviorRegistry(), event_version=event_version, schema_version=schema_version)


def test_run_cannot_select_two_versions_of_the_same_event_type_without_envelope_event_version():
    registry = EventBehaviorRegistry()
    register(registry)
    register(registry, event_version=2, schema_version=3)
    with pytest.raises(ContractValidationError, match="multiple versions"):
        registry.freeze(
            producer=producer(), event_refs=[
                {"event_type": "chapter.check.progress", "event_version": 1},
                {"event_type": "chapter.check.progress", "event_version": 2},
            ],
        )


def test_frozen_schema_unknown_core_version_or_mutation_is_never_filled_from_latest():
    registry = EventBehaviorRegistry()
    frozen = registry.freeze(producer=producer(), core_event_types=("diagnostic",))
    candidate = frozen.to_json()
    candidate["core_events"][0]["event_version"] = 2
    with pytest.raises(UnsupportedEventDeclaration):
        registry.restore(candidate)
    candidate = frozen.to_json()
    candidate["core_events"][0]["payload_schema_ref"]["version"] = 2
    with pytest.raises(UnsupportedEventDeclaration):
        registry.restore(candidate)
    candidate = frozen.to_json()
    candidate["core_events"][0]["payload_schema"]["additionalProperties"] = True
    with pytest.raises(ContractValidationError, match="exact supported version"):
        registry.restore(candidate)
    candidate = frozen.to_json()
    candidate.pop("core_events")
    with pytest.raises(ContractValidationError, match="fields must be exact"):
        registry.restore(candidate)


def test_active_core_schema_basis_is_detached_while_restore_checks_exact_version(monkeypatch):
    registry = EventBehaviorRegistry()
    frozen = registry.freeze(producer=producer(), core_event_types=("diagnostic",))
    serialized = frozen.to_json()
    monkeypatch.setitem(CORE_PAYLOAD_SCHEMAS, "diagnostic", {
        "type": "object", "required": ["unsafe_new_field"],
    })
    assert frozen.validate_core(
        "diagnostic", core_payload_schema_ref("diagnostic"),
        {"code": "MODEL_FAILED", "category": "model"},
    ) == {"code": "MODEL_FAILED", "category": "model"}
    with pytest.raises(ContractValidationError, match="exact schema"):
        frozen.validate_core(
            "diagnostic", core_payload_schema_ref("diagnostic"),
            {"unsafe_new_field": "cannot silently change active core rules"},
        )
    with pytest.raises(ContractValidationError, match="exact supported version"):
        registry.restore(serialized)


def test_unknown_type_bad_payload_bad_schema_and_wrong_producer_fail_without_fake_core_event():
    registry = EventBehaviorRegistry()
    register(registry)
    frozen = freeze(registry)
    ref = {"schema_id": "chapter-progress", "version": 2}
    with pytest.raises(UnsupportedEventDeclaration):
        frozen.validate_report("chapter.unknown", ref, {})
    with pytest.raises(ContractValidationError, match="exact schema"):
        frozen.validate_report("chapter.check.progress", ref, {"chapter": 1, "complete": "yes"})
    with pytest.raises(ContractValidationError, match="producer differs"):
        frozen.validate_report(
            "chapter.check.progress", ref, {"chapter": 1, "complete": True},
            producer=producer(version="2.0.0"),
        )
    with pytest.raises(ContractValidationError, match="core payload schema"):
        registry.register(
            "chapter.new", 1, payload_schema_ref=core_payload_schema_ref("run_state"),
            payload_schema=payload_schema(), producer=producer(),
        )
    with pytest.raises(ContractValidationError, match="local fragment"):
        registry.register(
            "chapter.external", 1,
            payload_schema_ref={"schema_id": "chapter-external", "version": 1},
            payload_schema={"$ref": "https://example.invalid/private"}, producer=producer(),
        )


@pytest.mark.parametrize("kind", ["io", "transform", "kernel"])
def test_generic_reports_do_not_require_agent_state_snapshot_budget_or_turn(kind):
    component = producer(kind)
    registry = EventBehaviorRegistry()
    register(registry, component=component)
    frozen = freeze(registry, component=component)
    assert frozen.validate_report(
        "chapter.check.progress", {"schema_id": "chapter-progress", "version": 2},
        {"chapter": 1, "complete": False},
    ) == {"chapter": 1, "complete": False}
    assert {"snapshot_id", "initial_budget", "turn_id", "status"}.isdisjoint(frozen.to_json())


def records():
    session = {
        "schema_version": 1, "workflow_session_id": uid(2), "workflow_definition_id": uid(3),
        "definition_revision": 5, "revision": 1, "source": {"kind": "new"},
    }
    binding = {
        "schema_version": 1, "node_binding_id": uid(4), "workflow_definition_id": uid(3),
        "workflow_definition_revision": 5, "component_id": uid(1), "component_version": "1.0.0",
        "config": {"owner_component_id": uid(1), "schema_version": 1, "payload": {}},
    }
    run = {
        "schema_version": 1, "profile": "node", "run_id": uid(5),
        "workflow_session_id": uid(2), "node_binding_id": uid(4), "chain_run_id": uid(6),
        "input_id": uid(7), "input_snapshot_id": None, "source_run_id": None, "status": "running",
        "revision": 1, "result_output_id": None, "superseded_by_run_id": None,
    }
    chain = {
        "schema_version": 1, "chain_run_id": uid(6), "workflow_session_id": uid(2),
        "input_id": uid(7), "status": "running", "node_run_ids": [uid(5)], "output_id": None,
    }
    return session, binding, run, chain


def test_report_scope_uses_actual_run_binding_and_fixed_definition_revision_not_component_names():
    session, binding, run, chain = records()
    first = ReportScope.from_records(session, binding, run, chain)
    assert first.workflow_definition_revision == 5
    binding2, run2, chain2 = deepcopy(binding), deepcopy(run), deepcopy(chain)
    binding2["node_binding_id"] = uid(40)
    run2.update(node_binding_id=uid(40), run_id=uid(50), chain_run_id=uid(60))
    chain2.update(chain_run_id=uid(60), node_run_ids=[uid(50)])
    second = ReportScope.from_records(session, binding2, run2, chain2)
    assert first.to_json() != second.to_json()
    candidate = {
        "schema_version": 2, "event_id": uid(8), "workflow_session_id": uid(2),
        "node_binding_id": uid(4), "run_id": uid(5), "sequence": 1,
        "event_type": "chapter.check.progress",
        "payload_schema_ref": {"schema_id": "chapter-progress", "version": 2},
        "payload": {"chapter": 1, "complete": False},
        "visibility": "private", "generation": uid(9),
    }
    assert first.validate_event(candidate) == candidate
    with pytest.raises(ContractValidationError, match="actual run scope"):
        second.validate_event(candidate)


@pytest.mark.parametrize("mutate", [
    lambda s, b, r, c: r.update(node_binding_id=uid(100)),
    lambda s, b, r, c: r.update(workflow_session_id=uid(100)),
    lambda s, b, r, c: b.update(workflow_definition_revision=6),
    lambda s, b, r, c: b.update(workflow_definition_id=uid(100)),
    lambda s, b, r, c: c.update(node_run_ids=[]),
    lambda s, b, r, c: c.update(chain_run_id=uid(100)),
])
def test_scope_rejects_cross_binding_cross_session_or_changed_workflow_definition(mutate):
    session, binding, run, chain = records()
    mutate(session, binding, run, chain)
    with pytest.raises(ContractValidationError):
        ReportScope.from_records(session, binding, run, chain)
