"""M2 facts persisted by RuntimeFactStore, read under synthetic live authority."""

from contextlib import closing
from copy import deepcopy
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.graph_records import graph_record, validate_graph_record
from phase1_agent.model_request_contracts import MODEL_REQUEST_SERVICE_REF
from phase1_agent.model_request_fact_reader import CAPABILITY, OPERATION, ModelRequestFactReader
from phase1_agent.storage import SqliteStore
from test_model_request_integration import IntegratedHost


class FactHarness:
    """Reusable real-table fact reader fixture with uninstalled graph outputs."""

    def __init__(self, directory, request_count=1, *, integrated=None, preparations=None, results=None):
        self.integrated = IntegratedHost(directory) if integrated is None else integrated
        self.path = self.integrated.resources.path
        self.sql, self.results, self.preparations = [], [], []
        if integrated is None:
            for index in range(request_count):
                key = "normal:" + str(index + 1)
                prepared = self.integrated.mock.prepare(key=key)
                self.preparations.append(prepared)
                self.results.append(self.integrated.mock.dispatch(prepared, key=key))
        else:
            assert type(preparations) is list and type(results) is list and preparations
            assert len(preparations) == len(results)
            self.preparations, self.results = deepcopy(preparations), deepcopy(results)
        self.expected_facts = self.integrated.facts()
        resources = self.integrated.resources
        self.agent_owner = {key: getattr(resources.context, key) for key in (
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")}
        self.binding_ref = {"scope": "artifact", "output_id": resources.evidence["binding_output"]["output_id"]}
        self.receipts = {
            "schema_version": 2, "kind": "workflow.agent-receipts", "owner": self.agent_owner,
            "executor_ref": {"executor_id": "agents.snapshot-kernel", "exact_version": "2.0.0"},
            "snapshot_id": str(uuid4()), "frozen_prompt_ref": {"scope": "artifact", "output_id": str(uuid4())},
            "model_ref": self.binding_ref, "unit_id": str(uuid4()), "fact_ids": [str(uuid4())],
            "model_requests": [{
                "request_id": result["request_id"], "settings_id": result["settings_id"],
                "fact_ids": [ref["fact_id"] for ref in result["fact_refs"]],
            } for result in self.results],
        }
        output_id, consumer_id, consumer_run_id, edge_id = [str(uuid4()) for _ in range(4)]
        agent_run = deepcopy(resources.evidence["consumer_run"])
        agent_run["status"] = "succeeded"
        agent_run["output_refs"] = {"context": output_id}
        chain = deepcopy(resources.evidence["chain"])
        chain["ordered_nodes"].append(consumer_id)
        chain["node_run_ids"].append(consumer_run_id)
        chain["node_run_attempts"].append([consumer_run_id])
        chain["completed_nodes"].append(agent_run["node_binding_id"])
        chain["next_node_index"] = 2
        chain["targets"] = [consumer_id]
        chain["outputs"][agent_run["node_binding_id"]] = {"context": output_id}
        input_refs = {"context": [{"edge_id": edge_id, "output_id": output_id, "order": 0}]}
        consumer_run = graph_record(
            "node_run", profile="node", run_id=consumer_run_id,
            workflow_session_id=self.agent_owner["workflow_session_id"],
            chain_run_id=self.agent_owner["chain_run_id"], node_binding_id=consumer_id,
            status="running", revision=1, input_refs=input_refs, input_values={}, output_refs={},
            reads=[], effects=[], config={}, diagnostic=None)
        self.evidence = {
            "session": deepcopy(resources.evidence["session"]), "chain": chain,
            "consumer_run": consumer_run, "producer_run": agent_run,
            "input_output": graph_record(
                "workflow_output", output_id=output_id, workflow_session_id=agent_run["workflow_session_id"],
                chain_run_id=agent_run["chain_run_id"], run_id=agent_run["run_id"],
                node_binding_id=agent_run["node_binding_id"], port_id="context",
                payload={"schema_version": 2, "kind": "workflow.agent-context-update",
                         "owner": self.agent_owner, "receipts": self.receipts}),
            "model_binding_output": deepcopy(resources.evidence["binding_output"]),
            "model_source_run": deepcopy(resources.evidence["producer_run"]),
            "source_node": {"node_binding_id": agent_run["node_binding_id"],
                            "component_id": "agents.execute", "component_version": "9", "config": {}},
            "model_source_node": deepcopy(resources.evidence["source_node"]),
            "consumer_node": {
                "node_binding_id": consumer_id, "component_id": "synthetic.merge",
                "component_version": "1", "config": {}, "capabilities": [CAPABILITY],
                "service_requirements": [{**MODEL_REQUEST_SERVICE_REF.to_dict(),
                                          "capability": CAPABILITY, "operations": [OPERATION]}],
            },
        }
        self.context = SimpleNamespace(
            workflow_session_id=consumer_run["workflow_session_id"],
            chain_run_id=consumer_run["chain_run_id"], node_binding_id=consumer_id,
            node_run_id=consumer_run_id, workflow_definition_id=chain["workflow_definition_id"],
            input_artifact_refs=lambda port: deepcopy(input_refs.get(port, [])))
        with closing(SqliteStore(self.path)) as store:
            for key, kind in self.record_kinds().items():
                record = self.evidence[key]
                validate_graph_record(kind, record)
                identity_key = {"workflow_session": "workflow_session_id", "chain_run": "chain_run_id",
                                "node_run": "run_id", "workflow_output": "output_id"}[kind]
                store._connection.execute(
                    "INSERT INTO records VALUES(?,?,?,?,?) ON CONFLICT(record_type,record_id) "
                    "DO UPDATE SET payload=excluded.payload",
                    (kind, record[identity_key], canonical_bytes(record).decode("utf-8"), 0, "synthetic"))
        self.payload = {"port": "context", "producer": deepcopy(self.agent_owner),
                        "binding_ref": deepcopy(self.binding_ref),
                        "requests": deepcopy(self.receipts["model_requests"])}
        self.reader = self.make_reader()

    @classmethod
    def from_integrated(cls, integrated, *, preparations, results):
        return cls(integrated.resources.path.parent, integrated=integrated,
                   preparations=preparations, results=results)

    @staticmethod
    def record_kinds():
        return {"session": "workflow_session", "chain": "chain_run", "consumer_run": "node_run",
                "producer_run": "node_run", "input_output": "workflow_output",
                "model_binding_output": "workflow_output", "model_source_run": "node_run"}

    def make_reader(self, **overrides):
        return ModelRequestFactReader(
            self.path, load_fact_authority=overrides.get("load_fact_authority", self.load_authority),
            source_component=("agents.execute", "9"), model_source_component=("models.source", "7"))

    def load_authority(self, connection, context, port):
        assert connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        connection.set_trace_callback(self.sql.append)

        def get(kind, identity):
            row = connection.execute("SELECT payload FROM records WHERE record_type=? AND record_id=?",
                                     (kind, identity)).fetchone()
            return None if row is None else loads_strict(row["payload"])

        session = get("workflow_session", context.workflow_session_id)
        chain = get("chain_run", context.chain_run_id)
        consumer = get("node_run", context.node_run_id)
        if any(record is None for record in (session, chain, consumer)):
            return None
        refs = consumer["input_refs"].get(port)
        if type(refs) is not list or len(refs) != 1:
            return None
        output = get("workflow_output", refs[0]["output_id"])
        if output is None:
            return None
        producer = get("node_run", output["run_id"])
        if producer is None:
            return None
        model_refs = producer["input_refs"].get("model")
        if type(model_refs) is not list or len(model_refs) != 1:
            return None
        binding_output = get("workflow_output", model_refs[0]["output_id"])
        if binding_output is None:
            return None
        model_source = get("node_run", binding_output["run_id"])
        if model_source is None:
            return None
        return {"session": session, "chain": chain, "consumer_run": consumer, "producer_run": producer,
                "input_output": output, "model_binding_output": binding_output, "model_source_run": model_source,
                **{key: deepcopy(self.evidence[key])
                   for key in ("source_node", "model_source_node", "consumer_node")}}

    def persist(self, key):
        kind, record = self.record_kinds()[key], self.evidence[key]
        identity_key = {"workflow_session": "workflow_session_id", "chain_run": "chain_run_id",
                        "node_run": "run_id", "workflow_output": "output_id"}[kind]
        with closing(SqliteStore(self.path)) as store:
            store._connection.execute("UPDATE records SET payload=? WHERE record_type=? AND record_id=?",
                                      (canonical_bytes(record).decode("utf-8"), kind, record[identity_key]))

    def read(self):
        return self.reader(self.context, CAPABILITY, OPERATION, self.payload)

    def close(self):
        self.integrated.mock.close()


@pytest.fixture
def host(tmp_path):
    fixture = FactHarness(tmp_path)
    yield fixture
    fixture.close()


def test_reads_only_accepted_model_request_phases_without_mutation_or_communication(host):
    before = host.path.read_bytes()
    counts = (len(host.integrated.mock.communications), len(host.integrated.mock.broker.freeze_calls),
              len(host.integrated.mock.broker.authorize_calls))
    result = host.read()
    assert result == {"requests": [{
        "request_id": host.results[0]["request_id"], "settings_id": host.results[0]["settings_id"],
        "facts": host.expected_facts,
    }]}
    host.integrated.mock.close()
    assert host.read() == result
    assert host.path.read_bytes() == before
    assert counts == (len(host.integrated.mock.communications), len(host.integrated.mock.broker.freeze_calls),
                      len(host.integrated.mock.broker.authorize_calls))
    assert not any(statement.startswith(("CREATE", "INSERT", "UPDATE", "DELETE")) for statement in host.sql)
    assert not any("global_resource_current" in statement for statement in host.sql)
    assert b"synthetic-integration-key" not in canonical_bytes(result)
    result["requests"][0]["facts"][0]["details"]["messages"].clear()
    assert host.read()["requests"][0]["facts"][0]["details"]["messages"]


def test_multiple_requests_preserve_accepted_receipt_order(tmp_path):
    host = FactHarness(tmp_path, request_count=2)
    try:
        result = host.read()
        assert [item["request_id"] for item in result["requests"]] == [
            request["request_id"] for request in host.receipts["model_requests"]]
        assert [item["facts"] for item in result["requests"]] == [
            host.expected_facts[:3], host.expected_facts[3:]]
        host.payload["requests"].reverse()
        with pytest.raises(ContractValidationError) as error:
            host.read()
        assert error.value.reason_code == "model_fact_reference_denied"
    finally:
        host.close()


def test_existing_dispatched_requests_seal_without_new_model_calls(tmp_path):
    integrated = IntegratedHost(tmp_path)
    try:
        prepared = integrated.mock.prepare()
        result = integrated.mock.dispatch(prepared)
        counts = (len(integrated.mock.communications), len(integrated.mock.broker.freeze_calls),
                  len(integrated.mock.projections))
        host = FactHarness.from_integrated(integrated, preparations=[prepared], results=[result])
        assert host.read()["requests"][0]["facts"] == integrated.facts()
        assert counts == (len(integrated.mock.communications), len(integrated.mock.broker.freeze_calls),
                          len(integrated.mock.projections))
    finally:
        integrated.mock.close()


@pytest.mark.parametrize("field", [
    "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id", "workflow_definition_id",
])
def test_another_caller_or_workflow_cannot_read_facts(host, field):
    setattr(host.context, field, str(uuid4()))
    with pytest.raises(ContractValidationError):
        host.read()


@pytest.mark.parametrize("selector", ["port", "producer", "binding", "request", "settings", "fact", "phase_order"])
def test_client_cannot_select_another_source_or_arbitrary_facts(host, selector):
    if selector == "port":
        host.payload["port"] = "unbound"
    elif selector == "producer":
        host.payload["producer"]["node_run_id"] = str(uuid4())
    elif selector == "binding":
        host.payload["binding_ref"]["output_id"] = str(uuid4())
    elif selector == "request":
        host.payload["requests"][0]["request_id"] = str(uuid4())
    elif selector == "settings":
        host.payload["requests"][0]["settings_id"] = str(uuid4())
    elif selector == "fact":
        host.payload["requests"][0]["fact_ids"][0] = str(uuid4())
    else:
        host.payload["requests"][0]["fact_ids"].reverse()
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == "model_fact_reference_denied"


@pytest.mark.parametrize("change", ["capability", "operation", "service_version", "duplicate", "self_declared"])
def test_exact_host_service_grant_is_required(host, change):
    declaration = host.evidence["consumer_node"]
    host.context.definition = deepcopy(declaration)
    if change == "capability":
        declaration["capabilities"] = []
    elif change == "operation":
        declaration["service_requirements"][0]["operations"] = ["read-executor-facts"]
    elif change == "service_version":
        declaration["service_requirements"][0]["exact_version"] = "1.0.0"
    elif change == "duplicate":
        declaration["service_requirements"] *= 2
    else:
        declaration["service_requirements"] = []
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == "model_fact_reference_denied"


@pytest.mark.parametrize("key,field,value", [
    ("session", "active_chain_run_id", None), ("chain", "status", "paused"),
    ("producer_run", "status", "running"), ("consumer_run", "status", "succeeded"),
    ("model_source_run", "status", "running"), ("producer_run", "output_refs", {}),
    ("model_source_run", "output_refs", {}), ("chain", "outputs", {}),
    ("input_output", "port_id", "result"), ("model_binding_output", "port_id", "context"),
    ("input_output", "chain_run_id", "new_uuid"),
])
def test_live_accepted_origin_and_binding_are_required(host, key, field, value):
    host.evidence[key][field] = str(uuid4()) if value == "new_uuid" else value
    host.persist(key)
    with pytest.raises(ContractValidationError):
        host.read()


@pytest.mark.parametrize("key,field", [
    ("source_node", "component_id"), ("source_node", "component_version"),
    ("model_source_node", "component_id"), ("model_source_node", "component_version"),
])
def test_frozen_source_identity_is_exact_and_uninstalled(host, key, field):
    host.evidence[key][field] = "other"
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == "model_fact_reference_denied"


def test_receipts_one_cannot_authorize_model_fact_reads(host):
    receipts = host.evidence["input_output"]["payload"]["receipts"]
    receipts["schema_version"] = 1
    receipts["executor_ref"]["exact_version"] = "1.0.0"
    receipts.pop("model_requests")
    host.persist("input_output")
    with pytest.raises(ContractValidationError):
        host.read()


@pytest.mark.parametrize("change", ["owner", "model_ref", "request_id", "settings_id", "facts"])
def test_persisted_receipts_must_match_exact_source_and_accepted_request(host, change):
    receipts = host.evidence["input_output"]["payload"]["receipts"]
    if change == "owner":
        receipts["owner"] = {**receipts["owner"], "node_run_id": str(uuid4())}
    elif change == "model_ref":
        receipts["model_ref"] = {"scope": "artifact", "output_id": str(uuid4())}
    elif change == "request_id":
        receipts["model_requests"][0]["request_id"] = str(uuid4())
        host.payload["requests"] = deepcopy(receipts["model_requests"])
    elif change == "settings_id":
        receipts["model_requests"][0]["settings_id"] = str(uuid4())
        host.payload["requests"] = deepcopy(receipts["model_requests"])
    else:
        receipts["model_requests"][0]["fact_ids"][0] = str(uuid4())
        host.payload["requests"] = deepcopy(receipts["model_requests"])
    host.persist("input_output")
    with pytest.raises(ContractValidationError):
        host.read()


@pytest.mark.parametrize("column,value", [
    ("session_id", "new_uuid"), ("chain_id", "new_uuid"), ("node_run_id", "new_uuid"),
    ("stream_id", "executor:agents.snapshot-kernel@2.0.0"), ("sequence", 10),
])
def test_sql_fact_owner_stream_and_phase_sequence_are_exact(host, column, value):
    with closing(SqliteStore(host.path)) as store:
        store._connection.execute(
            "UPDATE workflow_runtime_facts SET " + column + "=? WHERE fact_id=?",
            (str(uuid4()) if value == "new_uuid" else value, host.expected_facts[0]["fact_id"]))
    with pytest.raises(ContractValidationError):
        host.read()


@pytest.mark.parametrize("field,value", [
    ("schema_version", 1), ("node_binding_id", "new_uuid"), ("request_id", "new_uuid"),
    ("binding_id", "new_uuid"), ("stage", "attempt"), ("sequence", True),
])
def test_fact_payload_must_match_exact_persisted_coordinates(host, field, value):
    fact = deepcopy(host.expected_facts[0])
    fact[field] = str(uuid4()) if value == "new_uuid" else value
    with closing(SqliteStore(host.path)) as store:
        store._connection.execute("UPDATE workflow_runtime_facts SET payload=? WHERE fact_id=?",
                                  (canonical_bytes(fact).decode("utf-8"), fact["fact_id"]))
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == "model_fact_storage_invalid"


def test_original_fact_model_input_must_match_producer_accepted_binding(host):
    fact = deepcopy(host.expected_facts[0])
    fact["details"]["input_refs"]["model"][0]["output_id"] = str(uuid4())
    with closing(SqliteStore(host.path)) as store:
        store._connection.execute("UPDATE workflow_runtime_facts SET payload=? WHERE fact_id=?",
                                  (canonical_bytes(fact).decode("utf-8"), fact["fact_id"]))
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == "model_fact_storage_invalid"


def test_missing_outcome_is_not_reconstructed_from_preparation_or_response(host):
    with closing(SqliteStore(host.path)) as store:
        store._connection.execute("DELETE FROM workflow_runtime_facts WHERE fact_id=?",
                                  (host.expected_facts[-1]["fact_id"],))
    counts = len(host.integrated.mock.communications)
    with pytest.raises(ContractValidationError) as error:
        host.read()
    assert error.value.reason_code == "model_fact_reference_denied"
    assert len(host.integrated.mock.communications) == counts


def test_fact_and_authority_reads_share_one_transaction(host):
    original = host.load_authority

    def change_after_authority(connection, context, port):
        evidence = original(connection, context, port)
        with closing(SqliteStore(host.path)) as store:
            store._connection.execute("DELETE FROM workflow_runtime_facts WHERE fact_id=?",
                                      (host.expected_facts[-1]["fact_id"],))
        return evidence

    reader = host.make_reader(load_fact_authority=change_after_authority)
    assert reader.read(host.context, host.payload)["requests"][0]["facts"] == host.expected_facts
    with pytest.raises(ContractValidationError):
        host.read()


def test_invalid_operation_payload_missing_authority_and_readonly_bridge(host, tmp_path):
    with pytest.raises(ContractValidationError):
        host.reader(host.context, "facts:read", OPERATION, host.payload)
    with pytest.raises(ContractValidationError):
        host.reader.read(host.context, {**host.payload, "fact_ids": []})
    reader = host.make_reader(load_fact_authority=lambda *args: None)
    with pytest.raises(ContractValidationError):
        reader.read(host.context, host.payload)

    def write_attempt(connection, context, port):
        connection.execute("DELETE FROM workflow_runtime_facts")

    reader = host.make_reader(load_fact_authority=write_attempt)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.read(host.context, host.payload)
    missing = tmp_path / "missing.sqlite"
    reader = ModelRequestFactReader(
        missing, load_fact_authority=host.load_authority,
        source_component=("agents.execute", "9"), model_source_component=("models.source", "7"))
    with pytest.raises(sqlite3.OperationalError):
        reader.read(host.context, host.payload)
    assert not missing.exists()
