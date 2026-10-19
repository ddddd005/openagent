"""Definition data port: existing evidence, independent re-read, fixed references."""

from copy import deepcopy

import pytest

from phase1_agent.configuration_data import WorkflowDataManagement
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import content_digest
from phase1_agent.definition_data import (
    DefinitionDataSession, DefinitionRepository, require_evidence,
)
from phase1_agent.graph_contracts import NodeRegistry
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, TypeRegistry
from phase1_agent.storage import SqliteStore
from phase1_agent.type_contract_store import TypeContractStore
from test_workflow_configuration import document as schema3_document
from test_workflow_configuration import registry as schema3_registry
from test_workflow_configuration import update as update_definition


PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
}


def store_factory(path):
    return lambda: SqliteStore(path)


def initialize_store(path):
    SqliteStore(path).close()


def register_payload_evidence(path):
    """Write the existing evidence in its own registration transaction."""
    types = TypeRegistry()
    types.register(DataTypeDefinition("sample.payload", 1, PAYLOAD_SCHEMA,
                                      {"value": "initial"}, "session"))
    store = SqliteStore(path)
    try:
        return TypeContractStore(store).register(types, "sample.payload", 1, "session")
    finally:
        store.close()


def test_existing_evidence_is_re_read_from_a_new_store_and_views_are_isolated(tmp_path):
    path = tmp_path / "definition-evidence.sqlite"
    original = register_payload_evidence(path)
    management = WorkflowDataManagement(store_factory(path))

    # The original write store is already closed; this context must open a new
    # normal store and read only the persisted record.
    with management.read_definitions() as data:
        assert isinstance(data, DefinitionDataSession)
        first = data.type_evidence("session", "sample.payload", 1)
        assert first == original
        assert first["digest"] == content_digest(first["declaration"])

        # Mutating the returned declaration, digest, envelope or an invented
        # provenance field must not change the persisted evidence.
        first["declaration"]["schema"] = {"type": "integer"}
        first["digest"] = "mutated"
        first["envelope_version"] = 99
        first["source"] = {"package_id": "invented.package"}
        second = data.type_evidence("session", "sample.payload", 1)
        assert second == original

    # Closing the read context drops the store; the next read is independent.
    with management.read_definitions() as data:
        third = data.type_evidence("session", "sample.payload", 1)
    assert third == original
    assert third["digest"] == original["digest"] == content_digest(third["declaration"])


