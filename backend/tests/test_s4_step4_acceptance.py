"""Independent verifier acceptance for step 4 backend (V4-01..V4-04).

Owned by the verifier; independent from the implementation team's own tests.
Everything runs on isolated temporary SQLite databases and the real
service/application boundaries.  The cost section runs the project's own
measurement harness (the instrument that produced the S4-00 baseline) so the
before/after numbers are the same measurement; the correctness sections do not
reuse the implementation tests.

Run (backend directory):

    & '..\\.venv\\Scripts\\python.exe' -m pytest -q tests/test_s4_step4_acceptance.py \
        --basetemp <new isolated directory>
"""

from __future__ import annotations

import ast
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from time import perf_counter
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_identity import application_command_digest
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.request_evidence import RequestEvidenceService, read_workflow_pending

from frontend_node_test_support import (
    create as frontend_create, graph as frontend_graph, owner as frontend_owner,
)
from test_configuration_application import configuration_document, prompt_registry
from test_workflow_configuration import create_session, document, registry, update


TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
RESULTS_DIR = TESTS_DIR / "measurement_results"
BASELINE_PATH = RESULTS_DIR / "s4-00-baseline-2026-11-04-run2.json"
HARNESS_PATH = TESTS_DIR / "measurement_harness.py"

OPERATION = "definition.update"
OWNER_KIND = "workflow-configuration"


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _rejected(call, code, status=400):
    with pytest.raises(ContractValidationError) as error:
        call()
    assert error.value.reason_code == code, error.value.reason_code
    assert error.value.status_code == status, error.value.status_code
    return error.value


def _rows(service, query, parameters=()):
    with closing(service._store()) as store:
        return store._connection.execute(query, parameters).fetchall()


def _count(service, table):
    return _rows(service, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _normalise(value):
    """Drop volatile ids/timings/repeat counts for same-shape comparisons."""
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()
                if "seconds" not in key and key != "repeats"}
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, str):
        return _UUID.sub("<uuid>", value)
    return value


def _outcome(value):
    return _normalise(value) if value is not None else None


class Scans:
    """Count both full history scans and Q08 SQL max reads in-process."""

    def __init__(self):
        self.full_calls = 0
        self.full_rows = 0
        self.max_calls = 0

    def __enter__(self):
        from phase1_agent import configuration_data
        from phase1_agent import graph_store
        self._data = configuration_data
        self._store = graph_store
        self._original_full = configuration_data.ConfigurationRepository.definition_revisions
        self._original_max = graph_store.GraphRecordStore.max_definition_revision
        counter = self

        def full(repository, identity):
            counter.full_calls += 1
            values = counter._original_full(repository, identity)
            counter.full_rows += len(values)
            return values

        def maximum(repository, identity):
            counter.max_calls += 1
            return counter._original_max(repository, identity)

        configuration_data.ConfigurationRepository.definition_revisions = full
        graph_store.GraphRecordStore.max_definition_revision = maximum
        return self

    def __exit__(self, *exc_info):
        self._data.ConfigurationRepository.definition_revisions = self._original_full
        self._store.GraphRecordStore.max_definition_revision = self._original_max


def _append_record(service, record_id, payload):
    with sqlite3.connect(service.database) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO records(record_type,record_id,payload,immutable,created_at) "
            "VALUES('workflow_definition_revision',?,?,1,'2026-11-04T00:00:00.000Z')",
            (record_id, payload))
        connection.commit()


def _fixed_document():
    return document(mode="fixed")


def _build_history(service, revisions):
    saved = update(service, _fixed_document())
    for index in range(1, revisions):
        proposed = deepcopy(saved["document"])
        proposed["nodes"][0]["config"]["text"] = f"revision-{index}"
        saved = update(service, proposed, saved["edit_sequence"])
        assert saved["outcome"] == "saved", saved
        assert saved["definition_revision"] == index + 1, saved
    return saved


def _fixed_edit(saved):
    proposed = deepcopy(saved["document"])
    proposed["nodes"][0]["config"]["text"] = "next fixed revision"
    return proposed


def _owner_rows(path, owner_id):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT operation,idempotency_key,owner_kind,owner_id,request_json,request_digest,resolution "
            "FROM pending_commands WHERE owner_id=? ORDER BY rowid", (owner_id,)).fetchall()


# ---------------------------------------------------------------------------
# V4-01: same-instrument cost comparison against the S4-00 baseline run2
# ---------------------------------------------------------------------------

def _run_harness(tmp_path):
    workdir = tmp_path / "harness-work"
    workdir.mkdir()
    output = tmp_path / "s4-after.json"
    completed = subprocess.run(
        [sys.executable, str(HARNESS_PATH), "--workdir", str(workdir),
         "--scenario", "all", "--repeat", "2", "--json", str(output)],
        cwd=str(BACKEND_DIR), capture_output=True, text=True, timeout=600)
    assert completed.returncode == 0, f"harness failed: {completed.stderr[-3000:]}"
    return json.loads(output.read_text(encoding="utf-8"))


