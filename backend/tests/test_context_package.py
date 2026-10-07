"""Context contracts and nodes use synthetic accepted artifacts, never models."""

from contextlib import closing
from copy import deepcopy
from uuid import UUID

import pytest

from phase1_agent.capability_packages import CapabilityPackageLoader
from phase1_agent.content_contracts import create_content_package, make_prompt_item, default_presentation, prompt_content, text_content
from phase1_agent.context_contract import (
    EFFECTIVE_CONTEXT_TYPE, advance_context, read_context_view, validate_agent_delta,
    validate_context_artifacts, validate_context_unit, validate_context_view, window_context,
)
from phase1_agent.context_package import (
    create_context_package, prepare_context_adoption, validate_context_adoption_proof,
)
from phase1_agent.context_prompt import assemble_context_prompt, validate_context_ready_prompt
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_execution import NodeExecutionContext
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.prompt_contract import validate_ready_prompt
from phase1_agent.prompt_package import create_prompt_package
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore


def uid(number):
    return str(UUID(int=number, version=4))


def ref(number):
    return {"scope": "artifact", "output_id": uid(number)}


def registry():
    return CapabilityPackageLoader((create_content_package(), create_prompt_package(),
                                    create_context_package())).load().registry


def unit(number, *, tool=False):
    messages = [{"message_id": uid(number + 100), "role": "assistant", "content": "answer"}]
    if tool:
        messages = [
            {"message_id": uid(number + 100), "role": "assistant", "content": "",
             "tool_calls": [{"id": "call-1", "name": "inspect", "arguments": "{}"},
                            {"id": "call-2", "name": "inspect", "arguments": "{}"}]},
            {"message_id": uid(number + 101), "role": "tool", "content": "one", "tool_call_id": "call-1",
             "status": "success", "is_error": False, "outcome_reason": None, "result": {"value": "one"}},
            {"message_id": uid(number + 102), "role": "tool", "content": "two", "tool_call_id": "call-2",
             "status": "success", "is_error": False, "outcome_reason": None, "result": {"value": "two"}},
            {"message_id": uid(number + 103), "role": "assistant", "content": "answer"},
        ]
    return {"schema_version": 1, "kind": "workflow.context-unit", "unit_id": uid(number),
            "source_kind": "accepted_execution", "root": {"role": "user", "content": "question"},
            "messages": messages, "source_refs": [ref(number + 200)]}


def record(*, value=None, revision=1):
    binding = ObjectBinding("context", EFFECTIVE_CONTEXT_TYPE, 1, "shared",
                            readers=(uid(3),), writers=(uid(3),)).to_dict()
    return {"revision_id": uid(50), "revision": revision, "type_id": EFFECTIVE_CONTEXT_TYPE,
            "schema_version": 1, "deleted": False, "binding": binding,
            "value": value or {"view_ref": None, "accepted_delta_ids": []}}


def view():
    return read_context_view(record(), uid(1), "context")


def delta(*, basis=60):
    return {"schema_version": 1, "kind": "workflow.agent-delta", "delta_id": uid(70),
            "owner": {"workflow_session_id": uid(1), "node_binding_id": uid(80),
                      "chain_run_id": uid(2), "node_run_id": uid(81)},
            "frozen_prompt_ref": ref(82), "basis_view_ref": ref(basis),
            "current_root_ref": ref(83), "unit_ref": ref(84), "unit": unit(10),
            "fact_receipt_refs": [ref(85)]}


def context(component, *, objects=None, artifacts=None, input_refs=None, host=None):
    types = registry()
    definition = types.get(component, "1").definition
    return NodeExecutionContext(
        definition=definition, node_binding_id=uid(3), workflow_session_id=uid(1),
        chain_run_id=uid(2), node_run_id=uid(4), state={}, private_states={}, external_inputs={},
        object_states=objects or {"context": record()}, type_registry=types.data_types,
        input_refs=input_refs or {"view": [{"edge_id": uid(90), "output_id": uid(60), "order": 0}]},
        host=host or (lambda ctx, cap, op, payload: validate_context_artifacts(
            payload, lambda artifact: deepcopy(artifacts[artifact["output_id"]]))),
    )


def execute(component, config, inputs, ctx):
    return registry().get(component, "1").executor(config, inputs, ctx)["output"]


