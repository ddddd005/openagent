"""S4-00 new cost baseline after steps 1-3 (application layer, own file).

What this is
------------
The step-4 baseline harness for
``docs/工作流优化执行规划-2026-11-04/04-内部协议与读取精简.md``.  It measures the
*application layer* (``GraphApplication`` command/query processing on isolated
temporary SQLite databases) after steps 1-3 changed the backend.  Every figure is
newly measured in this run; no number from the preparation snapshot is reused.

It never starts a product service, never reads or writes the product database and
never calls a model.  Synthetic registries are registered in process; runs use
only in-process tool-free node executors.

Usage (from ``backend/``)::

    # full baseline, results stored under tests/measurement_results/
    ..\\.venv\\Scripts\\python.exe tests\\measurement_harness.py \\
        --workdir <new empty temp dir> --scenario all \\
        --json tests\\measurement_results\\s4-00-baseline-2026-11-04.json

    # one or more scenarios
    ..\\.venv\\Scripts\\python.exe tests\\measurement_harness.py \\
        --workdir <new empty temp dir> --scenario config,history

    # timing repeats (default 3; history-build/q08 cases always run once)
    ..\\.venv\\Scripts\\python.exe tests\\measurement_harness.py \\
        --workdir <new empty temp dir> --scenario chain --repeat 5

Scenarios
---------
``chain``       normal configuration save: the five serial application calls
                (evidence write, confirm read, definition.update, confirm read,
                resolve) at 1/20/100 nodes, per-call and total request/response
                bytes and backend seconds, plus the R1 read-only replay probe.
``chain-narrow`` S4-01 save flow: owner-scoped pending summary, then write,
                point confirm, business, point confirm, resolve at 1/20/100
                nodes; compare against ``chain`` on the same sizes.
``config``      save kinds at one node: initial / display-only / fixed /
                unchanged / stale-basis rejected / stale-sequence rejected /
                modification-busy rejected, with history scans, validated bytes,
                ``validate_graph_document`` counts and declared-reference checks.
``history``     history-size grid (1/10/44/100 fixed revisions): display-only
                save (first scan only) vs fixed save (first scan + second scan),
                reporting scans/rows/bytes/seconds; Q08 keeps the first scan.
``evidence``    owner evidence history (0/50/200/600 rows, pending 0/1): full
                owner read vs narrow SQL point/pending reads, and the pending
                summary page (Q07 baseline: layered init not implemented yet).
``q08``         corrupt stored history behaviour (two silent-skip classes, two
                explicit-error classes) so S4-02 can show the behaviour is
                unchanged by the MAX optimization.
``session``     effective-session outcomes: idle session view, fixed edit ->
                confirmation_required, confirmed -> derived, G21 run lock
                rejection with occupying sessions.
``fault``       unknown-result read-only receipt verification, failed evidence
                closure attempt, rejected evidence write, exact replay while a
                modification is in flight (R1).
``poll``        idle / running / completed session polls (350 ms interval:
                session.summaries + session.read), frozen-definition read,
                run.read and the frozen-definition duplicate-read multiplier.
``management``  management commands (definition.archive/delete, session.create/
                archive/delete, run.start): six evidence calls plus one business
                call, counted and timed separately.
``preview``     read-only ``definition.preview`` and ``document.contract`` cost
                at 1/20/100 nodes, with a zero-write verification; explicitly
                outside the save five-step chain.

Metrics are request/response JSON bytes of the application call (the same JSON
bodies an HTTP hop would carry, excluding HTTP headers/framing), ``perf_counter``
seconds inside this process, and structural counters.  They do not include
network RTT, server scheduling or real frontend wait/lock windows.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND / "src"))

from phase1_agent.contract_json import canonical_bytes  # noqa: E402
from phase1_agent.graph_application import GraphApplication  # noqa: E402
from phase1_agent.graph_contracts import (  # noqa: E402
    NodeDefinition, NodePort, NodeRegistry,
)
from phase1_agent.graph_service import GraphWorkflowService  # noqa: E402
from phase1_agent.request_evidence import RequestEvidenceService  # noqa: E402


OPERATION = "definition.update"
OWNER_KIND = "workflow-configuration"
MANAGEMENT_OWNER = "workflow-management"


# --------------------------------------------------------------------------- #
# generic measurement helpers
# --------------------------------------------------------------------------- #
def json_bytes(value) -> int:
    return len(canonical_bytes(value))


def describe_failure(exc: BaseException | None) -> dict | None:
    if exc is None:
        return None
    return {"type": type(exc).__name__,
            "reason_code": getattr(exc, "reason_code", None),
            "status": getattr(exc, "status_code", None),
            "message": str(exc)}


def timed(call):
    started = time.perf_counter()
    try:
        value = call()
        failed = None
    except BaseException as exc:  # rejected paths are measurements too
        value, failed = None, exc
    elapsed = time.perf_counter() - started
    return {"seconds": elapsed, "value": value, "failed": failed}


def seconds_summary(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"min": round(ordered[0], 6),
            "median": round(ordered[len(ordered) // 2], 6),
            "max": round(ordered[-1], 6)}


def result_outcome(value) -> dict:
    """Compact identity of a configuration result / application command envelope."""
    if not isinstance(value, dict):
        return {}
    body = value.get("result", value)
    if not isinstance(body, dict):
        return {}
    return {key: body[key] for key in ("outcome", "definition_revision", "edit_sequence",
                                       "workflow_definition_id", "changed_categories")
            if key in body}


# --------------------------------------------------------------------------- #
# structural counters (patched product functions, no product file changed)
# --------------------------------------------------------------------------- #
class HistoryScanCounter:
    """Count ``ConfigurationRepository.definition_revisions`` calls and their rows.

    Both full-history scans of one save go through this method:
    ``workflow_configuration.py`` (boolean ``existing``, first scan) and
    ``configuration_data.save_fixed_revision`` (max revision, second scan).
    """

    def __init__(self):
        self.calls = 0
        self.rows = 0
        self.seconds = 0.0
        self.validated_bytes = 0
        self.error = None

    def __enter__(self):
        from phase1_agent import configuration_data

        self._module = configuration_data
        self._original = configuration_data.ConfigurationRepository.definition_revisions

        def counted(repository, identity):
            started = time.perf_counter()
            try:
                value = self._original(repository, identity)
            except BaseException as exc:
                self.error = describe_failure(exc)
                raise
            self.calls += 1
            self.rows += len(value)
            self.seconds += time.perf_counter() - started
            for row in value:
                self.validated_bytes += json_bytes(row)
            return value

        configuration_data.ConfigurationRepository.definition_revisions = counted
        return self

    def __exit__(self, *exc_info):
        self._module.ConfigurationRepository.definition_revisions = self._original
        return False

    def snapshot(self) -> dict:
        return {"definition_revisions_calls": self.calls,
                "historical_rows_validated": self.rows,
                "history_validation_seconds": round(self.seconds, 6),
                "history_validated_bytes": self.validated_bytes,
                "history_scan_error": self.error}


class _PatchCounter:
    """Patch one module attribute and forward every positional/keyword call."""

    def __init__(self, module, attribute):
        self._module = module
        self._attribute = attribute
        self.calls = 0
        self.seconds = 0.0
        self.bytes = 0

    def __enter__(self):
        original = getattr(self._module, self._attribute)
        counter = self

        def counted(*args, **kwargs):
            counter.calls += 1
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                counter.seconds += time.perf_counter() - started
                if args:
                    try:
                        counter.bytes += json_bytes(args[0])
                    except (TypeError, ValueError):
                        pass

        setattr(self._module, self._attribute, counted)
        self._original = original
        return self

    def __exit__(self, *exc_info):
        setattr(self._module, self._attribute, self._original)
        return False


class DocumentValidationCounter(_PatchCounter):
    """Count full ``validate_graph_document`` runs (the phase kwarg is forwarded)."""

    def __init__(self):
        from phase1_agent import graph_contracts

        super().__init__(graph_contracts, "validate_graph_document")

    def snapshot(self) -> dict:
        return {"validate_graph_document_calls": self.calls,
                "validate_graph_document_bytes": self.bytes,
                "validate_graph_document_seconds": round(self.seconds, 6)}


class DeclarationReferenceCounter(_PatchCounter):
    """Count the Q05-2 save-time declared-reference walk."""

    def __init__(self):
        from phase1_agent import graph_contracts

        super().__init__(graph_contracts, "validate_document_references")

    def snapshot(self) -> dict:
        return {"validate_document_references_calls": self.calls,
                "validate_document_references_seconds": round(self.seconds, 6)}


class ReplayProbeCounter(_PatchCounter):
    """Count the R1 read-only replay probe before a modification entitlement."""

    def __init__(self):
        from phase1_agent import graph_store

        super().__init__(graph_store, "replayed_result")

    def snapshot(self) -> dict:
        return {"replay_probe_calls": self.calls,
                "replay_probe_seconds": round(self.seconds, 6)}


class CallLedger:
    """Record every application command/query one measured flow performs."""

    def __init__(self, app: GraphApplication):
        self._app = app
        self.rows: list[dict] = []

    def command(self, name: str, parameters: dict):
        started = time.perf_counter()
        try:
            value = self._app.command(name, deepcopy(parameters))
            failed = None
        except BaseException as exc:
            value, failed = None, exc
        self.rows.append({"call": f"command:{name}", "kind": "command",
                          "seconds": time.perf_counter() - started,
                          "request_bytes": json_bytes(parameters),
                          "response_bytes": None if value is None else json_bytes(value),
                          "outcome": None if failed is not None else result_outcome(value),
                          "error": describe_failure(failed)})
        if failed is not None:
            raise failed
        return value

    def query(self, name: str, parameters: dict | None = None):
        parameters = {} if parameters is None else parameters
        started = time.perf_counter()
        try:
            value = self._app.query(name, deepcopy(parameters))
            failed = None
        except BaseException as exc:
            value, failed = None, exc
        self.rows.append({"call": f"query:{name}", "kind": "query",
                          "seconds": time.perf_counter() - started,
                          "request_bytes": json_bytes(parameters),
                          "response_bytes": None if value is None else json_bytes(value),
                          "outcome": None, "error": describe_failure(failed)})
        if failed is not None:
            raise failed
        return value

    def totals(self) -> dict:
        return {"application_calls": len(self.rows),
                "command_calls": sum(1 for row in self.rows if row["kind"] == "command"),
                "query_calls": sum(1 for row in self.rows if row["kind"] == "query"),
                "request_bytes": sum(row["request_bytes"] for row in self.rows),
                "response_bytes": sum(row["response_bytes"] or 0 for row in self.rows),
                "seconds": round(sum(row["seconds"] for row in self.rows), 6),
                "failed_calls": sum(1 for row in self.rows if row["error"] is not None)}

    def rows_rounded(self) -> list[dict]:
        return [{**row, "seconds": round(row["seconds"], 6)} for row in self.rows]


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def measurement_registry(*, gate: threading.Event | None = None,
                         include_runnable: bool = False) -> NodeRegistry:
    """Synthetic in-process registry; no packages, no tools, no model calls."""
    registry = NodeRegistry()
    registry.register(
        NodeDefinition("measure.node", "1", "Measurement node", "Test", {},
                       {"type": "object", "properties": {}, "additionalProperties": True}),
        lambda config, inputs, context: {},
    )
    if include_runnable:
        def text_envelope(value: str) -> dict:
            return {"schema_version": 1, "kind": "workflow.text", "text": value}

        registry.register(
            NodeDefinition("measure.text", "1", "Measurement text", "Test", {"text": "apple apple"},
                           {"type": "object", "properties": {"text": {"type": "string"}}},
                           outputs=(NodePort("output", "TEXT"),)),
            lambda config, inputs, context: {"output": text_envelope(config.get("text", ""))},
        )
        registry.register(
            NodeDefinition("measure.output", "1", "Measurement output", "Test", {},
                           {"type": "object", "properties": {}}, is_output=True,
                           inputs=(NodePort("input", "TEXT"),)),
            lambda config, inputs, context: {},
        )
        registry.register(
            NodeDefinition("measure.relay", "1", "Measurement relay", "Test", {},
                           {"type": "object", "properties": {}},
                           inputs=(NodePort("input", "TEXT"),),
                           outputs=(NodePort("output", "TEXT"),)),
            lambda config, inputs, context: {"output": inputs["input"]},
        )
        if gate is not None:
            def gated(config, inputs, context):
                gate.wait(20)
                return {"output": inputs["input"]}

            registry.register(
                NodeDefinition("measure.gate", "1", "Measurement gate", "Test", {},
                               {"type": "object", "properties": {}},
                               inputs=(NodePort("input", "TEXT"),),
                               outputs=(NodePort("output", "TEXT"),)),
                gated,
            )
    return registry


def _node(component: str, title: str, position: int, config: dict | None = None) -> dict:
    return {"node_binding_id": str(uuid4()), "component_id": component, "component_version": "1",
            "title": title, "position": {"x": position * 40, "y": 0},
            "config": deepcopy(config) if config is not None else {},
            "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"}}


def simple_document(node_count: int = 1, *, name: str = "measure") -> dict:
    """Schema-3 document with ``node_count`` config-free nodes and chain edges."""
    nodes = [_node("measure.node", f"node-{index}", index) for index in range(node_count)]
    edges = [{"edge_id": str(uuid4()), "source_node_id": nodes[index]["node_binding_id"],
              "source_port_id": "output", "target_node_id": nodes[index + 1]["node_binding_id"],
              "target_port_id": "input", "order": 0} for index in range(node_count - 1)]
    return {"schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": name, "nodes": nodes, "edges": edges, "object_bindings": [],
            "package_lock": []}


def runnable_document(*, gated: bool = False, runs: int = 0) -> dict:
    """text -> relay(s) -> [gate] -> output; ``runs`` widens the round's node set."""
    nodes = [_node("measure.text", "text", 0, {"text": "apple apple"})]
    for index in range(runs):
        nodes.append(_node("measure.relay", f"relay-{index}", index + 1))
    if gated:
        nodes.append(_node("measure.gate", "gate", len(nodes)))
    nodes.append(_node("measure.output", "out", len(nodes)))
    edges = [{"edge_id": str(uuid4()), "source_node_id": source["node_binding_id"],
              "source_port_id": "output", "target_node_id": target["node_binding_id"],
              "target_port_id": "input", "order": order}
             for source, target, order in zip(nodes, nodes[1:], range(len(nodes) - 1))]
    return {"schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
            "name": "runnable", "nodes": nodes, "edges": edges, "object_bindings": [],
            "package_lock": []}


