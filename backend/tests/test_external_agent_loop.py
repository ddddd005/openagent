"""S5-01: external agent completes the whole workflow loop locally.

The transport is the same application/HTTP handler the running local server uses
(``GraphApplication`` in-process and ``dispatch_graph`` for the HTTP routes).
Every test runs against an isolated tmp database, starts no server and calls no
real model: the graph only contains built-in tool nodes plus one synthetic
in-process rejection node.
"""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import COMMANDS, QUERIES, validate_parameters
from phase1_agent.graph_application_identity import application_command_digest
from phase1_agent.graph_contracts import (
    NodeDefinition, NodePort, document_contract, validate_graph_document,
)
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_receipts import read_graph_application_receipt
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.workflow_modification_gate import modification_gate


DOC_PATH = (Path(__file__).resolve().parents[2] / "docs" / "Agent接入工作流-2026-11-04"
            / "外部Agent接入使用说明-2026-11-04.md")


# -- isolated fixture --------------------------------------------------------

def _registry():
    """Built-in packages plus one synthetic in-process rejecting node."""
    registry = create_package_registry().registry.detached()

    def reject(config, inputs, context):
        if config["mode"] == "reject":
            raise DiagnosticError(
                "probe_text_rejected", "Synthetic node rejected the text",
                node_id=context.node_binding_id, phase="run", path="/config/mode",
                expected={"mode": "pass"}, actual={"mode": config["mode"]})
        return {"output": inputs["input"]}

    registry.register(NodeDefinition(
        "probe.reject", "1", "Synthetic reject", "Test", {"mode": "pass"},
        {"type": "object", "properties": {"mode": {"enum": ["pass", "reject"]}},
         "required": ["mode"], "additionalProperties": False},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),)), reject)
    return registry


def _node(component, identity, config):
    return {"node_binding_id": identity, "component_id": component, "component_version": "1",
            "title": component, "position": {"x": 0, "y": 0}, "config": dict(config),
            "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"}}


def _edge(source, source_port, target, target_port, order=0):
    return {"edge_id": str(uuid4()), "source_node_id": source, "source_port_id": source_port,
            "target_node_id": target, "target_port_id": target_port, "order": order}


def _document(*, mode="reject", name="external agent loop"):
    text = _node("tools.text", str(uuid4()), {"text": "hello from agent"})
    reject = _node("probe.reject", str(uuid4()), {"mode": mode})
    output = _node("tools.output", str(uuid4()), {"mode": "text"})
    return {"schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": name, "nodes": [text, reject, output],
            "edges": [_edge(text["node_binding_id"], "output", reject["node_binding_id"], "input"),
                      _edge(reject["node_binding_id"], "output", output["node_binding_id"], "input")],
            "object_bindings": [], "package_lock": []}


@pytest.fixture
def service(tmp_path):
    instance = GraphWorkflowService(tmp_path / "agent-loop.sqlite", registry=_registry())
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def app(service):
    return GraphApplication(service)


def update(app, document, sequence, key=None, *, confirm=None):
    request = {"workflow_definition_id": document["workflow_definition_id"],
               "document": deepcopy(document), "expected_edit_sequence": sequence,
               "idempotency_key": key or str(uuid4())}
    if confirm is not None:
        request["confirm_derivation"] = confirm
    return app.command("definition.update", request)["result"], request


def read_definition(app, identity):
    return app.query("definition.read", {"identity": identity})


def create_session(app, identity, revision, key=None):
    return app.command("session.create", {
        "workflow_definition_id": identity, "definition_revision": revision,
        "idempotency_key": key or str(uuid4())})["result"]


def start_run(app, session, key=None):
    return app.command("run.start", {
        "session_id": session["workflow_session_id"],
        "expected_revision": session["revision"],
        "idempotency_key": key or str(uuid4())})["result"]


def session_read(app, session):
    return app.query("session.read", {"session_id": session["workflow_session_id"]})


def run_read(app, session, chain_id):
    return app.query("run.read", {"session_id": session["workflow_session_id"],
                                  "chain_id": chain_id})


