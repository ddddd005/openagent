"""One invocation must consume the load result captured for it, not current tables."""

from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.run_basis import RunBasis
from phase1_agent.workflow_definitions import WorkflowDefinitionCatalog

from test_definition_cross_layer import make_service


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

        def execute(execution_sid, chain_id):
            executed.append(service._execution_registries[chain_id])
            return original_execute(execution_sid, chain_id)

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
