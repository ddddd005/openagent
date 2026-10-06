"""Read-only, allowlisted observations of the fixed workflow's selected execution."""

from __future__ import annotations

import copy
import re
from typing import Any

from .workflow_result_ports import ResultPortStore


def project_observation(store, view, bundle, *, bindings: tuple[str, str, str]) -> dict[str, Any]:
    sid = view["workflow_session_id"]
    chains = view["chains"]
    latest = chains[-1] if chains else None
    tail = view["messages"][-1] if view["messages"] else None
    # A completed candidate selection can move Head without creating a new chain.
    chain_id = (
        tail["chain_run_id"]
        if tail and tail["role"] == "assistant" and (latest is None or latest["status"] == "succeeded")
        else latest["chain_run_id"] if latest else None
    )
    chain = next((row for row in bundle.get("chain_run", [])
                  if row["chain_run_id"] == chain_id), None)
    runs = [row for kind in ("run_record", "node_run") for row in bundle.get(kind, [])]
    ports = ResultPortStore(store, allowed_bindings=bindings[:2])
    nodes = []
    for binding, label in zip(bindings, ("A", "B", "Output")):
        run = next((row for row in reversed(runs)
                    if row["node_binding_id"] == binding and row["chain_run_id"] == chain_id), None)
        node = {
            "node_binding_id": binding, "label": label,
            "workflow_session_id": sid, "chain_run_id": chain_id,
            "source_workflow_session_id": run["workflow_session_id"] if run else None,
            "run_id": run["run_id"] if run else None,
            "revision": run["revision"] if run else None,
            "status": run["status"] if run else "idle",
            "diagnostic": None,
            "result": {"availability": "unavailable", "reason_code": "no_result"},
        }
        if run is not None and run.get("profile") == "agent":
            source = next((row for row in view["nodes"] if row["run_id"] == run["run_id"]), None)
            if source and source.get("budget"):
                node["budget"] = copy.deepcopy(source["budget"])
            else:
                facts = store.read_execution_facts(run["run_id"])
                limits = run["initial_budget"]
                extensions = [row["payload"] for row in bundle.get("workflow_operation", [])
                              if row["kind"] == "extend_budget"
                              and row["target"] == {"kind": "run_record", "id": run["run_id"]}]
                node["budget"] = {
                    "max_model_requests": limits["max_model_requests"] + sum(
                        row["additional_model_requests"] for row in extensions),
                    "max_model_attempts": limits.get("max_model_attempts", limits["max_model_requests"] * 4) + sum(
                        row["additional_model_attempts"] for row in extensions),
                    "model_requests": sum(row["kind"] == "model_request" for row in facts),
                    "attempts": sum(row["kind"] == "model_attempt_started" for row in facts),
                }
            facts = store.read_execution_facts(run["run_id"])
            failure = next((row["payload"] for row in reversed(facts)
                            if row["kind"] == "execution_failed"), None)
            if failure and run["status"] in ("failed", "paused", "closed", "recovery_unavailable"):
                node["diagnostic"] = {"code": failure["code"], "category": failure["category"]}
            final = ports.read_port(run["run_id"], "final") if run["status"] == "succeeded" else None
            if final and isinstance(final["value"], dict) and isinstance(final["value"].get("text"), str):
                delivered = (binding == bindings[0] or tail is not None
                             and tail["role"] == "assistant" and tail["chain_run_id"] == chain_id)
                if delivered:
                    node["result"] = {
                        "availability": "available",
                        "text": final["value"]["text"],
                        "output_id": final["source"]["output_id"],
                        "source_run_id": run["run_id"],
                    }
                else:
                    node["result"]["reason_code"] = "not_delivered"
        if run and node["diagnostic"] is None and run["status"] == "recovery_unavailable":
            node["diagnostic"] = {"code": "RECOVERY_UNAVAILABLE", "category": "interrupted"}
        if run and node["result"]["availability"] == "unavailable":
            node["result"]["reason_code"] = (
                "archive_pending" if run["status"] == "final_ready"
                else "run_failed" if run["status"] in ("failed", "closed", "recovery_unavailable")
                else node["result"]["reason_code"]
            )
        nodes.append(node)
    if nodes[2]["status"] == "succeeded" and nodes[1]["result"]["availability"] == "available":
        nodes[2]["result"] = copy.deepcopy(nodes[1]["result"])
    error = view.get("error")
    diagnostic = None
    if isinstance(error, dict) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", str(error.get("code", ""))):
        diagnostic = {"code": error["code"]}
    return {
        "schema_version": 1, "kind": "workflow_observation",
        "workflow_session_id": sid, "session_revision": view["revision"],
        "head_commit_id": view["head_commit_id"], "chain_run_id": chain_id,
        "chain_status": chain["status"] if chain else "idle",
        "diagnostic": diagnostic, "nodes": nodes,
    }
