"""Schema 3 editing against isolated stores and trusted synthetic registrations."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import NodeDefinition, NodeRegistry, validate_graph_document
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.prompt_lifecycle_nodes import lifecycle_member_schema, lifecycle_members_schema
from phase1_agent.content_contracts import object_schema
from phase1_agent.workflow_configuration import (
    PROMPT_CONFIGURATION_FIELDS, fixed_configuration_projection,
)
from test_graph_service import save_definition_fixture


def declaration(group=False):
    value = {"schema_version": 1, "policy": "prompt",
             "fields": ["members"] if group else list(PROMPT_CONFIGURATION_FIELDS)}
    if group:
        value.update(member_identity_field="id", member_fields=list(PROMPT_CONFIGURATION_FIELDS))
    return value


def member(text="A"):
    return {"id": str(uuid4()), "text": text, "metadata": {},
            "presentation": {"role": "system", "placement": "before", "depth": None,
                             "order": 0, "enabled": True},
            "lifecycle": "context_once", "compaction": "never"}


def registry(*, executable=True):
    value = NodeRegistry()
    default_member = member()
    default_member["id"] = "59e405d0-debd-4d7a-aa7f-ce6445d72665"
    for group in (False, True):
        value.register(NodeDefinition(
            "prompts.group" if group else "prompts.item", "3", "Synthetic prompt", "Test",
            {"members": []} if group else default_member,
            object_schema({"members": lifecycle_members_schema()}) if group else lifecycle_member_schema(),
            adjustable_configuration=declaration(group)),
            (lambda config, inputs, context: {}) if executable else None)
    return value


def document(*, group=False, mode="adjustable", component=None, config=None):
    node = {
        "node_binding_id": str(uuid4()), "component_id": component or ("prompts.group" if group else "prompts.item"),
        "component_version": "3", "title": "Synthetic node", "position": {"x": 10, "y": 20},
        "configuration_policy": {"schema_version": 1, "prompt_mode": mode},
        "config": deepcopy(config) if config is not None else {"members": [member()]} if group else member(),
    }
    return {"schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": "Configuration test", "nodes": [node], "edges": [], "object_bindings": []}


@pytest.fixture
def service(tmp_path):
    instance = GraphWorkflowService(tmp_path / "configuration.sqlite", registry=registry())
    try:
        yield instance
    finally:
        instance.close()


def update(service, doc, sequence=0, key=None, *, confirm=None):
    return service.update_definition(
        doc, workflow_definition_id=doc["workflow_definition_id"],
        expected_edit_sequence=sequence, idempotency_key=key or str(uuid4()),
        confirm_derivation=confirm)


def create_session(service, saved):
    return service.create_session(saved["workflow_definition_id"], saved["definition_revision"],
                                  idempotency_key=str(uuid4()))


def bundle(service):
    with closing(service._store()) as store:
        return store.read_bundle(include_graph=True)


def test_initial_save_and_unchanged_receipt(service):
    saved = update(service, document())
    assert (saved["outcome"], saved["edit_sequence"], saved["definition_revision"]) == ("saved", 1, 1)
    assert saved["changed_categories"] == ["fixed"]
    assert saved["source_has_effective_session"] is False
    assert saved["session_id"] is None
    same = update(service, saved["document"], 1)
    assert (same["outcome"], same["edit_sequence"], same["definition_revision"]) == ("unchanged", 1, 1)
    assert same["changed_categories"] == []
    assert len(bundle(service)["workflow_definition_revision"]) == 1
    assert "edit_sequence" not in service.get_definition(saved["workflow_definition_id"], 1)


def test_adjustable_save_uses_current_projection_preserving_fixed_seed(service):
    saved = update(service, document())
    session = create_session(service, saved)
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    changed["nodes"][0]["config"]["lifecycle"] = "per_request"
    after = update(service, changed, 1)
    assert (after["outcome"], after["definition_revision"], after["edit_sequence"]) == ("saved", 1, 2)
    assert after["changed_categories"] == ["adjustable"]
    assert after["source_has_effective_session"] is True
    assert service.get_definition(saved["workflow_definition_id"]) == after["document"]
    assert service.list_definitions() == [{"document": after["document"], "archived": False}]
    assert service.get_definition(saved["workflow_definition_id"], 1)["nodes"][0]["config"]["text"] == "A"
    assert service.get_session(session["workflow_session_id"])["definition_revision"] == 1


def test_display_only_save_does_not_derive_with_session(service):
    saved = update(service, document(mode="fixed"))
    create_session(service, saved)
    changed = deepcopy(saved["document"])
    changed["name"] = "Renamed"
    changed["nodes"][0].update(title="Title", position={"x": 100, "y": -25})
    after = update(service, changed, 1)
    assert after["outcome"] == "saved"
    assert after["changed_categories"] == ["display"]
    assert (after["definition_revision"], after["edit_sequence"]) == (1, 2)
    assert after["document"]["nodes"][0]["position"] == {"x": 100, "y": -25}
    assert service.get_definition(saved["workflow_definition_id"], 1)["name"] == "Configuration test"


def test_fixed_save_without_effective_session_advances_fixed_revision(service):
    saved = update(service, document(mode="fixed"))
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    after = update(service, changed, 1)
    assert (after["outcome"], after["definition_revision"], after["edit_sequence"]) == ("saved", 2, 2)
    assert after["workflow_definition_id"] == saved["workflow_definition_id"]
    assert after["changed_categories"] == ["fixed"]


@pytest.mark.parametrize("change", ["policy", "item_id", "structure", "binding", "unknown_config"])
def test_fixed_changes_ask_first_and_only_confirmed_consent_derives(service, change):
    saved = update(service, document())
    session = create_session(service, saved)
    source_before = service.get_session(session["workflow_session_id"])
    changed = deepcopy(saved["document"])
    node = changed["nodes"][0]
    if change == "policy":
        node["configuration_policy"]["prompt_mode"] = "fixed"
    elif change == "item_id":
        node["config"]["id"] = str(uuid4())
    elif change == "structure":
        changed["execution_roots"] = [node["node_binding_id"]]
    elif change == "binding":
        node["config"]["reference"] = {"resource_id": str(uuid4())}
    else:
        node["config"]["run_permissions"] = ["write"]
    asked = update(service, changed, 1)
    assert asked["outcome"] == "confirmation_required"
    assert (asked["definition_revision"], asked["edit_sequence"]) == (1, 1)
    assert asked["workflow_definition_id"] == saved["workflow_definition_id"]
    assert asked["source_has_effective_session"] is True
    assert "fixed" in asked["changed_categories"]
    assert asked["document"]["nodes"][0]["config"] == node["config"]
    # The ask wrote nothing: no new workflow, no new session, source unchanged.
    assert service.list_definitions() == [{"document": saved["document"], "archived": False}]
    assert service.get_definition(saved["workflow_definition_id"]) == saved["document"]
    assert service.get_session(session["workflow_session_id"]) == source_before
    after = update(service, changed, 1, confirm=True)
    assert after["outcome"] == "derived"
    assert (after["definition_revision"], after["edit_sequence"]) == (1, 1)
    assert after["workflow_definition_id"] != saved["workflow_definition_id"]
    assert after["source_has_effective_session"] is True
    assert "fixed" in after["changed_categories"]
    identity = node["node_binding_id"]
    assert after["node_id_map"] == {identity: identity}
    assert service.get_definition(saved["workflow_definition_id"]) == saved["document"]
    assert service.get_session(session["workflow_session_id"]) == source_before
    assert service.list_sessions(after["workflow_definition_id"]) == []


def test_mixed_derivation_is_atomic_and_target_contains_no_runtime_rows(service):
    saved = update(service, document())
    create_session(service, saved)
    before = bundle(service)
    changed = deepcopy(saved["document"])
    changed["name"] = "Derived"
    changed["nodes"][0]["config"].update(text="B", id=str(uuid4()))
    asked = update(service, changed, 1)
    assert asked["outcome"] == "confirmation_required"
    assert asked["changed_categories"] == ["display", "adjustable", "fixed"]
    assert bundle(service) == before
    after = update(service, changed, 1, confirm=True)
    assert after["changed_categories"] == ["display", "adjustable", "fixed"]
    assert after["document"]["nodes"][0]["config"]["text"] == "B"
    assert service.get_definition(saved["workflow_definition_id"]) == saved["document"]
    current = bundle(service)
    for kind in ("workflow_session", "node_session", "chain_run", "node_run",
                 "state_snapshot", "workflow_ref", "workflow_commit", "workflow_output"):
        assert current.get(kind, []) == before.get(kind, [])
    definitions = current["workflow_definition_revision"]
    assert {row["workflow_definition_id"] for row in definitions} == {
        saved["workflow_definition_id"], after["workflow_definition_id"]}


def test_group_add_remove_reorder_are_adjustable_with_stable_ids(service):
    saved = update(service, document(group=True))
    create_session(service, saved)
    changed = deepcopy(saved["document"])
    first = changed["nodes"][0]["config"]["members"][0]
    second = member("second")
    changed["nodes"][0]["config"]["members"] = [second, first]
    after = update(service, changed, 1)
    assert after["outcome"] == "saved"
    assert after["changed_categories"] == ["adjustable"]
    changed = deepcopy(after["document"])
    changed["nodes"][0]["config"]["members"] = [first]
    again = update(service, changed, 2)
    assert (again["outcome"], again["edit_sequence"], again["definition_revision"]) == ("saved", 3, 1)
    assert again["document"]["nodes"][0]["config"]["members"][0]["id"] == first["id"]


@pytest.mark.parametrize("group", [False, True])
def test_unreviewed_member_fields_are_fixed_even_inside_adjustable_field(service, group):
    saved = update(service, document(group=group))
    create_session(service, saved)
    changed = deepcopy(saved["document"])
    value = changed["nodes"][0]["config"]["members"][0] if group else changed["nodes"][0]["config"]
    value["presentation"]["execution_binding"] = {"node": str(uuid4())}
    asked = update(service, changed, 1)
    assert asked["outcome"] == "confirmation_required"
    assert "fixed" in asked["changed_categories"]
    after = update(service, changed, 1, confirm=True)
    assert after["outcome"] == "derived"
    assert "fixed" in after["changed_categories"]


def test_archived_sessions_allow_fixed_edit_and_old_fixed_history_survives(service):
    saved = update(service, document(mode="fixed"))
    session = create_session(service, saved)
    service.archive_session(session["workflow_session_id"], expected_revision=session["revision"],
                            idempotency_key=str(uuid4()))
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    after = update(service, changed, 1)
    assert (after["outcome"], after["definition_revision"]) == ("saved", 2)
    assert after["source_has_effective_session"] is False
    assert service.get_definition(saved["workflow_definition_id"], 1)["nodes"][0]["config"]["text"] == "A"
    with pytest.raises(ContractValidationError) as error:
        service.create_session(saved["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    assert error.value.reason_code == "stale_revision"
    assert create_session(service, after)["definition_revision"] == 2


def test_any_remaining_unarchived_session_counts_when_another_is_archived(service):
    saved = update(service, document(mode="fixed"))
    archived = create_session(service, saved)
    other = create_session(service, saved)
    service.archive_session(archived["workflow_session_id"], expected_revision=archived["revision"],
                            idempotency_key=str(uuid4()))
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    asked = update(service, changed, 1)
    assert asked["outcome"] == "confirmation_required"
    after = update(service, changed, 1, confirm=True)
    assert after["outcome"] == "derived"
    assert service.get_session(other["workflow_session_id"])["definition_revision"] == 1


def test_deleted_sessions_do_not_count(service):
    saved = update(service, document(mode="fixed"))
    session = create_session(service, saved)
    service.delete_session(session["workflow_session_id"], expected_revision=session["revision"],
                           idempotency_key=str(uuid4()))
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    after = update(service, changed, 1)
    assert after["outcome"] == "saved"
    assert after["source_has_effective_session"] is False


def test_missing_package_fixed_document_saves_then_execution_reports_missing_package(service):
    value = document(mode="fixed")
    value["package_lock"] = [{"package_id": "missing.package", "version": "9.0.0"}]
    value["execution_roots"] = [value["nodes"][0]["node_binding_id"]]
    saved = update(service, value)
    assert service.get_definition(value["workflow_definition_id"]) == saved["document"]
    session = create_session(service, saved)
    with pytest.raises(ContractValidationError) as error:
        service.start(session["workflow_session_id"], expected_revision=session["revision"],
                      idempotency_key=str(uuid4()))
    assert error.value.reason_code == "graph_missing_package"


def test_missing_registration_cannot_enable_adjustable_policy(service):
    with pytest.raises(ContractValidationError) as error:
        update(service, document(component="missing.component"))
    assert error.value.reason_code == "graph_missing_node_type"
    assert not bundle(service)


def test_missing_current_declaration_does_not_restore_descriptor_editing_after_reopen(tmp_path):
    path = tmp_path / "missing-registration.sqlite"
    first = GraphWorkflowService(path, registry=registry())
    try:
        saved = update(first, document())
        create_session(first, saved)
    finally:
        first.close()
    second = GraphWorkflowService(path, registry=NodeRegistry())
    try:
        with pytest.raises(ContractValidationError) as error:
            second.get_definition(saved["workflow_definition_id"])
        assert error.value.reason_code == "graph_missing_node_type"
        assert second.get_definition(saved["workflow_definition_id"], 1)["nodes"][0]["config"]["text"] == "A"
    finally:
        second.close()


def test_executor_unavailability_does_not_prevent_legal_configuration_storage(tmp_path):
    path = tmp_path / "missing-executor.sqlite"
    first = GraphWorkflowService(path, registry=registry())
    try:
        saved = update(first, document())
    finally:
        first.close()
    second = GraphWorkflowService(path, registry=registry(executable=False))
    try:
        changed = second.get_definition(saved["workflow_definition_id"])
        changed["nodes"][0]["config"]["text"] = "B"
        assert update(second, changed, 1)["outcome"] == "saved"
    finally:
        second.close()


def test_incomplete_business_configuration_is_saved_without_compilation(service):
    value = document(config={})
    value["execution_roots"] = [value["nodes"][0]["node_binding_id"]]
    saved = update(service, value)
    assert saved["document"]["nodes"][0]["config"] == {}
    session = create_session(service, saved)
    with pytest.raises(ContractValidationError) as error:
        service.start(session["workflow_session_id"], expected_revision=session["revision"],
                      idempotency_key=str(uuid4()))
    assert error.value.reason_code == "graph_no_outputs"


def test_derived_idempotency_receipt_creates_one_target(service):
    saved = update(service, document(mode="fixed"))
    create_session(service, saved)
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    assert update(service, changed, 1, "ask-once")["outcome"] == "confirmation_required"
    assert len(service.list_definitions()) == 1
    first = update(service, changed, 1, "derive-once", confirm=True)
    assert first["outcome"] == "derived"
    assert update(service, changed, 1, "derive-once", confirm=True) == first
    assert len(service.list_definitions()) == 2
    changed["name"] = "different request"
    with pytest.raises(ContractValidationError) as error:
        update(service, changed, 1, "derive-once", confirm=True)
    assert error.value.reason_code == "idempotency_conflict"


@pytest.mark.parametrize("failure", ["after_configuration_write", "before_commit"])
def test_configuration_and_fixed_definition_rollback_together(tmp_path, failure):
    state = {"active": False}

    def fault(point):
        if state["active"] and point == failure:
            raise RuntimeError("synthetic write failure")

    instance = GraphWorkflowService(tmp_path / "rollback.sqlite", registry=registry(), fault_injector=fault)
    try:
        saved = update(instance, document(mode="fixed"))
        create_session(instance, saved)
        before = bundle(instance)
        changed = deepcopy(saved["document"])
        changed["nodes"][0]["config"]["text"] = "B"
        state["active"] = True
        with pytest.raises(RuntimeError, match="synthetic write failure"):
            update(instance, changed, 1, "failed-derive", confirm=True)
        state["active"] = False
        assert bundle(instance) == before
        assert instance.list_definitions() == [{"document": saved["document"], "archived": False}]
        with closing(instance._store()) as store:
            assert store.read_receipt("graph.definition.update", "failed-derive") is None
            assert store._connection.execute("SELECT COUNT(*) FROM workflow_current_configuration").fetchone()[0] == 1
    finally:
        instance.close()


@pytest.mark.parametrize("failure", ["edit_sequence", "document_sequence", "document_revision", "identity"])
def test_conflicting_edit_basis_is_rejected_without_mutation(service, failure):
    saved = update(service, document())
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    arguments = {"workflow_definition_id": saved["workflow_definition_id"], "expected_edit_sequence": 1}
    if failure == "edit_sequence":
        arguments["expected_edit_sequence"] = 0
        changed.pop("edit_sequence")
    elif failure == "document_sequence":
        changed["edit_sequence"] = 2
    elif failure == "document_revision":
        changed["revision"] = 2
    else:
        arguments["workflow_definition_id"] = str(uuid4())
    with pytest.raises(ContractValidationError):
        service.update_definition(changed, idempotency_key=str(uuid4()), **arguments)
    assert service.get_definition(saved["workflow_definition_id"]) == saved["document"]


def test_archived_workflow_is_readonly_and_directory_projection_remains_formal(service):
    saved = update(service, document())
    service.archive_definition(saved["workflow_definition_id"], expected_revision=1, idempotency_key=str(uuid4()))
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    with pytest.raises(ContractValidationError) as error:
        update(service, changed, 1)
    assert error.value.reason_code == "workflow_archived"
    assert service.list_definitions() == []
    assert service.list_definitions(include_archived=True) == [{"document": saved["document"], "archived": True}]


@pytest.mark.parametrize("policy", [None, {"schema_version": 2, "prompt_mode": "fixed"},
                                  {"schema_version": 1, "prompt_mode": "unknown"},
                                  {"schema_version": 1, "prompt_mode": "fixed", "fields": ["text"]}])
def test_schema3_requires_strict_node_policy(policy):
    value = document()
    value["nodes"][0]["configuration_policy"] = policy
    with pytest.raises(ContractValidationError):
        validate_graph_document(value)


@pytest.mark.parametrize("group", [False, True])
def test_registry_rejects_unreviewed_adjustable_contracts(group):
    value = NodeRegistry()
    wrong = declaration(group)
    wrong["fields"].append("reference")
    with pytest.raises(ContractValidationError) as error:
        value.register(NodeDefinition("prompts.group" if group else "prompts.item", "3", "Bad", "Test",
                                      {}, {"type": "object"}, adjustable_configuration=wrong), None)
    assert error.value.reason_code == "graph_invalid_adjustable_declaration"
    with pytest.raises(ContractValidationError):
        value.register(NodeDefinition("test.arbitrary", "1", "Bad", "Test",
                                      {}, {"type": "object"}, adjustable_configuration=declaration()), None)


def test_retired_definition_save_command_cannot_bypass_schema3_partitioning(service):
    with pytest.raises(ContractValidationError) as error:
        GraphApplication(service).command("definition.save", {
            "document": document(), "expected_revision": 0, "idempotency_key": str(uuid4()),
        })
    assert error.value.reason_code == "not_found"
    assert not bundle(service)


@pytest.mark.parametrize("field,value", [("lifecycle", "replay"), ("compaction", None),
                                       ("lifecycle", ["context_once"]), ("compaction", True)])
def test_provided_lifecycle_fields_keep_storage_format_validation(service, field, value):
    proposed = document()
    proposed["nodes"][0]["config"][field] = value
    with pytest.raises(ContractValidationError) as error:
        update(service, proposed)
    assert error.value.reason_code == "graph_invalid_config"
    assert not bundle(service)


def test_missing_lifecycle_fields_are_legal_incomplete_configuration(service):
    proposed = document()
    proposed["nodes"][0]["config"].pop("lifecycle")
    proposed["nodes"][0]["config"].pop("compaction")
    saved = update(service, proposed)
    assert saved["outcome"] == "saved"
    assert "lifecycle" not in saved["document"]["nodes"][0]["config"]
    assert "compaction" not in saved["document"]["nodes"][0]["config"]


def _copy(service, session, target):
    return service.copy_session(
        session["workflow_session_id"], document=target,
        expected_session_revision=session["revision"], expected_data_revision=session["data_revision"],
        expected_definition_revision=session["definition_revision"], expected_head_revision=session["head_revision"],
        idempotency_key=str(uuid4()))


def test_legacy_copy_cannot_bypass_schema3_source_partition(service):
    saved = update(service, document())
    session = create_session(service, saved)
    before = bundle(service)
    target = document()
    target["schema_version"] = 2
    target["nodes"][0].pop("configuration_policy")
    with pytest.raises(ContractValidationError) as error:
        _copy(service, session, target)
    assert error.value.reason_code == "graph_configuration_update_required"
    assert bundle(service) == before


def test_legacy_copy_cannot_create_schema3_target_without_current_partition(service):
    source = document()
    source["schema_version"] = 2
    source["nodes"][0].pop("configuration_policy")
    save_definition_fixture(service, source)
    session = service.create_session(source["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
    before = bundle(service)
    with pytest.raises(ContractValidationError) as error:
        _copy(service, session, document())
    assert error.value.reason_code == "graph_configuration_update_required"
    assert bundle(service) == before


def test_session_rebind_service_entrypoint_is_retired(service):
    saved = update(service, document())
    create_session(service, saved)
    assert not hasattr(service, "rebind_session")


def test_fixed_projection_keeps_item_identity_and_unsupported_fields():
    value = member()
    value["reference"] = {"id": "fixed"}
    value["presentation"]["port"] = "fixed"
    fixed = fixed_configuration_projection(value, declaration(), {"schema_version": 1, "prompt_mode": "adjustable"})
    assert fixed == {"id": value["id"], "reference": {"id": "fixed"}, "presentation": {"port": "fixed"}}
    assert fixed_configuration_projection(value, declaration(), {"schema_version": 1, "prompt_mode": "fixed"}) == value


@pytest.mark.parametrize("mode", ["fixed", "adjustable"])
def test_policy_switch_is_a_fixed_change_that_asks_first(service, mode):
    """The fixed/adjustable switch itself classifies as a fixed modification."""
    saved = update(service, document(mode=mode))
    session = create_session(service, saved)
    before = bundle(service)
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["configuration_policy"]["prompt_mode"] = "adjustable" if mode == "fixed" else "fixed"
    asked = update(service, changed, 1)
    assert asked["outcome"] == "confirmation_required"
    assert "fixed" in asked["changed_categories"]
    assert asked["document"]["nodes"][0]["configuration_policy"]["prompt_mode"] == changed["nodes"][0]["configuration_policy"]["prompt_mode"]
    assert bundle(service) == before
    assert service.get_session(session["workflow_session_id"])["definition_revision"] == 1
    after = update(service, changed, 1, confirm=True)
    assert after["outcome"] == "derived"
    assert after["document"]["nodes"][0]["configuration_policy"]["prompt_mode"] == changed["nodes"][0]["configuration_policy"]["prompt_mode"]
    assert service.get_definition(saved["workflow_definition_id"]) == saved["document"]


def test_policy_switch_without_effective_session_saves_in_place(service):
    saved = update(service, document(mode="fixed"))
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["configuration_policy"]["prompt_mode"] = "adjustable"
    after = update(service, changed, 1)
    assert after["outcome"] == "saved"
    assert "fixed" in after["changed_categories"]
    assert after["workflow_definition_id"] == saved["workflow_definition_id"]
    assert after["definition_revision"] == 2


def test_confirmation_declined_leaves_source_and_target_untouched(service):
    """Declining is simply never confirming: the ask itself wrote nothing."""
    saved = update(service, document(mode="fixed"))
    session = create_session(service, saved)
    before = bundle(service)
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    asked = update(service, changed, 1, "ask-then-decline")
    assert asked["outcome"] == "confirmation_required"
    assert bundle(service) == before
    assert update(service, changed, 1, "ask-then-decline") == asked
    assert service.list_definitions() == [{"document": saved["document"], "archived": False}]
    assert [row["workflow_session_id"] for row in service.list_sessions(saved["workflow_definition_id"])] \
        == [session["workflow_session_id"]]
    # A later confirmed submission at the same basis derives exactly one target.
    after = update(service, changed, 1, confirm=True)
    assert after["outcome"] == "derived"
    assert len(service.list_definitions()) == 2
    assert service.list_sessions(after["workflow_definition_id"]) == []


def test_stale_confirmation_basis_is_rejected_without_writing(service):
    saved = update(service, document(mode="fixed"))
    create_session(service, saved)
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    assert update(service, changed, 1)["outcome"] == "confirmation_required"
    # A real display save moves the basis before the user answers.
    renamed = deepcopy(saved["document"])
    renamed["name"] = "Renamed during the question"
    saved_later = update(service, renamed, 1)
    assert saved_later["outcome"] == "saved" and saved_later["edit_sequence"] == 2
    before = bundle(service)
    with pytest.raises(ContractValidationError) as error:
        update(service, changed, 1, confirm=True)
    assert error.value.reason_code == "stale_edit_sequence"
    assert bundle(service) == before
    assert len(service.list_definitions()) == 1


def test_unchanged_fixed_fields_with_adjustable_edit_never_asks(service):
    saved = update(service, document())
    create_session(service, saved)
    changed = deepcopy(saved["document"])
    changed["nodes"][0]["config"]["text"] = "B"
    changed["nodes"][0]["config"]["presentation"]["enabled"] = False
    after = update(service, changed, 1)
    assert after["outcome"] == "saved"
    assert after["changed_categories"] == ["adjustable"]


def test_exact_prompt_business_fields_stay_adjustable_for_item_and_group(service):
    """Position, order, content and every other reviewed field remain adjustable."""
    saved = update(service, document())
    create_session(service, saved)
    changed = deepcopy(saved["document"])
    config = changed["nodes"][0]["config"]
    config["text"] = "revised"
    config["presentation"].update(role="user", placement="middle", depth=2, order=-3, enabled=False)
    config["metadata"] = {"note": "kept"}
    config["lifecycle"] = "per_request"
    config["compaction"] = "allowed"
    after = update(service, changed, 1)
    assert after["outcome"] == "saved"
    assert after["changed_categories"] == ["adjustable"]
    assert after["edit_sequence"] == 2 and after["definition_revision"] == 1


def test_policy_kind_table_drives_classification_not_prompt_branches():
    from phase1_agent.workflow_configuration import CONFIGURATION_POLICY_KINDS, configuration_policy_kind
    kind = CONFIGURATION_POLICY_KINDS["prompt"]
    assert kind.contracts == (("prompts.item", "3"), ("prompts.group", "3"))
    assert kind.member_contracts == (("prompts.group", "3"),)
    assert set(kind.fields) == set(PROMPT_CONFIGURATION_FIELDS)
    assert set(kind.nested_fields[0][1]) == {"role", "placement", "depth", "order", "enabled"}
    assert configuration_policy_kind(declaration()) is kind
    # The kind validator rejects an out-of-whitelist member field for every
    # contract in the kind, without any component-identity branch.
    with pytest.raises(ContractValidationError) as error:
        kind.validate_entry({"text": 42}, node_id=str(uuid4()))
    assert error.value.reason_code == "graph_invalid_config"
