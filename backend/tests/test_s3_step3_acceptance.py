"""Independent verifier acceptance for step 3 backend (V3-01..V3-05).

Owned by the verifier; independent from the implementation team's tests. Uses an
isolated temporary SQLite database per test and the real service/application
boundaries (save, compile, preview, prepare/run, G21, close). No product service,
business database or model call is used.

Run (backend directory):

    & '..\\.venv\\Scripts\\python.exe' -m pytest -q tests/test_s3_step3_acceptance.py \
        --basetemp <new isolated directory>
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from phase1_agent import workflow_modification_gate as gate_module
from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import (
    DOCUMENT_CONTRACT_VERSION, MAX_GRAPH_BYTES, MAX_GRAPH_EDGES, MAX_GRAPH_NODES, MAX_CONTENT_CHARS,
    SUPPORTED_GRAPH_VERSIONS, GraphCompiler, NodeDefinition, NodePort, document_contract,
    validate_graph_document,
)
from phase1_agent.graph_service import GraphWorkflowService

from test_graph_service import document, edge, node, text_graph


# ---------------------------------------------------------------------------
# local fixtures and helpers
# ---------------------------------------------------------------------------

def _registry():
    return create_package_registry().registry.detached()


@pytest.fixture
def service(tmp_path):
    instance = GraphWorkflowService(tmp_path / "s3-acceptance.sqlite", registry=_registry())
    try:
        yield instance
    finally:
        instance.close()


def _schema3(value, registry):
    result = deepcopy(value)
    result["schema_version"] = 3
    result.setdefault("package_lock", list(registry.execution_package_lock))
    for entry in result["nodes"]:
        entry["configuration_policy"] = deepcopy(registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"])
    return result


def _save(service, value, sequence=0, key=None):
    return service.update_definition(
        deepcopy(value), workflow_definition_id=value["workflow_definition_id"],
        expected_edit_sequence=sequence, idempotency_key=key or str(uuid4()))


def _rejected(call, code, status=400):
    with pytest.raises(ContractValidationError) as error:
        call()
    assert error.value.reason_code == code, error.value.reason_code
    assert error.value.status_code == status, error.value.status_code
    return error.value


def _counts(database):
    with closing(sqlite3.connect(
            Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return {table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                for table in ("records", "idempotency", "workflow_current_configuration",
                              "workflow_management", "pending_commands")}


def _record_count(database, record_type):
    with closing(sqlite3.connect(
            Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type=?", (record_type,)).fetchone()[0]


def _receipt(database, operation, key):
    with closing(sqlite3.connect(
            Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute(
            "SELECT * FROM idempotency WHERE operation=? AND key=?", (operation, key)).fetchone()


def _application(service):
    return GraphApplication(service)


def _preview(service, value, expected_edit_sequence=0):
    return _application(service).query("definition.preview", {
        "workflow_definition_id": value["workflow_definition_id"],
        "document": deepcopy(value), "expected_edit_sequence": expected_edit_sequence})


def _binding(object_key, type_id, *, readers=(), writers=(), schema_version=1, scope="shared",
             owner=None):
    return {"object_key": object_key, "type_id": type_id, "schema_version": schema_version,
            "scope": scope, "readers": list(readers), "writers": list(writers),
            "owner_node_id": owner}


def _add_reference_nodes(registry):
    registry.register(NodeDefinition(
        "verifier.source", "1", "Verifier source", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT"),)), lambda config, inputs, context: {})
    registry.register(NodeDefinition(
        "verifier.sink", "1", "Verifier sink", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "JSON"),), is_output=True), lambda config, inputs, context: {})
    registry.register(NodeDefinition(
        "verifier.output", "1", "Verifier output", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT"),), is_output=True), lambda config, inputs, context: {})
    registry.register(NodeDefinition(
        "verifier.refer", "1", "Verifier refer", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT"),), outputs=(NodePort("output", "TEXT"),),
        config_node_references=(
            {"config_field": "source_ref", "multiple": False, "allow_empty": True,
             "target_types": [{"component_id": "verifier.source", "component_version": "1"}]},
        )), lambda config, inputs, context: {})
    registry.register(NodeDefinition(
        "verifier.access", "1", "Verifier access", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT"),), capabilities=("objects:read",),
        object_accesses=(
            {"config_field": "object_key", "type_id": "verifier.object", "schema_version": 1,
             "access": "read", "multiple": False},
        )), lambda config, inputs, context: {})


def _node(component_id, identity_note, *, config=None, version="1"):
    return {"node_binding_id": str(uuid4()), "component_id": component_id,
            "component_version": version, "title": identity_note,
            "position": {"x": 0, "y": 0}, "config": dict(config or {}),
            "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"}}


def _edge(source, source_port, target, target_port, order=0):
    return {"edge_id": str(uuid4()), "source_node_id": source["node_binding_id"],
            "source_port_id": source_port, "target_node_id": target["node_binding_id"],
            "target_port_id": target_port, "order": order}


def _document(nodes, edges=(), *, bindings=()):
    return {"schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": "verifier step 3", "nodes": list(nodes), "edges": list(edges),
            "object_bindings": list(bindings), "package_lock": []}


# ---------------------------------------------------------------------------
# V3-01: ordinary edge endpoint existence is a save hard rule; incomplete
# graphs still save, and the compiler checks independently.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["source_node_id", "target_node_id"])
def test_v3_01_dangling_normal_edge_is_rejected_with_location_and_no_partial_write(
        service, field):
    value = _schema3(text_graph(service.registry), service.registry)
    identity = value["workflow_definition_id"]
    edge_row = value["edges"][0]
    missing = str(uuid4())
    edge_row[field] = missing
    before = _counts(service.database)
    key = "dangling-" + field
    error = _rejected(lambda: _save(service, value, 0, key), "graph_missing_edge_node")
    assert error.status_code == 400
    diagnostic = error.diagnostics[0]
    assert diagnostic["code"] == error.reason_code
    assert diagnostic["phase"] == "save"
    assert diagnostic["path"] == f"/edges/0/{field}"
    assert diagnostic["edge_id"] == edge_row["edge_id"]
    assert diagnostic["node_id"] == missing
    assert diagnostic["expected"] == {"node_id_exists": True}
    assert diagnostic["actual"] == {"node_id_exists": False, "node_id": missing}
    # No partial write: no record, current configuration, management row or receipt.
    assert _counts(service.database) == before
    assert _receipt(service.database, "graph.definition.update", key) is None
    _rejected(lambda: service.get_definition(identity), "not_found", 404)


def test_v3_01_compiler_checks_edge_endpoints_without_the_save_gate(service):
    value = _schema3(text_graph(service.registry), service.registry)
    missing = str(uuid4())
    value["edges"][1]["target_node_id"] = missing
    # The document is not savable, but an unsaved/historical document still goes
    # through the compiler and must not silently ignore the missing target.
    with pytest.raises(ContractValidationError) as error:
        GraphCompiler(service.registry).compile(value)
    assert error.value.reason_code == "graph_missing_edge_node"
    diagnostic = error.value.diagnostics[0]
    assert diagnostic["phase"] == "compile"
    assert diagnostic["edge_id"] == value["edges"][1]["edge_id"]
    assert diagnostic["node_id"] == missing


def test_v3_01_incomplete_graph_saves_but_does_not_compile(service):
    value = _schema3(text_graph(service.registry), service.registry)
    value["edges"] = []
    saved = _save(service, value, 0, "incomplete")
    assert (saved["outcome"], saved["definition_revision"], saved["edit_sequence"]) == ("saved", 1, 1)
    assert service.get_definition(saved["workflow_definition_id"])["edges"] == []
    with pytest.raises(ContractValidationError) as compile_error:
        GraphCompiler(service.registry).compile(saved["document"])
    diagnostic = compile_error.value.diagnostics[0]
    assert diagnostic["phase"] == "compile"
    assert "path" in diagnostic
    # Completing the graph later is a normal save on the accepted baseline.
    completed = deepcopy(saved["document"])
    completed["edges"] = [
        {**deepcopy(text_graph(service.registry)["edges"][0]),
         "source_node_id": completed["nodes"][0]["node_binding_id"],
         "target_node_id": completed["nodes"][1]["node_binding_id"]},
        {**deepcopy(text_graph(service.registry)["edges"][1]),
         "source_node_id": completed["nodes"][1]["node_binding_id"],
         "target_node_id": completed["nodes"][2]["node_binding_id"]},
    ]
    again = _save(service, completed, 1, "completed")
    assert again["outcome"] == "saved" and again["edit_sequence"] == 2
    assert GraphCompiler(service.registry).compile(again["document"]).ordered_node_ids


# ---------------------------------------------------------------------------
# V3-02: declared references are checked at save; unconfigured values stay
# allowed and nothing is cleaned or migrated implicitly.
# ---------------------------------------------------------------------------

def _reference_graph(registry):
    source = _node("verifier.source", "source")
    refer = _node("verifier.refer", "refer", config={"source_ref": source["node_binding_id"]})
    output = _node("verifier.output", "output")
    value = _document([source, refer, output],
                      [_edge(source, "output", refer, "input"),
                       _edge(refer, "output", output, "input")])
    return _schema3(value, registry)


def test_v3_02_deleting_a_referenced_node_is_rejected_with_referrer_location(tmp_path):
    registry = _registry()
    _add_reference_nodes(registry)
    instance = GraphWorkflowService(tmp_path / "references.sqlite", registry=registry)
    try:
        value = _reference_graph(registry)
        identity = value["workflow_definition_id"]
        saved = _save(instance, value, 0, "reference-initial")
        before = _counts(instance.database)
        refer_index = next(index for index, row in enumerate(saved["document"]["nodes"])
                           if row["component_id"] == "verifier.refer")
        refer = saved["document"]["nodes"][refer_index]
        # Delete the referenced source node and its edge but keep the configured
        # reference value untouched: this is an explicit dangling reference.
        deleted = deepcopy(saved["document"])
        source_id = refer["config"]["source_ref"]
        deleted["nodes"] = [row for row in deleted["nodes"]
                            if row["node_binding_id"] != source_id]
        deleted["edges"] = [row for row in deleted["edges"]
                            if row["source_node_id"] != source_id]
        deleted_refer_index = next(index for index, row in enumerate(deleted["nodes"])
                                   if row["node_binding_id"] == refer["node_binding_id"])
        error = _rejected(lambda: _save(instance, deleted, saved["edit_sequence"], "delete-referenced"),
                          "graph_missing_config_reference")
        diagnostic = error.diagnostics[0]
        assert diagnostic["phase"] == "save"
        assert diagnostic["path"] == f"/nodes/{deleted_refer_index}/config/source_ref"
        assert diagnostic["node_id"] == refer["node_binding_id"]
        assert diagnostic["expected"]["target_types"] == [
            {"component_id": "verifier.source", "component_version": "1"}]
        assert diagnostic["actual"] == {"node_id": source_id, "node_id_exists": False}
        # Nothing was written and the configured value was not silently cleaned.
        assert _counts(instance.database) == before
        current = instance.get_definition(identity)
        assert current["revision"] == saved["definition_revision"]
        assert any(row["node_binding_id"] == source_id for row in current["nodes"])
        assert next(row for row in current["nodes"]
                    if row["node_binding_id"] == refer["node_binding_id"])["config"]["source_ref"] == source_id

        # A declared reference with the wrong target type is located as well.
        wrong = deepcopy(saved["document"])
        next(row for row in wrong["nodes"]
             if row["node_binding_id"] == refer["node_binding_id"])["config"]["source_ref"] = \
            next(row["node_binding_id"] for row in wrong["nodes"]
                 if row["component_id"] == "verifier.output")
        type_error = _rejected(lambda: _save(instance, wrong, saved["edit_sequence"], "wrong-type"),
                               "graph_config_reference_type_mismatch")
        assert type_error.diagnostics[0]["path"] == f"/nodes/{refer_index}/config/source_ref"

        # An unconfigured declared reference (allow_empty "") is valid.
        unconfigured = deepcopy(saved["document"])
        unconfigured["nodes"] = [row for row in unconfigured["nodes"]
                                 if row["node_binding_id"] != source_id]
        unconfigured["edges"] = [row for row in unconfigured["edges"]
                                 if row["source_node_id"] != source_id]
        next(row for row in unconfigured["nodes"]
             if row["component_id"] == "verifier.refer")["config"]["source_ref"] = ""
        accepted = _save(instance, unconfigured, saved["edit_sequence"], "unconfigured")
        assert accepted["outcome"] == "saved"
    finally:
        instance.close()


def test_v3_02_object_key_binding_rules_are_shared_and_not_migrated(tmp_path):
    registry = _registry()
    _add_reference_nodes(registry)
    instance = GraphWorkflowService(tmp_path / "objects.sqlite", registry=registry)
    try:
        access = _node("verifier.access", "access", config={"object_key": "ctx"})
        value = _schema3(_document([access]), registry)
        identity = value["workflow_definition_id"]

        # A configured object key without a binding is rejected at save.
        error = _rejected(lambda: _save(instance, value, 0, "unbound"), "session_object_unbound")
        diagnostic = error.diagnostics[0]
        assert diagnostic["phase"] == "save"
        assert diagnostic["path"] == "/nodes/0/config/object_key"
        assert diagnostic["node_id"] == access["node_binding_id"]
        assert diagnostic["expected"] == {"object_key": "ctx", "type_id": "verifier.object",
                                          "schema_version": 1}
        assert diagnostic["actual"] == {"object_key": "ctx", "bound": False}

        # A binding with the wrong declared type is rejected with a location.
        wrong = deepcopy(value)
        wrong["object_bindings"] = [_binding(
            "ctx", "verifier.other", readers=[access["node_binding_id"]])]
        type_error = _rejected(lambda: _save(instance, wrong, 0, "wrong-object-type"),
                               "session_object_type_mismatch")
        assert type_error.diagnostics[0]["path"] == "/nodes/0/config/object_key"

        # A correctly bound declared object saves, and deleting the binding later
        # is rejected instead of being cleaned up or migrated.
        bound = deepcopy(value)
        bound["object_bindings"] = [_binding(
            "ctx", "verifier.object", readers=[access["node_binding_id"]])]
        saved = _save(instance, bound, 0, "bound")
        assert saved["outcome"] == "saved"
        removed = deepcopy(saved["document"])
        removed["object_bindings"] = []
        before = _counts(instance.database)
        _rejected(lambda: _save(instance, removed, saved["edit_sequence"], "unbind"),
                  "session_object_unbound")
        assert _counts(instance.database) == before
        assert len(instance.get_definition(identity)["object_bindings"]) == 1

        # An unconfigured object access value is allowed without a binding.
        empty = deepcopy(bound)
        empty["object_bindings"] = []
        empty["nodes"][0]["config"]["object_key"] = ""
        empty["workflow_definition_id"] = str(uuid4())
        unconfigured = _save(instance, empty, 0, "object-unconfigured")
        assert unconfigured["outcome"] == "saved"
    finally:
        instance.close()


def test_v3_02_replacement_is_saved_but_incompatible_edges_stay_compile_diagnostics(tmp_path):
    registry = _registry()
    _add_reference_nodes(registry)
    instance = GraphWorkflowService(tmp_path / "replacement.sqlite", registry=registry)
    try:
        source = _node("verifier.source", "source")
        output = _node("verifier.output", "output")
        value = _schema3(_document([source, output],
                                   [_edge(source, "output", output, "input")]), registry)
        saved = _save(instance, value, 0, "replacement-initial")
        assert saved["outcome"] == "saved"
        # Replace the TEXT-producing source by a node without outputs: the save
        # structure stays legal (endpoints exist) while port compatibility is a
        # compile-only diagnostic.
        replaced = deepcopy(saved["document"])
        source_row = next(row for row in replaced["nodes"]
                          if row["node_binding_id"] == source["node_binding_id"])
        source_row["component_id"] = "verifier.sink"
        source_row["config"] = {}
        accepted = _save(instance, replaced, saved["edit_sequence"], "replacement")
        assert accepted["outcome"] == "saved"
        with pytest.raises(ContractValidationError) as compile_error:
            GraphCompiler(instance.registry).compile(accepted["document"])
        diagnostics = compile_error.value.diagnostics[0]
        assert diagnostics["phase"] == "compile"
        assert diagnostics["code"] in {
            "graph_type_mismatch", "graph_unknown_port", "graph_required_input_missing"}
        assert diagnostics.get("path", "").startswith(("/edges/", "/nodes/"))

        # A compatible replacement keeps the existing edge identities and order.
        second = GraphWorkflowService(tmp_path / "replacement-compatible.sqlite", registry=registry)
        try:
            second.registry.register(NodeDefinition(
                "verifier.alt-source", "1", "Verifier alt source", "Test", {}, {"type": "object"},
                outputs=(NodePort("output", "TEXT"),)), lambda config, inputs, context: {})
            compatible_source = _node("verifier.source", "source")
            compatible_output = _node("verifier.output", "output")
            compatible = _schema3(_document(
                [compatible_source, compatible_output],
                [_edge(compatible_source, "output", compatible_output, "input")]), registry)
            original = _save(second, compatible, 0, "compatible-initial")
            replaced_compatible = deepcopy(original["document"])
            compatible_row = next(row for row in replaced_compatible["nodes"]
                                  if row["node_binding_id"] == compatible_source["node_binding_id"])
            compatible_row["component_id"] = "verifier.alt-source"
            compatible_row["config"] = {}
            updated = _save(second, replaced_compatible, original["edit_sequence"], "compatible")
            assert updated["outcome"] == "saved"
            assert [row["edge_id"] for row in updated["document"]["edges"]] == [
                row["edge_id"] for row in original["document"]["edges"]]
            assert GraphCompiler(second.registry).compile(updated["document"]).ordered_node_ids
        finally:
            second.close()
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# V3-03: located save diagnostics and the versioned contract source of truth.
# ---------------------------------------------------------------------------

def test_v3_03_save_diagnostics_carry_phase_path_and_bounded_summaries(service):
    base = _schema3(text_graph(service.registry), service.registry)
    cases = []
    wrong_version = deepcopy(base)
    wrong_version["schema_version"] = 9
    cases.append((wrong_version, "graph_invalid_document", "/schema_version", 400))
    empty_name = deepcopy(base)
    empty_name["name"] = ""
    cases.append((empty_name, "graph_invalid_document", "/name", 400))
    missing_config = deepcopy(base)
    missing_config["nodes"][0].pop("config")
    cases.append((missing_config, "graph_invalid_document", "/nodes/0", 400))
    bad_port = deepcopy(base)
    bad_port["edges"][0]["source_port_id"] = 5
    cases.append((bad_port, "graph_invalid_document", "/edges/0", 400))
    for value, code, path, status in cases:
        error = _rejected(lambda value=value: validate_graph_document(value), code, status)
        diagnostic = error.diagnostics[0]
        assert diagnostic["code"] == code
        assert diagnostic["phase"] == "save"
        assert diagnostic["path"] == path
        # Unknown locations stay absent/None instead of being invented.
        assert diagnostic.get("node_id") is None
        assert "expected" not in diagnostic or isinstance(diagnostic["expected"], (list, dict))
        if path == "/schema_version":
            assert diagnostic["expected"] == list(SUPPORTED_GRAPH_VERSIONS)
            assert diagnostic["actual"] == 9

    # The same location reaches the save boundary and the transaction writes nothing.
    before = _counts(service.database)
    invalid = deepcopy(base)
    invalid["name"] = ""
    error = _rejected(lambda: _save(service, invalid, 0, "invalid-name"),
                      "graph_invalid_document")
    assert error.diagnostics[0]["path"] == "/name"
    assert _counts(service.database) == before
    _rejected(lambda: service.get_definition(invalid["workflow_definition_id"]), "not_found", 404)


def test_v3_03_contract_is_single_source_and_matches_observed_catalogue(service):
    contract = document_contract()
    assert contract["kind"] == "workflow.document-contract"
    assert contract["contract_version"] == DOCUMENT_CONTRACT_VERSION == 2
    assert contract["document"]["limits"] == {
        "max_nodes": MAX_GRAPH_NODES, "max_edges": MAX_GRAPH_EDGES,
        "max_bytes": MAX_GRAPH_BYTES, "max_content_chars": MAX_CONTENT_CHARS}
    schema = contract["document"]["json_schema"]
    Draft202012Validator.check_schema(schema)
    for example in contract["document"]["examples"]:
        Draft202012Validator(schema).validate(example)
        validate_graph_document(example)
    errors = contract["errors"]
    observed_codes = {
        "graph_invalid_document", "graph_missing_edge_node", "graph_no_outputs",
        "graph_missing_config_reference", "graph_config_reference_type_mismatch",
        "session_object_unbound", "session_object_type_mismatch",
        "stale_edit_sequence", "configuration_run_locked", "package_missing_dependency",
    }
    assert observed_codes <= set(errors)
    phases = set(contract["diagnostics"]["phases"])
    for code in observed_codes:
        row = errors[code]
        assert row["code"] == code and isinstance(row["status"], int)
        assert row["summary"]
        assert row["phases"] and set(row["phases"]) <= phases
        assert set(row["location_fields"]) <= {
            "node_id", "port_id", "edge_id", "dependent_outputs", "path", "expected", "actual"}
    # The discovery surface and the boundary report the same version/query.
    described = _application(service).describe()
    assert described["boundary"]["document_contract_version"] == DOCUMENT_CONTRACT_VERSION
    assert described["boundary"]["document_contract"] == "document.contract"
    assert described["boundary"]["preview_query"] == "definition.preview"
    assert _application(service).query("document.contract")["contract_version"] == DOCUMENT_CONTRACT_VERSION
    # Render is deterministic and deletes nothing from the module source.
    first, second = document_contract(), document_contract()
    first["errors"].clear()
    assert second == document_contract() and document_contract()["errors"]


def test_v3_03_diagnostic_reason_code_mapping_is_consistent(service):
    # DiagnosticError: outer reason_code == diagnostics[0].code.
    value = _schema3(text_graph(service.registry), service.registry)
    value["edges"][0]["target_node_id"] = str(uuid4())
    error = _rejected(lambda: _save(service, value, 0, "mapping"), "graph_missing_edge_node")
    assert error.diagnostics[0]["code"] == error.reason_code
    # Located compile errors carry the same code mapping at compile stage.
    compile_value = _schema3(text_graph(service.registry), service.registry)
    compile_value["edges"] = []
    with pytest.raises(ContractValidationError) as compile_error:
        GraphCompiler(service.registry).compile(compile_value)
    assert compile_error.value.diagnostics[0]["code"] == compile_error.value.reason_code


# ---------------------------------------------------------------------------
# V3-04: save succeeds while compile fails; prepare failures leave no run
# records; preview is read-only and reserves nothing.
# ---------------------------------------------------------------------------

def test_v3_04_save_succeeds_with_compile_error_and_start_keeps_original_response(service):
    value = _schema3(text_graph(service.registry), service.registry)
    value["edges"] = []
    identity = value["workflow_definition_id"]
    app = _application(service)
    saved = app.command("definition.update", {
        "workflow_definition_id": identity, "document": deepcopy(value),
        "expected_edit_sequence": 0, "idempotency_key": "save-with-compile-error"})
    assert saved["result"]["outcome"] == "saved"
    session = service.create_session(identity, saved["result"]["definition_revision"],
                                     idempotency_key=str(uuid4()))
    error = _rejected(lambda: service.start(
        session["workflow_session_id"], expected_revision=session["revision"],
        idempotency_key="start-compile-error"), "graph_no_outputs")
    diagnostic = error.diagnostics[0]
    assert diagnostic["phase"] == "compile"
    assert diagnostic["code"] == error.reason_code
    assert diagnostic["message"]
    # No chain or node run record exists; the session is unchanged.
    assert _record_count(service.database, "chain_run") == 0
    assert _record_count(service.database, "node_run") == 0
    view = service.get_session(session["workflow_session_id"])
    assert view["chains"] == [] and view["active_chain_run_id"] is None
    # The original error response is retained (re-reading the session cannot
    # reproduce it), so the caller still holds code/message/diagnostics.
    assert error.diagnostics and error.status_code == 400

    # Completing the fixed revision and starting a session on the repaired
    # revision succeeds; the compile-failed session stays pinned to the old one.
    # The fixed change needs confirmation because an unarchived session exists.
    fixed = deepcopy(saved["result"]["document"])
    fixed["edges"] = [
        {**deepcopy(text_graph(service.registry)["edges"][0]),
         "source_node_id": fixed["nodes"][0]["node_binding_id"],
         "target_node_id": fixed["nodes"][1]["node_binding_id"]},
        {**deepcopy(text_graph(service.registry)["edges"][1]),
         "source_node_id": fixed["nodes"][1]["node_binding_id"],
         "target_node_id": fixed["nodes"][2]["node_binding_id"]},
    ]
    repaired = service.update_definition(
        fixed, workflow_definition_id=identity,
        expected_edit_sequence=saved["result"]["edit_sequence"],
        idempotency_key="completed-after-compile", confirm_derivation=True)
    assert repaired["outcome"] == "derived"
    repaired_identity = repaired["workflow_definition_id"]
    assert repaired_identity != identity
    retry_session = service.create_session(
        repaired_identity, repaired["definition_revision"], idempotency_key=str(uuid4()))
    started = service.start(retry_session["workflow_session_id"],
                            expected_revision=retry_session["revision"],
                            idempotency_key="start-after-fix")
    service.wait(started["active_chain_run_id"])
    assert service.get_run(retry_session["workflow_session_id"],
                           started["active_chain_run_id"])["chain"]["status"] == "succeeded"


def test_v3_04_prepare_failure_has_no_run_records_and_is_retryable(service):
    value = _schema3(text_graph(service.registry), service.registry)
    saved = _save(service, value, 0, "prepare")
    identity = saved["workflow_definition_id"]
    session = service.create_session(identity, saved["definition_revision"],
                                     idempotency_key=str(uuid4()))
    previous = list(service._package_diagnostics)
    service._package_diagnostics = [{"package_id": "verifier.missing", "version": "1"}]
    try:
        error = _rejected(lambda: service.start(
            session["workflow_session_id"], expected_revision=session["revision"],
            idempotency_key="prepare-fail"), "package_missing_dependency", 409)
    finally:
        service._package_diagnostics = previous
    diagnostic = error.diagnostics[0]
    assert diagnostic["phase"] == "prepare"
    assert diagnostic["code"] == error.reason_code
    assert _record_count(service.database, "chain_run") == 0
    assert _record_count(service.database, "node_run") == 0
    view = service.get_session(session["workflow_session_id"])
    assert view["chains"] == [] and view["active_chain_run_id"] is None
    started = service.start(session["workflow_session_id"], expected_revision=view["revision"],
                            idempotency_key="prepare-retry")
    service.wait(started["active_chain_run_id"])
    assert service.get_run(session["workflow_session_id"],
                           started["active_chain_run_id"])["chain"]["status"] == "succeeded"


def test_v3_04_preview_writes_nothing_takes_no_entitlement_and_submit_rechecks(service):
    value = _schema3(text_graph(service.registry), service.registry)
    identity = value["workflow_definition_id"]
    before = _counts(service.database)
    assert gate_module._in_flight == {}
    preview = _preview(service, value, 0)
    assert preview["kind"] == "workflow.configuration-preview"
    assert preview["contract_version"] == DOCUMENT_CONTRACT_VERSION
    assert preview["read_only"] is True and preview["reserves_save_right"] is False
    assert preview["structure"] == {"ok": True, "diagnostics": []}
    assert preview["declared_references"] == {"ok": True, "diagnostics": []}
    assert preview["compilation"]["ok"] is True
    assert preview["changes"]["would_outcome"] == "saved"
    assert preview["baseline"]["edit_sequence_matches"] is True
    assert preview["run_conditions"]["package_lock_available"] is True
    assert any("model" in item for item in preview["run_conditions"]["not_checked"])
    # Read-only: no table changed, no save right/modification entitlement taken.
    assert _counts(service.database) == before
    assert gate_module._in_flight == {}
    assert _application(service).query("definition.preview", {
        "workflow_definition_id": identity, "document": deepcopy(value),
        "expected_edit_sequence": 0})["read_only"] is True
    assert _counts(service.database) == before

    # Submit still executes the real rules: CAS first, then the write.
    saved = _save(service, value, 0, "after-preview")
    assert saved["outcome"] == "saved"
    stale = deepcopy(saved["document"])
    stale["name"] = "after preview rename"
    stale["edit_sequence"] = saved["edit_sequence"] + 5
    error = _rejected(lambda: _save(service, stale, saved["edit_sequence"] + 5, "stale-after-preview"),
                      "stale_edit_sequence", 409)
    assert error.diagnostics[0]["phase"] == "save"
    accepted_document = deepcopy(saved["document"])
    accepted_document["name"] = "after preview accepted rename"
    accepted = _save(service, accepted_document, saved["edit_sequence"], "after-preview-2")
    assert accepted["outcome"] == "saved"

    # G21 is reported by the preview and re-checked by the actual submission.
    gated = _gated_graph(service)
    gate_saved = _save(service, gated["document"], 0, "gated-save")
    session = service.create_session(gate_saved["workflow_definition_id"],
                                     gate_saved["definition_revision"], idempotency_key=str(uuid4()))
    started = service.start(session["workflow_session_id"],
                            expected_revision=session["revision"], idempotency_key="gated-start")
    assert gated["entered"].wait(5)
    try:
        locked_preview = _preview(service, gate_saved["document"], gate_saved["edit_sequence"])
        assert locked_preview["baseline"]["run_locked"] is True
        assert len(locked_preview["baseline"]["occupying_sessions"]) == 1
        assert gate_module._in_flight == {}
        _rejected(lambda: _save(service, gate_saved["document"], gate_saved["edit_sequence"],
                                "g21-submit"), "configuration_run_locked", 409)
    finally:
        gated["release"].set()
    service.wait(started["active_chain_run_id"])


def _gated_graph(service):
    from threading import Event
    entered, release = Event(), Event()

    def execute(config, inputs, context):
        entered.set()
        assert release.wait(10), "verifier gate timed out"
        return {"output": inputs["input"]}

    service.registry.register(NodeDefinition(
        "verifier.gate", "1", "Verifier gate", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), execute)
    first = node(service.registry, "tools.text", 1)
    gate = node(service.registry, "verifier.gate", 2, component_version="1")
    output = node(service.registry, "tools.output", 4)
    value = _schema3(document([first, gate, output],
                              [edge(first, gate, 1), edge(gate, output, 2)]), service.registry)
    return {"document": value, "entered": entered, "release": release}


# ---------------------------------------------------------------------------
# V3-05: G21 occupancy, close semantics and explicit close consequences.
# ---------------------------------------------------------------------------

def test_v3_05_g21_occupying_sessions_and_close_does_not_clear_all_occupancy(service):
    gated = _gated_graph(service)
    saved = _save(service, gated["document"], 0, "g21-initial")
    identity = saved["workflow_definition_id"]
    sessions = []
    for index in range(2):
        session = service.create_session(identity, saved["definition_revision"],
                                         idempotency_key=f"g21-session-{index}")
        started = service.start(session["workflow_session_id"],
                                expected_revision=session["revision"],
                                idempotency_key=f"g21-start-{index}")
        sessions.append((session, started["active_chain_run_id"]))
    assert gated["entered"].wait(5)

    # Submission is rejected with every occupying session listed.
    error = _rejected(lambda: _save(service, saved["document"], saved["edit_sequence"], "g21-blocked"),
                      "configuration_run_locked", 409)
    diagnostic = error.diagnostics[0]
    assert diagnostic["phase"] == "save"
    assert diagnostic["path"] == ""
    occupying = diagnostic["occupying_sessions"]
    assert {row["workflow_session_id"] for row in occupying} == {
        session["workflow_session_id"] for session, _ in sessions}
    assert {row["active_chain_run_id"] for row in occupying} == {
        chain_id for _, chain_id in sessions}
    preview = _preview(service, saved["document"], saved["edit_sequence"])
    assert preview["baseline"]["run_locked"] is True
    assert len(preview["baseline"]["occupying_sessions"]) == 2

    # While running, close is not available; only pause is offered.
    first_view = service.get_session(sessions[0][0]["workflow_session_id"])
    assert "close" not in first_view["available_actions"]
    assert "pause" in first_view["available_actions"]
    invalid = _rejected(lambda: service.control(
        sessions[0][0]["workflow_session_id"], action="close",
        expected_revision=first_view["revision"], idempotency_key="close-while-running"),
        "invalid_state", 409)
    assert invalid.diagnostics

    # Pause both sessions, release the gate and let them settle to paused.
    for index, (session, _) in enumerate(sessions):
        view = service.get_session(session["workflow_session_id"])
        service.control(session["workflow_session_id"], action="pause",
                        expected_revision=view["revision"],
                        idempotency_key=f"pause-{index}")
    gated["release"].set()
    for session, chain_id in sessions:
        service.wait(chain_id)
    paused = [service.get_session(session["workflow_session_id"]) for session, _ in sessions]
    assert all(view["status"] == "paused" for view in paused)
    assert all(view["available_actions"] == ["resume", "close"] for view in paused)

    # Closing one session releases only its own chain; the other still blocks.
    first_paused = paused[0]
    closed = service.control(first_paused["workflow_session_id"], action="close",
                             expected_revision=first_paused["revision"],
                             idempotency_key="close-first")
    assert closed["active_chain_run_id"] is None
    assert closed["chains"][0]["status"] == "closed"
    assert closed["available_actions"] == []
    assert closed["can_submit"] is True
    remaining = _rejected(lambda: _save(service, saved["document"], saved["edit_sequence"],
                                        "g21-after-first-close"), "configuration_run_locked", 409)
    remaining_sessions = remaining.diagnostics[0]["occupying_sessions"]
    assert [row["workflow_session_id"] for row in remaining_sessions] == [
        sessions[1][0]["workflow_session_id"]]

    # Closing the second session frees G21; close did not archive either session.
    second_paused = service.get_session(sessions[1][0]["workflow_session_id"])
    service.control(second_paused["workflow_session_id"], action="close",
                    expected_revision=second_paused["revision"], idempotency_key="close-second")
    listed = service.list_sessions(identity)
    assert {row["workflow_session_id"] for row in listed} == {
        session["workflow_session_id"] for session, _ in sessions}
    assert all(row.get("archived") is False for row in listed)
    freed = deepcopy(saved["document"])
    freed["name"] = "after explicit closes"
    accepted = _save(service, freed, saved["edit_sequence"], "g21-freed")
    assert accepted["outcome"] == "saved"
    # An unarchived closed session still needs derivation confirmation for a
    # fixed change (close is not archive and does not clear the session basis).
    fixed = deepcopy(accepted["document"])
    fixed["nodes"][0]["config"]["text"] = "after-close fixed"
    preview = _preview(service, fixed, accepted["edit_sequence"])
    assert preview["baseline"]["has_effective_session"] is True
    assert preview["changes"]["would_outcome"] == "confirmation_required"
    assert preview["changes"]["would_require_confirmation"] is True


def test_v3_05_completion_or_failure_does_not_close_the_session_automatically(service):
    value = _schema3(text_graph(service.registry), service.registry)
    saved = _save(service, value, 0, "auto-close-initial")
    identity = saved["workflow_definition_id"]
    session = service.create_session(identity, saved["definition_revision"],
                                     idempotency_key=str(uuid4()))
    started = service.start(session["workflow_session_id"], expected_revision=session["revision"],
                            idempotency_key="auto-close-start")
    service.wait(started["active_chain_run_id"])
    view = service.get_session(session["workflow_session_id"])
    assert view["chains"][-1]["status"] == "succeeded"
    assert view["active_chain_run_id"] is None
    assert view["archived"] is False
    assert view["workflow_session_id"] == session["workflow_session_id"]
    assert view["can_submit"] is True  # finished, not closed/archived

    # A failed run stays open until an explicit close, and keeps G21 occupancy.
    def fail(config, inputs, context):
        from phase1_agent.contract_errors import DiagnosticError
        raise DiagnosticError("verifier.run_failure", "injected node failure",
                              node_id=context.node_binding_id)

    service.registry.register(NodeDefinition(
        "verifier.fail", "1", "Verifier fail", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), fail)
    text_row = node(service.registry, "tools.text", 11)
    fail_row = node(service.registry, "verifier.fail", 12, component_version="1")
    out_row = node(service.registry, "tools.output", 13)
    failing = _schema3(document([text_row, fail_row, out_row],
                                [edge(text_row, fail_row, 11), edge(fail_row, out_row, 12)]),
                       service.registry)
    failing_saved = _save(service, failing, 0, "failing-graph")
    failing_session = service.create_session(
        failing_saved["workflow_definition_id"], failing_saved["definition_revision"],
        idempotency_key=str(uuid4()))
    failing_started = service.start(failing_session["workflow_session_id"],
                                    expected_revision=failing_session["revision"],
                                    idempotency_key="failing-start")
    service.wait(failing_started["active_chain_run_id"])
    failed_view = service.get_session(failing_session["workflow_session_id"])
    assert failed_view["status"] == "failed"
    assert failed_view["archived"] is False
    assert "close" in failed_view["available_actions"]
    after_failure = _rejected(lambda: _save(
        service, failing_saved["document"], failing_saved["edit_sequence"], "blocked-after-failure"),
        "configuration_run_locked", 409)
    assert [row["workflow_session_id"] for row in after_failure.diagnostics[0]["occupying_sessions"]] == [
        failing_session["workflow_session_id"]]
    closed = service.control(failing_session["workflow_session_id"], action="close",
                             expected_revision=failed_view["revision"],
                             idempotency_key="close-failed")
    assert closed["active_chain_run_id"] is None
    assert closed["chains"][-1]["status"] == "closed"
    retried = deepcopy(failing_saved["document"])
    retried["name"] = "after closing the failed chain"
    finally_saved = _save(service, retried, failing_saved["edit_sequence"],
                          "after-close-failure")
    assert finally_saved["outcome"] == "saved"