def test_loads_independent_package_exact_versions_and_protected_types():
    types = registry()
    assert types.get("workflow.agent", "1") is None
    assert types.data_types.get("PROMPT", 2, scope="content")
    assert types.data_types.get("PROMPT", 3, scope="content")
    assert {entry["component_id"] for entry in types.catalog() if entry["component_id"].startswith("context.")} == {
        "context.read", "context.window", "context.project-text", "context.assembly", "context.advance", "context.save",
        "context.output", "context.merge"}
    for identity, value in (("CONTEXT_VIEW", view()), ("AGENT_DELTA", delta()),
                            ("CONTEXT_UNIT", unit(10, tool=True))):
        with pytest.raises(ContractValidationError) as caught:
            types.data_types.transform_content(identity, 1, value, lambda text: "forged", scope="all")
        assert caught.value.reason_code == "host_transform_unavailable"


@pytest.mark.parametrize("mutation", [
    lambda value: value["messages"].pop(1),
    lambda value: value["messages"].reverse(),
    lambda value: value["messages"][2].update(tool_call_id="call-1"),
    lambda value: value["messages"][0]["tool_calls"].append(
        {"id": "call-1", "name": "inspect", "arguments": "{}"}),
    lambda value: value["messages"].pop(),
    lambda value: value["messages"][1].update(message_id=value["messages"][0]["message_id"]),
])
def test_closed_unit_protocol_rejects_partial_reordered_or_duplicate_facts(mutation):
    value = unit(10, tool=True)
    mutation(value)
    with pytest.raises(ContractValidationError):
        validate_context_unit(value)


def test_manual_source_cannot_claim_generated_protocol():
    value = unit(10)
    value["source_kind"] = "manual"
    with pytest.raises(ContractValidationError) as caught:
        validate_context_unit(value)
    assert caught.value.reason_code == "context_manual_protocol_forbidden"


@pytest.mark.parametrize("reason", ["never_started", "interrupted", "outcome_unknown"])
def test_tool_observation_remains_typed_error_in_context_and_cannot_claim_success(reason):
    value = unit(10, tool=True)
    message = value["messages"][1]
    message.update(status="error", is_error=True, outcome_reason=reason, result={"reason": reason})
    assert validate_context_unit(value)["messages"][1]["outcome_reason"] == reason
    changed = deepcopy(value)
    changed["messages"][1].update(status="success", is_error=False)
    with pytest.raises(ContractValidationError) as caught:
        validate_context_unit(changed)
    assert caught.value.reason_code == "context_invalid_tool_outcome"


def test_missing_typed_tool_outcome_is_rejected_instead_of_guessed_from_display_text():
    value = unit(10, tool=True)
    value["messages"][1]["content"] = '{"reason":"outcome_unknown"}'
    del value["messages"][1]["status"]
    with pytest.raises(ContractValidationError) as caught:
        validate_context_unit(value)
    assert caught.value.reason_code == "context_invalid_tool_outcome"


def test_window_then_advance_preserves_selection_and_consumed_identity_boundary():
    initial = view()
    initial["units"] = [{"unit_ref": ref(100 + i), "unit": unit(20 + i)} for i in range(3)]
    initial["accepted_delta_ids"] = [uid(200)]
    selected = window_context(initial, ref(61), last_units=1)
    before = deepcopy(selected)
    updated = advance_context(selected, ref(60), delta(), ref(71))
    assert selected == before
    assert [entry["unit"]["unit_id"] for entry in updated["units"]] == [uid(22), uid(10)]
    assert updated["accepted_delta_ids"] == [uid(200)]
    assert updated["applied_delta_ids"] == [uid(70)]
    assert updated["basis"] == initial["basis"]
    assert window_context(updated, ref(72), last_units=0)["applied_delta_ids"] == [uid(70)]


def test_advance_rejects_old_unwindowed_basis_and_duplicate_consumption():
    with pytest.raises(ContractValidationError) as caught:
        advance_context(view(), ref(61), delta(), ref(71))
    assert caught.value.reason_code == "context_delta_basis_mismatch"
    current = view()
    current["accepted_delta_ids"] = [uid(70)]
    with pytest.raises(ContractValidationError) as caught:
        advance_context(current, ref(60), delta(), ref(71))
    assert caught.value.reason_code == "context_delta_already_consumed"
    other = delta()
    other["owner"]["workflow_session_id"] = uid(9)
    with pytest.raises(ContractValidationError):
        advance_context(view(), ref(60), other, ref(71))


def test_history_assembly_preserves_protocol_and_places_depth_one_before_current_root():
    history = view()
    history["units"] = [{"unit_ref": ref(84), "unit": unit(10, tool=True)}]
    presentation = {**default_presentation(), "placement": "middle", "depth": 1}
    item = make_prompt_item(uid(3), uid(4), "at historical boundary", presentation,
                            source={"kind": "fixture"})
    ready = assemble_context_prompt([prompt_content([item])], history, text_content("new root"),
                                    context_ref=ref(60), current_input_ref=ref(83))
    assert [message["role"] for message in ready["messages"]] == [
        "user", "assistant", "tool", "tool", "assistant", "system", "user"]
    assert ready["messages"][1]["tool_calls"][0]["function"]["name"] == "inspect"
    assert ready["current_input"] == {"role": "user", "content": "new root"}
    assert validate_context_ready_prompt(ready) == ready
    tampered = deepcopy(ready)
    tampered["messages"][3]["content"] = "forged result"
    with pytest.raises(ContractValidationError):
        validate_context_ready_prompt(tampered)
    with pytest.raises(ContractValidationError):
        validate_ready_prompt(ready)