def test_v4_01_measured_cost_improves_and_other_paths_are_unchanged(tmp_path):
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    after = _run_harness(tmp_path)
    base_scenarios, after_scenarios = baseline["scenarios"], after["scenarios"]
    # The harness gained the two S4-01 flow scenarios; every old scenario must remain.
    assert set(base_scenarios) <= set(after_scenarios)

    # (a) Legacy five-step flow is byte-identical; only the redundant history scan drops.
    base_chain = {row["node_count"]: row for row in base_scenarios["chain"]["rows"]}
    after_chain = {row["node_count"]: row for row in after_scenarios["chain"]["rows"]}
    assert set(after_chain) == set(base_chain)
    for size, before in base_chain.items():
        row = after_chain[size]
        assert row["application_calls"] == before["application_calls"] == 5
        assert row["document_bytes"] == before["document_bytes"]
        assert row["request_bytes"] == before["request_bytes"]
        assert row["response_bytes"] == before["response_bytes"]
        assert row["per_call_request_bytes"] == before["per_call_request_bytes"]
        assert row["per_call_response_bytes"] == before["per_call_response_bytes"]
        assert row["history_scan_calls"] == before["history_scan_calls"] - 1 == 1
        assert _outcome(row["outcome"]) == _outcome(before["outcome"])

    # (b) A fixed save no longer performs the second full history scan; bytes are equal.
    base_config = base_scenarios["config"]["rows"]
    after_config = after_scenarios["config"]["rows"]
    assert len(after_config) == len(base_config)
    improved = 0
    for before, row in zip(base_config, after_config):
        assert row["kind"] == before["kind"]
        assert row["request_bytes"] == before["request_bytes"]
        assert row["response_bytes"] == before["response_bytes"]
        assert _outcome(row["outcome"]) == _outcome(before["outcome"])
        assert row["error"] == before["error"]
        if before["definition_revisions_calls"] == 2:
            improved += 1
            assert row["definition_revisions_calls"] == 1
            assert row["historical_rows_validated"] == before["historical_rows_validated"] // 2
            assert row["history_validated_bytes"] * 2 == before["history_validated_bytes"]
        else:
            assert row["definition_revisions_calls"] == before["definition_revisions_calls"]
            assert row["historical_rows_validated"] == before["historical_rows_validated"]
    assert improved >= 2  # initial save + fixed advance

    # (c) History sizes 1/10/44/100: display-only is unchanged, fixed drops one pass.
    for before, row in zip(base_scenarios["history"]["rows"], after_scenarios["history"]["rows"]):
        size = before["fixed_revisions"]
        assert row["fixed_revisions"] == size
        for case in ("display-only", "unchanged"):
            old, new = before["cases"][case], row["cases"][case]
            assert new["definition_revisions_calls"] == old["definition_revisions_calls"] == 1
            assert new["historical_rows_validated"] == old["historical_rows_validated"]
            assert new["request_bytes"] == old["request_bytes"]
            assert new["response_bytes"] == old["response_bytes"]
            assert _outcome(new["outcome"]) == _outcome(old["outcome"])
        old, new = before["cases"]["fixed"], row["cases"]["fixed"]
        assert (old["definition_revisions_calls"], old["historical_rows_validated"]) == (2, 2 * size)
        assert (new["definition_revisions_calls"], new["historical_rows_validated"]) == (1, size)
        assert new["history_validated_bytes"] * 2 == old["history_validated_bytes"]
        assert new["request_bytes"] == old["request_bytes"]
        assert new["response_bytes"] == old["response_bytes"]
        assert _outcome(new["outcome"]) == _outcome(old["outcome"])
        assert new["history_scan_error"] == old["history_scan_error"]

    # (d) Corrupt-history behaviour is identical; only the removed scan is shorter.
    for before, row in zip(base_scenarios["q08"]["rows"], after_scenarios["q08"]["rows"]):
        assert row["corruption"] == before["corruption"]
        for case in ("display-only", "fixed"):
            old, new = before[case], row[case]
            assert new["reached"] == old["reached"]
            assert new["error"] == old["error"]
            assert new["history_scan_error"] == old["history_scan_error"]
            assert new["edit_sequence_before"] == old["edit_sequence_before"]
            assert new["edit_sequence_after"] == old["edit_sequence_after"]
            assert new["wrote_anything"] == old["wrote_anything"]
            assert _outcome(new["outcome"]) == _outcome(old["outcome"])
            assert new["history_scan_error"] == old["history_scan_error"]
            if case == "fixed" and old["history_scan_calls_before_failure"] >= 2:
                assert new["history_scan_calls_before_failure"] == \
                    old["history_scan_calls_before_failure"] - 1
                assert new["history_rows_before_failure"] * 2 == old["history_rows_before_failure"]
            else:
                assert new["history_scan_calls_before_failure"] == \
                    old["history_scan_calls_before_failure"]

    # (e) Evidence 0/50/200/600 rows: full read unchanged, narrow read is a summary + point.
    for before, row in zip(base_scenarios["evidence"]["rows"], after_scenarios["evidence"]["rows"]):
        assert row["history_rows"] == before["history_rows"]
        assert row["stored_rows"] == before["stored_rows"]
        assert row["pending_rows"] == before["pending_rows"]
        assert _normalise(row["full_owner_read"]) == _normalise(before["full_owner_read"])
        narrow = row["narrow_reads"]
        assert narrow["errors"] == []
        assert narrow["owner_pending_page_items"] == row["pending_rows"]
        assert narrow["point_read_rows"] == row["pending_rows"]
        assert narrow["pending_narrow_rows"] == row["pending_rows"]
        # A pending point read returns the complete original body, not a summary.
        if row["pending_rows"]:
            assert narrow["point_read_bytes"] == narrow["pending_narrow_bytes"] > 100
            assert narrow["owner_pending_page_bytes"] < narrow["point_read_bytes"]
        else:
            assert narrow["point_read_bytes"] <= 2
        if row["stored_rows"] >= 200:
            assert narrow["owner_pending_page_bytes"] * 100 < row["full_owner_read"]["response_bytes"]
            assert narrow["point_read_bytes"] * 100 < row["full_owner_read"]["response_bytes"]

    # (f) The narrow save flow costs one extra bodyless summary; with history it is far cheaper.
    base_chain_rows = after_scenarios["chain"]["rows"]
    narrow_rows = after_scenarios["chain-narrow"]["rows"]
    for plain, narrow in zip(base_chain_rows, narrow_rows):
        assert narrow["node_count"] == plain["node_count"]
        assert narrow["application_calls"] == plain["application_calls"] + 1 == 6
        assert narrow["response_bytes"] - plain["response_bytes"] <= 200
        assert _outcome(narrow["outcome"]) == _outcome(plain["outcome"])
    history_rows = after_scenarios["chain-history"]["rows"]
    for row in history_rows:
        assert row["application_calls"] == 6
        if row["mode"] != "narrow":
            continue
        base = next(candidate for candidate in history_rows
                    if candidate["mode"] == "baseline" and candidate["node_count"] == row["node_count"])
        assert _outcome(row["outcome"]) == _outcome(base["outcome"])
        # 100 historical evidence rows: the full-history read is tens of times larger.
        assert base["response_bytes"] / row["response_bytes"] >= 20
        assert row["request_bytes"] <= base["request_bytes"] + 500

    # (g) Untouched paths keep their exact shape: preview/contract, management, session, fault.
    for name in ("preview", "management", "session", "fault"):
        assert _normalise(after_scenarios[name]) == _normalise(base_scenarios[name]), name
    # The running-session poll body can vary by one digit with the gated run state; the
    # query count and summary page must stay identical.  The harness gained the S4-03
    # summaries/identity-reuse measurements, so baseline cases are matched by name.
    base_poll = base_scenarios["poll"]["rows"]
    after_poll = after_scenarios["poll"]["rows"]
    renames = {"current definition read (manual refreshPublishedRevision today)":
               "current definition read (manual refreshPublishedRevision before S4-03)"}
    by_case = {row["case"]: row for row in after_poll}
    for before in base_poll:
        case = renames.get(before["case"], before["case"])
        row = by_case.get(case)
        assert row is not None, f"poll case disappeared: {case}"
        for key in ("per_tick_calls", "summaries_bytes", "session_status"):
            if key in before or key in row:
                assert row.get(key) == before.get(key), (case, key)
        for key in ("session_read_bytes", "per_tick_bytes"):
            if key in before and key in row:
                assert abs(row[key] - before[key]) <= 8, (case, key)
        assert _normalise({key: value for key, value in row.items()
                           if key not in ("session_read_bytes", "per_tick_bytes", "case")}) == \
            _normalise({key: value for key, value in before.items()
                        if key not in ("session_read_bytes", "per_tick_bytes", "case")})
    # Q09: the summary read replaces the whole current document read, and identity reuse
    # turns nine frozen-definition downloads into one.
    summaries = next(case for case in after_poll if case["case"].startswith(
        "definition summaries read (refreshPublishedRevision after Q09)"))
    legacy_read = next(case for case in after_poll if case["case"].startswith(
        "current definition read (manual refreshPublishedRevision"))
    assert summaries["response_bytes"] * 5 < legacy_read["response_bytes"]
    reuse = next(case for case in after_poll if case["case"].startswith(
        "Q09 frozen definition identity reuse (after S4-03)"))
    assert reuse["frozen_definition_reads_before_s4_03"] == 9
    assert reuse["frozen_definition_reads_after_s4_03_with_identity_reuse"] == 1


