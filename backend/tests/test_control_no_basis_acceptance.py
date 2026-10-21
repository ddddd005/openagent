"""C1 acceptance: released run basis keeps control diagnostics and never resumes.

Every call goes through the public ``GraphWorkflowService.control`` entry.  A
missing run basis is produced the same way the retained-basis tests do it: the
failed process itself released the basis, or the explicit in-memory basis is
removed after a supported pause/archive boundary.  No private
``_control_change_current`` call and no fabricated ``prepared`` tail is used.
"""

from contextlib import closing
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.execution_data import ConfigurationReadSession
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService

from test_graph_service import create, document, edge, gate_graph, node, text_graph
from test_model_package import uid
from test_models_service_integration import ModelDatabaseFixture
from test_run_basis import (
    _foreign_session, _manifest_rows, _record_payload, _rewrite_chain_start,
    _watch_boundary, _watch_end_manifest, _watch_resource_identities,
    _write_chain_start_payload,
)


NO_BASIS_MESSAGE = "Execution has no retained run basis"
NO_CONTINUATION_MESSAGE = "Execution has no same-process continuation"


def _registry():
    return create_package_registry().registry.detached()


@pytest.fixture
def service(tmp_path):
    instance = GraphWorkflowService(
        tmp_path / "control-no-basis.sqlite", registry=_registry())
    try:
        yield instance
    finally:
        instance.close()


class _RetainedExecutor:
    """Stands in for a worker that prepared a run without executing it."""

    def __init__(self):
        self.submitted = 0

    def submit(self, callback, *args, **kwargs):
        self.submitted += 1
        return SimpleNamespace(result=lambda timeout=None: None, done=lambda: True)

    def shutdown(self, wait=True):
        pass


def _watch_executor_submissions(monkeypatch, service):
    submissions = []
    original = service._executor.submit

    def submit(*args, **kwargs):
        submissions.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(service._executor, "submit", submit)
    return submissions


def _assert_no_basis_rejection(caught):
    assert caught.value.reason_code == "recovery_unavailable"
    assert caught.value.status_code == 409
    assert caught.value.diagnostics[0]["message"] == NO_BASIS_MESSAGE


def _assert_run_unchanged(before, after):
    assert after["chain"]["status"] == before["chain"]["status"]
    assert after["chain"]["revision"] == before["chain"]["revision"]
    assert after["chain"]["next_node_index"] == before["chain"]["next_node_index"]
    assert [run["run_id"] for run in after["node_runs"]] == [
        run["run_id"] for run in before["node_runs"]]
    assert [run["status"] for run in after["node_runs"]] == [
        run["status"] for run in before["node_runs"]]


def test_resume_without_run_basis_is_rejected_without_rescheduling(service, monkeypatch):
    sid, chain_id, release = gate_graph(service)
    try:
        running = service.get_session(sid)
        assert running["status"] == "running"
        service.control(sid, action="pause", expected_revision=running["revision"],
                        idempotency_key="no-basis-resume-pause")
    finally:
        release.set()
    service.wait(chain_id)
    paused = service.get_session(sid)
    assert paused["status"] == "paused"
    assert chain_id in service._execution_registries
    service._execution_registries.pop(chain_id)

    submissions = _watch_executor_submissions(monkeypatch, service)
    before_session = service.get_session(sid)
    before = service.get_run(sid, chain_id)
    with pytest.raises(ContractValidationError) as caught:
        service.control(sid, action="resume", expected_revision=paused["revision"],
                        idempotency_key="no-basis-resume-rejected")
    _assert_no_basis_rejection(caught)

    after = service.get_run(sid, chain_id)
    after_session = service.get_session(sid)
    _assert_run_unchanged(before, after)
    assert after["chain"]["status"] == "paused"
    assert submissions == []
    assert after_session["revision"] == before_session["revision"]
    assert after_session["active_chain_run_id"] == chain_id