def test_role_cards_do_not_enter_ready_v3():
    item = make_prompt_item(uid(3), uid(4), "text", default_presentation(), source={"kind": "fixture"})
    item["purpose"] = "role-card"
    with pytest.raises(ContractValidationError):
        assemble_context_prompt([prompt_content([item])], view(), text_content("q"),
                                context_ref=ref(60), current_input_ref=ref(83))


def test_artifact_validation_checks_exact_unit_and_frozen_input_evidence():
    current, change = view(), delta()
    ready = assemble_context_prompt([], current, text_content("question"),
                                    context_ref=ref(60), current_input_ref=ref(83))
    artifacts = {uid(60): current, uid(71): change, uid(82): ready,
                 uid(83): text_content("question"), uid(84): change["unit"]}
    payload = {"view": current, "view_ref": ref(60), "delta": change, "delta_ref": ref(71)}
    resolver = lambda artifact: artifacts[artifact["output_id"]]
    assert validate_context_artifacts(payload, resolver)["applied_delta_ids"] == [uid(70)]
    artifacts[uid(83)] = text_content("original, before actual transform")
    with pytest.raises(ContractValidationError) as caught:
        validate_context_artifacts(payload, resolver)
    assert caught.value.reason_code == "context_delta_prompt_mismatch"
    artifacts[uid(83)] = text_content("question")
    artifacts[uid(84)] = unit(11)
    with pytest.raises(ContractValidationError) as caught:
        validate_context_artifacts(payload, resolver)
    assert caught.value.reason_code == "context_artifact_mismatch"


def test_read_records_permissions_and_rebinds_retained_owner_without_mutating_facts():
    retained = advance_context(view(), ref(60), delta(), ref(71))
    old = record(value={"view_ref": ref(72), "accepted_delta_ids": [uid(70)]})
    ctx = context("context.read", objects={"context": old},
                  host=lambda ctx, cap, op, payload: {"object": deepcopy(old), "view": retained})
    actual = execute("context.read", {"object_key": "context"}, {}, ctx)
    assert actual["accepted_delta_ids"] == [uid(70)] and actual["applied_delta_ids"] == []
    assert actual["units"] == retained["units"]
    assert ctx.reads[0]["revision_id"] == uid(50)
    fork = read_context_view(old, uid(99), "context", retained)
    assert fork["owner"]["workflow_session_id"] == uid(99)
    assert fork["units"][0]["unit_ref"] == ref(84)


def test_read_cannot_borrow_private_owner():
    denied = record()
    denied["binding"] = ObjectBinding("context", EFFECTIVE_CONTEXT_TYPE, 1, "private",
                                     readers=(uid(9),), writers=(uid(9),), owner_node_id=uid(9)).to_dict()
    ctx = context("context.read", objects={"context": denied})
    with pytest.raises(ContractValidationError) as caught:
        execute("context.read", {"object_key": "context"}, {}, ctx)
    assert caught.value.reason_code == "session_object_access_denied"


def test_project_text_is_explicit_read_only_and_empty_context_is_valid():
    initial = view()
    ctx = context("context.project-text", artifacts={uid(60): initial})
    assert execute("context.project-text", {}, {"view": initial}, ctx) == text_content("")
    retained = deepcopy(initial)
    retained["units"] = [{"unit_ref": ref(84), "unit": unit(10, tool=True)}]
    current = read_context_view(record(value={"view_ref": ref(61), "accepted_delta_ids": []}),
                                uid(1), "context", retained)
    original = deepcopy(current)
    ctx = context("context.project-text", artifacts={uid(60): current, uid(61): retained,
                                                    uid(84): retained["units"][0]["unit"]})
    projection = execute("context.project-text", {}, {"view": current}, ctx)
    assert "user: question" in projection["text"] and "assistant: answer" in projection["text"]
    assert '"tool_calls"' in projection["text"] and "tool_call_id: call-2" in projection["text"]
    assert current == original and not ctx.object_writes