def operation_request(document: dict, edit_sequence: int, *, key: str | None = None,
                      confirm_derivation: bool | None = None) -> dict:
    request = {"workflow_definition_id": document["workflow_definition_id"],
               "document": deepcopy(document), "expected_edit_sequence": edit_sequence,
               "idempotency_key": key or str(uuid4())}
    if confirm_derivation is not None:
        request["confirm_derivation"] = confirm_derivation
    return request


def open_service(workdir: Path, name: str, *, gate: threading.Event | None = None,
                 include_runnable: bool = False):
    service = GraphWorkflowService(
        workdir / name,
        registry=measurement_registry(gate=gate, include_runnable=include_runnable))
    return service, GraphApplication(service)


def create_session(app: GraphApplication, workflow_id: str, revision: int) -> dict:
    return app.command("session.create", {"workflow_definition_id": workflow_id,
                                          "definition_revision": revision,
                                          "idempotency_key": str(uuid4())})["result"]


def start_run(app: GraphApplication, session: dict, *, key: str | None = None) -> dict:
    return app.command("run.start", {"session_id": session["workflow_session_id"],
                                     "expected_revision": session["revision"],
                                     "idempotency_key": key or str(uuid4())})["result"]


def wait_for_status(app: GraphApplication, session_id: str, statuses: set[str],
                    timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    view = None
    while time.monotonic() < deadline:
        view = app.query("session.read", {"session_id": session_id})
        if view["status"] in statuses:
            return view
        time.sleep(0.005)
    raise TimeoutError(f"session did not reach {statuses}: last "
                       f"{None if view is None else view['status']}")


def count_rows(database: Path) -> dict:
    with sqlite3.connect(database) as connection:
        return {"records": connection.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                "definition_revisions": connection.execute(
                    "SELECT COUNT(*) FROM records WHERE record_type='workflow_definition_revision'"
                ).fetchone()[0],
                "idempotency": connection.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0],
                "pending_commands": connection.execute(
                    "SELECT COUNT(*) FROM pending_commands").fetchone()[0]}


# --------------------------------------------------------------------------- #
# evidence helpers
# --------------------------------------------------------------------------- #
def save_verified_evidence(database: Path, operation: str, owner_kind: str, owner_id: str,
                           request: dict, resolution: str = "verified") -> dict:
    """Persist one real evidence row through the product service."""
    evidence = RequestEvidenceService(database)
    record = evidence.write_evidence(operation, request["idempotency_key"], owner_kind,
                                     owner_id, deepcopy(request))
    if resolution != "pending":
        record = evidence.resolve_evidence(operation, request["idempotency_key"],
                                           record["request_digest"], resolution)
    return record


def insert_historical_evidence(database: Path, operation: str, owner_kind: str, owner_id: str,
                               template: dict, count: int) -> None:
    """Insert synthetic historical rows whose digest the product helper builds."""
    from phase1_agent.graph_application_identity import application_command_digest

    if count <= 0:
        return
    with sqlite3.connect(database) as connection:
        for index in range(count):
            body = deepcopy(template)
            body["idempotency_key"] = f"seed-{index:05d}"
            digest = application_command_digest(operation, body)
            connection.execute(
                "INSERT OR IGNORE INTO pending_commands VALUES(?,?,?,?,?,?,?)",
                (operation, body["idempotency_key"], owner_kind, owner_id,
                 canonical_bytes(body).decode("utf-8"), digest, "verified"))
        connection.commit()


