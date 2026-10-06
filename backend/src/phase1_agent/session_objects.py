"""Registered session objects with immutable values and session-owned CAS heads.

This repository deliberately knows no context, chat or plugin algorithms.
Callers use the surrounding graph transaction to settle a node's entire write
set together with its output and execution evidence.
"""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from .contract_json import canonical_bytes, content_digest, loads_strict
from .graph_records import require, uuid_value
from .host_sdk import ObjectBinding, WriteIntent, validate_reference


def initialize_object_tables(connection) -> None:
    for statement in (
        "CREATE TABLE IF NOT EXISTS session_object_bindings ("
        "session_id TEXT NOT NULL, object_key TEXT NOT NULL, binding TEXT NOT NULL, "
        "revision INTEGER NOT NULL, revision_id TEXT NOT NULL, "
        "PRIMARY KEY(session_id,object_key))",
        "CREATE TABLE IF NOT EXISTS session_object_values ("
        "revision_id TEXT PRIMARY KEY NOT NULL, type_id TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL, deleted INTEGER NOT NULL, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS session_object_revision_membership ("
        "session_id TEXT NOT NULL, object_key TEXT NOT NULL, revision_id TEXT NOT NULL, "
        "PRIMARY KEY(session_id,object_key,revision_id))",
        "CREATE TABLE IF NOT EXISTS session_object_receipts ("
        "session_id TEXT NOT NULL, operation_key TEXT NOT NULL, digest TEXT NOT NULL, "
        "result TEXT NOT NULL, PRIMARY KEY(session_id,operation_key))",
        "CREATE TABLE IF NOT EXISTS graph_state_manifests ("
        "owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, payload TEXT NOT NULL, "
        "PRIMARY KEY(owner_kind,owner_id))",
        "CREATE TABLE IF NOT EXISTS graph_project_packages ("
        "configuration_id TEXT PRIMARY KEY NOT NULL, payload TEXT NOT NULL)",
    ):
        connection.execute(statement)


