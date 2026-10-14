"""Uninstalled live model-fact reader with exact accepted-input authority."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3

from .context_contract import artifact_ref
from .contract_json import canonical_bytes, loads_strict, validate_json_value
from .graph_records import require, uuid_value, validate_graph_record
from .model_execution_contracts import (
    model_request_stream_id, validate_agent_receipts_v2,
)
from .model_request_contracts import (
    MODEL_REQUEST_SERVICE_REF, validate_model_binding, validate_model_settings,
)
from .runtime_hosting import InvocationOwner


CAPABILITY = "model-facts:read"
OPERATION = "read-model-request-facts"


def _requests(value):
    require(type(value) is list and 0 < len(value) <= 4096,
            "model_fact_reference_invalid", "Model request references must be a bounded array")
    request_ids, settings_ids, fact_ids = set(), set(), set()
    for request in value:
        require(type(request) is dict and set(request) == {"request_id", "settings_id", "fact_ids"}
                and uuid_value(request["request_id"]) and uuid_value(request["settings_id"])
                and type(request["fact_ids"]) is list and len(request["fact_ids"]) == 3
                and all(uuid_value(identity) for identity in request["fact_ids"])
                and len(set(request["fact_ids"])) == 3
                and request["request_id"] not in request_ids
                and request["settings_id"] not in settings_ids
                and not fact_ids.intersection(request["fact_ids"]),
                "model_fact_reference_invalid", "Model requests require distinct exact phase identities")
        request_ids.add(request["request_id"])
        settings_ids.add(request["settings_id"])
        fact_ids.update(request["fact_ids"])
    return deepcopy(value)


class ModelRequestFactReader:
    """Read facts listed by a live Agent receipt input accepted by context.merge.

    The trusted host bridge ``load_fact_authority(connection, context, port)``
    derives producer and model binding from the consumer's persisted receipts input.
    It returns graph records and frozen declarations, never client declarations.
    Historical artifact closure and final node/service installation are separate.
    """

    def __init__(self, database_path, *, load_fact_authority,
                 source_component: tuple[str, str], model_source_component: tuple[str, str]):
        require(callable(load_fact_authority)
                and all(type(component) is tuple and len(component) == 2
                        and all(type(item) is str and bool(item) for item in component)
                        for component in (source_component, model_source_component)),
                "model_fact_host_invalid", "Exact trusted host dependencies are required")
        self._uri = Path(database_path).resolve().as_uri() + "?mode=ro"
        self._load_authority = load_fact_authority
        self._source_component = source_component
        self._model_source_component = model_source_component

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
    def _owner(context):
        return InvocationOwner.from_dict({key: getattr(context, key, None) for key in (
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")}).to_dict()

    @staticmethod
    def _node_owner(run):
        return {"workflow_session_id": run["workflow_session_id"], "chain_run_id": run["chain_run_id"],
                "node_binding_id": run["node_binding_id"], "node_run_id": run["run_id"]}

    def _authorize(self, connection, context, payload):
        owner = self._owner(context)
        evidence = self._load_authority(connection, context, payload["port"])
        require(type(evidence) is dict and set(evidence) == {
            "session", "chain", "consumer_run", "producer_run", "input_output",
            "model_binding_output", "model_source_run", "source_node", "model_source_node", "consumer_node",
        }, "model_fact_reference_denied", "Trusted accepted-input authority evidence is missing", 403)
        records = {key: validate_graph_record(kind, evidence[key]) for key, kind in (
            ("session", "workflow_session"), ("chain", "chain_run"), ("consumer_run", "node_run"),
            ("producer_run", "node_run"), ("input_output", "workflow_output"),
            ("model_binding_output", "workflow_output"), ("model_source_run", "node_run"),
        )}
        session, chain = records["session"], records["chain"]
        consumer, producer, model_source = (records[key] for key in (
            "consumer_run", "producer_run", "model_source_run"))
        output, binding_output = records["input_output"], records["model_binding_output"]
        producer_owner = self._node_owner(producer)
        require(self._node_owner(consumer) == owner
                and producer_owner == payload["producer"]
                and session["workflow_session_id"] == owner["workflow_session_id"]
                and session["active_chain_run_id"] == owner["chain_run_id"]
                and chain["chain_run_id"] == owner["chain_run_id"] and chain["status"] == "running"
                and chain["workflow_session_id"] == owner["workflow_session_id"]
                and (chain["workflow_definition_id"], chain["definition_revision"])
                    == (session["workflow_definition_id"], session["definition_revision"])
                and consumer["status"] == "running"
                and producer["status"] == model_source["status"] == "succeeded"
                and all(item["workflow_session_id"] == owner["workflow_session_id"]
                        and item["chain_run_id"] == owner["chain_run_id"]
                        for item in (producer, model_source, output, binding_output)),
                "model_fact_reference_denied", "Facts require the exact active accepted producer invocation", 403)
        declared_workflow = getattr(context, "workflow_definition_id", None)
        require(declared_workflow is None or declared_workflow == chain["workflow_definition_id"],
                "model_fact_reference_denied", "Caller belongs to another workflow", 403)
        require(output["run_id"] == producer["run_id"]
                and output["node_binding_id"] == producer["node_binding_id"]
                and output["port_id"] == "receipts"
                and producer["output_refs"].get("receipts") == output["output_id"]
                and binding_output["run_id"] == model_source["run_id"]
                and binding_output["node_binding_id"] == model_source["node_binding_id"]
                and binding_output["port_id"] == "output"
                and model_source["output_refs"].get("output") == binding_output["output_id"],
                "model_fact_reference_denied", "Input and binding lack exact accepted source outputs", 403)
        nodes = chain["ordered_nodes"]
        require(all(run["node_binding_id"] in nodes for run in (model_source, producer, consumer)),
                "model_fact_reference_denied", "Source or consumer is absent from this execution plan", 403)
        indices = [nodes.index(run["node_binding_id"]) for run in (model_source, producer, consumer)]
        require(indices[0] < indices[1] < indices[2] == chain["next_node_index"]
                and all(chain["node_run_ids"][index] == run["run_id"]
                        for index, run in zip(indices, (model_source, producer, consumer)))
                and chain["outputs"].get(producer["node_binding_id"], {}).get("receipts") == output["output_id"]
                and chain["outputs"].get(model_source["node_binding_id"], {}).get("output")
                    == binding_output["output_id"],
                "model_fact_reference_denied", "Accepted receipts differ from exact chain progress", 403)
        refs = context.input_artifact_refs(payload["port"])
        require(type(refs) is list and len(refs) == 1
                and refs == consumer["input_refs"].get(payload["port"])
                and refs[0]["output_id"] == output["output_id"],
                "model_fact_reference_denied", "Caller input differs from its persisted accepted receipts", 403)
        model_refs = producer["input_refs"].get("model")
        require(type(model_refs) is list and len(model_refs) == 1
                and model_refs[0]["output_id"] == payload["binding_ref"]["output_id"]
                    == binding_output["output_id"],
                "model_fact_reference_denied", "Binding is outside the accepted Agent model input", 403)
        for key, run, component in (
            ("source_node", producer, self._source_component),
            ("model_source_node", model_source, self._model_source_component),
        ):
            declaration = evidence[key]
            require(type(declaration) is dict and set(declaration) == {
                "node_binding_id", "component_id", "component_version", "config",
            } and declaration["node_binding_id"] == run["node_binding_id"]
                and (declaration["component_id"], declaration["component_version"]) == component
                and declaration["config"] == run["config"],
                "model_fact_reference_denied", "Producer lacks its exact frozen host declaration", 403)
        declaration = evidence["consumer_node"]
        service = MODEL_REQUEST_SERVICE_REF.to_dict()
        require(type(declaration) is dict and declaration.get("node_binding_id") == owner["node_binding_id"]
                and (declaration.get("component_id"), declaration.get("component_version"))
                    == ("context.merge", "6")
                and type(declaration.get("capabilities")) in (list, tuple)
                and CAPABILITY in declaration["capabilities"]
                and type(declaration.get("service_requirements")) in (list, tuple),
                "model_fact_reference_denied", "Consumer lacks a trusted fact-read declaration", 403)
        grants = [grant for grant in declaration["service_requirements"]
                  if type(grant) is dict and grant.get("service_id") == service["service_id"]
                  and grant.get("exact_version") == service["exact_version"]
                  and grant.get("capability") == CAPABILITY
                  and type(grant.get("operations")) is list and OPERATION in grant["operations"]]
        require(len(grants) == 1 and set(grants[0]) == {
            "service_id", "exact_version", "capability", "operations",
        }, "model_fact_reference_denied", "Consumer lacks one exact model-fact operation grant", 403)
        binding = validate_model_binding(binding_output["payload"])
        source_config = evidence["model_source_node"]["config"]
        require(type(source_config) is dict and set(source_config) == {"reference"}
                and source_config["reference"] == binding["reference"],
                "model_fact_reference_denied", "Binding differs from its original model source root", 403)
        packet = output["payload"]
        require(type(packet) is dict and type(packet.get("schema_version")) is int
                and packet["schema_version"] == 2 and packet.get("kind") == "workflow.agent-receipts",
                "model_fact_reference_denied", "Accepted input must be Agent receipts schema 2", 403)
        receipts = validate_agent_receipts_v2(packet)
        require(receipts["owner"] == producer_owner and receipts["model_ref"] == payload["binding_ref"],
                "model_fact_reference_denied", "Accepted receipts refer to another Agent or model binding", 403)
        policy_refs = producer["input_refs"].get("compaction_policy", [])
        require(type(policy_refs) is list and len(policy_refs) in (0, 1)
                and len(policy_refs) == (0 if receipts["compaction_policy_ref"] is None else 1)
                and (not policy_refs
                     or policy_refs[0]["output_id"] == receipts["compaction_policy_ref"]["output_id"]),
                "model_fact_reference_denied",
                "Receipt policy reference differs from the accepted Agent input", 403)
        return producer_owner, binding, _requests(receipts["model_requests"]), deepcopy(producer["input_refs"])

    @staticmethod
    def _facts(connection, owner, binding, request, input_refs):
        stream = model_request_stream_id(request["request_id"])
        result = []
        for index, (stage, identity) in enumerate(zip(
            ("request", "attempt", "outcome"), request["fact_ids"]), 1):
            row = connection.execute(
                "SELECT session_id,chain_id,node_run_id,stream_id,sequence,payload "
                "FROM workflow_runtime_facts WHERE session_id=? AND chain_id=? AND node_run_id=? "
                "AND stream_id=? AND fact_id=?",
                (owner["workflow_session_id"], owner["chain_run_id"], owner["node_run_id"], stream, identity),
            ).fetchone()
            require(row is not None, "model_fact_reference_denied",
                    "Fact is outside the accepted model request owner and service stream", 403)
            fact = loads_strict(row["payload"])
            require(type(fact) is dict and set(fact) == {
                "schema_version", "kind", "fact_id", "session_id", "chain_run_id", "node_binding_id",
                "node_run_id", "binding_id", "request_id", "stage", "sequence", "details",
            } and type(fact["schema_version"]) is int and fact["schema_version"] == 2
                and fact["kind"] == "workflow.model-fact" and fact["fact_id"] == identity
                and fact["session_id"] == owner["workflow_session_id"]
                and fact["chain_run_id"] == owner["chain_run_id"]
                and fact["node_binding_id"] == owner["node_binding_id"]
                and fact["node_run_id"] == owner["node_run_id"]
                and fact["binding_id"] == binding["binding_id"]
                and fact["request_id"] == request["request_id"] and fact["stage"] == stage
                and type(fact["sequence"]) is int and fact["sequence"] == row["sequence"] == index
                and type(fact["details"]) is dict,
                "model_fact_storage_invalid", "Persisted model fact differs from its exact phase coordinates", 500)
            result.append(fact)
        settings = validate_model_settings(result[0]["details"].get("settings"))
        require(settings["settings_id"] == request["settings_id"]
                and settings["request_id"] == request["request_id"]
                and settings["binding_id"] == binding["binding_id"]
                and settings["configuration"]["reference"] == binding["reference"]
                and settings["purpose"] in binding["authorization"]["purposes"],
                "model_fact_reference_denied", "Accepted request facts refer to another settings identity", 403)
        require(result[0]["details"].get("input_refs") == input_refs,
                "model_fact_storage_invalid", "Accepted request differs from its exact producer inputs", 500)
        return result

    def __call__(self, context, capability, operation, payload):
        require(capability == CAPABILITY and operation == OPERATION,
                "model_fact_operation_invalid", "Only the exact model-fact read operation is available")
        return self.read(context, payload)

    def read(self, context, payload):
        validate_json_value(payload)
        require(type(payload) is dict and set(payload) == {"port", "producer", "binding_ref", "requests"}
                and type(payload["port"]) is str and 0 < len(payload["port"]) <= 128,
                "model_fact_reference_invalid", "Model fact read fields are invalid")
        payload = deepcopy(payload)
        payload["producer"] = InvocationOwner.from_dict(payload["producer"]).to_dict()
        payload["binding_ref"] = artifact_ref(payload["binding_ref"])
        requests = _requests(payload["requests"])
        with self._read() as connection:
            owner, binding, accepted, input_refs = self._authorize(connection, context, payload)
            require(requests == accepted, "model_fact_reference_denied",
                    "Requested facts differ from the accepted receipt request list", 403)
            output = []
            for request in requests:
                facts = self._facts(connection, owner, binding, request, input_refs)
                require(facts[0]["details"]["input_refs"]["model"][0]["output_id"]
                        == payload["binding_ref"]["output_id"],
                        "model_fact_reference_denied", "Model facts describe another binding artifact", 403)
                output.append({"request_id": request["request_id"], "settings_id": request["settings_id"],
                               "facts": facts})
            result = {"requests": output}
            require(len(canonical_bytes(result)) <= 4_000_000, "model_fact_response_too_large",
                    "Model fact response exceeds its public read bound", 409)
            return deepcopy(result)
