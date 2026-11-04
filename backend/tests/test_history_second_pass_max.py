"""S4-02/Q08: the fixed-revision second history pass becomes a SQL MAX.

The save path read one workflow's whole revision history twice: the boolean
``existing`` read in ``workflow_configuration.py`` (first pass, per-record
validation) and the maximum-revision read inside ``save_fixed_revision``
(second pass).  Q08 keeps the first pass unchanged and only replaces the second
one for callers that already completed the first pass on the same identity
inside the same transaction.  These tests pin the call-path scope, the exact
skipped-record filter semantics and the unchanged save/rollback results.
"""

from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.storage import _record_id

from test_workflow_configuration import create_session, document, registry, update


class HistoryScans:
    """Count the full history reads that both passes use.

    Counts calls and validated rows of
    ``ConfigurationRepository.definition_revisions`` exactly like the S4-00
    baseline harness, so one save yields one call per remaining full scan.
    """

    def __init__(self):
        self.calls = 0
        self.rows = 0

    def __enter__(self):
        from phase1_agent import configuration_data
        self._module = configuration_data
        self._original = configuration_data.ConfigurationRepository.definition_revisions
        counter = self

        def counted(repository, identity):
            counter.calls += 1
            values = counter._original(repository, identity)
            counter.rows += len(values)
            return values

        configuration_data.ConfigurationRepository.definition_revisions = counted
        return self

    def __exit__(self, *exc_info):
        self._module.ConfigurationRepository.definition_revisions = self._original


@pytest.fixture
def service(tmp_path):
    from phase1_agent.graph_service import GraphWorkflowService
    instance = GraphWorkflowService(tmp_path / "second-pass.sqlite", registry=registry())
    try:
        yield instance
    finally:
        instance.close()


def fixed_document():
    return document(mode="fixed")


def build_history(service, revisions):
    """Create a real fixed-revision history; returns the latest save result."""
    saved = update(service, fixed_document())
    for index in range(1, revisions):
        proposed = deepcopy(saved["document"])
        proposed["nodes"][0]["config"]["text"] = f"revision-{index}"
        saved = update(service, proposed, saved["edit_sequence"])
        assert saved["outcome"] == "saved", saved
        assert saved["definition_revision"] == index + 1, saved
    return saved


def fixed_edit(saved):
    proposed = deepcopy(saved["document"])
    proposed["nodes"][0]["config"]["text"] = "next fixed revision"
    return proposed


def append_record(service, record_id, payload):
    """Append one raw revision row to the isolated test database."""
    connection = sqlite3.connect(service.database)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO records(record_type,record_id,payload,immutable,created_at) "
            "VALUES('workflow_definition_revision',?,?,1,'2026-11-04T00:00:00.000Z')",
            (record_id, payload))
        connection.commit()
    finally:
        connection.close()


def count_rows(service, query, parameters=()):
    with closing(service._store()) as store:
        return store._connection.execute(query, parameters).fetchone()[0]


# -- first pass stays, second pass disappears on the fixed advance -------------

def test_display_only_only_reads_history_once(service):
    saved = build_history(service, 6)
    proposed = deepcopy(saved["document"])
    proposed["name"] = "renamed after Q08"
    with HistoryScans() as scans:
        result = update(service, proposed, saved["edit_sequence"])
    assert result["outcome"] == "saved"
    assert result["changed_categories"] == ["display"]
    assert result["definition_revision"] == 6
    assert (scans.calls, scans.rows) == (1, 6)


def test_fixed_advance_drops_second_full_scan(service):
    saved = build_history(service, 6)
    with HistoryScans() as scans:
        result = update(service, fixed_edit(saved), saved["edit_sequence"])
    assert result["outcome"] == "saved"
    assert result["changed_categories"] == ["fixed"]
    assert result["definition_revision"] == 7
    # Only the unchanged first pass scans the history; the max is read by SQL.
    assert (scans.calls, scans.rows) == (1, 6)


def test_unchanged_save_only_reads_history_once(service):
    saved = build_history(service, 5)
    with HistoryScans() as scans:
        result = update(service, deepcopy(saved["document"]), saved["edit_sequence"])
    assert result["outcome"] == "unchanged"
    assert (scans.calls, scans.rows) == (1, 5)


def test_new_workflow_write_uses_the_validated_empty_history(service):
    value = fixed_document()
    with HistoryScans() as scans:
        saved = update(service, value)
    assert saved["outcome"] == "saved"
    assert saved["definition_revision"] == 1
    assert (scans.calls, scans.rows) == (1, 0)


# -- call paths without a first pass keep the original full scan ---------------

def test_derived_target_write_keeps_the_original_full_scan(service):
    saved = build_history(service, 4)
    create_session(service, saved)
    changed = fixed_edit(saved)
    asked = update(service, changed, saved["edit_sequence"])
    assert asked["outcome"] == "confirmation_required"
    with HistoryScans() as scans:
        derived = update(service, changed, saved["edit_sequence"], confirm=True)
    assert derived["outcome"] == "derived"
    assert derived["definition_revision"] == 1
    # The source first pass plus the untouched scan of the brand-new target.
    assert (scans.calls, scans.rows) == (2, 4)


