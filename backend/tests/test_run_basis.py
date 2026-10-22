"""One invocation must consume the load result captured for it, not current tables."""

from contextlib import closing
import inspect
import json
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.run_basis import RunBasis
from phase1_agent.workflow_definitions import WorkflowDefinitionCatalog

from test_definition_cross_layer import make_service


def _watch_boundary(service, monkeypatch):
    calls = []
    original = service._data_management.execution_boundary_transaction

    def boundary(records):
        calls.append(records)
        return original(records)

    monkeypatch.setattr(service._data_management, "execution_boundary_transaction", boundary)
    return calls


def _watch_end_manifest(service, monkeypatch, kinds=("chain_end",)):
    calls = []
    original = service._save_manifest

    def save(source, kind, identity, session, **options):
        if kind in kinds:
            calls.append((kind, hasattr(source, "registry"),
                          options.get("fallback_manifest"), options.get("registry")))
        return original(source, kind, identity, session, **options)

    monkeypatch.setattr(service, "_save_manifest", save)
    return calls


def _watch_resource_identities(service, monkeypatch):
    calls = []
    original = service._resource_identities

    def identities(document, *, registry=None):
        calls.append(registry)
        return original(document, registry=registry)

    monkeypatch.setattr(service, "_resource_identities", identities)
    return calls


def _rewrite_chain_start(service, chain_id, mutate):
    """Rewrite this chain's prepared boundary payload through the real table."""
    with closing(service._store()) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        try:
            row = store._connection.execute(
                "SELECT payload FROM graph_state_manifests "
                "WHERE owner_kind='chain_start' AND owner_id=?", (chain_id,)).fetchone()
            assert row is not None
            manifest = loads_strict(row["payload"])
            mutate(manifest)
            store._connection.execute(
                "UPDATE graph_state_manifests SET payload=? "
                "WHERE owner_kind='chain_start' AND owner_id=?",
                (canonical_bytes(manifest).decode("utf-8"), chain_id))
            store._connection.execute("COMMIT")
        except BaseException:
            if store._connection.in_transaction:
                store._connection.execute("ROLLBACK")
            raise


def _record_payload(service, record_type, record_id):
    """Read one stored record payload without decoding manifests."""
    with closing(service._store()) as store:
        row = store._connection.execute(
            "SELECT payload FROM records WHERE record_type=? AND record_id=?",
            (record_type, record_id)).fetchone()
    assert row is not None
    return row["payload"]


def _write_chain_start_payload(service, chain_id, payload):
    """Replace this chain's prepared boundary payload with raw stored text."""
    with closing(service._store()) as store:
        store._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = store._connection.execute(
                "UPDATE graph_state_manifests SET payload=? "
                "WHERE owner_kind='chain_start' AND owner_id=?", (payload, chain_id))
            assert cursor.rowcount == 1
            store._connection.execute("COMMIT")
        except BaseException:
            if store._connection.in_transaction:
                store._connection.execute("ROLLBACK")
            raise


def _foreign_session(manifest):
    manifest["workflow_session_id"] = str(uuid4())


def _manifest_rows(service):
    with closing(service._store()) as store:
        return [tuple(row) for row in store._connection.execute(
            "SELECT owner_kind,owner_id,payload FROM graph_state_manifests "
            "ORDER BY owner_kind,owner_id")]


def _watch_direct_repo_access(monkeypatch):
    """Record repo.get/put issued directly from the exception-close callback."""
    direct = []
    original_get, original_put = GraphRecordStore.get, GraphRecordStore.put

    def get(self, kind, **identity):
        if inspect.currentframe().f_back.f_code.co_name == "fail":
            direct.append(("get", kind))
        return original_get(self, kind, **identity)

    def put(self, kind, value):
        if inspect.currentframe().f_back.f_code.co_name == "fail":
            direct.append(("put", kind))
        return original_put(self, kind, value)

    monkeypatch.setattr(GraphRecordStore, "get", get)
    monkeypatch.setattr(GraphRecordStore, "put", put)
    return direct


