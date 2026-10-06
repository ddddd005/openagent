"""Current-only global storage never creates a second historical body archive."""

from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.global_resources import (
    GLOBAL_CONTENT_TYPE, GlobalResourceStore, create_global_type_registry,
    global_resource_reference, legacy_content_to_current, validate_global_resource_reference,
)
from phase1_agent.host_sdk import DataTypeDefinition, ResourceIdentity, TypeRegistry
from phase1_agent.storage import SqliteStore
from phase1_agent.workbench_resources import WorkbenchResourceStore
from resource_fixtures import resource


def current(text="current-only-original"):
    return legacy_content_to_current(resource(text))


def reference(record):
    return ResourceIdentity(record["scope"], record["type_id"], record["resource_id"]).to_dict()


def test_updates_keep_one_current_body_and_identity_only_receipts_after_reopen(tmp_path):
    path = tmp_path / "current.sqlite"
    original = current()
    key = str(uuid4())
    command = dict(expected_sequence=0, idempotency_key=key)
    with closing(SqliteStore(path)) as store:
        catalog = GlobalResourceStore(store)
        first_receipt = catalog.write(original, **command)
        changed = deepcopy(original)
        changed["update_sequence"] = 2
        changed["value"]["members"][0]["text"] = "current-only-replacement"
        catalog.write(changed, expected_sequence=1, idempotency_key=str(uuid4()))
        assert catalog.write(original, **command) == first_receipt
        assert first_receipt == {"reference": reference(original), "update_sequence": 1, "deleted": False}
        assert catalog.get(reference(original)) == changed
        rows = [dict(row) for row in store._connection.execute("SELECT * FROM global_resource_current")]
        receipts = [dict(row) for row in store._connection.execute("SELECT * FROM global_resource_receipts")]
        assert len(rows) == 1 and len(receipts) == 2
        assert "current-only-original" not in canonical_bytes(rows + receipts).decode("utf-8")
        assert "current-only-replacement" not in canonical_bytes(receipts).decode("utf-8")
        assert store._connection.execute("SELECT COUNT(*) FROM workbench_resource_revisions").fetchone()[0] == 0
    with closing(SqliteStore(path)) as store:
        catalog = GlobalResourceStore(store)
        assert catalog.get(reference(original)) == changed
        assert catalog.list(scope="workspace", type_id=GLOBAL_CONTENT_TYPE) == [changed]


def test_cas_idempotency_conflicts_and_invalid_schema_do_not_change_current(tmp_path):
    with closing(SqliteStore(tmp_path / "cas.sqlite")) as store:
        catalog = GlobalResourceStore(store)
        original = current()
        key = str(uuid4())
        catalog.write(original, expected_sequence=0, idempotency_key=key)
        changed = deepcopy(original)
        changed["value"]["name"] = "different request"
        with pytest.raises(ContractValidationError) as conflict:
            catalog.write(changed, expected_sequence=0, idempotency_key=key)
        assert conflict.value.reason_code == "idempotency_conflict"
        with pytest.raises(ContractValidationError) as stale:
            catalog.write(changed, expected_sequence=0, idempotency_key=str(uuid4()))
        assert stale.value.reason_code == "stale_revision"
        changed["update_sequence"] = 2
        changed["value"]["members"][0]["text"] = 3
        with pytest.raises(ContractValidationError):
            catalog.write(changed, expected_sequence=1, idempotency_key=str(uuid4()))
        assert catalog.get(reference(original)) == original
        assert store._connection.execute("SELECT COUNT(*) FROM global_resource_receipts").fetchone()[0] == 1


def test_delete_purges_body_and_keeps_monotonic_identity_tombstone(tmp_path):
    with closing(SqliteStore(tmp_path / "delete.sqlite")) as store:
        catalog = GlobalResourceStore(store)
        original = current("delete-current-body")
        catalog.write(original, expected_sequence=0, idempotency_key=str(uuid4()))
        command = dict(expected_sequence=1, idempotency_key=str(uuid4()))
        removed = catalog.delete(reference(original), **command)
        assert catalog.delete(reference(original), **command) == removed
        assert removed == {"reference": reference(original), "update_sequence": 2, "deleted": True}
        assert catalog.head(reference(original)) == removed
        assert catalog.get(reference(original)) is None and catalog.list() == []
        with pytest.raises(ContractValidationError) as missing:
            catalog.read_many([reference(original)])
        assert missing.value.reason_code == "global_resource_missing"
        rows = [dict(row) for row in store._connection.execute("SELECT * FROM global_resource_current")]
        assert rows[0]["payload"] is None
        assert "delete-current-body" not in canonical_bytes(rows).decode("utf-8")
        with pytest.raises(ContractValidationError) as stale:
            catalog.write(original, expected_sequence=0, idempotency_key=str(uuid4()))
        assert stale.value.reason_code == "stale_revision"
        recreated = deepcopy(original)
        recreated["update_sequence"] = 3
        recreated["value"]["members"][0]["text"] = "recreated"
        catalog.write(recreated, expected_sequence=2, idempotency_key=str(uuid4()))
        assert catalog.get(reference(original)) == recreated