class SessionObjectStore:
    def __init__(self, store, types):
        self.store, self.connection, self.types = store, store._connection, types

    def _check_type(self, binding, *, legacy=False, missing=False):
        from .type_contract_store import TypeContractStore
        return TypeContractStore(self.store).check(
            self.types, binding.type_id, binding.schema_version, "session",
            allow_legacy=legacy, allow_missing=missing)

    def _transaction(self):
        require(self.connection.in_transaction, "storage_contract_violation",
                "Object changes require a transaction", 500)

    def _binding(self, sid, key):
        row = self.connection.execute(
            "SELECT * FROM session_object_bindings WHERE session_id=? AND object_key=?", (sid, key),
        ).fetchone()
        require(row is not None, "session_object_unbound", "Object is not bound in this session", 404)
        return row, ObjectBinding.from_dict(loads_strict(row["binding"]))

    @staticmethod
    def authorize(binding, node_id, *, write=False):
        allowed = binding.writers if write else binding.readers
        require(node_id in allowed, "session_object_access_denied",
                "Node has no permission for this object", 403)
        require(binding.scope != "private" or node_id == binding.owner_node_id,
                "session_object_access_denied", "Private object belongs to another node", 403)

    def _value(self, revision_id):
        row = self.connection.execute(
            "SELECT * FROM session_object_values WHERE revision_id=?", (revision_id,),
        ).fetchone()
        require(row is not None, "storage_contract_violation", "Object revision is missing", 500)
        return {"revision_id": row["revision_id"], "type_id": row["type_id"],
                "schema_version": row["schema_version"], "deleted": bool(row["deleted"]),
                "value": loads_strict(row["value"])}

    def current(self, sid) -> dict:
        """Trusted platform projection; it also works while a package is absent."""
        result = {}
        for row in self.connection.execute(
            "SELECT * FROM session_object_bindings WHERE session_id=? ORDER BY object_key", (sid,),
        ).fetchall():
            binding = ObjectBinding.from_dict(loads_strict(row["binding"]))
            value = self._value(row["revision_id"])
            require((value["type_id"], value["schema_version"]) == (binding.type_id, binding.schema_version),
                    "storage_contract_violation", "Object head type differs from its binding", 500)
            result[row["object_key"]] = {**value, "revision": row["revision"],
                                         "binding": binding.to_dict()}
        return result

    def read(self, sid, key, *, node_id, revision_id=None) -> dict:
        row, binding = self._binding(sid, key)
        self.authorize(binding, node_id)
        self._check_type(binding)
        if revision_id is not None:
            require(self.connection.execute(
                "SELECT 1 FROM session_object_revision_membership WHERE session_id=? AND object_key=? AND revision_id=?",
                (sid, key, revision_id)).fetchone() is not None,
                "session_object_reference_denied", "Object revision is not authorized", 403)
        value = self._value(revision_id or row["revision_id"])
        require(not value["deleted"], "session_object_deleted", "Object has been deleted", 410)
        self.types.validate(binding.type_id, binding.schema_version, value["value"], scope="session")
        return {**value, "revision": row["revision"], "binding": binding.to_dict()}

    def _put_value(self, binding, value, *, deleted=False):
        revision_id = str(uuid4())
        self.connection.execute(
            "INSERT INTO session_object_values VALUES(?,?,?,?,?)",
            (revision_id, binding.type_id, binding.schema_version, int(deleted),
             canonical_bytes(value).decode("utf-8")),
        )
        return revision_id

    def _grant_revision(self, sid, key, revision_id):
        self.connection.execute(
            "INSERT OR IGNORE INTO session_object_revision_membership VALUES(?,?,?)",
            (sid, key, revision_id),
        )

    def initialize(self, sid, document, *, inherited=None, mapping=None, history=None, source_session_id=None):
        self._transaction()
        require(uuid_value(sid), "invalid_request", "Session identity is invalid")
        inherited = inherited or {}
        mapping = mapping or {}
        if source_session_id:
            mapping.setdefault("workflow_session_id", {})[source_session_id] = sid
        bindings = [ObjectBinding.from_dict(item) for item in document.get("object_bindings", [])]
        initialized_keys = {binding.object_key for binding in bindings}
        for binding in bindings:
            self._check_type(binding)
            require(self.connection.execute(
                "SELECT 1 FROM session_object_bindings WHERE session_id=? AND object_key=?",
                (sid, binding.object_key),
            ).fetchone() is None, "session_object_binding_exists", "Object already exists", 409)
            old = inherited.get(binding.object_key)
            if old is not None:
                require((old["type_id"], old["schema_version"]) == (binding.type_id, binding.schema_version),
                        "state_migration_required", "Object type changed; explicit migration is required", 409)
                value = self.types.remap(binding.type_id, binding.schema_version, old["value"],
                                         mapping, scope="session") if not old["deleted"] else None
                if not old["deleted"]:
                    self.types.validate(binding.type_id, binding.schema_version, value, scope="session")
                revision_id = (old["revision_id"] if canonical_bytes(value) == canonical_bytes(old["value"])
                               else self._put_value(binding, value, deleted=old["deleted"]))
            else:
                data = binding.to_dict()
                value = (data["default_value"] if "default_value" in data
                         else self.types.default(binding.type_id, binding.schema_version, scope="session"))
                value = self.types.validate(binding.type_id, binding.schema_version, value, scope="session")
                revision_id = self._put_value(binding, value)
            revision = self._historical_revision(sid, binding.object_key) + 1
            require(revision <= 2**53 - 1, "session_object_revision_exhausted",
                    "Object revision exceeds its protocol limit", 409)
            self.connection.execute(
                "INSERT INTO session_object_bindings VALUES(?,?,?,?,?)",
                (sid, binding.object_key, canonical_bytes(binding.to_dict()).decode("utf-8"),
                 revision, revision_id),
            )
            self._grant_revision(sid, binding.object_key, revision_id)
        for key, item in self.current(sid).items():
            if not item["deleted"]:
                old = inherited.get(key)
                if old and not old["deleted"] and source_session_id:
                    for ref in self.types.references(old["type_id"], old["schema_version"],
                                                     old["value"], scope="session"):
                        if ref["scope"] == "session":
                            require(ref["workflow_session_id"] == source_session_id
                                    and self.connection.execute(
                                        "SELECT 1 FROM session_object_revision_membership "
                                        "WHERE session_id=? AND object_key=? AND revision_id=?",
                                        (source_session_id, ref["object_key"], ref["revision_id"]),
                                    ).fetchone() is not None,
                                    "session_object_reference_denied", "Source revision is not authorized", 403)
                            self._grant_revision(sid, ref["object_key"], ref["revision_id"])
                self._check_references(sid, ObjectBinding.from_dict(item["binding"]), item["value"], history=history)
                if old is None and key in initialized_keys:
                    self.types.validate_write(
                        item["type_id"], item["schema_version"], item["value"],
                        self._write_context(sid, key, None, "initialize", history=history),
                    )

    def _artifact_history(self, sid):
        from .graph_store import GraphRecordStore
        repo = GraphRecordStore(self.store)
        head = next((row for row in repo.rows("workflow_ref") if row["workflow_session_id"] == sid), None)
        if head is None:
            return []
        commit = repo.get("workflow_commit", commit_id=head["head_commit_id"])
        return repo.get("state_snapshot", state_snapshot_id=commit["state_snapshot_id"])["history_refs"]

    def _resolve_write_artifact(self, sid, reference, *, history=None):
        reference = validate_reference(reference)
        require(reference["scope"] == "artifact", "session_object_reference_invalid",
                "Write validators can resolve only immutable artifact references")
        output = self.store._get("workflow_output", reference["output_id"])
        require(output is not None, "session_object_reference_denied", "Artifact is unavailable", 403)
        granted = self._artifact_history(sid) if history is None else history
        require(output["workflow_session_id"] == sid or output["chain_run_id"] in granted,
                "session_object_reference_denied", "Artifact is outside authorized session history", 403)
        producer = self.store._get("node_run", output["run_id"])
        require(producer is not None and producer["status"] == "succeeded"
                and producer["workflow_session_id"] == output["workflow_session_id"]
                and producer["chain_run_id"] == output["chain_run_id"]
                and producer["node_binding_id"] == output["node_binding_id"]
                and producer["output_refs"].get(output["port_id"]) == output["output_id"],
                "session_object_reference_denied", "Artifact has no accepted producer", 403)
        from .graph_store import GraphRecordStore
        repo = GraphRecordStore(self.store)
        chain = repo.get("chain_run", chain_run_id=output["chain_run_id"])
        document = repo.get(
            "workflow_definition_revision", workflow_definition_id=chain["workflow_definition_id"],
            revision=chain["definition_revision"],
        )["document"]
        node = next((node for node in document["nodes"]
                     if node["node_binding_id"] == output["node_binding_id"]), None)
        require(node is not None, "storage_contract_violation", "Artifact producer declaration is missing", 500)
        return {"value": deepcopy(output["payload"]), "component_id": node["component_id"],
                "component_version": node["component_version"], "config": deepcopy(node["config"]),
                "producer": {"workflow_session_id": output["workflow_session_id"],
                             "chain_run_id": output["chain_run_id"],
                             "node_binding_id": output["node_binding_id"], "node_run_id": output["run_id"]},
                "input_refs": deepcopy(producer["input_refs"])}

    def _write_context(self, sid, key, current_record, operation, *, history=None):
        return {"workflow_session_id": sid, "object_key": key,
                "current_record": deepcopy(current_record), "operation": operation,
                "resolve_artifact": lambda reference: self._resolve_write_artifact(sid, reference, history=history)}

    def _check_references(self, sid, binding, value, *, history=None):
        for ref in self.types.references(binding.type_id, binding.schema_version, value, scope="session"):
            require(type(ref) is dict and type(ref.get("scope")) is str,
                    "session_object_reference_invalid", "Reference scope must be declared")
            if ref["scope"] == "session":
                require(ref.get("workflow_session_id") == sid,
                        "session_object_reference_denied", "Reference escapes this session", 403)
                target, _ = self._binding(sid, ref.get("object_key"))
                require(type(ref.get("revision_id")) is str,
                        "session_object_reference_invalid", "Session references must identify an immutable revision")
                known = self._value(ref["revision_id"])
                require(self.connection.execute(
                    "SELECT 1 FROM session_object_revision_membership "
                    "WHERE session_id=? AND object_key=? AND revision_id=?",
                    (sid, ref["object_key"], ref["revision_id"]),
                ).fetchone() is not None, "session_object_reference_denied",
                    "Revision is outside this object's authorized history", 403)
                target_binding = ObjectBinding.from_dict(loads_strict(target["binding"]))
                # Access to an object cannot grant access to another private object.
                require(set(binding.readers) <= set(target_binding.readers),
                        "session_object_reference_denied", "Reference readers exceed target permissions", 403)
                require(known["type_id"] == target_binding.type_id
                        and known["schema_version"] == target_binding.schema_version,
                        "session_object_reference_invalid", "Reference target type differs")
            elif ref["scope"] == "artifact":
                output = self.store._get("workflow_output", ref.get("output_id", ""))
                if history is None:
                    history = self._artifact_history(sid)
                require(output is not None and (output.get("workflow_session_id") == sid
                        or output.get("chain_run_id") in history),
                        "session_object_reference_denied", "Artifact is outside this session", 403)
            else:
                from .host_sdk import ResourceIdentity
                ResourceIdentity.from_dict(ref)

    def apply(self, sid, *, node_id, writes: list[dict]) -> list[dict]:
        """CAS and receipts are scoped to the owning session, including tombstones."""
        self._transaction()
        require(type(writes) is list and len(writes) <= 256, "invalid_request", "Write set is invalid")
        intents = [WriteIntent.from_dict(item) for item in writes]
        require(len({item.object_key for item in intents}) == len(intents),
                "session_object_duplicate_write", "Write set repeats an object")
        results = []
        for intent in intents:
            row, binding = self._binding(sid, intent.object_key)
            self.authorize(binding, node_id, write=True)
            self._check_type(binding)
            request = {"node_id": node_id, "intent": intent.to_dict()}
            digest = content_digest(request)
            prior = self.connection.execute(
                "SELECT digest,result FROM session_object_receipts WHERE session_id=? AND operation_key=?",
                (sid, intent.operation_key),
            ).fetchone()
            if prior:
                require(prior["digest"] == digest, "idempotency_conflict", "Object key has another request", 409)
                results.append(loads_strict(prior["result"]))
                continue
            require(row["revision"] == intent.expected_revision, "stale_object_revision",
                    "Object changed since it was read", 409)
            deleted = intent.operation == "delete"
            value = None if deleted else self.types.validate(
                binding.type_id, binding.schema_version, intent.value, scope="session")
            if not deleted:
                self._check_references(sid, binding, value)
            current_record = {**self._value(row["revision_id"]), "revision": row["revision"],
                              "binding": binding.to_dict()}
            self.types.validate_write(binding.type_id, binding.schema_version, value,
                                      self._write_context(sid, intent.object_key, current_record, intent.operation))
            revision_id = self._put_value(binding, value, deleted=deleted)
            self._grant_revision(sid, intent.object_key, revision_id)
            revision = row["revision"] + 1
            require(revision <= 2**53 - 1, "session_object_revision_exhausted",
                    "Object revision exceeds its protocol limit", 409)
            self.connection.execute(
                "UPDATE session_object_bindings SET revision=?,revision_id=? WHERE session_id=? AND object_key=?",
                (revision, revision_id, sid, intent.object_key),
            )
            result = {"object_key": intent.object_key, "revision": revision,
                      "revision_id": revision_id, "deleted": deleted}
            self.connection.execute(
                "INSERT INTO session_object_receipts VALUES(?,?,?,?)",
                (sid, intent.operation_key, digest, canonical_bytes(result).decode("utf-8")),
            )
            results.append(result)
        return results

    def references(self, sid) -> dict:
        return {key: {field: value[field] for field in (
            "revision_id", "type_id", "schema_version", "deleted", "revision", "binding",
        )} for key, value in self.current(sid).items()}

    def at_manifest(self, references: dict) -> dict:
        result = {}
        for key, reference in references.items():
            value = self._value(reference["revision_id"])
            require(all(value[field] == reference[field] for field in ("type_id", "schema_version", "deleted")),
                    "storage_contract_violation", "Manifest object reference differs", 500)
            result[key] = {**deepcopy(reference), **value}
        return result

    def _historical_revision(self, sid, key):
        if self.connection.execute(
                "SELECT 1 FROM session_object_revision_membership WHERE session_id=? AND object_key=? LIMIT 1",
                (sid, key)).fetchone() is None:
            return 0
        revisions = [0]
        for row in self.connection.execute("SELECT payload FROM graph_state_manifests").fetchall():
            manifest = loads_strict(row["payload"])
            if manifest["workflow_session_id"] == sid and key in manifest["objects"]:
                revisions.append(manifest["objects"][key]["revision"])
        for row in self.connection.execute(
                "SELECT result FROM session_object_receipts WHERE session_id=?", (sid,)).fetchall():
            result = loads_strict(row["result"])
            if result["object_key"] == key:
                revisions.append(result["revision"])
        return max(revisions)

    def restore(self, sid, document, references: dict, *, source_session_id=None, history=None, node_mapping=None,
                restore_bindings=False):
        """Restore values without rewinding CAS counters or borrowing parent heads."""
        self._transaction()
        values = self.at_manifest(references)
        bindings = {item["object_key"]: ObjectBinding.from_dict(item)
                    for item in document.get("object_bindings", [])}
        require(set(values) == set(bindings), "state_migration_required",
                "Object bindings differ from this checkpoint", 409)
        if restore_bindings:
            for key in self.current(sid).keys() - bindings.keys():
                self.connection.execute(
                    "DELETE FROM session_object_bindings WHERE session_id=? AND object_key=?", (sid, key))
        for key, binding in bindings.items():
            value = values[key]
            binding_changed = False
            if restore_bindings:
                require(value["binding"] == binding.to_dict(),
                        "storage_contract_violation", "Historical object binding differs from its definition", 500)
                row = self.connection.execute(
                    "SELECT * FROM session_object_bindings WHERE session_id=? AND object_key=?", (sid, key),
                ).fetchone()
                if row is None:
                    revision = self._historical_revision(sid, key) + 1
                    require(revision <= 2**53 - 1, "session_object_revision_exhausted",
                            "Object revision exceeds its protocol limit", 409)
                    self.connection.execute(
                        "INSERT INTO session_object_bindings VALUES(?,?,?,?,?)",
                        (sid, key, canonical_bytes(binding.to_dict()).decode("utf-8"), revision, value["revision_id"]),
                    )
                    self._grant_revision(sid, key, value["revision_id"])
                elif loads_strict(row["binding"]) != binding.to_dict():
                    binding_changed = True
                    self.connection.execute(
                        "UPDATE session_object_bindings SET binding=? WHERE session_id=? AND object_key=?",
                        (canonical_bytes(binding.to_dict()).decode("utf-8"), sid, key),
                    )
            row, current_binding = self._binding(sid, key)
            require((value["type_id"], value["schema_version"]) == (binding.type_id, binding.schema_version)
                    and binding.to_dict() == current_binding.to_dict(),
                    "state_migration_required", "Object binding requires migration", 409)
            self._check_type(binding)
            revision_id = value["revision_id"]
            if source_session_id and source_session_id != sid and not value["deleted"]:
                mapping = {"workflow_session_id": {source_session_id: sid},
                           "node_binding_id": node_mapping or {}}
                remapped = self.types.remap(binding.type_id, binding.schema_version,
                                            value["value"], mapping, scope="session")
                for ref in self.types.references(binding.type_id, binding.schema_version,
                                                  value["value"], scope="session"):
                    if ref["scope"] == "session":
                        require(ref["workflow_session_id"] == source_session_id
                                and self.connection.execute(
                                    "SELECT 1 FROM session_object_revision_membership "
                                    "WHERE session_id=? AND object_key=? AND revision_id=?",
                                    (source_session_id, ref["object_key"], ref["revision_id"]),
                                ).fetchone() is not None,
                                "session_object_reference_denied", "Source reference is not authorized", 403)
                        self._grant_revision(sid, ref["object_key"], ref["revision_id"])
                if canonical_bytes(remapped) != canonical_bytes(value["value"]):
                    revision_id = self._put_value(binding, remapped)
            if row["revision_id"] != revision_id or binding_changed:
                require(row["revision"] < 2**53 - 1, "session_object_revision_exhausted",
                        "Object revision exceeds its protocol limit", 409)
                self.connection.execute(
                    "UPDATE session_object_bindings SET revision=?,revision_id=? WHERE session_id=? AND object_key=?",
                    (row["revision"] + 1, revision_id, sid, key),
                )
                self._grant_revision(sid, key, revision_id)
        for item in self.current(sid).values():
            if not item["deleted"]:
                self._check_references(sid, ObjectBinding.from_dict(item["binding"]), item["value"], history=history)

    def rebind(self, sid, document):
        """Compatible declarations retain values; unbinding retains immutable history."""
        self._transaction()
        current = self.current(sid)
        target = {item["object_key"]: item for item in document.get("object_bindings", [])}
        require(all(key in target and target[key] == value["binding"] for key, value in current.items()),
                "state_migration_required", "Object declaration changed; migration must be explicit", 409)
        added = {**document, "object_bindings": [value for key, value in target.items() if key not in current]}
        self.initialize(sid, added)