def runnable_document(service, identity):
    source, output = str(uuid4()), str(uuid4())
    return {
        "schema_version": 3,
        "workflow_definition_id": identity,
        "revision": 1,
        "name": "Run basis workflow",
        "nodes": [
            {
                "node_binding_id": source,
                "component_id": "sample.source",
                "component_version": "1",
                "title": "Source",
                "position": {"x": 0, "y": 0},
                "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
                "config": {"text": "ready"},
            },
            {
                "node_binding_id": output,
                "component_id": "sample.uppercase",
                "component_version": "1",
                "title": "Upper",
                "position": {"x": 200, "y": 0},
                "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
                "config": {},
            },
        ],
        "edges": [{
            "edge_id": str(uuid4()),
            "source_node_id": source,
            "source_port_id": "output",
            "target_node_id": output,
            "target_port_id": "input",
            "order": 0,
        }],
        "object_bindings": [],
        "package_lock": [dict(item) for item in service.registry.package_lock],
    }


def test_captured_basis_keeps_its_registry_and_same_source_query(tmp_path):
    service = make_service(tmp_path / "basis-value.sqlite")
    try:
        basis = service._capture_run_basis()
        assert basis.registry is not service.registry
        assert basis.registry._frozen is True
        assert basis.definitions.registry is basis.registry
        view = basis.definitions.get("node", {
            "component_id": "sample.uppercase", "component_version": "1"})
        assert view["source"] == [
            {"package_id": "sample.definition-node", "package_version": "1"}]
        assert service.definition_query.registry is service.registry

        with pytest.raises(ValueError):
            RunBasis(basis.registry, WorkflowDefinitionCatalog(service.registry, ()))
    finally:
        service.close()


def test_missing_basis_is_reported_instead_of_reselecting_current_tables(tmp_path):
    service = make_service(tmp_path / "basis-missing.sqlite")
    try:
        with pytest.raises(ContractValidationError) as caught:
            service._require_run_basis(str(uuid4()))
        assert caught.value.reason_code == "run_basis_unavailable"
    finally:
        service.close()


def test_prepare_uses_captured_basis_after_current_package_lock_is_emptied(tmp_path):
    service = make_service(tmp_path / "basis-prepare.sqlite")
    try:
        identity = str(uuid4())
        document = runnable_document(service, identity)
        service.update_definition(
            document, workflow_definition_id=identity,
            expected_edit_sequence=0, idempotency_key=str(uuid4()))
        session = service.create_session(identity, 1, idempotency_key=str(uuid4()))
        sid = session["workflow_session_id"]
        basis = service._capture_run_basis()

        # Inject a current-table change after capture.  The fixed document still
        # requires the packages captured in the basis; only the basis can satisfy it.
        service.registry.package_lock = ()

        response = service._change(
            "graph.run.start", "basis-captured",
            {"session": sid, "expected_revision": session["revision"], "inputs": {}},
            lambda repo: service._prepare_run(
                service._data_management.execution_transaction(repo, basis=basis),
                sid, session["revision"], {}, basis=basis))
        chain_id = response["active_chain_run_id"]
        assert service._execution_registries[chain_id] is basis

        second = service.create_session(identity, 1, idempotency_key=str(uuid4()))
        current_basis = service._capture_run_basis()
        with pytest.raises(ContractValidationError) as caught:
            service._change(
                "graph.run.start", "basis-current",
                {"session": second["workflow_session_id"],
                 "expected_revision": second["revision"], "inputs": {}},
                lambda repo: service._prepare_run(
                    service._data_management.execution_transaction(
                        repo, basis=current_basis),
                    second["workflow_session_id"], second["revision"], {}, basis=current_basis))
        assert caught.value.reason_code == "graph_missing_package"
    finally:
        service.close()


def test_real_start_captures_once_and_executes_the_same_basis(tmp_path, monkeypatch):
    service = make_service(tmp_path / "basis-start.sqlite")
    try:
        identity = str(uuid4())
        document = runnable_document(service, identity)
        service.update_definition(
            document, workflow_definition_id=identity,
            expected_edit_sequence=0, idempotency_key=str(uuid4()))
        session = service.create_session(identity, 1, idempotency_key=str(uuid4()))
        sid = session["workflow_session_id"]

        captured, executed = [], []
        original_capture = service._capture_run_basis
        original_execute = service._execute

        def capture():
            basis = original_capture()
            captured.append(basis)
            return basis

        def execute(execution_sid, chain_id, steps=None):
            executed.append(service._execution_registries[chain_id])
            return original_execute(execution_sid, chain_id, steps)

        monkeypatch.setattr(service, "_capture_run_basis", capture)
        monkeypatch.setattr(service, "_execute", execute)
        started = service.start(
            sid, expected_revision=session["revision"], idempotency_key=str(uuid4()))
        service.wait(started["active_chain_run_id"])

        assert len(captured) == 1
        assert executed == [captured[0]]
        assert service.get_session(sid)["status"] == "succeeded"
    finally:
        service.close()