def evidence_read_report(app: GraphApplication, owner_kind: str, owner_id: str) -> dict:
    outcome = timed(lambda: app.query("request-evidence.read",
                                     {"owner_kind": owner_kind, "owner_id": owner_id}))
    records = outcome["value"] or []
    return {"seconds": round(outcome["seconds"], 6),
            "response_bytes": None if outcome["value"] is None else json_bytes(outcome["value"]),
            "rows": len(records),
            "rows_bytes": sum(json_bytes(record) for record in records),
            "error": describe_failure(outcome["failed"])}


def narrow_sql_report(database: Path, *, owner_kind: str, owner_id: str,
                      operation: str, key: str) -> dict:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        point = timed(lambda: connection.execute(
            "SELECT * FROM pending_commands WHERE operation=? AND idempotency_key=?",
            (operation, key)).fetchall())
        pending = timed(lambda: connection.execute(
            "SELECT * FROM pending_commands WHERE owner_kind=? AND owner_id=? AND resolution='pending'",
            (owner_kind, owner_id)).fetchall())
    return {"sql_point_seconds": round(point["seconds"], 6),
            "sql_point_rows": len(point["value"]),
            "sql_pending_seconds": round(pending["seconds"], 6),
            "sql_pending_rows": len(pending["value"])}


def narrow_read_report(app: GraphApplication, owner_kind: str, owner_id: str,
                       operation: str, key: str) -> dict:
    """The S4-01 narrow reads through the application contract."""
    page = timed(lambda: app.query("request-evidence.pending", {
        "owner_kind": owner_kind, "owner_id": owner_id, "limit": 2}))
    point = timed(lambda: app.query("request-evidence.read", {
        "owner_kind": owner_kind, "owner_id": owner_id,
        "operation": operation, "idempotency_key": key}))
    pending = timed(lambda: app.query("request-evidence.read", {
        "owner_kind": owner_kind, "owner_id": owner_id, "resolution": "pending"}))
    return {
        "owner_pending_page_seconds": round(page["seconds"], 6),
        "owner_pending_page_bytes": (None if page["value"] is None
                                     else json_bytes(page["value"])),
        "owner_pending_page_items": (None if page["value"] is None
                                     else len(page["value"]["items"])),
        "point_read_seconds": round(point["seconds"], 6),
        "point_read_bytes": None if point["value"] is None else json_bytes(point["value"]),
        "point_read_rows": None if point["value"] is None else len(point["value"]),
        "pending_narrow_seconds": round(pending["seconds"], 6),
        "pending_narrow_bytes": (None if pending["value"] is None
                                 else json_bytes(pending["value"])),
        "pending_narrow_rows": (None if pending["value"] is None else len(pending["value"])),
        "errors": [error for error in (describe_failure(page["failed"]),
                                       describe_failure(point["failed"]),
                                       describe_failure(pending["failed"])) if error],
    }


def save_chain_flow(ledger: CallLedger, document: dict, *, owner_kind: str = OWNER_KIND,
                    operation: str = OPERATION) -> tuple[dict, dict]:
    """Run the exact five serial calls of one normal configuration save."""
    workflow_id = document["workflow_definition_id"]
    request = operation_request(document, 0)
    written = ledger.command("request-evidence.write", {
        "operation": operation, "idempotency_key": request["idempotency_key"],
        "owner_kind": owner_kind, "owner_id": workflow_id, "request": request})
    ledger.query("request-evidence.read", {"owner_kind": owner_kind, "owner_id": workflow_id})
    result = ledger.command(operation, request)["result"]
    ledger.query("request-evidence.read", {"owner_kind": owner_kind, "owner_id": workflow_id})
    ledger.command("request-evidence.resolve", {
        "operation": operation, "idempotency_key": request["idempotency_key"],
        "request_digest": written["result"]["request_digest"], "resolution": "verified"})
    return result, request


# --------------------------------------------------------------------------- #
# scenario: five-step save chain at 1/20/100 nodes
# --------------------------------------------------------------------------- #
def scenario_chain(workdir: Path, repeat: int) -> dict:
    rows = []
    for node_count in (1, 20, 100):
        samples = []
        for _ in range(repeat):
            case_dir = workdir / f"chain-{node_count}-{uuid4().hex[:8]}"
            case_dir.mkdir(parents=True, exist_ok=True)
            service, app = open_service(case_dir, "chain.sqlite")
            try:
                ledger = CallLedger(app)
                document = simple_document(node_count)
                replay = ReplayProbeCounter()
                validations = DocumentValidationCounter()
                declarations = DeclarationReferenceCounter()
                history = HistoryScanCounter()
                with replay, validations, declarations, history:
                    result, _request = save_chain_flow(ledger, document)
                samples.append({
                    "totals": ledger.totals(),
                    "calls": ledger.rows_rounded(),
                    "outcome": result_outcome({"result": result}),
                    "replay": replay.snapshot(),
                    "history": history.snapshot(),
                    "validations": validations.snapshot(),
                    "declarations": declarations.snapshot(),
                    "document_bytes": json_bytes(document),
                })
            finally:
                service.close()
        first = samples[0]
        rows.append({
            "node_count": node_count,
            "repeats": repeat,
            "document_bytes": first["document_bytes"],
            "application_calls": first["totals"]["application_calls"],
            "request_bytes": first["totals"]["request_bytes"],
            "response_bytes": first["totals"]["response_bytes"],
            "application_seconds": seconds_summary([s["totals"]["seconds"] for s in samples]),
            "per_call_request_bytes": {row["call"]: row["request_bytes"]
                                       for row in first["calls"]},
            "per_call_response_bytes": {row["call"]: row["response_bytes"]
                                        for row in first["calls"]},
            "outcome": first["outcome"],
            "history_scan_calls": first["history"]["definition_revisions_calls"],
            "history_rows_validated": first["history"]["historical_rows_validated"],
            "replay_probe_calls": first["replay"]["replay_probe_calls"],
            "replay_probe_seconds_median": seconds_summary(
                [s["replay"]["replay_probe_seconds"] for s in samples])["median"],
            "validate_graph_document_calls": first["validations"]["validate_graph_document_calls"],
            "validate_graph_document_seconds_median": seconds_summary(
                [s["validations"]["validate_graph_document_seconds"] for s in samples])["median"],
            "declaration_reference_calls": first["declarations"]["validate_document_references_calls"],
            "sample_calls": first["calls"],
        })
    return {"scenario": "chain-five-step-save",
            "note": ("five serial application calls of one initialized normal save; one additional "
                     "request-evidence.read initializes the channel on its first use"),
            "rows": rows}


def narrow_save_chain_flow(ledger: CallLedger, document: dict, *,
                           owner_kind: str = OWNER_KIND,
                           operation: str = OPERATION) -> tuple[dict, dict]:
    """S4-01 save flow: layered init summary + point confirms + five-step chain."""
    workflow_id = document["workflow_definition_id"]
    ledger.query("request-evidence.pending", {"owner_kind": owner_kind,
                                              "owner_id": workflow_id, "limit": 2})
    request = operation_request(document, 0)
    written = ledger.command("request-evidence.write", {
        "operation": operation, "idempotency_key": request["idempotency_key"],
        "owner_kind": owner_kind, "owner_id": workflow_id, "request": request})
    ledger.query("request-evidence.read", {
        "owner_kind": owner_kind, "owner_id": workflow_id,
        "operation": operation, "idempotency_key": request["idempotency_key"]})
    result = ledger.command(operation, request)["result"]
    ledger.query("request-evidence.read", {
        "owner_kind": owner_kind, "owner_id": workflow_id,
        "operation": operation, "idempotency_key": request["idempotency_key"]})
    ledger.command("request-evidence.resolve", {
        "operation": operation, "idempotency_key": request["idempotency_key"],
        "request_digest": written["result"]["request_digest"], "resolution": "verified"})
    return result, request


def scenario_chain_narrow(workdir: Path, repeat: int) -> dict:
    rows = []
    for node_count in (1, 20, 100):
        samples = []
        for _ in range(repeat):
            case_dir = workdir / f"chain-narrow-{node_count}-{uuid4().hex[:8]}"
            case_dir.mkdir(parents=True, exist_ok=True)
            service, app = open_service(case_dir, "chain.sqlite")
            try:
                ledger = CallLedger(app)
                document = simple_document(node_count)
                replay = ReplayProbeCounter()
                validations = DocumentValidationCounter()
                declarations = DeclarationReferenceCounter()
                history = HistoryScanCounter()
                with replay, validations, declarations, history:
                    result, _request = narrow_save_chain_flow(ledger, document)
                samples.append({
                    "totals": ledger.totals(),
                    "calls": ledger.rows_rounded(),
                    "outcome": result_outcome({"result": result}),
                    "document_bytes": json_bytes(document),
                })
            finally:
                service.close()
        first = samples[0]
        rows.append({
            "node_count": node_count,
            "repeats": repeat,
            "document_bytes": first["document_bytes"],
            "application_calls": first["totals"]["application_calls"],
            "request_bytes": first["totals"]["request_bytes"],
            "response_bytes": first["totals"]["response_bytes"],
            "application_seconds": seconds_summary([s["totals"]["seconds"] for s in samples]),
            "per_call_request_bytes": {row["call"]: row["request_bytes"]
                                       for row in first["calls"]},
            "per_call_response_bytes": {row["call"]: row["response_bytes"]
                                        for row in first["calls"]},
            "outcome": first["outcome"],
            "sample_calls": first["calls"],
        })
    return {"scenario": "chain-narrow-save",
            "note": ("S4-01 flow: one owner-scoped pending summary (no pending on the normal "
                     "path), then write, point confirm, business, point confirm, resolve; "
                     "compare with `chain` which reads the owner's full history"),
            "rows": rows}