def control(app, session, action, key=None):
    return app.command("run.control", {
        "session_id": session["workflow_session_id"], "action": action,
        "expected_revision": session["revision"],
        "idempotency_key": key or str(uuid4())})["result"]


def counts(service):
    """Row counts of the durable surfaces a read/preview must not change."""
    with closing(sqlite3.connect(service.database)) as connection:
        return {
            "records": connection.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            "idempotency": connection.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0],
            "pending_commands": connection.execute(
                "SELECT COUNT(*) FROM pending_commands").fetchone()[0],
            "current_configuration": connection.execute(
                "SELECT COUNT(*) FROM workflow_current_configuration").fetchone()[0],
        }


def failure(error):
    return {"reason_code": error.reason_code, "status": error.status_code,
            "diagnostics": error.diagnostics}


# -- 1. the whole loop -------------------------------------------------------

def test_external_agent_full_loop_discover_preview_save_run_diagnose_fix(app, service):
    # Step 1: discover the contract and the node catalog.
    description = app.describe()
    assert description["boundary"]["caller"] == "trusted_local"
    assert description["boundary"]["preview_query"] == "definition.preview"
    contract = app.query("document.contract")
    assert contract["kind"] == "workflow.document-contract"
    assert contract["save"]["operation"] == "definition.update"
    catalog = app.query("catalog.node-types", {"protocol_version": 2})
    components = {(item["component_id"], item["component_version"])
                  for item in catalog["node_types"]}
    assert {("tools.text", "1"), ("tools.output", "1"),
            ("probe.reject", "1")} <= components
    platform = app.query("platform")
    assert platform["package_lock"] == list(service.registry.package_lock)

    # Step 2: create the workflow with a configuration that will fail on run.
    document = _document()
    identity = document["workflow_definition_id"]
    saved, _request = update(app, document, 0, "agent-initial")
    assert (saved["outcome"], saved["definition_revision"], saved["edit_sequence"]) == ("saved", 1, 1)
    summaries = app.query("definition.summaries")
    assert next(item for item in summaries
                if item["workflow_definition_id"] == identity)["edit_sequence"] == 1
    baseline = read_definition(app, identity)
    assert baseline["revision"] == 1 and baseline["edit_sequence"] == 1

    # Step 3: preview the repair.  It is read-only and reports the exact outcome.
    repaired = deepcopy(baseline)
    repaired["nodes"][1]["config"]["mode"] = "pass"
    before = counts(service)
    preview = app.query("definition.preview", {
        "workflow_definition_id": identity, "document": repaired,
        "expected_edit_sequence": baseline["edit_sequence"]})
    assert preview["kind"] == "workflow.configuration-preview"
    assert preview["read_only"] is True and preview["reserves_save_right"] is False
    assert preview["structure"]["ok"] and preview["declared_references"]["ok"]
    assert preview["compilation"]["ok"] and preview["compilation"]["ordered_node_count"] == 3
    assert preview["changes"]["would_outcome"] == "saved"
    assert preview["baseline"]["edit_sequence_matches"] is True
    assert preview["baseline"]["run_locked"] is False
    assert preview["run_conditions"]["checked"] and preview["run_conditions"]["not_checked"]
    assert counts(service) == before

    # Step 4: run through a session pinned to the saved fixed revision.
    session = create_session(app, identity, 1, "agent-session-1")
    started = start_run(app, session, "agent-run-1")
    assert started["status"] == "prepared"
    assert started["can_submit"] is False
    service.wait(started["active_chain_run_id"])
    after_run = session_read(app, session)
    assert after_run["status"] == "failed"
    assert after_run["available_actions"] == ["close"]
    assert after_run["can_submit"] is False
    assert after_run["active_chain_run_id"] is not None  # failures keep G21 occupancy
    chain_id = after_run["active_chain_run_id"]

    # Step 5: read the persisted run diagnostics (phase/node/path/expected/actual).
    run = run_read(app, session, chain_id)
    diagnostic = run["chain"]["diagnostic"]
    node_identity = baseline["nodes"][1]["node_binding_id"]
    assert diagnostic["code"] == "probe_text_rejected"
    assert diagnostic["phase"] == "run"
    assert diagnostic["node_id"] == node_identity
    assert diagnostic["path"] == "/config/mode"
    assert diagnostic["expected"] == {"mode": "pass"}
    assert diagnostic["actual"] == {"mode": "reject"}
    failed_run = next(item for item in run["node_runs"] if item["status"] == "failed")
    assert failed_run["diagnostic"]["node_id"] == node_identity

    # Step 6: with an unfinished run, saving is refused by G21 and names its owner.
    with pytest.raises(ContractValidationError) as locked:
        update(app, repaired, baseline["edit_sequence"], "agent-locked")
    assert locked.value.reason_code == "configuration_run_locked"
    assert locked.value.status_code == 409
    locked_diagnostic = locked.value.diagnostics[0]
    assert locked_diagnostic["phase"] == "save"
    assert locked_diagnostic["occupying_sessions"] == [{
        "workflow_session_id": session["workflow_session_id"],
        "active_chain_run_id": chain_id}]
    assert read_definition(app, identity)["edit_sequence"] == 1

    # Step 7: close is an explicit control action, never automatic.
    actions = app.query("session.actions", {"session_id": session["workflow_session_id"]})
    assert actions["status"] == "failed" and actions["available_actions"] == ["close"]
    closed = control(app, after_run, "close", "agent-close")
    assert closed["active_chain_run_id"] is None and closed["status"] == "closed"
    assert read_definition(app, identity)["edit_sequence"] == 1

    # Step 8: archive the blocking session explicitly, then the fixed repair saves.
    archived = app.command("session.archive", {
        "session_id": session["workflow_session_id"], "expected_revision": closed["revision"],
        "idempotency_key": "agent-archive"})["result"]
    assert archived["archived"] is True and archived["workflow_session_id"] == (
        session["workflow_session_id"])
    fixed, _repair_request = update(app, repaired, baseline["edit_sequence"], "agent-repair")
    assert (fixed["outcome"], fixed["definition_revision"], fixed["edit_sequence"]) == ("saved", 2, 2)
    assert read_definition(app, identity)["nodes"][1]["config"]["mode"] == "pass"

    # Step 9: a fresh session on the repaired revision runs to success.
    session2 = create_session(app, identity, 2, "agent-session-2")
    started2 = start_run(app, session2, "agent-run-2")
    service.wait(started2["active_chain_run_id"])
    final = session_read(app, session2)
    assert final["status"] == "succeeded"
    outputs = run_read(app, session2, started2["active_chain_run_id"])["outputs"]
    assert outputs and {row["payload"]["text"] for row in outputs} == {"hello from agent"}


