"""Real offline rounds and immutable native-context lineage in temporary SQLite."""

from copy import deepcopy
import json
from uuid import uuid4

from phase1_agent.checkpoint_repository import CheckpointRepository, initialize_checkpoint_tables
from phase1_agent.content_contracts import text_content
from phase1_agent.context_contract import (
    EFFECTIVE_CONTEXT_TYPE, effective_context_references, preserve_artifact_references,
    validate_effective_context,
)
from phase1_agent.context_host_service import native_service_requirement
from phase1_agent.context_prompt_v6 import (
    assemble_native_context_prompt, native_context_prompt_references, validate_native_context_prompt,
)
from phase1_agent.context_receipt import validate_context_receipt
from phase1_agent.context_update import context_update_references
from phase1_agent.context_update_v2 import validate_context_update_v2
from phase1_agent.context_v4 import (
    merge_native_context, native_context_references, prepare_native_context_adoption, read_native_context,
    validate_native_context_view,
)
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_records import graph_record
from phase1_agent.historical_artifact_reader import HistoryArtifactAuthority
from phase1_agent.host_sdk import DataTypeDefinition, ObjectBinding, TypeRegistry
from phase1_agent.model_execution_contracts import AGENT_EXECUTOR_REF_V2
from phase1_agent.model_request_contracts import (
    MODEL_REQUEST_SERVICE_REF, validate_compaction_policy, validate_model_binding,
)
from phase1_agent.prompt_lifecycle import validate_lifecycle_prompt_materials
from phase1_agent.runtime import CanonicalModelAdapter
from phase1_agent.runtime_checkpoints import make_diff
from phase1_agent.runtime_fact_store import RuntimeFactStore
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore
from phase1_agent.type_contract_store import TypeContractStore
from test_agent_executor_v2 import Callbacks, ExecutorHarness
from test_checkpoint_history_integration import transaction
from test_historical_artifact_reader import read_record, write_record
from test_prompt_lifecycle import content, item


def artifact(identity):
    return {"scope": "artifact", "output_id": identity}


