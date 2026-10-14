"""Configuration consumers read only the unified node-definition authority."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodeRegistry
from phase1_agent.graph_records import graph_record
from phase1_agent.graph_service import GraphManagementService, GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.prompt_lifecycle import PROMPT_MATERIALS_TYPE
from phase1_agent.workflow_configuration import (
    PROMPT_CONFIGURATION_FIELDS,
    classify_configuration_changes,
    fixed_configuration_projection,
    validate_adjustable_configuration,
    validate_configuration_policy,
)


COMPONENT_ID = "prompts.item"
COMPONENT_VERSION = "3"


def declaration():
    return {
        "schema_version": 1,
        "policy": "prompt",
        "fields": list(PROMPT_CONFIGURATION_FIELDS),
    }


def prompt_config(text):
    return {
        "id": str(uuid4()),
        "text": text,
        "presentation": {
            "role": "system",
            "placement": "before",
            "depth": None,
            "order": 0,
            "enabled": True,
        },
        "metadata": {},
        "lifecycle": "context_once",
        "compaction": "never",
    }


def trusted_registry():
    registry = NodeRegistry()
    config = prompt_config("Registry declaration")
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "text": {"type": "string"},
            "presentation": {"type": "object"},
            "metadata": {"type": "object"},
            "lifecycle": {"type": "string"},
            "compaction": {"type": "string"},
        },
        "additionalProperties": False,
    }
    registry.register(NodeDefinition(
        component_id=COMPONENT_ID,
        component_version=COMPONENT_VERSION,
        display_name="Synthetic prompt",
        category="Test",
        default_config=config,
        config_schema=schema,
        adjustable_configuration=declaration(),
    ), lambda config, inputs, context: {})
    return registry


def seed_current_configuration(service, *, mode="adjustable"):
    workflow_id, node_id = str(uuid4()), str(uuid4())
    config = prompt_config("Fixed seed")
    node = {
        "node_binding_id": node_id,
        "component_id": COMPONENT_ID,
        "component_version": COMPONENT_VERSION,
        "title": "Prompt",
        "position": {"x": 12, "y": 24},
        "configuration_policy": {"schema_version": 1, "prompt_mode": mode},
        "config": deepcopy(config),
    }
    document = {
        "schema_version": 3,
        "workflow_definition_id": workflow_id,
        "revision": 1,
        "name": "Seeded workflow",
        "nodes": [node],
        "edges": [],
        "object_bindings": [],
    }
    old_descriptor = service.registry.get_definition(COMPONENT_ID, COMPONENT_VERSION).to_dict()
    old_descriptor["display_name"] = "Retired conflicting descriptor"
    old_descriptor["adjustable_configuration"] = None

    with closing(service._store()) as store:
        repository = GraphRecordStore(store)
        store._connection.execute("BEGIN IMMEDIATE")
        repository.put("node_definition", graph_record(
            "node_definition",
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            descriptor=old_descriptor,
        ))
        repository.put("node_binding", graph_record(
            "node_binding",
            workflow_definition_id=workflow_id,
            workflow_definition_revision=1,
            node_binding_id=node_id,
            component_id=COMPONENT_ID,
            component_version=COMPONENT_VERSION,
            config=config,
        ))
        repository.put("workflow_definition_revision", graph_record(
            "workflow_definition_revision",
            workflow_definition_id=workflow_id,
            revision=1,
            bindings=[node_id],
            edges=[],
            document=document,
        ))
        repository.put_current_configuration({
            "workflow_definition_id": workflow_id,
            "edit_sequence": 7,
            "fixed_definition_revision": 1,
            "display_name": "Current display name",
            "node_presentation": {
                node_id: {"title": "Current title", "position": {"x": 40, "y": 80}},
            },
            "adjustable_config": {node_id: prompt_config("Current accepted value") if mode == "adjustable" else {}},
        })
        store._connection.execute("COMMIT")
    return workflow_id, node_id, document


def business_snapshot(service):
    with closing(service._store()) as store:
        connection = store._connection
        return (
            tuple(tuple(row) for row in connection.execute(
                "SELECT record_type,record_id,payload,immutable,created_at "
                "FROM records ORDER BY record_type,record_id")),
            tuple(tuple(row) for row in connection.execute(
                "SELECT * FROM workflow_current_configuration ORDER BY workflow_definition_id")),
            tuple(tuple(row) for row in connection.execute(
                "SELECT * FROM idempotency ORDER BY operation,key")),
        )


def domain_snapshot(service):
    records, configurations, _ = business_snapshot(service)
    return records, configurations


@pytest.fixture
def management_service(tmp_path):
    service = GraphManagementService(tmp_path / "definition-retirement.sqlite")
    service.registry = trusted_registry()
    try:
        yield service
    finally:
        service.close()


def submit(service, document, sequence, *, key=None, confirm=None):
    return service.update_definition(
        document,
        workflow_definition_id=document["workflow_definition_id"],
        expected_edit_sequence=sequence,
        idempotency_key=key or str(uuid4()),
        confirm_derivation=confirm,
    )


def formal_prompt_execution_document(registry):
    prompt_id, assembly_id, output_id = str(uuid4()), str(uuid4()), str(uuid4())
    prompt_config = deepcopy(registry.get_definition("prompts.item", "3").default_config)
    prompt_config["text"] = "Initial formal prompt"
    nodes = [
        {
            "node_binding_id": prompt_id,
            "component_id": "prompts.item",
            "component_version": "3",
            "title": "Prompt",
            "position": {"x": 0, "y": 0},
            "configuration_policy": {"schema_version": 1, "prompt_mode": "adjustable"},
            "config": prompt_config,
        },
        {
            "node_binding_id": assembly_id,
            "component_id": "prompts.assembly",
            "component_version": "2",
            "title": "Assembly",
            "position": {"x": 160, "y": 0},
            "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
            "config": {},
        },
        {
            "node_binding_id": output_id,
            "component_id": "tools.output",
            "component_version": "1",
            "title": "Output",
            "position": {"x": 320, "y": 0},
            "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
            "config": {"mode": "prompt"},
            "public_outputs": ["output"],
        },
    ]
    return {
        "schema_version": 3,
        "workflow_definition_id": str(uuid4()),
        "revision": 1,
        "name": "Formal adjustable prompt",
        "nodes": nodes,
        "edges": [
            {
                "edge_id": str(uuid4()),
                "source_node_id": prompt_id,
                "source_port_id": "output",
                "target_node_id": assembly_id,
                "target_port_id": "input",
                "order": 0,
            },
            {
                "edge_id": str(uuid4()),
                "source_node_id": assembly_id,
                "source_port_id": "output",
                "target_node_id": output_id,
                "target_port_id": "input",
                "order": 0,
            },
        ],
        "object_bindings": [],
        "package_lock": list(registry.execution_package_lock),
    }, prompt_id, output_id


def test_current_configuration_uses_registry_definition_not_conflicting_legacy_row(
        management_service, monkeypatch):
    workflow_id, node_id, _ = seed_current_configuration(management_service)
    before = business_snapshot(management_service)
    descriptor_reads = []
    lookups = []
    original_maybe = GraphRecordStore.maybe
    original_get_definition = management_service.registry.get_definition

    def track_descriptor_read(repository, kind, **identity):
        if kind == "node_definition":
            descriptor_reads.append(identity)
            raise AssertionError("configuration read consulted retired descriptor storage")
        return original_maybe(repository, kind, **identity)

    def track_current_definition(component_id, component_version):
        lookups.append((component_id, component_version))
        return original_get_definition(component_id, component_version)

    monkeypatch.setattr(GraphRecordStore, "maybe", track_descriptor_read)
    monkeypatch.setattr(management_service.registry, "get_definition", track_current_definition)

    current = management_service.get_definition(workflow_id)

    assert current["name"] == "Current display name"
    assert current["nodes"][0]["node_binding_id"] == node_id
    assert current["nodes"][0]["title"] == "Current title"
    assert current["nodes"][0]["config"]["text"] == "Current accepted value"
    assert lookups == [(COMPONENT_ID, COMPONENT_VERSION)]
    assert descriptor_reads == []
    assert business_snapshot(management_service) == before


def test_missing_current_type_fails_without_legacy_fallback_or_writes(management_service, monkeypatch):
    workflow_id, _, _ = seed_current_configuration(management_service)
    before = business_snapshot(management_service)
    descriptor_reads = []
    original_maybe = GraphRecordStore.maybe

    def track_descriptor_read(repository, kind, **identity):
        if kind == "node_definition":
            descriptor_reads.append(identity)
            raise AssertionError("configuration read consulted retired descriptor storage")
        return original_maybe(repository, kind, **identity)

    monkeypatch.setattr(GraphRecordStore, "maybe", track_descriptor_read)
    monkeypatch.setattr(management_service.registry, "get_definition",
                        lambda component_id, component_version: None)

    with pytest.raises(ContractValidationError) as error:
        management_service.get_definition(workflow_id)

    assert error.value.reason_code == "graph_missing_node_type"
    assert error.value.status_code == 409
    assert descriptor_reads == []
    assert business_snapshot(management_service) == before


def test_configuration_submit_preserves_cas_and_saves_adjustable_values(management_service):
    workflow_id, _, fixed_document = seed_current_configuration(management_service)
    before = business_snapshot(management_service)
    proposed = deepcopy(fixed_document)
    proposed["nodes"][0]["config"]["text"] = "Proposed current value"

    with pytest.raises(ContractValidationError) as stale:
        submit(management_service, proposed, 6)
    assert stale.value.reason_code == "stale_edit_sequence"
    assert stale.value.status_code == 409
    assert business_snapshot(management_service) == before

    proposed = management_service.get_definition(workflow_id)
    proposed["nodes"][0]["config"]["text"] = "Proposed current value"
    saved = submit(management_service, proposed, 7)

    assert saved["outcome"] == "saved"
    assert saved["changed_categories"] == ["adjustable"]
    assert saved["workflow_definition_id"] == workflow_id
    assert saved["edit_sequence"] == 8
    assert saved["definition_revision"] == 1
    assert saved["document"]["nodes"][0]["config"]["text"] == "Proposed current value"
    with closing(management_service._store()) as store:
        assert len(GraphRecordStore(store).rows(
            "workflow_definition_revision", workflow_definition_id=workflow_id)) == 1


@pytest.mark.parametrize(("start_mode", "target_mode"), [
    ("fixed", "adjustable"),
    ("adjustable", "fixed"),
])
def test_policy_switch_without_effective_session_saves_in_place(
        management_service, start_mode, target_mode):
    workflow_id, node_id, _ = seed_current_configuration(management_service, mode=start_mode)
    proposed = management_service.get_definition(workflow_id)
    proposed["nodes"][0]["configuration_policy"]["prompt_mode"] = target_mode

    saved = submit(management_service, proposed, 7)

    assert saved["outcome"] == "saved"
    assert saved["source_has_effective_session"] is False
    assert saved["workflow_definition_id"] == workflow_id
    assert saved["definition_revision"] == 2
    assert saved["edit_sequence"] == 8
    assert "fixed" in saved["changed_categories"]
    assert saved["document"]["nodes"][0]["configuration_policy"]["prompt_mode"] == target_mode
    assert saved["document"]["nodes"][0]["node_binding_id"] == node_id


def test_unconfirmed_fixed_policy_change_with_session_leaves_domain_state_untouched(management_service):
    workflow_id, _, _ = seed_current_configuration(management_service)
    session = management_service.create_session(workflow_id, 1, idempotency_key=str(uuid4()))
    source_before = management_service.get_definition(workflow_id)
    session_before = management_service.get_session(session["workflow_session_id"])
    before = domain_snapshot(management_service)
    receipts_before = business_snapshot(management_service)[2]
    proposed = deepcopy(source_before)
    proposed["nodes"][0]["configuration_policy"]["prompt_mode"] = "fixed"

    asked = submit(management_service, proposed, 7, key="ask-fixed-change")

    assert asked["outcome"] == "confirmation_required"
    assert "fixed" in asked["changed_categories"]
    assert domain_snapshot(management_service) == before
    assert management_service.get_definition(workflow_id) == source_before
    assert management_service.get_session(session["workflow_session_id"]) == session_before
    assert len(management_service.list_definitions()) == 1
    assert len(business_snapshot(management_service)[2]) == len(receipts_before) + 1


def test_confirmed_fixed_policy_change_derives_and_preserves_source_session(management_service):
    workflow_id, _, _ = seed_current_configuration(management_service)
    session = management_service.create_session(workflow_id, 1, idempotency_key=str(uuid4()))
    source_before = management_service.get_definition(workflow_id)
    session_before = management_service.get_session(session["workflow_session_id"])
    proposed = deepcopy(source_before)
    proposed["nodes"][0]["configuration_policy"]["prompt_mode"] = "fixed"
    asked = submit(management_service, proposed, 7, key="ask-before-derive")
    assert asked["outcome"] == "confirmation_required"

    derived = submit(management_service, proposed, 7, key="confirm-derive", confirm=True)

    assert derived["outcome"] == "derived"
    assert derived["workflow_definition_id"] != workflow_id
    assert derived["definition_revision"] == 1
    assert derived["edit_sequence"] == 1
    assert derived["document"]["nodes"][0]["configuration_policy"]["prompt_mode"] == "fixed"
    assert management_service.get_definition(workflow_id) == source_before
    assert management_service.get_session(session["workflow_session_id"]) == session_before
    assert len(management_service.list_definitions()) == 2
    assert management_service.list_sessions(derived["workflow_definition_id"]) == []


def test_same_version_declaration_updates_are_visible_without_rewriting_fixed_records(
        management_service, monkeypatch):
    workflow_id, _, _ = seed_current_configuration(management_service)
    before = business_snapshot(management_service)
    initial = management_service.registry.get_definition(COMPONENT_ID, COMPONENT_VERSION)
    first_update = replace(initial, checkpoint_save={"config_paths": [["text"]]})
    management_service.registry.update_declarations(first_update, trusted_scope="host")
    current = management_service.registry.get_definition(COMPONENT_ID, COMPONENT_VERSION)
    second_update = replace(current, execution_class="non_working")
    management_service.registry.update_declarations(second_update, trusted_scope="host")

    observed = []
    original_descriptor = management_service._configuration_descriptor

    def capture_descriptor(repo, node):
        descriptor = original_descriptor(repo, node)
        observed.append((descriptor["checkpoint_save"], descriptor["execution_class"]))
        return descriptor

    monkeypatch.setattr(management_service, "_configuration_descriptor", capture_descriptor)
    current_document = management_service.get_definition(workflow_id)

    assert current_document["nodes"][0]["config"]["text"] == "Current accepted value"
    assert observed == [({"config_paths": [["text"]]}, "non_working")]
    assert business_snapshot(management_service) == before


def test_formal_adjustable_prompt_value_is_consumed_by_successful_service_run(tmp_path):
    registry = create_package_registry(enabled={
        "workflow.tools": "1.0.0",
        "workflow.prompts": "1.0.0",
    }).registry.detached()
    service = GraphWorkflowService(tmp_path / "formal-adjustable-prompt.sqlite", registry=registry)
    try:
        document, prompt_id, output_id = formal_prompt_execution_document(service.registry)
        saved = service.update_definition(
            document,
            workflow_definition_id=document["workflow_definition_id"],
            expected_edit_sequence=0,
            idempotency_key=str(uuid4()),
        )
        assert saved["outcome"] == "saved"
        assert saved["definition_revision"] == 1

        session = service.create_session(
            saved["workflow_definition_id"], saved["definition_revision"], idempotency_key=str(uuid4()))
        changed = service.get_definition(saved["workflow_definition_id"])
        changed["nodes"][0]["config"]["text"] = "Current registry-backed value"
        updated = service.update_definition(
            changed,
            workflow_definition_id=saved["workflow_definition_id"],
            expected_edit_sequence=saved["edit_sequence"],
            idempotency_key=str(uuid4()),
        )

        assert updated["outcome"] == "saved"
        assert updated["changed_categories"] == ["adjustable"]
        assert updated["definition_revision"] == 1
        assert updated["edit_sequence"] == 2
        assert service.get_definition(saved["workflow_definition_id"], revision=1)["nodes"][0]["config"]["text"] \
            == "Initial formal prompt"

        started = service.start(
            session["workflow_session_id"],
            expected_revision=session["revision"],
            idempotency_key=str(uuid4()),
        )
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        run = service.get_run(session["workflow_session_id"], chain_id)

        assert run["chain"]["status"] == "succeeded"
        prompt_run = next(item for item in run["node_runs"] if item["node_binding_id"] == prompt_id)
        assert prompt_run["config"]["text"] == "Current registry-backed value"
        output = next(item for item in run["outputs"] if item["node_binding_id"] == output_id)
        assert output["payload"]["items"][0]["text"] == "Current registry-backed value"
        with closing(service._store()) as store:
            assert GraphRecordStore(store).rows("node_definition") == []
    finally:
        service.close()


def test_fixed_historical_revision_read_does_not_require_current_type(management_service, monkeypatch):
    workflow_id, _, fixed_document = seed_current_configuration(management_service)
    before = business_snapshot(management_service)

    def unavailable(*_args, **_kwargs):
        raise AssertionError("historical fixed revision read requested a current definition")

    monkeypatch.setattr(management_service.registry, "get_definition", unavailable)

    assert management_service.get_definition(workflow_id, revision=1) == fixed_document
    assert business_snapshot(management_service) == before


def test_configuration_policy_whitelist_and_fixed_projection_remain_pure():
    adjustable = declaration()
    node_config = prompt_config("Current value")
    node_config["reference"] = {"resource_id": str(uuid4())}
    policy = {"schema_version": 1, "prompt_mode": "adjustable"}

    assert validate_configuration_policy(policy) == policy
    assert validate_adjustable_configuration(COMPONENT_ID, COMPONENT_VERSION, adjustable) == adjustable
    assert fixed_configuration_projection(node_config, adjustable, policy) == {
        "id": node_config["id"],
        "reference": node_config["reference"],
    }
    assert fixed_configuration_projection(node_config, adjustable, {
        "schema_version": 1, "prompt_mode": "fixed",
    }) == node_config

    invalid_policy = {"schema_version": 2, "prompt_mode": "fixed"}
    with pytest.raises(ContractValidationError) as error:
        validate_configuration_policy(invalid_policy)
    assert error.value.reason_code == "graph_invalid_configuration_policy"

    invalid_declaration = deepcopy(adjustable)
    invalid_declaration["fields"].append("reference")
    with pytest.raises(ContractValidationError) as error:
        validate_adjustable_configuration(COMPONENT_ID, COMPONENT_VERSION, invalid_declaration)
    assert error.value.reason_code == "graph_invalid_adjustable_declaration"


def test_change_classification_keeps_current_value_and_policy_semantics():
    previous = {
        "schema_version": 3,
        "workflow_definition_id": str(uuid4()),
        "revision": 1,
        "name": "Workflow",
        "nodes": [{
            "node_binding_id": str(uuid4()),
            "component_id": COMPONENT_ID,
            "component_version": COMPONENT_VERSION,
            "title": "Prompt",
            "position": {"x": 0, "y": 0},
            "configuration_policy": {"schema_version": 1, "prompt_mode": "adjustable"},
            "config": prompt_config("A"),
        }],
        "edges": [],
        "object_bindings": [],
    }
    descriptor = {"adjustable_configuration": declaration()}
    lookup = lambda node: descriptor
    current_value_change = deepcopy(previous)
    current_value_change["nodes"][0]["config"]["text"] = "B"
    policy_change = deepcopy(previous)
    policy_change["nodes"][0]["configuration_policy"]["prompt_mode"] = "fixed"

    assert classify_configuration_changes(previous, current_value_change, lookup) == ["adjustable"]
    assert classify_configuration_changes(previous, policy_change, lookup) == ["adjustable", "fixed"]