def test_save_checks_both_basis_fences_and_stages_stable_public_write_only():
    current = view()
    ctx = context("context.save", artifacts={uid(60): current})
    result = execute("context.save", {"object_key": "context"}, {"view": current}, ctx)
    assert result["status"] == "write_intent"
    assert len(ctx.object_writes) == 1
    assert ctx.object_writes[0]["value"] == {"view_ref": ref(60), "accepted_delta_ids": []}
    retry = context("context.save", artifacts={uid(60): current})
    execute("context.save", {"object_key": "context"}, {"view": current}, retry)
    assert retry.object_writes[0]["operation_key"] == ctx.object_writes[0]["operation_key"]
    for basis in ({"revision_id": uid(51), "head_revision": 1},
                  {"revision_id": uid(50), "head_revision": 2}):
        stale = deepcopy(current)
        stale["basis"] = basis
        ctx = context("context.save", artifacts={uid(60): stale})
        with pytest.raises(ContractValidationError) as caught:
            execute("context.save", {"object_key": "context"}, {"view": stale}, ctx)
        assert caught.value.reason_code == "context_stale_basis"
        assert not ctx.object_writes


def test_save_failure_is_retried_with_same_candidate_and_settled_save_is_idempotent():
    candidate = view()
    def fail(ctx, capability, operation, payload):
        raise OSError("temporary artifact read fault")
    failed = context("context.save", host=fail)
    with pytest.raises(OSError):
        execute("context.save", {"object_key": "context"}, {"view": candidate}, failed)
    assert not failed.object_writes
    settled = record(value={"view_ref": ref(60), "accepted_delta_ids": []}, revision=2)
    retry = context("context.save", objects={"context": settled}, artifacts={uid(60): candidate})
    assert execute("context.save", {"object_key": "context"}, {"view": candidate}, retry)["status"] == "already_committed"
    assert not retry.object_writes


def test_registered_object_default_reopen_and_fork_permissions(tmp_path):
    database = tmp_path / "context.sqlite"
    types = registry().data_types
    binding = ObjectBinding("context", EFFECTIVE_CONTEXT_TYPE, 1, "shared",
                            readers=(uid(3),), writers=(uid(3),)).to_dict()
    with closing(SqliteStore(database)) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        objects = SessionObjectStore(store, types)
        objects.initialize(uid(1), {"object_bindings": [binding]})
        parent = objects.current(uid(1))
        store._connection.commit()
    with closing(SqliteStore(database)) as store:
        objects = SessionObjectStore(store, types)
        assert objects.read(uid(1), "context", node_id=uid(3))["value"] == {
            "view_ref": None, "accepted_delta_ids": []}
        with pytest.raises(ContractValidationError):
            objects.read(uid(1), "context", node_id=uid(9))
        store._connection.execute("BEGIN IMMEDIATE")
        objects.initialize(uid(99), {"object_bindings": [binding]}, inherited=parent,
                           source_session_id=uid(1))
        store._connection.commit()
        assert objects.read(uid(99), "context", node_id=uid(3))["revision"] == 1
        assert objects.read(uid(99), "context", node_id=uid(3))["revision_id"] == parent["context"]["revision_id"]


def test_adoption_helper_has_same_cas_consumption_and_stable_intent_as_save_node():
    candidate = view()
    prepared = prepare_context_adoption(workflow_session_id=uid(1), object_key="context",
                                        object_record=record(), view_ref=ref(60), view=candidate)
    ctx = context("context.save", artifacts={uid(60): candidate})
    execute("context.save", {"object_key": "context"}, {"view": candidate}, ctx)
    assert prepared["write_intent"] == ctx.object_writes[0]
    assert not prepared["already_committed"]
    for owner in (uid(99), uid(1)):
        with pytest.raises(ContractValidationError):
            prepare_context_adoption(workflow_session_id=owner, object_key="different",
                                     object_record=record(), view_ref=ref(60), view=candidate)


def test_adoption_proof_accepts_official_exact_advance_and_rejects_typed_json_source():
    initial, change = view(), delta()
    ready = assemble_context_prompt([], initial, text_content("question"),
                                    context_ref=ref(60), current_input_ref=ref(83))
    candidate = advance_context(initial, ref(60), change, ref(71))
    artifacts = {uid(60): initial, uid(71): change, uid(72): candidate,
                 uid(82): ready, uid(83): text_content("question"), uid(84): change["unit"]}
    resolve = lambda artifact: artifacts[artifact["output_id"]]
    assert validate_context_adoption_proof(ref(72), candidate, resolve, "context.advance", {}) == candidate
    with pytest.raises(ContractValidationError) as caught:
        validate_context_adoption_proof(ref(72), candidate, resolve, "test.json-to-view", {})
    assert caught.value.reason_code == "context_adoption_source_invalid"
    with pytest.raises(ContractValidationError):
        validate_context_adoption_proof(ref(72), candidate, resolve, "context.window", {"last_units": 1})
