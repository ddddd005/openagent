"""Durable explicit context pipelines with a trusted synthetic execution package."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.capability_packages import CapabilityPackage, PackageManifest, PackageDependency
from phase1_agent.content_contracts import json_content, text_content
from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.storage import SqliteStore
from phase1_agent.contract_json import loads_strict

from test_graph_service import create, copy_current, document, edge, node, run


def exact(context, port):
    return {"scope": "artifact", "output_id": context.input_artifact_refs(port)[0]["output_id"]}


def synthetic_package(counter, *, delta_mutator=None, unit_extra_refs=None):
    """Test-only authority; it reports no model, tool, or production Agent work."""
    def register(host):
        empty = {"type": "object", "additionalProperties": False}

        def register_node(name, executor, *, inputs=(), outputs, config=None, schema=None):
            host.register_node(NodeDefinition(
                "context-test." + name, "1", name, "Test", config or {}, schema or empty,
                inputs=inputs, outputs=outputs, input_storage="references"), executor)

        register_node("text", lambda config, inputs, ctx: {"output": text_content(config["text"])},
                      config={"text": "actual root"},
                      schema={"type": "object", "additionalProperties": False,
                              "properties": {"text": {"type": "string"}}, "required": ["text"]},
                      outputs=(NodePort("output", "TEXT", data_schema_version=2),))
        register_node("receipt", lambda config, inputs, ctx: {
            "output": json_content({"synthetic_node_run_id": ctx.node_run_id})},
            outputs=(NodePort("output", "JSON", data_schema_version=2),))

        def make_unit(config, inputs, ctx):
            counter.append(ctx.node_run_id)
            return {"output": {
                "schema_version": 1, "kind": "workflow.context-unit", "unit_id": ctx.node_run_id,
                "source_kind": "accepted_execution", "root": deepcopy(inputs["prompt"]["current_input"]),
                "messages": [{"message_id": str(uuid4()), "role": "assistant", "content": "synthetic answer"}],
                "source_refs": [inputs["prompt"]["current_input_ref"], exact(ctx, "receipt"),
                                *deepcopy(unit_extra_refs or [])],
            }}
        register_node("unit", make_unit, inputs=(NodePort("prompt", "PROMPT", data_schema_version=3),
                                                NodePort("receipt", "JSON", data_schema_version=2)),
                      outputs=(NodePort("output", "CONTEXT_UNIT"),))

        def make_delta(config, inputs, ctx):
            value = {
                "schema_version": 1, "kind": "workflow.agent-delta", "delta_id": ctx.node_run_id,
                "owner": {"workflow_session_id": ctx.workflow_session_id,
                          "node_binding_id": ctx.node_binding_id, "chain_run_id": ctx.chain_run_id,
                          "node_run_id": ctx.node_run_id},
                "frozen_prompt_ref": exact(ctx, "prompt"), "basis_view_ref": inputs["prompt"]["context_ref"],
                "current_root_ref": inputs["prompt"]["current_input_ref"],
                "unit_ref": exact(ctx, "unit"), "unit": deepcopy(inputs["unit"]),
                "fact_receipt_refs": [exact(ctx, "receipt")],
            }
            if delta_mutator is not None:
                delta_mutator(value, inputs)
            return {"output": value}
        register_node("delta", make_delta,
                      inputs=(NodePort("prompt", "PROMPT", data_schema_version=3),
                              NodePort("unit", "CONTEXT_UNIT"), NodePort("receipt", "JSON", data_schema_version=2)),
                      outputs=(NodePort("output", "AGENT_DELTA"),))

    return CapabilityPackage(PackageManifest(
        "workflow.context-test.synthetic", "1.0.0", (PackageDependency("workflow.context", "1.0.0"),)), register)


def context_graph(service):
    registry = service.registry
    read, window, text, prompt, receipt, unit, delta, advance, save = (
        node(registry, component, index) for index, component in enumerate((
            "context.read", "context.window", "context-test.text", "context.assembly",
            "context-test.receipt", "context-test.unit", "context-test.delta",
            "context.advance", "context.save"), start=1))
    window["config"]["last_units"] = 1
    save["public_outputs"] = ["output"]
    doc = document([read, window, text, prompt, receipt, unit, delta, advance, save], [
        edge(read, window, 1, target_port="view"),
        edge(window, prompt, 2, target_port="view"), edge(text, prompt, 3, target_port="current_input"),
        edge(prompt, unit, 4, target_port="prompt"), edge(receipt, unit, 5, target_port="receipt"),
        edge(prompt, delta, 6, target_port="prompt"), edge(unit, delta, 7, target_port="unit"),
        edge(receipt, delta, 8, target_port="receipt"),
        edge(window, advance, 9, target_port="view"), edge(delta, advance, 10, target_port="delta"),
        edge(advance, save, 11, target_port="view"),
    ])
    doc.update(schema_version=2, execution_roots=[save["node_binding_id"]],
               package_lock=list(registry.package_lock), object_bindings=[
        ObjectBinding("context", EFFECTIVE_CONTEXT_TYPE, 1, "shared",
                      readers=(read["node_binding_id"], save["node_binding_id"]),
                      writers=(save["node_binding_id"],)).to_dict()])
    return doc


def _output(session, component):
    return next(entry["outputs"]["output"] for entry in session["nodes"] if entry["label"] == component)


def test_context_rounds_persist_exact_views_and_window_never_rereads_full_old_history(tmp_path):
    counter = []
    with closing(GraphWorkflowService(tmp_path / "rounds.sqlite",
                                      capability_packages=[synthetic_package(counter)],
                                      enabled_packages={"workflow.context-test.synthetic": "1.0.0"})) as service:
        initial = create(service, context_graph(service))
        first = run(service, initial)
        assert first["status"] == "succeeded", first["chains"]
        assert len(_output(first, "context.advance")["units"]) == 1
        second = run(service, first)
        assert second["status"] == "succeeded", second["chains"]
        assert len(_output(second, "context.advance")["units"]) == 2
        third = run(service, second)
        assert third["status"] == "succeeded", third["chains"]
        assert len(_output(third, "context.read")["units"]) == 2
        assert len(_output(third, "context.window")["units"]) == 1
        assert len(_output(third, "context.advance")["units"]) == 2
        assert len(third["objects"]["context"]["value"]["accepted_delta_ids"]) == 3
        assert third["objects"]["context"]["revision"] == 4
        operation = _output(third, "context.save")["operation_key"]
        with closing(SqliteStore(service.database)) as store:
            receipt = store._connection.execute(
                "SELECT result FROM session_object_receipts WHERE session_id=? AND operation_key=?",
                (third["workflow_session_id"], operation)).fetchone()
            assert loads_strict(receipt["result"])["revision"] == 4
        assert len(counter) == 3


@pytest.mark.parametrize("mutation,expected", [
    (lambda value, inputs: value["owner"].update(workflow_session_id=str(uuid4())),
     "context_delta_owner_mismatch"),
    (lambda value, inputs: value.update(basis_view_ref=inputs["prompt"]["context"]["derivation"]["input_view_ref"]),
     "context_delta_prompt_mismatch"),
    (lambda value, inputs: value["unit"]["root"].update(content="fabricated current input"),
     "context_artifact_mismatch"),
])
def test_untrusted_delta_claims_fail_before_context_pointer_or_consumption_changes(tmp_path, mutation, expected):
    counter = []
    with closing(GraphWorkflowService(
        tmp_path / "claims.sqlite", capability_packages=[synthetic_package(counter, delta_mutator=mutation)],
        enabled_packages={"workflow.context-test.synthetic": "1.0.0"},
    )) as service:
        final = run(service, create(service, context_graph(service)))
        assert final["status"] == "failed"
        failed = next(entry for entry in final["nodes"] if entry["status"] == "failed")
        assert failed["diagnostic"]["code"] == expected
        assert final["objects"]["context"]["revision"] == 1
        assert final["objects"]["context"]["value"] == {"view_ref": None, "accepted_delta_ids": []}
        assert len(counter) == 1
        assert not service._service_runs


def test_fork_cannot_launder_parent_artifact_created_after_fork_through_unit_provenance(tmp_path):
    counter, extra_refs = [], []
    with closing(GraphWorkflowService(
        tmp_path / "authority.sqlite", capability_packages=[synthetic_package(counter, unit_extra_refs=extra_refs)],
        enabled_packages={"workflow.context-test.synthetic": "1.0.0"},
    )) as service:
        doc = context_graph(service)
        first = run(service, create(service, doc))
        assert first["status"] == "succeeded"
        fork_doc = deepcopy(doc)
        fork_doc["workflow_definition_id"] = str(uuid4())
        fork = copy_current(service, first, fork_doc)
        parent = run(service, first)
        assert parent["status"] == "succeeded"
        extra_refs.append(_output(parent, "context.advance")["units"][-1]["unit_ref"])
        failed = run(service, fork)
        assert failed["status"] == "failed"
        unit_node = next(entry for entry in failed["nodes"] if entry["label"] == "context-test.unit")
        assert unit_node["status"] == "failed"
        assert unit_node["diagnostic"]["code"] == "graph_artifact_reference_denied"
        assert failed["objects"]["context"]["revision"] == 1
        assert failed["objects"]["context"]["value"] == fork["objects"]["context"]["value"]
        assert not service._service_runs
        assert service._runtime_hosts == {}


def test_context_reopen_fork_freezes_exact_inherited_reference_and_remaps_owner(tmp_path):
    counter = []
    database = tmp_path / "fork.sqlite"
    with closing(GraphWorkflowService(database, capability_packages=[synthetic_package(counter)],
                                      enabled_packages={"workflow.context-test.synthetic": "1.0.0"})) as service:
        doc = context_graph(service)
        first = run(service, create(service, doc))
        assert first["status"] == "succeeded", first["chains"]
        original_ref = deepcopy(first["objects"]["context"]["value"]["view_ref"])
        fork_doc = deepcopy(doc)
        fork_doc["workflow_definition_id"] = str(uuid4())
        fork = copy_current(service, first, fork_doc)
        fork_sid, parent_sid = fork["workflow_session_id"], first["workflow_session_id"]
        assert fork["objects"]["context"]["value"]["view_ref"] == original_ref
        parent = run(service, first)
        assert parent["status"] == "succeeded", parent["chains"]
        assert parent["objects"]["context"]["value"]["view_ref"] != original_ref
        assert service.get_session(fork_sid)["objects"]["context"]["value"]["view_ref"] == original_ref
    with closing(GraphWorkflowService(database, capability_packages=[synthetic_package(counter)])) as reopened:
        fork = run(reopened, reopened.get_session(fork_sid))
        assert fork["status"] == "succeeded", fork["chains"]
        read = _output(fork, "context.read")
        assert read["owner"]["workflow_session_id"] == fork_sid
        assert len(read["units"]) == 1
        assert read["units"] == _output(first, "context.advance")["units"]
        assert len(fork["objects"]["context"]["value"]["accepted_delta_ids"]) == 2
        assert len(reopened.get_session(parent_sid)["objects"]["context"]["value"]["accepted_delta_ids"]) == 2
        assert len(counter) == 3
