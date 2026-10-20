"""A failure in the tool host's post-commit independent re-read fails the run.

The variable tool write is already accepted and committed when the host reads
the object back independently to compare it with its receipt.  One injected
failure at exactly that read must fail the run explicitly, keep the accepted
value, revision and receipt, report no successful tool result, and neither
replay nor duplicate the write.
"""

from contextlib import closing

from phase1_agent import tool_package
from phase1_agent.execution_data import ExecutionState

from test_models_service_integration import ModelDatabaseFixture
from test_variable_tool_bindings import (
    KEY, VariableToolTransport, _registered, _run, _service, build_graph,
)


def _events(service, session, chain_id):
    return [fact["payload"] for fact in service.get_run(
        session["workflow_session_id"], chain_id)["runtime_facts"]
        if fact.get("executor_ref") and isinstance(fact.get("payload"), dict)]


def _write_evidence(service, session_id):
    """Accepted tool-write receipts and their object receipts, read directly."""
    with closing(service._store()) as store:
        accepted = store._connection.execute(
            "SELECT COUNT(*) FROM idempotency "
            "WHERE operation='graph.tool.object-write'").fetchone()[0]
        receipts = [row["operation_key"] for row in store._connection.execute(
            "SELECT operation_key FROM session_object_receipts "
            "WHERE session_id=? AND operation_key LIKE 'tool:%'",
            (session_id,)).fetchall()]
    return accepted, receipts


def test_post_commit_reread_failure_keeps_write_without_success_result(tmp_path, monkeypatch):
    counts = {"assign": 0}
    original_assign = tool_package._assign

    def counted_assign(config, inputs, context):
        counts["assign"] += 1
        return original_assign(config, inputs, context)

    monkeypatch.setattr(tool_package, "_assign", counted_assign)
    transport = VariableToolTransport([("score_set", {"value": 10})])
    database = tmp_path / "reread-failure.sqlite"
    with closing(_service(database, transport)) as service:
        ModelDatabaseFixture.write(service, 1)
        document, parts = build_graph(service)
        session = _registered(service, document)
        session_id = session["workflow_session_id"]
        agent_node_id = parts["execute"]["node_binding_id"]
        # Session creation owns revision 1 and the register event revision 2.
        assert session["objects"][KEY]["revision"] == 2

        original = ExecutionState.read_object
        fired, reads = [], []

        def wrapper(self, sid, object_key, *, node_id, revision_id=None):
            if node_id == agent_node_id and not fired:
                # ExecutionState._store is the live SqliteStore of this read session.
                count = self._store._connection.execute(
                    "SELECT COUNT(*) FROM idempotency "
                    "WHERE operation='graph.tool.object-write'").fetchone()[0]
                reads.append(count)
                if count > 0:
                    fired.append(True)
                    raise RuntimeError("injected post-commit reread failure")
            return original(self, sid, object_key, node_id=node_id, revision_id=revision_id)

        monkeypatch.setattr(ExecutionState, "read_object", wrapper)
        chain_id, view = _run(service, session)

        # The preparation-time and pre-execution reads see no accepted tool
        # write; only the re-read after the acceptance commit is failed once.
        assert fired == [True]
        assert reads[-1] == 1 and all(count == 0 for count in reads[:-1]), reads
        # One normal assign at 5 plus one tool set at 10, with no replay.
        assert counts == {"assign": 2}, counts
        assert view["status"] == "failed", view["chains"]
        assert view["chains"][-1]["diagnostic"]["code"] == "tool_contract_error", view["chains"]
        # The normal assignment at 5 owned revision 3; the committed tool write
        # is revision 4 and must survive the failed re-read.
        assert view["objects"][KEY]["value"]["value"] == 10
        assert view["objects"][KEY]["revision"] == 4

        accepted, tool_receipts = _write_evidence(service, session_id)
        assert accepted == 1
        assert len(tool_receipts) == 1

        settled = [event["payload"] for event in _events(service, view, chain_id)
                   if event["kind"] == "tool_settled"]
        bodies = [fact["message"]["blocks"][0].get("content") for fact in settled
                  if isinstance(fact.get("message"), dict)
                  and fact["message"].get("blocks")
                  and isinstance(fact["message"]["blocks"][0], dict)]
        assert not any(isinstance(body, dict) and body.get("value") == 10
                       for body in bodies), bodies
        assert all(fact["outcome"] != "success" for fact in settled), settled
        # The Agent issued its one call; a second model request would be replay.
        assert transport.calls
        assert len(transport.calls) == 1, transport.calls
    with closing(_service(database, transport)) as reopened:
        reopened_session = reopened.get_session(session_id)
        assert reopened_session["objects"][KEY]["value"]["value"] == 10
        assert reopened_session["objects"][KEY]["revision"] == 4
        reopened_accepted, reopened_receipts = _write_evidence(reopened, session_id)
        assert reopened_accepted == 1
        assert reopened_receipts == tool_receipts
