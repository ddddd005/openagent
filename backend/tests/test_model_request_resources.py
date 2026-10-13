"""Synthetic persisted authority and current-resource tests; no product I/O."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.global_resources import GlobalResourceStore
from phase1_agent.graph_records import graph_record, validate_graph_record
from phase1_agent.host_sdk import DataTypeDefinition, ResourceIdentity, TypeRegistry
from phase1_agent.model_contract import CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE, model_type_definitions
from phase1_agent.model_request_contracts import (
    MODEL_REQUEST_SERVICE_REF, default_model_runtime, model_profile_schema,
    validate_model_profile,
)
from phase1_agent.model_request_resources import ModelRequestResourceReader
from phase1_agent.storage import SqliteStore


def identity(record):
    return ResourceIdentity(record["scope"], record["type_id"], record["resource_id"]).to_dict()


class ResourceHarness:
    """A trusted test host reads graph evidence through the reader's connection."""

    def __init__(self, directory):
        self.path = directory / "资源 current %# database.sqlite"
        self.types = TypeRegistry()
        self.references_hook = None
        self.sql = []
        self.types.register(model_type_definitions()[0])
        self.types.register(DataTypeDefinition(
            MODEL_PROFILE_TYPE, 2, model_profile_schema(), scope="global",
            validator=validate_model_profile, max_bytes=8192, references=self.references,
            reference_mapper=lambda value, mapping: deepcopy(value)))
        self.provider = {
            "envelope_version": 1, "scope": "workspace", "type_id": CHAT_PROVIDER_TYPE,
            "resource_id": str(uuid4()), "data_schema_version": 3, "update_sequence": 1,
            "value": {"name": "Synthetic provider", "protocol": "chat", "base_url": "https://example.test",
                      "credential_ref": None, "enabled": True},
        }
        self.profile = {
            "envelope_version": 1, "scope": "workspace", "type_id": MODEL_PROFILE_TYPE,
            "resource_id": str(uuid4()), "data_schema_version": 2, "update_sequence": 1,
            "value": {
                "name": "Synthetic profile", "provider": identity(self.provider),
                "model": {"id": "mock-model", "capacity": {"context_window_tokens": 400000,
                          "input_limit_tokens": 400000, "max_output_tokens": 64000}},
                "runtime": default_model_runtime(), "enabled": True,
            },
        }
        sid, cid, wid, source, target, producer_id, consumer_id, output_id, edge_id = [
            str(uuid4()) for _ in range(9)]
        self.binding = {"schema_version": 5, "kind": "workflow.model-binding",
                        "binding_id": str(uuid4()), "reference": identity(self.profile),
                        "authorization": {"purposes": ["normal", "compaction"]}}
        source_config = {"reference": identity(self.profile)}
        common = {"profile": "node", "workflow_session_id": sid, "chain_run_id": cid,
                  "revision": 1, "reads": [], "effects": [], "diagnostic": None}
        input_refs = {"model": [{"edge_id": edge_id, "output_id": output_id, "order": 0}]}
        self.evidence = {
            "session": graph_record(
                "workflow_session", workflow_session_id=sid, workflow_definition_id=wid,
                definition_revision=1, revision=1, source={}, active_chain_run_id=cid),
            "chain": graph_record(
                "chain_run", chain_run_id=cid, workflow_session_id=sid,
                workflow_definition_id=wid, definition_revision=1, status="running", revision=1,
                targets=[target], ordered_nodes=[source, target], node_run_ids=[producer_id, consumer_id],
                completed_nodes=[source], next_node_index=1, inputs={}, outputs={source: {"output": output_id}},
                diagnostic=None),
            "consumer_run": graph_record(
                "node_run", **common, run_id=consumer_id, node_binding_id=target, status="running",
                input_refs=input_refs, input_values={}, output_refs={}, config={}),
            "producer_run": graph_record(
                "node_run", **common, run_id=producer_id, node_binding_id=source, status="succeeded",
                input_refs={}, input_values={}, output_refs={"output": output_id}, config=source_config),
            "binding_output": graph_record(
                "workflow_output", output_id=output_id, workflow_session_id=sid, chain_run_id=cid,
                run_id=producer_id, node_binding_id=source, port_id="output", payload=self.binding),
            "source_node": {"node_binding_id": source, "component_id": "models.source",
                            "component_version": "7", "config": source_config},
            "consumer_node": {"node_binding_id": target, "component_id": "synthetic.consumer",
                              "component_version": "1", "config": {},
                              "capabilities": ["models:call", "model-projection:read"],
                              "service_requirements": [
                                  {**MODEL_REQUEST_SERVICE_REF.to_dict(), "capability": "models:call",
                                   "operations": ["prepare-request", "dispatch-request", "discard-prepared-request"]},
                                  {**MODEL_REQUEST_SERVICE_REF.to_dict(), "capability": "model-projection:read",
                                   "operations": ["measure-context"]},
                              ]},
        }
        self.context = SimpleNamespace(
            workflow_session_id=sid, chain_run_id=cid, node_binding_id=target,
            node_run_id=consumer_id, workflow_definition_id=wid,
            input_artifact_refs=lambda port: deepcopy(input_refs.get(port, [])))
        with closing(SqliteStore(self.path)) as store:
            store._connection.execute("PRAGMA journal_mode=WAL")
            catalog = GlobalResourceStore(store, self.types)
            for record in (self.provider, self.profile):
                catalog.write(record, expected_sequence=0, idempotency_key=str(uuid4()))
            for key, kind in self.record_kinds().items():
                record = self.evidence[key]
                validate_graph_record(kind, record)
                record_id = record[{"workflow_session": "workflow_session_id", "chain_run": "chain_run_id",
                                    "node_run": "run_id", "workflow_output": "output_id"}[kind]]
                store._connection.execute(
                    "INSERT INTO records VALUES(?,?,?,?,?)",
                    (kind, record_id, canonical_bytes(record).decode("utf-8"), 0, "synthetic"))
        self.reader = self.make_reader()

    @staticmethod
    def record_kinds():
        return {"session": "workflow_session", "chain": "chain_run", "consumer_run": "node_run",
                "producer_run": "node_run", "binding_output": "workflow_output"}

    def references(self, value):
        if self.references_hook is not None:
            return self.references_hook(value)
        return [deepcopy(value["provider"])]

    def load_authority(self, connection, context, output_id):
        connection.set_trace_callback(self.sql.append)
        assert connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        result = {}
        selectors = {"session": ("workflow_session_id", context.workflow_session_id),
                     "chain": ("chain_run_id", context.chain_run_id),
                     "consumer_run": ("run_id", context.node_run_id),
                     "binding_output": ("output_id", output_id)}
        for key, (_, record_id) in selectors.items():
            row = connection.execute(
                "SELECT payload FROM records WHERE record_type=? AND record_id=?",
                (self.record_kinds()[key], record_id)).fetchone()
            if row is None:
                return None
            result[key] = loads_strict(row["payload"])
        row = connection.execute("SELECT payload FROM records WHERE record_type='node_run' AND record_id=?",
                                 (result["binding_output"]["run_id"],)).fetchone()
        if row is None:
            return None
        result["producer_run"] = loads_strict(row["payload"])
        result["source_node"] = deepcopy(self.evidence["source_node"])
        result["consumer_node"] = deepcopy(self.evidence["consumer_node"])
        return result

    def make_reader(self, **overrides):
        return ModelRequestResourceReader(
            self.path, types=overrides.get("types", self.types),
            load_binding_authority=overrides.get("load_binding_authority", self.load_authority),
            source_component=("models.source", "7"))

    def persist_evidence(self, key):
        kind = self.record_kinds()[key]
        record = self.evidence[key]
        id_key = {"workflow_session": "workflow_session_id", "chain_run": "chain_run_id",
                  "node_run": "run_id", "workflow_output": "output_id"}[kind]
        with closing(SqliteStore(self.path)) as store:
            store._connection.execute("UPDATE records SET payload=? WHERE record_type=? AND record_id=?",
                                      (canonical_bytes(record).decode("utf-8"), kind, record[id_key]))

    def save(self, record):
        with closing(SqliteStore(self.path)) as store:
            GlobalResourceStore(store, self.types).write(
                record, expected_sequence=record["update_sequence"] - 1, idempotency_key=str(uuid4()))

    def read(self):
        return self.reader.read_resource_bundle(
            self.context, self.binding["reference"], self.evidence["binding_output"]["output_id"])

    def revisions(self, bundle):
        return {"configuration": {"reference": identity(bundle["profile"]),
                                  "update_sequence": bundle["profile"]["update_sequence"]},
                "provider": {"reference": identity(bundle["provider"]),
                             "update_sequence": bundle["provider"]["update_sequence"]}}

    def check(self, revisions):
        return self.reader.check_resource_bundle(
            self.context, self.binding["reference"], self.evidence["binding_output"]["output_id"], revisions)