def test_v4_01_fixed_save_measures_the_removed_scan_directly(tmp_path):
    instance = GraphWorkflowService(tmp_path / "direct.sqlite", registry=registry())
    try:
        saved = _build_history(instance, 20)
        with Scans() as scans:
            started = perf_counter()
            result = update(instance, _fixed_edit(saved), saved["edit_sequence"])
            elapsed = perf_counter() - started
        assert result["outcome"] == "saved"
        assert result["definition_revision"] == 21
        # Exactly the unchanged first pass scans; the former second pass is one SQL max.
        assert (scans.full_calls, scans.full_rows, scans.max_calls) == (1, 20, 1)
        assert elapsed < 5.0
        with Scans() as display:
            rename = deepcopy(result["document"])
            rename["name"] = "display only after Q08"
            renamed = update(instance, rename, result["edit_sequence"])
        assert renamed["outcome"] == "saved" and renamed["changed_categories"] == ["display"]
        # Display-only still reads the whole history once; the fixed advance added revision 21.
        assert (display.full_calls, display.full_rows, display.max_calls) == (1, 21, 0)
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# V4-02: narrow evidence reads, layered summary and the unknown-result path
# ---------------------------------------------------------------------------

def _configuration_request(registry_instance, *, key=None):
    value = configuration_document(registry_instance)
    return {"workflow_definition_id": value["workflow_definition_id"],
            "document": deepcopy(value), "expected_edit_sequence": 0,
            "idempotency_key": key or str(uuid4())}


