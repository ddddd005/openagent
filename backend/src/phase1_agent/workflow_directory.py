"""Narrow read-only directory projections over the shared workflow SQLite store.

The directory serves two management-scope summaries: the formal workflow
definitions and the formal sessions of one definition.  It projects only:

* the current-configuration scalar columns;
* the latest fixed revision's name and node count, read through SQLite JSON
  paths instead of decoding every historical fixed document;
* the session header row, its metadata row and its archived flag;
* the minimal Head/commit/history references needed to repeat the existing
  session status selection rule.

It never composes a configuration, never calls the full ``_view`` /
``list_definitions`` consumers, and never reads node outputs, private node
state, program variables or session object bodies.  Each caller opens an
independent read transaction on one fresh ``SqliteStore`` connection so one
summary response observes a consistent view.

The list order stays the current explainable one: definitions by the first
appearance of their fixed-revision rows (record rowid), sessions by their row
insertion order.  No new user-visible sorting, paging or persistent counts are
introduced.
"""

from __future__ import annotations

from .contract_json import loads_strict
from .graph_records import require
from .storage import _record_id


_GRAPH = "json_extract(payload,'$.execution_model')='graph'"


class WorkflowDirectoryRead:
    """One consistent read-only projection bound to the caller's transaction."""

    def __init__(self, connection):
        self._connection = connection

    # -- definition summaries -------------------------------------------------

    def definition_summaries(self, *, include_archived=False):
        """Formal workflow definitions with their current/legacy directory state."""
        latest = self._latest_fixed_rows()
        currents = {row["workflow_definition_id"]: row for row in self._current_rows()}
        archived = self._management_flags()
        counts = self._session_counts()
        result = []
        for identity, fixed in latest.items():
            is_archived = archived.get(("definition", identity), False)
            if is_archived and not include_archived:
                continue
            current = currents.get(identity)
            result.append({
                "workflow_definition_id": identity,
                "display_name": current["display_name"] if current is not None else fixed["name"],
                "fixed_definition_revision": (current["fixed_definition_revision"]
                                              if current is not None else fixed["revision"]),
                "edit_sequence": current["edit_sequence"] if current is not None else None,
                "archived": is_archived,
                "node_count": fixed["node_count"],
                "session_count": counts.get(identity, 0),
                "has_current_configuration": current is not None,
            })
        return result

    def _latest_fixed_rows(self):
        """Latest revision name/node count per definition via JSON paths only."""
        latest = {}
        for row in self._connection.execute(
                "SELECT json_extract(payload,'$.workflow_definition_id') AS identity, "
                "json_extract(payload,'$.revision') AS revision, "
                "json_extract(payload,'$.document.name') AS name, "
                "json_array_length(payload,'$.document.nodes') AS node_count "
                "FROM records WHERE record_type='workflow_definition_revision' "
                "AND json_valid(payload) AND " + _GRAPH + " ORDER BY rowid"):
            identity = row["identity"]
            current = latest.get(identity)
            if current is None or row["revision"] > current["revision"]:
                # Existing rows win ties; insertion order keeps first appearance.
                latest[identity] = {"revision": row["revision"], "name": row["name"],
                                    "node_count": row["node_count"]}
        return latest

    def _current_rows(self):
        return [dict(row) for row in self._connection.execute(
            "SELECT workflow_definition_id, edit_sequence, fixed_definition_revision, "
            "display_name FROM workflow_current_configuration ORDER BY rowid")]

    def _session_counts(self):
        return {row["identity"]: row["count"] for row in self._connection.execute(
            "SELECT json_extract(payload,'$.workflow_definition_id') AS identity, "
            "COUNT(*) AS count FROM records WHERE record_type='workflow_session' "
            "AND json_valid(payload) AND " + _GRAPH + " GROUP BY identity")}

    def _management_flags(self):
        return {(row["object_kind"], row["object_id"]): bool(row["archived"])
                for row in self._connection.execute(
                    "SELECT object_kind, object_id, archived FROM workflow_management")}

    # -- session summaries ----------------------------------------------------

    def session_summaries(self, workflow_definition_id, *, include_archived=False):
        """Formal sessions of one definition with the existing derived status."""
        archived = self._management_flags()
        definition_archived = archived.get(("definition", workflow_definition_id), False)
        sessions = self._session_rows(workflow_definition_id)
        metadata = self._session_metadata([row["workflow_session_id"] for row in sessions])
        result = []
        for session in sessions:
            sid = session["workflow_session_id"]
            meta = metadata.get(sid, {"name": "", "metadata_sequence": 0})
            session_archived = bool(archived.get(("session", sid), False)
                                    or definition_archived)
            if session_archived and not include_archived:
                continue
            result.append({
                "workflow_session_id": sid,
                "workflow_definition_id": session["workflow_definition_id"],
                "definition_revision": session["definition_revision"],
                "revision": session["revision"],
                "name": meta["name"],
                "metadata_sequence": meta["metadata_sequence"],
                "archived": session_archived,
                "active_chain_run_id": session["active_chain_run_id"],
                "status": self._session_status(session),
            })
        return result

    def _session_rows(self, workflow_definition_id):
        return [dict(row) for row in self._connection.execute(
            "SELECT json_extract(payload,'$.workflow_session_id') AS workflow_session_id, "
            "json_extract(payload,'$.workflow_definition_id') AS workflow_definition_id, "
            "json_extract(payload,'$.definition_revision') AS definition_revision, "
            "json_extract(payload,'$.revision') AS revision, "
            "json_extract(payload,'$.active_chain_run_id') AS active_chain_run_id "
            "FROM records WHERE record_type='workflow_session' AND json_valid(payload) AND "
            + _GRAPH + " AND json_extract(payload,'$.workflow_definition_id')=? ORDER BY rowid",
            (workflow_definition_id,))]

    def _session_metadata(self, identities):
        if not identities:
            return {}
        placeholders = ",".join("?" for _ in identities)
        return {row["workflow_session_id"]: {
                    "name": row["name"], "metadata_sequence": row["metadata_sequence"]}
                for row in self._connection.execute(
                    "SELECT workflow_session_id, name, metadata_sequence "
                    "FROM workflow_session_metadata WHERE workflow_session_id IN ("
                    + placeholders + ")", tuple(identities))}

    def _session_status(self, session):
        """Repeat the existing view's status selection without reading a full view.

        An active session chain keeps priority, exactly as ``_view`` reports it.
        Otherwise the Head-selected chain decides; the Head walk is the existing
        ``_selected_chain`` rule (frozen history only, event chains skipped,
        inherited/rebind ownership checks kept).  With neither, the session is
        idle.  This deliberately does not report the latest chain of the session.
        """
        sid = session["workflow_session_id"]
        active_id = session["active_chain_run_id"]
        if active_id is not None:
            active = self._chain(active_id)
            if active is not None and active["workflow_session_id"] == sid:
                return active["status"]
        selected = self._selected_chain(session)
        return selected["status"] if selected is not None else "idle"

    def _selected_chain(self, session):
        sid = session["workflow_session_id"]
        head = self._head(sid)
        history = set(self._history(sid, head))
        commit_id = head["head_commit_id"]
        seen = set()
        while commit_id is not None:
            require(commit_id not in seen, "storage_contract_violation",
                    "Selected history is cyclic", 500)
            seen.add(commit_id)
            commit = self._commit(commit_id)
            source = commit["source"]
            chain_id = source.get("chain_run_id")
            if chain_id is not None:
                chain = self._chain(chain_id)
                require(chain is not None, "storage_contract_violation",
                        "Selected history chain is missing", 500)
                require(chain_id in history, "storage_contract_violation",
                        "Selected execution escaped frozen history", 500)
                if (chain["workflow_session_id"] == sid
                        and (chain["workflow_definition_id"], chain["definition_revision"])
                            != (session["workflow_definition_id"],
                                session["definition_revision"])
                        and source.get("kind") != "select_execution_candidate"):
                    return None
                if chain["execution_kind"] == "round":
                    return chain
                commit_id = commit["parent_commit_id"]
                continue
            if source.get("kind") == "session_seed":
                origin = source.get("source") or {}
                commit_id = (origin.get("candidate_commit_id", origin.get("head_commit_id"))
                             if origin.get("kind") in ("copy_current", "fork_candidate")
                             else None)
            else:
                commit_id = commit["parent_commit_id"]
        return None

    def _head(self, sid):
        rows = [dict(row) for row in self._connection.execute(
            "SELECT json_extract(payload,'$.head_commit_id') AS head_commit_id "
            "FROM records WHERE record_type='workflow_ref' AND json_valid(payload) AND "
            + _GRAPH + " AND json_extract(payload,'$.workflow_session_id')=? ORDER BY rowid",
            (sid,))]
        require(len(rows) == 1, "storage_contract_violation",
                "Session must have one Head", 500)
        return rows[0]

    def _history(self, sid, head):
        commit = self._commit(head["head_commit_id"])
        snapshot = self._connection.execute(
            "SELECT json_extract(payload,'$.history_refs') AS history_refs FROM records "
            "WHERE record_type='state_snapshot' AND record_id=?",
            (_record_id("state_snapshot", {"state_snapshot_id": commit["state_snapshot_id"]}),)
        ).fetchone()
        frozen = loads_strict(snapshot["history_refs"]) if snapshot is not None else []
        own = [row[0] for row in self._connection.execute(
            "SELECT json_extract(payload,'$.chain_run_id') FROM records "
            "WHERE record_type='chain_run' AND json_valid(payload) AND " + _GRAPH
            + " AND json_extract(payload,'$.workflow_session_id')=? ORDER BY rowid", (sid,))]
        return list(dict.fromkeys([*frozen, *own]))

    def _commit(self, commit_id):
        row = self._connection.execute(
            "SELECT json_extract(payload,'$.parent_commit_id') AS parent_commit_id, "
            "json_extract(payload,'$.state_snapshot_id') AS state_snapshot_id, "
            "json_extract(payload,'$.source') AS source FROM records "
            "WHERE record_type='workflow_commit' AND record_id=?",
            (_record_id("workflow_commit", {"commit_id": commit_id}),)).fetchone()
        require(row is not None, "storage_contract_violation",
                "Selected history commit is missing", 500)
        source = loads_strict(row["source"])
        require(type(source) is dict, "storage_contract_violation",
                "Selected history commit source is invalid", 500)
        return {"parent_commit_id": row["parent_commit_id"],
                "state_snapshot_id": row["state_snapshot_id"], "source": source}

    def _chain(self, chain_id):
        row = self._connection.execute(
            "SELECT json_extract(payload,'$.workflow_session_id') AS workflow_session_id, "
            "json_extract(payload,'$.workflow_definition_id') AS workflow_definition_id, "
            "json_extract(payload,'$.definition_revision') AS definition_revision, "
            "COALESCE(json_extract(payload,'$.execution_kind'),'round') AS execution_kind, "
            "json_extract(payload,'$.status') AS status FROM records "
            "WHERE record_type='chain_run' AND record_id=?",
            (_record_id("chain_run", {"chain_run_id": chain_id}),)).fetchone()
        return dict(row) if row is not None else None
