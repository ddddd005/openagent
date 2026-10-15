"""Narrow configuration mutations use trusted permissions and one transaction."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.configuration_mutations import (
    ConfigurationMutationRule, configuration_mutation_rule, validate_editable_input_configuration,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.workflow_configuration import (
    MAX_EDIT_SEQUENCE, default_configuration_policy, validate_adjustable_configuration,
)
from test_workflow_configuration import declaration as prompt_declaration
from test_workflow_configuration import registry as prompt_registry


def input_declaration():
    return {"schema_version": 1, "policy": "input", "fields": ["text"]}


def registry():
    value = prompt_registry()
    value.register(NodeDefinition(
        "tools.editable-input", "1", "Editable input", "Test", {"text": ""},
        {"type": "object", "properties": {"text": {"type": "string", "maxLength": 1_000_000}},
         "required": ["text"], "additionalProperties": False},
        adjustable_configuration=input_declaration()),
        lambda config, inputs, context: {})
    return value


def document(*, mode="adjustable", text="A"):
    return {
        "schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Narrow mutation test", "edges": [], "object_bindings": [],
        "nodes": [{
            "node_binding_id": str(uuid4()), "component_id": "tools.editable-input",
            "component_version": "1", "title": "Input", "position": {"x": 0, "y": 0},
            "configuration_policy": {"schema_version": 1, "prompt_mode": mode}, "config": {"text": text},
        }],
    }


@pytest.fixture
def service(tmp_path):
    instance = GraphWorkflowService(tmp_path / "mutations.sqlite", registry=registry())
    try:
        yield instance
    finally:
        instance.close()


def save(service, doc):
    return service.update_definition(
        doc, workflow_definition_id=doc["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))


def patch(service, saved, *, text="B", sequence=None, key=None, **overrides):
    node = saved["document"]["nodes"][0]
    arguments = {
        "workflow_definition_id": saved["workflow_definition_id"],
        "node_binding_id": node["node_binding_id"], "component_id": node["component_id"],
        "component_version": node["component_version"], "changes": {"text": text},
        "expected_edit_sequence": saved["edit_sequence"] if sequence is None else sequence,
        "idempotency_key": key or str(uuid4()),
    }
    arguments.update(overrides)
    return service.patch_node_configuration(**arguments)


def snapshot(service):
    with closing(service._store()) as store:
        return tuple(
            tuple(tuple(row) for row in store._connection.execute(query))
            for query in (
                "SELECT * FROM records ORDER BY record_type,record_id",
                "SELECT * FROM workflow_current_configuration ORDER BY workflow_definition_id",
                "SELECT * FROM idempotency ORDER BY operation,key",
            ))


def receipt(service, key):
    with closing(service._store()) as store:
        return store.read_receipt("graph.configuration.patch-node", key)


def test_default_policy_comes_from_exact_trusted_declaration():
    assert default_configuration_policy("tools.editable-input", "1", input_declaration()) == {
        "schema_version": 1, "prompt_mode": "adjustable"}
    assert default_configuration_policy("prompts.item", "3", prompt_declaration()) == {
        "schema_version": 1, "prompt_mode": "fixed"}
    assert default_configuration_policy("tools.text", "1", None) == {
        "schema_version": 1, "prompt_mode": "fixed"}
    with pytest.raises(ContractValidationError) as error:
        default_configuration_policy("tools.editable-input", "2", input_declaration())
    assert error.value.reason_code == "graph_invalid_adjustable_declaration"
    assert validate_adjustable_configuration("tools.editable-input", "1", input_declaration()) == input_declaration()


def test_mutation_permissions_are_independent_of_adjustable_declarations():
    assert configuration_mutation_rule("tools.editable-input", "1").fields == ("text",)
    assert default_configuration_policy("prompts.item", "3", prompt_declaration())["prompt_mode"] == "fixed"
    with pytest.raises(ContractValidationError) as error:
        configuration_mutation_rule("prompts.item", "3")
    assert error.value.reason_code == "graph_configuration_mutation_not_allowed"


def test_partial_permission_check_does_not_validate_a_complete_configuration():
    validated = []
    rule = ConfigurationMutationRule(
        fields=("first", "second"),
        validate_configuration=lambda value, **kwargs: validated.append(value))
    rule.validate_changes({"first": "changed"}, node_id=str(uuid4()))
    assert validated == []
    with pytest.raises(ContractValidationError) as error:
        rule.validate_changes({"other": "changed"}, node_id=str(uuid4()))
    assert error.value.reason_code == "graph_invalid_configuration_patch"


def test_adjustable_patch_with_session_saves_exact_text_and_unchanged_receipt(service):
    saved = save(service, document())
    session = service.create_session(
        saved["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    before_session = service.get_session(session["workflow_session_id"])
    text = "  \r\n\u539f\u59cb\u8f93\u5165\n\t"
    result = patch(service, saved, text=text, key="save-text")
    assert (result["outcome"], result["edit_sequence"], result["definition_revision"]) == ("saved", 2, 1)
    assert result["changed_categories"] == ["adjustable"]
    assert result["source_has_effective_session"] is True
    assert result["document"]["nodes"][0]["config"] == {"text": text}
    assert service.get_definition(saved["workflow_definition_id"], 1)["nodes"][0]["config"] == {"text": "A"}
    assert service.get_session(session["workflow_session_id"]) == before_session
    assert receipt(service, "save-text") == [{"graph_result": result}]
    unchanged = patch(service, result, text=text)
    assert (unchanged["outcome"], unchanged["edit_sequence"]) == ("unchanged", 2)
    assert unchanged["changed_categories"] == []
    empty = patch(service, unchanged, text="")
    assert empty["document"]["nodes"][0]["config"] == {"text": ""}


def test_replay_uses_original_narrow_request_after_graph_changes(service):
    saved = save(service, document())
    result = patch(service, saved, key="original")
    latest = patch(service, result, text="C")
    assert latest["edit_sequence"] == 3
    before = snapshot(service)
    assert patch(service, saved, key="original") == result
    assert snapshot(service) == before
    assert service.get_definition(saved["workflow_definition_id"]) == latest["document"]


def test_patch_snapshots_caller_changes_before_transaction(monkeypatch, service):
    from phase1_agent import graph_store

    saved = save(service, document())
    caller_changes = {"text": "original"}
    original_digest = graph_store.content_digest

    def digest_then_edit(request):
        digest = original_digest(request)
        caller_changes["text"] = "late caller edit"
        return digest

    monkeypatch.setattr(graph_store, "content_digest", digest_then_edit)
    result = patch(service, saved, changes=caller_changes, key="snapshot")
    assert caller_changes == {"text": "late caller edit"}
    assert result["document"]["nodes"][0]["config"] == {"text": "original"}
    assert receipt(service, "snapshot") == [{"graph_result": result}]
    assert patch(service, saved, changes={"text": "original"}, key="snapshot") == result
    before = snapshot(service)
    with pytest.raises(ContractValidationError) as error:
        patch(service, saved, changes=caller_changes, key="snapshot")
    assert error.value.reason_code == "idempotency_conflict"
    assert snapshot(service) == before


@pytest.mark.parametrize("overrides", [
    {"text": "different"}, {"node_binding_id": str(uuid4())},
    {"component_id": "tools.text"}, {"component_version": "2"},
    {"sequence": 2}, {"confirm_derivation": False},
])
def test_same_key_different_narrow_request_is_conflict(service, overrides):
    saved = save(service, document())
    patch(service, saved, key="identity")
    before = snapshot(service)
    with pytest.raises(ContractValidationError) as error:
        patch(service, saved, key="identity", **overrides)
    assert error.value.reason_code == "idempotency_conflict"
    assert snapshot(service) == before


def test_fixed_patch_requires_confirmation_then_derives_with_new_key(service):
    saved = save(service, document(mode="fixed"))
    session = service.create_session(
        saved["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    before = snapshot(service)
    before_session = service.get_session(session["workflow_session_id"])
    asked = patch(service, saved, key="ask")
    assert (asked["outcome"], asked["edit_sequence"], asked["definition_revision"]) == (
        "confirmation_required", 1, 1)
    assert asked["changed_categories"] == ["fixed"]
    assert snapshot(service)[:2] == before[:2]
    assert len(snapshot(service)[2]) == len(before[2]) + 1
    with pytest.raises(ContractValidationError) as error:
        patch(service, saved, key="ask", confirm_derivation=True)
    assert error.value.reason_code == "idempotency_conflict"
    result = patch(service, saved, key="confirm", confirm_derivation=True)
    assert result["outcome"] == "derived"
    assert result["workflow_definition_id"] != saved["workflow_definition_id"]
    assert (result["edit_sequence"], result["definition_revision"]) == (1, 1)
    assert result["document"]["nodes"][0]["config"] == {"text": "B"}
    assert result["document"]["nodes"][0]["configuration_policy"]["prompt_mode"] == "fixed"
    node_id = saved["document"]["nodes"][0]["node_binding_id"]
    assert result["node_id_map"] == {node_id: node_id}
    assert service.list_sessions(result["workflow_definition_id"]) == []
    assert service.get_definition(saved["workflow_definition_id"]) == saved["document"]
    assert service.get_session(session["workflow_session_id"]) == before_session
    before_replay = snapshot(service)
    assert patch(service, saved, key="ask") == asked
    assert patch(service, saved, key="confirm", confirm_derivation=True) == result
    assert snapshot(service) == before_replay


def test_fixed_patch_without_session_uses_existing_revision_path(service):
    saved = save(service, document(mode="fixed"))
    result = patch(service, saved)
    assert (result["outcome"], result["edit_sequence"], result["definition_revision"]) == ("saved", 2, 2)
    assert result["workflow_definition_id"] == saved["workflow_definition_id"]
    assert result["document"]["nodes"][0]["configuration_policy"]["prompt_mode"] == "fixed"


def test_stale_request_does_not_rebase_or_write_receipt(service):
    saved = save(service, document())
    latest = patch(service, saved)
    before = snapshot(service)
    with pytest.raises(ContractValidationError) as error:
        patch(service, saved, text="C", key="stale")
    assert error.value.reason_code == "stale_edit_sequence"
    assert snapshot(service) == before
    assert receipt(service, "stale") is None
    assert service.get_definition(saved["workflow_definition_id"]) == latest["document"]


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"changes": {}}, "graph_invalid_configuration_patch"),
    ({"changes": []}, "graph_invalid_configuration_patch"),
    ({"changes": {"text": "B", "prompt_mode": "fixed"}}, "graph_invalid_configuration_patch"),
    ({"changes": {"configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"}}},
     "graph_invalid_configuration_patch"),
    ({"changes": {"text": None}}, "graph_invalid_config"),
    ({"changes": {"text": False}}, "graph_invalid_config"),
    ({"changes": {"text": 1}}, "graph_invalid_config"),
    ({"changes": {"text": "x" * 1_000_001}}, "graph_invalid_config"),
    ({"component_id": "tools.text"}, "graph_configuration_mutation_not_allowed"),
    ({"component_id": "prompts.item", "component_version": "3"}, "graph_configuration_mutation_not_allowed"),
    ({"component_version": "2"}, "graph_configuration_mutation_not_allowed"),
    ({"component_version": 1}, "invalid_request"),
    ({"node_binding_id": str(uuid4())}, "graph_missing_node"),
    ({"sequence": True}, "invalid_request"),
    ({"sequence": -1}, "invalid_request"),
    ({"sequence": MAX_EDIT_SEQUENCE + 1}, "invalid_request"),
    ({"confirm_derivation": 1}, "invalid_request"),
])
def test_invalid_patch_has_no_business_or_receipt_writes(service, overrides, reason):
    saved = save(service, document())
    before = snapshot(service)
    with pytest.raises(ContractValidationError) as error:
        patch(service, saved, key="invalid", **overrides)
    assert error.value.reason_code == reason
    assert snapshot(service) == before
    assert receipt(service, "invalid") is None


def test_exact_requested_contract_must_match_target_binding(service):
    doc = document()
    second = deepcopy(doc["nodes"][0])
    second.update(node_binding_id=str(uuid4()), component_id="prompts.item", component_version="3",
                  config=deepcopy(service.registry.get_definition("prompts.item", "3").default_config))
    doc["nodes"].append(second)
    saved = save(service, doc)
    before = snapshot(service)
    with pytest.raises(ContractValidationError) as error:
        patch(service, saved, node_binding_id=second["node_binding_id"])
    assert error.value.reason_code == "graph_configuration_target_mismatch"
    assert snapshot(service) == before


def test_patch_affects_only_exact_node_in_exact_workflow(service):
    doc = document()
    second = deepcopy(doc["nodes"][0])
    second["node_binding_id"] = str(uuid4())
    second["config"]["text"] = "second"
    doc["nodes"].append(second)
    saved = save(service, doc)
    other = save(service, document(text="other"))
    result = patch(service, saved)
    assert result["document"]["nodes"][1] == saved["document"]["nodes"][1]
    assert service.get_definition(other["workflow_definition_id"]) == other["document"]


@pytest.mark.parametrize("failure", ["after_configuration_write", "before_commit"])
def test_patch_configuration_and_receipt_rollback_together(tmp_path, failure):
    armed = False

    def fault(point):
        if armed and point == failure:
            raise RuntimeError("injected patch failure")

    instance = GraphWorkflowService(
        tmp_path / "rollback.sqlite", registry=registry(), fault_injector=fault)
    try:
        saved = save(instance, document())
        before = snapshot(instance)
        armed = True
        with pytest.raises(RuntimeError, match="injected patch failure"):
            patch(instance, saved, key="rollback")
        armed = False
        assert snapshot(instance) == before
        assert receipt(instance, "rollback") is None
        result = patch(instance, saved, key="rollback")
        assert (result["outcome"], result["edit_sequence"]) == ("saved", 2)
    finally:
        instance.close()


def test_text_limit_counts_python_characters_and_rejects_invalid_configuration_shape(service):
    saved = save(service, document())
    value = "\u5b57" * 1_000_000
    result = patch(service, saved, text=value)
    assert result["document"]["nodes"][0]["config"]["text"] == value
    for invalid in ({}, {"text": "", "other": True}, {"text": ["text"]}):
        with pytest.raises(ContractValidationError) as error:
            validate_editable_input_configuration(invalid, node_id=saved["document"]["nodes"][0]["node_binding_id"])
        assert error.value.reason_code == "graph_invalid_config"
