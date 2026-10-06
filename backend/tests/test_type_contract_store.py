import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from phase1_agent.host_sdk import DataTypeDefinition, HostContractError, TypeRegistry
from phase1_agent.type_contract_store import TypeContractStore, initialize_type_contract_tables


def registry(*, schema=None, default="", version=1, scope="session", identity="sample.value"):
    types = TypeRegistry()
    types.register(DataTypeDefinition(identity, version, schema or {"type": "string"}, default, scope))
    return types


def open_contracts(path):
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    initialize_type_contract_tables(connection)
    return connection, TypeContractStore(SimpleNamespace(_connection=connection))


def test_restart_rejects_redefined_schema_and_retains_the_original_declaration(tmp_path):
    path = tmp_path / "contracts.sqlite"
    connection, contracts = open_contracts(path)
    with closing(connection):
        original = contracts.register(registry(), "sample.value", 1, "session")
    connection, contracts = open_contracts(path)
    with closing(connection):
        with pytest.raises(HostContractError) as caught:
            contracts.register(registry(schema={"type": "integer"}, default=0), "sample.value", 1, "session")
        assert caught.value.reason_code == "type_contract_redefined"
        assert contracts.get("sample.value", 1, "session") == original
        assert contracts.check(registry(), "sample.value", 1, "session") == original
        # A declared new schema identity can be adopted without rewriting v1.
        contracts.register(registry(schema={"type": "integer"}, default=0, version=2), "sample.value", 2, "session")
        assert contracts.get("sample.value", 2, "session")["declaration"]["schema"] == {"type": "integer"}
        assert contracts.get("sample.value", 1, "session") == original


def test_read_missing_evidence_requires_explicit_legacy_mode_and_does_not_set_baseline(tmp_path):
    connection, contracts = open_contracts(tmp_path / "legacy.sqlite")
    with closing(connection):
        with pytest.raises(HostContractError) as caught:
            contracts.check(registry(), "sample.value", 1, "session")
        assert caught.value.reason_code == "type_contract_evidence_missing"
        assert contracts.check(registry(), "sample.value", 1, "session", allow_legacy=True) is None
        assert contracts.get("sample.value", 1, "session") is None
        assert not connection.in_transaction
        # The first real write transaction fixes the declaration and participates
        # in the same rollback as the corresponding business-object save.
        connection.execute("BEGIN IMMEDIATE")
        assert contracts.check(registry(), "sample.value", 1, "session", allow_legacy=True) is None
        assert contracts.get("sample.value", 1, "session") is None
        contracts.check(registry(), "sample.value", 1, "session")
        assert contracts.get("sample.value", 1, "session") is not None
        connection.execute("ROLLBACK")
        assert contracts.get("sample.value", 1, "session") is None


def test_absent_package_can_leave_evidence_untouched_but_not_replace_it(tmp_path):
    connection, contracts = open_contracts(tmp_path / "missing.sqlite")
    with closing(connection):
        original = contracts.register(registry(), "sample.value", 1, "session")
        assert contracts.check(TypeRegistry(), "sample.value", 1, "session", allow_missing=True) is None
        assert contracts.get("sample.value", 1, "session") == original
        with pytest.raises(HostContractError) as caught:
            contracts.check(TypeRegistry(), "sample.value", 1, "session")
        assert caught.value.reason_code == "host_unknown_type"
        # Legacy/missing flags never relax an actual contradictory declaration.
        with pytest.raises(HostContractError) as caught:
            contracts.check(registry(schema={"type": "integer"}, default=0), "sample.value", 1, "session",
                            allow_missing=True, allow_legacy=True)
        assert caught.value.reason_code == "type_contract_redefined"


def test_project_registration_failure_rolls_back_partial_contracts_and_preserves_external_transaction(tmp_path):
    connection, contracts = open_contracts(tmp_path / "atomic.sqlite")
    with closing(connection):
        contracts.register(registry(identity="z.conflict"), "z.conflict", 1, "session")
        changed = registry(identity="a.new")
        changed.register(DataTypeDefinition("z.conflict", 1, {"type": "integer"}, 0))
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO unrelated VALUES('caller work')")
        with pytest.raises(HostContractError):
            contracts.check_registry(changed)
        assert connection.in_transaction
        assert contracts.get("a.new", 1, "session") is None
        assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "caller work"
        contracts.check_registry(registry(identity="b.accepted"))
        assert connection.in_transaction
        connection.execute("ROLLBACK")
        assert contracts.get("b.accepted", 1, "session") is None
        assert connection.execute("SELECT value FROM unrelated").fetchone() is None


def test_enabled_global_and_content_contracts_record_only_declarations_and_remain_immutable(tmp_path):
    connection, contracts = open_contracts(tmp_path / "domains.sqlite")
    with closing(connection):
        types = registry(scope="global", identity="sample.global")
        types.register(DataTypeDefinition("sample.content", 1, {
            "type": "object", "properties": {"schema_version": {"const": 1}},
            "required": ["schema_version"],
        }, scope="content"))
        evidence = contracts.check_registry(types)
        assert {record["declaration"]["scope"] for record in evidence} == {"global", "content"}
        assert set(evidence[0]) == {"envelope_version", "kind", "digest", "declaration"}
        assert all("resource_id" not in record["declaration"] and "value" not in record["declaration"]
                   for record in evidence)
        assert not connection.in_transaction
        changed_default = registry(scope="global", identity="sample.global", default="new default")
        with pytest.raises(HostContractError) as caught:
            contracts.check_registry(changed_default)
        assert caught.value.reason_code == "type_contract_redefined"


def test_corrupted_persistent_evidence_is_rejected_instead_of_replaced(tmp_path):
    connection, contracts = open_contracts(tmp_path / "corrupt.sqlite")
    with closing(connection):
        contracts.register(registry(), "sample.value", 1, "session")
        connection.execute("UPDATE registered_type_contracts SET digest='wrong'")
        with pytest.raises(HostContractError) as caught:
            contracts.check(registry(), "sample.value", 1, "session")
        assert caught.value.reason_code == "type_contract_evidence_invalid"