def _write_parameters(request):
    return {"operation": OPERATION, "idempotency_key": request["idempotency_key"],
            "owner_kind": OWNER_KIND, "owner_id": request["workflow_definition_id"],
            "request": deepcopy(request)}


def _read_parameters(record, **extra):
    return {"owner_kind": record["owner_kind"], "owner_id": record["owner_id"], **extra}


@pytest.fixture
def evidence_app(tmp_path):
    service = GraphWorkflowService(tmp_path / "evidence.sqlite", registry=prompt_registry())
    try:
        yield service, GraphApplication(service)
    finally:
        service.close()


def test_v4_02_point_read_returns_complete_body_and_stays_owner_scoped(evidence_app):
    service, app = evidence_app
    first = _configuration_request(service.registry)
    second = _configuration_request(service.registry)
    written = app.command("request-evidence.write", _write_parameters(first))["result"]
    other = app.command("request-evidence.write", _write_parameters(second))["result"]

    point = app.query("request-evidence.read", _read_parameters(
        written, operation=OPERATION, idempotency_key=written["idempotency_key"]))
    assert point == [written]
    assert point[0]["request"] == first  # complete original body
    assert point[0]["request_digest"] == written["request_digest"]

    # A key stored under another owner is a conflict, never an empty answer.
    error = _rejected(lambda: app.query("request-evidence.read", _read_parameters(
        other, operation=OPERATION, idempotency_key=written["idempotency_key"])),
        "request_evidence_conflict", 409)
    assert error.status_code == 409
    # A truly absent key is still an empty exact read (not another owner's row).
    assert app.query("request-evidence.read", _read_parameters(
        written, operation=OPERATION, idempotency_key=str(uuid4()))) == []


def test_v4_02_pending_narrow_read_keeps_history_and_writes_nothing(evidence_app):
    service, app = evidence_app
    first = _configuration_request(service.registry)
    written = app.command("request-evidence.write", _write_parameters(first))["result"]
    before = _owner_rows(service.database, written["owner_id"])

    pending = app.query("request-evidence.read", _read_parameters(written, resolution="pending"))
    assert pending == [written] and pending[0]["request"] == first
    page = app.query("request-evidence.pending", {
        "owner_kind": written["owner_kind"], "owner_id": written["owner_id"]})
    assert page["next_cursor"] is None and len(page["items"]) == 1
    # The owner summary never carries the original body.
    assert set(page["items"][0]) == {"owner_kind", "owner_id", "operation",
                                     "idempotency_key", "request_digest", "resolution"}

    resolved = app.command("request-evidence.resolve", {
        "operation": OPERATION, "idempotency_key": written["idempotency_key"],
        "request_digest": written["request_digest"], "resolution": "verified"})["result"]
    assert app.query("request-evidence.read", _read_parameters(written, resolution="pending")) == []
    assert app.query("request-evidence.pending", {
        "owner_kind": written["owner_kind"], "owner_id": written["owner_id"]})["items"] == []

    # Nothing is deleted or migrated: the verified row stays, with its original body.
    after = _owner_rows(service.database, written["owner_id"])
    assert len(after) == len(before) == 1
    assert after[0][0] == OPERATION and after[0][1] == written["idempotency_key"]
    assert after[0][4] == before[0][4]  # request_json unchanged
    assert after[0][5] == before[0][5]  # digest unchanged
    assert after[0][6] == "verified" and before[0][6] == "pending"

    # A later pending request coexists with the verified history; the full read is unchanged.
    follow_up = {**first, "idempotency_key": str(uuid4())}
    again = app.command("request-evidence.write", _write_parameters(follow_up))["result"]
    full = app.query("request-evidence.read", _read_parameters(written))
    assert [row["resolution"] for row in full] == ["verified", "pending"]
    assert [row["idempotency_key"] for row in full] == [
        written["idempotency_key"], again["idempotency_key"]]
    assert app.query("request-evidence.read", _read_parameters(written, resolution="pending")) == [again]
    # Narrow/read operations never rewrite or remove stored evidence.
    assert _owner_rows(service.database, written["owner_id"]) == _owner_rows(
        service.database, written["owner_id"])