# -- 2. read-only analysis and preview do not hold the edit lock -------------

def test_reads_and_preview_do_not_take_the_modification_gate(app, service):
    document = _document(mode="pass")
    identity = document["workflow_definition_id"]
    saved, _request = update(app, document, 0, "gate-initial")
    baseline = read_definition(app, identity)
    proposed = deepcopy(baseline)
    proposed["name"] = "renamed during gate"
    with modification_gate(service.database, identity):
        # Reads, contract discovery and preview stay available while a write holds
        # the entitlement.
        assert app.describe()["boundary"]["caller"] == "trusted_local"
        assert app.query("document.contract")["contract_version"] == 2
        assert app.query("catalog.node-types", {"protocol_version": 2})["node_types"]
        assert read_definition(app, identity)["edit_sequence"] == 1
        preview = app.query("definition.preview", {
            "workflow_definition_id": identity, "document": proposed,
            "expected_edit_sequence": baseline["edit_sequence"]})
        assert preview["read_only"] is True
        before = counts(service)
        with pytest.raises(ContractValidationError) as busy:
            update(app, proposed, baseline["edit_sequence"], "gate-write")
        assert busy.value.reason_code == "workflow_modification_busy"
        assert busy.value.status_code == 409
        assert busy.value.diagnostics[0]["workflow_definition_id"] == identity
        assert counts(service) == before
    after = update(app, proposed, baseline["edit_sequence"], "gate-write-after")
    assert after[0]["outcome"] == "saved" and after[0]["edit_sequence"] == 2


