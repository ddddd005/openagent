"""Session-owned data access; node producers retain ownership of output logic."""

from __future__ import annotations

import copy
from contextlib import closing

from .contract_json import canonical_bytes
from .program_variable_store import ProgramVariableStore
from .workbench_resources import (
    WorkbenchResourceStore, require, resource_error, resource_id, resource_revision,
    session_data_entry, validate_data_definition, validate_data_value,
)


class WorkbenchInterfaces:
    """Mixin using the coordinator's lock, transaction and selected history."""

    def list_global_content(self):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return WorkbenchResourceStore(store).list("content")

    def resolve_global_content(self, resource_ids):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            store._connection.execute("BEGIN")
            try:
                result = WorkbenchResourceStore(store).resolve_content(resource_ids)
                store._connection.execute("COMMIT")
                return result
            except BaseException:
                store._connection.execute("ROLLBACK")
                raise

    def save_global_content(self, record, *, expected_revision, idempotency_key):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return WorkbenchResourceStore(store).write(
                "content", record, expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )

    def delete_global_content(self, identity, *, expected_revision):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            WorkbenchResourceStore(store).delete(identity, expected_revision=expected_revision)
            return {"resource_id": identity, "deleted": True}

    def register_session_data(self, definition):
        """Plugin installation registers definitions through the trusted service API."""
        definition = validate_data_definition(definition)
        with self._lock, closing(self._store()) as store:
            self._check_open()
            resources = WorkbenchResourceStore(store)
            old = resources.get("data-definition", definition["definition_id"], definition["revision"])
            if old is not None:
                require(canonical_bytes(old) == canonical_bytes(definition))
                return old
            from uuid import uuid4
            return resources.write(
                "data-definition", definition, expected_revision=definition["revision"] - 1,
                idempotency_key=str(uuid4()),
            )

    def list_session_data_definitions(self):
        with self._lock, closing(self._store()) as store:
            self._check_open()
            return WorkbenchResourceStore(store).list("data-definition")

    @staticmethod
    def _registered_data(store, definition_id, revision):
        definition = WorkbenchResourceStore(store).get("data-definition", definition_id, revision)
        if definition is None:
            raise resource_error("session_data_unregistered", "Session data is not registered", 404)
        return definition

    def _validate_data_program(self, store, program):
        for node in program["nodes"]:
            if node["kind"] not in ("session-data-read", "session-data-write"):
                continue
            definition = node["config"]["definition"]
            saved = self._registered_data(store, definition["definition_id"], definition["revision"])
            require(canonical_bytes(saved) == canonical_bytes(definition),
                    "Node data declaration differs from registered capability")
            require(not node.get("public_outputs") or definition["public"],
                    "Private registered data cannot be publicly exposed")

    def _resolve_global_programs(self, store, configs):
        identities = sorted({
            node["config"]["resource_id"]
            for config in configs.values() if config["schema_version"] == 3
            for node in config["preparation"]["nodes"] if node["kind"] == "global-source"
        })
        records = {record["resource_id"]: record for record in
                   WorkbenchResourceStore(store).resolve_content(identities)}
        for config in configs.values():
            if config["schema_version"] != 3:
                continue
            for node in config["preparation"]["nodes"]:
                if node["kind"] == "global-source":
                    node["config"]["record"] = copy.deepcopy(records[node["config"]["resource_id"]])
            self._validate_data_program(store, config["preparation"])
        return configs

    def _bind_context_sources(self, store, sid, config, request):
        from .workflow import A_BINDING, B_BINDING
        bindings = {
            node["config"]["binding_id"] for node in config["preparation"]["nodes"]
            if node["kind"] == "context-source" and "binding_id" in node["config"]
        }
        require(bindings <= {A_BINDING, B_BINDING}, "Context source Agent is not registered")
        if not bindings:
            return
        config["context_sources"] = {}
        for binding in sorted(bindings):
            scope, archived = self._selected_workbench_context(store, sid, binding, request)
            config["context_sources"][binding] = {
                "scope": scope, "messages": archived.messages,
                "logical_floors": archived.logical_floors,
                "protected_blocks": archived.protected_blocks,
            }

    @staticmethod
    def _assembly_archive(config, binding, archived):
        from .preparation_program import selected_context_binding
        from .workflow_context_view import ArchivedContext
        selected = selected_context_binding(config["preparation"], binding)
        source = config.get("context_sources", {}).get(selected)
        return archived if source is None else ArchivedContext(
            copy.deepcopy(source["messages"]), copy.deepcopy(source["logical_floors"]),
            copy.deepcopy(source["protected_blocks"]), [],
        )

    def read_session_data(self, sid, *, definition_id, revision, public=False):
        resource_id(sid)
        require(type(public) is bool)
        with self._lock, closing(self._store()) as store:
            self._check_open()
            self._session(store, sid)
            definition = self._registered_data(store, definition_id, revision)
            if public and not definition["public"]:
                raise resource_error("output_not_public", "Session data is internal", 403)
            state = self._program_current_state(store, sid)
            entry = state.get("data", {}).get(definition["key"], session_data_entry(definition))
            require(canonical_bytes(entry["definition"]) == canonical_bytes(definition))
            return {
                "schema_version": 1, "workflow_session_id": sid, "revision": state["revision"],
                "definition": definition, "assigned": "value" in entry,
                **({"value": copy.deepcopy(entry["value"])} if "value" in entry else {}),
            }

    def write_session_data(self, sid, *, definition_id, revision, value,
                           expected_revision, expected_session_revision, idempotency_key):
        resource_id(sid)
        resource_id(idempotency_key)
        resource_revision(expected_revision, zero=True)
        resource_revision(expected_session_revision, zero=True)
        request = {
            "kind": "session-data-write", "session_id": sid,
            "definition_id": definition_id, "revision": revision, "value": value,
            "expected_revision": expected_revision,
            "expected_session_revision": expected_session_revision,
        }
        with self._lock, closing(self._store()) as store:
            self._check_open()
            variables = ProgramVariableStore(store)
            receipt = variables.receipt(sid, idempotency_key, request)
            if receipt is not None:
                state = receipt["state"]
            else:
                session = self._session(store, sid)
                if session["revision"] != expected_session_revision:
                    raise resource_error("stale_revision", "Session revision changed", 409)
                definition = self._registered_data(store, definition_id, revision)
                if not definition["writable"]:
                    raise resource_error("output_read_only", "Session data is read-only", 403)
                validate_data_value(definition, value)
                state = variables.current(sid)
                state.setdefault("data", {})[definition["key"]] = session_data_entry(
                    definition, value, assigned=True,
                )
                store._connection.execute("BEGIN IMMEDIATE")
                try:
                    state = variables.write_in_transaction(
                        sid, state, expected_revision=expected_revision,
                        key=idempotency_key, request=request,
                    )["state"]
                    store._connection.execute("COMMIT")
                except BaseException:
                    store._connection.execute("ROLLBACK")
                    raise
            entry = state["data"][self._registered_data(store, definition_id, revision)["key"]]
            return {
                "schema_version": 1, "workflow_session_id": sid, "revision": state["revision"],
                "definition": entry["definition"], "assigned": True, "value": entry["value"],
                "idempotency_key": idempotency_key,
            }

    def read_node_outputs(self, sid, *, node_id, run_id=None, public=True,
                          exposure_configuration=None):
        resource_id(sid)
        require(type(public) is bool)
        require(type(node_id) is str and 0 < len(node_id) <= 128)
        with self._lock, closing(self._store()) as store:
            self._check_open()
            view = self._view(store, sid)
            if public and exposure_configuration is not None:
                require(type(exposure_configuration) is dict
                        and set(exposure_configuration) == {"config_id", "revision"},
                        "Public output requires an exact declaration")
                from .exposure_configuration import ExposureConfigurationStore, read_registered_exposures
                catalog = ExposureConfigurationStore(store)
                record = catalog.get(exposure_configuration["config_id"],
                                     exposure_configuration["revision"])
                if record is None:
                    raise resource_error("output_not_public", "Output declaration is unavailable", 403)
                if record["workflow_id"] != WorkbenchResourceStore(store).session_owner(sid):
                    raise resource_error("ownership_mismatch", "Declaration belongs to another workflow", 409)
                rows = read_registered_exposures(record, catalog.get(record["config_id"]), view)
                declarations = [row for row in record["registrations"] if row["nodeBindingId"] == node_id]
                require(bool(declarations), "Node has no public output declaration")
                registered_ids = {row["id"] for row in declarations}
                rows["registrations"] = declarations
                rows["observations"] = [row for row in rows["observations"]
                                        if row["registrationId"] in registered_ids]
                actual_run = next((row["runId"] for row in rows["observations"]), None)
                if run_id is not None and run_id != actual_run:
                    raise resource_error("unsupported", "Historical Agent output requires a run-specific declaration", 409)
                return {
                    "schema_version": 1, "workflow_session_id": sid, "node_id": node_id,
                    "run_id": actual_run, "status": "declared", "outputs": rows,
                }
            builtins = next((node for node in view["nodes"] if node["node_binding_id"] == node_id), None)
            if builtins is not None and run_id is None:
                if public:
                    raise resource_error("output_not_public", "Agent output requires a public declaration", 403)
                observed = next(row for row in view["observation"]["nodes"]
                                if row["node_binding_id"] == node_id)
                return {
                    "schema_version": 1, "workflow_session_id": sid, "node_id": node_id,
                    "run_id": builtins["run_id"], "status": builtins["status"],
                    "outputs": {
                        "state": copy.deepcopy(builtins),
                        "out": {"kind": "text", "text": observed["result"].get("text")},
                    },
                }
            runs = [run for run in store.list_records("run_record")
                    if run["workflow_session_id"] == sid]
            if run_id is not None:
                resource_id(run_id)
                runs = [run for run in runs if run["run_id"] == run_id]
                if not runs:
                    raise resource_error("not_found", "Run does not belong to this session", 404)
            else:
                chains = [chain for chain in store.list_records("chain_run")
                          if chain["workflow_session_id"] == sid]
                current_chain = chains[-1]["chain_run_id"] if chains else None
                runs = [run for run in runs if run["chain_run_id"] == current_chain]
            for run in reversed(runs):
                snapshot = store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]})
                from .prepared_context import validate_frozen_preparation
                evidence = validate_frozen_preparation(snapshot) or {}
                stage = next((item for item in evidence.get("program", {}).get("stages", [])
                              if item["node_id"] == node_id), None)
                if stage is not None:
                    from .preparation_program import project_node_outputs
                    producer = next(node for node in evidence["program"]["program"]["nodes"] if node["node_id"] == node_id)
                    if public and not producer.get("public_outputs"):
                        raise resource_error("output_not_public", "Node output is internal", 403)
                    # Only prepared text/material outputs; no private kernel or credentials.
                    return {
                        "schema_version": 1, "workflow_session_id": sid, "node_id": node_id,
                        "run_id": run["run_id"], "status": run["status"],
                        "outputs": project_node_outputs(producer, stage["output"],
                                                        producer.get("public_outputs") if public else None,
                                                        program=evidence["program"]["program"]),
                    }
            return {
                "schema_version": 1, "workflow_session_id": sid, "node_id": node_id,
                "run_id": run_id, "status": "unproduced", "outputs": {},
            }

    def list_public_node_outputs(self, sid):
        """Discover only declarations produced in the current chain, never previous-round values."""
        resource_id(sid)
        with self._lock, closing(self._store()) as store:
            self._check_open()
            view = self._view(store, sid)
            chains = [chain for chain in store.list_records("chain_run")
                      if chain["workflow_session_id"] == sid]
            chain_id = chains[-1]["chain_run_id"] if chains else None
            nodes = {}
            from .prepared_context import validate_frozen_preparation
            for run in reversed(store.list_records("run_record")):
                if run["workflow_session_id"] != sid or run["chain_run_id"] != chain_id:
                    continue
                snapshot = store.get_record("input_snapshot", {"snapshot_id": run["snapshot_id"]})
                evidence = validate_frozen_preparation(snapshot) or {}
                for producer in evidence.get("program", {}).get("program", {}).get("nodes", []):
                    identity = producer["node_id"]
                    if producer.get("public_outputs") and identity not in nodes:
                        nodes[identity] = self.read_node_outputs(sid, node_id=identity, run_id=run["run_id"])
            return {
                "schema_version": 1, "kind": "session_public_outputs",
                "workflow_session_id": sid, "session_revision": view["revision"],
                "chain_run_id": chain_id, "nodes": list(nodes.values()),
            }
