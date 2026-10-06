"""Evidenced same-process restart, with failure policy owned by node packages."""

from copy import deepcopy

from .graph_records import graph_record, require
from .program_variable_store import ProgramVariableStore


class GraphFailedRetry:
    def _failed_retry_basis(self, repo, chain):
        sid = chain["workflow_session_id"]
        return {"definition": [chain["workflow_definition_id"], chain["definition_revision"]],
                "inputs": deepcopy(chain["inputs"]), "outputs": deepcopy(chain["outputs"]),
                "completed_nodes": list(chain["completed_nodes"]),
                "node_run_ids": list(chain["node_run_ids"]), "next_node_index": chain["next_node_index"],
                "state": ProgramVariableStore(repo.store).current(sid),
                "private": self._private(repo, sid), "objects": self._objects(repo).current(sid)}

    def _validate_failed_retry_candidate(self, chain_id, candidate, *, authorize=False):
        if candidate["service_ref"] is None:
            return
        from .host_sdk import ServiceReference
        reference = ServiceReference.from_dict(candidate["service_ref"])
        frame = self._service_runs.get(chain_id)
        routes = frame.routes.get(candidate["owner"]["node_binding_id"], {}) if frame else {}
        instance = frame.instances.get(reference) if frame and not frame.released else None
        require(any(ref == reference for ref, _ in routes.values()) and instance is not None
                and callable(getattr(instance, "validate_failed_retry", None)),
                "failed_retry_unavailable", "Original declared service is unavailable", 409)
        require(instance.validate_failed_retry(
            deepcopy(candidate["owner"]), deepcopy(candidate["evidence"]), authorize=authorize) is True,
            "failed_retry_unavailable", "Original service rejected the restart evidence", 409)

    def _capture_failed_retry(self, repo, chain):
        index, sid = chain["next_node_index"], chain["workflow_session_id"]
        require(chain["schema_version"] == 5 and chain.get("execution_kind") == "round"
                and index < len(chain["node_run_ids"]), "failed_retry_unsupported",
                "This failure has no supported restart boundary", 409)
        run_id = chain["node_run_ids"][index]
        run = repo.get("node_run", run_id=run_id)
        document = self._document(repo, chain["workflow_definition_id"], chain["definition_revision"])
        declarations = {node["node_binding_id"]: node for node in document["nodes"]}
        declaration = declarations[run["node_binding_id"]]
        registry = self._execution_registries.get(chain["chain_run_id"])
        entry = registry.get(declaration["component_id"], declaration["component_version"]) if registry else None
        require(entry is not None and entry.failed_retry_validator is not None
                and run["status"] == "failed" and not run["output_refs"] and not run["effects"],
                "failed_retry_unsupported", "This unaccepted node has no registered restart policy", 409)
        owner = {"workflow_session_id": sid, "chain_run_id": chain["chain_run_id"],
                 "node_binding_id": run["node_binding_id"], "node_run_id": run_id}
        from .runtime_hosting import InvocationOwner
        host = self._runtime_hosts.get(chain["chain_run_id"])
        invocation = InvocationOwner.from_dict(owner)
        require(host is not None and host.contains(invocation), "failed_retry_unavailable",
                "Original hosted invocation is unavailable", 409)
        hosted = host.snapshot(invocation)
        require(hosted["status"] == "failed" and hosted["disposed"] and hosted["disposal_error"] is None,
                "failed_retry_unavailable", "Failed executor has not safely released its handle", 409)
        evidence = {"owner": owner, "run": run,
                    "facts": self._runtime_facts(repo.store, chain["chain_run_id"], node_run_id=run_id),
                    "completed": [{"component_id": declarations[node_id]["component_id"],
                                   "component_version": declarations[node_id]["component_version"],
                                   "node_binding_id": node_id, "node_run_id": chain["node_run_ids"][position]}
                                  for position, node_id in enumerate(chain["completed_nodes"])]}
        policy = entry.failed_retry_validator(deepcopy(declaration["config"]), deepcopy(evidence))
        from .contract_json import validate_json_value
        validate_json_value(policy)
        require(type(policy) is dict and set(policy) == {"classification", "service_ref", "service_evidence"}
                and type(policy["classification"]) is str and 0 < len(policy["classification"]) <= 128
                and type(policy["service_evidence"]) is dict
                and (policy["service_ref"] is not None or not policy["service_evidence"]),
                "failed_retry_invalid_policy", "Registered restart policy returned invalid evidence", 409)
        require(chain["chain_run_id"] in self._resource_frames
                and chain["chain_run_id"] in self._execution_registries,
                "failed_retry_unavailable", "Original frozen execution frame is unavailable", 409)
        candidate = {"basis": self._failed_retry_basis(repo, chain), "owner": owner,
                     "classification": policy["classification"],
                     "service_ref": policy["service_ref"], "evidence": policy["service_evidence"]}
        self._validate_failed_retry_candidate(chain["chain_run_id"], candidate)
        self._failed_retry_candidates[chain["chain_run_id"]] = candidate
        return {"allowed": True, "reason_code": "confirmed_failed_invocation",
                "node_run_id": run_id, "classification": policy["classification"]}

    def _failed_retry_permission(self, repo, chain):
        candidate = self._failed_retry_candidates.get(chain["chain_run_id"])
        original = (chain.get("diagnostic") or {}).get("failure_retry") or {}
        denied = {"allowed": False, "reason_code": original.get("reason_code", "failed_retry_unavailable"),
                  "node_run_id": chain["node_run_ids"][chain["next_node_index"]]
                  if chain["next_node_index"] < len(chain["node_run_ids"]) else None,
                  "classification": original.get("classification")}
        if candidate is None:
            if original.get("allowed"):
                denied["reason_code"] = "failed_retry_unavailable"
            return denied
        if candidate["basis"] != self._failed_retry_basis(repo, chain):
            denied["reason_code"] = "failed_retry_basis_changed"
            return denied
        try:
            self._validate_failed_retry_candidate(chain["chain_run_id"], candidate)
        except Exception:
            denied["reason_code"] = "failed_retry_unavailable"
            return denied
        return {"allowed": True, "reason_code": "confirmed_failed_invocation",
                "node_run_id": candidate["owner"]["node_run_id"],
                "classification": candidate["classification"]}

    def _prepare_failed_retry(self, repo, chain):
        from uuid import uuid4
        permission = self._failed_retry_permission(repo, chain)
        require(permission["allowed"], permission["reason_code"],
                "This failed invocation cannot safely restart", 409)
        candidate = self._failed_retry_candidates[chain["chain_run_id"]]
        self._validate_failed_retry_candidate(chain["chain_run_id"], candidate)
        index = chain["next_node_index"]
        old = repo.get("node_run", run_id=chain["node_run_ids"][index])
        new_id = str(uuid4())
        repo.put("node_run", graph_record("node_run", profile="node", run_id=new_id,
            workflow_session_id=old["workflow_session_id"], node_binding_id=old["node_binding_id"],
            chain_run_id=old["chain_run_id"], status="prepared", revision=1, input_refs={},
            input_values={}, output_refs={}, reads=[], effects=[], config=deepcopy(old["config"]),
            diagnostic=None, input_storage=old["input_storage"]))
        chain["node_run_ids"][index] = new_id
        chain["node_run_attempts"][index].append(new_id)
        chain["status"] = "prepared"
        chain["diagnostic"] = None
        chain["revision"] += 1
        repo.put("chain_run", chain)