class NativeHistoryFixture:
    """Only graph declarations are synthetic; execution, M2 facts and object CAS are real."""

    def __init__(self, directory, *, rounds=2, compact_second=True):
        self.host = ExecutorHarness(directory)
        resources = self.host.host.resources
        self.path = resources.path
        self.store = SqliteStore(self.path)
        self.connection = self.store._connection
        self.sid = self.host.context.workflow_session_id
        self.workflow = resources.evidence["session"]["workflow_definition_id"]
        self.agent = self.host.context.node_binding_id
        self.nodes = {
            "model": resources.evidence["source_node"]["node_binding_id"],
            "agent": self.agent,
            **{key: str(uuid4()) for key in (
                "read", "materials", "text", "policy", "assembly", "merge", "unrelated")},
        }
        self.order = ("model", "read", "materials", "text", "policy", "assembly", "agent",
                      "merge", "unrelated")
        self.once = item(39701, "ONCE_MATERIAL")
        self.fixed = item(39702, "FIXED_MATERIAL", role="system",
                          lifecycle="per_request", compaction="never")
        self.materials = content(self.once, self.fixed)
        self.policy = {
            "schema_version": 2, "kind": "workflow.context-compaction-policy",
            "enabled": True, "keep_depth": 0, "summary_prompt": "Summarize permitted text.",
        }
        self.types = self._types()
        self.definitions = self._definitions()
        self.edges = self._edges()
        context_binding = ObjectBinding(
            "context", EFFECTIVE_CONTEXT_TYPE, 4, "shared",
            readers=(self.nodes["read"], self.nodes["merge"]),
            writers=(self.nodes["merge"],),
            default_value={"view_ref": None, "accepted_delta_ids": []})
        links_binding = ObjectBinding(
            "links", "test.proof-links", 1, "shared", readers=tuple(self.nodes.values()),
            writers=tuple(self.nodes.values()), default_value={"refs": []})
        self.document = {
            "schema_version": 2, "workflow_definition_id": self.workflow, "revision": 1,
            "name": "Original native history proof", "edges": self.edges,
            "nodes": [
                {"node_binding_id": self.nodes[key], "component_id": definition.component_id,
                 "component_version": definition.component_version, "config": definition.default_config,
                 "title": definition.display_name, "position": {"x": 0, "y": 0}}
                for key, definition in self.definitions.items()
            ],
            "object_bindings": [context_binding.to_dict(), links_binding.to_dict()],
        }
        self.objects = SessionObjectStore(self.store, self.types)
        self.rounds, self.checkpoints = [], []
        with transaction(self.connection):
            initialize_checkpoint_tables(self.connection)
            TypeContractStore(self.store).check_registry(self.types)
            self._persist_definition()
            self.objects.initialize(self.sid, self.document)
            self._checkpoint()
        for number in range(rounds):
            if number == 1 and compact_second:
                resources.profile["update_sequence"] += 1
                resources.profile["value"]["runtime"].update(
                    output_limit_tokens=2000, context_length_tokens=8000)
                resources.save(resources.profile)
            self._execute_round(number, compact=number == 1 and compact_second)
        self.checkpoint = self.checkpoints[-1]

    @staticmethod
    def _types():
        types = TypeRegistry()
        for identity, version, validator, references, scope in (
            ("TEXT", 2, None, None, "content"),
            ("MODEL_BINDING", 5, validate_model_binding, None, "content"),
            ("PROMPT_MATERIALS", 1, validate_lifecycle_prompt_materials, None, "content"),
            ("CONTEXT_COMPACTION_POLICY", 2, validate_compaction_policy, None, "content"),
            ("CONTEXT_VIEW", 4, validate_native_context_view, native_context_references, "content"),
            ("PROMPT", 6, validate_native_context_prompt, native_context_prompt_references, "content"),
            ("AGENT_CONTEXT_UPDATE", 2, validate_context_update_v2, context_update_references, "content"),
            ("CONTEXT_COMMIT", 2, validate_context_receipt,
             lambda value: [value["view_ref"]], "content"),
            (EFFECTIVE_CONTEXT_TYPE, 4, validate_effective_context,
             effective_context_references, "session"),
            ("test.proof-links", 1, None, lambda value: value["refs"], "session"),
        ):
            types.register(DataTypeDefinition(
                identity, version, {"type": "object"}, scope=scope, validator=validator,
                references=references, reference_mapper=preserve_artifact_references))
        return types

    def _definitions(self):
        config = {"object_key": "context", "agent_node_id": self.agent}
        access = {
            "config_field": "object_key", "type_id": EFFECTIVE_CONTEXT_TYPE,
            "schema_version": 4, "access": "read", "multiple": False,
        }
        service = MODEL_REQUEST_SERVICE_REF.to_dict()
        view = NodePort("view", "CONTEXT_VIEW", data_schema_version=4)
        return {
            "model": NodeDefinition(
                "models.source", "7", "Model", "Test",
                self.host.host.resources.evidence["source_node"]["config"], {"type": "object"},
                outputs=(NodePort("output", "MODEL_BINDING", data_schema_version=5),),
                capabilities=("models:resolve",), service_requirements=({
                    **service, "capability": "models:resolve", "operations": ["bind-native-model"]},)),
            "read": NodeDefinition(
                "context.output", "4", "Read", "Test", config, {"type": "object"},
                outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=4),),
                capabilities=("objects:read", "context:read"), object_accesses=(access,),
                service_requirements=(native_service_requirement("context:read", "read-context"),)),
            "materials": NodeDefinition(
                "test.materials", "1", "Materials", "Test", {}, {"type": "object"},
                outputs=(NodePort("output", "PROMPT_MATERIALS", data_schema_version=1),)),
            "text": NodeDefinition(
                "tools.current-input", "1", "Input", "Test", {}, {"type": "object"},
                outputs=(NodePort("output", "TEXT", data_schema_version=2),)),
            "policy": NodeDefinition(
                "test.policy", "2", "Policy", "Test", {}, {"type": "object"},
                outputs=(NodePort("output", "CONTEXT_COMPACTION_POLICY", data_schema_version=2),)),
            "assembly": NodeDefinition(
                "context.assembly", "4", "Assembly", "Test", {}, {"type": "object"},
                inputs=(view, NodePort("materials", "PROMPT_MATERIALS", multiple=True),
                        NodePort("current_input", "TEXT", data_schema_version=2)),
                outputs=(NodePort("output", "PROMPT", data_schema_version=6),)),
            "agent": NodeDefinition(
                "agents.execute", "9", "Agent", "Test", {}, {"type": "object"},
                inputs=(NodePort("prompt", "PROMPT", data_schema_version=6),
                        NodePort("model", "MODEL_BINDING", data_schema_version=5),
                        NodePort("compaction_policy", "CONTEXT_COMPACTION_POLICY", data_schema_version=2)),
                outputs=(NodePort("context", "AGENT_CONTEXT_UPDATE", data_schema_version=2),),
                capabilities=("models:call", "model-projection:read"), service_requirements=({
                    **service, "capability": "models:call",
                    "operations": ["prepare-request", "dispatch-request", "discard-prepared-request"]}, {
                    **service, "capability": "model-projection:read", "operations": ["measure-context"]})),
            "merge": NodeDefinition(
                "context.merge", "4", "Merge", "Test", config, {"type": "object"},
                inputs=(view, NodePort("context", "AGENT_CONTEXT_UPDATE", data_schema_version=2)),
                outputs=(NodePort("output", "CONTEXT_VIEW", data_schema_version=4),
                         NodePort("commit", "CONTEXT_COMMIT", data_schema_version=2)),
                capabilities=("objects:read", "objects:write", "context:validate", "artifacts:read", "facts:read"),
                object_accesses=({**access, "access": "read_write"},),
                service_requirements=(native_service_requirement("context:validate", "validate-context"),)),
            "unrelated": NodeDefinition(
                "test.unrelated", "1", "Unrelated", "Test", {}, {"type": "object"},
                outputs=(NodePort("output", "TEXT", data_schema_version=2),)),
        }

    def _edges(self):
        return [
            {"edge_id": str(uuid4()), "order": 0, "source_node_id": self.nodes[source],
             "source_port_id": source_port, "target_node_id": self.nodes[target], "target_port_id": port}
            for source, target, port, source_port in (
                ("read", "assembly", "view", "output"),
                ("materials", "assembly", "materials", "output"),
                ("text", "assembly", "current_input", "output"),
                ("assembly", "agent", "prompt", "output"),
                ("model", "agent", "model", "output"),
                ("policy", "agent", "compaction_policy", "output"),
                ("read", "merge", "view", "output"),
                ("agent", "merge", "context", "context"),
            )
        ]

    def _persist_definition(self):
        write_record(self.connection, "workflow_definition_revision", graph_record(
            "workflow_definition_revision", workflow_definition_id=self.workflow, revision=1,
            bindings=list(self.nodes.values()), edges=self.edges, document=self.document))
        for key, definition in self.definitions.items():
            descriptor = definition.to_dict()
            if key == "agent":
                descriptor["executor_ref"] = deepcopy(AGENT_EXECUTOR_REF_V2)
            write_record(self.connection, "node_definition", graph_record(
                "node_definition", component_id=definition.component_id,
                component_version=definition.component_version, descriptor=descriptor))
            write_record(self.connection, "node_binding", graph_record(
                "node_binding", workflow_definition_id=self.workflow, workflow_definition_revision=1,
                node_binding_id=self.nodes[key], component_id=definition.component_id,
                component_version=definition.component_version, config=definition.default_config))

    def _refs(self, key, outputs):
        result = {}
        reverse = {node: name for name, node in self.nodes.items()}
        for edge in self.edges:
            if edge["target_node_id"] == self.nodes[key]:
                result.setdefault(edge["target_port_id"], []).append({
                    "edge_id": edge["edge_id"], "order": edge["order"],
                    "output_id": outputs[reverse[edge["source_node_id"]]],
                })
        return result

    def _prepare_execution_owner(self, chain, runs, outputs, refs):
        host, resources = self.host, self.host.host.resources
        for context in (host.context, host.mock.context):
            context.chain_run_id = chain
            context.node_run_id = runs["agent"]
            context.input_artifact_refs = lambda port: deepcopy(host.references.get(port, []))
        evidence = resources.evidence
        evidence["session"].update(active_chain_run_id=chain)
        evidence["chain"].update(
            chain_run_id=chain, status="running",
            ordered_nodes=[self.nodes["model"], self.agent],
            node_run_ids=[runs["model"], runs["agent"]],
            node_run_attempts=[[runs["model"]], [runs["agent"]]],
            completed_nodes=[self.nodes["model"]], next_node_index=1,
            outputs={self.nodes["model"]: {"output": outputs["model"]}})
        evidence["consumer_run"].update(
            chain_run_id=chain, run_id=runs["agent"], status="running",
            input_refs=deepcopy(refs), output_refs={})
        evidence["producer_run"].update(
            chain_run_id=chain, run_id=runs["model"], output_refs={"output": outputs["model"]})
        evidence["binding_output"].update(
            chain_run_id=chain, run_id=runs["model"], output_id=outputs["model"])
        host.references = deepcopy(refs)
        host.mock.input_refs = deepcopy(refs)
        host.mock.output_id = outputs["model"]
        with transaction(self.connection):
            for name, kind in resources.record_kinds().items():
                write_record(self.connection, kind, evidence[name])

    def _execute_round(self, number, *, compact):
        chain = str(uuid4())
        runs, outputs = ({key: str(uuid4()) for key in self.order} for _ in range(2))
        commit_id = str(uuid4())
        record = self.objects.current(self.sid)["context"]
        retained = self.rounds[-1]["view"] if self.rounds else None
        view = read_native_context(record, self.sid, "context", self.agent, retained)
        current = text_content("previous context " * 700 if number == 0 else "Next task")
        prompt = assemble_native_context_prompt(
            [self.materials], view, current, context_ref=artifact(outputs["read"]),
            current_input_ref=artifact(outputs["text"]),
            source_output_refs=[{
                "edge_id": next(edge["edge_id"] for edge in self.edges
                                if edge["target_port_id"] == "materials"),
                "output_id": outputs["materials"], "order": 0,
            }])
        refs = self._refs("agent", outputs)
        self._prepare_execution_owner(chain, runs, outputs, refs)
        self.host.prompt = prompt
        self.host.inputs = {"prompt": prompt, "model": self.host.mock.binding, "compaction_policy": self.policy}
        communication_index = len(self.host.mock.communications)

        def communicate():
            if compact and len(self.host.mock.communications) == communication_index + 1:
                self.host.mock.response = ModelResponse("stop", "Short retained summary.", model="mock-model")
            else:
                self.host.mock.response = ModelResponse("tool_calls", tool_calls=(
                    ModelToolCall("provider-final", "final_answer",
                                  json.dumps({"answer": {"text": "Accepted answer"}})),), model="mock-model")

        self.host.mock.on_communication = communicate
        callbacks = Callbacks()
        packet = self.host.executor().advance(callbacks, None)["context"]
        merged = merge_native_context(view, artifact(outputs["read"]), packet, artifact(outputs["agent"]), prompt)
        owner = packet["owner"]
        bodies = {
            "model": self.host.mock.binding, "read": view, "materials": self.materials, "text": current,
            "policy": self.policy, "assembly": prompt, "agent": packet, "merge": merged,
            "unrelated": text_content("Outside the formal pointer closure"),
        }
        with transaction(self.connection):
            facts = RuntimeFactStore(self.store)
            for sequence, identity in enumerate(packet["receipts"]["fact_ids"], 1):
                facts.accept(owner=owner, stream_id="executor:agents.snapshot-kernel@2.0.0", fact={
                    "schema_version": 1, "owner": owner, "executor_ref": AGENT_EXECUTOR_REF_V2,
                    "generation": 1, "sequence": sequence, "fact_id": identity,
                    "payload": callbacks.events[identity]})
            chain_outputs = {}
            for key in self.order:
                port = "context" if key == "agent" else "output"
                output_refs = {port: outputs[key]}
                if key == "merge":
                    output_refs["commit"] = commit_id
                reads = [{
                    "kind": "object_read", "object_key": "context",
                    "revision": record["revision"], "revision_id": record["revision_id"],
                }] if key in ("read", "merge") else []
                write_record(self.connection, "node_run", graph_record(
                    "node_run", profile="node", run_id=runs[key], workflow_session_id=self.sid,
                    node_binding_id=self.nodes[key], chain_run_id=chain, status="succeeded", revision=2,
                    input_refs=self._refs(key, outputs), input_values={}, output_refs=output_refs,
                    reads=reads, effects=[], config=self.definitions[key].default_config, diagnostic=None))
                self._output(outputs[key], bodies[key], chain, runs[key], key, port)
                chain_outputs[self.nodes[key]] = output_refs
            prepared = prepare_native_context_adoption(
                workflow_session_id=self.sid, object_key="context", object_record=record,
                view_ref=artifact(outputs["merge"]), view=merged)
            receipt = self.objects.apply(
                self.sid, node_id=self.nodes["merge"], writes=[prepared["write_intent"]])[0]
            commit = validate_context_receipt({
                "schema_version": 2, "kind": "workflow.context-commit", "owner": merged["owner"],
                "view_ref": artifact(outputs["merge"]),
                "adopted_delta_ids": prepared["desired_value"]["accepted_delta_ids"],
                "operation_key": prepared["operation_key"], "receipt": receipt})
            self._output(commit_id, commit, chain, runs["merge"], "merge", "commit")
            merge_run = read_record(self.connection, "node_run", runs["merge"])
            merge_run["effects"] = [{"kind": "object_write", **receipt}]
            write_record(self.connection, "node_run", merge_run)
            write_record(self.connection, "chain_run", graph_record(
                "chain_run", chain_run_id=chain, workflow_session_id=self.sid,
                workflow_definition_id=self.workflow, definition_revision=1, status="succeeded", revision=3,
                targets=[self.agent], ordered_nodes=[self.nodes[key] for key in self.order],
                node_run_ids=[runs[key] for key in self.order],
                completed_nodes=[self.nodes[key] for key in self.order], next_node_index=len(self.order),
                inputs={}, outputs=chain_outputs, diagnostic=None))
            write_record(self.connection, "workflow_session", graph_record(
                "workflow_session", workflow_session_id=self.sid, workflow_definition_id=self.workflow,
                definition_revision=1, revision=number + 2, source={"kind": "new"}, active_chain_run_id=None))
            self.rounds.append({
                "chain": chain, "read_id": outputs["read"], "read_run": runs["read"],
                "read": view, "prompt_id": outputs["assembly"], "prompt": prompt,
                "update_id": outputs["agent"], "packet": packet, "merge_id": outputs["merge"],
                "merge_run": runs["merge"], "commit_id": commit_id, "commit": commit, "view": merged,
                "record_before": record, "record_after": self.objects.current(self.sid)["context"],
                "unrelated_id": outputs["unrelated"],
            })
            self._checkpoint(chain)

    def _output(self, identity, body, chain, run, key, port="output", *, sid=None):
        write_record(self.connection, "workflow_output", graph_record(
            "workflow_output", output_id=identity, workflow_session_id=sid or self.sid,
            chain_run_id=chain, run_id=run, node_binding_id=self.nodes[key], port_id=port, payload=body))

    def _logical_state(self, *, sid=None):
        return {
            "schema_version": 1, "kind": "workflow.runtime-state", "node_states": {},
            "program_state": {"values": {}, "data": {}},
            "history": {"selected_round_id": None, "visible_entry_ids": []},
            "objects": {
                key: {field: deepcopy(record[field]) for field in (
                    "type_id", "schema_version", "deleted", "value")}
                for key, record in self.objects.current(sid or self.sid).items()
            },
        }

    def _checkpoint(self, chain=None):
        state = self._logical_state()
        root = {
            "schema_version": 1, "checkpoint_id": str(uuid4()), "workflow_definition_id": self.workflow,
            "workflow_session_id": self.sid, "fixed_definition_revision": 1,
            "kind": "successful_round" if chain else "initial",
            "parent_checkpoint_id": self.checkpoints[-1]["checkpoint_id"] if chain else None,
            "round_id": str(uuid4()) if chain else None, "chain_run_id": chain,
            "assistant_entry_id": str(uuid4()) if chain else None,
            "state_storage": ({"mode": "diff", "base_checkpoint_id": self.checkpoints[0]["checkpoint_id"],
                               **make_diff(self.last_state, state)}
                              if chain else {"mode": "full", "state": state}),
        }
        repository = self.repository(self.connection)
        repository.insert(root)
        repository.retain_checkpoint_history(root["checkpoint_id"])
        self.checkpoints.append(root)
        self.last_state = state
        return root

    def repository(self, connection, sid=None):
        return CheckpointRepository(
            connection, workflow_definition_id=self.workflow, workflow_session_id=sid or self.sid,
            fixed_definition_revision=1)

    def reader(self, connection, checkpoint=None, *, workflow_session_id=None, **options):
        parameters = {
            "workflow_definition_id": self.workflow, "workflow_session_id": workflow_session_id or self.sid,
            "fixed_definition_revision": 1, "context_sources": (("agents.execute", "9"),),
            "model_sources": (("models.source", "7"),), "project_messages": CanonicalModelAdapter._project,
            "resolve_transport": self.host.mock.resolve_transport,
        }
        parameters.update(options)
        authority = HistoryArtifactAuthority(connection, self.types, **parameters)
        authority.add_checkpoint_root((checkpoint or self.checkpoint)["checkpoint_id"])
        return authority

    def fork_target(self):
        target, chain, read_run, read_id = (str(uuid4()) for _ in range(4))
        with transaction(self.connection):
            write_record(self.connection, "workflow_session", graph_record(
                "workflow_session", workflow_session_id=target, workflow_definition_id=self.workflow,
                definition_revision=1, revision=1, source={"kind": "new"}, active_chain_run_id=None))
            history = [value["chain"] for value in self.rounds]
            self.objects.initialize(
                target, self.document, inherited=self.objects.current(self.sid),
                source_session_id=self.sid, history=history)
            repository = self.repository(self.connection, target)
            copied, previous = [], None
            for original in self.checkpoints:
                point = deepcopy(original)
                point.update(workflow_session_id=target, checkpoint_id=str(uuid4()))
                if previous is not None:
                    point["parent_checkpoint_id"] = previous
                    point["state_storage"]["base_checkpoint_id"] = copied[0]["checkpoint_id"]
                repository.insert(point)
                copied.append(point)
                previous = point["checkpoint_id"]
            repository.retain_checkpoint_history(previous)
            record = self.objects.current(target)["context"]
            view = read_native_context(record, target, "context", self.agent, self.rounds[-1]["view"])
            write_record(self.connection, "node_run", graph_record(
                "node_run", profile="node", run_id=read_run, workflow_session_id=target,
                node_binding_id=self.nodes["read"], chain_run_id=chain, status="succeeded", revision=1,
                input_refs={}, input_values={}, output_refs={"output": read_id},
                reads=[{"kind": "object_read", "object_key": "context",
                        "revision": record["revision"], "revision_id": record["revision_id"]}],
                effects=[], config=self.definitions["read"].default_config, diagnostic=None))
            self._output(read_id, view, chain, read_run, "read", sid=target)
            write_record(self.connection, "chain_run", graph_record(
                "chain_run", chain_run_id=chain, workflow_session_id=target,
                workflow_definition_id=self.workflow, definition_revision=1, status="succeeded", revision=1,
                targets=[self.nodes["read"]], ordered_nodes=[self.nodes["read"]], node_run_ids=[read_run],
                completed_nodes=[self.nodes["read"]], next_node_index=1, inputs={},
                outputs={self.nodes["read"]: {"output": read_id}}, diagnostic=None))
            state = self._logical_state(sid=target)
            state["objects"]["links"]["value"]["refs"] = [artifact(read_id)]
            point = {
                **copied[-1], "checkpoint_id": str(uuid4()), "parent_checkpoint_id": previous,
                "round_id": str(uuid4()), "chain_run_id": chain, "assistant_entry_id": str(uuid4()),
                "state_storage": {"mode": "diff", "base_checkpoint_id": copied[0]["checkpoint_id"],
                                  **make_diff(self.last_state, state)},
            }
            repository.insert(point)
            repository.retain_checkpoint_history(point["checkpoint_id"])
        return {"sid": target, "chain": chain, "read_id": read_id, "read_run": read_run,
                "view": view, "checkpoint": point, "record": record}

    def close(self):
        self.host.close()
        self.store.close()
