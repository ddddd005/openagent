"""Prompt catalog persistence, exact references and transactional CAS behavior."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.prompt_store import PromptConfigStore
from phase1_agent.storage import SqliteStore


@pytest.fixture
def tmp_path():
    with TemporaryDirectory(prefix="prompt-store-", dir=Path(__file__).resolve().parent) as directory:
        yield Path(directory)


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def item(number=1, revision=1, **changes):
    result = {
        "schema_version": 1, "kind": "item", "item_id": uid(number),
        "revision": revision, "name": "Instructions", "text": "Stay concise",
        "role": "system", "enabled": True, "placement": "before", "depth": None,
        "order": 0, "interpolation": "variables", "source": {"kind": "configuration"},
    }
    result.update(changes)
    return result


def group(number=2, revision=1, item_number=1, item_revision=1):
    return {
        "schema_version": 1, "kind": "group", "group_id": uid(number),
        "revision": revision, "name": "Preset",
        "members": [{
            "item_instance_id": uid(20), "item_id": uid(item_number),
            "revision": item_revision, "overrides": {},
        }],
    }


def config(number=3, revision=1, group_number=2, group_revision=1):
    return {
        "schema_version": 1, "kind": "config", "config_id": uid(number),
        "revision": revision, "name": "Agent settings",
        "inputs": [{
            "name": "preset", "kind": "group", "group_instance_id": uid(30),
            "group_id": uid(group_number), "revision": group_revision, "enabled": True,
            "member_overrides": [],
        }],
    }


def write(catalog, kind, record, expected=0, key=None):
    return catalog.write_revision(
        kind, record, expected_revision=expected,
        idempotency_key=key or f"{kind}:{record[kind + '_id']}:{record['revision']}:{expected}",
    )


def seed(catalog):
    return [write(catalog, kind, record) for kind, record in (
        ("item", item()), ("group", group()), ("config", config()),
    )]


def assert_reason(error, reason, status):
    assert error.value.reason_code == reason
    assert error.value.status_code == status


def test_create_update_old_revisions_and_detached_reads(tmp_path):
    path = tmp_path / "workflow.sqlite3"
    original = item()
    with closing(SqliteStore(path)) as store:
        catalog = PromptConfigStore(store)
        first = write(catalog, "item", original)
        assert first == {"record": original, "head": {
            "kind": "item", "id": uid(1), "definition_revision": 1,
            "catalog_revision": 1, "selectable": True,
        }}
        original["text"] = "Caller edits do not modify the database"
        first["record"]["text"] = "Returned values are detached"
        head = catalog.get_head("item", uid(1))
        head["catalog_revision"] = 99
        current = catalog.list_current("item")
        current[0]["record"]["source"]["kind"] = "wrong"
        assert catalog.get_revision("item", uid(1), 1) == item()
        assert catalog.get_head("item", uid(1))["catalog_revision"] == 1

        second = write(catalog, "item", item(revision=2, text="New settings"), expected=1)
        assert second["head"]["catalog_revision"] == 2
        assert catalog.get_revision("item", uid(1), 1) == item()
        assert catalog.list_current("item") == [second]
        assert catalog.get_revision("item", uid(1), 8) is None
        assert catalog.get_head("item", uid(99)) is None
        assert store.read_bundle() == {}

    with closing(SqliteStore(path)) as reopened:
        catalog = PromptConfigStore(reopened)
        assert catalog.get_revision("item", uid(1), 1) == item()
        assert catalog.get_revision("item", uid(1), 2)["text"] == "New settings"
        assert catalog.list_current("item") == [second]


def test_tombstone_keeps_all_references_and_catalog_revision_separate(tmp_path):
    path = tmp_path / "workflow.sqlite3"
    with closing(SqliteStore(path)) as store:
        catalog = PromptConfigStore(store)
        seed(catalog)
        hidden = catalog.delete("item", uid(1), expected_revision=1, idempotency_key="hide-item")
        assert hidden["head"] == {
            "kind": "item", "id": uid(1), "definition_revision": 1,
            "catalog_revision": 2, "selectable": False,
        }
        assert catalog.list_current("item") == []
        assert catalog.list_current("item", include_hidden=True) == [hidden]
        assert catalog.get_revision("item", uid(1), 1) == item()
        assert catalog.resolve_config(uid(3), 1)[0]["text"] == item()["text"]
        write(catalog, "group", group(number=4))
        catalog.delete("group", uid(2), expected_revision=1, idempotency_key="hide-group")
        write(catalog, "config", config(number=5))
        assert catalog.resolve_config(uid(5), 1)[0]["item_id"] == uid(1)

        restored = write(catalog, "item", item(revision=2, text="Restored"), expected=2)
        assert restored["head"]["catalog_revision"] == 3
        assert restored["head"]["definition_revision"] == 2
        assert restored["head"]["selectable"] is True
        assert catalog.resolve_config(uid(3), 1)[0]["text"] == item()["text"]
        assert catalog.get_revision("group", uid(2), 1) == group()

    with closing(SqliteStore(path)) as reopened:
        assert PromptConfigStore(reopened).resolve_config(uid(3), 1)[0]["text"] == item()["text"]


def test_idempotency_replay_survives_reopen_and_newer_mutations(tmp_path):
    path = tmp_path / "workflow.sqlite3"
    with closing(SqliteStore(path)) as store:
        catalog = PromptConfigStore(store)
        first = write(catalog, "item", item(), key="create")
        write(catalog, "item", item(revision=2, text="New"), expected=1, key="update")
        assert write(catalog, "item", item(), key="create") == first
        hidden = catalog.delete("item", uid(1), expected_revision=2, idempotency_key="hide")
        write(catalog, "item", item(revision=3), expected=3, key="restore")
        assert catalog.delete("item", uid(1), expected_revision=2, idempotency_key="hide") == hidden

    with closing(SqliteStore(path)) as reopened:
        catalog = PromptConfigStore(reopened)
        assert write(catalog, "item", item(), key="create") == first
        assert catalog.delete("item", uid(1), expected_revision=2, idempotency_key="hide") == hidden
        for changed in (item(text="Changed"), item(number=9)):
            with pytest.raises(ContractValidationError) as error:
                write(catalog, "item", changed, key="create")
            assert_reason(error, "idempotency_conflict", 409)
        with pytest.raises(ContractValidationError) as error:
            catalog.delete("item", uid(1), expected_revision=0, idempotency_key="create")
        assert_reason(error, "idempotency_conflict", 409)
        with pytest.raises(ContractValidationError) as error:
            write(catalog, "item", item(), expected=4, key="create")
        assert_reason(error, "idempotency_conflict", 409)
        assert catalog.get_head("item", uid(1))["catalog_revision"] == 4


@pytest.mark.parametrize("point", [
    "prompt_before_write", "prompt_after_revision_write",
    "prompt_after_head_write", "prompt_before_commit",
])
def test_write_fault_rolls_back_revision_head_and_receipt(tmp_path, point):
    path = tmp_path / "workflow.sqlite3"

    def fail(observed):
        if observed == point:
            raise RuntimeError("injected catalog write failure")

    with closing(SqliteStore(path, fault_injector=fail)) as store:
        catalog = PromptConfigStore(store)
        with pytest.raises(RuntimeError, match="injected catalog write failure"):
            write(catalog, "item", item(), key="create")
        assert catalog.get_revision("item", uid(1), 1) is None
        assert catalog.get_head("item", uid(1)) is None
    with closing(SqliteStore(path)) as reopened:
        catalog = PromptConfigStore(reopened)
        assert write(catalog, "item", item(), key="create")["head"]["catalog_revision"] == 1
        assert reopened._connection.execute("SELECT COUNT(*) FROM prompt_mutations").fetchone()[0] == 1


@pytest.mark.parametrize("point", [
    "prompt_before_write", "prompt_after_head_write", "prompt_before_commit",
])
def test_delete_fault_does_not_hide_or_save_receipt(tmp_path, point):
    path = tmp_path / "workflow.sqlite3"
    with closing(SqliteStore(path)) as store:
        original = write(PromptConfigStore(store), "item", item())

    def fail(observed):
        if observed == point:
            raise RuntimeError("injected catalog hide failure")

    with closing(SqliteStore(path, fault_injector=fail)) as store:
        catalog = PromptConfigStore(store)
        with pytest.raises(RuntimeError, match="injected catalog hide failure"):
            catalog.delete("item", uid(1), expected_revision=1, idempotency_key="hide")
        assert catalog.list_current("item") == [original]
        assert store._connection.execute(
            "SELECT COUNT(*) FROM prompt_mutations WHERE idempotency_key = 'hide'",
        ).fetchone()[0] == 0
    with closing(SqliteStore(path)) as reopened:
        hidden = PromptConfigStore(reopened).delete(
            "item", uid(1), expected_revision=1, idempotency_key="hide",
        )
        assert hidden["head"]["catalog_revision"] == 2


def test_cas_revision_and_wrong_kind_failures_do_not_write(tmp_path):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        write(catalog, "item", item())
        with pytest.raises(ContractValidationError) as error:
            write(catalog, "item", item(revision=2), expected=0)
        assert_reason(error, "stale_revision", 409)
        for record in (item(), item(revision=4)):
            with pytest.raises(ContractValidationError) as error:
                write(catalog, "item", record, expected=1)
            assert_reason(error, "invalid_request", 400)
        with pytest.raises(ContractValidationError) as error:
            catalog.write_revision(
                "group", item(), expected_revision=0, idempotency_key="wrong-kind",
            )
        assert_reason(error, "invalid_request", 400)
        with pytest.raises(ContractValidationError) as error:
            catalog.delete("item", uid(1), expected_revision=0, idempotency_key="stale-hide")
        assert_reason(error, "stale_revision", 409)
        with pytest.raises(ContractValidationError) as error:
            catalog.delete("item", uid(99), expected_revision=0, idempotency_key="missing-hide")
        assert_reason(error, "not_found", 404)
        assert catalog.get_head("item", uid(1))["catalog_revision"] == 1
        assert store._connection.execute("SELECT COUNT(*) FROM prompt_revisions").fetchone()[0] == 1
        assert store._connection.execute("SELECT COUNT(*) FROM prompt_mutations").fetchone()[0] == 1


@pytest.mark.parametrize("change", [
    {"members": [{"item_instance_id": uid(20), "item_id": uid(99), "revision": 1, "overrides": {}}]},
    {"members": [{"item_instance_id": uid(20), "item_id": uid(1), "revision": 2, "overrides": {}}]},
    {"members": [{"item_instance_id": uid(20), "item_id": uid(1), "revision": 1,
                  "overrides": {"placement": "middle"}}]},
    {"members": [{"item_instance_id": uid(20), "item_id": uid(1), "revision": 1,
                  "overrides": {"item_id": uid(99)}}]},
])
def test_invalid_group_reference_and_effective_override_are_rejected(tmp_path, change):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        write(catalog, "item", item())
        value = group()
        value.update(change)
        with pytest.raises(ContractValidationError) as error:
            write(catalog, "group", value)
        assert_reason(error, "invalid_request", 400)
        assert catalog.list_current("group") == []
        assert store._connection.execute("SELECT COUNT(*) FROM prompt_mutations").fetchone()[0] == 1


def test_disabled_config_still_validates_exact_refs_and_override_targets(tmp_path):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        write(catalog, "item", item())
        write(catalog, "group", group())
        missing = config(group_number=99)
        missing["inputs"][0]["enabled"] = False
        with pytest.raises(ContractValidationError):
            write(catalog, "config", missing)
        bad_target = config()
        bad_target["inputs"][0]["enabled"] = False
        bad_target["inputs"][0]["member_overrides"] = [{
            "item_instance_id": uid(99), "overrides": {"text": "Invalid target"},
        }]
        with pytest.raises(ContractValidationError):
            write(catalog, "config", bad_target)
        valid = config()
        valid["inputs"][0]["enabled"] = False
        write(catalog, "config", valid)
        assert catalog.resolve_config(uid(3), 1) == []


def test_repeated_group_refs_keep_member_instance_scope(tmp_path):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        write(catalog, "item", item())
        write(catalog, "group", group())
        value = config()
        second = deepcopy(value["inputs"][0])
        second.update(name="preset_again", group_instance_id=uid(31))
        second["member_overrides"] = [{
            "item_instance_id": uid(20), "overrides": {"text": "Second instance"},
        }]
        value["inputs"].append(second)
        write(catalog, "config", value)
        expanded = catalog.resolve_config(uid(3), 1)
        assert [entry["item_id"] for entry in expanded] == [uid(1), uid(1)]
        assert [entry["group_instance_id"] for entry in expanded] == [uid(30), uid(31)]
        assert [entry["item_instance_id"] for entry in expanded] == [uid(20), uid(20)]
        assert [entry["text"] for entry in expanded] == ["Stay concise", "Second instance"]
        assert catalog.get_revision("item", uid(1), 1)["text"] == "Stay concise"


@pytest.mark.parametrize("mutation", ["update", "delete"])
def test_concurrent_catalog_cas_has_exactly_one_winner(tmp_path, mutation):
    path = tmp_path / "workflow.sqlite3"
    with closing(SqliteStore(path)) as store:
        write(PromptConfigStore(store), "item", item())

    def apply(number):
        with closing(SqliteStore(path)) as store:
            catalog = PromptConfigStore(store)
            try:
                if mutation == "delete":
                    return catalog.delete(
                        "item", uid(1), expected_revision=1, idempotency_key=f"hide-{number}",
                    )
                return write(
                    catalog, "item", item(revision=2, text=f"Update {number}"),
                    expected=1, key=f"update-{number}",
                )
            except ContractValidationError as error:
                return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, (1, 2)))
    winners = [result for result in results if isinstance(result, dict)]
    losers = [result for result in results if isinstance(result, ContractValidationError)]
    assert len(winners) == len(losers) == 1
    assert losers[0].reason_code == "stale_revision"
    with closing(SqliteStore(path)) as store:
        catalog = PromptConfigStore(store)
        assert catalog.get_head("item", uid(1))["catalog_revision"] == 2
        assert store._connection.execute("SELECT COUNT(*) FROM prompt_mutations").fetchone()[0] == 2


def test_concurrent_identical_create_replays_one_mutation(tmp_path):
    path = tmp_path / "workflow.sqlite3"

    def create(_):
        with closing(SqliteStore(path)) as store:
            return write(PromptConfigStore(store), "item", item(), key="same-create")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (1, 2)))
    assert results[0] == results[1]
    with closing(SqliteStore(path)) as store:
        assert store._connection.execute("SELECT COUNT(*) FROM prompt_revisions").fetchone()[0] == 1
        assert store._connection.execute("SELECT COUNT(*) FROM prompt_mutations").fetchone()[0] == 1


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_v6_migration_preserves_runtime_rows_receipts_and_has_no_prompt_data(tmp_path, version):
    path = tmp_path / "workflow.sqlite3"
    payload = canonical_bytes({"saved": "runtime history"}).decode("utf-8")
    with closing(sqlite3.connect(path)) as raw:
        raw.execute(
            "CREATE TABLE records (record_type TEXT NOT NULL, record_id TEXT NOT NULL, "
            "payload TEXT NOT NULL, immutable INTEGER NOT NULL, created_at TEXT NOT NULL, "
            "PRIMARY KEY (record_type, record_id))",
        )
        raw.execute(
            "CREATE TABLE idempotency (operation TEXT NOT NULL, key TEXT NOT NULL, "
            "digest TEXT NOT NULL, result_refs TEXT NOT NULL, result_payload TEXT NOT NULL"
            + (", request_digest TEXT" if version >= 2 else "")
            + ", PRIMARY KEY (operation, key))",
        )
        raw.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)",
                    ("test-runtime", "saved-id", payload, 1, "2026-09-30T00:00:00.000Z"))
        raw.execute(
            "INSERT INTO idempotency (operation, key, digest, result_refs, result_payload) "
            "VALUES (?, ?, ?, ?, ?)",
            ("archive", "old-receipt", "old-digest", "[]", payload),
        )
        if version == 5:
            raw.execute(
                "CREATE TABLE execution_facts (fact_id TEXT PRIMARY KEY NOT NULL, "
                "run_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence >= 1), "
                "payload TEXT NOT NULL, UNIQUE (run_id, sequence))",
            )
            raw.execute("INSERT INTO execution_facts VALUES (?, ?, ?, ?)",
                        ("fact-id", "run-id", 1, payload))
        raw.execute(f"PRAGMA user_version = {version}")
        raw.commit()

    with closing(SqliteStore(path)) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert store._connection.execute("SELECT payload FROM records").fetchone()[0] == payload
        assert store._connection.execute(
            "SELECT result_payload FROM idempotency WHERE key = 'old-receipt'",
        ).fetchone()[0] == payload
        assert store._connection.execute("SELECT COUNT(*) FROM execution_facts").fetchone()[0] == (
            1 if version == 5 else 0
        )
        catalog = PromptConfigStore(store)
        assert all(catalog.list_current(kind) == [] for kind in ("item", "group", "config"))
        assert store._connection.execute("SELECT COUNT(*) FROM prompt_mutations").fetchone()[0] == 0
        assert store._connection.execute("SELECT COUNT(*) FROM prompt_revisions").fetchone()[0] == 0


@pytest.mark.parametrize("kind,identity,revision", [
    ("invalid", uid(1), 1), ("item", "not-an-id", 1),
    ("item", uid(0xABC).upper(), 1), ("item", uid(1), True), ("item", uid(1), 0),
])
def test_read_identity_is_checked_even_if_record_is_missing(tmp_path, kind, identity, revision):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        with pytest.raises(ContractValidationError) as error:
            catalog.get_revision(kind, identity, revision)
        assert_reason(error, "invalid_request", 400)


def test_missing_config_and_bad_kind_have_explicit_public_errors(tmp_path):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        with pytest.raises(ContractValidationError) as error:
            catalog.resolve_config(uid(3), 1)
        assert_reason(error, "not_found", 404)
        with pytest.raises(ContractValidationError) as error:
            catalog.list_current("unknown")
        assert_reason(error, "invalid_request", 400)
        with pytest.raises(ContractValidationError) as error:
            catalog.get_head("item", "not-an-id")
        assert_reason(error, "invalid_request", 400)


def test_list_current_is_id_sorted_without_name_or_insertion_order(tmp_path):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        write(catalog, "item", item(number=5, name="Same label"))
        write(catalog, "item", item(number=1, name="Same label"))
        assert [entry["record"]["item_id"] for entry in catalog.list_current("item")] == [
            uid(1), uid(5),
        ]


@pytest.mark.parametrize("key", ["", " ", "\t\n", "a" * 129, "\ud800", None, 12])
def test_invalid_mutation_key_is_rejected_before_writes(tmp_path, key):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        with pytest.raises(ContractValidationError) as error:
            catalog.write_revision(
                "item", item(), expected_revision=0, idempotency_key=key,
            )
        assert_reason(error, "invalid_request", 400)
        assert catalog.list_current("item") == []


@pytest.mark.parametrize("change", [
    {"kind": "group"},
    {"schema_version": 2},
    {"item_id": uid(9)},
    {"revision": 2},
    {"role": "tool"},
])
def test_corrupt_saved_body_is_a_storage_error_on_reads_and_mutations(tmp_path, change):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        write(catalog, "item", item())
        corrupt = item()
        corrupt.update(change)
        store._connection.execute(
            "UPDATE prompt_revisions SET payload = ? WHERE kind = 'item'",
            (canonical_bytes(corrupt).decode("utf-8"),),
        )
        operations = [
            lambda: catalog.get_revision("item", uid(1), 1),
            lambda: catalog.get_head("item", uid(1)),
            lambda: catalog.list_current("item"),
            lambda: write(catalog, "item", item(revision=2), expected=1, key="new"),
            lambda: catalog.delete("item", uid(1), expected_revision=1, idempotency_key="hide"),
            lambda: write(catalog, "item", item()),
        ]
        for operation in operations:
            with pytest.raises(ContractValidationError) as error:
                operation()
            assert_reason(error, "storage_contract_violation", 500)
        assert store._connection.execute("SELECT COUNT(*) FROM prompt_mutations").fetchone()[0] == 1


def test_missing_head_body_fails_instead_of_disappearing_or_being_recreated(tmp_path):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        write(catalog, "item", item())
        store._connection.execute(
            "UPDATE prompt_heads SET definition_revision = 2, catalog_revision = 2",
        )
        operations = [
            lambda: catalog.get_head("item", uid(1)),
            lambda: catalog.list_current("item"),
            lambda: write(catalog, "item", item(revision=3), expected=2, key="new"),
            lambda: catalog.delete("item", uid(1), expected_revision=2, idempotency_key="hide"),
        ]
        for operation in operations:
            with pytest.raises(ContractValidationError) as error:
                operation()
            assert_reason(error, "storage_contract_violation", 500)
        assert catalog.get_revision("item", uid(1), 1) == item()
        assert catalog.get_revision("item", uid(1), 3) is None


@pytest.mark.parametrize("column,value", [
    ("catalog_revision", 0), ("catalog_revision", 1.5),
    ("definition_revision", 0), ("selectable", 2), ("selectable", "true"),
])
def test_corrupt_catalog_head_attributes_are_not_coerced(tmp_path, column, value):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        write(catalog, "item", item())
        store._connection.execute("PRAGMA ignore_check_constraints = ON")
        store._connection.execute(f"UPDATE prompt_heads SET {column} = ?", (value,))
        with pytest.raises(ContractValidationError) as error:
            catalog.get_head("item", uid(1))
        assert_reason(error, "storage_contract_violation", 500)
        with pytest.raises(ContractValidationError) as error:
            catalog.list_current("item", include_hidden=True)
        assert_reason(error, "storage_contract_violation", 500)


@pytest.mark.parametrize("corruption", [
    "invalid_json", "shape", "record_identity", "head_identity", "head_revision",
    "head_selectable", "record_text", "digest",
])
def test_corrupt_receipt_cannot_replay_a_fabricated_result(tmp_path, corruption):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        result = write(catalog, "item", item(), key="create")
        if corruption == "invalid_json":
            payload = "{"
        elif corruption == "shape":
            payload = "[]"
        else:
            if corruption == "record_identity":
                result["record"]["item_id"] = uid(9)
            elif corruption == "head_identity":
                result["head"]["id"] = uid(9)
            elif corruption == "head_revision":
                result["head"]["catalog_revision"] = 2
            elif corruption == "head_selectable":
                result["head"]["selectable"] = False
            elif corruption == "record_text":
                result["record"]["text"] = "Fabricated result"
            payload = canonical_bytes(result).decode("utf-8")
        store._connection.execute(
            "UPDATE prompt_mutations SET result_payload = ? WHERE idempotency_key = 'create'",
            (payload,),
        )
        if corruption == "digest":
            store._connection.execute(
                "UPDATE prompt_mutations SET digest = 'bad' WHERE idempotency_key = 'create'",
            )
        with pytest.raises(ContractValidationError) as error:
            write(catalog, "item", item(), key="create")
        assert_reason(error, "storage_contract_violation", 500)
        assert catalog.get_revision("item", uid(1), 1) == item()


@pytest.mark.parametrize("corruption", ["missing_item", "bad_effective_override"])
def test_saved_config_with_broken_refs_fails_loudly_on_resolve(tmp_path, corruption):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        seed(catalog)
        if corruption == "missing_item":
            store._connection.execute("DELETE FROM prompt_revisions WHERE kind = 'item'")
        else:
            corrupt = group()
            corrupt["members"][0]["overrides"] = {"placement": "middle"}
            store._connection.execute(
                "UPDATE prompt_revisions SET payload = ? WHERE kind = 'group'",
                (canonical_bytes(corrupt).decode("utf-8"),),
            )
        with pytest.raises(ContractValidationError) as error:
            catalog.resolve_config(uid(3), 1)
        assert_reason(error, "storage_contract_violation", 500)


@pytest.mark.parametrize("enabled,overrides", [
    (True, {}),
    (True, {"placement": "before"}),
    (False, {"placement": "before"}),
])
def test_new_config_cannot_mask_corrupt_saved_group_with_local_overrides(
    tmp_path, enabled, overrides,
):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        seed(catalog)
        corrupt = group()
        corrupt["members"][0]["overrides"] = {"placement": "middle"}
        store._connection.execute(
            "UPDATE prompt_revisions SET payload = ? WHERE kind = 'group'",
            (canonical_bytes(corrupt).decode("utf-8"),),
        )
        request = config(number=4)
        request["inputs"][0].update(enabled=enabled, member_overrides=[{
            "item_instance_id": uid(20), "overrides": overrides,
        }])
        with pytest.raises(ContractValidationError) as error:
            write(catalog, "config", request)
        assert_reason(error, "storage_contract_violation", 500)
        assert catalog.get_revision("config", uid(4), 1) is None
        assert catalog.get_head("config", uid(4)) is None
        assert store._connection.execute(
            "SELECT COUNT(*) FROM prompt_mutations",
        ).fetchone()[0] == 3


def test_nested_mutation_does_not_rollback_callers_transaction(tmp_path):
    with closing(SqliteStore(tmp_path / "workflow.sqlite3")) as store:
        catalog = PromptConfigStore(store)
        store._connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ContractValidationError) as error:
            write(catalog, "item", item())
        assert_reason(error, "storage_contract_violation", 500)
        assert store._connection.in_transaction
        store._connection.execute("ROLLBACK")