def historic_save_flow(ledger: CallLedger, document: dict, mode: str,
                       *, owner_kind: str = OWNER_KIND, operation: str = OPERATION) -> dict:
    """One save after the owner accumulated history, before vs after S4-01.

    ``baseline`` reads the owner's entire evidence history for initialization and
    for both confirm steps; ``narrow`` reads the owner pending summary once and
    point-reads the exact record for both confirm steps.
    """
    workflow_id = document["workflow_definition_id"]
    if mode == "baseline":
        ledger.query("request-evidence.read", {"owner_kind": owner_kind, "owner_id": workflow_id})
    else:
        ledger.query("request-evidence.pending", {"owner_kind": owner_kind,
                                                  "owner_id": workflow_id, "limit": 2})
    request = operation_request(document, document["revision"])
    written = ledger.command("request-evidence.write", {
        "operation": operation, "idempotency_key": request["idempotency_key"],
        "owner_kind": owner_kind, "owner_id": workflow_id, "request": request})
    if mode == "baseline":
        ledger.query("request-evidence.read", {"owner_kind": owner_kind, "owner_id": workflow_id})
    else:
        ledger.query("request-evidence.read", {
            "owner_kind": owner_kind, "owner_id": workflow_id,
            "operation": operation, "idempotency_key": request["idempotency_key"]})
    result = ledger.command(operation, request)["result"]
    if mode == "baseline":
        ledger.query("request-evidence.read", {"owner_kind": owner_kind, "owner_id": workflow_id})
    else:
        ledger.query("request-evidence.read", {
            "owner_kind": owner_kind, "owner_id": workflow_id,
            "operation": operation, "idempotency_key": request["idempotency_key"]})
    ledger.command("request-evidence.resolve", {
        "operation": operation, "idempotency_key": request["idempotency_key"],
        "request_digest": written["result"]["request_digest"], "resolution": "verified"})
    return result


def scenario_chain_history(workdir: Path, repeat: int) -> dict:
    """Before/after save flow over an owner that already holds 100 evidence rows."""
    history_rows = 100
    rows = []
    for node_count in (1, 20, 100):
        for mode in ("baseline", "narrow"):
            samples = []
            for _ in range(repeat):
                case_dir = workdir / f"chain-history-{node_count}-{mode}-{uuid4().hex[:8]}"
                case_dir.mkdir(parents=True, exist_ok=True)
                service, app = open_service(case_dir, "chain.sqlite")
                try:
                    saved = app.command(OPERATION, operation_request(simple_document(node_count), 0))["result"]
                    workflow_id = saved["workflow_definition_id"]
                    template = operation_request(saved["document"], saved["edit_sequence"])
                    insert_historical_evidence(case_dir / "chain.sqlite", OPERATION, OWNER_KIND,
                                               workflow_id, template, history_rows)
                    ledger = CallLedger(app)
                    result = historic_save_flow(ledger, saved["document"], mode)
                    samples.append({"totals": ledger.totals(),
                                    "calls": ledger.rows_rounded(),
                                    "outcome": result_outcome({"result": result})})
                finally:
                    service.close()
            first = samples[0]
            rows.append({
                "node_count": node_count,
                "history_rows": history_rows,
                "mode": mode,
                "repeats": repeat,
                "application_calls": first["totals"]["application_calls"],
                "request_bytes": first["totals"]["request_bytes"],
                "response_bytes": first["totals"]["response_bytes"],
                "application_seconds": seconds_summary([s["totals"]["seconds"] for s in samples]),
                "outcome": first["outcome"],
                "sample_calls": first["calls"],
            })
    return {"scenario": "chain-history-before-after",
            "note": ("the owner already holds 100 verified evidence rows of the same document "
                     "size; baseline = full owner read for init + both confirms, narrow = one "
                     "owner pending summary + two point reads"),
            "rows": rows}


# --------------------------------------------------------------------------- #
# scenario: save kinds
# --------------------------------------------------------------------------- #
def scenario_config(workdir: Path, repeat: int) -> dict:
    rows = []
    for label in ("initial", "display-only", "fixed", "unchanged", "invalid-basis",
                  "stale-edit-sequence", "modification-busy"):
        samples = []
        for _ in range(repeat):
            case_dir = workdir / f"config-{label}-{uuid4().hex[:8]}"
            case_dir.mkdir(parents=True, exist_ok=True)
            service, app = open_service(case_dir, "config.sqlite")
            try:
                document = simple_document()
                workflow_id = document["workflow_definition_id"]
                if label == "initial":
                    request = operation_request(document, 0)
                else:
                    saved = app.command(OPERATION, operation_request(document, 0))["result"]
                    proposed = deepcopy(saved["document"])
                    expected = saved["edit_sequence"]
                    if label == "display-only":
                        proposed["name"] = "renamed after measure"
                    elif label == "fixed":
                        proposed["nodes"][0]["config"] = {"value": 1}
                    elif label == "invalid-basis":
                        proposed["name"] = "invalid basis"
                        expected = saved["edit_sequence"] + 5
                    elif label == "stale-edit-sequence":
                        # Concurrent modification: the request basis is one edit behind
                        # and the submitted document carries no newer counter.
                        proposed.pop("edit_sequence", None)
                        proposed["name"] = "stale basis"
                        expected = saved["edit_sequence"] - 1
                    request = operation_request(proposed, expected)
                history = HistoryScanCounter()
                validations = DocumentValidationCounter()
                declarations = DeclarationReferenceCounter()
                replay = ReplayProbeCounter()
                if label == "modification-busy":
                    from phase1_agent.workflow_modification_gate import modification_gate
                    with replay, history, validations, declarations, \
                            modification_gate(service.database, workflow_id):
                        outcome = timed(lambda: app.command(OPERATION, request))
                else:
                    with replay, history, validations, declarations:
                        outcome = timed(lambda: app.command(OPERATION, request))
                response = outcome["value"]
                result = None if response is None else response.get("result")
                samples.append({
                    "seconds": outcome["seconds"],
                    "request_bytes": json_bytes(request),
                    "response_bytes": None if response is None else json_bytes(response),
                    "document_bytes": json_bytes(request["document"]),
                    "error": describe_failure(outcome["failed"]),
                    "outcome": result_outcome({"result": result}),
                    **history.snapshot(),
                    **validations.snapshot(),
                    **declarations.snapshot(),
                    **replay.snapshot(),
                })
            finally:
                service.close()
        first = samples[0]
        rows.append({
            "kind": label,
            "repeats": repeat,
            "outcome": first["outcome"],
            "error": first["error"],
            "request_bytes": first["request_bytes"],
            "response_bytes": first["response_bytes"],
            "document_bytes": first["document_bytes"],
            "seconds": seconds_summary([s["seconds"] for s in samples]),
            "definition_revisions_calls": first["definition_revisions_calls"],
            "historical_rows_validated": first["historical_rows_validated"],
            "history_validated_bytes": first["history_validated_bytes"],
            "history_validation_seconds": first["history_validation_seconds"],
            "validate_graph_document_calls": first["validate_graph_document_calls"],
            "validate_graph_document_seconds": first["validate_graph_document_seconds"],
            "declaration_reference_calls": first["validate_document_references_calls"],
            "declaration_reference_seconds": first["validate_document_references_seconds"],
            "replay_probe_calls": first["replay_probe_calls"],
            "replay_probe_seconds": first["replay_probe_seconds"],
        })
    return {"scenario": "save-kinds",
            "note": ("one-node document; modification-busy holds the step-1 process gate in this "
                     "process and measures the immediate 409; the R1 read-only replay probe runs once "
                     "per new modification and is reported separately"),
            "rows": rows}


# --------------------------------------------------------------------------- #
# scenario: history-size grid (Q08)
# --------------------------------------------------------------------------- #
def build_history(app: GraphApplication, target_revisions: int) -> dict:
    document = simple_document()
    saved = app.command(OPERATION, operation_request(document, 0))["result"]
    for revision in range(1, target_revisions):
        proposed = deepcopy(saved["document"])
        proposed["nodes"][0]["config"] = {"value": revision}
        saved = app.command(OPERATION, operation_request(proposed, saved["edit_sequence"]))["result"]
    return saved


