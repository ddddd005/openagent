"""Workflow-owned management operations, separate from global resources."""

from contextlib import closing

from .graph_records import require, uuid_value
from .graph_store import GraphRecordStore


def initialize_session_metadata(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS workflow_session_metadata ("
        "workflow_session_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "metadata_sequence INTEGER NOT NULL)")


class WorkflowLifecycle:
    @staticmethod
    def _session_metadata(repo, sid):
        row = repo.store._connection.execute(
            "SELECT name,metadata_sequence FROM workflow_session_metadata "
            "WHERE workflow_session_id=?", (sid,)).fetchone()
        return {"name": row["name"], "metadata_sequence": row["metadata_sequence"]} if row else {
            "name": "", "metadata_sequence": 0}

    def get_session_metadata(self, sid):
        require(uuid_value(sid), "invalid_request", "Session target must be an exact identity")
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            session = repo.get("workflow_session", workflow_session_id=sid)
            return {"schema_version": 1, "kind": "workflow.session-metadata",
                    "workflow_definition_id": session["workflow_definition_id"],
                    "workflow_session_id": sid, **self._session_metadata(repo, sid)}

    def update_session_metadata(self, sid, *, name, expected_metadata_sequence, idempotency_key):
        require(uuid_value(sid), "invalid_request", "Session target must be an exact identity")
        require(type(name) is str and len(name) <= 128
                and all(ord(char) >= 32 and ord(char) != 127
                        and not 0xD800 <= ord(char) <= 0xDFFF for char in name),
                "invalid_request", "Session name must be bounded text")
        require(type(expected_metadata_sequence) is int
                and 0 <= expected_metadata_sequence < 2**53 - 1,
                "invalid_request", "Session metadata sequence is invalid")
        request = {"session": sid, "name": name,
                   "expected_metadata_sequence": expected_metadata_sequence}

        def change(repo):
            session = repo.get("workflow_session", workflow_session_id=sid)
            self._require_session_active(repo, session)
            require(session["active_chain_run_id"] is None, "active_execution_not_editable",
                    "Session metadata cannot be edited during an unfinished execution", 409)
            current = self._session_metadata(repo, sid)
            require(current["metadata_sequence"] == expected_metadata_sequence,
                    "stale_metadata_sequence", "Session name changed", 409)
            metadata = {"name": name.strip(), "metadata_sequence": expected_metadata_sequence + 1}
            repo.store._connection.execute(
                "INSERT INTO workflow_session_metadata VALUES(?,?,?) "
                "ON CONFLICT(workflow_session_id) DO UPDATE SET "
                "name=excluded.name,metadata_sequence=excluded.metadata_sequence",
                (sid, metadata["name"], metadata["metadata_sequence"]))
            return {"schema_version": 1, "kind": "workflow.session-metadata",
                    "workflow_definition_id": session["workflow_definition_id"],
                    "workflow_session_id": sid, **metadata}

        return self._change("graph.session.metadata.update", idempotency_key, request, change)

    @staticmethod
    def _archived(repo, kind, identity):
        row = repo.store._connection.execute(
            "SELECT archived FROM workflow_management WHERE object_kind=? AND object_id=?",
            (kind, identity)).fetchone()
        return bool(row and row["archived"])

    @staticmethod
    def _archive(repo, kind, identity):
        repo.store._connection.execute(
            "INSERT INTO workflow_management VALUES(?,?,1) "
            "ON CONFLICT(object_kind,object_id) DO UPDATE SET archived=1", (kind, identity))

    def _require_definition_active_in(self, data, identity):
        """Check definition writability through the limited data interface."""
        require(not data.archived("definition", identity), "workflow_archived",
                "Archived workflows are read-only", 409)
        require(identity not in self._management_workflows, "workflow_management_pending",
                "Workflow archive or deletion is in progress", 409)

    def _require_session_active_in(self, data, session):
        self._require_definition_active_in(data, session["workflow_definition_id"])
        sid = session["workflow_session_id"]
        require(not data.archived("session", sid), "session_archived",
                "Archived sessions are read-only", 409)
        require(sid not in self._management_sessions, "session_management_pending",
                "Session archive or deletion is in progress", 409)

    def _require_definition_active(self, repo, identity):
        self._require_definition_active_in(self._configuration_data.transaction(repo), identity)

    def _require_session_active(self, repo, session):
        self._require_session_active_in(self._configuration_data.transaction(repo), session)

    def list_definitions(self, *, include_archived=False):
        # Keep one directory implementation; the fixed-only projection here
        # would silently drop current-configuration composition if it were ever
        # reached instead of GraphWorkflowService.list_definitions.
        return self.configuration_service.list_configuration_definitions(
            include_archived=include_archived)

    def _stop_owned_runs(self, sessions, key):
        chains = [(session["workflow_session_id"], session["active_chain_run_id"])
                  for session in sessions if session["active_chain_run_id"] is not None]
        with self._lock:
            self._pauses.update(chain for _, chain in chains)
        for _, chain in chains:
            self._control_hosted_invocation(chain, "pause", key)
        for _, chain in chains:
            self.wait(chain)

    def _close_owned_runs(self, repo, sessions):
        for original in sessions:
            sid = original["workflow_session_id"]
            session = repo.get("workflow_session", workflow_session_id=sid)
            chain_id = session["active_chain_run_id"]
            if chain_id is None:
                continue
            chain = repo.get("chain_run", chain_run_id=chain_id)
            require(chain["status"] != "running", "execution_stop_unconfirmed",
                    "Execution has not acknowledged stopping; management was not completed", 409)
            chain["status"] = "closed"
            chain["revision"] += 1
            repo.put("chain_run", chain)
            for attempts in chain.get("node_run_attempts", [[item] for item in chain["node_run_ids"]]):
                for run_id in attempts:
                    run = repo.get("node_run", run_id=run_id)
                    if run["status"] not in ("succeeded", "closed", "failed"):
                        run["status"] = "closed"
                        run["revision"] += 1
                        repo.put("node_run", run)
            session["active_chain_run_id"] = None
            session["revision"] += 1
            repo.put("workflow_session", session)
            basis = self._execution_registries.get(chain_id)
            run_registry = basis.registry if basis is not None else None
            if chain.get("execution_kind", "round") == "round":
                self._commit(repo, session, source={"kind": "closed_execution", "chain_run_id": chain_id},
                             registry=run_registry)
            self._save_manifest(repo, "chain_end", chain_id, session, status="closed",
                                registry=run_registry)

    def _delete_owned_sessions(self, repo, sessions):
        connection = repo.store._connection
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        ids = {session["workflow_session_id"] for session in sessions}
        chains = {row["chain_run_id"] for sid in ids
                  for row in repo.rows("chain_run", workflow_session_id=sid)}
        for chain_id in chains:
            dependent = connection.execute(
                "SELECT json_extract(payload,'$.workflow_session_id') FROM records "
                "WHERE record_type='state_snapshot' AND json_valid(payload) AND EXISTS "
                "(SELECT 1 FROM json_each(records.payload,'$.history_refs') WHERE value=?)",
                (chain_id,)).fetchall()
            require(all(row[0] in ids for row in dependent), "session_history_in_use",
                    "Another session still references this historical evidence", 409)
        object_revisions = set()
        for sid in ids:
            from .workflow_chat_runtime import delete_chat_session_records
            delete_chat_session_records(repo, sid)
            for table in ("session_object_bindings", "session_object_revision_membership"):
                object_revisions.update(row[0] for row in connection.execute(
                    f"SELECT revision_id FROM {table} WHERE session_id=?", (sid,)))
            for table in ("program_variable_states", "program_variable_heads", "program_variable_bindings",
                          "session_object_bindings", "session_object_revision_membership",
                          "session_object_receipts", "workflow_runtime_facts"):
                if table in tables:
                    connection.execute(f'DELETE FROM "{table}" WHERE session_id=?', (sid,))
            if "workflow_checkpoints" in tables:
                connection.execute(
                    "DELETE FROM workflow_checkpoints WHERE workflow_session_id=?", (sid,))
            for table, column in (("graph_state_manifests", "payload"),
                                  ("workflow_information_bindings", "owner")):
                if table in tables:
                    connection.execute(f"DELETE FROM {table} WHERE json_extract({column},'$.workflow_session_id')=?",
                                       (sid,))
            connection.execute("DELETE FROM local_editor_drafts WHERE workflow_session_id=?", (sid,))
            connection.execute(
                "DELETE FROM records WHERE json_valid(payload) AND json_extract(payload,'$.workflow_session_id')=?",
                (sid,))
            connection.execute("DELETE FROM workflow_management WHERE object_kind='session' AND object_id=?", (sid,))
            connection.execute("DELETE FROM workflow_session_metadata WHERE workflow_session_id=?", (sid,))
        connection.executemany(
            "DELETE FROM session_object_values WHERE revision_id=? AND revision_id NOT IN "
            "(SELECT revision_id FROM session_object_revision_membership "
            "UNION SELECT revision_id FROM session_object_bindings)",
            [(identity,) for identity in object_revisions])
        # Receipts remain request evidence; they are never replayed to restore deleted product data.

    @staticmethod
    def _require_deletion_resolved(repo, sessions):
        for session in sessions:
            sid = session["workflow_session_id"]
            for chain in repo.rows("chain_run", workflow_session_id=sid):
                diagnostic = chain["diagnostic"] or {}
                require(chain["status"] not in ("archive_failed", "recovery_unavailable")
                        and diagnostic.get("reason_code") not in (
                            "execution_result_unknown", "recovery_unavailable",
                            "result_acceptance_pending", "checkpoint_commit_pending",
                            "model_dispatch_unknown", "host_result_recovery_ambiguous"),
                        "execution_result_unresolved", "Unconfirmed execution evidence must be retained", 409)
            unresolved = repo.store._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_runtime_facts'").fetchone()
            if unresolved:
                unresolved = repo.store._connection.execute(
                    "SELECT 1 FROM workflow_runtime_facts WHERE session_id=? "
                    "AND json_extract(payload,'$.details.classification')='dispatch_unknown' LIMIT 1",
                    (sid,)).fetchone()
            require(not unresolved, "execution_result_unresolved",
                    "Unknown model request evidence must be retained", 409)

    def _manage(self, kind, identity, expected_revision, key, delete):
        require(uuid_value(identity), "invalid_request", "Management target must be an exact identity")
        require(type(expected_revision) is int and expected_revision >= 1,
                "invalid_request", "Management revision must be positive")
        action = "delete" if delete else "archive"
        operation = f"graph.{kind}.{action}"
        request = {("identity" if kind == "definition" else "session"): identity,
                   "expected_revision": expected_revision}
        execution_chains = set()
        known_chains = {}
        with self._lock, closing(self._store()) as store:
            repo = GraphRecordStore(store)
            if store.read_receipt(operation, key) is not None:
                return self._change(operation, key, request, lambda target: None)
            if kind == "definition":
                document = self._document(repo, identity)
                require(document["revision"] == expected_revision, "stale_revision", "Workflow changed", 409)
                sessions = repo.rows("workflow_session", workflow_definition_id=identity)
                marker = self._management_workflows
                workflow_id = identity
            else:
                session = repo.get("workflow_session", workflow_session_id=identity)
                require(session["revision"] == expected_revision, "stale_revision", "Session changed", 409)
                sessions = [session]
                marker = self._management_sessions
                workflow_id = session["workflow_definition_id"]
            if delete:
                execution_chains = {
                    chain["chain_run_id"] for session in sessions
                    for chain in repo.rows("chain_run", workflow_session_id=session["workflow_session_id"])
                }
            require(identity not in marker, "management_pending", "Target management is already in progress", 409)
            marker.add(identity)
        try:
            self._stop_owned_runs(sessions, key)

            def change(repo):
                if delete:
                    self._require_deletion_resolved(repo, sessions)
                self._close_owned_runs(repo, sessions)
                if delete:
                    for original in sessions:
                        chain_id = original["active_chain_run_id"]
                        if chain_id is not None:
                            known_chains[chain_id] = repo.get("chain_run", chain_run_id=chain_id)
                    self._delete_owned_sessions(repo, sessions)
                    if kind == "definition":
                        connection = repo.store._connection
                        configuration = self._configuration_data.transaction(repo)
                        configuration.delete_definition_revisions(identity)
                        connection.execute("DELETE FROM local_editor_drafts WHERE workflow_definition_id=?", (identity,))
                        configuration.delete_current_configuration(identity)
                        connection.execute(
                            "DELETE FROM workflow_management WHERE object_kind='definition' AND object_id=?",
                            (identity,))
                else:
                    self._archive(repo, kind, identity)
                return {"schema_version": 1, "kind": f"workflow.{kind}-management",
                        "workflow_definition_id": workflow_id,
                        **({"workflow_session_id": identity} if kind == "session" else {}),
                        "revision": expected_revision, "archived": not delete, "deleted": delete}

            result = self._change(operation, key, request, change)
            for session in sessions:
                chain_id = session["active_chain_run_id"]
                if chain_id is not None:
                    self._release_public_capabilities(
                        session["workflow_session_id"], chain_id,
                        known_chain=known_chains.get(chain_id) if delete else None)
                    self._pauses.discard(chain_id)
                    self._resource_frames.pop(chain_id, None)
                    self._execution_registries.pop(chain_id, None)
                    self._acceptance_candidates.pop(chain_id, None)
                    self._failed_retry_candidates.pop(chain_id, None)
            if delete:
                with self._lock:
                    for chain_id in execution_chains:
                        self._futures.pop(chain_id, None)
                        self._execution_errors.pop(chain_id, None)
            return result
        finally:
            with self._lock:
                marker.discard(identity)

    def archive_definition(self, identity, *, expected_revision, idempotency_key):
        return self._manage("definition", identity, expected_revision, idempotency_key, False)

    def delete_definition(self, identity, *, expected_revision, idempotency_key):
        return self._manage("definition", identity, expected_revision, idempotency_key, True)

    def archive_session(self, sid, *, expected_revision, idempotency_key):
        return self._manage("session", sid, expected_revision, idempotency_key, False)

    def delete_session(self, sid, *, expected_revision, idempotency_key):
        return self._manage("session", sid, expected_revision, idempotency_key, True)