# -- 3. stale baseline, unknown result verification and termination ----------

def test_stale_baseline_is_rejected_and_unknown_results_are_verified(app, service):
    document = _document(mode="pass")
    identity = document["workflow_definition_id"]
    first, first_request = update(app, document, 0, "verify-initial")
    renamed = deepcopy(first["document"])
    renamed["name"] = "renamed by agent"
    second, second_request = update(app, renamed, 1, "verify-rename")
    assert second["outcome"] == "saved" and second["edit_sequence"] == 2

    # An edit built on the old baseline is rejected; the durable result is kept.
    stale = deepcopy(first["document"])
    stale["name"] = "stale overwrite"
    before = read_definition(app, identity)
    with pytest.raises(ContractValidationError) as rejected:
        update(app, stale, 1, "verify-stale")
    assert rejected.value.reason_code == "stale_edit_sequence"
    assert rejected.value.status_code == 409
    assert rejected.value.diagnostics[0]["expected"] == {"edit_sequence": 1}
    assert rejected.value.diagnostics[0]["actual"] == {"edit_sequence": 2}
    assert read_definition(app, identity) == before

    # A completed write is verified read-only, and the same request replays.
    matched = app.query("receipt.read", {
        "operation": "definition.update", "parameters": second_request})
    assert matched["outcome"] == "matched"
    assert matched["result"]["outcome"] == "saved"
    assert read_definition(app, identity) == before
    replayed = app.command("definition.update", second_request)["result"]
    assert replayed == second
    assert read_definition(app, identity) == before

    # An unknown request is verified read-only before any decision: no write.
    previous_sequence = before["edit_sequence"]
    unknown_document = deepcopy(before)
    unknown_document["name"] = "unknown request"
    unknown_request = {"workflow_definition_id": identity, "document": unknown_document,
                       "expected_edit_sequence": previous_sequence,
                       "idempotency_key": "verify-unknown"}
    unknown = app.query("receipt.read", {
        "operation": "definition.update", "parameters": unknown_request})
    assert unknown["outcome"] == "unresolved"
    assert unknown["reason_code"] == "application_identity_missing"
    assert read_definition(app, identity) == before

    # Arbitration requires the original request evidence; then the terminated
    # request can never execute through a late replay.
    evidence = app.command("request-evidence.write", {
        "operation": "definition.update", "idempotency_key": unknown_request["idempotency_key"],
        "owner_kind": "workflow-configuration", "owner_id": identity,
        "request": unknown_request})["result"]
    terminated = app.command("request.abandon", {
        "workflow_definition_id": identity, "target_operation": "definition.update",
        "target_idempotency_key": unknown_request["idempotency_key"],
        "target_request_digest": application_command_digest(
            "definition.update", unknown_request),
        "idempotency_key": "verify-abandon"})["result"]
    assert terminated["kind"] == "workflow.request-arbitration"
    assert terminated["outcome"] == "abandoned"
    assert terminated["evidence_resolution"] == "archived"
    assert evidence["request_digest"] == terminated["request_digest"]
    with pytest.raises(ContractValidationError) as terminated_replay:
        app.command("definition.update", unknown_request)
    assert terminated_replay.value.reason_code == "request_abandoned"
    assert terminated_replay.value.status_code == 409
    assert read_definition(app, identity) == before
    # The original completed request stays readable after later edits.
    assert app.query("receipt.read", {
        "operation": "definition.update", "parameters": first_request})["result"]["outcome"] == "saved"


# -- 4. fixed changes with an effective session need explicit derivation -----