def scenario_history(workdir: Path) -> dict:
    rows = []
    for target in (1, 10, 44, 100):
        case_dir = workdir / f"history-{target}-{uuid4().hex[:8]}"
        case_dir.mkdir(parents=True, exist_ok=True)
        service, app = open_service(case_dir, "history.sqlite")
        try:
            started = time.perf_counter()
            saved = build_history(app, target)
            build_seconds = time.perf_counter() - started
            history_rows = saved["definition_revision"]
            cases = {}
            for label in ("display-only", "fixed", "unchanged"):
                proposed = deepcopy(saved["document"])
                if label == "display-only":
                    proposed["name"] = f"rename at r{target}"
                elif label == "fixed":
                    proposed["nodes"][0]["config"] = {"value": target + 1000}
                request = operation_request(proposed, saved["edit_sequence"])
                scans = HistoryScanCounter()
                validations = DocumentValidationCounter()
                with scans, validations:
                    outcome = timed(lambda: app.command(OPERATION, request))
                response = outcome["value"]
                cases[label] = {
                    "seconds": round(outcome["seconds"], 6),
                    "request_bytes": json_bytes(request),
                    "response_bytes": None if response is None else json_bytes(response),
                    "outcome": result_outcome(response) if response else None,
                    "error": describe_failure(outcome["failed"]),
                    **scans.snapshot(),
                    **validations.snapshot(),
                }
                if outcome["failed"] is None:
                    saved = response["result"]
            rows.append({"fixed_revisions": history_rows,
                         "build_seconds": round(build_seconds, 6),
                         "build_seconds_per_revision": round(
                             build_seconds / max(1, history_rows - 1), 6),
                         "cases": cases})
        finally:
            service.close()
    return {"scenario": "history-size-grid",
            "note": ("each row builds a real fixed-revision history, then measures one display-only, "
                     "one fixed and one unchanged save at that history size; the fixed case shows the "
                     "first save-time scan plus the second MAX scan that Q08 may remove, while the "
                     "first scan stays and keeps growing with history"),
            "rows": rows}


# --------------------------------------------------------------------------- #
# scenario: evidence history reads (Q07 baseline)
# --------------------------------------------------------------------------- #
def scenario_evidence(workdir: Path) -> dict:
    rows = []
    for history in (0, 50, 200, 600):
        for pending_count in (0, 1):
            case_dir = workdir / f"evidence-{history}-{pending_count}-{uuid4().hex[:8]}"
            case_dir.mkdir(parents=True, exist_ok=True)
            service, app = open_service(case_dir, "evidence.sqlite")
            database = case_dir / "evidence.sqlite"
            try:
                saved = app.command(OPERATION, operation_request(simple_document(), 0))["result"]
                workflow_id = saved["workflow_definition_id"]
                template = operation_request(saved["document"], saved["edit_sequence"] + 1)
                insert_historical_evidence(database, OPERATION, OWNER_KIND, workflow_id,
                                           template, history)
                if pending_count:
                    pending_request = operation_request(saved["document"],
                                                        saved["edit_sequence"] + 1,
                                                        key=str(uuid4()))
                    save_verified_evidence(database, OPERATION, OWNER_KIND, workflow_id,
                                           pending_request, resolution="pending")
                else:
                    pending_request = template
                page = timed(lambda: app.query("request-evidence.pending", {"limit": 50}))
                full = evidence_read_report(app, OWNER_KIND, workflow_id)
                narrow = narrow_sql_report(database, owner_kind=OWNER_KIND, owner_id=workflow_id,
                                           operation=OPERATION,
                                           key=pending_request["idempotency_key"])
                narrow_reads = narrow_read_report(app, OWNER_KIND, workflow_id,
                                                  OPERATION,
                                                  pending_request["idempotency_key"])
                rows.append({
                    "history_rows": history,
                    "pending_rows": pending_count,
                    "stored_rows": history + pending_count,
                    "pending_page_seconds": round(page["seconds"], 6),
                    "pending_page_bytes": None if page["value"] is None else json_bytes(page["value"]),
                    "full_owner_read": full,
                    "narrow_sql": narrow,
                    "narrow_reads": narrow_reads,
                })
            finally:
                service.close()
    return {"scenario": "evidence-owner-history",
            "note": ("owner evidence rows are verified historical rows plus at most one pending row; "
                     "`full_owner_read` is the pre-S4-01 read and `narrow_reads` are the S4-01 "
                     "owner summary / point / pending reads on the same data"),
            "rows": rows}


# --------------------------------------------------------------------------- #
# scenario: corrupt stored history (Q08 behaviour baseline)
# --------------------------------------------------------------------------- #
_CORRUPTIONS = {
    "invalid_json": lambda wid: (
        '{"workflow_definition_id": "' + wid + '", "revision": 99, '),
    "valid_json_not_graph": lambda wid: json.dumps(
        {"workflow_definition_id": wid, "revision": 99, "payload_note": "not a graph record"}),
    "graph_record_bad_fields": lambda wid: json.dumps(
        {"schema_version": 2, "execution_model": "graph", "workflow_definition_id": wid,
         "revision": 99}),
    "graph_record_bad_document": lambda wid: json.dumps(
        {"schema_version": 2, "execution_model": "graph", "workflow_definition_id": wid,
         "revision": 99, "bindings": [], "edges": [],
         "document": {"schema_version": 3, "workflow_definition_id": wid, "revision": 99,
                      "name": "bad", "nodes": "not-a-list", "edges": [],
                      "object_bindings": []}}),
}


def _append_bad_record(database: Path, workflow_id: str, corruption: str) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO records(record_type,record_id,payload,immutable,created_at) "
            "VALUES('workflow_definition_revision',?,?,1,'2026-11-04T00:00:00.000Z')",
            (f"corrupt:{corruption}", _CORRUPTIONS[corruption](workflow_id)))
        connection.commit()
    finally:
        connection.close()


def scenario_q08(workdir: Path) -> dict:
    seed_dir = workdir / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    seed_service, seed_app = open_service(seed_dir, "seed.sqlite")
    try:
        saved = seed_app.command(OPERATION, operation_request(simple_document(), 0))["result"]
        proposed = deepcopy(saved["document"])
        proposed["nodes"][0]["config"] = {"value": 2}
        seed_app.command(OPERATION, operation_request(proposed, saved["edit_sequence"]))
        database, workflow_id = seed_dir / "seed.sqlite", saved["workflow_definition_id"]
    finally:
        seed_service.close()

    def attempt(case_db: Path, mutate, label: str) -> dict:
        service, app = open_service(case_db.parent, case_db.name)
        try:
            current = app.query("definition.read", {"identity": workflow_id})
            proposed = deepcopy(current)
            mutate(proposed)
            request = operation_request(proposed, current["edit_sequence"])
            scans = HistoryScanCounter()
            with scans:
                outcome = timed(lambda: app.command(OPERATION, request))
            after = app.query("definition.read", {"identity": workflow_id})
            return {"label": label,
                    "reached": outcome["failed"] is None,
                    "outcome": result_outcome(outcome["value"]) if outcome["value"] else None,
                    "seconds": round(outcome["seconds"], 6),
                    "error": describe_failure(outcome["failed"]),
                    "history_scan_calls_before_failure": scans.calls,
                    "history_rows_before_failure": scans.rows,
                    "history_scan_error": scans.error,
                    "edit_sequence_before": current["edit_sequence"],
                    "edit_sequence_after": after.get("edit_sequence"),
                    "wrote_anything": after.get("edit_sequence") != current["edit_sequence"]}
        finally:
            service.close()

    def display_edit(proposed):
        proposed["name"] = "rename with corrupt history"

    def fixed_edit(proposed):
        proposed["nodes"][0]["config"] = {"value": 3}

    rows = []
    for corruption in _CORRUPTIONS:
        row = {"corruption": corruption}
        for label, mutate in (("display-only", display_edit), ("fixed", fixed_edit)):
            case_dir = workdir / f"q08-{corruption}-{label}-{uuid4().hex[:8]}"
            case_dir.mkdir(parents=True, exist_ok=True)
            case_db = case_dir / "case.sqlite"
            import shutil
            shutil.copy(database, case_db)
            _append_bad_record(case_db, workflow_id, corruption)
            row[label] = attempt(case_db, mutate, label)
        rows.append(row)
    return {"scenario": "corrupt-history-behaviour",
            "note": ("corrupt rows are appended directly to isolated temp copies; product database "
                     "untouched; this is the behaviour baseline Q08 must not change silently"),
            "rows": rows}