class StateManifestStore:
    def __init__(self, store, types):
        self.connection = store._connection
        self.objects = SessionObjectStore(store, types)

    def get(self, owner_kind, owner_id) -> dict | None:
        if owner_kind == "chain_end":
            rows = self.connection.execute(
                "SELECT owner_id,payload FROM graph_state_manifests "
                "WHERE owner_kind='chain_boundary' AND owner_id LIKE ?", (owner_id + ":%",),
            ).fetchall()
            row = max(rows, key=lambda item: int(item["owner_id"].rsplit(":", 1)[1])) if rows else None
            return loads_strict(row["payload"]) if row else None
        row = self.connection.execute(
            "SELECT payload FROM graph_state_manifests WHERE owner_kind=? AND owner_id=?",
            (owner_kind, owner_id),
        ).fetchone()
        return loads_strict(row["payload"]) if row else None

    def save(self, owner_kind, owner_id, *, session, data_revision, node_states, history_refs,
             package_lock, global_resources=None, status=None):
        require(self.connection.in_transaction, "storage_contract_violation",
                "Manifests require a consistent transaction", 500)
        if owner_kind == "chain_end":
            owner_kind, owner_id = "chain_boundary", owner_id + ":" + str(session["revision"])
        value = {
            "envelope_version": 1, "kind": "workflow.state-manifest",
            "workflow_session_id": session["workflow_session_id"],
            "workflow_definition_id": session["workflow_definition_id"],
            "definition_revision": session["definition_revision"],
            "objects": self.objects.references(session["workflow_session_id"]),
            "legacy_data_revision": data_revision,
            "legacy_node_states": deepcopy(node_states), "history_refs": deepcopy(history_refs),
            "package_lock": deepcopy(list(package_lock)), "global_resources": deepcopy(global_resources or []),
            "status": status,
        }
        previous = self.get(owner_kind, owner_id)
        require(previous is None or canonical_bytes(previous) == canonical_bytes(value),
                "immutable_conflict", "Manifest already has another state", 409)
        if previous is None:
            self.connection.execute(
                "INSERT INTO graph_state_manifests VALUES(?,?,?)",
                (owner_kind, owner_id, canonical_bytes(value).decode("utf-8")),
            )
        return value