def test_fixed_change_with_effective_session_needs_explicit_confirmation(app):
    document = _document(mode="pass")
    identity = document["workflow_definition_id"]
    saved, _request = update(app, document, 0, "derive-initial")
    create_session(app, identity, 1, "derive-session")
    changed = deepcopy(saved["document"])
    changed["nodes"][1]["config"]["mode"] = "reject"
    preview = app.query("definition.preview", {
        "workflow_definition_id": identity, "document": changed,
        "expected_edit_sequence": saved["edit_sequence"]})
    assert preview["changes"]["would_outcome"] == "confirmation_required"
    assert preview["changes"]["would_require_confirmation"] is True
    assert preview["changes"]["would_derive_after_confirmation"] is True
    assert preview["baseline"]["has_effective_session"] is True

    asked, _ask_request = update(app, changed, saved["edit_sequence"], "derive-ask")
    assert asked["outcome"] == "confirmation_required"
    assert asked["changed_categories"] == ["fixed"]
    assert asked["source_has_effective_session"] is True
    assert (asked["definition_revision"], asked["edit_sequence"]) == (1, 1)
    # Not confirmed means zero writes and the same workflow identity.
    assert read_definition(app, identity)["nodes"][1]["config"]["mode"] == "pass"

    derived, _derive_request = update(app, changed, saved["edit_sequence"], "derive-confirm",
                                      confirm=True)
    assert derived["outcome"] == "derived"
    assert derived["workflow_definition_id"] != identity
    assert derived["definition_revision"] == 1
    assert derived["document"]["nodes"][1]["config"]["mode"] == "reject"
    assert read_definition(app, identity)["nodes"][1]["config"]["mode"] == "pass"


# -- 5. HTTP handler equivalence and documented examples --------------------

def test_http_handler_transport_returns_the_same_results(service, app):
    document = _document(mode="pass")
    identity = document["workflow_definition_id"]
    request = {"workflow_definition_id": identity, "document": document,
               "expected_edit_sequence": 0, "idempotency_key": "http-initial"}
    status, result = dispatch_graph(service, "POST", "/api/graph/commands",
                                    {"operation": "definition.update", "parameters": request})
    assert status == 200 and result["result"]["outcome"] == "saved"
    status, described = dispatch_graph(service, "GET", "/api/graph/application")
    assert status == 200 and described["boundary"]["caller"] == "trusted_local"
    status, read = dispatch_graph(service, "GET", f"/api/graph/definitions/{identity}")
    assert status == 200 and read["edit_sequence"] == 1
    status, preview = dispatch_graph(service, "POST", "/api/graph/queries", {
        "operation": "definition.preview",
        "parameters": {"workflow_definition_id": identity, "document": read,
                       "expected_edit_sequence": 1}})
    assert status == 200 and preview["read_only"] is True
    with pytest.raises(ContractValidationError) as stale:
        dispatch_graph(service, "POST", "/api/graph/commands", {
            "operation": "definition.update",
            "parameters": {**request, "idempotency_key": "http-stale"}})
    assert stale.value.reason_code == "stale_edit_sequence"


_BLOCK = re.compile(r"```json (command|query|document|error|response)\n(.*?)```", re.S)


