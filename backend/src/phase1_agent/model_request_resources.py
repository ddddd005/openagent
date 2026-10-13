"""Uninstalled M2 resource reader; trusted host bridges own binding evidence."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from .contract_json import canonical_bytes, content_digest, loads_strict, validate_json_value
from .global_resources import MAX_GLOBAL_RESOURCE_BYTES
from .graph_records import require, uuid_value, validate_graph_record
from .host_sdk import ResourceIdentity, TypeRegistry
from .model_contract import CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE, validate_local_provider
from .model_request_contracts import (
    MAX_SAFE_INTEGER, MODEL_REQUEST_SERVICE_REF, model_request_operations,
    validate_model_binding, validate_model_profile,
)
from .type_contract_store import TypeContractStore


class ModelRequestResourceReader:
    """Read exact authorized current resources without grants, Key or lease I/O.

    ``load_binding_authority(connection, context, output_id)`` is a trusted host
    bridge, never a client callback. It must load session, chain, consumer_run,
    producer_run and binding_output graph records through the supplied read
    transaction, plus frozen source_node and consumer_node declarations.
    The source component is supplied by the host; this module installs no nodes.
    """

    def __init__(self, database_path: str | Path, *, types: TypeRegistry,
                 load_binding_authority, source_component: tuple[str, str]):
        require(isinstance(types, TypeRegistry) and callable(load_binding_authority)
                and type(source_component) is tuple and len(source_component) == 2
                and all(type(item) is str and bool(item) for item in source_component),
                "model_resource_host_invalid", "Exact trusted host dependencies are required")
        self._uri = Path(database_path).resolve().as_uri() + "?mode=ro"
        self._types = types.detached(frozen=True)
        self._load_authority = load_binding_authority
        self._source_component = source_component
        # Only revision identities are retained for dispatch rechecks, not values.
        self._read_bases: set[str] = set()

    @contextmanager
    def _read(self):
        connection = sqlite3.connect(self._uri, uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            yield connection
            connection.execute("COMMIT")
        finally:
            connection.close()

    @staticmethod
    def _context_owner(context) -> dict:
        keys = ("workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")
        owner = {key: getattr(context, key, None) for key in keys}
        require(all(uuid_value(value) for value in owner.values()),
                "model_binding_unauthorized", "Caller requires an exact runtime owner")
        return owner

    def _authorize(self, connection, context, capability, operation, output_id):
        owner = self._context_owner(context)
        require((capability, operation) in {
            (item.capability, item.operation) for item in model_request_operations()
        }, "model_binding_unauthorized", "Model operation has no exact declared route")
        require(uuid_value(output_id), "model_binding_unauthorized",
                "Model input requires an exact accepted output identity")
        evidence = self._load_authority(connection, context, output_id)
        require(type(evidence) is dict and set(evidence) == {
            "session", "chain", "consumer_run", "producer_run", "binding_output",
            "source_node", "consumer_node",
        }, "model_binding_unauthorized", "Trusted binding authority evidence is missing")
        records = {
            key: validate_graph_record(kind, evidence[key])
            for key, kind in (
                ("session", "workflow_session"), ("chain", "chain_run"),
                ("consumer_run", "node_run"), ("producer_run", "node_run"),
                ("binding_output", "workflow_output"),
            )
        }
        session, chain = records["session"], records["chain"]
        consumer, producer = records["consumer_run"], records["producer_run"]
        output = records["binding_output"]
        require(session["workflow_session_id"] == owner["workflow_session_id"]
                and session["active_chain_run_id"] == owner["chain_run_id"]
                and chain["workflow_session_id"] == owner["workflow_session_id"]
                and chain["chain_run_id"] == owner["chain_run_id"]
                and chain["workflow_definition_id"] == session["workflow_definition_id"]
                and chain["definition_revision"] == session["definition_revision"]
                and chain["status"] == "running"
                and consumer["run_id"] == owner["node_run_id"]
                and consumer["node_binding_id"] == owner["node_binding_id"]
                and consumer["status"] == "running",
                "model_binding_unauthorized", "Caller is not this active running invocation")
        declared_workflow = getattr(context, "workflow_definition_id", None)
        require(declared_workflow is None or declared_workflow == session["workflow_definition_id"],
                "model_binding_unauthorized", "Caller declares another workflow owner")
        require(all(record["workflow_session_id"] == owner["workflow_session_id"]
                    and record["chain_run_id"] == owner["chain_run_id"]
                    for record in (consumer, producer, output))
                and producer["status"] == "succeeded"
                and output["output_id"] == output_id
                and output["run_id"] == producer["run_id"]
                and output["node_binding_id"] == producer["node_binding_id"]
                and output["port_id"] == "output"
                and producer["output_refs"].get("output") == output_id,
                "model_binding_unauthorized", "Model output has no exact accepted producer")
        nodes = chain["ordered_nodes"]
        require(consumer["node_binding_id"] in nodes and producer["node_binding_id"] in nodes,
                "model_binding_unauthorized", "Producer and consumer are absent from the chain")
        consumer_index = nodes.index(consumer["node_binding_id"])
        producer_index = nodes.index(producer["node_binding_id"])
        require(producer_index < consumer_index == chain["next_node_index"]
                and chain["node_run_ids"][consumer_index] == consumer["run_id"]
                and chain["node_run_ids"][producer_index] == producer["run_id"]
                and chain["outputs"].get(producer["node_binding_id"], {}).get("output") == output_id,
                "model_binding_unauthorized", "Accepted model output differs from chain progress")
        refs = context.input_artifact_refs("model")
        require(type(refs) is list and len(refs) == 1
                and refs == consumer["input_refs"].get("model")
                and refs[0]["output_id"] == output_id,
                "model_binding_unauthorized", "Model input differs from the accepted binding")
        source, declaration = evidence["source_node"], evidence["consumer_node"]
        require(type(source) is dict and set(source) == {
            "node_binding_id", "component_id", "component_version", "config",
        } and source["node_binding_id"] == producer["node_binding_id"]
            and (source["component_id"], source["component_version"]) == self._source_component
            and source["config"] == producer["config"]
            and type(source["config"]) is dict and set(source["config"]) == {"reference"},
            "model_binding_unauthorized", "Model source has no exact frozen declaration")
        require(type(declaration) is dict
                and declaration.get("node_binding_id") == owner["node_binding_id"]
                and type(declaration.get("capabilities")) in (list, tuple)
                and capability in declaration["capabilities"]
                and type(declaration.get("service_requirements")) in (list, tuple),
                "model_binding_unauthorized", "Consumer has no trusted model service declaration")
        service = MODEL_REQUEST_SERVICE_REF.to_dict()
        grants = [grant for grant in declaration["service_requirements"]
                  if type(grant) is dict and grant.get("service_id") == service["service_id"]
                  and grant.get("exact_version") == service["exact_version"]
                  and grant.get("capability") == capability
                  and type(grant.get("operations")) is list
                  and operation in grant["operations"]]
        require(len(grants) == 1 and set(grants[0]) == {
            "service_id", "exact_version", "capability", "operations",
        }, "model_binding_unauthorized", "Consumer lacks one exact model service operation grant")
        binding = validate_model_binding(output["payload"])
        require(binding["reference"] == source["config"]["reference"],
                "model_binding_unauthorized", "Binding root differs from the original model source")
        return {"binding": binding, "binding_output_id": output_id,
                "producer_node_run_id": producer["run_id"],
                "input_refs": deepcopy(consumer["input_refs"])}

    def authorize_binding(self, context, capability, operation, binding_id) -> dict:
        refs = context.input_artifact_refs("model")
        require(type(refs) is list and len(refs) == 1 and type(refs[0]) is dict,
                "model_binding_unauthorized", "Model input requires one exact accepted artifact")
        with self._read() as connection:
            authority = self._authorize(connection, context, capability, operation,
                                        refs[0].get("output_id"))
            require(authority["binding"]["binding_id"] == binding_id,
                    "model_binding_unauthorized", "Selected binding differs from the accepted model input")
            return authority

    def _record(self, connection, identity, schema_version):
        row = connection.execute(
            "SELECT update_sequence,deleted,payload FROM global_resource_current "
            "WHERE scope=? AND type_id=? AND resource_id=?",
            (identity["scope"], identity["type_id"], identity["resource_id"]),
        ).fetchone()
        require(row is not None and not row["deleted"], "model_resource_missing",
                "Current model resource is missing or deleted", 404)
        record = loads_strict(row["payload"])
        validate_json_value(record)
        require(type(record) is dict and set(record) == {
            "envelope_version", "scope", "type_id", "resource_id", "data_schema_version",
            "update_sequence", "value",
        } and type(record["envelope_version"]) is int and record["envelope_version"] == 1
            and len(canonical_bytes(record)) <= MAX_GLOBAL_RESOURCE_BYTES
            and {key: record[key] for key in identity} == identity
            and type(record["update_sequence"]) is int
            and 1 <= record["update_sequence"] <= MAX_SAFE_INTEGER
            and record["update_sequence"] == row["update_sequence"],
            "model_resource_invalid", "Current resource envelope differs from its stored identity")
        require(type(record["data_schema_version"]) is int
                and record["data_schema_version"] == schema_version,
                "model_resource_schema_unsupported", "Current resource schema is unsupported")
        self._types.validate(identity["type_id"], schema_version, record["value"], scope="global")
        accepted = TypeContractStore(SimpleNamespace(_connection=connection)).get(
            identity["type_id"], schema_version, "global")
        require(accepted is not None, "type_contract_evidence_missing",
                "Current model resource lacks accepted type evidence")
        definition = self._types.get(identity["type_id"], schema_version, scope="global")
        require(canonical_bytes(accepted["declaration"]) == canonical_bytes(definition.to_dict()),
                "type_contract_redefined", "Trusted type differs from its accepted schema declaration")
        return record

    def _basis(self, context, output_id, revisions):
        return content_digest({"owner": self._context_owner(context), "output_id": output_id,
                               "revisions": revisions})

    def read_resource_bundle(self, context, root_reference, binding_output_id) -> dict:
        root = ResourceIdentity.from_dict(root_reference).to_dict()
        require(root["scope"] == "workspace" and root["type_id"] == MODEL_PROFILE_TYPE,
                "model_binding_unauthorized", "Model bundle requires its exact workspace profile root")
        with self._read() as connection:
            authority = self._authorize(connection, context, "models:call", "prepare-request",
                                        binding_output_id)
            require(authority["binding"]["reference"] == root, "model_binding_unauthorized",
                    "Requested root differs from the accepted source binding")
            profile = self._record(connection, root, 2)
            value = validate_model_profile(profile["value"])
            require(value["enabled"], "model_profile_disabled", "Current model configuration is disabled")
            dependencies = self._types.references(MODEL_PROFILE_TYPE, 2, value, scope="global")
            require(dependencies == [value["provider"]], "model_resource_references_invalid",
                    "Trusted profile must declare exactly its current provider dependency")
            provider_ref = ResourceIdentity.from_dict(dependencies[0]).to_dict()
            require(provider_ref["scope"] == "workspace" and provider_ref["type_id"] == CHAT_PROVIDER_TYPE,
                    "model_resource_references_invalid", "Model dependency requires the current provider type")
            provider = self._record(connection, provider_ref, 3)
            provider_value = validate_local_provider(provider["value"])
            require(provider_value["enabled"], "model_provider_disabled", "Current provider is disabled")
            revisions = {"configuration": {"reference": root, "update_sequence": profile["update_sequence"]},
                         "provider": {"reference": provider_ref, "update_sequence": provider["update_sequence"]}}
        self._read_bases.add(self._basis(context, binding_output_id, revisions))
        return deepcopy({"profile": profile, "provider": provider})

    def check_resource_bundle(self, context, root_reference, binding_output_id, revisions) -> bool:
        """Check heads of a previously authorized bundle without reloading values."""
        root = ResourceIdentity.from_dict(root_reference).to_dict()
        require(type(revisions) is dict and set(revisions) == {"configuration", "provider"},
                "model_resource_revision_invalid", "Bundle revision evidence is invalid")
        for key, type_id in (("configuration", MODEL_PROFILE_TYPE), ("provider", CHAT_PROVIDER_TYPE)):
            item = revisions[key]
            require(type(item) is dict and set(item) == {"reference", "update_sequence"}
                    and type(item["update_sequence"]) is int
                    and 1 <= item["update_sequence"] <= MAX_SAFE_INTEGER,
                    "model_resource_revision_invalid", "Bundle requires exact reference and sequence evidence")
            reference = ResourceIdentity.from_dict(item["reference"]).to_dict()
            require(reference["scope"] == "workspace" and reference["type_id"] == type_id,
                    "model_resource_revision_invalid", "Bundle revision has another resource type")
        with self._read() as connection:
            authority = self._authorize(connection, context, "models:call", "prepare-request",
                                        binding_output_id)
            require(root == authority["binding"]["reference"] == revisions["configuration"]["reference"],
                    "model_binding_unauthorized", "Bundle revision root differs from the accepted source")
            if self._basis(context, binding_output_id, revisions) not in self._read_bases:
                return False
            for item in revisions.values():
                reference = item["reference"]
                row = connection.execute(
                    "SELECT update_sequence,deleted FROM global_resource_current "
                    "WHERE scope=? AND type_id=? AND resource_id=?",
                    (reference["scope"], reference["type_id"], reference["resource_id"]),
                ).fetchone()
                if row is None or row["deleted"] or row["update_sequence"] != item["update_sequence"]:
                    return False
        return True