def test_retry_archive_without_run_basis_is_rejected_without_replay(service, monkeypatch):
    original_commit = service._commit
    attempts = []

    def save(repo, session, *, source, **kwargs):
        original_commit(repo, session, source=source, **kwargs)
        if source["kind"] == "completed_execution":
            attempts.append(True)
            raise OSError("checkpoint transaction failed")

    monkeypatch.setattr(service, "_commit", save)
    initial = create(service, text_graph(service.registry))
    sid = initial["workflow_session_id"]
    started = service.start(sid, expected_revision=initial["revision"],
                            idempotency_key="no-basis-archive-start")
    chain_id = started["active_chain_run_id"]
    service.wait(chain_id)
    pending = service.get_session(sid)
    assert pending["status"] == "archive_failed", pending["chains"]
    assert pending["available_actions"] == ["retry_archive", "close"]
    retained = service._acceptance_candidates[chain_id]["completed_execution"]["finish_key"]
    assert chain_id in service._execution_registries
    service._execution_registries.pop(chain_id)

    submissions = _watch_executor_submissions(monkeypatch, service)
    before = service.get_run(sid, chain_id)
    with pytest.raises(ContractValidationError) as caught:
        service.control(sid, action="retry_archive", expected_revision=pending["revision"],
                        idempotency_key="no-basis-archive-rejected")
    _assert_no_basis_rejection(caught)

    after = service.get_run(sid, chain_id)
    _assert_run_unchanged(before, after)
    assert after["chain"]["status"] == "archive_failed"
    assert service._acceptance_candidates[chain_id]["completed_execution"][
        "finish_key"] == retained
    assert attempts == [True]
    assert submissions == []


def test_retry_acceptance_without_run_basis_is_rejected_with_retained_candidate(
        tmp_path, monkeypatch):
    fixture = ModelDatabaseFixture()
    with closing(GraphWorkflowService(
            tmp_path / "acceptance-no-basis.sqlite",
            public_model_factory=fixture.factory)) as service:
        fixture.write(service, 1)
        initial = fixture.create(service, fixture.document(service))
        original = service._node_event

        def fail(sid, chain, event):
            if event["node_binding_id"] == uid(13) and event["event"] == "succeeded":
                raise OSError("output store unavailable")
            return original(sid, chain, event)

        monkeypatch.setattr(service, "_node_event", fail)
        started = service.start(
            initial["workflow_session_id"], expected_revision=initial["revision"],
            idempotency_key="no-basis-acceptance-start")
        chain_id = started["active_chain_run_id"]
        service.wait(chain_id)
        pending = service.get_session(initial["workflow_session_id"])
        assert pending["status"] == "archive_failed", pending["chains"]
        assert pending["available_actions"] == ["retry_acceptance", "close"]
        with service._data_management.read_execution(registry=service.registry) as data:
            chain = data.records.chain(chain_id)
        candidate_run_id = chain["node_run_ids"][chain["next_node_index"]]
        assert candidate_run_id in service._acceptance_candidates[chain_id]
        service._execution_registries.pop(chain_id)

        submissions = _watch_executor_submissions(monkeypatch, service)
        before = service.get_run(pending["workflow_session_id"], chain_id)
        with pytest.raises(ContractValidationError) as caught:
            service.control(
                pending["workflow_session_id"], action="retry_acceptance",
                expected_revision=pending["revision"],
                idempotency_key="no-basis-acceptance-rejected")
        _assert_no_basis_rejection(caught)

        after = service.get_run(pending["workflow_session_id"], chain_id)
        _assert_run_unchanged(before, after)
        assert after["chain"]["status"] == "archive_failed"
        assert candidate_run_id in service._acceptance_candidates[chain_id]
        assert len(fixture.calls) == 1
        assert submissions == []


def test_prior_control_diagnostics_precede_the_missing_basis_rejection(service, monkeypatch):
    def fail(config, inputs, context):
        raise ValueError("controlled business failure")

    service.registry.register(NodeDefinition(
        "test.control-no-basis-failure", "1", "Failure", "Test", {},
        {"type": "object"},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),)), fail)
    failing = node(service.registry, "test.control-no-basis-failure", 2)
    output = node(service.registry, "tools.output", 3)
    output["public_outputs"] = ["output"]
    graph = document([failing, output], [edge(failing, output, 1)])
    graph["package_lock"] = list(service.registry.execution_package_lock)
    initial = create(service, graph)
    sid = initial["workflow_session_id"]
    started = service.start(sid, expected_revision=initial["revision"],
                            idempotency_key="no-basis-prior-start")
    chain_id = started["active_chain_run_id"]
    service.wait(chain_id)
    failed = service.get_session(sid)
    assert failed["status"] == "failed", failed["chains"]
    # A settled failure releases its writable basis: this is the real no-basis state.
    assert chain_id not in service._execution_registries
    assert chain_id in service._futures

    submissions = _watch_executor_submissions(monkeypatch, service)
    with pytest.raises(ContractValidationError) as stale:
        service.control(sid, action="resume", expected_revision=failed["revision"] + 1,
                        idempotency_key="no-basis-prior-stale")
    assert stale.value.reason_code == "stale_revision"
    assert stale.value.diagnostics[0]["message"] == "Session changed"
    with pytest.raises(ContractValidationError) as state:
        service.control(sid, action="resume", expected_revision=failed["revision"],
                        idempotency_key="no-basis-prior-state")
    assert state.value.reason_code == "recovery_unavailable"
    assert state.value.diagnostics[0]["message"] == NO_CONTINUATION_MESSAGE
    assert submissions == []