@pytest.fixture
def host(tmp_path):
    return ResourceHarness(tmp_path)


def test_exact_binding_current_records_and_readonly_database(host):
    before = host.path.read_bytes()
    bundle = host.read()
    assert bundle == {"profile": host.profile, "provider": host.provider}
    authority = host.reader.authorize_binding(host.context, "models:call", "prepare-request",
                                             host.binding["binding_id"])
    assert authority == {
        "binding": host.binding, "binding_output_id": host.evidence["binding_output"]["output_id"],
        "producer_node_run_id": host.evidence["producer_run"]["run_id"],
        "input_refs": host.evidence["consumer_run"]["input_refs"],
    }
    assert host.check(host.revisions(bundle))
    assert host.path.read_bytes() == before
    assert not any(statement.startswith(("INSERT", "UPDATE", "DELETE", "CREATE")) for statement in host.sql)
    bundle["profile"]["value"]["name"] = "Client mutation"
    assert host.read()["profile"]["value"]["name"] == "Synthetic profile"


@pytest.mark.parametrize("capability,operation", [
    ("models:call", "prepare-request"), ("models:call", "dispatch-request"),
    ("models:call", "discard-prepared-request"), ("model-projection:read", "measure-context"),
])
def test_each_operation_requires_its_exact_grant(host, capability, operation):
    assert host.reader.authorize_binding(host.context, capability, operation,
                                        host.binding["binding_id"])["binding"] == host.binding
    host.evidence["consumer_node"]["service_requirements"] = []
    with pytest.raises(ContractValidationError) as error:
        host.reader.authorize_binding(host.context, capability, operation, host.binding["binding_id"])
    assert error.value.reason_code == "model_binding_unauthorized"


