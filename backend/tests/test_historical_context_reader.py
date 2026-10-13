"""Complete native history lineage, without current reads or historical rewrites."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.historical_artifact_reader import readonly_history_connection
from phase1_agent.historical_context_reader import (
    prove_historical_native_context, validate_historical_context_object,
)
from phase1_agent.storage import _record_id
from historical_context_test_support import NativeHistoryFixture, artifact
from test_checkpoint_history_integration import transaction
from test_historical_artifact_reader import read_record, write_record


@pytest.fixture
def native_history(tmp_path):
    fixture = NativeHistoryFixture(tmp_path)
    try:
        yield fixture
    finally:
        fixture.close()


def persistent_rows(connection):
    tables = ("records", "global_resource_current", "workflow_runtime_facts",
              "workflow_checkpoints", "workflow_history_membership",
              "session_object_bindings", "session_object_values",
              "session_object_revision_membership", "session_object_receipts")
    return {table: sorted(tuple(row) for row in connection.execute("SELECT * FROM " + table))
            for table in tables}


def test_every_read_and_merge_proves_real_offline_sql_facts_without_writes(native_history, monkeypatch):
    fixture = native_history
    assert len(fixture.rounds) == 2
    assert fixture.rounds[1]["read"]["derivation"]["input_view_ref"] == artifact(fixture.rounds[0]["merge_id"])
    assert fixture.rounds[1]["view"]["generation"] == 1
    assert len(fixture.rounds[1]["packet"]["receipts"]["model_requests"]) == 2
    assert [item["kind"] for item in fixture.rounds[0]["view"]["layout"]] == ["once", "history", "history"]
    assert "ONCE_MATERIAL" in str(fixture.rounds[0]["view"]["messages"])
    # Compaction is not special: once the source text is gone, the same source re-enters.
    assert not any(item["kind"] == "once" for item in fixture.rounds[1]["view"]["layout"])
    assert fixture.rounds[1]["prompt"]["layout"][1]["kind"] == "once"
    assert "ONCE_MATERIAL" in str(fixture.rounds[1]["prompt"]["messages"])
    assert sum(operation["kind"] == "compact" for operation in fixture.rounds[1]["packet"]["operations"]) == 1
    resources = fixture.host.host.resources
    resources.profile["update_sequence"] += 1
    resources.profile["value"]["runtime"].update(output_limit_tokens=1500, context_length_tokens=9000)
    resources.save(resources.profile)
    before = persistent_rows(fixture.connection)
    counts = (fixture.host.host.read_count, len(fixture.host.mock.communications),
              len(fixture.host.mock.broker.freeze_calls))
    proven = []
    import phase1_agent.historical_context_reader as reader_module
    original = reader_module.read_context_update_facts

    def prove_actual_facts(connection, authority, reference):
        result = original(connection, authority, reference)
        proven.append(reference["output_id"])
        assert result["executor_facts"] and result["model_facts"]["requests"]
        return result

    monkeypatch.setattr(reader_module, "read_context_update_facts", prove_actual_facts)
    statements = []
    with readonly_history_connection(fixture.path) as connection:
        connection.set_trace_callback(statements.append)
        authority = fixture.reader(connection)
        view = prove_historical_native_context(authority, artifact(fixture.rounds[-1]["merge_id"]))
        assert view == fixture.rounds[-1]["view"] and view is not fixture.rounds[-1]["view"]
        view["messages"].clear()
        checked = validate_historical_context_object(
            authority, fixture.rounds[-1]["record_after"]["value"], object_key="context")
        assert checked == fixture.rounds[-1]["record_after"]["value"]
    assert proven == [value["update_id"] for value in fixture.rounds] * 2
    assert persistent_rows(fixture.connection) == before
    assert counts == (fixture.host.host.read_count, len(fixture.host.mock.communications),
                      len(fixture.host.mock.broker.freeze_calls))
    assert not any(statement.lstrip().upper().startswith((
        "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE"))
        for statement in statements)


@pytest.mark.parametrize("layer", [0, 1])
@pytest.mark.parametrize("fault", ["executor_missing", "executor_sequence", "model_missing",
                                 "model_sequence", "final_settlement"])
def test_missing_or_tampered_facts_at_any_merge_depth_are_rejected(native_history, layer, fault):
    fixture = native_history
    selected = fixture.rounds[layer]
    packet = selected["packet"]
    with transaction(fixture.connection):
        if fault.startswith("executor"):
            identity = packet["receipts"]["fact_ids"][0]
        elif fault.startswith("model"):
            identity = packet["receipts"]["model_requests"][0]["fact_ids"][-1]
        else:
            rows = fixture.connection.execute(
                "SELECT fact_id,payload FROM workflow_runtime_facts WHERE stream_id LIKE 'executor:%' "
                "AND chain_id=?", (selected["chain"],)).fetchall()
            identity, body = next(
                (row["fact_id"], loads_strict(row["payload"])) for row in rows
                if loads_strict(row["payload"])["payload"]["kind"] == "tool_settled")
            body["payload"]["payload"]["message"]["blocks"][0]["content"] = {"text": "Forged final"}
            fixture.connection.execute(
                "UPDATE workflow_runtime_facts SET payload=? WHERE fact_id=?",
                (canonical_bytes(body).decode(), identity))
        if fault.endswith("missing"):
            fixture.connection.execute("DELETE FROM workflow_runtime_facts WHERE fact_id=?", (identity,))
        elif fault.endswith("sequence"):
            fixture.connection.execute(
                "UPDATE workflow_runtime_facts SET sequence=sequence+50 WHERE fact_id=?", (identity,))
    with readonly_history_connection(fixture.path) as connection:
        authority = fixture.reader(connection)
        with pytest.raises(ContractValidationError):
            prove_historical_native_context(authority, artifact(fixture.rounds[-1]["merge_id"]))


@pytest.mark.parametrize("layer", [0, 1])
def test_history_membership_is_required_even_for_deep_prior_merge(native_history, layer):
    fixture = native_history
    with transaction(fixture.connection):
        fixture.connection.execute(
            "DELETE FROM workflow_history_membership WHERE workflow_session_id=? AND chain_run_id=?",
            (fixture.sid, fixture.rounds[layer]["chain"]))
    with readonly_history_connection(fixture.path) as connection:
        with pytest.raises(ContractValidationError) as error:
            fixture.reader(connection)
        assert error.value.reason_code == "history_membership_denied"


@pytest.mark.parametrize("fault", ["read_revision", "read_head", "merge_revision",
                                 "membership", "body_pointer"])
def test_exact_original_object_read_basis_is_required(native_history, fault):
    fixture = native_history
    selected = fixture.rounds[1]
    with transaction(fixture.connection):
        if fault in ("read_revision", "read_head", "merge_revision"):
            run_id = selected["merge_run"] if fault == "merge_revision" else selected["read_run"]
            run = read_record(fixture.connection, "node_run", run_id)
            run["reads"][0]["revision" if fault == "read_head" else "revision_id"] = (
                99 if fault == "read_head" else str(uuid4()))
            write_record(fixture.connection, "node_run", run)
        elif fault == "membership":
            fixture.connection.execute(
                "DELETE FROM session_object_revision_membership "
                "WHERE session_id=? AND object_key='context' AND revision_id=?",
                (fixture.sid, selected["record_before"]["revision_id"]))
        else:
            fixture.connection.execute(
                "UPDATE session_object_values SET value=? WHERE revision_id=?",
                (canonical_bytes({"view_ref": None, "accepted_delta_ids": []}).decode(),
                 selected["record_before"]["revision_id"]))
    with readonly_history_connection(fixture.path) as connection:
        authority = fixture.reader(connection)
        with pytest.raises(ContractValidationError):
            prove_historical_native_context(authority, artifact(selected["merge_id"]))


def test_cyclic_lineage_is_rejected_with_no_write_attempt(native_history):
    fixture = native_history
    selected = fixture.rounds[0]
    with transaction(fixture.connection):
        output = read_record(fixture.connection, "workflow_output", selected["read_id"])
        output["payload"]["derivation"]["input_view_ref"] = artifact(fixture.rounds[-1]["merge_id"])
        write_record(fixture.connection, "workflow_output", output)
    with readonly_history_connection(fixture.path) as connection:
        authority = fixture.reader(connection)
        with pytest.raises(ContractValidationError) as error:
            prove_historical_native_context(authority, artifact(fixture.rounds[-1]["merge_id"]))
        assert error.value.reason_code == "history_context_lineage_cycle"


def test_same_session_membership_does_not_authorize_unrelated_artifact(native_history):
    fixture = native_history
    with readonly_history_connection(fixture.path) as connection:
        authority = fixture.reader(connection)
        with pytest.raises(ContractValidationError) as error:
            prove_historical_native_context(authority, artifact(fixture.rounds[-1]["unrelated_id"]))
        assert error.value.reason_code == "history_artifact_denied"


@pytest.mark.parametrize("fault", ["once_source", "generation", "compacted_messages", "consumed"])
def test_read_cannot_revert_compaction_generation_or_consumption(native_history, fault):
    fixture = native_history
    target = fixture.fork_target()
    with transaction(fixture.connection):
        output = read_record(fixture.connection, "workflow_output", target["read_id"])
        value = output["payload"]
        if fault == "once_source":
            value["messages"][0]["blocks"][0]["text"] = "FORGED_ONCE_MATERIAL"
        elif fault == "generation":
            value["generation"] = 0
        elif fault == "compacted_messages":
            value["messages"] = deepcopy(fixture.rounds[0]["view"]["messages"])
            value["layout"] = deepcopy(fixture.rounds[0]["view"]["layout"])
        else:
            value["accepted_delta_ids"].pop()
        write_record(fixture.connection, "workflow_output", output)
    with readonly_history_connection(fixture.path) as connection:
        authority = fixture.reader(connection, target["checkpoint"], workflow_session_id=target["sid"])
        with pytest.raises(ContractValidationError):
            prove_historical_native_context(authority, artifact(target["read_id"]))


def test_target_owned_read_keeps_original_lineage_after_source_session_removal(native_history):
    fixture = native_history
    target = fixture.fork_target()
    with transaction(fixture.connection):
        fixture.connection.execute(
            "DELETE FROM records WHERE record_type='workflow_session' AND record_id=?", (fixture.sid,))
        fixture.connection.execute("DELETE FROM session_object_bindings WHERE session_id=?", (fixture.sid,))
    before = persistent_rows(fixture.connection)
    with readonly_history_connection(fixture.path) as connection:
        authority = fixture.reader(connection, target["checkpoint"], workflow_session_id=target["sid"])
        view = prove_historical_native_context(authority, artifact(target["read_id"]))
        assert view == target["view"]
        assert view["owner"]["workflow_session_id"] == target["sid"]
        source = authority.resolve_detail(artifact(fixture.rounds[0]["update_id"]))
        assert source["producer"]["workflow_session_id"] == fixture.sid
        assert source["value"]["receipts"]["owner"]["workflow_session_id"] == fixture.sid
    assert persistent_rows(fixture.connection) == before


@pytest.mark.parametrize("fault", ["service", "object_access", "reader_grant"])
def test_native_reader_requires_exact_original_frozen_grants(native_history, fault):
    fixture = native_history
    with transaction(fixture.connection):
        if fault != "reader_grant":
            identity = _record_id("node_definition", {"component_id": "context.output", "component_version": "4"})
            record = read_record(fixture.connection, "node_definition", identity)
            record["descriptor"]["service_requirements" if fault == "service" else "object_accesses"] = []
            write_record(fixture.connection, "node_definition", record)
        else:
            identity = _record_id("workflow_definition_revision", {
                "workflow_definition_id": fixture.workflow, "revision": 1})
            record = read_record(fixture.connection, "workflow_definition_revision", identity)
            record["document"]["object_bindings"][0]["readers"].remove(fixture.nodes["read"])
            write_record(fixture.connection, "workflow_definition_revision", record)
            fixture.connection.execute(
                "UPDATE session_object_bindings SET binding=? WHERE session_id=? AND object_key='context'",
                (canonical_bytes(record["document"]["object_bindings"][0]).decode(), fixture.sid))
    with readonly_history_connection(fixture.path) as connection:
        authority = fixture.reader(connection)
        with pytest.raises(ContractValidationError):
            prove_historical_native_context(authority, artifact(fixture.rounds[-1]["merge_id"]))