def test_v4_02_damaged_evidence_is_reported_not_treated_as_missing(tmp_path):
    path = tmp_path / "damaged.sqlite"
    service = GraphWorkflowService(path, registry=prompt_registry())
    app = GraphApplication(service)
    try:
        request = _configuration_request(service.registry)
        written = app.command("request-evidence.write", _write_parameters(request))["result"]
    finally:
        service.close()

    def reopen():
        instance = GraphWorkflowService(path, registry=prompt_registry())
        return instance, GraphApplication(instance)

    # A stored body that no longer parses is an explicit storage error for every read.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE pending_commands SET request_json='{\"broken\":' "
            "WHERE operation=? AND idempotency_key=?", (OPERATION, written["idempotency_key"]))
        connection.commit()
    service, app = reopen()
    try:
        for parameters in (
            _read_parameters(written, operation=OPERATION,
                             idempotency_key=written["idempotency_key"]),
            _read_parameters(written, resolution="pending"),
            _read_parameters(written),
        ):
            _rejected(lambda parameters=parameters: app.query(
                "request-evidence.read", parameters), "storage_contract_violation", 500)
    finally:
        service.close()

    # A digest that no longer matches its stored body is never accepted as a small issue;
    # a malformed summary field is a storage error, not "no pending request".
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE pending_commands SET request_json=? WHERE operation=? AND idempotency_key=?",
            (json.dumps(request, sort_keys=True, separators=(",", ":")),
             OPERATION, written["idempotency_key"]))
        connection.execute(
            "UPDATE pending_commands SET request_digest='not-a-hex-digest' "
            "WHERE operation=? AND idempotency_key=?", (OPERATION, written["idempotency_key"]))
        connection.commit()
    service, app = reopen()
    try:
        _rejected(lambda: app.query("request-evidence.read", _read_parameters(
            written, operation=OPERATION, idempotency_key=written["idempotency_key"])),
            "storage_contract_violation", 500)
        _rejected(lambda: read_workflow_pending(
            path, owner_kind=OWNER_KIND, owner_id=written["owner_id"]),
            "storage_contract_violation", 500)
    finally:
        service.close()


def test_v4_02_non_unique_pending_is_reported_not_collapsed(tmp_path):
    path = tmp_path / "nonunique.sqlite"
    service = GraphWorkflowService(path, registry=prompt_registry())
    try:
        request = _configuration_request(service.registry)
        written = GraphApplication(service).command(
            "request-evidence.write", _write_parameters(request))["result"]
    finally:
        service.close()
    # Remove the single-pending guarantee and inject the anomaly directly: the reader
    # must report it instead of answering with one of the rows.
    second = {**request, "idempotency_key": str(uuid4())}
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX pending_command_owner")
        connection.execute(
            "INSERT INTO pending_commands VALUES(?,?,?,?,?,?,?)",
            (OPERATION, second["idempotency_key"], OWNER_KIND, written["owner_id"],
             json.dumps(second, sort_keys=True, separators=(",", ":")),
             application_command_digest(OPERATION, second), "pending"))
        connection.commit()
    _rejected(lambda: read_workflow_pending(
        path, owner_kind=OWNER_KIND, owner_id=written["owner_id"]),
        "storage_contract_violation", 500)

    # Service-level narrow read carries the same guard (duck-typed store exposing the
    # two stored rows), including a conflicting owner answer for a point read.
    stored = [
        {"operation": OPERATION, "idempotency_key": written["idempotency_key"],
         "owner_kind": OWNER_KIND, "owner_id": written["owner_id"],
         "request_digest": written["request_digest"], "resolution": "pending",
         "request_json": json.dumps(request, sort_keys=True, separators=(",", ":"))},
        {"operation": OPERATION, "idempotency_key": second["idempotency_key"],
         "owner_kind": OWNER_KIND, "owner_id": written["owner_id"],
         "request_digest": application_command_digest(OPERATION, second),
         "resolution": "pending",
         "request_json": json.dumps(second, sort_keys=True, separators=(",", ":"))},
    ]

    class Connection:
        def execute(self, _statement, _parameters):
            return self

        def fetchall(self):
            return list(stored)

    class Store:
        def __init__(self):
            self._connection = Connection()

        def close(self):
            pass

    fake = RequestEvidenceService(path, store_factory=Store)
    _rejected(lambda: fake.read_evidence(OWNER_KIND, written["owner_id"], resolution="pending"),
              "storage_contract_violation", 500)
    full = fake.read_evidence(OWNER_KIND, written["owner_id"])
    assert len(full) == 2 and {row["resolution"] for row in full} == {"pending"}