@pytest.mark.parametrize("context_field", [
    "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id", "workflow_definition_id",
])
def test_another_runtime_or_workflow_cannot_select_binding(host, context_field):
    setattr(host.context, context_field, str(uuid4()))
    with pytest.raises(ContractValidationError):
        host.read()


@pytest.mark.parametrize("key,field,value", [
    ("session", "active_chain_run_id", None),
    ("chain", "status", "paused"),
    ("consumer_run", "status", "succeeded"),
    ("producer_run", "status", "running"),
    ("producer_run", "output_refs", {}),
    ("binding_output", "port_id", "other"),
    ("binding_output", "workflow_session_id", "new_uuid"),
    ("binding_output", "chain_run_id", "new_uuid"),
    ("binding_output", "node_binding_id", "new_uuid"),
    ("chain", "outputs", {}),
    ("consumer_run", "input_refs", {}),
])
def test_missing_acceptance_or_wrong_exact_output_is_rejected(host, key, field, value):
    host.evidence[key][field] = str(uuid4()) if value == "new_uuid" else value
    host.persist_evidence(key)
    with pytest.raises(ContractValidationError):
        host.read()


def test_client_input_binding_id_root_and_fake_grants_do_not_authorize(host):
    host.context.definition = host.evidence["consumer_node"]
    with pytest.raises(ContractValidationError):
        host.reader.authorize_binding(host.context, "models:call", "prepare-request", str(uuid4()))
    other_root = {**host.binding["reference"], "resource_id": str(uuid4())}
    with pytest.raises(ContractValidationError):
        host.reader.read_resource_bundle(host.context, other_root, host.evidence["binding_output"]["output_id"])
    host.context.input_artifact_refs = lambda port: [
        {**host.evidence["consumer_run"]["input_refs"]["model"][0], "edge_id": str(uuid4())}]
    with pytest.raises(ContractValidationError):
        host.read()


