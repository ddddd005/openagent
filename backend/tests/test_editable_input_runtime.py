"""Editable input uses current configuration, frozen runs and fixed checkpoints."""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import GraphCompiler, NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService

from test_graph_service import document, edge, node, run
from test_success_checkpoints import checkpoint_history


def _save_graph(service, entries, edges, **extra):
    graph = document(entries, edges)
    graph.update(schema_version=3, package_lock=list(service.registry.execution_package_lock), **extra)
    for entry in entries:
        entry.setdefault("configuration_policy", deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"]))
    saved = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()),
    )
    assert saved["outcome"] == "saved"
    return saved["document"]


def _create(service, graph):
    return service.create_session(
        graph["workflow_definition_id"], graph["revision"], idempotency_key=str(uuid4()),
    )


def _patch_text(service, graph, entry, text):
    """Change the saved text through the workbench's complete-document entry."""
    document = deepcopy(service.get_definition(graph["workflow_definition_id"]))
    target = next(item for item in document["nodes"]
                  if item["node_binding_id"] == entry["node_binding_id"])
    target["config"] = {**target["config"], "text": text}
    return service.update_definition(
        document, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=document["edit_sequence"],
        idempotency_key=str(uuid4()),
    )


def _output(session, entry):
    return next(item["outputs"]["output"] for item in session["nodes"]
                if item["node_binding_id"] == entry["node_binding_id"])


def test_prepared_input_stays_frozen_while_next_round_uses_new_saved_text(tmp_path):
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10), "editable input gate timed out"
        return {"output": text_content("gate")}

    with closing(GraphWorkflowService(tmp_path / "editable-frozen.sqlite")) as service:
        service.registry.register(NodeDefinition(
            "test.editable-gate", "1", "Gate", "Test", {}, {"type": "object"},
            outputs=(NodePort("output", "TEXT", data_schema_version=2),),
        ), gate)
        wait = node(service.registry, "test.editable-gate", 18001)
        source = node(service.registry, "tools.editable-input", 18002, text="A")
        output = node(service.registry, "tools.output", 18003)
        output["public_outputs"] = ["output"]
        graph = _save_graph(service, [wait, source, output], [edge(source, output, 18001)],
                            control_edges=[{
                                "edge_id": str(uuid4()), "source_node_id": wait["node_binding_id"],
                                "target_node_id": source["node_binding_id"],
                            }])
        plan = GraphCompiler(service.registry).compile(graph)
        assert plan.ordered_node_ids.index(wait["node_binding_id"]) < plan.ordered_node_ids.index(
            source["node_binding_id"])
        initial = _create(service, graph)
        started = service.start(
            initial["workflow_session_id"], expected_revision=initial["revision"],
            idempotency_key=str(uuid4()),
        )
        try:
            assert entered.wait(5), "editable input execution did not reach gate"
            # G21: a new complete save is rejected while the run is unfinished.
            # The former intent "save new adjustable text while the prepared run
            # is in flight" is replaced; the save happens after the run settles,
            # and the next prepared round then reads the new text.
            with pytest.raises(ContractValidationError) as rejected:
                _patch_text(service, graph, source, "B")
            assert rejected.value.reason_code == "configuration_run_locked"
            assert service.get_definition(graph["workflow_definition_id"])["nodes"][1]["config"] == {
                "text": "A",
            }
        finally:
            release.set()
            service.wait(started["active_chain_run_id"])
        first = service.get_session(initial["workflow_session_id"])
        assert first["status"] == "succeeded", first["chains"]
        assert _output(first, source) == text_content("A")
        assert _output(first, output) == text_content("A")
        with closing(service._store()) as store:
            frozen = store._connection.execute(
                "SELECT json_extract(payload,'$.config.text') FROM records "
                "WHERE record_type='node_run' "
                "AND json_extract(payload,'$.chain_run_id')=? "
                "AND json_extract(payload,'$.node_binding_id')=?",
                (started["active_chain_run_id"], source["node_binding_id"]),
            ).fetchone()[0]
            assert frozen == "A"
        saved = _patch_text(service, graph, source, "B")
        assert saved["outcome"] == "saved"
        assert saved["changed_categories"] == ["adjustable"]
        assert saved["definition_revision"] == graph["revision"]
        assert service.get_definition(graph["workflow_definition_id"])["nodes"][1]["config"] == {
            "text": "B",
        }
        second = run(service, first)
        assert second["status"] == "succeeded", second["chains"]
        assert _output(second, source) == text_content("B")
        assert _output(second, output) == text_content("B")
        assert _output(first, source) == text_content("A")


def test_checkpoint_saves_fixed_text_and_fork_uses_latest_adjustable_configuration(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "editable-checkpoint.sqlite")) as service:
        adjustable = node(service.registry, "tools.editable-input", 18101, text="current A")
        fixed = node(service.registry, "tools.editable-input", 18102, text="  fixed\r\n")
        fixed["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
        outputs = [node(service.registry, "tools.output", number) for number in (18103, 18104)]
        for output in outputs:
            output["public_outputs"] = ["output"]
        graph = _save_graph(
            service, [adjustable, fixed, *outputs],
            [edge(adjustable, outputs[0], 18101), edge(fixed, outputs[1], 18102)],
        )
        first = run(service, _create(service, graph))
        assert first["status"] == "succeeded", first["chains"]
        history = checkpoint_history(service, first)
        point = history["latest"]
        assert history["state"]["configuration"]["nodes"] == {
            fixed["node_binding_id"]: {
                "component_id": "tools.editable-input", "component_version": "1",
                "config": {"text": "  fixed\r\n"},
            },
        }
        assert history["state"]["agent_contexts"] == {}
        assert history["state"]["variables"] == {}
        saved = _patch_text(service, graph, adjustable, "current B")
        assert saved["outcome"] == "saved"
        source = run(service, first)
        assert source["status"] == "succeeded", source["chains"]
        assert _output(source, adjustable) == text_content("current B")
        before_fork = deepcopy(source)
        result = service.fork_checkpoint(
            source["workflow_session_id"], checkpoint_id=point["checkpoint_id"],
            expected_session_revision=source["revision"],
            expected_data_revision=source["data_revision"],
            expected_head_revision=source["head_revision"], idempotency_key=str(uuid4()),
        )
        target = service.get_session(result["target_session_id"])
        assert service.get_session(source["workflow_session_id"]) == before_fork
        assert target["workflow_definition_id"] == graph["workflow_definition_id"]
        assert target["definition_revision"] == graph["revision"]
        assert target["objects"] == {}
        with closing(service._store()) as store:
            for kind in ("chain_run", "node_run", "workflow_output"):
                assert store._connection.execute(
                    "SELECT count(*) FROM records WHERE record_type=? "
                    "AND json_extract(payload,'$.workflow_session_id')=?",
                    (kind, target["workflow_session_id"]),
                ).fetchone()[0] == 0
        fork_history = checkpoint_history(service, target)
        assert fork_history["state"] == history["state"]
        assert fork_history["latest"]["checkpoint_id"] != point["checkpoint_id"]
        continued = run(service, target)
        assert continued["status"] == "succeeded", continued["chains"]
        assert _output(continued, adjustable) == text_content("current B")
        assert _output(continued, fixed) == text_content("  fixed\r\n")
