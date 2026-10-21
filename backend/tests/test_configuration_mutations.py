"""Narrow configuration mutations use trusted permissions and one transaction."""

from contextlib import closing
from uuid import uuid4

import pytest

from phase1_agent.configuration_mutations import (
    ConfigurationMutationRule, configuration_mutation_rule, validate_editable_input_configuration,
)
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_node_operations import FrontendNodeOperations
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.workflow_configuration import (
    MAX_EDIT_SEQUENCE, default_configuration_policy, validate_adjustable_configuration,
)

from frontend_node_test_support import graph as frontend_graph, save as _save
from test_workflow_configuration import declaration as prompt_declaration


def input_declaration():
    return {"schema_version": 1, "policy": "input", "fields": ["text"]}


def document(*, mode="adjustable", text="A"):
    """Complete schema 3 document consumed by the workbench-source interface test."""
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
    with closing(GraphWorkflowService(tmp_path / "mutations.sqlite")) as instance:
        yield instance


def prepared(service, *, fixed=False, count=1):
    """Save a frontend-node graph whose message node is the editable input target."""
    graph, messages, frontends = frontend_graph(service, count=count, bound=True)
    if fixed:
        messages[0]["configuration_policy"]["prompt_mode"] = "fixed"
    return _save(service, graph), messages, frontends


def bind(service, graph, frontend, view=None):
    return FrontendNodeOperations(service).bind(
        workflow_definition_id=graph["workflow_definition_id"],
        definition_revision=graph["revision"],
        node_binding_id=frontend["node_binding_id"],
        session_id=view["workflow_session_id"] if view is not None else None)


def create_session(service, graph):
    return service.create_session(
        graph["workflow_definition_id"], graph["revision"], idempotency_key=str(uuid4()))


def patch(bound, *, text="B", sequence=1, key=None, **overrides):
    return bound.patch_input(
        text, expected_edit_sequence=sequence,
        idempotency_key=key or str(uuid4()), **overrides)


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
        return store.read_receipt("graph.frontend-node.patch-input", key)


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
    graph, messages, frontends = prepared(service)
    session = create_session(service, graph)
    bound = bind(service, graph, frontends[0], session)
    before_session = service.get_session(session["workflow_session_id"])
    text = "  \r\n\u539f\u59cb\u8f93\u5165\n\t"
    result = patch(bound, text=text, sequence=1, key="save-text")
    assert (result["outcome"], result["edit_sequence"], result["definition_revision"]) == ("saved", 2, 1)
    assert result["changed_categories"] == ["adjustable"]
    assert result["source_has_effective_session"] is True
    assert result["document"]["nodes"][0]["config"] == {"text": text}
    assert service.get_definition(graph["workflow_definition_id"], 1)["nodes"][0]["config"] == {
        "text": "message 0"}
    assert service.get_session(session["workflow_session_id"]) == before_session
    assert receipt(service, "save-text") == [{"graph_result": result}]
    unchanged = patch(bound, text=text, sequence=2)
    assert (unchanged["outcome"], unchanged["edit_sequence"]) == ("unchanged", 2)
    assert unchanged["changed_categories"] == []
    empty = patch(bound, text="", sequence=2)
    assert empty["document"]["nodes"][0]["config"] == {"text": ""}


def test_replay_uses_original_narrow_request_after_graph_changes(service):
    graph, messages, frontends = prepared(service)
    bound = bind(service, graph, frontends[0])
    result = patch(bound, key="original")
    latest = patch(bound, text="C", sequence=2)
    assert latest["edit_sequence"] == 3
    before = snapshot(service)
    assert patch(bound, key="original") == result
    assert snapshot(service) == before
    assert service.get_definition(graph["workflow_definition_id"]) == latest["document"]


@pytest.mark.parametrize("overrides", [
    {"text": "different"}, {"sequence": 2}, {"confirm_derivation": False},
])
def test_same_key_different_narrow_request_is_conflict(service, overrides):
    graph, messages, frontends = prepared(service)
    bound = bind(service, graph, frontends[0])
    patch(bound, key="identity")
    before = snapshot(service)
    with pytest.raises(ContractValidationError) as error:
        patch(bound, key="identity", **overrides)
    assert error.value.reason_code == "idempotency_conflict"
    assert snapshot(service) == before