def test_context_self_declared_grant_does_not_override_trusted_host(host):
    host.context.definition = deepcopy(host.evidence["consumer_node"])
    host.evidence["consumer_node"]["service_requirements"] = []
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == "model_binding_unauthorized"


@pytest.mark.parametrize("change", ["root", "component", "version", "service_version", "capability", "operation"])
def test_original_frozen_source_and_service_declaration_are_required(host, change):
    source, consumer = host.evidence["source_node"], host.evidence["consumer_node"]
    if change == "root":
        source["config"] = {"reference": {**host.binding["reference"], "resource_id": str(uuid4())}}
    elif change == "component":
        source["component_id"] = "client.source"
    elif change == "version":
        source["component_version"] = "6"
    elif change == "service_version":
        consumer["service_requirements"][0]["exact_version"] = "1.0.0"
    elif change == "capability":
        consumer["capabilities"] = []
    else:
        consumer["service_requirements"][0]["operations"] = ["dispatch-request"]
    with pytest.raises(ContractValidationError):
        host.read()


def test_current_provider_rebinding_needs_no_initial_whitelist(host):
    first = host.read()
    rebound = deepcopy(host.provider)
    rebound["resource_id"] = str(uuid4())
    rebound["value"]["base_url"] = "https://second.example.test"
    host.save(rebound)
    updated = deepcopy(host.profile)
    updated["update_sequence"] = 2
    updated["value"]["provider"] = identity(rebound)
    updated["value"]["runtime"]["output_limit_tokens"] = 16000
    host.save(updated)
    second = host.read()
    assert second == {"profile": updated, "provider": rebound}
    assert first == {"profile": host.profile, "provider": host.provider}
    assert not host.check(host.revisions(first))
    assert host.check(host.revisions(second))


def test_one_read_transaction_keeps_profile_and_provider_coherent(host):
    changed_profile, changed_provider = deepcopy(host.profile), deepcopy(host.provider)
    changed_profile["update_sequence"] = changed_provider["update_sequence"] = 2
    changed_profile["value"]["name"] = "After save"
    changed_provider["value"]["base_url"] = "https://after.example.test"

    def update_during_references(value):
        host.references_hook = None
        host.save(changed_profile)
        host.save(changed_provider)
        return [value["provider"]]

    host.references_hook = update_during_references
    assert host.read() == {"profile": host.profile, "provider": host.provider}
    assert host.read() == {"profile": changed_profile, "provider": changed_provider}


@pytest.mark.parametrize("refs", ["none", "extra", "other"])
def test_only_exact_trusted_profile_references_can_read_provider(host, refs):
    def incorrect(value):
        if refs == "none":
            return []
        other = {**value["provider"], "resource_id": str(uuid4())}
        return [value["provider"], other] if refs == "extra" else [other]
    host.references_hook = incorrect
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == "model_resource_references_invalid"


@pytest.mark.parametrize("reference", [
    {"scope": "artifact", "output_id": str(uuid4())},
    {"scope": "session", "workflow_session_id": str(uuid4()),
     "object_key": "unrelated", "revision_id": str(uuid4())},
])
def test_nonresource_trusted_reference_does_not_expand_model_dependency_reads(host, reference):
    host.references_hook = lambda value: [reference]
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == "model_resource_references_invalid"


@pytest.mark.parametrize("kind,problem,code", [
    ("profile", "disabled", "model_profile_disabled"),
    ("provider", "disabled", "model_provider_disabled"),
    ("profile", "deleted", "model_resource_missing"),
    ("provider", "deleted", "model_resource_missing"),
    ("profile", "schema", "model_resource_schema_unsupported"),
    ("provider", "schema", "model_resource_schema_unsupported"),
])
def test_unavailable_current_resource_fails_without_old_value_fallback(host, kind, problem, code):
    record = deepcopy(getattr(host, kind))
    if problem == "disabled":
        record["value"]["enabled"] = False
        record["update_sequence"] += 1
        host.save(record)
    else:
        with closing(SqliteStore(host.path)) as store:
            if problem == "deleted":
                GlobalResourceStore(store, host.types).delete(
                    identity(record), expected_sequence=1, idempotency_key=str(uuid4()))
            else:
                record["data_schema_version"] = 1
                store._connection.execute(
                    "UPDATE global_resource_current SET payload=? WHERE resource_id=?",
                    (canonical_bytes(record).decode("utf-8"), record["resource_id"]))
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == code