# --------------------------------------------------------------------------- #
# scenario: effective session outcomes
# --------------------------------------------------------------------------- #
def scenario_session(workdir: Path) -> dict:
    rows = []
    case_dir = workdir / f"session-confirm-{uuid4().hex[:8]}"
    case_dir.mkdir(parents=True, exist_ok=True)
    service, app = open_service(case_dir, "session.sqlite")
    try:
        saved = app.command(OPERATION, operation_request(simple_document(), 0))["result"]
        create_parameters = {"workflow_definition_id": saved["workflow_definition_id"],
                             "definition_revision": saved["definition_revision"],
                             "idempotency_key": str(uuid4())}
        created = timed(lambda: app.command("session.create", create_parameters))
        rows.append({"case": "session.create business command (idle session)",
                     "seconds": round(created["seconds"], 6),
                     "request_bytes": json_bytes(create_parameters),
                     "response_bytes": (None if created["value"] is None
                                        else json_bytes(created["value"])),
                     "status": created["value"]["result"]["status"]})
        fixed = deepcopy(saved["document"])
        fixed["nodes"][0]["config"] = {"value": 7}
        ask_request = operation_request(fixed, saved["edit_sequence"])
        before = count_rows(case_dir / "session.sqlite")
        ask = timed(lambda: app.command(OPERATION, ask_request))
        after = count_rows(case_dir / "session.sqlite")
        rows.append({
            "case": "confirmation_required (effective idle session, fixed edit)",
            "seconds": round(ask["seconds"], 6),
            "request_bytes": json_bytes(ask_request),
            "response_bytes": None if ask["value"] is None else json_bytes(ask["value"]),
            "outcome": result_outcome(ask["value"]) if ask["value"] else None,
            "wrote_records_delta": after["records"] - before["records"],
            "wrote_definition_revisions_delta":
                after["definition_revisions"] - before["definition_revisions"],
            "error": describe_failure(ask["failed"]),
        })
        confirm_request = operation_request(fixed, saved["edit_sequence"], confirm_derivation=True)
        before = count_rows(case_dir / "session.sqlite")
        derived = timed(lambda: app.command(OPERATION, confirm_request))
        after = count_rows(case_dir / "session.sqlite")
        rows.append({
            "case": "derived (confirmed fixed edit on effective session)",
            "seconds": round(derived["seconds"], 6),
            "request_bytes": json_bytes(confirm_request),
            "response_bytes": None if derived["value"] is None else json_bytes(derived["value"]),
            "outcome": result_outcome(derived["value"]) if derived["value"] else None,
            "wrote_records_delta": after["records"] - before["records"],
            "wrote_definition_revisions_delta":
                after["definition_revisions"] - before["definition_revisions"],
            "error": describe_failure(derived["failed"]),
        })
    finally:
        service.close()

    gate = threading.Event()
    case_dir = workdir / f"session-g21-{uuid4().hex[:8]}"
    case_dir.mkdir(parents=True, exist_ok=True)
    service, app = open_service(case_dir, "g21.sqlite", gate=gate, include_runnable=True)
    try:
        document = runnable_document(gated=True)
        saved = app.command(OPERATION, operation_request(document, 0))["result"]
        session = create_session(app, saved["workflow_definition_id"],
                                 saved["definition_revision"])
        started = start_run(app, session)
        wait_for_status(app, session["workflow_session_id"], {"running", "prepared"})
        locked_request = operation_request(saved["document"], saved["edit_sequence"])
        locked_request["document"]["name"] = "edit during run"
        locked = timed(lambda: app.command(OPERATION, locked_request))
        diagnostics = getattr(locked["failed"], "diagnostics", None) or []
        rows.append({
            "case": "G21 configuration_run_locked during a running (gated) session",
            "seconds": round(locked["seconds"], 6),
            "request_bytes": json_bytes(locked_request),
            "response_bytes": None if locked["value"] is None else json_bytes(locked["value"]),
            "error": describe_failure(locked["failed"]),
            "occupying_sessions": diagnostics[0].get("occupying_sessions") if diagnostics else None,
        })
        gate.set()
        service.wait(started["active_chain_run_id"], timeout=20)
    finally:
        gate.set()
        service.close()
    return {"scenario": "effective-session-outcomes",
            "note": ("an idle effective session is enough for confirmation_required/derived; the G21 "
                     "case blocks inside a gated in-process node executor, never a model"),
            "rows": rows}


# --------------------------------------------------------------------------- #
# scenario: failure-side costs
# --------------------------------------------------------------------------- #
def scenario_fault(workdir: Path) -> dict:
    case_dir = workdir / f"fault-{uuid4().hex[:8]}"
    case_dir.mkdir(parents=True, exist_ok=True)
    service, app = open_service(case_dir, "fault.sqlite")
    database = case_dir / "fault.sqlite"
    rows = []
    try:
        document = simple_document()
        ledger = CallLedger(app)
        saved_result, original_request = save_chain_flow(ledger, document)
        rows.append({"case": "five-step save (reference flow for the fault cases)",
                     "totals": ledger.totals()})

        receipt = timed(lambda: app.query("receipt.read", {"operation": OPERATION,
                                                          "parameters": original_request}))
        rows.append({"case": "unknown-result receipt.read verification (read-only)",
                     "seconds": round(receipt["seconds"], 6),
                     "request_bytes": json_bytes({"operation": OPERATION,
                                                  "parameters": original_request}),
                     "response_bytes": None if receipt["value"] is None else json_bytes(receipt["value"]),
                     "outcome": None if receipt["value"] is None else receipt["value"].get("outcome"),
                     "error": describe_failure(receipt["failed"])})

        evidence = RequestEvidenceService(database)
        saved = app.query("definition.read", {"identity": document["workflow_definition_id"]})
        closure_request = operation_request(saved, saved["edit_sequence"])
        save_verified_evidence(database, OPERATION, OWNER_KIND,
                               document["workflow_definition_id"], closure_request,
                               resolution="pending")
        wrong = timed(lambda: app.command("request-evidence.resolve", {
            "operation": OPERATION, "idempotency_key": closure_request["idempotency_key"],
            "request_digest": "0" * 64, "resolution": "verified"}))
        rows.append({"case": "failed evidence closure attempt (digest mismatch)",
                     "seconds": round(wrong["seconds"], 6),
                     "request_bytes": json_bytes({
                         "operation": OPERATION,
                         "idempotency_key": closure_request["idempotency_key"],
                         "request_digest": "0" * 64, "resolution": "verified"}),
                     "error": describe_failure(wrong["failed"])})
        pending = next(record for record in evidence.read_evidence(
            OWNER_KIND, document["workflow_definition_id"]) if record["resolution"] == "pending")
        correct = timed(lambda: app.command("request-evidence.resolve", {
            "operation": OPERATION, "idempotency_key": pending["idempotency_key"],
            "request_digest": pending["request_digest"], "resolution": "verified"}))
        rows.append({"case": "retried evidence closure (correct digest)",
                     "seconds": round(correct["seconds"], 6),
                     "error": describe_failure(correct["failed"])})

        second = operation_request(saved, saved["edit_sequence"], key=str(uuid4()))
        evidence.write_evidence(OPERATION, second["idempotency_key"], OWNER_KIND,
                                document["workflow_definition_id"], deepcopy(second))
        third = operation_request(saved, saved["edit_sequence"], key=str(uuid4()))
        rejected = timed(lambda: app.command("request-evidence.write", {
            "operation": OPERATION, "idempotency_key": third["idempotency_key"],
            "owner_kind": OWNER_KIND, "owner_id": document["workflow_definition_id"],
            "request": third}))
        rows.append({"case": "rejected evidence write while one pending exists",
                     "seconds": round(rejected["seconds"], 6),
                     "error": describe_failure(rejected["failed"])})

        from phase1_agent.workflow_modification_gate import modification_gate
        with modification_gate(service.database, document["workflow_definition_id"]):
            exact = timed(lambda: app.command(OPERATION, deepcopy(original_request)))
        rows.append({"case": "exact replay while a modification is in flight (R1)",
                     "seconds": round(exact["seconds"], 6),
                     "outcome": result_outcome(exact["value"]) if exact["value"] else None,
                     "error": describe_failure(exact["failed"])})
    finally:
        service.close()
    return {"scenario": "failure-side-costs",
            "note": ("unknown-result verification uses the read-only receipt route and does not read "
                     "the evidence table; closure/write rejections are the measured backend costs"),
            "rows": rows}