def test_execute_without_captured_basis_fails_closed_with_explicit_diagnostic(tmp_path, monkeypatch):
    service = make_service(tmp_path / "basis-execute.sqlite")
    try:
        identity = str(uuid4())
        document = runnable_document(service, identity)
        service.update_definition(
            document, workflow_definition_id=identity,
            expected_edit_sequence=0, idempotency_key=str(uuid4()))
        session = service.create_session(identity, 1, idempotency_key=str(uuid4()))
        sid = session["workflow_session_id"]
        basis = service._capture_run_basis()
        response = service._change(
            "graph.run.start", "basis-execute",
            {"session": sid, "expected_revision": session["revision"], "inputs": {}},
            lambda repo: service._prepare_run(
                service._data_management.execution_transaction(repo, basis=basis),
                sid, session["revision"], {}, basis=basis))
        chain_id = response["active_chain_run_id"]
        start = service.get_run(sid, chain_id)["state_manifests"]["start"]

        # Simulate loss of the captured basis.  Execution must not rebuild it
        # from service tables or continue with the current registry.
        service._execution_registries.pop(chain_id)
        boundary_calls = _watch_boundary(service, monkeypatch)
        end_manifests = _watch_end_manifest(service, monkeypatch)
        direct = _watch_direct_repo_access(monkeypatch)
        resource_calls = []
        original_identities = service._resource_identities

        def identities(document, *, registry=None):
            resource_calls.append(registry)
            return original_identities(document, registry=registry)

        monkeypatch.setattr(service, "_resource_identities", identities)
        service._execute(sid, chain_id)

        chain = service.get_run(sid, chain_id)["chain"]
        assert chain["status"] == "recovery_unavailable"
        assert chain["diagnostic"]["reason_code"] == "run_basis_unavailable"
        assert chain_id not in service._execution_registries
        # Scenario B (basis and local registry both missing) stays on the
        # same-chain prepared boundary through the limited boundary port.
        assert len(boundary_calls) == 1
        assert end_manifests == [("chain_end", False, ("chain_start", chain_id), None)]
        assert direct == []
        # The failed boundary must reuse the prepared run basis instead of
        # recomputing resource dependencies from the current registry.
        assert resource_calls == []
        end = service.get_run(sid, chain_id)["state_manifests"]["end"]
        assert end["status"] == "recovery_unavailable"
        assert end["package_lock"] == start["package_lock"]
        assert end["global_resources"] == start["global_resources"]
        with pytest.raises(ContractValidationError) as caught:
            service.wait(chain_id)
        assert caught.value.reason_code == "run_basis_unavailable"
    finally:
        service.close()