def test_check_only_authorized_revision_heads_without_references_or_body_reload(host):
    revisions = host.revisions({"profile": host.profile, "provider": host.provider})
    assert not host.check(revisions)
    host.read()
    host.sql.clear()
    host.references_hook = lambda value: pytest.fail("Recheck must not resolve configuration again")
    assert host.check(revisions)
    assert all("payload" not in query for query in host.sql if "global_resource_current" in query)
    assert not host.check({**revisions, "provider": {
        "reference": {**revisions["provider"]["reference"], "resource_id": str(uuid4())},
        "update_sequence": 1}})
    changed = deepcopy(host.provider)
    changed["update_sequence"] = 2
    host.save(changed)
    assert not host.check(revisions)


def test_exact_accepted_type_evidence_is_required_and_never_initialized_on_read(host):
    with closing(SqliteStore(host.path)) as store:
        store._connection.execute(
            "DELETE FROM registered_type_contracts WHERE type_id=?", (MODEL_PROFILE_TYPE,))
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == "type_contract_evidence_missing"
    with closing(SqliteStore(host.path)) as store:
        assert store._connection.execute(
            "SELECT COUNT(*) FROM registered_type_contracts WHERE type_id=?", (MODEL_PROFILE_TYPE,)).fetchone()[0] == 0


def test_trusted_registry_definition_cannot_silently_replace_persisted_contract(host):
    types = TypeRegistry()
    for definition in host.types.definitions():
        types.register(replace(definition, max_bytes=4096))
    reader = host.make_reader(types=types)
    with pytest.raises(ContractValidationError) as error:
        reader.read_resource_bundle(host.context, host.binding["reference"],
                                    host.evidence["binding_output"]["output_id"])
    assert error.value.reason_code == "type_contract_redefined"


@pytest.mark.parametrize("field,value", [
    ("envelope_version", True), ("update_sequence", True),
    ("update_sequence", 2), ("resource_id", str(uuid4())),
])
def test_current_resource_envelope_must_match_exact_sql_row(host, field, value):
    record = deepcopy(host.profile)
    record[field] = value
    with closing(SqliteStore(host.path)) as store:
        store._connection.execute(
            "UPDATE global_resource_current SET payload=? WHERE resource_id=?",
            (canonical_bytes(record).decode("utf-8"), host.profile["resource_id"]))
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == "model_resource_invalid"


def test_trusted_bridge_cannot_mutate_the_reader_transaction(host):
    def write_attempt(connection, context, output_id):
        connection.execute("UPDATE global_resource_current SET deleted=1,payload=NULL")
        return host.load_authority(connection, context, output_id)
    reader = host.make_reader(load_binding_authority=write_attempt)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.read_resource_bundle(host.context, host.binding["reference"],
                                    host.evidence["binding_output"]["output_id"])
    assert host.read() == {"profile": host.profile, "provider": host.provider}


def test_missing_authority_rejects_and_nonexistent_read_uri_creates_no_database(host, tmp_path):
    reader = host.make_reader(load_binding_authority=lambda *args: None)
    with pytest.raises(ContractValidationError):
        reader.read_resource_bundle(host.context, host.binding["reference"],
                                    host.evidence["binding_output"]["output_id"])
    missing = tmp_path / "does not exist.sqlite"
    reader = ModelRequestResourceReader(
        missing, types=host.types, load_binding_authority=host.load_authority,
        source_component=("models.source", "7"))
    with pytest.raises(sqlite3.OperationalError):
        reader.read_resource_bundle(host.context, host.binding["reference"],
                                    host.evidence["binding_output"]["output_id"])
    assert not missing.exists()
