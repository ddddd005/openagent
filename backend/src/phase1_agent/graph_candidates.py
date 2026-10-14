"""Read-only projections over immutable completed graph commits."""

from contextlib import closing

from .graph_records import require
from .graph_store import GraphRecordStore


class GraphCandidateHost:
    def _selected_chain(self, repo, sid):
        session = repo.get("workflow_session", workflow_session_id=sid)
        if session["active_chain_run_id"] is not None:
            active = repo.get("chain_run", chain_run_id=session["active_chain_run_id"])
            if active.get("execution_kind", "round") == "round":
                return active
        commit_id = self._head(repo, sid)["head_commit_id"]
        seen = set()
        while commit_id is not None:
            require(commit_id not in seen, "storage_contract_violation", "Selected history is cyclic", 500)
            seen.add(commit_id)
            commit = repo.get("workflow_commit", commit_id=commit_id)
            source = commit["source"]
            chain_id = source.get("chain_run_id")
            if chain_id is not None:
                chain = repo.get("chain_run", chain_run_id=chain_id)
                require(chain_id in self._history(repo, sid), "storage_contract_violation", "Selected execution escaped frozen history", 500)
                if (chain["workflow_session_id"] == sid
                        and (chain["workflow_definition_id"], chain["definition_revision"])
                            != (session["workflow_definition_id"], session["definition_revision"])
                        and source.get("kind") != "select_execution_candidate"):
                    return None
                if chain.get("execution_kind", "round") == "round":
                    return chain
                commit_id = commit["parent_commit_id"]
                continue
            if source.get("kind") == "session_seed":
                origin = source.get("source") or {}
                commit_id = (origin.get("candidate_commit_id", origin.get("head_commit_id"))
                             if origin.get("kind") in ("copy_current", "fork_candidate") else None)
            else:
                commit_id = commit["parent_commit_id"]
        return None

    def _candidate_record(self, repo, sid, candidate_id):
        require(type(candidate_id) is str, "invalid_request", "Candidate identity is required")
        commit = repo.get("workflow_commit", commit_id=candidate_id)
        require(commit["source"].get("kind") == "completed_execution",
                "graph_candidate_invalid", "Only a completed execution is a candidate", 409)
        chain = repo.get("chain_run", chain_run_id=commit["source"]["chain_run_id"])
        require(chain["chain_run_id"] in self._history(repo, sid),
                "graph_candidate_scope_mismatch", "Candidate is outside frozen session history", 403)
        require(chain["status"] == "succeeded" and chain.get("execution_kind", "round") == "round"
                and commit["workflow_session_id"] == chain["workflow_session_id"],
                "graph_candidate_invalid", "Candidate has no complete execution", 409)
        snapshot = repo.get("state_snapshot", state_snapshot_id=commit["state_snapshot_id"])
        require((snapshot["workflow_definition_id"], snapshot["definition_revision"])
                == (chain["workflow_definition_id"], chain["definition_revision"]),
                "storage_contract_violation", "Candidate definition differs from its execution", 500)
        return commit, chain, snapshot

    def list_graph_candidates(self, sid):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            repo = GraphRecordStore(store)
            session = repo.get("workflow_session", workflow_session_id=sid)
            visible = set(self._history(repo, sid))
            selected = self._selected_chain(repo, sid)
            candidates = []
            for commit in repo.rows("workflow_commit"):
                if (commit["source"].get("kind") != "completed_execution"
                        or commit["source"].get("chain_run_id") not in visible):
                    continue
                _, chain, snapshot = self._candidate_record(repo, sid, commit["commit_id"])
                candidates.append({"candidate_id": commit["commit_id"], "chain_run_id": chain["chain_run_id"],
                    "source_session_id": chain["workflow_session_id"],
                    "source_workflow_definition_id": chain["workflow_definition_id"],
                    "source_definition_revision": chain["definition_revision"],
                    "data_revision": snapshot["data_revision"],
                    "selected": selected is not None and selected["chain_run_id"] == chain["chain_run_id"],
                    "can_select": False,
                    "diagnostic": None})
            return {"schema_version": 1, "kind": "workflow.graph-candidates", "workflow_session_id": sid,
                    "workflow_definition_id": session["workflow_definition_id"],
                    "definition_revision": session["definition_revision"], "session_revision": session["revision"],
                    "head_revision": self._head(repo, sid)["revision"], "candidates": candidates}