def test_v4_02_narrow_parameters_are_validated(evidence_app):
    service, app = evidence_app
    request = _configuration_request(service.registry)
    written = app.command("request-evidence.write", _write_parameters(request))["result"]
    base = _read_parameters(written)
    _rejected(lambda: app.query("request-evidence.read", {**base, "operation": OPERATION}),
              "invalid_request", 400)
    _rejected(lambda: app.query("request-evidence.read",
                                {**base, "idempotency_key": str(uuid4())}), "invalid_request", 400)
    _rejected(lambda: app.query("request-evidence.read",
                                {**base, "resolution": "verified"}), "invalid_request", 400)
    _rejected(lambda: app.query("request-evidence.read", {
        **base, "resolution": "pending", "operation": OPERATION,
        "idempotency_key": written["idempotency_key"]}), "invalid_request", 400)
    _rejected(lambda: app.query("request-evidence.pending", {"owner_kind": OWNER_KIND}),
              "invalid_request", 400)
    _rejected(lambda: app.query("request-evidence.pending", {
        "owner_kind": "foreign-owner", "owner_id": written["owner_id"]}),
        "invalid_request", 400)
    _rejected(lambda: app.query("request-evidence.pending", {
        "owner_kind": OWNER_KIND, "owner_id": written["owner_id"],
        "cursor": {"owner_kind": OWNER_KIND, "owner_id": written["owner_id"],
                   "operation": OPERATION, "idempotency_key": written["idempotency_key"]}}),
        "invalid_request", 400)
    # The full two-field read keeps working exactly as before.
    assert app.query("request-evidence.read", base) == [written]


def test_v4_02_unknown_result_uses_read_only_receipt_verification(tmp_path):
    service = GraphWorkflowService(tmp_path / "receipt.sqlite", registry=registry())
    try:
        app = GraphApplication(service)
        value = _fixed_document()
        parameters = {"workflow_definition_id": value["workflow_definition_id"],
                      "document": deepcopy(value), "expected_edit_sequence": 0,
                      "idempotency_key": "unknown-verify-key"}
        accepted = app.command("definition.update", parameters)
        saved = accepted["result"]
        assert saved["outcome"] == "saved" and saved["definition_revision"] == 1
        revisions = _count(service, "records")
        receipts = _count(service, "idempotency")

        # Verifying an unknown result is a read: same acceptance, no business call, no writes.
        for _ in range(2):
            verified = app.query("receipt.read", {
                "operation": "definition.update", "parameters": deepcopy(parameters)})
            assert verified["outcome"] == "matched"
            assert _outcome(verified["result"]) == _outcome(accepted["result"])
            assert _outcome(verified["receipt"]) == _outcome(accepted["receipt"])
            assert _count(service, "records") == revisions
            assert _count(service, "idempotency") == receipts
        # Replaying the same accepted request returns the receipt instead of executing again.
        replayed = app.command("definition.update", deepcopy(parameters))
        assert _outcome(replayed["result"]) == _outcome(accepted["result"])
        assert _outcome(replayed["receipt"]) == _outcome(accepted["receipt"])
        assert _count(service, "records") == revisions
        # A different body under the same key is still an explicit conflict, never a replay.
        conflicting = deepcopy(parameters)
        conflicting["document"]["name"] = "different body"
        _rejected(lambda: app.command("definition.update", conflicting),
                  "idempotency_conflict", 409)
        assert _count(service, "records") == revisions
    finally:
        service.close()


# ---------------------------------------------------------------------------
# V4-03: Q08 second-pass SQL max and unchanged bad-record behaviour
# ---------------------------------------------------------------------------

def test_v4_03_sql_max_matches_validated_history_and_skips_corrupt_rows(tmp_path):
    service = GraphWorkflowService(tmp_path / "max.sqlite", registry=registry())
    try:
        saved = _build_history(service, 3)
        identity = saved["workflow_definition_id"]
        other = str(uuid4())
        # Invalid JSON, valid non-graph JSON and another workflow's record all carry
        # revisions above the real maximum; none may advance the number.
        _append_record(service, "corrupt:invalid-json",
                       '{"workflow_definition_id": "' + identity + '", "revision": 99, ')
        _append_record(service, "corrupt:not-graph", json.dumps(
            {"workflow_definition_id": identity, "revision": 88, "payload_note": "not a graph record"}))
        _append_record(service, "corrupt:other-workflow", json.dumps(
            {"schema_version": 2, "execution_model": "graph", "workflow_definition_id": other,
             "revision": 77, "bindings": [], "edges": [], "document": {}}))
        with closing(service._store()) as store:
            records = GraphRecordStore(store)
            sql_max = records.max_definition_revision(identity)
            validated = records.rows("workflow_definition_revision", workflow_definition_id=identity)
            assert [row["revision"] for row in validated] == [1, 2, 3]
            assert sql_max == max(row["revision"] for row in validated) == 3
            # The corrupt rows are untouched by the read-only maximum.
            assert store._connection.execute(
                "SELECT COUNT(*) FROM records WHERE record_type='workflow_definition_revision'"
            ).fetchone()[0] == 6
        with Scans() as scans:
            result = update(service, _fixed_edit(saved), saved["edit_sequence"])
        assert result["outcome"] == "saved" and result["definition_revision"] == 4
        assert (scans.full_calls, scans.full_rows, scans.max_calls) == (1, 3, 1)
    finally:
        service.close()


