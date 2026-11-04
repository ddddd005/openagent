"""S3-02: read-only save preview, run/prepare diagnostics, G21 occupancy, close.

Covers Q06-2 preview (no writes, no save entitlement), Q06-1 phase/path fields
on prepare/node/settlement/storage diagnostics, R01 occupying sessions, R02
close visibility semantics and the version-2 discovery contract.
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

from jsonschema import Draft202012Validator
import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_contracts import (
    DOCUMENT_CONTRACT_VERSION, NodeDefinition, NodePort, document_contract,
    validate_graph_document,
)
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.storage import SqliteStore

from test_graph_service import document, edge, node


def _schema3(value):
    value = deepcopy(value)
    value["schema_version"] = 3
    for item in value["nodes"]:
        item["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    return value


def _graph(registry):
    text = node(registry, "tools.text", 1, text="hello")
    regex = node(registry, "tools.regex", 2, pattern="hello", replacement="hi")
    output = node(registry, "tools.output", 3)
    output["public_outputs"] = ["output"]
    value = _schema3(document([text, regex, output],
                              [edge(text, regex, 1), edge(regex, output, 2)]))
    value["package_lock"] = list(registry.execution_package_lock)
    return value


@pytest.fixture
def service(tmp_path):
    registry = create_package_registry().registry.detached()
    instance = GraphWorkflowService(tmp_path / "preview.sqlite", registry=registry)
    try:
        yield instance
    finally:
        instance.close()


def _application(service):
    return GraphApplication(service)


def _preview(service, value, *, expected_edit_sequence=0):
    return _application(service).query("definition.preview", {
        "workflow_definition_id": value["workflow_definition_id"],
        "document": value,
        "expected_edit_sequence": expected_edit_sequence,
    })


def _save(service, value):
    return service.update_definition(
        value, workflow_definition_id=value["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()))


def _counts(service):
    with closing(SqliteStore(service.database)) as store:
        connection = store._connection
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("idempotency", "records", "workflow_current_configuration",
                          "workflow_management")
        }


def _gated(service):
    """Save a schema-3 gated graph and start one blocked run on it."""
    entered, release = Event(), Event()

    def execute(config, inputs, context):
        entered.set()
        assert release.wait(10), "test gate timed out"
        return {"output": inputs["input"]}

    service.registry.register(NodeDefinition(
        "test.gate", "1", "Gate", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), execute)
    text = node(service.registry, "tools.text", 1)
    gate = node(service.registry, "test.gate", 2)
    output = node(service.registry, "tools.output", 4)
    value = _schema3(document([text, gate, output],
                              [edge(text, gate, 1), edge(gate, output, 2)]))
    value["package_lock"] = list(service.registry.execution_package_lock)
    saved = _save(service, value)
    current = service.get_definition(saved["workflow_definition_id"])
    initial = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    started = service.start(initial["workflow_session_id"],
                            expected_revision=initial["revision"],
                            idempotency_key=str(uuid4()))
    assert entered.wait(5)
    return current, saved, initial, started["active_chain_run_id"], release


# -- Q06-2: the preview is read-only and evaluates each stage separately ------

def test_preview_writes_nothing_and_reports_all_stages(service):
    value = _graph(service.registry)
    before = _counts(service)
    result = _preview(service, value)
    assert _counts(service) == before
    assert result["read_only"] is True and result["reserves_save_right"] is False
    assert result["kind"] == "workflow.configuration-preview"
    assert result["contract_version"] == DOCUMENT_CONTRACT_VERSION
    assert result["structure"] == {"ok": True, "diagnostics": []}
    assert result["declared_references"] == {"ok": True, "diagnostics": []}
    assert result["compilation"]["ok"] is True
    assert result["compilation"]["target_node_ids"]
    assert result["baseline"]["configuration_exists"] is False
    assert result["baseline"]["edit_sequence_matches"] is True
    assert result["baseline"]["run_locked"] is False
    assert result["changes"]["would_outcome"] == "saved"
    assert result["changes"]["categories"] == ["fixed"]
    assert result["run_conditions"]["package_lock_available"] is True
    assert "actual background execution and result acceptance" in result["run_conditions"]["not_checked"]
    assert "model credentials and model calls" in result["run_conditions"]["not_checked"]
    # The document itself was not persisted by the preview.
    assert service.list_definitions(include_archived=True) == []


def test_preview_reports_structure_failure_without_running_compile(service):
    value = _graph(service.registry)
    missing = str(uuid4())
    value["edges"][0]["target_node_id"] = missing
    result = _preview(service, value)
    assert result["structure"]["ok"] is False
    diagnostic = result["structure"]["diagnostics"][0]
    assert diagnostic["code"] == "graph_missing_edge_node"
    assert diagnostic["phase"] == "save"
    assert diagnostic["path"] == "/edges/0/target_node_id"
    assert diagnostic["node_id"] == missing
    assert result["compilation"]["skipped"] is True
    assert result["changes"]["would_outcome"] is None


def test_preview_reports_compile_failure_while_save_stays_possible(service):
    value = _graph(service.registry)
    value["edges"] = []
    saved = _save(service, value)
    assert saved["outcome"] == "saved"
    result = _preview(service, value, expected_edit_sequence=saved["edit_sequence"])
    assert result["structure"]["ok"] is True
    assert result["declared_references"]["ok"] is True
    assert result["compilation"]["ok"] is False
    diagnostic = result["compilation"]["diagnostics"][0]
    assert diagnostic["phase"] == "compile"
    assert diagnostic["code"] == "graph_no_outputs"


def test_preview_classifies_display_and_fixed_and_predicts_confirmation(service):
    value = _graph(service.registry)
    saved = _save(service, value)
    service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    display = deepcopy(value)
    display["nodes"][0]["title"] = "Renamed"
    result = _preview(service, display, expected_edit_sequence=saved["edit_sequence"])
    assert result["changes"]["categories"] == ["display"]
    assert result["changes"]["would_outcome"] == "saved"
    assert result["changes"]["would_require_confirmation"] is False

    fixed = deepcopy(value)
    fixed["nodes"][0]["config"]["text"] = "changed"
    result = _preview(service, fixed, expected_edit_sequence=saved["edit_sequence"])
    assert result["changes"]["categories"] == ["fixed"]
    assert result["changes"]["would_outcome"] == "confirmation_required"
    assert result["changes"]["would_require_confirmation"] is True
    assert result["changes"]["would_derive_after_confirmation"] is True


def test_preview_reports_baseline_mismatch_but_does_not_write(service):
    value = _graph(service.registry)
    saved = _save(service, value)
    before = _counts(service)
    result = _preview(service, value, expected_edit_sequence=saved["edit_sequence"] + 5)
    assert _counts(service) == before
    assert result["baseline"]["edit_sequence_matches"] is False
    assert result["changes"]["would_outcome"] == "unchanged"


# -- Q06-1/R01: G21 occupancy is visible and still blocks submit -------------

def test_g21_reports_each_occupying_session_and_preview_does_not_bypass(service):
    current, saved, initial, chain_id, release = _gated(service)
    try:
        second = service.create_session(
            saved["workflow_definition_id"], saved["definition_revision"],
            idempotency_key=str(uuid4()))
        started = service.start(
            second["workflow_session_id"], expected_revision=second["revision"],
            idempotency_key=str(uuid4()))
        second_chain = started["active_chain_run_id"]
        with pytest.raises(ContractValidationError) as error:
            service.update_definition(
                current, workflow_definition_id=saved["workflow_definition_id"],
                expected_edit_sequence=saved["edit_sequence"],
                idempotency_key=str(uuid4()))
        diagnostic = error.value.diagnostics[0]
        assert error.value.reason_code == "configuration_run_locked"
        assert error.value.status_code == 409
        assert diagnostic["phase"] == "save"
        occupying = diagnostic["occupying_sessions"]
        assert {row["workflow_session_id"] for row in occupying} == {
            initial["workflow_session_id"], second["workflow_session_id"]}
        assert {row["active_chain_run_id"] for row in occupying} == {chain_id, second_chain}
        # Preview is read-only and reports the same lock instead of bypassing it.
        result = _preview(service, current, expected_edit_sequence=saved["edit_sequence"])
        assert result["baseline"]["run_locked"] is True
        assert len(result["baseline"]["occupying_sessions"]) == 2
        # Submission still re-runs G21 after a successful preview.
        with pytest.raises(ContractValidationError) as rejected:
            service.update_definition(
                current, workflow_definition_id=saved["workflow_definition_id"],
                expected_edit_sequence=saved["edit_sequence"],
                idempotency_key=str(uuid4()))
        assert rejected.value.reason_code == "configuration_run_locked"
        release.set()
        service.wait(chain_id)
        service.wait(second_chain)
    finally:
        release.set()


# -- R02: close visibility and consequences (no behavior change) -------------

def test_close_visibility_after_pause_and_effect_on_fixed_confirmation(service):
    current, saved, initial, chain_id, release = _gated(service)
    try:
        with pytest.raises(ContractValidationError) as running_close:
            view = service.get_session(initial["workflow_session_id"])
            service.control(initial["workflow_session_id"], action="close",
                            expected_revision=view["revision"],
                            idempotency_key=str(uuid4()))
        assert running_close.value.reason_code == "invalid_state"
        view = service.get_session(initial["workflow_session_id"])
        assert view["available_actions"] == ["pause"]
        service.control(initial["workflow_session_id"], action="pause",
                        expected_revision=view["revision"],
                        idempotency_key=str(uuid4()))
        # Pause takes effect at the next node boundary; the blocked execution
        # must be released so it can settle into the paused state.
        release.set()
        service.wait(chain_id)
        paused = service.get_session(initial["workflow_session_id"])
        assert paused["status"] == "paused"
        assert paused["available_actions"] == ["resume", "close"]
        closed = service.control(initial["workflow_session_id"], action="close",
                                 expected_revision=paused["revision"],
                                 idempotency_key=str(uuid4()))
        assert closed["active_chain_run_id"] is None
        assert closed["chains"][0]["status"] == "closed"
        assert closed["available_actions"] == []
        assert closed["can_submit"] is True
        # The fixed edit still requires derivation confirmation because close
        # does not archive the session.
        edited = deepcopy(current)
        edited["nodes"][0]["config"]["text"] = "after close"
        result = _preview(service, edited, expected_edit_sequence=saved["edit_sequence"])
        assert result["baseline"]["has_effective_session"] is True
        assert result["changes"]["would_outcome"] == "confirmation_required"
    finally:
        release.set()


# -- Q06-1: prepare failures are explained and leave no chain/node_run -------

def test_prepare_compile_failure_keeps_phase_and_creates_no_chain(service):
    value = _graph(service.registry)
    value["edges"] = []
    saved = _save(service, value)
    session = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    with pytest.raises(ContractValidationError) as error:
        service.start(session["workflow_session_id"],
                      expected_revision=session["revision"], idempotency_key=str(uuid4()))
    diagnostic = error.value.diagnostics[0]
    assert error.value.reason_code == "graph_no_outputs"
    assert diagnostic["phase"] == "compile"
    assert "path" in diagnostic
    view = service.get_session(session["workflow_session_id"])
    assert view["chains"] == []
    assert view["active_chain_run_id"] is None
    with closing(SqliteStore(service.database)) as store:
        connection = store._connection
        assert connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type IN ('chain_run', 'node_run')"
        ).fetchone()[0] == 0


def test_prepare_phase_failure_is_marked_and_retryable(service):
    value = _graph(service.registry)
    saved = _save(service, value)
    session = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    previous = list(service._package_diagnostics)
    service._package_diagnostics = [{"package_id": "sample.missing", "version": "1"}]
    try:
        with pytest.raises(ContractValidationError) as error:
            service.start(session["workflow_session_id"],
                          expected_revision=session["revision"], idempotency_key=str(uuid4()))
    finally:
        service._package_diagnostics = previous
    diagnostic = error.value.diagnostics[0]
    assert error.value.reason_code == "package_missing_dependency"
    assert error.value.status_code == 409
    assert diagnostic["phase"] == "prepare"
    assert service.get_session(session["workflow_session_id"])["chains"] == []
    # Restoring packages allows the same session to start.
    started = service.start(session["workflow_session_id"],
                            expected_revision=session["revision"], idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    assert service.get_run(
        started["workflow_session_id"],
        started["active_chain_run_id"])["chain"]["status"] == "succeeded"


# -- Q06-1/O01: node-execution and settlement failures stay distinguishable --

def test_node_execution_failure_persists_run_phase_and_location(service):
    def fail(config, inputs, context):
        from phase1_agent.contract_errors import DiagnosticError
        raise DiagnosticError("graph_node_boom", "boom", node_id=context.node_binding_id)

    service.registry.register(NodeDefinition(
        "test.fail", "1", "Fail", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), fail)
    text = node(service.registry, "tools.text", 1)
    failure = node(service.registry, "test.fail", 2)
    output = node(service.registry, "tools.output", 4)
    value = _schema3(document([text, failure, output],
                              [edge(text, failure, 1), edge(failure, output, 2)]))
    saved = _save(service, value)
    session = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    started = service.start(session["workflow_session_id"],
                            expected_revision=session["revision"],
                            idempotency_key=str(uuid4()))
    chain_id = started["active_chain_run_id"]
    service.wait(chain_id)
    view = service.get_session(session["workflow_session_id"])
    assert view["status"] == "failed"
    failed_node = next(item for item in view["nodes"]
                       if item["node_binding_id"] == failure["node_binding_id"])
    assert failed_node["diagnostic"]["phase"] == "run"
    assert failed_node["diagnostic"]["code"] == "graph_node_boom"
    assert failed_node["diagnostic"]["node_id"] == failure["node_binding_id"]
    stored = service.get_run(session["workflow_session_id"], chain_id)
    assert stored["chain"]["status"] == "failed"
    failing = next(item for item in stored["node_runs"]
                   if item["node_binding_id"] == failure["node_binding_id"])
    assert failing["diagnostic"]["phase"] == "run"
    # Node identity alone already locates the failure in the fixed document.
    assert failed_node["node_binding_id"] == failure["node_binding_id"]


def test_settlement_failure_keeps_phase_and_retryable_diagnostic(tmp_path):
    injected = []

    def fail_once(point):
        if point == "after_checkpoint_write" and not injected:
            injected.append(point)
            raise OSError("injected success checkpoint write failure")

    registry = create_package_registry().registry.detached()
    instance = GraphWorkflowService(tmp_path / "settlement.sqlite", registry=registry,
                                    fault_injector=fail_once)
    try:
        value = _graph(registry)
        saved = _save(instance, value)
        session = instance.create_session(
            saved["workflow_definition_id"], saved["definition_revision"],
            idempotency_key=str(uuid4()))
        started = instance.start(session["workflow_session_id"],
                                 expected_revision=session["revision"],
                                 idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        instance.wait(chain_id)
        view = instance.get_session(session["workflow_session_id"])
        assert view["status"] == "archive_failed"
        stored = instance.get_run(session["workflow_session_id"], chain_id)
        diagnostic = stored["chain"]["diagnostic"]
        assert diagnostic["reason_code"] == "checkpoint_commit_pending"
        assert diagnostic["phase"] == "settlement"
        assert diagnostic["failure_stage"] == "round_acceptance"
    finally:
        instance.close()


# -- Q06-3: version-2 contract extension -------------------------------------

def test_contract_v2_documents_preview_run_close_and_g21(service):
    application = _application(service)
    contract = application.query("document.contract")
    assert contract["contract_version"] == DOCUMENT_CONTRACT_VERSION == 2
    assert contract["discovery"]["preview_query"] == "definition.preview"
    preview = contract["preview"]
    assert preview["query"] == "definition.preview"
    assert preview["read_only"] is True
    for field in ("structure", "declared_references", "compilation", "baseline",
                  "changes", "run_conditions"):
        assert field in preview["returns"]
    run = contract["run"]
    assert run["start_command"] == "run.start" and run["control_command"] == "run.control"
    assert set(run["failure_phases"]) == {"prepare", "run", "settlement", "storage"}
    close = run["close"]
    assert close["requires_pause_first"] == ["running", "prepared"]
    assert any("does not archive the session" in limit for limit in close["limits"])
    assert any("G21" in limit for limit in close["limits"])
    g21 = run["g21"]
    assert g21["code"] == "configuration_run_locked"
    assert g21["occupying_sessions_row"] == {
        "workflow_session_id": "uuid", "active_chain_run_id": "uuid"}
    assert "preview" in g21["not_bypassed_by"]
    errors = contract["errors"]
    assert errors["package_missing_dependency"]["phases"] == ["prepare"]
    assert "run" in errors["graph_node_failed"]["phases"]
    assert set(errors["result_acceptance_pending"]["phases"]) == {"settlement"}
    # Schema and examples still validate under the version-2 contract.
    schema = contract["document"]["json_schema"]
    Draft202012Validator.check_schema(schema)
    for example in contract["document"]["examples"]:
        Draft202012Validator(schema).validate(example)
        validate_graph_document(example)
    # The preview query is discoverable as a management read query.
    described = application.describe()
    assert described["boundary"]["preview_query"] == "definition.preview"
    query = next(item for item in described["queries"] if item["name"] == "definition.preview")
    assert query["kind"] == "query" and query["idempotency"] == "none"
    # Rendering the contract is deterministic.
    assert document_contract() == document_contract()