def test_no_basis_pause_command_is_accepted_replayed_and_leaves_no_tail(tmp_path):
    registry = _registry()
    path = tmp_path / "no-basis-pause.sqlite"
    first = GraphWorkflowService(path, registry=registry)
    executor = _RetainedExecutor()
    first._executor = executor
    try:
        initial = create(first, text_graph(registry))
        sid = initial["workflow_session_id"]
        started = first.start(sid, expected_revision=initial["revision"],
                              idempotency_key="no-basis-pause-start")
        chain_id = started["active_chain_run_id"]
        assert started["status"] == "prepared"
        assert executor.submitted == 1
    finally:
        first.close()

    with closing(GraphWorkflowService(path, registry=registry)) as reopened:
        prepared = reopened.get_session(sid)
        assert prepared["status"] == "prepared"
        assert prepared["available_actions"] == ["pause"]
        assert chain_id not in reopened._execution_registries
        request = {"action": "pause", "expected_revision": prepared["revision"],
                   "idempotency_key": "no-basis-pause-command"}
        manifest_calls = []
        original_save = reopened._save_manifest

        def save(source, kind, identity, session, **options):
            manifest_calls.append(kind)
            return original_save(source, kind, identity, session, **options)

        reopened._save_manifest = save
        accepted = reopened.control(sid, **request)
        # No writable execution data session settles the pause: only the command
        # receipt and the session revision advance, exactly as before C1.
        assert manifest_calls == []
        assert accepted["status"] == "prepared"
        assert accepted["revision"] == prepared["revision"] + 1
        assert accepted["active_chain_run_id"] == chain_id
        stored = reopened.get_run(sid, chain_id)
        assert stored["chain"]["status"] == "prepared"
        assert stored["chain"]["revision"] == 1
        assert [run["status"] for run in stored["node_runs"]] == [
            "prepared", "prepared", "prepared"]
        assert reopened.control(sid, **request) == accepted

    with closing(GraphWorkflowService(path, registry=registry)) as reread:
        assert reread.get_session(sid)["revision"] == prepared["revision"] + 1
        assert reread.get_run(sid, chain_id)["chain"]["status"] == "prepared"
        assert reread.control(sid, **request) == accepted


def test_no_basis_close_persists_on_a_paused_execution(service, monkeypatch):
    sid, chain_id, release = gate_graph(service)
    try:
        running = service.get_session(sid)
        service.control(sid, action="pause", expected_revision=running["revision"],
                        idempotency_key="no-basis-close-pause")
    finally:
        release.set()
    service.wait(chain_id)
    paused = service.get_session(sid)
    assert paused["status"] == "paused"
    assert chain_id in service._execution_registries
    start = service.get_run(sid, chain_id)["state_manifests"]["start"]
    service._execution_registries.pop(chain_id)
    boundary_calls = _watch_boundary(service, monkeypatch)
    manifest_calls = _watch_end_manifest(
        service, monkeypatch, kinds=("state_snapshot", "chain_end"))
    resource_calls = _watch_resource_identities(service, monkeypatch)

    closed = service.control(sid, action="close", expected_revision=paused["revision"],
                             idempotency_key="no-basis-close-command")
    # The released basis persists through the limited boundary port, and both
    # close manifests reuse the closed chain's prepared boundary.
    assert len(boundary_calls) == 1
    assert manifest_calls == [
        ("state_snapshot", False, ("chain_start", chain_id), None),
        ("chain_end", False, ("chain_start", chain_id), None),
    ]
    assert resource_calls == []
    assert closed["status"] == "closed"
    assert closed["active_chain_run_id"] is None
    assert service.get_session(sid)["status"] == "closed"
    stored = service.get_run(sid, chain_id)
    assert stored["chain"]["status"] == "closed"
    assert stored["chain"]["revision"] == paused["chains"][-1]["revision"] + 1
    assert all(run["status"] in ("succeeded", "closed") for run in stored["node_runs"])
    with service._data_management.read_execution(registry=service.registry) as data:
        data_revision = data.state.variables(sid)["revision"]
        head = data.records.head(sid)
        commit = data.records.commit(head["head_commit_id"])
        snapshot_manifest = data.settlement.manifest(
            "state_snapshot", commit["state_snapshot_id"])
    assert commit["source"] == {"kind": "closed_execution", "chain_run_id": chain_id}
    for manifest in (stored["state_manifests"]["end"], snapshot_manifest):
        assert manifest["package_lock"] == start["package_lock"]
        assert manifest["global_resources"] == start["global_resources"]
    assert stored["state_manifests"]["end"]["status"] == "closed"
    assert snapshot_manifest["legacy_data_revision"] == data_revision
    assert snapshot_manifest["legacy_node_states"] == closed["private_states"]


