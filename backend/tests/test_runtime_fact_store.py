"""Runtime facts have one durable authority and do not need receipt body copies."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.runtime_fact_store import RuntimeFactStore
from phase1_agent.storage import SqliteStore


def uid():
    return str(uuid4())


def owner():
    return dict(zip(("workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"),
                    [uid() for _ in range(4)]))


def test_fact_commit_duplicate_and_reopen_share_one_immutable_row(tmp_path):
    identity = owner()
    fact = {"fact_id": "dispatch", "sequence": 1, "payload": {"action": "model"}}
    path = tmp_path / "facts.sqlite"
    with closing(SqliteStore(path)) as store:
        facts = RuntimeFactStore(store)
        store._connection.execute("BEGIN IMMEDIATE")
        assert facts.accept(owner=identity, stream_id="plugin@1", fact=fact) == {"fact_id": "dispatch"}
        assert facts.accept(owner=identity, stream_id="plugin@1", fact=fact) == {"fact_id": "dispatch"}
        store._connection.execute("COMMIT")
        assert store._connection.execute("SELECT COUNT(*) FROM workflow_runtime_facts").fetchone()[0] == 1
        assert store._connection.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == 0
    with closing(SqliteStore(path)) as store:
        assert RuntimeFactStore(store).list(identity["chain_run_id"]) == [fact]


def test_fact_stream_is_scoped_to_invocation_and_rejects_conflicting_or_skipped_facts(tmp_path):
    first, second = owner(), owner()
    fact = {"fact_id": "dispatch", "sequence": 1, "payload": {"action": "tool"}}
    with closing(SqliteStore(tmp_path / "streams.sqlite")) as store:
        facts = RuntimeFactStore(store)
        store._connection.execute("BEGIN IMMEDIATE")
        facts.accept(owner=first, stream_id="plugin@1", fact=fact)
        facts.accept(owner=second, stream_id="plugin@1", fact=fact)
        changed = deepcopy(fact)
        changed["payload"]["action"] = "different"
        with pytest.raises(ContractValidationError):
            facts.accept(owner=first, stream_id="plugin@1", fact=changed)
        with pytest.raises(ContractValidationError):
            facts.accept(owner=first, stream_id="plugin@1",
                         fact={"fact_id": "outcome", "sequence": 3, "payload": {}})
        store._connection.execute("ROLLBACK")
        assert facts.list(first["chain_run_id"]) == []


def test_fact_storage_fault_rolls_back_without_authorizing_dispatch(tmp_path):
    def fault(stage):
        if stage == "after_runtime_fact_write":
            raise OSError("simulated storage failure")

    with closing(SqliteStore(tmp_path / "failure.sqlite", fault_injector=fault)) as store:
        facts = RuntimeFactStore(store)
        store._connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(OSError):
            facts.accept(owner=owner(), stream_id="plugin@1",
                         fact={"fact_id": "dispatch", "sequence": 1, "payload": {}})
        store._connection.execute("ROLLBACK")
        assert store._connection.execute("SELECT COUNT(*) FROM workflow_runtime_facts").fetchone()[0] == 0