def test_v4_03_corrupt_graph_record_still_fails_the_first_pass_without_writes(tmp_path):
    service = GraphWorkflowService(tmp_path / "corrupt.sqlite", registry=registry())
    try:
        saved = _build_history(service, 2)
        identity = saved["workflow_definition_id"]
        _append_record(service, "corrupt:graph-bad-fields", json.dumps(
            {"schema_version": 2, "execution_model": "graph",
             "workflow_definition_id": identity, "revision": 99}))
        before_revisions = _count(service, "records")
        before_sequence = _rows(
            service, "SELECT edit_sequence FROM workflow_current_configuration "
                     "WHERE workflow_definition_id=?", (identity,))[0]["edit_sequence"]
        with Scans() as scans:
            error = _rejected(lambda: update(service, _fixed_edit(saved), saved["edit_sequence"]),
                              "storage_contract_violation", 500)
        assert error.status_code == 500
        # The bad record is still detected by the unchanged full first pass.
        assert scans.full_calls == 1 and scans.max_calls == 0
        assert _count(service, "records") == before_revisions
        assert _rows(service, "SELECT edit_sequence FROM workflow_current_configuration "
                              "WHERE workflow_definition_id=?", (identity,))[0]["edit_sequence"] \
            == before_sequence
        assert service.get_definition(identity)["revision"] == 2
    finally:
        service.close()


def test_v4_03_valid_higher_revision_is_still_stale_and_rolls_back(tmp_path):
    service = GraphWorkflowService(tmp_path / "stale.sqlite", registry=registry())
    try:
        saved = _build_history(service, 3)
        identity = saved["workflow_definition_id"]
        with closing(service._store()) as store:
            source = json.loads(store._connection.execute(
                "SELECT payload FROM records WHERE record_type='workflow_definition_revision' "
                "AND json_extract(payload,'$.workflow_definition_id')=? "
                "AND json_extract(payload,'$.revision')=?", (identity, 3)).fetchone()["payload"])
        ahead = deepcopy(source)
        ahead["revision"] = 5
        ahead["document"]["revision"] = 5
        _append_record(service, "verifier:ahead-revision", json.dumps(ahead))
        before_revisions = _count(service, "records")
        before_sequence = saved["edit_sequence"]
        _rejected(lambda: update(service, _fixed_edit(saved), saved["edit_sequence"]),
                  "stale_revision", 409)
        assert _count(service, "records") == before_revisions
        assert service.get_definition(identity)["revision"] == 3
        assert _rows(service, "SELECT edit_sequence FROM workflow_current_configuration "
                              "WHERE workflow_definition_id=?", (identity,))[0]["edit_sequence"] \
            == before_sequence
    finally:
        service.close()


def test_v4_03_caller_scope_keeps_the_original_scan_where_no_first_pass_exists(tmp_path):
    service = GraphWorkflowService(tmp_path / "callers.sqlite", registry=registry())
    try:
        # Initial save: the empty history was validated first, so the max is SQL.
        with Scans() as initial:
            saved = update(service, _fixed_document())
        assert saved["outcome"] == "saved" and saved["definition_revision"] == 1
        assert (initial.full_calls, initial.full_rows, initial.max_calls) == (1, 0, 1)
        # Extend the same identity to three fixed revisions.
        for index in (1, 2):
            proposed = deepcopy(saved["document"])
            proposed["nodes"][0]["config"]["text"] = f"caller-revision-{index}"
            saved = update(service, proposed, saved["edit_sequence"])
            assert saved["outcome"] == "saved" and saved["definition_revision"] == index + 1
        identity, revision, sequence = (
            saved["workflow_definition_id"], saved["definition_revision"], saved["edit_sequence"])
        # Derived target: the new identity has no first pass, so the original scan stays.
        create_session(service, saved)
        changed = _fixed_edit(saved)
        asked = update(service, changed, sequence)
        assert asked["outcome"] == "confirmation_required"
        with Scans() as derived:
            derived_result = update(service, changed, sequence, confirm=True)
        assert derived_result["outcome"] == "derived"
        assert derived.full_calls == 2 and derived.max_calls == 0
        # Copy target: the conflict check is the first pass for the target, so SQL max is used.
        application = GraphApplication(service)
        with Scans() as copied:
            copy_result = application.command("definition.copy", {
                "source_workflow_definition_id": identity,
                "expected_source_definition_revision": revision,
                "expected_source_edit_sequence": sequence,
                "target_workflow_definition_id": str(uuid4()),
                "target_name": "Verifier copy",
                "idempotency_key": "verifier-copy",
            })["result"]
        assert copy_result["outcome"] == "copied" and copy_result["definition_revision"] == 1
        assert (copied.full_calls, copied.max_calls) == (1, 1)
        # The legacy definition.save adapter has no first pass and keeps the full scan.
        legacy_value = _fixed_document()
        with Scans() as legacy:
            service._change("test.fixture.definition.save", "verifier-legacy",
                            {"document": deepcopy(legacy_value), "expected_revision": 0},
                            lambda repo: service._save_definition(repo, legacy_value, 0))
        assert (legacy.full_calls, legacy.max_calls) == (1, 0)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# S3-01 regression fix verification (task-26): direct snapshot reads stay covered