def test_no_basis_close_requires_the_closed_chain_prepared_boundary(service):
    sid, chain_id, release = gate_graph(service)
    try:
        running = service.get_session(sid)
        service.control(sid, action="pause", expected_revision=running["revision"],
                        idempotency_key="no-basis-close-missing-pause")
    finally:
        release.set()
    service.wait(chain_id)
    paused = service.get_session(sid)
    assert paused["status"] == "paused"
    service._execution_registries.pop(chain_id)
    with closing(service._store()) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        try:
            store._connection.execute(
                "DELETE FROM graph_state_manifests "
                "WHERE owner_kind='chain_start' AND owner_id=?", (chain_id,))
            store._connection.execute("COMMIT")
        except BaseException:
            if store._connection.in_transaction:
                store._connection.execute("ROLLBACK")
            raise
    before = service.get_run(sid, chain_id)
    before_session = service.get_session(sid)
    head_before = before_session["head_commit_id"]
    manifests_before = _manifest_rows(service)

    # The original CAS runs before any boundary write and fails unchanged.
    with pytest.raises(ContractValidationError) as stale:
        service.control(sid, action="close", expected_revision=paused["revision"] + 1,
                        idempotency_key="no-basis-close-missing-stale")
    assert stale.value.reason_code == "stale_revision"
    assert service.get_session(sid) == before_session

    # Missing prepared evidence fails the close explicitly and rolls everything
    # back: the chain is not reported closed and committed node results remain.
    with pytest.raises(ContractValidationError) as missing:
        service.control(sid, action="close", expected_revision=paused["revision"],
                        idempotency_key="no-basis-close-missing-command")
    assert missing.value.reason_code == "storage_contract_violation"
    assert missing.value.status_code == 500
    after = service.get_run(sid, chain_id)
    after_session = service.get_session(sid)
    assert after == before
    assert after_session == before_session
    assert after_session["head_commit_id"] == head_before
    # The paused end manifest stays the last committed boundary; no closed
    # boundary was persisted and no close receipt was written.
    assert after["state_manifests"]["end"]["status"] == "paused"
    assert _manifest_rows(service) == manifests_before
    with closing(service._store()) as store:
        assert store._connection.execute(
            "SELECT 1 FROM idempotency WHERE operation='graph.run.control' AND key=?",
            ("no-basis-close-missing-command",)).fetchone() is None


def _foreign_revision(manifest):
    manifest["definition_revision"] += 1


def _broken_package_lock(manifest):
    manifest["package_lock"] = {"not": "a list"}


def _watch_fixed_document(monkeypatch):
    calls = []
    original = ConfigurationReadSession.fixed_document

    def fixed_document(self, identity, revision=None):
        calls.append((identity, revision))
        return original(self, identity, revision)

    monkeypatch.setattr(ConfigurationReadSession, "fixed_document", fixed_document)
    return calls


TAMPERED_BOUNDARIES = [
    pytest.param(
        _foreign_session,
        "The prepared boundary for this execution belongs to another execution",
        id="foreign-session"),
    pytest.param(
        _foreign_revision,
        "The prepared boundary for this execution belongs to another execution",
        id="foreign-revision"),
    pytest.param(
        _broken_package_lock,
        "The prepared boundary for this execution has a damaged package lock",
        id="broken-package-lock"),
]


