"""Read complete reply candidates for one formal user input."""

from __future__ import annotations

from typing import Any

from .contract_errors import ContractValidationError


def list_workflow_reply_candidates(
    bundle: dict[str, list[dict[str, Any]]],
    workflow_session_id: str,
    visible_message_id: str,
) -> list[dict[str, str]]:
    """Enumerate a validated bundle's archived candidates in storage order.

    The caller supplies a bundle already checked by ``validate_bundle``. This
    query neither selects a candidate nor freezes membership for a future fork.
    """
    refs = [
        row for row in bundle.get("visible_message_ref", [])
        if row["workflow_session_id"] == workflow_session_id
        and row["visible_message_id"] == visible_message_id
    ]
    if len(refs) != 1 or refs[0]["role"] != "user":
        raise ContractValidationError("Reply candidates require a formal user anchor")
    boundary = refs[0]["boundary"]
    root_id = boundary["chain_run_id"]
    if boundary["input_status"] != "completed" or root_id is None:
        raise ContractValidationError("Reply candidates require a completed user anchor")

    chains = {row["chain_run_id"]: row for row in bundle.get("chain_run", [])}
    root = chains.get(root_id)
    if root is None or root["workflow_session_id"] != workflow_session_id:
        raise ContractValidationError("Reply anchor chain belongs to another session")
    if root["input_id"] != boundary["input_id"] or root["status"] not in ("succeeded", "superseded", "closed"):
        raise ContractValidationError("Reply anchor has no successful original chain")

    descendants = {root_id}
    origins = bundle.get("chain_input_origin", [])
    sessions = {
        row["workflow_session_id"]: row
        for row in bundle.get("workflow_session", [])
    }
    anchors = {
        row["fork_anchor_id"]: row
        for row in bundle.get("fork_anchor", [])
    }
    candidates = bundle.get("workflow_candidate", [])

    def inherits_from(child_session_id: str, ancestor_id: str) -> bool:
        seen = set()
        cursor = child_session_id
        while cursor not in seen and cursor in sessions:
            seen.add(cursor)
            source = sessions[cursor]["source"]
            if source["kind"] != "fork":
                return False
            cursor = source["source_workflow_session_id"]
            if cursor == ancestor_id:
                return True
        return False

    changed = True
    while changed:
        changed = False
        for origin in origins:
            if origin["source_chain_run_id"] not in descendants:
                continue
            if origin["workflow_session_id"] != workflow_session_id:
                child = chains.get(origin["chain_run_id"])
                child_session = sessions.get(origin["workflow_session_id"])
                fork = child_session["source"] if child_session is not None else {}
                anchor = anchors.get(fork.get("fork_anchor_id"))
                frozen = (
                    anchor is not None
                    and anchor["role"] == "assistant"
                    and bool(anchor.get("candidate_floors"))
                    and any(
                        row["chain_run_id"] == origin["source_chain_run_id"]
                        and row["candidate_id"] in anchor["candidate_floors"][-1]["candidate_ids"]
                        for row in candidates
                    )
                )
                if (child is None or child["workflow_session_id"] != origin["workflow_session_id"]
                        or fork.get("kind") != "fork"
                        or not inherits_from(origin["workflow_session_id"], workflow_session_id)
                        or not frozen):
                    raise ContractValidationError(
                        "Reply candidate origin belongs to another session or input"
                    )
                continue  # A frozen descendant's replacement belongs to its own floor.
            child_id = origin["chain_run_id"]
            child = chains.get(child_id)
            if (child is None or child["workflow_session_id"] != workflow_session_id
                    or origin["visible_message_id"] != visible_message_id):
                raise ContractValidationError("Reply candidate origin belongs to another session or input")
            if child_id not in descendants:
                descendants.add(child_id)
                changed = True

    result = []
    root_candidate = False
    for candidate in bundle.get("workflow_candidate", []):
        chain_id = candidate["chain_run_id"]
        if chain_id not in descendants:
            continue
        chain = chains[chain_id]
        if (candidate["workflow_session_id"] != workflow_session_id
                or chain["status"] != "succeeded"
                or chain["output_id"] != candidate["output_id"]):
            raise ContractValidationError("Reply candidate is not a completed result in its session")
        if chain_id == root_id:
            root_candidate = True
        result.append({
            "candidate_id": candidate["candidate_id"],
            "chain_run_id": chain_id,
            "output_id": candidate["output_id"],
        })
    if not root_candidate and root["status"] not in ("superseded", "closed"):
        raise ContractValidationError("Reply anchor has no complete original candidate")
    if not result:
        raise ContractValidationError("Reply anchor has no complete candidate")
    return result