def test_absent_evidence_is_none_and_require_evidence_reports_exact_failures(tmp_path):
    path = tmp_path / "definition-errors.sqlite"
    initialize_store(path)
    management = WorkflowDataManagement(store_factory(path))
    missing = {"scope": "session", "type_id": "sample.absent", "schema_version": 1}

    with management.read_definitions() as data:
        assert data.type_evidence(**missing) is None
        with pytest.raises(ContractValidationError) as caught:
            require_evidence(data.type_evidence(**missing), missing)
        assert caught.value.reason_code == "definition_evidence_missing"
        assert caught.value.status_code == 404

    # A plain definition query never establishes a baseline for the missing type.
    with management.read_definitions() as data:
        assert data.type_evidence(**missing) is None

    evidence = register_payload_evidence(path)
    with management.read_definitions() as data:
        stored = data.type_evidence("session", "sample.payload", 1)
        assert require_evidence(stored, {"scope": "session", "type_id": "sample.payload",
                                         "schema_version": 1}) == evidence
        for wrong_reference in (
            {"scope": "session", "type_id": "sample.payload", "schema_version": 2},
            {"scope": "session", "type_id": "sample.other", "schema_version": 1},
            {"scope": "global", "type_id": "sample.payload", "schema_version": 1},
        ):
            with pytest.raises(ContractValidationError) as caught:
                require_evidence(stored, wrong_reference)
            assert caught.value.reason_code == "definition_evidence_mismatch"
            assert caught.value.status_code == 409

    store = SqliteStore(path)
    try:
        assert store._connection.execute(
            "SELECT COUNT(*) FROM registered_type_contracts WHERE type_id='sample.absent'"
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_fixed_revision_references_come_from_configuration_data_not_current_synthesis(tmp_path):
    path = tmp_path / "definition-references.sqlite"

    initial = schema3_document(mode="fixed")
    initial["package_lock"] = [{"package_id": "sample.locked", "version": "1.0.0"}]
    first_service = GraphWorkflowService(path, registry=schema3_registry())
    try:
        saved_first = update_definition(first_service, initial)
    finally:
        first_service.close()
    identity = saved_first["workflow_definition_id"]
    assert (saved_first["outcome"], saved_first["definition_revision"]) == ("saved", 1)

    # A second fixed revision changes both the node contract and the package
    # lock, so the current configuration no longer matches revision 1.
    second_document = deepcopy(saved_first["document"])
    second_document["package_lock"] = [{"package_id": "sample.locked", "version": "2.0.0"}]
    second_document["nodes"][0]["component_id"] = "prompts.group"
    second_document["nodes"][0]["component_version"] = "3"
    second_document["nodes"][0]["config"] = deepcopy(
        schema3_document(group=True)["nodes"][0]["config"])
    second_service = GraphWorkflowService(path, registry=schema3_registry())
    try:
        saved_second = update_definition(second_service, second_document, 1)
    finally:
        second_service.close()
    assert (saved_second["outcome"], saved_second["definition_revision"]) == ("saved", 2)

    # Independently reopen the persisted fixed revisions through the
    # configuration data port; no service or composition is involved.
    management = WorkflowDataManagement(store_factory(path))
    with management.read_configuration() as data:
        fixed_first = data.fixed_document(identity, 1)
        fixed_second = data.fixed_document(identity, 2)
    expected_first = deepcopy(saved_first["document"])
    expected_first.pop("edit_sequence")
    expected_second = deepcopy(saved_second["document"])
    expected_second.pop("edit_sequence")
    assert fixed_first == expected_first
    assert fixed_second == expected_second
    assert fixed_first["revision"] == 1 and fixed_second["revision"] == 2
    assert fixed_first["package_lock"] == [{"package_id": "sample.locked", "version": "1.0.0"}]
    assert fixed_second["package_lock"] == [{"package_id": "sample.locked", "version": "2.0.0"}]

    # The definition query resolves the exact fixed revision it is given.
    # Revision 1 still names prompts.item although the current configuration
    # now names prompts.group; a current-configuration synthesis could not
    # produce that answer.
    reference_service = GraphWorkflowService(path, registry=schema3_registry())
    try:
        first_references = reference_service.definition_references(identity, 1)
        second_references = reference_service.definition_references(identity, 2)
    finally:
        reference_service.close()
    assert first_references == {
        "workflow_definition_id": identity,
        "revision": 1,
        "package_lock": [{"package_id": "sample.locked", "version": "1.0.0"}],
        "nodes": [{
            "kind": "node",
            "registration_ref": {"component_id": "prompts.item", "component_version": "3"},
            "source": [],
            "availability": "registered",
        }],
        "data_types": [],
    }
    assert second_references["revision"] == 2
    assert second_references["package_lock"] == [{"package_id": "sample.locked",
                                                  "version": "2.0.0"}]
    assert second_references["nodes"][0]["registration_ref"] == {
        "component_id": "prompts.group", "component_version": "3"}
    assert second_references["nodes"][0]["availability"] == "registered"

    # A trusted registry without the saved node still reports the persisted
    # reference, marking only availability as unavailable.
    missing_service = GraphWorkflowService(path, registry=NodeRegistry())
    try:
        missing_references = missing_service.definition_references(identity, 1)
    finally:
        missing_service.close()
    assert missing_references["nodes"] == [{
        "kind": "node",
        "registration_ref": {"component_id": "prompts.item", "component_version": "3"},
        "source": [],
        "availability": "unavailable",
    }]
    assert missing_references["package_lock"] == [{"package_id": "sample.locked",
                                                   "version": "1.0.0"}]
    assert missing_references["revision"] == 1


def test_definition_data_session_has_no_registration_entrypoints():
    for name in ("check", "register", "check_registry"):
        assert not hasattr(DefinitionDataSession, name)
        assert not hasattr(DefinitionRepository, name)
    assert hasattr(DefinitionDataSession, "type_evidence")
    assert hasattr(DefinitionRepository, "type_evidence")