def test_package_registered_types_and_namespaces_need_no_resource_kind_branch(tmp_path):
    types = create_global_type_registry()
    types.register(DataTypeDefinition("example.numeric", 1, {"type": "integer"}, scope="global"))
    with closing(SqliteStore(tmp_path / "plugin.sqlite")) as store:
        catalog = GlobalResourceStore(store, types)
        identity = str(uuid4())
        first = {"envelope_version": 1, "scope": "project:one", "type_id": "example.numeric",
                 "resource_id": identity, "data_schema_version": 1, "update_sequence": 1, "value": 42}
        second = {**first, "scope": "project:two", "value": 7}
        catalog.write(first, expected_sequence=0, idempotency_key=str(uuid4()))
        catalog.write(second, expected_sequence=0, idempotency_key=str(uuid4()))
        assert catalog.read_many([reference(first), reference(second)]) == [first, second]
        with pytest.raises(ContractValidationError):
            catalog.read_many([reference(first), reference(first)])
        unavailable = GlobalResourceStore(store, TypeRegistry())
        with pytest.raises(ContractValidationError) as missing:
            unavailable.get(reference(first))
        assert missing.value.reason_code == "host_unknown_type"
        assert catalog.get(reference(first)) == first
        with pytest.raises(ContractValidationError):
            catalog.write({**first, "update_sequence": 2, "value": "bad"},
                          expected_sequence=1, idempotency_key=str(uuid4()))
        assert catalog.get(reference(first))["value"] == 42


def test_multiple_resource_reads_share_one_view_despite_concurrent_management_update(tmp_path):
    path = tmp_path / "consistent.sqlite"
    types = create_global_type_registry()
    types.register(DataTypeDefinition("example.numeric", 1, {"type": "integer"}, scope="global"))
    with closing(SqliteStore(path)) as reader_store, closing(SqliteStore(path)) as writer_store:
        reader_store._connection.execute("PRAGMA journal_mode=WAL")
        writer = GlobalResourceStore(writer_store, types)
        records = [{"envelope_version": 1, "scope": "workspace", "type_id": "example.numeric",
                    "resource_id": str(uuid4()), "data_schema_version": 1, "update_sequence": 1, "value": 1}
                   for _ in range(2)]
        for record in records:
            writer.write(record, expected_sequence=0, idempotency_key=str(uuid4()))

        class InterleavedReader(GlobalResourceStore):
            def get(self, identity):
                result = super().get(identity)
                if identity == reference(records[0]):
                    writer.write({**records[1], "update_sequence": 2, "value": 2},
                                 expected_sequence=1, idempotency_key=str(uuid4()))
                return result

        reading = InterleavedReader(reader_store, types).read_many([reference(record) for record in records])
        assert [record["value"] for record in reading] == [1, 1]
        assert writer.get(reference(records[1]))["value"] == 2


def test_explicit_legacy_import_keeps_old_archive_and_does_not_replay_newer_body_on_retry(tmp_path):
    with closing(SqliteStore(tmp_path / "legacy.sqlite")) as store:
        legacy = WorkbenchResourceStore(store)
        original = resource("legacy-archived-body")
        legacy.write("content", original, expected_revision=0, idempotency_key=str(uuid4()))
        old_current = deepcopy(original)
        old_current["revision"] = 2
        old_current["members"][0]["text"] = "legacy-current-body"
        legacy.write("content", old_current, expected_revision=1, idempotency_key=str(uuid4()))
        catalog = GlobalResourceStore(store)
        assert catalog.list() == []
        key = str(uuid4())
        receipt = catalog.import_legacy_current(original["resource_id"], idempotency_key=key)
        assert receipt["legacy_source"] == {"resource_id": original["resource_id"], "revision": 2}
        adopted = legacy_content_to_current(old_current)
        assert catalog.get(reference(adopted)) == adopted
        changed = {**adopted, "update_sequence": 2,
                   "value": {**adopted["value"], "name": "current-only admin"}}
        catalog.write(changed, expected_sequence=1, idempotency_key=str(uuid4()))
        assert catalog.import_legacy_current(original["resource_id"], idempotency_key=key) == receipt
        assert catalog.get(reference(adopted)) == changed
        assert legacy.get("content", original["resource_id"], 1) == original
        assert legacy.get("content", original["resource_id"]) == old_current


def test_identity_content_rejects_body_version_pins_and_extra_fields():
    identity = ResourceIdentity("workspace", GLOBAL_CONTENT_TYPE, str(uuid4())).to_dict()
    output = global_resource_reference(identity)
    assert validate_global_resource_reference(output) == output
    assert create_global_type_registry().references("GLOBAL_RESOURCE_REF", 1, output, scope="content") == [identity]
    for changed in ({**output, "text": "body"}, {**output, "reference": {**identity, "revision": 7}}):
        with pytest.raises(ContractValidationError):
            validate_global_resource_reference(changed)


def test_version_two_global_node_emits_only_identity_and_preserves_legacy_version():
    from phase1_agent.graph_contracts import NodeRegistry
    from phase1_agent.graph_prompt_nodes import register_prompt_nodes

    registry = NodeRegistry(create_global_type_registry())
    register_prompt_nodes(registry)
    record = current("ephemeral-current-resource")
    reads, calls = [], []

    def host_call(capability, operation, payload):
        calls.append((capability, operation, payload))
        return deepcopy(record)

    context = SimpleNamespace(host_call=host_call, reads=reads)
    installed = registry.get("workflow.global-content", "2")
    config = {"resource_id": record["resource_id"]}
    output = installed.executor(config, {}, context)["output"]
    assert registry.validate_content(output, "GLOBAL_RESOURCE_REF") == global_resource_reference(reference(record))
    assert reads == [{"kind": "global_resource_read", "reference": reference(record)}]
    assert calls == [("resources:read", "current-global-resource", reference(record))]
    assert installed.resource_dependencies_declaration(config) == [
        {"kind": "global-resource", "reference": reference(record)},
    ]
    assert "ephemeral-current-resource" not in canonical_bytes(output).decode("utf-8")
    legacy = registry.get("workflow.global-content", "1")
    assert legacy.definition.outputs[0].data_type == "PROMPT"
    assert "legacy v1" in legacy.definition.display_name