def test_retained_basis_end_manifest_recomputes_from_its_own_registry(tmp_path, monkeypatch):
    service = make_service(tmp_path / "basis-retained-end.sqlite")
    try:
        identity = str(uuid4())
        document = runnable_document(service, identity)
        service.update_definition(
            document, workflow_definition_id=identity,
            expected_edit_sequence=0, idempotency_key=str(uuid4()))
        session = service.create_session(identity, 1, idempotency_key=str(uuid4()))
        sid = session["workflow_session_id"]
        captured = []
        original_capture = service._capture_run_basis

        def capture():
            basis = original_capture()
            captured.append(basis)
            return basis

        monkeypatch.setattr(service, "_capture_run_basis", capture)
        original_commit = service._commit

        def fail_after_commit(target, session_value, *, source, **options):
            outcome = original_commit(target, session_value, source=source, **options)
            if source["kind"] == "completed_execution":
                raise OSError("injected checkpoint transaction failure")
            return outcome

        monkeypatch.setattr(service, "_commit", fail_after_commit)
        end_manifests = _watch_end_manifest(service, monkeypatch)
        resource_calls = _watch_resource_identities(service, monkeypatch)
        started = service.start(sid, expected_revision=session["revision"],
                                idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        resource_calls.clear()
        end_manifests.clear()
        # The exact retained result is kept for retry, so wait() returns after
        # recording the pending checkpoint boundary instead of raising.
        service.wait(chain_id)

        run = service.get_run(sid, chain_id)
        assert run["chain"]["status"] == "archive_failed"
        assert run["chain"]["diagnostic"]["reason_code"] == "checkpoint_commit_pending"
        # Scenario A keeps the retained basis: the end manifest is recomputed
        # from the same basis registry, never from current service tables.
        assert end_manifests == [("chain_end", True, None, None)]
        assert resource_calls
        assert all(registry is captured[0].registry for registry in resource_calls)
        fixed = service.get_definition(identity)
        end = run["state_manifests"]["end"]
        assert end["package_lock"] == fixed["package_lock"]
        assert end["status"] == "archive_failed"
    finally:
        service.close()


def test_lost_basis_end_manifest_reuses_the_same_chain_start_evidence(tmp_path, monkeypatch):
    service = make_service(tmp_path / "basis-lost-end.sqlite")
    try:
        identity = str(uuid4())
        document = runnable_document(service, identity)
        service.update_definition(
            document, workflow_definition_id=identity,
            expected_edit_sequence=0, idempotency_key=str(uuid4()))
        session = service.create_session(identity, 1, idempotency_key=str(uuid4()))
        sid = session["workflow_session_id"]
        boundary_calls = _watch_boundary(service, monkeypatch)
        end_manifests = _watch_end_manifest(service, monkeypatch)
        resource_calls = _watch_resource_identities(service, monkeypatch)
        original_node_event = service._node_event
        lost = []

        def lose_basis(execution_sid, chain_value, event):
            if not lost:
                lost.append(service._execution_registries.pop(chain_value))
                raise OSError("injected failure after basis loss")
            return original_node_event(execution_sid, chain_value, event)

        monkeypatch.setattr(service, "_node_event", lose_basis)
        started = service.start(sid, expected_revision=session["revision"],
                                idempotency_key=str(uuid4()))
        chain_id = started["active_chain_run_id"]
        resource_calls.clear()
        with pytest.raises(OSError, match="injected failure after basis loss"):
            service.wait(chain_id)

        run = service.get_run(sid, chain_id)
        assert run["chain"]["status"] == "recovery_unavailable"
        assert run["chain"]["diagnostic"]["reason_code"] == "execution_result_unknown"
        # Scenario C now reuses the same-chain prepared boundary instead of
        # recomputing from the retained invocation registry.
        assert resource_calls == []
        assert len(boundary_calls) == 1
        assert end_manifests == [("chain_end", False, ("chain_start", chain_id), None)]
        start, end = run["state_manifests"]["start"], run["state_manifests"]["end"]
        assert end["package_lock"] == start["package_lock"]
        assert end["global_resources"] == start["global_resources"]
        assert end["status"] == "recovery_unavailable"
    finally:
        service.close()


def test_missing_chain_start_boundary_fails_without_partial_close(tmp_path, monkeypatch):
    service = make_service(tmp_path / "basis-boundary-missing.sqlite")
    try:
        identity = str(uuid4())
        document = runnable_document(service, identity)
        service.update_definition(
            document, workflow_definition_id=identity,
            expected_edit_sequence=0, idempotency_key=str(uuid4()))
        session = service.create_session(identity, 1, idempotency_key=str(uuid4()))
        sid = session["workflow_session_id"]
        basis = service._capture_run_basis()
        response = service._change(
            "graph.run.start", "basis-boundary-missing",
            {"session": sid, "expected_revision": session["revision"], "inputs": {}},
            lambda repo: service._prepare_run(
                service._data_management.execution_transaction(repo, basis=basis),
                sid, session["revision"], {}, basis=basis))
        chain_id = response["active_chain_run_id"]
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

        service._execute(sid, chain_id)

        run = service.get_run(sid, chain_id)
        # The prepared boundary is the only allowed evidence source: its absence
        # fails explicitly and rolls the close back instead of borrowing the
        # current registry or pretending the boundary was persisted.
        assert run["chain"]["status"] == "prepared"
        assert run["chain"]["revision"] == 1
        assert run["state_manifests"]["end"] is None
        assert service.get_session(sid)["active_chain_run_id"] == chain_id
        with closing(service._store()) as store:
            assert store._connection.execute(
                "SELECT 1 FROM idempotency WHERE operation='graph.run.unconfirmed'").fetchone() is None
        with pytest.raises(ContractValidationError) as caught:
            service.wait(chain_id)
        assert caught.value.reason_code == "run_basis_unavailable"
    finally:
        service.close()


def test_foreign_chain_start_boundary_fails_without_partial_close(tmp_path):
    service = make_service(tmp_path / "basis-boundary-foreign.sqlite")
    try:
        identity = str(uuid4())
        document = runnable_document(service, identity)
        service.update_definition(
            document, workflow_definition_id=identity,
            expected_edit_sequence=0, idempotency_key=str(uuid4()))
        session = service.create_session(identity, 1, idempotency_key=str(uuid4()))
        sid = session["workflow_session_id"]
        basis = service._capture_run_basis()
        response = service._change(
            "graph.run.start", "basis-boundary-foreign",
            {"session": sid, "expected_revision": session["revision"], "inputs": {}},
            lambda repo: service._prepare_run(
                service._data_management.execution_transaction(repo, basis=basis),
                sid, session["revision"], {}, basis=basis))
        chain_id = response["active_chain_run_id"]
        service._execution_registries.pop(chain_id)
        _rewrite_chain_start(service, chain_id, _foreign_session)
        before = service.get_run(sid, chain_id)
        manifests_before = _manifest_rows(service)

        service._execute(sid, chain_id)

        run = service.get_run(sid, chain_id)
        # The prepared boundary must identify this exact execution.  A boundary
        # that names another session cannot settle the unconfirmed close, and
        # the failed boundary write rolls back completely.
        assert run["chain"]["status"] == "prepared"
        assert run["chain"]["revision"] == 1
        assert run["state_manifests"]["end"] is None
        assert run["chain"] == before["chain"]
        assert _manifest_rows(service) == manifests_before
        assert service.get_session(sid)["active_chain_run_id"] == chain_id
        with closing(service._store()) as store:
            assert store._connection.execute(
                "SELECT 1 FROM idempotency WHERE operation='graph.run.unconfirmed'").fetchone() is None
        with pytest.raises(ContractValidationError) as caught:
            service.wait(chain_id)
        assert caught.value.reason_code == "run_basis_unavailable"
    finally:
        service.close()


def test_unreadable_chain_start_boundary_rolls_back_unconfirmed_close(tmp_path):
    service = make_service(tmp_path / "basis-boundary-unreadable.sqlite")
    try:
        identity = str(uuid4())
        document = runnable_document(service, identity)
        service.update_definition(
            document, workflow_definition_id=identity,
            expected_edit_sequence=0, idempotency_key=str(uuid4()))
        session = service.create_session(identity, 1, idempotency_key=str(uuid4()))
        sid = session["workflow_session_id"]
        basis = service._capture_run_basis()
        response = service._change(
            "graph.run.start", "basis-boundary-unreadable",
            {"session": sid, "expected_revision": session["revision"], "inputs": {}},
            lambda repo: service._prepare_run(
                service._data_management.execution_transaction(repo, basis=basis),
                sid, session["revision"], {}, basis=basis))
        chain_id = response["active_chain_run_id"]
        service._execution_registries.pop(chain_id)
        before_chain = _record_payload(service, "chain_run", chain_id)
        before_session = _record_payload(service, "workflow_session", sid)
        _write_chain_start_payload(service, chain_id, "{not valid json")
        manifests_before = _manifest_rows(service)

        service._execute(sid, chain_id)

        # A prepared boundary whose stored JSON cannot be decoded is unreadable
        # evidence: the unconfirmed close must roll back rather than commit or
        # leak a 400-class caller error.  Stored records are compared directly
        # because the public run view intentionally decodes this same evidence.
        assert _record_payload(service, "chain_run", chain_id) == before_chain
        stored_chain = json.loads(before_chain)
        assert stored_chain["status"] == "prepared"
        assert stored_chain["revision"] == 1
        assert _record_payload(service, "workflow_session", sid) == before_session
        assert json.loads(before_session)["active_chain_run_id"] == chain_id
        assert _manifest_rows(service) == manifests_before
        assert all(json.loads(row[2]).get("status") != "closed"
                   for row in manifests_before if row[0] == "chain_boundary")
        with closing(service._store()) as store:
            assert store._connection.execute(
                "SELECT 1 FROM idempotency WHERE operation='graph.run.unconfirmed'").fetchone() is None
        with pytest.raises(ContractValidationError) as caught:
            service.wait(chain_id)
        assert caught.value.reason_code == "run_basis_unavailable"
    finally:
        service.close()