def test_copy_target_write_uses_the_validated_empty_history(service):
    saved = build_history(service, 3)
    application = GraphApplication(service)
    request = {
        "source_workflow_definition_id": saved["workflow_definition_id"],
        "expected_source_definition_revision": 3,
        "expected_source_edit_sequence": saved["edit_sequence"],
        "target_workflow_definition_id": "00000000-0000-4000-8000-0000000000aa",
        "target_name": "Copied after Q08",
        "idempotency_key": "copy-second-pass",
    }
    with HistoryScans() as scans:
        copied = application.command("definition.copy", request)["result"]
    assert copied["outcome"] == "copied"
    assert copied["definition_revision"] == 1
    # The target conflict check is the first pass; no second full scan follows.
    assert (scans.calls, scans.rows) == (1, 0)


def test_legacy_adapter_write_still_scans_because_it_has_no_first_pass(service):
    value = fixed_document()
    with HistoryScans() as scans:
        service._change(
            "test.fixture.definition.save", "legacy-second-pass",
            {"document": deepcopy(value), "expected_revision": 0},
            lambda repo: service._save_definition(repo, value, 0))
    assert (scans.calls, scans.rows) == (1, 0)


# -- SQL MAX filter semantics match the validated read ------------------------

def test_sql_max_matches_validated_rows_and_skips_corrupt_high_revisions(service, tmp_path):
    saved = build_history(service, 3)
    identity = saved["workflow_definition_id"]
    other = str(uuid4())
    # Invalid JSON, a valid JSON non-graph payload and another workflow's record
    # all carry revisions above the real maximum; none may advance the number.
    append_record(service, "corrupt:invalid-json",
                  '{"workflow_definition_id": "' + identity + '", "revision": 99, ')
    append_record(service, "corrupt:not-graph", json.dumps(
        {"workflow_definition_id": identity, "revision": 88, "payload_note": "not a graph record"}))
    append_record(service, "corrupt:other-workflow", json.dumps(
        {"schema_version": 2, "execution_model": "graph", "workflow_definition_id": other,
         "revision": 77, "bindings": [], "edges": [], "document": {}}))
    with closing(service._store()) as store:
        records = GraphRecordStore(store)
        sql_max = records.max_definition_revision(identity)
        validated = records.rows("workflow_definition_revision",
                                 workflow_definition_id=identity)
        assert sql_max == 3
        assert sql_max == max(row["revision"] for row in validated)
        assert [row["revision"] for row in validated] == [1, 2, 3]
        # The corrupt rows are still stored; only this optimisation reads max.
        assert store._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='workflow_definition_revision'"
            ).fetchone()[0] == 6
    with HistoryScans() as scans:
        result = update(service, fixed_edit(saved), saved["edit_sequence"])
    assert result["outcome"] == "saved"
    assert result["definition_revision"] == 4
    assert (scans.calls, scans.rows) == (1, 3)


def test_corrupt_graph_record_still_fails_the_unchanged_first_pass(service):
    saved = build_history(service, 2)
    identity = saved["workflow_definition_id"]
    append_record(service, "corrupt:graph-bad-fields", json.dumps(
        {"schema_version": 2, "execution_model": "graph",
         "workflow_definition_id": identity, "revision": 99}))
    before_rows = count_rows(service, "SELECT COUNT(*) FROM records "
                                        "WHERE record_type='workflow_definition_revision'")
    before_current = count_rows(
        service, "SELECT edit_sequence FROM workflow_current_configuration WHERE "
                 "workflow_definition_id=?", (identity,))
    with pytest.raises(ContractValidationError) as error:
        update(service, fixed_edit(saved), saved["edit_sequence"])
    assert error.value.reason_code == "storage_contract_violation"
    assert error.value.status_code == 500
    assert count_rows(service, "SELECT COUNT(*) FROM records "
                               "WHERE record_type='workflow_definition_revision'") == before_rows
    assert count_rows(service, "SELECT edit_sequence FROM workflow_current_configuration WHERE "
                               "workflow_definition_id=?", (identity,)) == before_current
    assert service.get_definition(identity)["revision"] == 2


def test_second_pass_max_still_guards_against_a_valid_higher_revision(service):
    saved = build_history(service, 3)
    identity = saved["workflow_definition_id"]
    with closing(service._store()) as store:
        source = json.loads(store._connection.execute(
            "SELECT payload FROM records WHERE record_type='workflow_definition_revision' "
            "AND json_extract(payload,'$.workflow_definition_id')=? "
            "AND json_extract(payload,'$.revision')=?", (identity, 3)).fetchone()["payload"])
    ahead = deepcopy(source)
    ahead["revision"] = 5
    ahead["document"]["revision"] = 5
    append_record(service, _record_id("workflow_definition_revision", ahead),
                  json.dumps(ahead))
    before_rows = count_rows(service, "SELECT COUNT(*) FROM records "
                                      "WHERE record_type='workflow_definition_revision'")
    with pytest.raises(ContractValidationError) as error:
        update(service, fixed_edit(saved), saved["edit_sequence"])
    assert error.value.reason_code == "stale_revision"
    assert error.value.status_code == 409
    # The failed command wrote nothing: no revision 4 and no configuration move.
    assert count_rows(service, "SELECT COUNT(*) FROM records "
                               "WHERE record_type='workflow_definition_revision'") == before_rows
    assert service.get_definition(identity)["revision"] == 3
    assert count_rows(service, "SELECT edit_sequence FROM workflow_current_configuration WHERE "
                               "workflow_definition_id=?", (identity,)) == saved["edit_sequence"]
