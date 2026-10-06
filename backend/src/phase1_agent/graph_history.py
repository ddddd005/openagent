"""Frozen history identity mapping shared by consumers and candidate selection."""

from .graph_records import require


def _document(repo, identity, revision):
    return repo.get("workflow_definition_revision", workflow_definition_id=identity,
                    revision=revision)["document"]


def _map_nodes(mapping, source, target, declarations, compatible_types=True):
    previous = {node["node_binding_id"]: node for node in source["nodes"]}
    explicit = {item["target_node_id"]: item for item in declarations}
    next_ids = {}
    for node in target["nodes"]:
        target_id = node["node_binding_id"]
        item = explicit.get(target_id, {})
        if item.get("action") == "reset":
            continue
        source_id = item.get("source_node_id", target_id)
        old = previous.get(source_id)
        if old and (not compatible_types or (old["component_id"], old["component_version"]) == (
                node["component_id"], node["component_version"])):
            if source_id in mapping:
                next_ids[target_id] = mapping[source_id]
    return next_ids


def _commit_path(repo, head_id):
    result, seen = [], set()
    while head_id is not None:
        require(head_id not in seen, "storage_contract_violation", "Frozen commit history is cyclic", 500)
        seen.add(head_id)
        commit = repo.get("workflow_commit", commit_id=head_id)
        result.append(commit)
        head_id = commit["parent_commit_id"]
    return list(reversed(result))


def _rebind_mapping(repo, mapping, document, head_id, chain_id=None, compatible_types=True, source_document=None):
    original = source_document if source_document is not None else document
    commits = _commit_path(repo, head_id)
    anchors = [index for index, commit in enumerate(commits)
               if commit["source"].get("kind") in ("completed_execution", "closed_execution")
               and commit["source"].get("chain_run_id") == chain_id and chain_id is not None]
    if anchors:
        commits = commits[anchors[-1] + 1:]
    elif chain_id is not None:
        chain = repo.get("chain_run", chain_run_id=chain_id)
        if chain.get("execution_kind", "round") == "event":
            starting_head = chain["base_commit_id"]
            anchors = [index for index, commit in enumerate(commits)
                       if commit["commit_id"] == starting_head]
            if not anchors:
                # A baseline outside this frozen path cannot grant node mapping.
                return {}, document
            commits = commits[anchors[-1] + 1:]
        session = repo.get("workflow_session", workflow_session_id=chain["workflow_session_id"])
        if session["active_chain_run_id"] == chain_id:
            return mapping, document
    for commit in commits:
        source = commit["source"]
        if source.get("kind") not in ("definition_rebind", "select_execution_candidate"):
            continue
        snapshot = repo.get("state_snapshot", state_snapshot_id=commit["state_snapshot_id"])
        target = _document(repo, snapshot["workflow_definition_id"], snapshot["definition_revision"])
        if source["kind"] == "select_execution_candidate":
            mapping = _map_nodes({node["node_binding_id"]: node["node_binding_id"] for node in original["nodes"]},
                                 original, target, [], compatible_types)
        else:
            mapping = _map_nodes(mapping, document, target, source.get("state_mappings", []), compatible_types)
        document = target
    return mapping, document


def history_node_mapping(repo, sid, source_chain, *, compatible_types=True):
    """Map each current target to its original node through frozen copies.

    This grants no history access by itself: callers must first check their
    session's frozen chain references and separately enforce public eligibility.
    """
    source_sid = source_chain["workflow_session_id"]
    cursor, hops, seen = sid, [], set()
    target_head = next(row["head_commit_id"] for row in repo.rows("workflow_ref")
                       if row["workflow_session_id"] == sid)
    while cursor != source_sid:
        require(cursor not in seen, "storage_contract_violation", "Frozen session ancestry is cyclic", 500)
        seen.add(cursor)
        session = repo.get("workflow_session", workflow_session_id=cursor)
        source = session["source"]
        if source.get("kind") not in ("copy_current", "fork_candidate"):
            return {}
        hops.append((session, source, target_head))
        target_head = source["head_commit_id"]
        # A historical fork's parent is a navigation relationship. Its frozen
        # state may come directly from an older ancestor's completion commit.
        head = repo.get("workflow_commit", commit_id=target_head)
        if source["kind"] == "fork_candidate":
            cursor = head["workflow_session_id"]
        else:
            require(head["workflow_session_id"] == source["workflow_session_id"],
                    "storage_contract_violation", "Copied history has another source owner", 500)
            cursor = source["workflow_session_id"]
    document = _document(repo, source_chain["workflow_definition_id"], source_chain["definition_revision"])
    original = document
    mapping = {node["node_binding_id"]: node["node_binding_id"] for node in document["nodes"]}
    mapping, document = _rebind_mapping(repo, mapping, document, target_head, source_chain["chain_run_id"], compatible_types)
    for session, source, head_id in reversed(hops):
        seed = _commit_path(repo, head_id)[0]
        snapshot = repo.get("state_snapshot", state_snapshot_id=seed["state_snapshot_id"])
        target = _document(repo, snapshot["workflow_definition_id"], snapshot["definition_revision"])
        mapping = _map_nodes(mapping, document, target, source.get("state_mappings", []), compatible_types)
        mapping, document = _rebind_mapping(repo, mapping, target, head_id, compatible_types=compatible_types,
                                           source_document=original)
    session = repo.get("workflow_session", workflow_session_id=sid)
    current = _document(repo, session["workflow_definition_id"], session["definition_revision"])
    return _map_nodes(mapping, document, current, [], compatible_types)
