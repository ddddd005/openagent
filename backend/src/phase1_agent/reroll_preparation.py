"""Pure, durable-fact preparation for a completed whole-chain reroll."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .contract_errors import ContractValidationError
from .contract_graph import validate_bundle
from .contracts_v2 import validate_record


@dataclass(frozen=True)
class CompletedRerollOrigin:
    workflow_session_id: str
    source_chain_run_id: str
    source_run_id: str
    source_candidate_id: str
    base_commit_id: str
    base_ref_revision: int
    node_input: dict[str, Any]
    input_snapshot: dict[str, Any]
    stopped_run_id: str | None = None

    def clone_frozen_start(
        self, *, input_id: str, snapshot_id: str, created_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Give a replacement A run new identities without rebuilding its S0."""
        node_input = copy.deepcopy(self.node_input)
        node_input.update(input_id=input_id, created_at=created_at)
        snapshot = copy.deepcopy(self.input_snapshot)
        snapshot.update(
            snapshot_id=snapshot_id, input_id=input_id, created_at=created_at,
            workflow_session_id=self.workflow_session_id,
        )
        return (
            validate_record("node_input", node_input),
            validate_record("input_snapshot", snapshot),
        )


def _frozen_first_run(records: dict[str, list[dict[str, Any]]], chain: dict[str, Any]) -> dict[str, Any]:
    """Continuation's own first run may be B; reroll still uses original A."""
    continuations = {row["chain_run_id"]: row for row in records.get("execution_continuation", [])}
    chains = {row["chain_run_id"]: row for row in records["chain_run"]}
    cursor = chain
    seen: set[str] = set()
    while cursor["chain_run_id"] in continuations:
        chain_id = cursor["chain_run_id"]
        if chain_id in seen:
            raise ContractValidationError("Continuation origin is cyclic")
        seen.add(chain_id)
        cursor = chains[continuations[chain_id]["source_chain_run_id"]]
    if not cursor["node_run_ids"]:
        raise ContractValidationError("Reroll source has no frozen first node")
    return next(row for row in records["run_record"]
                if row["run_id"] == cursor["node_run_ids"][0])