def test_documentation_examples_match_the_current_contract():
    text = DOC_PATH.read_text(encoding="utf-8")
    commands = {spec.name: spec for spec in COMMANDS}
    queries = {spec.name: spec for spec in QUERIES}
    kinds = {"command": 0, "query": 0, "document": 0, "error": 0, "response": 0}
    receipt_examples = 0
    command_examples = {}
    for kind, body in _BLOCK.findall(text):
        value = json.loads(body)
        kinds[kind] += 1
        if kind in ("command", "query"):
            assert set(value) == {"operation", "parameters"}
            spec = (commands if kind == "command" else queries).get(value["operation"])
            assert spec is not None, value["operation"]
            validate_parameters(spec, value["parameters"])
            if kind == "command":
                command_examples[value["operation"]] = deepcopy(value["parameters"])
            if kind == "query" and value["operation"] == "receipt.read":
                # The nested request is the complete original request and must pass
                # the same stored-evidence validation the receipt reader applies.
                # This assertion rejects the old example that omitted "document".
                receipt_examples += 1
                inner_operation = value["parameters"]["operation"]
                inner_parameters = value["parameters"]["parameters"]
                inner_spec = commands.get(inner_operation)
                assert inner_spec is not None, inner_operation
                validated = validate_parameters(inner_spec, inner_parameters,
                                                stored_evidence=True)
                assert validated == inner_parameters
                # The verified request must be exactly the documented submission.
                assert inner_operation in command_examples
                assert inner_parameters == command_examples[inner_operation]
        elif kind == "document":
            assert validate_graph_document(value) == value
        else:
            assert isinstance(value, dict)
            if "reason_code" in value:
                assert isinstance(value["reason_code"], str)
                assert isinstance(value.get("status", 400), int)
    assert receipt_examples >= 1
    assert kinds["command"] >= 2 and kinds["query"] >= 2
    assert kinds["document"] >= 1 and kinds["error"] >= 1
    for topic in ("接入方式", "信任", "幂等", "CAS", "G21", "preview", "receipt.read",
                  "request.abandon", "run.start", "close", "available_actions", "限制"):
        assert topic in text, topic
    # The receipt verification semantics must stay precise: unresolved never proves
    # non-execution, the same-key replay continues execution instead of terminating,
    # and only request.abandon may terminate.
    for phrase in ("不能证明", "未执行", "已取消", "继续执行", "首次执行", "终止",
                   "不能替代", "逐字段一致", "request_arbitration_unavailable",
                   "invalid_request", "abandoned", "request_abandoned"):
        assert phrase in text, phrase
    for operation in ("definition.update", "definition.preview", "definition.read",
                      "catalog.node-types", "session.create", "run.start", "run.control",
                      "receipt.read", "request.abandon", "session.read", "run.read",
                      "document.contract"):
        assert operation in text, operation


def test_receipt_read_documentation_shape_verifies_a_real_save(app, service):
    """The documented receipt.read shape is the complete original request."""
    document = _document(mode="pass")
    identity = document["workflow_definition_id"]
    original_request = {
        "workflow_definition_id": identity, "document": document,
        "expected_edit_sequence": 0, "idempotency_key": "docs-receipt-1"}
    saved = app.command("definition.update", original_request)["result"]
    assert (saved["outcome"], saved["definition_revision"], saved["edit_sequence"]) == (
        "saved", 1, 1)

    # The documented shape: outer receipt.read + nested complete original request.
    documented = {"operation": "definition.update",
                  "parameters": deepcopy(original_request)}
    validate_parameters(next(spec for spec in COMMANDS if spec.name == "definition.update"),
                        documented["parameters"], stored_evidence=True)
    matched = app.query("receipt.read", documented)
    assert matched["outcome"] == "matched"
    assert matched["reason_code"] == "receipt_matched"
    assert matched["result"]["outcome"] == "saved"
    assert matched["result"]["definition_revision"] == saved["definition_revision"]
    direct = read_graph_application_receipt(
        service.database, "definition.update", documented["parameters"])
    assert direct["outcome"] == "matched"
    assert direct["result"] == matched["result"]
    # Verification is read-only: no extra write happened.
    assert read_definition(app, identity)["edit_sequence"] == 1

    # The previous broken example dropped "document"; it cannot verify the save.
    incomplete = {key: value for key, value in original_request.items()
                  if key != "document"}
    broken = app.query("receipt.read", {
        "operation": "definition.update", "parameters": incomplete})
    assert broken["outcome"] == "unresolved"
    assert broken["reason_code"] == "invalid_request"
    with pytest.raises(ContractValidationError) as invalid:
        validate_parameters(next(spec for spec in COMMANDS
                                 if spec.name == "definition.update"),
                            incomplete, stored_evidence=True)
    assert invalid.value.reason_code == "invalid_request"
    assert read_definition(app, identity)["edit_sequence"] == 1