def test_fixed_patch_requires_confirmation_then_derives_with_new_key(service):
    graph, messages, frontends = prepared(service, fixed=True)
    session = create_session(service, graph)
    bound = bind(service, graph, frontends[0], session)
    before = snapshot(service)
    before_session = service.get_session(session["workflow_session_id"])
    asked = patch(bound, key="ask")
    assert (asked["outcome"], asked["edit_sequence"], asked["definition_revision"]) == (
        "confirmation_required", 1, 1)
    assert asked["changed_categories"] == ["fixed"]
    assert snapshot(service)[:2] == before[:2]
    assert len(snapshot(service)[2]) == len(before[2]) + 1
    with pytest.raises(ContractValidationError) as error:
        patch(bound, key="ask", confirm_derivation=True)
    assert error.value.reason_code == "idempotency_conflict"
    result = patch(bound, key="confirm", confirm_derivation=True)
    assert result["outcome"] == "derived"
    assert result["workflow_definition_id"] != graph["workflow_definition_id"]
    assert (result["edit_sequence"], result["definition_revision"]) == (1, 1)
    assert result["document"]["nodes"][0]["config"] == {"text": "B"}
    assert result["document"]["nodes"][0]["configuration_policy"]["prompt_mode"] == "fixed"
    assert result["node_id_map"] == {
        node["node_binding_id"]: node["node_binding_id"] for node in graph["nodes"]}
    assert service.list_sessions(result["workflow_definition_id"]) == []
    assert service.get_definition(graph["workflow_definition_id"]) == graph
    assert service.get_session(session["workflow_session_id"]) == before_session
    before_replay = snapshot(service)
    assert patch(bound, key="ask") == asked
    assert patch(bound, key="confirm", confirm_derivation=True) == result
    assert snapshot(service) == before_replay


def test_fixed_patch_without_session_uses_existing_revision_path(service):
    graph, messages, frontends = prepared(service, fixed=True)
    bound = bind(service, graph, frontends[0])
    result = patch(bound)
    assert (result["outcome"], result["edit_sequence"], result["definition_revision"]) == ("saved", 2, 2)
    assert result["workflow_definition_id"] == graph["workflow_definition_id"]
    assert result["document"]["nodes"][0]["configuration_policy"]["prompt_mode"] == "fixed"


def test_stale_request_does_not_rebase_or_write_receipt(service):
    graph, messages, frontends = prepared(service)
    bound = bind(service, graph, frontends[0])
    latest = patch(bound)
    before = snapshot(service)
    with pytest.raises(ContractValidationError) as error:
        patch(bound, text="C", sequence=1, key="stale")
    assert error.value.reason_code == "stale_edit_sequence"
    assert snapshot(service) == before
    assert receipt(service, "stale") is None
    assert service.get_definition(graph["workflow_definition_id"]) == latest["document"]


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"text": None}, "graph_invalid_config"),
    ({"text": False}, "graph_invalid_config"),
    ({"text": 1}, "graph_invalid_config"),
    ({"text": "x" * 1_000_001}, "graph_invalid_config"),
    ({"sequence": True}, "invalid_request"),
    ({"sequence": -1}, "invalid_request"),
    ({"sequence": MAX_EDIT_SEQUENCE + 1}, "invalid_request"),
    ({"confirm_derivation": 1}, "invalid_request"),
])
def test_invalid_patch_has_no_business_or_receipt_writes(service, overrides, reason):
    graph, messages, frontends = prepared(service)
    bound = bind(service, graph, frontends[0])
    before = snapshot(service)
    with pytest.raises(ContractValidationError) as error:
        patch(bound, key="invalid", **overrides)
    assert error.value.reason_code == reason
    assert snapshot(service) == before
    assert receipt(service, "invalid") is None


def test_patch_affects_only_exact_node_in_exact_workflow(service):
    graph, messages, frontends = prepared(service, count=2)
    other_graph, _, _ = prepared(service)
    bound = bind(service, graph, frontends[0])
    result = patch(bound)
    assert result["document"]["nodes"][1] == graph["nodes"][1]
    assert service.get_definition(other_graph["workflow_definition_id"]) == other_graph


@pytest.mark.parametrize("failure", ["after_configuration_write", "before_commit"])
def test_patch_configuration_and_receipt_rollback_together(tmp_path, failure):
    armed = False

    def fault(point):
        if armed and point == failure:
            raise RuntimeError("injected patch failure")

    instance = GraphWorkflowService(tmp_path / "rollback.sqlite", fault_injector=fault)
    try:
        graph, messages, frontends = prepared(instance)
        bound = bind(instance, graph, frontends[0])
        before = snapshot(instance)
        armed = True
        with pytest.raises(RuntimeError, match="injected patch failure"):
            patch(bound, key="rollback")
        armed = False
        assert snapshot(instance) == before
        assert receipt(instance, "rollback") is None
        result = patch(bound, key="rollback")
        assert (result["outcome"], result["edit_sequence"]) == ("saved", 2)
    finally:
        instance.close()


def test_text_limit_counts_python_characters_and_rejects_invalid_configuration_shape(service):
    graph, messages, frontends = prepared(service)
    bound = bind(service, graph, frontends[0])
    value = "\u5b57" * 1_000_000
    result = patch(bound, text=value)
    assert result["document"]["nodes"][0]["config"]["text"] == value
    for invalid in ({}, {"text": "", "other": True}, {"text": ["text"]}):
        with pytest.raises(ContractValidationError) as error:
            validate_editable_input_configuration(invalid, node_id=messages[0]["node_binding_id"])
        assert error.value.reason_code == "graph_invalid_config"