def _user_floor(
    records: dict[str, list[dict[str, Any]]], session_id: str, chain: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    refs = sorted((row for row in records["visible_message_ref"]
                   if row["workflow_session_id"] == session_id), key=lambda row: row["sequence"])
    origin = next((row for row in records.get("chain_input_origin", [])
                   if row["chain_run_id"] == chain["chain_run_id"]), None)
    user = next((row for row in refs if row["role"] == "user"
                 and (row["visible_message_id"] == origin["visible_message_id"]
                      if origin is not None else
                      row["boundary"]["chain_run_id"] == chain["chain_run_id"])), None)
    latest = next((row for row in reversed(refs) if row["role"] == "user"), None)
    if user is None or latest is None or user["visible_message_id"] != latest["visible_message_id"]:
        raise ContractValidationError("Reroll requires the latest user floor")
    return user, refs


def inspect_completed_reroll_origin(
    bundle: dict[str, list[dict[str, Any]]], session_id: str, chain_id: str,
) -> CompletedRerollOrigin:
    """Inspect the selected terminal chain; runtime/tool settlement is a separate gate."""
    records = validate_bundle(bundle)
    chains = [row for row in records.get("chain_run", [])
              if row["workflow_session_id"] == session_id]
    sources = [row for row in chains if row["chain_run_id"] == chain_id]
    if (len(sources) != 1 or sources[0]["status"] != "succeeded"
            or any(row["status"] not in ("succeeded", "superseded", "closed") for row in chains)):
        raise ContractValidationError("Reroll requires a completed, settled session")
    chain = sources[0]
    if chain["schema_version"] != 2:
        raise ContractValidationError("Reroll requires a versioned chain")

    candidates = [row for row in records.get("workflow_candidate", [])
                  if row["workflow_session_id"] == session_id
                  and row["chain_run_id"] == chain_id]
    heads = [row for row in records.get("workflow_ref", [])
             if row["workflow_session_id"] == session_id]
    if (len(candidates) != 1 or len(heads) != 1
            or heads[0]["head_commit_id"] != candidates[0]["result_commit_id"]):
        raise ContractValidationError("Reroll source is not the selected Head result")
    candidate = candidates[0]
    if (candidate["base_commit_id"] != chain["base_commit_id"]
            or chain["base_ref_revision"] >= heads[0]["revision"]):
        raise ContractValidationError("Reroll source has an inconsistent frozen base")

    refs = sorted((row for row in records.get("visible_message_ref", [])
                   if row["workflow_session_id"] == session_id),
                  key=lambda row: row["sequence"])
    selected = next(row for row in records["workflow_commit"]
                    if row["commit_id"] == candidate["result_commit_id"])
    selected_snapshot = next(row for row in records["state_snapshot"]
                             if row["state_snapshot_id"] == selected["state_snapshot_id"])
    selected_refs = selected_snapshot["visible_message_refs"]
    registered = {row["visible_message_id"]: row for row in refs}
    origin = next((row for row in records.get("chain_input_origin", [])
                   if row["chain_run_id"] == chain_id), None)
    user_id = (origin["visible_message_id"] if origin is not None else
               next((row["visible_message_id"] for row in refs
                     if row["role"] == "user"
                     and row["boundary"]["chain_run_id"] == chain_id), None))
    if (user_id is None or len(selected_refs) < 2
            or selected_refs[-2]["role"] != "user"
            or selected_refs[-2]["visible_message_id"] != user_id
            or user_id not in registered
            or selected_refs[-2]["boundary"] != registered[user_id]["boundary"]
            or selected_refs[-2]["boundary"]["input_status"] != "completed"
            or selected_refs[-1]["role"] != "assistant"
            or selected_refs[-1]["visible_message_id"] not in registered
            or selected_refs[-1]["boundary"] != registered[
                selected_refs[-1]["visible_message_id"]]["boundary"]
            or selected_refs[-1]["boundary"]["chain_run_id"] != chain_id
            or selected_refs[-1]["boundary"]["output_id"] != candidate["output_id"]
            or selected_refs[-1]["boundary"]["after_checkpoint_id"] != candidate["checkpoint_id"]):
        raise ContractValidationError("Reroll source has no terminal formal reply")
    if any(
        row["target"] == {"kind": "ui", "workflow_session_id": session_id}
        and row["status"] not in ("succeeded", "failed")
        for row in records.get("output_delivery", [])
    ):
        raise ContractValidationError("Reroll source has an unsettled delivery")
    delivered = [
        row for row in records.get("output_delivery", [])
        if row["output_id"] == candidate["output_id"]
        and row["target"] == {"kind": "ui", "workflow_session_id": session_id}
    ]
    if len(delivered) != 1 or delivered[0]["status"] != "succeeded":
        raise ContractValidationError("Reroll source has no successful UI delivery")

    first_run = _frozen_first_run(records, chain)
    is_continuation = any(row["chain_run_id"] == chain_id
                          for row in records.get("execution_continuation", []))
    if (first_run["workflow_session_id"] != session_id
            or (not is_continuation and (first_run["status"] != "succeeded"
                                        or first_run["input_id"] != chain["input_id"]))):
        raise ContractValidationError("Reroll source has no completed first node")
    original_input = next(row for row in records["node_input"]
                          if row["input_id"] == first_run["input_id"])
    snapshot = next(row for row in records["input_snapshot"]
                    if row["snapshot_id"] == first_run["snapshot_id"])
    if (original_input["source"]["kind"] != "visible_message"
            or original_input["source"]["visible_message_id"] != user_id):
        raise ContractValidationError("Reroll source has no formal user input")
    return CompletedRerollOrigin(
        workflow_session_id=session_id,
        source_chain_run_id=chain_id,
        source_run_id=first_run["run_id"],
        source_candidate_id=candidate["candidate_id"],
        base_commit_id=chain["base_commit_id"],
        base_ref_revision=chain["base_ref_revision"],
        node_input=copy.deepcopy(original_input),
        input_snapshot=copy.deepcopy(snapshot),
    )


def inspect_inherited_reroll_origin(
    bundle: dict[str, list[dict[str, Any]]], session_id: str, chain_id: str,
) -> CompletedRerollOrigin:
    """Inspect a fork's selected, frozen terminal reply without owning its execution."""
    records = validate_bundle(bundle)
    session = next((row for row in records["workflow_session"]
                    if row["workflow_session_id"] == session_id), None)
    if session is None or session["source"]["kind"] != "fork":
        raise ContractValidationError("Reroll source is not a frozen inherited reply")
    anchor = next(row for row in records["fork_anchor"]
                  if row["fork_anchor_id"] == session["source"]["fork_anchor_id"])
    if anchor["role"] != "assistant" or not anchor.get("candidate_floors"):
        raise ContractValidationError("Reroll source is not a frozen inherited reply")
    floor = anchor["candidate_floors"][-1]
    source = next((row for row in records["chain_run"]
                   if row["chain_run_id"] == chain_id), None)
    if source is None or source["workflow_session_id"] == session_id or source["status"] != "succeeded":
        raise ContractValidationError("Reroll source is not a completed inherited chain")
    candidate = next((row for row in records["workflow_candidate"]
                      if row["chain_run_id"] == chain_id
                      and row["candidate_id"] in floor["candidate_ids"]), None)
    if candidate is None:
        raise ContractValidationError("Reroll source is not in the frozen fork floor")
    head = next(row for row in records["workflow_ref"]
                if row["workflow_session_id"] == session_id)
    commit = next(row for row in records["workflow_commit"]
                  if row["commit_id"] == head["head_commit_id"])
    state = next(row for row in records["state_snapshot"]
                 if row["state_snapshot_id"] == commit["state_snapshot_id"])
    refs = state["visible_message_refs"]
    if (len(refs) < 2 or refs[-2]["role"] != "user"
            or refs[-2]["visible_message_id"] != floor["user_visible_message_id"]
            or refs[-1]["role"] != "assistant"
            or refs[-1]["boundary"] != {
                "chain_run_id": chain_id, "output_id": candidate["output_id"],
                "after_checkpoint_id": candidate["checkpoint_id"],
            }):
        raise ContractValidationError("Reroll source is not the terminal selected inherited reply")
    if any(row["workflow_session_id"] == session_id
           and row["status"] not in ("succeeded", "superseded", "closed")
           for row in records["chain_run"]):
        raise ContractValidationError("Reroll requires a completed, settled session")
    if any(row["target"] == {"kind": "ui", "workflow_session_id": session_id}
           and row["status"] not in ("succeeded", "failed")
           for row in records["output_delivery"]):
        raise ContractValidationError("Reroll source has an unsettled delivery")
    source_deliveries = [
        row for row in records["output_delivery"]
        if row["output_id"] == candidate["output_id"]
        and row["target"] == {
            "kind": "ui", "workflow_session_id": source["workflow_session_id"],
        } and row["status"] == "succeeded"
    ]
    if len(source_deliveries) != 1:
        raise ContractValidationError("Reroll source has no successful UI delivery")
    run = _frozen_first_run(records, source)
    node_input = next(row for row in records["node_input"]
                      if row["input_id"] == run["input_id"])
    snapshot = next(row for row in records["input_snapshot"]
                    if row["snapshot_id"] == run["snapshot_id"])
    is_continuation = any(row["chain_run_id"] == chain_id
                          for row in records.get("execution_continuation", []))
    if ((not is_continuation and (run["status"] != "succeeded" or run["input_id"] != source["input_id"]))
            or node_input["source"] != {
                "kind": "visible_message", "visible_message_id": floor["user_visible_message_id"],
            }):
        raise ContractValidationError("Reroll source has no frozen formal user input")
    return CompletedRerollOrigin(
        workflow_session_id=session_id, source_chain_run_id=chain_id,
        source_run_id=run["run_id"], source_candidate_id=candidate["candidate_id"],
        base_commit_id=head["head_commit_id"], base_ref_revision=head["revision"],
        node_input=copy.deepcopy(node_input), input_snapshot=copy.deepcopy(snapshot),
    )


def inspect_interrupted_reroll_origin(
    bundle: dict[str, list[dict[str, Any]]], session_id: str, chain_id: str,
) -> CompletedRerollOrigin:
    """A safely paused A or B can be replaced by a complete fresh A chain."""
    return _inspect_unfinished_reroll_origin(bundle, session_id, chain_id, status="paused")


def inspect_closed_reroll_origin(
    bundle: dict[str, list[dict[str, Any]]], session_id: str, chain_id: str,
) -> CompletedRerollOrigin:
    """A manually closed latest execution remains a durable reroll source."""
    return _inspect_unfinished_reroll_origin(bundle, session_id, chain_id, status="closed")


def _inspect_unfinished_reroll_origin(
    bundle: dict[str, list[dict[str, Any]]], session_id: str, chain_id: str, *, status: str,
) -> CompletedRerollOrigin:
    records = validate_bundle(bundle)
    chains = [row for row in records["chain_run"]
              if row["workflow_session_id"] == session_id]
    source = next((row for row in chains if row["chain_run_id"] == chain_id), None)
    if (source is None or source["schema_version"] != 2
            or source["status"] != status or not source["node_run_ids"]
            or any(row["status"] not in ("succeeded", "superseded", "closed", status) for row in chains)
            or any(row["chain_run_id"] != chain_id and row["status"] == "paused" for row in chains)):
        raise ContractValidationError("Reroll requires a safely paused or closed execution")
    run = _frozen_first_run(records, source)
    stopped = next(row for row in records["run_record"]
                   if row["run_id"] == source["node_run_ids"][-1])
    session = next(row for row in records["workflow_session"]
                   if row["workflow_session_id"] == session_id)
    definition = next(row for row in records["workflow_definition_revision"]
                      if row["workflow_definition_id"] == session["workflow_definition_id"]
                      and row["revision"] == session["definition_revision"])
    no_unfinished_node = status == "closed" and all(
        next(row for row in records["run_record"] if row["run_id"] == run_id)["status"] == "succeeded"
        for run_id in source["node_run_ids"]
    )
    if ((stopped["status"] != status and not no_unfinished_node)
            or stopped["node_binding_id"] not in definition["bindings"][:2]
            or run["node_binding_id"] != definition["bindings"][0]):
        raise ContractValidationError("Reroll requires a safely paused or closed Agent")
    effective_runs = source["node_run_ids"]
    continuation = next((row for row in records.get("execution_continuation", [])
                         if row["chain_run_id"] == chain_id), None)
    if continuation is not None:
        effective_runs = [*continuation["reused_run_ids"], *effective_runs]
    if len(effective_runs) not in (1, 2) or (
        len(effective_runs) == 2 and next(
            row for row in records["run_record"] if row["run_id"] == effective_runs[0]
        )["status"] != "succeeded"
    ):
        raise ContractValidationError("Reroll has no trusted paused A or B boundary")
    if any(row["chain_run_id"] == chain_id
           for row in records.get("workflow_candidate", [])):
        raise ContractValidationError("Reroll source already has a candidate")
    head = next(row for row in records["workflow_ref"]
                if row["workflow_session_id"] == session_id)
    user, refs = _user_floor(records, session_id, source)
    selected = next(row for row in records["workflow_commit"] if row["commit_id"] == head["head_commit_id"])
    state = next(row for row in records["state_snapshot"] if row["state_snapshot_id"] == selected["state_snapshot_id"])
    head_refs = state["visible_message_refs"]
    if head["head_commit_id"] != source["base_commit_id"] and (
        not head_refs or len(head_refs) < 2
        or head_refs[-2]["visible_message_id"] != user["visible_message_id"]
        or head_refs[-1]["role"] != "assistant"
    ):
        raise ContractValidationError("Interrupted input is not based on current Head")
    if (status == "paused" and user["boundary"]["input_status"] not in ("running", "completed")
            or status == "closed" and user["boundary"]["input_status"] not in ("failed", "running", "completed")):
        raise ContractValidationError("Reroll requires the latest unfinished input")
    if any(row["target"] == {"kind": "ui", "workflow_session_id": session_id}
           and row["status"] == "pending"
           for row in records.get("output_delivery", [])):
        raise ContractValidationError("Reroll source has an unsettled delivery")
    node_input = next(row for row in records["node_input"]
                      if row["input_id"] == run["input_id"])
    snapshot = next(row for row in records["input_snapshot"]
                    if row["snapshot_id"] == run["snapshot_id"])
    if node_input["source"] != {
        "kind": "visible_message", "visible_message_id": user["visible_message_id"],
    }:
        raise ContractValidationError("Reroll source has no frozen formal user input")
    return CompletedRerollOrigin(
        workflow_session_id=session_id, source_chain_run_id=chain_id,
        source_run_id=run["run_id"], source_candidate_id="",
        base_commit_id=source["base_commit_id"],
        base_ref_revision=source["base_ref_revision"],
        node_input=copy.deepcopy(node_input), input_snapshot=copy.deepcopy(snapshot),
        stopped_run_id=None if no_unfinished_node else stopped["run_id"],
    )
