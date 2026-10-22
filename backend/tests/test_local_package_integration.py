"""Disk packages through normal Host acquisition and formal workflow commands."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.builtin_packages import DEFAULT_PACKAGES
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_node_channel import FrontendNodeChannel
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_package_selection import GraphPackageSelectionStore
from phase1_agent.prompt_lifecycle_nodes import lifecycle_prompt_resource_definition
from phase1_agent.workflow_host import WorkflowHost

from test_content_configuration import prompt_record, provider_record, reference
from test_graph_package_selection import configuration_document, saved_selection
from test_graph_service import edge, node
from test_frontend_node_channel import prepared as prepared_frontend, request as frontend_request
from test_frontend_node_evidence import snapshot as frontend_storage_snapshot


PACKAGE_ID = "integration.local-nodes"
COMPONENT_ID = "integration.local-uppercase"
PACKAGE_VERSION = "1"


def disk_package(root, *, package_id=PACKAGE_ID, component_id=COMPONENT_ID,
                 extra_dependencies=(), declared_component=None):
    directory = root / package_id
    module = directory / "local_nodes"
    module.mkdir(parents=True)
    manifest = {
        "schema_version": 1, "package_id": package_id, "version": PACKAGE_VERSION,
        "host_protocol_version": 1,
        "dependencies": [
            {"package_id": "workflow.content", "version": "1.0.0"}, *extra_dependencies],
        "exports": {"nodes": [{
            "component_id": declared_component or component_id, "component_version": "1"}]},
    }
    (directory / "openagent-package.json").write_text(json.dumps({
        "schema_version": 1, "entrypoint": "local_nodes.package:create_package",
        "manifest": manifest,
    }), encoding="utf-8")
    (module / "__init__.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('imported.marker').write_text('imported', encoding='utf-8')\n",
        encoding="utf-8")
    (module / "operation.py").write_text(
        "def transform(value, prefix):\n"
        "    return prefix + value.upper()\n", encoding="utf-8")
    (module / "package.py").write_text(
        "from phase1_agent.capability_packages import CapabilityPackage, PackageManifest\n"
        "from phase1_agent.content_contracts import text_content\n"
        "from phase1_agent.graph_contracts import NodeDefinition, NodePort\n"
        "from .operation import transform\n"
        f"MANIFEST = {manifest!r}\n"
        "def execute(config, inputs, context):\n"
        "    return {'output': text_content(transform(inputs['input']['text'], config['prefix']))}\n"
        "def register(host):\n"
        "    host.register_node(NodeDefinition(\n"
        f"        {component_id!r}, '1', 'Local uppercase', 'Integration', {{'prefix': 'disk:'}},\n"
        "        {'type': 'object', 'properties': {'prefix': {'type': 'string'}},\n"
        "         'required': ['prefix'], 'additionalProperties': False},\n"
        "        inputs=(NodePort('input', 'TEXT', data_schema_version=2),),\n"
        "        outputs=(NodePort('output', 'TEXT', data_schema_version=2),)), execute)\n"
        "def create_package():\n"
        "    return CapabilityPackage(PackageManifest.from_dict(MANIFEST), register)\n",
        encoding="utf-8")
    return module / "imported.marker"


def selection():
    return {**DEFAULT_PACKAGES, PACKAGE_ID: PACKAGE_VERSION}


def save_configuration(application, document, key="save-local-configuration"):
    request = {
        "workflow_definition_id": document["workflow_definition_id"],
        "document": document, "expected_edit_sequence": 0, "idempotency_key": key,
    }
    result = application.command("definition.update", request)
    assert result["result"]["outcome"] == "saved"
    return request, result


def plugin_configuration(service):
    document = configuration_document(service)
    source, output = document["nodes"]
    source["config"]["text"] = "local source"
    plugin = node(service.registry, COMPONENT_ID, 30, component_version="1", prefix="saved:")
    plugin["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    document["nodes"] = [source, plugin, output]
    document["edges"] = [edge(source, plugin, 30), edge(plugin, output, 31)]
    document["package_lock"] = deepcopy(list(service.registry.execution_package_lock))
    return document


def create_and_run(application, service, document, revision):
    initial = application.command("session.create", {
        "workflow_definition_id": document["workflow_definition_id"],
        "definition_revision": revision, "idempotency_key": "create-local-session",
    })["result"]
    started = application.command("run.start", {
        "session_id": initial["workflow_session_id"], "expected_revision": initial["revision"],
        "idempotency_key": "run-local-session",
    })["result"]
    chain_id = started["active_chain_run_id"]
    service.wait(chain_id)
    run = application.query("run.read", {
        "session_id": initial["workflow_session_id"], "chain_id": chain_id,
    })
    assert run["chain"]["status"] == "succeeded"
    output = service.read_public_output(
        initial["workflow_session_id"],
        workflow_definition_id=document["workflow_definition_id"],
        definition_revision=revision, node_id=document["nodes"][-1]["node_binding_id"],
        port_id="output")
    assert output["output"]["payload"]["text"] == "saved:LOCAL SOURCE"
    return initial["workflow_session_id"], chain_id, run, output


@pytest.mark.parametrize("custom_root,management_first", [(False, True), (True, False)],
                         ids=["default-root-management-first", "custom-root-execution-first"])
def test_disk_package_formal_configuration_execution_and_reopen(tmp_path, custom_root, management_first):
    database = tmp_path / "workflow.sqlite"
    root = tmp_path / "custom-packages" if custom_root else tmp_path / "packages" / "workflow"
    marker = disk_package(root)
    unused_marker = disk_package(
        root, package_id="integration.unselected", component_id="integration.unselected-node")
    options = {"workflow_package_root": root} if custom_root else {}
    with closing(WorkflowHost(database, **options)) as host:
        first = host.graph_management_service if management_first else host.graph_service
        initial_platform = first.platform_capabilities()
        sources = initial_platform["local_package_sources"]
        assert sources["root"] == str(root.resolve())
        assert len(sources["packages"]) == 2 and sources["diagnostics"] == []
        assert not marker.exists() and not unused_marker.exists()
        assert first.registry.get(COMPONENT_ID, "1") is None
        application = GraphApplication(host)
        application.command("packages.configure", {"enabled_packages": selection()})
        service = host.graph_service
        assert marker.exists() and not unused_marker.exists()
        assert host.graph_management_service is service
        catalog = application.query("catalog.node-types", {"protocol_version": 2})["node_types"]
        entry = next(row for row in catalog if row["component_id"] == COMPONENT_ID)
        assert entry["executable"] is True
        assert entry["config_schema"]["properties"]["prefix"] == {"type": "string"}
        assert entry["default_config"] == {"prefix": "disk:"}
        document = plugin_configuration(service)
        request, accepted = save_configuration(application, document)
        revision = accepted["result"]["definition_revision"]
        reread = application.query("definition.read", {"identity": document["workflow_definition_id"]})
        assert next(row for row in reread["nodes"] if row["component_id"] == COMPONENT_ID)["config"] == {
            "prefix": "saved:"}
        sid, chain_id, historical, output = create_and_run(application, service, document, revision)
        assert json.loads(saved_selection(database)) == selection()
    with closing(WorkflowHost(database, **options)) as reopened:
        application = GraphApplication(reopened)
        assert application.query("definition.read", {"identity": document["workflow_definition_id"]}) == reread
        assert application.query("run.read", {"session_id": sid, "chain_id": chain_id}) == historical
        assert reopened.graph_management_service.read_public_output(
            sid, workflow_definition_id=document["workflow_definition_id"], definition_revision=revision,
            node_id=document["nodes"][-1]["node_binding_id"], port_id="output") == output
        receipt = application.query("receipt.read", {"operation": "definition.update", "parameters": request})
        assert receipt["outcome"] == "matched" and receipt["result"] == accepted["result"]
        assert json.loads(saved_selection(database)) == selection()
        assert not unused_marker.exists()


def loader_history(service):
    return deepcopy({key: value for key, value in vars(service._package_loader).items()
                     if key.endswith("_contracts") or key == "_node_contract_scopes"})


def test_selection_database_failure_keeps_published_state_and_loader_history(tmp_path, monkeypatch):
    database = tmp_path / "rollback.sqlite"
    disk_package(tmp_path / "packages" / "workflow")
    with closing(WorkflowHost(database)) as host:
        service = host.graph_service
        application = GraphApplication(host)
        published = deepcopy(service.platform_capabilities())
        history = loader_history(service)
        persisted = saved_selection(database)

        def reject_write(store, enabled):
            raise RuntimeError("integration selection transaction rejected")

        with monkeypatch.context() as patch:
            patch.setattr(GraphPackageSelectionStore, "write_current", reject_write)
            with pytest.raises(RuntimeError, match="integration selection transaction rejected"):
                application.command("packages.configure", {"enabled_packages": selection()})
        assert saved_selection(database) == persisted
        assert service.platform_capabilities() == published
        assert loader_history(service) == history
        application.command("packages.configure", {"enabled_packages": selection()})
        assert service.registry.get(COMPONENT_ID, "1") is not None
        assert json.loads(saved_selection(database)) == selection()


def test_committed_publication_failure_blocks_workflow_but_keeps_diagnostics_and_independent_domains(
        tmp_path, monkeypatch):
    database = tmp_path / "publication.sqlite"
    disk_package(tmp_path / "packages" / "workflow")
    with closing(WorkflowHost(database)) as host:
        service = host.graph_service
        application = GraphApplication(host)
        document = configuration_document(service)
        original, accepted = save_configuration(application, document, key="original-before-publication")
        initial = application.command("session.create", {
            "workflow_definition_id": document["workflow_definition_id"],
            "definition_revision": accepted["result"]["definition_revision"],
            "idempotency_key": "session-before-publication",
        })["result"]
        sid = initial["workflow_session_id"]
        evidence_request = {
            "operation": "definition.update", "idempotency_key": original["idempotency_key"],
            "owner_kind": "integration", "owner_id": "original-definition", "request": original,
        }
        application.command("request-evidence.write", evidence_request)
        evidence = application.query("request-evidence.read", {
            "owner_kind": "integration", "owner_id": "original-definition"})
        old_lock = deepcopy(service.registry.package_lock)

        def reject_publication(candidate):
            raise RuntimeError("integration publication rejected")

        with monkeypatch.context() as patch:
            patch.setattr(service, "_publish_capabilities", reject_publication)
            with pytest.raises(ContractValidationError) as failed:
                application.command("packages.configure", {"enabled_packages": selection()})
        assert failed.value.reason_code == "package_publication_failed"
        diagnostic = failed.value.diagnostics[0]
        assert diagnostic["selection_committed"] is True
        assert diagnostic["publication_completed"] is False
        assert json.loads(saved_selection(database)) == selection()
        platform = application.query("platform")
        assert platform["package_publication"]["selection_committed"] is True
        assert platform["package_publication"]["publication_completed"] is False
        assert platform["package_lock"] == list(old_lock)
        for operation, parameters in (
            ("definition.list", {}),
            ("definition.read", {"identity": document["workflow_definition_id"]}),
            ("catalog.node-types", {"protocol_version": 2}),
            ("session.read", {"session_id": sid}),
            ("checkpoint.list", {"session_id": sid}),
        ):
            with pytest.raises(ContractValidationError) as denied:
                application.query(operation, parameters)
            assert denied.value.reason_code == "package_publication_failed"
        for operation, parameters in (
            ("definition.update", {**original, "idempotency_key": "blocked-definition"}),
            ("session.create", {
                "workflow_definition_id": document["workflow_definition_id"],
                "definition_revision": accepted["result"]["definition_revision"],
                "idempotency_key": "blocked-session"}),
            ("packages.configure", {"enabled_packages": DEFAULT_PACKAGES}),
            ("request-evidence.write", {
                **evidence_request, "idempotency_key": "blocked-evidence",
                "owner_id": "blocked-workflow-evidence",
                "request": {**original, "idempotency_key": "blocked-evidence"}}),
            ("request-evidence.resolve", {
                "operation": "definition.update",
                "idempotency_key": original["idempotency_key"], "request_digest": "0" * 64,
                "resolution": "verified"}),
        ):
            with pytest.raises(ContractValidationError) as denied:
                application.command(operation, parameters)
            assert denied.value.reason_code == "package_publication_failed"
        for callback in (
            lambda: service.node_types(protocol_version=2),
            lambda: service.get_definition(document["workflow_definition_id"]),
            lambda: service.list_definitions(),
            lambda: service.get_session(sid),
            lambda: service.list_sessions(document["workflow_definition_id"]),
            lambda: service.get_run(sid, str(uuid4())),
            lambda: service.list_checkpoints(sid),
            lambda: service.read_checkpoint(sid, checkpoint_id=str(uuid4())),
            lambda: service.archive_session(
                sid, expected_revision=initial["revision"], idempotency_key="blocked-direct-archive"),
            lambda: service.start(
                sid, expected_revision=initial["revision"], idempotency_key="blocked-direct-start"),
        ):
            with pytest.raises(ContractValidationError) as denied:
                callback()
            assert denied.value.reason_code == "package_publication_failed"
        assert not service._management_sessions and not service._futures
        receipt = application.query("receipt.read", {"operation": "definition.update", "parameters": original})
        assert receipt["outcome"] == "matched" and receipt["result"] == accepted["result"]
        assert application.query("request-evidence.read", {
            "owner_kind": "integration", "owner_id": "original-definition"}) == evidence
        assert isinstance(application.query("credential.protection"), dict)
        record = prompt_record()
        application.command("resource.save", {
            "record": record, "expected_sequence": 0, "idempotency_key": str(uuid4())})
        assert application.query("resource.read", {"identity": reference(record)}) == record
        assert record in application.query("resource.list")
        metadata = application.query("content.metadata.read", {"reference": reference(record)})
        assert metadata["reference"] == reference(record)
        changed_metadata = application.command("content.metadata.update", {
            "reference": reference(record), "display_name": "Publication-independent content",
            "expected_metadata_sequence": metadata["metadata_sequence"], "idempotency_key": str(uuid4()),
        })["result"]
        assert changed_metadata["display_name"] == "Publication-independent content"
        provider = provider_record()
        application.command("resource.save", {
            "record": provider, "expected_sequence": 0, "idempotency_key": str(uuid4())})
        assert application.query("resource.read", {"identity": reference(provider)}) == provider
        application.command("resource.delete", {
            "identity": reference(provider), "expected_sequence": 1, "idempotency_key": str(uuid4())})
        assert application.query("resource.read", {"identity": reference(provider)}) is None
        assert json.loads(saved_selection(database)) == selection()
    with closing(WorkflowHost(database)) as reopened:
        application = GraphApplication(reopened)
        catalog = application.query("catalog.node-types", {"protocol_version": 2})["node_types"]
        assert any(row["component_id"] == COMPONENT_ID for row in catalog)
        assert application.query("definition.read", {"identity": document["workflow_definition_id"]})
        assert json.loads(saved_selection(database)) == selection()


@pytest.mark.parametrize("record_factory", [prompt_record, provider_record],
                         ids=["content-resource", "provider-resource"])
def test_publication_failure_keeps_independent_request_evidence_business_receipt_and_resolution(
        tmp_path, monkeypatch, record_factory):
    database = tmp_path / "independent-evidence.sqlite"
    disk_package(tmp_path / "packages" / "workflow")
    with closing(WorkflowHost(database)) as host:
        service = host.graph_service
        application = GraphApplication(host)

        def reject_publication(candidate):
            raise RuntimeError("integration evidence publication rejected")

        with monkeypatch.context() as patch:
            patch.setattr(service, "_publish_capabilities", reject_publication)
            with pytest.raises(ContractValidationError) as failed:
                application.command("packages.configure", {"enabled_packages": selection()})
        assert failed.value.reason_code == "package_publication_failed"

        record = record_factory()
        request = {"record": record, "expected_sequence": 0, "idempotency_key": str(uuid4())}
        evidence_request = {
            "operation": "resource.save", "idempotency_key": request["idempotency_key"],
            "owner_kind": "integration-resource", "owner_id": record["resource_id"],
            "request": deepcopy(request),
        }
        original = deepcopy(evidence_request)
        evidence = application.command("request-evidence.write", evidence_request)["result"]
        assert evidence_request == original
        assert evidence["resolution"] == "pending" and evidence["request"] == request
        owner = {key: evidence[key] for key in ("owner_kind", "owner_id")}
        assert application.query("request-evidence.read", owner) == [evidence]
        assert application.query("resource.read", {"identity": reference(record)}) is None

        accepted = application.command("resource.save", request)
        assert application.query("resource.read", {"identity": reference(record)}) == record
        receipt = application.query("receipt.read", {"operation": "resource.save", "parameters": request})
        assert receipt["outcome"] == "matched" and receipt["result"] == accepted["result"]
        assert receipt["receipt"]["request_sha256"] == evidence["request_digest"]
        resolution = {
            **{key: evidence[key] for key in ("operation", "idempotency_key", "request_digest")},
            "resolution": "verified",
        }
        resolved = application.command("request-evidence.resolve", resolution)["result"]
        assert resolved == {**evidence, "resolution": "verified"}
        assert application.query("request-evidence.read", owner) == [resolved]
        assert application.query("receipt.read", {
            "operation": "resource.save", "parameters": request}) == receipt
        assert json.loads(saved_selection(database)) == selection()
        assert application.query("platform")["package_publication"]["publication_completed"] is False


@pytest.mark.parametrize("publication_fails", [False, True], ids=["confirmed", "failed"])
def test_content_metadata_uses_confirmed_types_during_package_publication(
        tmp_path, monkeypatch, publication_fails):
    database = tmp_path / "concurrent-publication.sqlite"
    disk_package(tmp_path / "packages" / "workflow")
    with closing(WorkflowHost(database)) as host:
        service = host.graph_service
        application = GraphApplication(service)
        record = prompt_record()
        application.command("resource.save", {
            "record": record, "expected_sequence": 0, "idempotency_key": str(uuid4())})
        parameters = {"reference": reference(record)}
        metadata = application.query("content.metadata.read", parameters)
        original_types = service._content_types()
        original_publish = service._package_loader._publish
        entered, release = Event(), Event()

        def paused_publish(candidate):
            assert service.registry.data_types.get(
                record["type_id"], record["data_schema_version"], scope="global") is None
            entered.set()
            assert release.wait(timeout=10), "Publication test barrier was not released"
            if publication_fails:
                raise RuntimeError("integration loader publication rejected")
            original_publish(candidate)

        monkeypatch.setattr(service._package_loader, "_publish", paused_publish)
        with ThreadPoolExecutor(max_workers=2) as executor:
            publication = executor.submit(
                application.command, "packages.configure", {"enabled_packages": {PACKAGE_ID: "1"}})
            try:
                assert entered.wait(timeout=10), "Publication did not reach the loader barrier"
                reading = executor.submit(application.query, "content.metadata.read", parameters)
                assert reading.result(timeout=5) == metadata
                assert service._content_types() is original_types
            finally:
                release.set()
            if publication_fails:
                with pytest.raises(ContractValidationError) as failed:
                    publication.result(timeout=10)
                assert failed.value.reason_code == "package_publication_failed"
            else:
                publication.result(timeout=10)

        if publication_fails:
            assert service._content_types() is original_types
            assert application.query("content.metadata.read", parameters) == metadata
        else:
            assert service._content_types() is service.registry.data_types
            assert service._content_types() is not original_types
            with pytest.raises(ContractValidationError) as denied:
                application.query("content.metadata.read", parameters)
            assert denied.value.reason_code == "content_resource_type_denied"
            service.registry.data_types.register(lifecycle_prompt_resource_definition())
            assert application.query("content.metadata.read", parameters) == metadata
        assert json.loads(saved_selection(database)) == {PACKAGE_ID: "1"}


def test_partial_publication_keeps_frontend_and_chat_receipts_readonly(tmp_path, monkeypatch):
    database = tmp_path / "frontend-publication.sqlite"
    disk_package(tmp_path / "packages" / "workflow")
    with closing(WorkflowHost(database)) as host:
        service = host.graph_service
        channel, owner, graph, _, initial = prepared_frontend(service, session=True)
        patch = frontend_request(owner, "patch_input", {
            "text": "publication receipt probe", "expected_edit_sequence": graph["edit_sequence"],
            "idempotency_key": "before-failure-patch"})
        start = frontend_request(owner, "start", {
            "expected_session_revision": initial["revision"], "idempotency_key": "before-failure-start"})
        channel.handle(patch)
        started = channel.handle(start)
        service.wait(started["result"]["start_result"]["active_chain_run_id"])
        view = service.get_session(initial["workflow_session_id"])
        round_id = view["objects"]["chat-0"]["value"]["rounds"][0]["round_id"]
        reroll = frontend_request(owner, "reroll", {
            "round_id": round_id, "expected_session_revision": view["revision"],
            "expected_data_revision": view["data_revision"], "expected_head_revision": view["head_revision"],
            "expected_object_revision": view["objects"]["chat-0"]["revision"],
            "idempotency_key": "before-failure-reroll"})
        rerolled = channel.handle(reroll)
        service.wait(rerolled["result"]["control_result"]["active_chain_run_id"])
        receipt_packets = [
            frontend_request(owner, "receipt", {
                "operation": packet["operation"],
                "original_request": {"owner": owner, "parameters": packet["parameters"]},
            })
            for packet in (patch, start, reroll)
        ]
        receipts = [channel.handle(packet) for packet in receipt_packets]
        assert all(result["outcome"] == "succeeded" or result["outcome"] == "saved" for result in receipts)
        old_catalog = service.platform_capabilities()

        def partially_publish(candidate):
            service.registry = candidate.registry
            assert service.registry.get("frontend.node", "2") is None
            raise RuntimeError("integration partial publication rejected")

        with monkeypatch.context() as patcher:
            patcher.setattr(service, "_publish_capabilities", partially_publish)
            with pytest.raises(ContractValidationError) as failed:
                service.configure_capability_packages({PACKAGE_ID: "1"})
        assert failed.value.reason_code == "package_publication_failed"
        assert service.registry.get("frontend.node", "2") is None
        assert service.platform_capabilities()["package_lock"] == old_catalog["package_lock"]
        before = frontend_storage_snapshot(service)
        fresh_channel = FrontendNodeChannel(service, owner)
        for packet, receipt in zip(receipt_packets, receipts):
            assert channel.handle(packet) == receipt
            assert fresh_channel.handle(packet) == receipt
            accept = {**packet, "operation": "accept_receipt"}
            with pytest.raises(ContractValidationError) as denied:
                fresh_channel.handle(accept)
            assert denied.value.reason_code == "package_publication_failed"
        for packet in (frontend_request(owner, "state"), patch, start, reroll):
            with pytest.raises(ContractValidationError) as denied:
                fresh_channel.handle(packet)
            assert denied.value.reason_code == "package_publication_failed"
        assert frontend_storage_snapshot(service) == before
        assert json.loads(saved_selection(database)) == {PACKAGE_ID: "1"}


@pytest.mark.parametrize("failure_kind", ["missing-dependency", "mismatched-export"])
def test_candidate_rejection_has_public_package_diagnostics_without_changing_selection(tmp_path, failure_kind):
    database = tmp_path / "candidate-diagnostic.sqlite"
    root = tmp_path / "packages" / "workflow"
    options = (
        {"extra_dependencies": [{"package_id": "integration.unavailable", "version": "9"}]}
        if failure_kind == "missing-dependency"
        else {"declared_component": "integration.unregistered-node"})
    marker = disk_package(root, **options)
    declaration = str((root / PACKAGE_ID / "openagent-package.json").resolve())
    with closing(WorkflowHost(database)) as host:
        service = host.graph_service
        application = GraphApplication(host)
        persisted, published = saved_selection(database), deepcopy(service.platform_capabilities())
        with pytest.raises(ContractValidationError) as failed:
            application.command("packages.configure", {"enabled_packages": selection()})
        expected = "package_missing_dependency" if failure_kind == "missing-dependency" else "package_export_mismatch"
        assert failed.value.reason_code == expected
        diagnostic = failed.value.diagnostics[0]
        assert diagnostic["reason_code"] == expected
        assert diagnostic["selection_committed"] is False
        assert diagnostic["publication_completed"] is False
        if failure_kind == "missing-dependency":
            assert diagnostic["stage"] == "resolution"
            assert (diagnostic["package_id"], diagnostic["version"]) == ("integration.unavailable", "9")
            assert diagnostic["required_by"] == {"package_id": PACKAGE_ID, "version": "1"}
            assert diagnostic["required_by_source"] == declaration
            assert not marker.exists()
        else:
            assert diagnostic["stage"] == "registration"
            assert (diagnostic["package_id"], diagnostic["version"]) == (PACKAGE_ID, "1")
            assert diagnostic["source"] == declaration
            assert marker.exists()
        assert saved_selection(database) == persisted
        assert service.platform_capabilities() == published
        assert service.registry.get(COMPONENT_ID, "1") is None
