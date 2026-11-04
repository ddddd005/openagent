"""S3-01: save structure/reference hard rules, located diagnostics and contract.

Covers Q05-1 edge endpoint existence at both save and compile, Q05-2 declared
dangling reference rejection with unconfigured values still allowed, the Q06-1
diagnostic location fields and the Q06-3 versioned machine-readable contract.
"""

from __future__ import annotations

from copy import deepcopy
import json
from uuid import uuid4

from jsonschema import Draft202012Validator
import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import (
    DOCUMENT_CONTRACT_VERSION, MAX_CONTENT_CHARS, MAX_GRAPH_BYTES, MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES, SUPPORTED_GRAPH_VERSIONS, GraphCompiler, NodeDefinition, NodePort,
    document_contract, validate_graph_document,
)
from phase1_agent.graph_service import GraphWorkflowService


def _registry():
    return create_package_registry().registry.detached()


def _synthetic_registry():
    registry = _registry()
    registry.register(NodeDefinition(
        "probe.source", "1", "Source", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT"),)), lambda config, inputs, context: {})
    registry.register(NodeDefinition(
        "probe.sink", "1", "Sink", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "JSON"),), is_output=True), lambda config, inputs, context: {})
    registry.register(NodeDefinition(
        "probe.output", "1", "Output", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT"),), is_output=True), lambda config, inputs, context: {})
    registry.register(NodeDefinition(
        "probe.refer", "1", "Refer", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT"),), outputs=(NodePort("output", "TEXT"),),
        config_node_references=(
            {"config_field": "source_ref", "multiple": False, "allow_empty": True,
             "target_types": [{"component_id": "probe.source", "component_version": "1"}]},
        )), lambda config, inputs, context: {})
    registry.register(NodeDefinition(
        "probe.access", "1", "Access", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT"),), capabilities=("objects:read",),
        object_accesses=(
            {"config_field": "object_key", "type_id": "probe.type", "schema_version": 1,
             "access": "read", "multiple": False},
        )), lambda config, inputs, context: {})
    return registry


def _node(component_id, identity, *, config=None, version="1"):
    return {"node_binding_id": identity, "component_id": component_id,
            "component_version": version, "title": component_id,
            "position": {"x": 0, "y": 0}, "config": dict(config or {}),
            "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"}}


def _edge(source, source_port, target, target_port, order=0):
    return {"edge_id": str(uuid4()), "source_node_id": source,
            "source_port_id": source_port, "target_node_id": target,
            "target_port_id": target_port, "order": order}


def _document(nodes, edges=(), *, bindings=(), controls=(), events=()):
    value = {"schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
             "name": "contract test", "nodes": list(nodes), "edges": list(edges),
             "object_bindings": list(bindings), "package_lock": []}
    if controls:
        value["control_edges"] = list(controls)
    if events:
        value["event_bindings"] = list(events)
    return value


def _builtin_document(registry):
    text = _node("tools.text", str(uuid4()), config={"text": "hello"})
    output = _node("tools.output", str(uuid4()))
    return _document([text, output], [_edge(text["node_binding_id"], "text",
                                            output["node_binding_id"], "result")])


def _rejected(call, reason_code):
    with pytest.raises(ContractValidationError) as error:
        call()
    assert error.value.reason_code == reason_code, error.value.reason_code
    assert error.value.status_code == 400, error.value.status_code
    return error.value.diagnostics[0]


def _service(tmp_path, name, registry):
    return GraphWorkflowService(tmp_path / name, registry=registry)


def _receipts(service, operation):
    from contextlib import closing

    from phase1_agent.storage import SqliteStore
    with closing(SqliteStore(service.database)) as store:
        return store._connection.execute(
            "SELECT COUNT(*) FROM idempotency WHERE operation=?", (operation,)).fetchone()[0]


# -- Q05-1: ordinary edge endpoints are a save structure rule -----------------

@pytest.mark.parametrize("field", ["source_node_id", "target_node_id"])
def test_save_rejects_dangling_edge_endpoint_with_location_and_no_write(tmp_path, field):
    registry = _registry()
    service = _service(tmp_path, f"edge-{field}.sqlite", registry)
    try:
        document = _builtin_document(registry)
        missing = str(uuid4())
        edge = _edge(document["nodes"][0]["node_binding_id"], "text",
                     document["nodes"][1]["node_binding_id"], "result")
        edge[field] = missing
        document["edges"] = [edge]
        diagnostic = _rejected(lambda: service.update_definition(
            document, workflow_definition_id=document["workflow_definition_id"],
            expected_edit_sequence=0, idempotency_key="dangling-" + field), "graph_missing_edge_node")
        assert diagnostic["phase"] == "save"
        assert diagnostic["edge_id"] == edge["edge_id"]
        assert diagnostic["node_id"] == missing
        assert diagnostic["path"] == f"/edges/0/{field}"
        assert diagnostic["expected"] == {"node_id_exists": True}
        assert diagnostic["actual"]["node_id_exists"] is False
        # The rejected save wrote nothing: no definition, no receipt.
        assert service.list_definitions(include_archived=True) == []
        assert _receipts(service, "graph.definition.update") == 0
        # The modification entitlement was released by the failure.
        good = _builtin_document(registry)
        assert service.update_definition(
            good, workflow_definition_id=good["workflow_definition_id"],
            expected_edit_sequence=0, idempotency_key="after-failure")["outcome"] == "saved"
    finally:
        service.close()


def test_compiler_rejects_missing_edge_target_without_silently_ignoring_it():
    registry = _registry()
    document = _builtin_document(registry)
    missing = str(uuid4())
    document["edges"].append(_edge(document["nodes"][0]["node_binding_id"], "text",
                                   missing, "result", order=1))
    with pytest.raises(ContractValidationError) as error:
        GraphCompiler(registry).compile(document)
    assert error.value.reason_code == "graph_missing_edge_node"
    diagnostic = error.value.diagnostics[0]
    assert diagnostic["phase"] == "compile"
    assert diagnostic["path"] == "/edges/1/target_node_id"
    assert diagnostic["node_id"] == missing


def test_compiler_port_and_type_diagnostics_carry_location_and_summaries():
    registry = _synthetic_registry()
    source = _node("probe.source", str(uuid4()))
    sink = _node("probe.sink", str(uuid4()))
    bad_port = _document([source, sink], [_edge(source["node_binding_id"], "output",
                                                sink["node_binding_id"], "missing")])
    diagnostic = _rejected(lambda: GraphCompiler(registry).compile(bad_port), "graph_unknown_port")
    assert diagnostic["phase"] == "compile"
    assert diagnostic["path"] == "/edges/0/target_port_id"
    assert diagnostic["port_id"] == "missing"
    assert diagnostic["expected"] == {"declared_input_ports": ["input"]}
    assert diagnostic["actual"] == {"port_id": "missing"}

    mismatch = _document([source, sink], [_edge(source["node_binding_id"], "output",
                                                sink["node_binding_id"], "input")])
    diagnostic = _rejected(lambda: GraphCompiler(registry).compile(mismatch), "graph_type_mismatch")
    assert diagnostic["phase"] == "compile"
    assert diagnostic["path"] == "/edges/0/target_port_id"
    assert diagnostic["expected"]["data_type"] == "JSON"
    assert diagnostic["actual"]["data_type"] == "TEXT"


def test_control_event_and_binding_structure_errors_have_location(tmp_path):
    registry = _registry()
    missing = str(uuid4())
    document = _builtin_document(registry)
    controls = deepcopy(document)
    controls["control_edges"] = [{"edge_id": str(uuid4()),
                                  "source_node_id": missing,
                                  "target_node_id": document["nodes"][1]["node_binding_id"]}]
    diagnostic = _rejected(lambda: validate_graph_document(controls), "graph_invalid_control_edge")
    assert diagnostic["path"] == "/control_edges/0/source_node_id"
    assert diagnostic["node_id"] == missing

    events = deepcopy(document)
    events["event_bindings"] = [{"event_id": "evt", "schema_version": 1,
                                 "display_name": "Event", "audience": "management",
                                 "target_node_ids": [missing],
                                 "payload_schema": {"type": "object"}}]
    diagnostic = _rejected(lambda: validate_graph_document(events), "graph_invalid_event_binding")
    assert diagnostic["path"] == "/event_bindings/0/target_node_ids"
    assert diagnostic["actual"]["missing_node_ids"] == [missing]

    bindings = deepcopy(document)
    bindings["object_bindings"] = [{
        "object_key": "obj", "type_id": "probe.type", "schema_version": 1,
        "scope": "shared", "readers": [missing], "writers": [],
        "owner_node_id": None, "registrar_node_id": None}]
    diagnostic = _rejected(lambda: validate_graph_document(bindings), "graph_invalid_document")
    assert diagnostic["path"] == "/object_bindings/0"
    assert diagnostic["missing_node_ids"] == [missing]


# -- Q05-2: declared dangling references reject; unconfigured values allowed --

def test_unconfigured_declared_references_are_allowed(tmp_path):
    registry = _synthetic_registry()
    service = _service(tmp_path, "unconfigured.sqlite", registry)
    try:
        source = _node("probe.source", str(uuid4()))
        refer = _node("probe.refer", str(uuid4()), config={"source_ref": ""})
        access = _node("probe.access", str(uuid4()), config={"object_key": ""})
        document = _document([source, refer, access])
        saved = service.update_definition(
            document, workflow_definition_id=document["workflow_definition_id"],
            expected_edit_sequence=0, idempotency_key="unconfigured")
        assert saved["outcome"] == "saved"
    finally:
        service.close()


def test_dangling_config_node_reference_is_rejected_with_location(tmp_path):
    registry = _synthetic_registry()
    service = _service(tmp_path, "dangling-ref.sqlite", registry)
    try:
        refer = _node("probe.refer", str(uuid4()), config={"source_ref": str(uuid4())})
        document = _document([refer])
        diagnostic = _rejected(lambda: service.update_definition(
            document, workflow_definition_id=document["workflow_definition_id"],
            expected_edit_sequence=0, idempotency_key="dangling-ref"), "graph_missing_config_reference")
        assert diagnostic["node_id"] == refer["node_binding_id"]
        assert diagnostic["path"] == "/nodes/0/config/source_ref"
        assert diagnostic["phase"] == "save"
        assert diagnostic["actual"]["node_id_exists"] is False
        assert diagnostic["expected"]["target_types"] == [
            {"component_id": "probe.source", "component_version": "1"}]
        assert service.list_definitions(include_archived=True) == []
    finally:
        service.close()


def test_dangling_object_key_is_rejected_with_location(tmp_path):
    registry = _synthetic_registry()
    service = _service(tmp_path, "dangling-object.sqlite", registry)
    try:
        access = _node("probe.access", str(uuid4()), config={"object_key": "missing-object"})
        document = _document([access])
        diagnostic = _rejected(lambda: service.update_definition(
            document, workflow_definition_id=document["workflow_definition_id"],
            expected_edit_sequence=0, idempotency_key="dangling-object"), "session_object_unbound")
        assert diagnostic["node_id"] == access["node_binding_id"]
        assert diagnostic["path"] == "/nodes/0/config/object_key"
        assert diagnostic["actual"] == {"object_key": "missing-object", "bound": False}
        assert diagnostic["expected"]["type_id"] == "probe.type"
        assert service.list_definitions(include_archived=True) == []
    finally:
        service.close()


# -- Q06-1: diagnostics stay backward compatible but can carry locations -----

def test_diagnostic_location_fields_are_optional_and_backward_compatible():
    legacy = DiagnosticError("legacy_code", "legacy message")
    assert legacy.diagnostics == [{
        "code": "legacy_code", "message": "legacy message", "node_id": None,
        "port_id": None, "edge_id": None, "dependent_outputs": [],
    }]
    located = DiagnosticError("located_code", "located message", phase="save",
                              path="/nodes/0/config", expected={"a": 1}, actual={"b": 2})
    assert located.diagnostics[0]["phase"] == "save"
    assert located.diagnostics[0]["path"] == "/nodes/0/config"
    assert located.diagnostics[0]["expected"] == {"a": 1}
    assert located.diagnostics[0]["actual"] == {"b": 2}


# -- Q06-3: versioned machine-readable contract ------------------------------

# S3-05 coverage review: codes this step's chain can actually raise, checked
# against the raise sites in graph_contracts.py, workflow_configuration.py,
# configuration_mutations.py, graph_service.py, graph_execution.py,
# graph_store.py, storage.py and the session-preload validation delegated by
# validate_graph_document.  These lists are golden data: when a reviewed
# reachable code or its stage changes, update the list and the catalogue.
_SAVE_REACHABLE = frozenset({
    "configuration_run_locked", "graph_config_reference_type_mismatch",
    "graph_configuration_missing", "graph_configuration_mutation_not_allowed",
    "graph_configuration_not_adjustable", "graph_configuration_target_mismatch",
    "graph_definition_basis_mismatch", "graph_definition_conflict",
    "graph_edit_sequence_exhausted", "graph_invalid_adjustable_declaration",
    "graph_invalid_config_reference", "graph_invalid_configuration_patch",
    "graph_invalid_configuration_policy", "graph_invalid_control_edge",
    "graph_invalid_document", "graph_invalid_event_binding", "graph_invalid_object_access",
    "graph_invalid_package_lock", "graph_missing_config_reference", "graph_missing_edge_node",
    "graph_missing_node_type", "idempotency_conflict", "package_publication_failed",
    "request_abandoned", "session_object_access_denied", "session_object_registrar_missing",
    "session_object_registrar_unavailable", "session_object_registration_duplicate",
    "session_object_registration_mismatch", "session_object_registration_missing",
    "session_object_type_mismatch", "session_object_unbound",
    "session_preload_binding_missing", "session_preload_duplicate", "session_preload_invalid",
    "session_preload_schema_reference_invalid", "session_preload_source_conflict",
    "session_preload_type_not_admitted", "session_preload_type_unavailable",
    "session_preload_type_unsupported", "session_preload_variable_unregistered",
    "stale_edit_sequence", "stale_revision", "storage_contract_violation",
    "variable_registration_default_invalid", "variable_registration_target_mismatch",
    "workflow_archived", "workflow_modification_busy",
})

_COMPILE_REACHABLE = frozenset({
    "graph_config_reference_type_mismatch", "graph_dependency_cycle",
    "graph_input_cardinality", "graph_input_order_conflict", "graph_invalid_config",
    "graph_invalid_configuration_policy", "graph_invalid_external_inputs",
    "graph_invalid_package_lock", "graph_missing_config_reference", "graph_missing_edge_node",
    "graph_missing_node", "graph_missing_package", "graph_no_outputs",
    "graph_non_working_dependency", "graph_required_input_missing", "graph_type_mismatch",
    "graph_unknown_port", "session_object_access_denied", "session_object_type_mismatch",
    "session_object_unbound", "session_preload_binding_missing", "session_preload_duplicate",
    "session_preload_invalid", "session_preload_schema_reference_invalid",
    "session_preload_source_conflict", "session_preload_type_not_admitted",
    "session_preload_type_unavailable", "session_preload_type_unsupported",
    "session_preload_variable_unregistered",
})

_PREPARE_REACHABLE = frozenset({
    "execution_authority_required", "graph_event_not_declared", "graph_event_payload_invalid",
    "package_missing_dependency", "package_publication_failed", "package_retired",
})

_RUN_REACHABLE = frozenset({
    "execution_dependency_unaccepted", "execution_owner_changed",
    "execution_stop_unconfirmed", "failed_retry_unavailable",
    "graph_acceptance_basis_changed", "graph_artifact_binding_mismatch",
    "graph_artifact_reference_denied", "graph_capability_denied", "graph_duplicate_material",
    "graph_external_input_missing", "graph_host_unavailable", "graph_implementation_changed",
    "graph_invalid_output", "graph_invalid_resume", "graph_legacy_prompt_retired",
    "graph_linked_output_missing", "graph_node_failed", "graph_node_output_invalid",
    "graph_node_run_missing", "graph_object_access_undeclared",
    "graph_private_state_incompatible", "graph_private_state_invalid",
    "graph_settlement_unavailable", "graph_type_mismatch", "graph_unknown_data_type",
    "host_unknown_type", "invalid_state", "package_publication_failed",
    "private_state_not_declared", "private_state_owner_mismatch",
    "private_state_schema_mismatch", "recovery_unavailable", "request_abandoned",
    "runtime_host_unavailable", "runtime_invocation_unavailable",
    "session_data_definition_conflict", "session_data_read_only", "session_data_unassigned",
    "session_object_deleted", "session_object_duplicate_write", "stale_data_revision",
    "stale_head_revision", "stale_object_revision", "storage_contract_violation",
    "tool_binding_not_prepared", "variable_not_registered", "variable_type_mismatch",
})

_SETTLEMENT_REACHABLE = frozenset({
    "checkpoint_commit_pending", "execution_result_unknown", "failed_retry_unavailable",
    "graph_acceptance_basis_changed", "graph_artifact_reference_denied",
    "graph_invalid_output", "graph_invalid_settlement", "graph_unknown_data_type",
    "host_result_recovery_ambiguous", "recovery_unavailable", "result_acceptance_pending",
    "result_committed_notification_failed", "run_basis_unavailable",
    "storage_contract_violation",
})

_STORAGE_REACHABLE = frozenset({"graph_storage_busy", "storage_contract_violation"})

# The codes the S3-05 coverage fix added to the catalogue (64 codes; the run
# phase of the pre-existing graph_type_mismatch row was extended separately).
_NEWLY_COVERED = frozenset({
    "graph_invalid_package_lock", "graph_invalid_configuration_policy",
    "session_preload_type_unsupported", "session_preload_schema_reference_invalid",
    "session_preload_invalid", "session_preload_duplicate", "session_preload_binding_missing",
    "session_preload_source_conflict", "session_preload_type_unavailable",
    "session_preload_type_not_admitted", "session_preload_variable_unregistered",
    "graph_configuration_not_adjustable", "graph_invalid_adjustable_declaration",
    "graph_definition_basis_mismatch", "graph_definition_conflict",
    "graph_configuration_target_mismatch", "graph_edit_sequence_exhausted",
    "graph_invalid_configuration_patch", "graph_configuration_mutation_not_allowed",
    "package_retired", "package_publication_failed", "graph_artifact_binding_mismatch",
    "graph_artifact_reference_denied", "graph_capability_denied",
    "graph_external_input_missing", "graph_host_unavailable", "graph_implementation_changed",
    "graph_invalid_resume", "graph_node_run_missing", "graph_object_access_undeclared",
    "graph_settlement_unavailable", "graph_duplicate_material", "graph_legacy_prompt_retired",
    "host_unknown_type", "runtime_host_unavailable", "runtime_invocation_unavailable",
    "session_data_definition_conflict", "session_data_read_only", "session_data_unassigned",
    "session_object_deleted", "session_object_duplicate_write", "stale_object_revision",
    "tool_binding_not_prepared", "variable_not_registered", "variable_type_mismatch",
    "private_state_not_declared", "private_state_owner_mismatch",
    "private_state_schema_mismatch", "graph_private_state_invalid",
    "graph_private_state_incompatible", "execution_owner_changed",
    "execution_dependency_unaccepted", "execution_stop_unconfirmed", "invalid_state",
    "stale_data_revision", "stale_head_revision", "graph_invalid_settlement",
    "graph_invalid_output", "graph_unknown_data_type", "graph_acceptance_basis_changed",
    "failed_retry_unavailable", "recovery_unavailable", "request_abandoned",
    "storage_contract_violation",
})

# Codes that deliberately stay outside this catalogue (partial coverage).
_NOT_IN_CATALOGUE = frozenset({
    # generic request contract
    "invalid_request", "not_found", "service_closed",
    # host package declaration registration only
    "graph_invalid_type", "graph_invalid_registration_scope", "graph_duplicate_type",
    "graph_invalid_execution_class", "host_registry_frozen", "runtime_missing_executor",
    "runtime_invalid_executor", "runtime_executor_conflict", "graph_invalid_checkpoint_save",
    "graph_invalid_session_item_declaration", "graph_missing_session_item_type",
    "graph_declaration_update_scope", "graph_declaration_update_contract_changed",
    # delegated sub-protocols and package/model/prompt implementations
    "frontend_node_input_unbound", "request_evidence_conflict", "request_evidence_pending",
    "storage_unavailable", "model_request_invalid", "prompt_lifecycle_invalid",
    "checkpoint_target_not_fresh",
    # definition.inputs diagnostics and workflow management commands
    "consumer_input_undeclared", "consumer_input_invalid", "consumer_input_conflict",
    "active_execution_not_copyable",
})


def _assert_stage_covered(contract, stage, codes):
    errors = contract["errors"]
    phases = set(contract["diagnostics"]["phases"])
    for code in codes:
        assert code in errors, (stage, code)
        row = errors[code]
        assert row["code"] == code
        assert isinstance(row["status"], int) and 400 <= row["status"] <= 599
        assert row["summary"]
        assert stage in row["phases"], (stage, code, row["phases"])
        assert set(row["phases"]) <= phases
        assert set(row["location_fields"]) == {
            "node_id", "port_id", "edge_id", "dependent_outputs", "path", "expected", "actual"}


def test_document_contract_covers_chain_diagnostics_and_declares_boundary(tmp_path):
    """S3-05/F1: the catalogue covers the reachable chain and states its boundary."""
    contract = document_contract()
    # The S3-05 fix is additive catalogue data, so the contract version is kept.
    assert contract["contract_version"] == DOCUMENT_CONTRACT_VERSION == 2
    errors = contract["errors"]
    for stage, codes in (
            ("save", _SAVE_REACHABLE), ("compile", _COMPILE_REACHABLE),
            ("prepare", _PREPARE_REACHABLE), ("run", _RUN_REACHABLE),
            ("settlement", _SETTLEMENT_REACHABLE), ("storage", _STORAGE_REACHABLE)):
        _assert_stage_covered(contract, stage, codes)
        assert any(stage in row["phases"] for row in errors.values())

    # Every code the coverage fix added is now present and linked to real stages.
    assert len(_NEWLY_COVERED) == 64
    assert _NEWLY_COVERED <= set(errors)
    expected_rows = {
        "graph_invalid_package_lock": (400, ["save", "compile"]),
        "session_preload_variable_unregistered": (400, ["save", "compile"]),
        "graph_configuration_not_adjustable": (400, ["save"]),
        "graph_edit_sequence_exhausted": (409, ["save"]),
        "graph_configuration_mutation_not_allowed": (400, ["save"]),
        "package_retired": (409, ["prepare"]),
        "package_publication_failed": (503, ["save", "prepare", "run"]),
        "graph_host_unavailable": (400, ["run"]),
        "graph_artifact_binding_mismatch": (400, ["run"]),
        "graph_artifact_reference_denied": (400, ["run", "settlement"]),
        "graph_invalid_output": (400, ["run", "settlement"]),
        "graph_invalid_settlement": (400, ["settlement"]),
        "session_object_deleted": (400, ["run"]),
        "variable_type_mismatch": (400, ["run"]),
        "private_state_not_declared": (409, ["run"]),
        "graph_private_state_invalid": (400, ["run"]),
        "graph_acceptance_basis_changed": (409, ["run", "settlement"]),
        "request_abandoned": (409, ["save", "run"]),
        "storage_contract_violation": (500, ["save", "run", "settlement", "storage"]),
    }
    for code, (status, phases) in expected_rows.items():
        assert errors[code]["status"] == status, code
        assert errors[code]["phases"] == phases, code
    # The pre-existing compile row gained its node-execution stage.
    assert errors["graph_type_mismatch"]["phases"] == ["compile", "run"]

    # Partial coverage is declared and the excluded vocabulary is not promised.
    notes = contract["notes"]
    assert any("PARTIAL catalogue" in note for note in notes)
    assert any("not the complete set" in note for note in notes)
    assert any("does not enumerate" in note for note in notes)
    assert any("extension:" in note for note in notes)
    assert any("contract_version stays 2" in note for note in notes)
    assert _NOT_IN_CATALOGUE.isdisjoint(errors)
    assert len(errors) == 116

    # The discovery surface reports the same unchanged version and plain JSON.
    service = _service(tmp_path, "coverage.sqlite", _registry())
    try:
        application = GraphApplication(service)
        described = application.describe()
        assert described["boundary"]["document_contract_version"] == DOCUMENT_CONTRACT_VERSION
        served = application.query("document.contract")
        assert served == contract and json.loads(json.dumps(contract)) == contract
    finally:
        service.close()


def test_document_contract_matches_actual_rules_limits_and_examples(tmp_path):
    registry = _registry()
    service = _service(tmp_path, "contract.sqlite", registry)
    try:
        application = GraphApplication(service)
        contract = application.query("document.contract")
        assert (contract["schema_version"], contract["kind"]) == (1, "workflow.document-contract")
        assert contract["contract_version"] == DOCUMENT_CONTRACT_VERSION
        document_contract_section = contract["document"]
        assert document_contract_section["supported_versions"] == list(SUPPORTED_GRAPH_VERSIONS)
        assert document_contract_section["limits"] == {
            "max_nodes": MAX_GRAPH_NODES, "max_edges": MAX_GRAPH_EDGES,
            "max_bytes": MAX_GRAPH_BYTES, "max_content_chars": MAX_CONTENT_CHARS,
        }
        schema = document_contract_section["json_schema"]
        Draft202012Validator.check_schema(schema)
        for example in document_contract_section["examples"]:
            Draft202012Validator(schema).validate(example)
            assert validate_graph_document(example) == example

        errors = contract["errors"]
        assert "graph_missing_edge_node" in errors
        assert set(errors["graph_missing_edge_node"]["phases"]) == {"save", "compile"}
        assert errors["graph_missing_config_reference"]["phases"] == ["save", "compile"]
        assert errors["workflow_modification_busy"]["status"] == 409
        assert errors["graph_storage_busy"]["phases"] == ["storage"]

        diagnostics = contract["diagnostics"]
        names = [field["name"] for field in diagnostics["fields"]]
        for field in ("code", "message", "node_id", "port_id", "edge_id",
                      "dependent_outputs", "phase", "path", "expected", "actual"):
            assert field in names
        assert diagnostics["phases"] == ["save", "compile", "prepare", "run",
                                         "settlement", "storage"]
        assert "reason_code" in diagnostics["reason_code_mapping"]

        described = application.describe()
        assert described["boundary"]["document_contract"] == "document.contract"
        assert described["boundary"]["document_contract_version"] == DOCUMENT_CONTRACT_VERSION
        query = next(item for item in described["queries"] if item["name"] == "document.contract")
        assert query["kind"] == "query" and query["idempotency"] == "none"
        # The contract is plain JSON and does not claim full runtime coverage.
        assert json.loads(json.dumps(contract)) == contract
        assert any("does not claim" in note or "not" in note for note in contract["notes"])
    finally:
        service.close()


def test_document_contract_function_is_self_contained():
    first, second = document_contract(), document_contract()
    assert first == second
    assert first["discovery"]["document_contract_query"] == "document.contract"
    # Mutating one returned copy does not change the module source of truth.
    first["errors"].clear()
    assert document_contract()["errors"]