# --------------------------------------------------------------------------- #
# scenario: polling and history panel reads (Q09 baseline)
# --------------------------------------------------------------------------- #
def scenario_poll(workdir: Path) -> dict:
    rows = []
    case_dir = workdir / f"poll-{uuid4().hex[:8]}"
    case_dir.mkdir(parents=True, exist_ok=True)
    service, app = open_service(case_dir, "poll.sqlite", include_runnable=True)
    try:
        saved = app.command(OPERATION, operation_request(runnable_document(runs=3), 0))["result"]
        workflow_id = saved["workflow_definition_id"]
        session = create_session(app, workflow_id, saved["definition_revision"])
        session_id = session["workflow_session_id"]

        def poll_tick(selected_session_id: str | None) -> dict:
            result = {}
            summaries = timed(lambda: app.query("session.summaries", {
                "workflow_definition_id": workflow_id, "include_archived": True}))
            result["summaries_seconds"] = round(summaries["seconds"], 6)
            result["summaries_bytes"] = (json_bytes(summaries["value"])
                                         if summaries["value"] is not None else None)
            if selected_session_id:
                view = timed(lambda: app.query("session.read",
                                               {"session_id": selected_session_id}))
                result["session_read_seconds"] = round(view["seconds"], 6)
                result["session_read_bytes"] = (json_bytes(view["value"])
                                                if view["value"] is not None else None)
                result["session_status"] = view["value"]["status"] if view["value"] else None
                result["per_tick_calls"] = 2
            else:
                result["per_tick_calls"] = 1
            result["per_tick_bytes"] = (result.get("summaries_bytes") or 0) \
                + (result.get("session_read_bytes") or 0)
            result["per_tick_seconds"] = round((result.get("summaries_seconds") or 0)
                                               + (result.get("session_read_seconds") or 0), 6)
            return result

        rows.append({"case": "idle session poll tick (350 ms interval, 2 queries)",
                     **poll_tick(session_id)})
        rows.append({"case": "no-selected-session poll tick (1 query)",
                     **poll_tick(None)})
        frozen = timed(lambda: app.query("definition.read", {
            "identity": workflow_id, "revision": saved["definition_revision"]}))
        current = timed(lambda: app.query("definition.read", {"identity": workflow_id}))
        summaries = timed(lambda: app.query("definition.summaries", {"include_archived": True}))
        rows.append({"case": "frozen definition read (history panel per chain revision today)",
                     "seconds": round(frozen["seconds"], 6),
                     "response_bytes": json_bytes(frozen["value"]),
                     "revision": frozen["value"]["revision"]})
        rows.append({"case": "current definition read (manual refreshPublishedRevision before S4-03)",
                     "seconds": round(current["seconds"], 6),
                     "response_bytes": json_bytes(current["value"])})
        rows.append({"case": "definition summaries read (refreshPublishedRevision after Q09)",
                     "seconds": round(summaries["seconds"], 6),
                     "response_bytes": json_bytes(summaries["value"]),
                     "rows": len(summaries["value"])})

        for _ in range(3):
            current_session = app.query("session.read", {"session_id": session_id})
            started = start_run(app, current_session)
            service.wait(started["active_chain_run_id"], timeout=20)
        completed = app.query("session.read", {"session_id": session_id})
        chain = max(completed["chains"], key=lambda row: row["revision"])
        run_read = timed(lambda: app.query("run.read", {
            "session_id": session_id, "chain_id": chain["chain_run_id"]}))
        frozen_again = timed(lambda: app.query("definition.read", {
            "identity": chain["workflow_definition_id"],
            "revision": chain["definition_revision"]}))
        rows.append({"case": "completed session poll tick (chained run views)",
                     **poll_tick(session_id),
                     "chain_revision": chain["revision"],
                     "chain_count": len(completed["chains"]),
                     "outputs": len(completed["outputs"])})
        rows.append({"case": "history panel: run.read for one selected chain",
                     "seconds": round(run_read["seconds"], 6),
                     "response_bytes": json_bytes(run_read["value"]),
                     "node_runs": len(run_read["value"]["node_runs"])})
        rows.append({"case": "history panel: frozen definition read after run.read",
                     "seconds": round(frozen_again["seconds"], 6),
                     "response_bytes": json_bytes(frozen_again["value"]),
                     "identity": f"{chain['workflow_definition_id']}@r{chain['definition_revision']}"})
        rows.append({"case": "Q09 duplicate-read multiplier for one measured round",
                     "chain_revision_advances": chain["revision"],
                     "frozen_definition_reads_today": chain["revision"],
                     "frozen_definition_reads_with_identity_reuse": 1,
                     "note_scope": ("one run round with 4 nodes; GraphHistory re-reads the same frozen "
                                    "revision on every chain revision change today")})
        rows.append({"case": "Q09 frozen definition identity reuse (after S4-03)",
                     "chain_revision_advances": chain["revision"],
                     "frozen_definition_reads_before_s4_03": chain["revision"],
                     "frozen_definition_reads_after_s4_03_with_identity_reuse": 1,
                     "note_scope": ("GraphHistory keeps (workflow_definition_id, definition_revision) and "
                                    "the store caches successful reads keyed by that identity; run.read "
                                    "stays the trigger and a cached hit performs no definition read")})
    finally:
        service.close()

    gate = threading.Event()
    run_dir = workdir / f"poll-running-{uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    service, app = open_service(run_dir, "running.sqlite", gate=gate, include_runnable=True)
    try:
        saved = app.command(OPERATION,
                            operation_request(runnable_document(gated=True, runs=3), 0))["result"]
        workflow_id = saved["workflow_definition_id"]
        session = create_session(app, workflow_id, saved["definition_revision"])
        session_id = session["workflow_session_id"]
        started = start_run(app, session)
        wait_for_status(app, session_id, {"running"})
        summaries = timed(lambda: app.query("session.summaries", {
            "workflow_definition_id": workflow_id, "include_archived": True}))
        view = timed(lambda: app.query("session.read", {"session_id": session_id}))
        rows.append({"case": "running session poll tick (gated in-process node, no model)",
                     "summaries_seconds": round(summaries["seconds"], 6),
                     "summaries_bytes": json_bytes(summaries["value"]),
                     "session_read_seconds": round(view["seconds"], 6),
                     "session_read_bytes": json_bytes(view["value"]),
                     "per_tick_calls": 2,
                     "per_tick_bytes": json_bytes(summaries["value"]) + json_bytes(view["value"]),
                     "per_tick_seconds": round(summaries["seconds"] + view["seconds"], 6),
                     "session_status": view["value"]["status"]})
        gate.set()
        service.wait(started["active_chain_run_id"], timeout=20)
        final = app.query("session.read", {"session_id": session_id})
        rows.append({"case": "running -> completed view size delta",
                     "status": final["status"],
                     "completed_response_bytes": json_bytes(final),
                     "chains": len(final["chains"]),
                     "chain_revision": max((row["revision"] for row in final["chains"]),
                                          default=None)})
    finally:
        gate.set()
        service.close()
    return {"scenario": "poll-and-history-reads",
            "note": ("application layer only; the frontend interval is 350 ms and the measured tick is "
                     "two serial application queries; RTT and service scheduling are not included"),
            "rows": rows}


# --------------------------------------------------------------------------- #
# scenario: management commands (six evidence calls + one business call)
# --------------------------------------------------------------------------- #
def management_flow(ledger: CallLedger, *, operation: str, owner_id: str,
                    business: str, parameters: dict) -> dict:
    """One management command through the durable original-request channel.

    Exact frontend sequence (resourceOutbox.send/submit): evidence write and its
    confirming read, the one business call, two ``current()`` owner reads around
    the acknowledgement, the confirming read before closure, then resolve.
    """
    key = parameters["idempotency_key"]
    written = ledger.command("request-evidence.write", {
        "operation": operation, "idempotency_key": key, "owner_kind": MANAGEMENT_OWNER,
        "owner_id": owner_id, "request": deepcopy(parameters)})
    ledger.query("request-evidence.read", {"owner_kind": MANAGEMENT_OWNER, "owner_id": owner_id})
    result = ledger.command(business, parameters)
    ledger.query("request-evidence.read", {"owner_kind": MANAGEMENT_OWNER, "owner_id": owner_id})
    ledger.query("request-evidence.read", {"owner_kind": MANAGEMENT_OWNER, "owner_id": owner_id})
    ledger.query("request-evidence.read", {"owner_kind": MANAGEMENT_OWNER, "owner_id": owner_id})
    ledger.command("request-evidence.resolve", {
        "operation": operation, "idempotency_key": key,
        "request_digest": written["result"]["request_digest"], "resolution": "verified"})
    return result


def scenario_management(workdir: Path) -> dict:
    rows = []

    def fresh(name: str, *, runnable: bool = False, session: bool = False,
              run: bool = False):
        case_dir = workdir / f"management-{name}-{uuid4().hex[:8]}"
        case_dir.mkdir(parents=True, exist_ok=True)
        service, app = open_service(case_dir, "management.sqlite", include_runnable=runnable)
        document = runnable_document() if runnable else simple_document()
        saved = app.command(OPERATION, operation_request(document, 0))["result"]
        payload = {"service": service, "app": app, "saved": saved,
                   "workflow_id": saved["workflow_definition_id"]}
        if session or run:
            payload["session"] = create_session(app, payload["workflow_id"],
                                                saved["definition_revision"])
        if run:
            started = start_run(app, payload["session"])
            service.wait(started["active_chain_run_id"], timeout=20)
            payload["session"] = app.query("session.read", {
                "session_id": payload["session"]["workflow_session_id"]})
        return payload

    def record(fixture, command: str, operation: str, business: str,
               parameters: dict) -> None:
        ledger = CallLedger(fixture["app"])
        management_flow(ledger, operation=operation, owner_id=fixture["workflow_id"],
                        business=business, parameters=parameters)
        rows.append({"command": command,
                     "evidence_calls": ledger.totals()["application_calls"] - 1,
                     "business_calls": ledger.totals()["command_calls"] - 2,
                     "totals": ledger.totals(),
                     "calls": ledger.rows_rounded()})

    try:
        fixture = fresh("archive")
        record(fixture, "definition.archive", "definition.archive", "definition.archive",
               {"identity": fixture["workflow_id"],
                "expected_revision": fixture["saved"]["definition_revision"],
                "idempotency_key": str(uuid4())})
    finally:
        fixture["service"].close()

    try:
        fixture = fresh("delete")
        record(fixture, "definition.delete", "definition.delete", "definition.delete",
               {"identity": fixture["workflow_id"],
                "expected_revision": fixture["saved"]["definition_revision"],
                "idempotency_key": str(uuid4())})
    finally:
        fixture["service"].close()

    try:
        fixture = fresh("session-create")
        record(fixture, "session.create", "session.create", "session.create",
               {"workflow_definition_id": fixture["workflow_id"],
                "definition_revision": fixture["saved"]["definition_revision"],
                "idempotency_key": str(uuid4())})
    finally:
        fixture["service"].close()

    for action in ("archive", "delete"):
        try:
            fixture = fresh(f"session-{action}", session=True)
            record(fixture, f"session.{action}", f"session.{action}", f"session.{action}",
                   {"session_id": fixture["session"]["workflow_session_id"],
                    "expected_revision": fixture["session"]["revision"],
                    "idempotency_key": str(uuid4())})
        finally:
            fixture["service"].close()

    try:
        fixture = fresh("run-start", runnable=True, session=True)
        ledger = CallLedger(fixture["app"])
        parameters = {"session_id": fixture["session"]["workflow_session_id"],
                      "expected_revision": fixture["session"]["revision"],
                      "idempotency_key": str(uuid4())}
        started = management_flow(ledger, operation="run.start",
                                  owner_id=fixture["workflow_id"],
                                  business="run.start", parameters=parameters)
        fixture["service"].wait(started["result"]["active_chain_run_id"], timeout=20)
        rows.append({"command": "run.start",
                     "evidence_calls": ledger.totals()["application_calls"] - 1,
                     "business_calls": ledger.totals()["command_calls"] - 2,
                     "totals": ledger.totals(),
                     "calls": ledger.rows_rounded()})
    finally:
        fixture["service"].close()
    return {"scenario": "management-commands",
            "note": ("each row is the exact frontend management channel sequence: 6 evidence calls "
                     "(write, confirming read, current read, current read, closure read, resolve) and "
                     "1 business call; counts are application calls, not HTTP requests"),
            "rows": rows}


