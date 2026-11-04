"""Independent verifier acceptance for step 5 backend (V5-01..V5-04).

Owned by the verifier; independent from the implementation team's tests.  Uses the
real application boundary, the HTTP-dispatch equivalent and isolated temporary
databases.  No product service, business database or real model call is used.

Run (backend directory):

    & '..\\.venv\\Scripts\\python.exe' -m pytest -q tests/test_s5_step5_acceptance.py \
        --basetemp <new isolated directory>
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError, DiagnosticError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_contracts import COMMANDS, QUERIES, validate_parameters
from phase1_agent.graph_application_identity import application_command_digest
from phase1_agent.graph_contracts import (
    DOCUMENT_CONTRACT_VERSION, NodeDefinition, NodePort, document_contract,
)
from phase1_agent.graph_http import dispatch_graph
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.workflow_modification_gate import modification_gate


DOC_PATH = (Path(__file__).resolve().parents[2] / "docs" / "Agent接入工作流-2026-11-04"
            / "外部Agent接入使用说明-2026-11-04.md")


# ---------------------------------------------------------------------------
# isolated fixture
# ---------------------------------------------------------------------------

def _registry():
    registry = create_package_registry().registry.detached()

    def verifier_node(config, inputs, context):
        if config["mode"] == "reject":
            raise DiagnosticError(
                "verifier_text_rejected", "Verifier node rejected the text",
                node_id=context.node_binding_id, phase="run", path="/config/mode",
                expected={"mode": "pass"}, actual={"mode": config["mode"]})
        return {"output": inputs["input"]}

    registry.register(NodeDefinition(
        "verifier.reject", "1", "Verifier reject node", "Test", {"mode": "pass"},
        {"type": "object", "properties": {"mode": {"enum": ["pass", "reject"]}},
         "required": ["mode"], "additionalProperties": False},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),)), verifier_node)

    def verifier_gate(config, inputs, context):
        _GATE["entered"].set()
        assert _GATE["release"].wait(10), "verifier gate timed out"
        return {"output": inputs["input"]}

    registry.register(NodeDefinition(
        "verifier.gate", "1", "Verifier gate node", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),)), verifier_gate)
    return registry


def _node(component, identity, config):
    return {"node_binding_id": identity, "component_id": component, "component_version": "1",
            "title": component, "position": {"x": 0, "y": 0}, "config": dict(config),
            "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"}}


def _edge(source, source_port, target, target_port, order=0):
    return {"edge_id": str(uuid4()), "source_node_id": source, "source_port_id": source_port,
            "target_node_id": target, "target_port_id": target_port, "order": order}


def _document(*, mode="pass", name="verifier external agent"):
    text = _node("tools.text", str(uuid4()), {"text": "hello from external agent"})
    middle = _node("verifier.reject", str(uuid4()), {"mode": mode})
    output = _node("tools.output", str(uuid4()), {"mode": "text"})
    return {"schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": name, "nodes": [text, middle, output],
            "edges": [_edge(text["node_binding_id"], "output", middle["node_binding_id"], "input"),
                      _edge(middle["node_binding_id"], "output", output["node_binding_id"], "input")],
            "object_bindings": [], "package_lock": []}


_GATE = {"entered": Event(), "release": Event()}


def _gated_document():
    document = _document(mode="pass")
    gate = _node("verifier.gate", str(uuid4()), {})
    output = document["nodes"][2]
    document["nodes"] = [document["nodes"][0], gate, output]
    document["edges"] = [
        _edge(document["nodes"][0]["node_binding_id"], "output", gate["node_binding_id"], "input"),
        _edge(gate["node_binding_id"], "output", output["node_binding_id"], "input")]
    return document


@pytest.fixture
def service(tmp_path):
    instance = GraphWorkflowService(tmp_path / "v5-acceptance.sqlite", registry=_registry())
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def app(service):
    return GraphApplication(service)


def _rejected(call, code, status=400):
    with pytest.raises(ContractValidationError) as error:
        call()
    assert error.value.reason_code == code, error.value.reason_code
    if status is not None:
        assert error.value.status_code == status
    return error.value


def _update(app, document, sequence, key=None, *, confirm=None):
    request = {"workflow_definition_id": document["workflow_definition_id"],
               "document": deepcopy(document), "expected_edit_sequence": sequence,
               "idempotency_key": key or str(uuid4())}
    if confirm is not None:
        request["confirm_derivation"] = confirm
    return app.command("definition.update", request)["result"], request


def _read(app, identity):
    return app.query("definition.read", {"identity": identity})


def _create_session(app, identity, revision, key=None):
    return app.command("session.create", {
        "workflow_definition_id": identity, "definition_revision": revision,
        "idempotency_key": key or str(uuid4())})["result"]


def _start(app, session, key=None):
    return app.command("run.start", {
        "session_id": session["workflow_session_id"], "expected_revision": session["revision"],
        "idempotency_key": key or str(uuid4())})["result"]


def _session(app, session):
    return app.query("session.read", {"session_id": session["workflow_session_id"]})


def _run(app, session, chain_id):
    return app.query("run.read", {"session_id": session["workflow_session_id"],
                                  "chain_id": chain_id})


def _control(app, session, action, key=None):
    return app.command("run.control", {
        "session_id": session["workflow_session_id"], "action": action,
        "expected_revision": session["revision"],
        "idempotency_key": key or str(uuid4())})["result"]


def _counts(service):
    with closing(sqlite3.connect(service.database)) as connection:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("records", "idempotency", "pending_commands",
                              "workflow_current_configuration")}


# ---------------------------------------------------------------------------
# V5-01: discovery, local trust boundary, explicit failures, read-only lock
# ---------------------------------------------------------------------------

def test_v5_01_discovery_surface_is_available_and_local_trusted(app, service):
    describe = app.describe()
    assert describe["kind"] == "workflow.application"
    assert describe["boundary"]["caller"] == "trusted_local"
    assert describe["boundary"]["remote_plugin_authentication"] is False
    assert describe["boundary"]["document_contract"] == "document.contract"
    assert describe["boundary"]["document_contract_version"] == DOCUMENT_CONTRACT_VERSION == 2
    assert describe["boundary"]["preview_query"] == "definition.preview"
    operation_names = {row["name"] for row in describe["commands"]}
    query_names = {row["name"] for row in describe["queries"]}
    # The core loop surface an external caller needs is discoverable.
    assert {"definition.update", "definition.copy", "session.create", "run.start",
            "run.control", "request.abandon"} <= operation_names
    assert {"document.contract", "definition.preview", "definition.read",
            "definition.summaries", "definition.inputs", "catalog.node-types",
            "session.read", "run.read", "receipt.read"} <= query_names
    contract = app.query("document.contract")
    assert contract["contract_version"] == DOCUMENT_CONTRACT_VERSION
    assert contract["document"]["limits"]["max_nodes"] == 512
    catalog = app.query("catalog.node-types", {"protocol_version": 2})
    assert catalog["schema_version"] == 2 and catalog["node_types"]
    document = _document()
    saved, _request = _update(app, document, 0, "discovery-initial")
    identity = document["workflow_definition_id"]
    baseline = _read(app, identity)
    assert baseline["edit_sequence"] == saved["edit_sequence"] == 1
    assert baseline["revision"] == saved["definition_revision"] == 1
    inputs = app.query("definition.inputs", {"document": baseline})
    assert isinstance(inputs, dict) and "inputs" in inputs
    session = _create_session(app, identity, 1, "discovery-session")
    actions = app.query("session.actions", {"session_id": session["workflow_session_id"]})
    assert actions["status"] == "idle" and actions["can_submit"] is True
    started = _start(app, session, "discovery-run")
    service.wait(started["active_chain_run_id"])
    view = _session(app, session)
    assert view["status"] == "succeeded"
    detail = _run(app, session, started["active_chain_run_id"])
    assert detail["chain"]["status"] == "succeeded" and detail["outputs"]
    # Local trust boundary: no credential/token parameter is part of the command
    # contract, and unexpected fields are rejected instead of silently ignored.
    _rejected(lambda: app.command("definition.update", {
        "workflow_definition_id": identity, "document": deepcopy(baseline),
        "expected_edit_sequence": 1, "idempotency_key": "auth-param",
        "authorization": "bearer remote"}), "invalid_request", 400)


def test_v5_01_unknown_or_out_of_scope_targets_fail_explicitly(app):
    _rejected(lambda: app.query("definition.read", {"identity": str(uuid4())}),
              "not_found", 404)
    _rejected(lambda: app.query("session.read", {"session_id": str(uuid4())}),
              "not_found", 404)
    _rejected(lambda: app.query("run.read", {
        "session_id": str(uuid4()), "chain_id": str(uuid4())}), "not_found", 404)
    _rejected(lambda: app.query("no.such.query", {}), "not_found", 404)
    _rejected(lambda: app.command("no.such.command", {"idempotency_key": "x"}),
              "not_found", 404)
    # A consumer-scoped caller cannot use management commands, but keeps its own.
    consumer = GraphApplication(app._service, scope="consumer")
    assert consumer.describe()["scope"] == "consumer"
    _rejected(lambda: consumer.command("definition.update", {
        "workflow_definition_id": str(uuid4()), "document": _document(),
        "expected_edit_sequence": 0, "idempotency_key": "consumer-write"}),
              "application_scope_denied", None)


def test_v5_01_reads_and_preview_do_not_take_the_modification_gate(app, service):
    document = _document()
    identity = document["workflow_definition_id"]
    saved, _request = _update(app, document, 0, "gate-initial")
    baseline = _read(app, identity)
    proposed = deepcopy(baseline)
    proposed["name"] = "renamed while the write gate is held"
    with modification_gate(service.database, identity):
        assert app.describe()["boundary"]["caller"] == "trusted_local"
        assert app.query("document.contract")["contract_version"] == 2
        assert app.query("catalog.node-types", {"protocol_version": 2})["node_types"]
        assert _read(app, identity)["edit_sequence"] == 1
        before = _counts(service)
        preview = app.query("definition.preview", {
            "workflow_definition_id": identity, "document": proposed,
            "expected_edit_sequence": baseline["edit_sequence"]})
        assert preview["read_only"] is True and preview["reserves_save_right"] is False
        assert preview["changes"]["would_outcome"] == "saved"
        assert _counts(service) == before  # preview/read write nothing
        busy = _rejected(lambda: _update(app, proposed, baseline["edit_sequence"], "gate-write"),
                         "workflow_modification_busy", 409)
        assert busy.diagnostics[0]["workflow_definition_id"] == identity
        assert _counts(service) == before
    after, _ = _update(app, proposed, baseline["edit_sequence"], "gate-write-after")
    assert after["outcome"] == "saved" and after["edit_sequence"] == 2


# ---------------------------------------------------------------------------
# V5-02: same-baseline edits, stale conflicts and explicit derivation
# ---------------------------------------------------------------------------

def test_v5_02_stale_baselines_are_rejected_without_overwrite(app, service):
    document = _document()
    identity = document["workflow_definition_id"]
    first, _request = _update(app, document, 0, "conflict-initial")
    renamed = deepcopy(first["document"])
    renamed["name"] = "renamed by the first edit"
    second, _ = _update(app, renamed, 1, "conflict-rename")
    assert second["outcome"] == "saved" and second["edit_sequence"] == 2
    before = _read(app, identity)
    stale = deepcopy(first["document"])
    stale["name"] = "stale overwrite from the old baseline"
    rejected = _rejected(lambda: _update(app, stale, 1, "conflict-stale"),
                         "stale_edit_sequence", 409)
    assert rejected.diagnostics[0]["expected"] == {"edit_sequence": 1}
    assert rejected.diagnostics[0]["actual"] == {"edit_sequence": 2}
    assert _read(app, identity) == before  # no automatic overwrite
    # A fixed revision that does not match the current stored revision is refused too.
    bad_revision = deepcopy(before)
    bad_revision["revision"] = before["revision"] + 1
    _rejected(lambda: _update(app, bad_revision, before["edit_sequence"], "conflict-revision"),
              "stale_revision", 409)
    assert _read(app, identity) == before


def test_v5_02_derivation_confirmation_is_explicit_and_never_automatic(app):
    document = _document()
    identity = document["workflow_definition_id"]
    saved, _request = _update(app, document, 0, "derive-initial")
    _create_session(app, identity, 1, "derive-session")
    changed = deepcopy(saved["document"])
    changed["nodes"][1]["config"]["mode"] = "reject"
    preview = app.query("definition.preview", {
        "workflow_definition_id": identity, "document": changed,
        "expected_edit_sequence": saved["edit_sequence"]})
    assert preview["changes"]["would_outcome"] == "confirmation_required"
    assert preview["changes"]["would_require_confirmation"] is True
    assert preview["changes"]["would_derive_after_confirmation"] is True
    assert preview["baseline"]["has_effective_session"] is True
    asked, _ = _update(app, changed, saved["edit_sequence"], "derive-ask")
    assert asked["outcome"] == "confirmation_required"
    assert (asked["definition_revision"], asked["edit_sequence"]) == (1, 1)
    assert _read(app, identity)["nodes"][1]["config"]["mode"] == "pass"
    derived, _ = _update(app, changed, saved["edit_sequence"], "derive-confirm", confirm=True)
    assert derived["outcome"] == "derived"
    assert derived["workflow_definition_id"] != identity
    assert derived["document"]["nodes"][1]["config"]["mode"] == "reject"
    assert _read(app, identity)["nodes"][1]["config"]["mode"] == "pass"


def test_v5_02_read_change_preview_save_reread_loop_is_repeatable(app):
    document = _document()
    identity = document["workflow_definition_id"]
    saved, _ = _update(app, document, 0, "loop-initial")
    sequence, revision = saved["edit_sequence"], saved["definition_revision"]
    for index in range(3):
        current = _read(app, identity)
        proposed = deepcopy(current)
        proposed["name"] = f"loop round {index}"
        preview = app.query("definition.preview", {
            "workflow_definition_id": identity, "document": proposed,
            "expected_edit_sequence": current["edit_sequence"]})
        assert preview["read_only"] is True
        assert preview["changes"]["would_outcome"] == "saved"
        result, _ = _update(app, proposed, current["edit_sequence"], f"loop-save-{index}")
        assert result["outcome"] == "saved"
        sequence, revision = result["edit_sequence"], result["definition_revision"]
        assert (sequence, revision) == (index + 2, 1)
        assert _read(app, identity)["name"] == f"loop round {index}"


# ---------------------------------------------------------------------------
# V5-03: unknown-result verification and durable termination
# ---------------------------------------------------------------------------

def test_v5_03_unknown_results_are_verified_read_only_and_not_replayed(app, service):
    document = _document()
    identity = document["workflow_definition_id"]
    first, first_request = _update(app, document, 0, "unknown-initial")
    before = _read(app, identity)
    counts = _counts(service)
    matched = app.query("receipt.read", {
        "operation": "definition.update", "parameters": deepcopy(first_request)})
    assert matched["outcome"] == "matched"
    assert matched["result"]["outcome"] == "saved"
    assert _read(app, identity) == before and _counts(service) == counts
    replay = app.command("definition.update", deepcopy(first_request))["result"]
    assert replay == first and _read(app, identity) == before
    assert _counts(service) == counts  # idempotent replay performs no new write
    unknown = {"workflow_definition_id": identity,
               "document": {**deepcopy(before), "name": "never executed"},
               "expected_edit_sequence": before["edit_sequence"],
               "idempotency_key": "unknown-request"}
    unresolved = app.query("receipt.read", {
        "operation": "definition.update", "parameters": unknown})
    assert unresolved["outcome"] == "unresolved"
    assert unresolved["reason_code"] == "application_identity_missing"
    assert _read(app, identity) == before and _counts(service) == counts


def test_v5_03_abandon_terminates_unsubmitted_and_returns_committed(app, service):
    document = _document()
    identity = document["workflow_definition_id"]
    saved, _ = _update(app, document, 0, "abandon-initial")
    before = _read(app, identity)
    unknown = {"workflow_definition_id": identity,
               "document": {**deepcopy(before), "name": "pending unknown"},
               "expected_edit_sequence": before["edit_sequence"],
               "idempotency_key": "abandon-unknown"}
    evidence = app.command("request-evidence.write", {
        "operation": "definition.update", "idempotency_key": unknown["idempotency_key"],
        "owner_kind": "workflow-configuration", "owner_id": identity,
        "request": deepcopy(unknown)})["result"]
    terminated = app.command("request.abandon", {
        "workflow_definition_id": identity, "target_operation": "definition.update",
        "target_idempotency_key": unknown["idempotency_key"],
        "target_request_digest": application_command_digest("definition.update", unknown),
        "idempotency_key": "abandon-command"})["result"]
    assert terminated["kind"] == "workflow.request-arbitration"
    assert terminated["outcome"] == "abandoned" and terminated["result"] is None
    assert terminated["evidence_resolution"] == "archived"
    assert terminated["request_digest"] == evidence["request_digest"]
    late = _rejected(lambda: app.command("definition.update", deepcopy(unknown)),
                     "request_abandoned", 409)
    assert late.status_code == 409
    assert _read(app, identity) == before  # the terminated request never executes

    # A committed target returns the formal result and performs no second execution.
    committed_request = {"workflow_definition_id": identity,
                         "document": {**deepcopy(before), "name": "committed target"},
                         "expected_edit_sequence": before["edit_sequence"],
                         "idempotency_key": "abandon-committed"}
    committed = app.command("definition.update", committed_request)["result"]
    assert committed["outcome"] == "saved"
    current = _read(app, identity)
    revision_records = _counts(service)["records"]
    app.command("request-evidence.write", {
        "operation": "definition.update", "idempotency_key": committed_request["idempotency_key"],
        "owner_kind": "workflow-configuration", "owner_id": identity,
        "request": deepcopy(committed_request)})
    arbitrated = app.command("request.abandon", {
        "workflow_definition_id": identity, "target_operation": "definition.update",
        "target_idempotency_key": committed_request["idempotency_key"],
        "target_request_digest": application_command_digest("definition.update", committed_request),
        "idempotency_key": "abandon-committed-command"})["result"]
    assert arbitrated["outcome"] == "committed"
    assert arbitrated["result"]["outcome"] == "saved"
    assert arbitrated["evidence_resolution"] == "verified"
    assert _read(app, identity) == current
    assert _counts(service)["records"] == revision_records


def test_v5_03_unarbitrable_unknown_stays_unresolved(app, service):
    identity = str(uuid4())
    digest = "0" * 64
    before = _counts(service)
    _rejected(lambda: app.command("request.abandon", {
        "workflow_definition_id": identity, "target_operation": "definition.update",
        "target_idempotency_key": "missing-evidence",
        "target_request_digest": digest,
        "idempotency_key": "unavailable-abandon"}),
        "request_arbitration_unavailable", 409)
    assert _counts(service) == before  # no fake termination and no write
    _rejected(lambda: app.command("request.abandon", {
        "workflow_definition_id": identity, "target_operation": "session.create",
        "target_idempotency_key": "missing-evidence", "target_request_digest": digest,
        "idempotency_key": "unavailable-target"}), "invalid_request", 400)


# ---------------------------------------------------------------------------
# V5-04: run semantics, diagnostics, explicit control and G21
# ---------------------------------------------------------------------------

def test_v5_04_run_start_is_not_completion_and_failure_diagnostics_are_readable(app, service):
    document = _document(mode="reject")
    identity = document["workflow_definition_id"]
    _update(app, document, 0, "run-initial")
    session = _create_session(app, identity, 1, "run-session")
    started = _start(app, session, "run-start")
    assert started["active_chain_run_id"]
    assert started["status"] in ("prepared", "running")
    initial = _read(app, identity)
    view = _session(app, session)
    assert view["status"] in ("prepared", "running", "failed")
    service.wait(started["active_chain_run_id"])
    failed = _session(app, session)
    assert failed["status"] == "failed"
    assert failed["available_actions"] == ["close"]
    assert failed["active_chain_run_id"] is not None  # no automatic close
    detail = _run(app, session, failed["active_chain_run_id"])
    diagnostic = detail["chain"]["diagnostic"]
    node_identity = initial["nodes"][1]["node_binding_id"]
    assert diagnostic["code"] == "verifier_text_rejected"
    assert diagnostic["phase"] == "run" and diagnostic["node_id"] == node_identity
    assert diagnostic["path"] == "/config/mode"
    assert diagnostic["expected"] == {"mode": "pass"}
    assert diagnostic["actual"] == {"mode": "reject"}
    assert any(row["status"] == "failed" and row["diagnostic"]["node_id"] == node_identity
               for row in detail["node_runs"])
    # The failure keeps G21 occupancy, named explicitly for an external caller.
    locked = _rejected(lambda: _update(app, deepcopy(initial), initial["edit_sequence"],
                                       "run-blocked"), "configuration_run_locked", 409)
    assert locked.diagnostics[0]["phase"] == "save"
    assert locked.diagnostics[0]["occupying_sessions"] == [{
        "workflow_session_id": session["workflow_session_id"],
        "active_chain_run_id": failed["active_chain_run_id"]}]


def test_v5_04_close_is_explicit_and_only_from_the_current_actions(app, service):
    success_document = _document(mode="pass")
    identity = success_document["workflow_definition_id"]
    _update(app, success_document, 0, "close-initial")
    session = _create_session(app, identity, 1, "close-session")
    started = _start(app, session, "close-start")
    service.wait(started["active_chain_run_id"])
    completed = _session(app, session)
    assert completed["status"] == "succeeded"
    assert completed["active_chain_run_id"] is None
    # A successful run is not closed or archived automatically: the session and its
    # chain stay observable and further edits are not blocked by G21.
    assert completed["can_submit"] is True
    assert completed["chains"][-1]["status"] == "succeeded"
    assert completed["archived"] is False
    assert "close" not in (completed["available_actions"] or [])
    _rejected(lambda: _control(app, completed, "close", "close-finished-run"),
              "invalid_state", 409)
    renamed, _ = _update(app, {**deepcopy(success_document), "name": "after success"},
                         1, "edit-after-success")
    assert renamed["outcome"] == "saved"
    # The finished session stays open and visible after that edit.
    still_open = _session(app, session)
    assert still_open["workflow_session_id"] == session["workflow_session_id"]
    assert still_open["archived"] is False


def test_v5_04_failed_run_close_frees_g21_and_retry_is_never_automatic(app, service):
    document = _document(mode="reject")
    identity = document["workflow_definition_id"]
    _update(app, document, 0, "retry-initial")
    session = _create_session(app, identity, 1, "retry-session")
    started = _start(app, session, "retry-start")
    service.wait(started["active_chain_run_id"])
    failed = _session(app, session)
    chains_before = len(failed["chains"])
    assert failed["status"] == "failed"
    # No automatic retry: the chain count and revision stay as observed.
    assert len(_session(app, session)["chains"]) == chains_before
    assert _session(app, session)["active_chain_run_id"] == failed["active_chain_run_id"]
    # Retrying without the explicit available action is an explicit failure, never a replay.
    if "retry_failed_node" not in (failed["available_actions"] or []):
        _rejected(lambda: _control(app, failed, "retry_failed_node", "retry-without-action"),
                  "failed_retry_unsupported", 409)
    closed = _control(app, failed, "close", "retry-close")
    assert closed["active_chain_run_id"] is None
    freed = _update(app, {**deepcopy(document), "name": "after failed close"},
                    1, "retry-after-close")
    assert freed[0]["outcome"] == "saved"


# ---------------------------------------------------------------------------
# V5 review: documented examples, no overclaim, HTTP handler equivalence
# ---------------------------------------------------------------------------

def test_v5_documented_operations_match_the_real_contracts(app):
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "trusted_local" in text or "本地可信" in text
    assert "run.start" in text and "不等于" in text
    assert "close" in text and "显式" in text
    assert "request.abandon" in text
    assert "receipt.read" in text
    assert "definition.preview" in text
    assert "occupying_sessions" in text
    assert "不新建" in text or "不建设" in text  # no new platform/auth/scheduler claim
    # Every operation name mentioned with the `phase1_agent` code font style must exist.
    documented = set(re.findall(r"`([a-z]+\.[a-z][a-z.-]+)`", text))
    known = {spec.name for spec in (*COMMANDS, *QUERIES)}
    documented_operations = {name for name in documented if "." in name
                             and name.split(".")[0] in {
                                 "definition", "session", "run", "request", "request-evidence",
                                 "receipt", "catalog", "document", "event", "output", "content",
                                 "provider", "resource", "checkpoint", "platform", "packages",
                                 "frontend", "object", "scope", "storage"}}
    assert documented_operations, "document mentions no operation names"
    unknown = {name for name in documented_operations - known if name.count(".") == 1}
    assert not unknown, f"documented operations are not real: {sorted(unknown)}"
    # Documented error codes must be real diagnostics: either published in the
    # partial document contract (reachable save/compile/run chain) or a code literal
    # that exists in the backend source (declared boundary families).
    errors = document_contract()["errors"]
    source_codes = set()
    for source in (Path(__file__).resolve().parents[1] / "src" / "phase1_agent").rglob("*.py"):
        source_codes.update(re.findall(r'"([a-z][a-z_]+)"', source.read_text(encoding="utf-8")))
    documented_codes = set(re.findall(r"`(graph_[a-z_]+|session_[a-z_]+|configuration_[a-z_]+|"
                                      r"request_[a-z_]+|workflow_[a-z_]+|package_[a-z_]+|"
                                      r"stale_[a-z_]+|invalid_state)`", text))
    documented_codes -= {"request_digest", "session_id", "workflow_definition_id"}
    missing = {code for code in documented_codes
               if code not in errors and code not in source_codes}
    assert not missing, f"documented error codes are not real: {sorted(missing)}"
    # The document must not promise that starting equals completion or that failures
    # are auto-cleared.
    assert not re.search(r"run\.start[^\n]{0,20}(即完成|就是完成|直接完成)", text)
    # The document must state explicitly that close is never automatic.
    assert re.search(r"(不会|不|不得|禁止)\s*自动[^\n]{0,8}close|"
                     r"close[^\n]{0,10}不(会)?\s*自动", text)


def test_v5_04_close_while_running_is_refused_and_pause_then_close_works(app, service):
    _GATE["entered"].clear()
    _GATE["release"].clear()
    document = _gated_document()
    identity = document["workflow_definition_id"]
    _update(app, document, 0, "gated-initial")
    session = _create_session(app, identity, 1, "gated-session")
    started = _start(app, session, "gated-start")
    assert _GATE["entered"].wait(5), "gate node was not entered"
    running = _session(app, session)
    assert running["status"] in ("prepared", "running")
    assert running["active_chain_run_id"] == started["active_chain_run_id"]
    assert "close" not in (running["available_actions"] or [])
    _rejected(lambda: _control(app, running, "close", "close-while-running"),
              "invalid_state", 409)
    locked = _rejected(lambda: _update(app, {**deepcopy(document), "name": "blocked while running"},
                                       1, "save-while-running"),
                       "configuration_run_locked", 409)
    assert locked.diagnostics[0]["occupying_sessions"] == [{
        "workflow_session_id": session["workflow_session_id"],
        "active_chain_run_id": started["active_chain_run_id"]}]
    paused_control = _control(app, running, "pause", "pause-while-running")
    assert paused_control["status"] in ("running", "pausing", "paused")
    _GATE["release"].set()
    service.wait(started["active_chain_run_id"])
    paused = _session(app, session)
    assert paused["status"] == "paused"
    assert paused["available_actions"] == ["resume", "close"]
    closed = _control(app, paused, "close", "close-after-pause")
    assert closed["status"] == "closed" and closed["active_chain_run_id"] is None
    assert closed["available_actions"] == []
    # The session is retained (close is not archive) and the workflow can be edited.
    retained = _session(app, session)
    assert retained["archived"] is False and retained["workflow_session_id"] == (
        session["workflow_session_id"])
    saved, _ = _update(app, {**deepcopy(document), "name": "after gated close"},
                       1, "save-after-gated-close")
    assert saved["outcome"] == "saved"


def test_v5_http_handler_equivalent_transport(app, service):
    document = _document()
    identity = document["workflow_definition_id"]
    status, describe = dispatch_graph(app._service, "GET", "/api/graph/application")
    assert status == 200 and describe["boundary"]["caller"] == "trusted_local"
    status, contract = dispatch_graph(app._service, "POST", "/api/graph/queries",
                                      {"operation": "document.contract", "parameters": {}})
    assert status == 200 and contract["contract_version"] == DOCUMENT_CONTRACT_VERSION
    request = {"workflow_definition_id": identity, "document": document,
               "expected_edit_sequence": 0, "idempotency_key": "http-initial"}
    status, envelope = dispatch_graph(app._service, "POST", "/api/graph/commands",
                                      {"operation": "definition.update", "parameters": request})
    assert status in (200, 201) and envelope["result"]["outcome"] == "saved"
    status, baseline = dispatch_graph(app._service, "POST", "/api/graph/queries",
                                      {"operation": "definition.read",
                                       "parameters": {"identity": identity}})
    assert status == 200 and baseline["edit_sequence"] == 1
    status, preview = dispatch_graph(app._service, "POST", "/api/graph/queries",
                                     {"operation": "definition.preview", "parameters": {
                                         "workflow_definition_id": identity,
                                         "document": {**deepcopy(baseline), "name": "http preview"},
                                         "expected_edit_sequence": 1}})
    assert status == 200 and preview["read_only"] is True
    with pytest.raises(ContractValidationError) as unknown:
        dispatch_graph(app._service, "POST", "/api/graph/queries",
                       {"operation": "no.such.query", "parameters": {}})
    assert unknown.value.reason_code == "not_found"
    with pytest.raises(ContractValidationError) as route:
        dispatch_graph(app._service, "GET", "/api/graph/not-a-route")
    assert route.value.reason_code == "not_found" and route.value.status_code == 404