@pytest.mark.parametrize("mutate,expected_message", TAMPERED_BOUNDARIES)
def test_no_basis_close_rejects_a_tampered_prepared_boundary(
        service, mutate, expected_message, monkeypatch):
    sid, chain_id, release = gate_graph(service)
    try:
        running = service.get_session(sid)
        service.control(sid, action="pause", expected_revision=running["revision"],
                        idempotency_key="no-basis-close-tampered-pause")
    finally:
        release.set()
    service.wait(chain_id)
    paused = service.get_session(sid)
    assert paused["status"] == "paused"
    service._execution_registries.pop(chain_id)
    _rewrite_chain_start(service, chain_id, mutate)

    before_run = service.get_run(sid, chain_id)
    before_session = service.get_session(sid)
    before_manifests = _manifest_rows(service)
    fixed_document_calls = _watch_fixed_document(monkeypatch)

    # A prepared boundary that names another execution or lost its structure
    # cannot settle the released close: the command reports a storage contract
    # violation and the whole transaction rolls back.
    with pytest.raises(ContractValidationError) as rejected:
        service.control(sid, action="close", expected_revision=paused["revision"],
                        idempotency_key="no-basis-close-tampered-command")
    assert rejected.value.reason_code == "storage_contract_violation"
    assert rejected.value.status_code == 500
    assert rejected.value.diagnostics[0]["message"] == expected_message
    # The released basis finishes from its prepared evidence only; it never
    # reads the current fixed definition table.
    assert fixed_document_calls == []

    after_run = service.get_run(sid, chain_id)
    after_session = service.get_session(sid)
    # Nothing settled: the paused boundary, the head commit and the exact
    # manifest rows are unchanged and no close receipt was persisted.
    assert after_run == before_run
    assert after_run["chain"]["status"] == "paused"
    assert after_session == before_session
    assert after_session["head_commit_id"] == before_session["head_commit_id"]
    assert after_session["status"] == "paused"
    assert after_session["active_chain_run_id"] == chain_id
    assert _manifest_rows(service) == before_manifests
    with closing(service._store()) as store:
        assert store._connection.execute(
            "SELECT 1 FROM idempotency WHERE operation='graph.run.control' AND key=?",
            ("no-basis-close-tampered-command",)).fetchone() is None


def test_no_basis_close_rejects_an_unreadable_prepared_boundary(service):
    sid, chain_id, release = gate_graph(service)
    try:
        running = service.get_session(sid)
        service.control(sid, action="pause", expected_revision=running["revision"],
                        idempotency_key="no-basis-close-unreadable-pause")
    finally:
        release.set()
    service.wait(chain_id)
    paused = service.get_session(sid)
    assert paused["status"] == "paused"
    service._execution_registries.pop(chain_id)
    before_chain = _record_payload(service, "chain_run", chain_id)
    before_session = _record_payload(service, "workflow_session", sid)
    _write_chain_start_payload(service, chain_id, "{not valid json")
    before_manifests = _manifest_rows(service)

    # A stored boundary whose JSON cannot be decoded is unreadable evidence.  The
    # read boundary converts the decode error into the documented storage
    # contract failure instead of letting a plain validation error escape as 400.
    with pytest.raises(ContractValidationError) as rejected:
        service.control(sid, action="close", expected_revision=paused["revision"],
                        idempotency_key="no-basis-close-unreadable-command")
    assert rejected.value.reason_code == "storage_contract_violation"
    assert rejected.value.status_code == 500
    assert rejected.value.diagnostics[0]["message"] == (
        "The prepared boundary for this execution is unreadable")

    # Stored records are compared directly because the public run view decodes
    # this same unreadable evidence; the rollback must leave every row intact.
    assert _record_payload(service, "chain_run", chain_id) == before_chain
    stored_chain = json.loads(before_chain)
    assert stored_chain["status"] == "paused"
    assert _record_payload(service, "workflow_session", sid) == before_session
    stored_session = json.loads(before_session)
    assert stored_session["active_chain_run_id"] == chain_id
    assert _manifest_rows(service) == before_manifests
    assert all(json.loads(row[2]).get("status") != "closed"
               for row in before_manifests if row[0] == "chain_boundary")
    with closing(service._store()) as store:
        assert store._connection.execute(
            "SELECT 1 FROM idempotency WHERE operation='graph.run.control' AND key=?",
            ("no-basis-close-unreadable-command",)).fetchone() is None