# --------------------------------------------------------------------------- #
# scenario: read-only preview and contract (outside the save chain)
# --------------------------------------------------------------------------- #
def scenario_preview(workdir: Path) -> dict:
    rows = []
    contract = None
    for node_count in (1, 20, 100):
        case_dir = workdir / f"preview-{node_count}-{uuid4().hex[:8]}"
        case_dir.mkdir(parents=True, exist_ok=True)
        service, app = open_service(case_dir, "preview.sqlite", include_runnable=True)
        try:
            document = runnable_document(runs=max(0, node_count - 2))
            saved = app.command(OPERATION, operation_request(document, 0))["result"]
            proposed = deepcopy(saved["document"])
            proposed["name"] = f"preview rename {node_count}"
            request = {"workflow_definition_id": saved["workflow_definition_id"],
                       "document": proposed, "expected_edit_sequence": saved["edit_sequence"]}
            before = count_rows(case_dir / "preview.sqlite")
            outcome = timed(lambda: app.query("definition.preview", request))
            after = count_rows(case_dir / "preview.sqlite")
            if contract is None:
                contract = timed(lambda: app.query("document.contract"))
            rows.append({
                "node_count": len(document["nodes"]),
                "seconds": round(outcome["seconds"], 6),
                "request_bytes": json_bytes(request),
                "response_bytes": None if outcome["value"] is None else json_bytes(outcome["value"]),
                "error": describe_failure(outcome["failed"]),
                "read_only": (outcome["value"].get("read_only")
                              if isinstance(outcome["value"], dict) else None),
                "reserves_save_right": (outcome["value"].get("reserves_save_right")
                                        if isinstance(outcome["value"], dict) else None),
                "structure_ok": outcome["value"]["structure"]["ok"] if outcome["value"] else None,
                "compilation_ok": outcome["value"]["compilation"]["ok"] if outcome["value"] else None,
                "compilation_ordered_nodes": (outcome["value"]["compilation"].get("ordered_node_count")
                                              if outcome["value"] else None),
                "would_outcome": (outcome["value"]["changes"].get("would_outcome")
                                  if outcome["value"] else None),
                "wrote_records_delta": after["records"] - before["records"],
                "wrote_idempotency_delta": after["idempotency"] - before["idempotency"],
                "wrote_pending_commands_delta":
                    after["pending_commands"] - before["pending_commands"],
            })
        finally:
            service.close()
    return {"scenario": "preview-and-contract",
            "note": ("preview is a separate read-only query and is never part of the save five-step "
                     "chain; zero-write is verified by row counts before/after"),
            "contract": {"seconds": round(contract["seconds"], 6) if contract else None,
                         "response_bytes": None if not contract or contract["value"] is None
                         else json_bytes(contract["value"]),
                         "contract_version": contract["value"].get("contract_version")
                         if contract and isinstance(contract["value"], dict) else None},
            "rows": rows}


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
SCENARIOS = {
    "chain": lambda workdir, repeat: scenario_chain(workdir, repeat),
    "chain-narrow": lambda workdir, repeat: scenario_chain_narrow(workdir, repeat),
    "chain-history": lambda workdir, repeat: scenario_chain_history(workdir, repeat),
    "config": lambda workdir, repeat: scenario_config(workdir, repeat),
    "history": lambda workdir, repeat: scenario_history(workdir),
    "evidence": lambda workdir, repeat: scenario_evidence(workdir),
    "q08": lambda workdir, repeat: scenario_q08(workdir),
    "session": lambda workdir, repeat: scenario_session(workdir),
    "fault": lambda workdir, repeat: scenario_fault(workdir),
    "poll": lambda workdir, repeat: scenario_poll(workdir),
    "management": lambda workdir, repeat: scenario_management(workdir),
    "preview": lambda workdir, repeat: scenario_preview(workdir),
}


def environment() -> dict:
    import hashlib
    import platform

    repository = BACKEND.parent
    measured = [
        "backend/src/phase1_agent/graph_contracts.py",
        "backend/src/phase1_agent/workflow_configuration.py",
        "backend/src/phase1_agent/configuration_data.py",
        "backend/src/phase1_agent/configuration_service.py",
        "backend/src/phase1_agent/graph_store.py",
        "backend/src/phase1_agent/graph_service.py",
        "backend/src/phase1_agent/graph_drafts.py",
        "backend/src/phase1_agent/context_host_service.py",
        "backend/src/phase1_agent/context_package.py",
        "backend/src/phase1_agent/graph_application.py",
        "backend/src/phase1_agent/graph_application_contracts.py",
        "backend/src/phase1_agent/request_evidence.py",
        "backend/src/phase1_agent/workflow_modification_gate.py",
        "frontend/src/application/workflowConfiguration.ts",
        "frontend/src/adapters/requestEvidenceApi.ts",
        "frontend/src/application/workflowConfigurationCopy.ts",
        "frontend/src/components/GraphHistory.vue",
        "frontend/src/components/GraphRunControls.vue",
        "frontend/src/components/WorkflowWorkspace.vue",
        "frontend/src/stores/workflowGraph.ts",
        "frontend/src/adapters/workflowGraphApi.ts",
        "backend/tests/measurement_harness.py",
    ]
    snapshot = {}
    for relative in measured:
        path = repository / relative
        if path.is_file():
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return {"python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "code_snapshot_sha256_16": snapshot}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", required=True,
                        help="new isolated temp directory; the harness writes only here")
    parser.add_argument("--scenario", default="all",
                        help="comma separated: " + ",".join(SCENARIOS) + " or all")
    parser.add_argument("--repeat", type=int, default=3,
                        help="timing repeats for mutating/read scenarios (default 3)")
    parser.add_argument("--json", default=None,
                        help="write the full result JSON to this path (relative to cwd)")
    arguments = parser.parse_args(argv)
    workdir = Path(arguments.workdir).resolve()
    if workdir.exists() and any(workdir.iterdir()):
        print(f"refusing to reuse non-empty workdir: {workdir}", file=sys.stderr)
        return 2
    workdir.mkdir(parents=True, exist_ok=True)
    names = list(SCENARIOS) if arguments.scenario == "all" else [
        name.strip() for name in arguments.scenario.split(",") if name.strip()]
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {unknown}", file=sys.stderr)
        return 2
    result = {"harness": "s4-00-application-layer-baseline",
              "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "workdir": str(workdir),
              "argv": sys.argv,
              "repeat": arguments.repeat,
              "environment": environment(),
              "limitations": [
                  "application layer only: no HTTP hop, no network RTT, no server scheduling",
                  "no real frontend wait or edit-lock window is measured",
                  "request/response bytes are canonical JSON bodies without HTTP headers",
                  "runs use in-process synthetic node executors; no package files, no tools, no model calls",
                  "unknown-result path is measured through the read-only receipt verification; no "
                  "post-commit fault injection was used for a simulated lost response",
                  "history/q08 cases build their fixtures once per size and are not repeated for timing",
              ],
              "scenarios": {}}
    for name in names:
        started = time.perf_counter()
        result["scenarios"][name] = SCENARIOS[name](workdir / name, arguments.repeat)
        result["scenarios"][name]["wall_seconds"] = round(time.perf_counter() - started, 6)
        print(f"[{name}] {result['scenarios'][name]['wall_seconds']}s", file=sys.stderr)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if arguments.json:
        target = Path(arguments.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(f"wrote {target}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