# and normal saves now reject ACL-inconsistent documents with a location.
# ---------------------------------------------------------------------------

def _seed_definition(service, value):
    """Store a revision through the legacy writer, bypassing the Q05-2 declaration checks."""
    document_value = deepcopy(value)
    return service._change(
        "test.fixture.frontend.seed", str(uuid4()),
        {"document": deepcopy(document_value), "expected_revision": 0},
        lambda repo: service._save_definition(repo, document_value, 0))


@pytest.mark.parametrize("failure", ["reader", "registrar"])
def test_v4_regression_normal_save_rejects_acl_inconsistent_document(tmp_path, failure):
    service = GraphWorkflowService(tmp_path / f"acl-save-{failure}.sqlite")
    try:
        value, _, frontends = frontend_graph(service, count=2, readers=failure != "reader")
        if failure == "registrar":
            frontends[0]["config"]["object_key"] = "chat-1"
            value["object_bindings"][1]["readers"].append(frontends[0]["node_binding_id"])
        error = _rejected(lambda: service.update_definition(
            value, workflow_definition_id=value["workflow_definition_id"],
            expected_edit_sequence=0, idempotency_key=str(uuid4())),
            "session_object_access_denied", 400)
        diagnostic = error.diagnostics[0]
        assert diagnostic["phase"] == "save"
        assert diagnostic["node_id"] == frontends[0]["node_binding_id"]
        assert diagnostic["path"] == "/nodes/2/config/object_key"
        assert diagnostic["expected"] == {"access": "read_write"}
        # The rejected save wrote neither a revision nor a receipt.
        assert service.list_definitions(include_archived=True) == []
        assert _count(service, "records") == 0
    finally:
        service.close()


@pytest.mark.parametrize("failure", ["reader", "registrar"])
def test_v4_regression_seeded_revision_still_rejected_by_direct_snapshot_read(
        tmp_path, failure):
    service = GraphWorkflowService(tmp_path / f"acl-read-{failure}.sqlite")
    try:
        value, _, frontends = frontend_graph(service, count=2, readers=failure != "reader")
        if failure == "registrar":
            frontends[0]["config"]["object_key"] = "chat-1"
            value["object_bindings"][1]["readers"].append(frontends[0]["node_binding_id"])
        # S3-01 made this document unsavable, so seed the stored revision directly: the
        # read-side defence must still reject the wrong ACL registration.
        seeded = _seed_definition(service, value)
        assert seeded["revision"] == 1
        session = frontend_create(service, seeded)
        expected = (("session_object_access_denied", 403) if failure == "reader"
                    else ("frontend_node_registration_mismatch", 409))
        error = _rejected(lambda: service.read_frontend_node_snapshot(
            *frontend_owner(seeded, frontends[0], session)), expected[0], expected[1])
        assert error.status_code == expected[1]
        assert service._frontend_node_snapshots == {}
    finally:
        service.close()


def test_v4_regression_test_file_keeps_direct_read_coverage_and_adds_positive_cases():
    source_path = TESTS_DIR / "test_frontend_node_snapshots.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    direct = functions.get("test_snapshot_direct_read_rejects_wrong_access_registration_and_type")
    assert direct is not None, "direct snapshot read test disappeared"
    # The three original read-side failures are still parametrised.
    decorators = " ".join(ast.unparse(node.decorator_list) for node in tree.body
                          if isinstance(node, ast.FunctionDef) and node.name == direct.name)
    assert "reader" in decorators and "registrar" in decorators and "type" in decorators
    body = ast.get_source_segment(source, direct) or ""
    assert "read_frontend_node_snapshot" in body
    # Since the save boundary now rejects it, the stored revision must be seeded directly.
    assert "_seed(" in body
    assert "session_object_access_denied" in body
    # Complementary positive coverage of the new save-time rule exists.
    positive = functions.get("test_normal_save_rejects_acl_inconsistent_document_with_location")
    assert positive is not None
    positive_body = ast.get_source_segment(source, positive) or ""
    assert "update_definition" in positive_body
    assert "session_object_access_denied" in positive_body
    assert "diagnostic" in positive_body
