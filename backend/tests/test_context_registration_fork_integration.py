"""Current registered items join computation, explicit writes and checkpoint forks."""

from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from uuid import uuid4

from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.frontend_contracts import FRONTEND_STATE_TYPE
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding

from test_graph_service import document, edge, node
from test_model_package import source_config
from test_models_service_integration import ModelDatabaseFixture
from test_success_checkpoints import checkpoint_history
from workflow_test_support import run


class DistinctAnswers:
    def __init__(self):
        self.calls = []

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate_prepared(self, prepared):
                messages = prepared["wire_request"]["messages"]
                fixture.calls.append(deepcopy(messages))
                label = "alpha" if any(
                    "ALPHA_RULE" in str(message.get("content", "")) for message in messages
                ) else "beta"
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final.provider", "final_answer",
                    json.dumps({"answer": {"text": label + " answer"}}),
                ),))

            def close(self):
                pass

        return Transport()


def _entry(service, component, version, number):
    value = node(service.registry, component, number)
    value["component_version"] = version
    value["config"] = deepcopy(service.registry.get(component, version).definition.default_config)
    return value


def _registered_graph(service, *, write_context=True, write_display=True):
    current = _entry(service, "tools.current-input", "1", 9701)
    model = _entry(service, "models.source", "7", 9702)
    model["config"] = source_config()
    agents = [_entry(service, "agents.execute", "10", 9710 + index) for index in range(2)]
    nodes, edges, bindings, branches = [current, model, *agents], [], [], {}
    for index, label in enumerate(("alpha", "beta")):
        base = 9730 + index * 20
        read = _entry(service, "context.output", "5", base)
        assembly = _entry(service, "context.assembly", "5", base + 1)
        material = _entry(service, "prompts.item", "2", base + 2)
        output = _entry(service, "tools.output", "2", base + 3)
        material["config"]["text"] = label.upper() + "_RULE"
        key, display_key = "context-" + label, "display-" + label
        read["config"]["object_key"] = key
        # The executor is intentionally not the item's registrar.
        execute, registrar = agents[1 - index], agents[index]
        nodes += [read, assembly, material, output]
        edges += [
            edge(read, assembly, base, target_port="view"),
            edge(current, assembly, base + 1, target_port="current_input"),
            edge(material, assembly, base + 2, target_port="materials"),
            edge(assembly, execute, base + 3, target_port="prompt"),
            edge(model, execute, base + 4, target_port="model"),
            edge(execute, output, base + 5, source_port="result"),
        ]
        readers, writers = [read["node_binding_id"]], []
        branch = {"read": read, "assembly": assembly, "agent": execute, "output": output}
        if write_context:
            merge = _entry(service, "context.merge", "6", base + 4)
            merge["config"]["object_key"] = key
            readers.append(merge["node_binding_id"])
            writers.append(merge["node_binding_id"])
            nodes.append(merge)
            edges += [
                edge(read, merge, base + 6, target_port="view"),
                edge(execute, merge, base + 7, source_port="receipts", target_port="receipts"),
            ]
            branch["merge"] = merge
        display_readers, display_writers = [], []
        if write_display:
            display_read = _entry(service, "frontend.state.output", "1", base + 5)
            append = _entry(service, "frontend.state.append", "1", base + 6)
            presentation = _entry(service, "frontend.presentation", "1", base + 7)
            for entry in (display_read, append):
                entry["config"]["object_key"] = display_key
            presentation["public_outputs"] = ["display"]
            display_readers = [display_read["node_binding_id"], append["node_binding_id"]]
            display_writers = [append["node_binding_id"]]
            nodes += [display_read, append, presentation]
            edges += [
                edge(display_read, append, base + 8, source_port="view", target_port="view"),
                edge(output, append, base + 9, target_port="content"),
                edge(append, presentation, base + 10, source_port="view", target_port="view"),
            ]
            branch.update(display_read=display_read, append=append, presentation=presentation)
        bindings += [
            ObjectBinding(
                key, EFFECTIVE_CONTEXT_TYPE, 4, "shared",
                readers=tuple(readers), writers=tuple(writers),
                registrar_node_id=registrar["node_binding_id"],
            ).to_dict(),
            ObjectBinding(
                display_key, FRONTEND_STATE_TYPE, 1, "shared",
                readers=tuple(display_readers), writers=tuple(display_writers),
                registrar_node_id=output["node_binding_id"],
            ).to_dict(),
        ]
        branches[label] = branch
    graph = document(nodes, edges)
    graph.update(schema_version=3, object_bindings=bindings,
                 package_lock=deepcopy(list(service.registry.execution_package_lock)))
    for entry in graph["nodes"]:
        entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    saved = service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=str(uuid4()),
    )
    assert saved["outcome"] == "saved"
    assert service.get_definition(saved["workflow_definition_id"]) == saved["document"]
    return saved["document"], branches


def _create(service, graph):
    return service.create_session(
        graph["workflow_definition_id"], graph["revision"], idempotency_key=str(uuid4()),
    )


def _output(session, entry, port):
    return next(item["outputs"][port] for item in session["nodes"]
                if item["node_binding_id"] == entry["node_binding_id"])


def test_same_type_items_and_sessions_use_explicit_cross_registrar_updates(tmp_path):
    transport = DistinctAnswers()
    with closing(GraphWorkflowService(
            tmp_path / "registered-items.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        graph, branches = _registered_graph(service)
        initial, other = _create(service, graph), _create(service, graph)
        before_other = deepcopy(other["objects"])
        first = run(service, initial, "first source input")
        assert first["status"] == "succeeded", first["chains"]
        for label, branch in branches.items():
            current = first["objects"]["context-" + label]
            loaded = _output(first, branch["read"], "output")
            evidence = _output(first, branch["agent"], "receipts")
            assert loaded["messages"] == [] and loaded["schema_version"] == 5
            assert set(loaded["owner"]) == {"workflow_session_id", "object_key"}
            assert current["value"]["messages"][-1]["blocks"][0]["text"] == label + " answer"
            assert evidence["owner"]["node_binding_id"] == branch["agent"]["node_binding_id"]
            assert evidence["owner"]["node_binding_id"] != current["binding"]["registrar_node_id"]
            assert current["revision"] == 2
            display = first["objects"]["display-" + label]
            assert display["revision"] == 2 and len(display["value"]["entries"]) == 1
            assert _output(first, branch["presentation"], "display")["entries"] == display["value"]["entries"]
            artifact = service.read_object_artifact(
                first["workflow_session_id"], object_key="display-" + label,
                node_id=branch["append"]["node_binding_id"],
                reference=display["value"]["entries"][0]["source_ref"],
            )
            assert artifact["value"]["text"] == label + " answer"
        assert service.get_session(other["workflow_session_id"])["objects"] == before_other
        assert first["objects"]["context-alpha"]["revision_id"] != first["objects"]["context-beta"]["revision_id"]
        assert first["objects"]["display-alpha"]["value"]["entries"] != first["objects"]["display-beta"]["value"]["entries"]
        frozen_read = deepcopy(_output(first, branches["alpha"]["read"], "output"))
        second = run(service, first, "second source input")
        assert second["status"] == "succeeded", second["chains"]
        assert _output(second, branches["alpha"]["read"], "output")["messages"] == first["objects"]["context-alpha"]["value"]["messages"]
        assert _output(first, branches["alpha"]["read"], "output") == frozen_read
        assert len(second["objects"]["display-alpha"]["value"]["entries"]) == 2
        other = run(service, other, "independent input")
        assert other["status"] == "succeeded", other["chains"]
        assert "first source input" not in str(other["objects"]["context-alpha"]["value"])
        assert "independent input" not in str(second["objects"]["context-alpha"]["value"])
        assert len(transport.calls) == 6


def test_fork_initializes_saved_values_without_nodes_and_excludes_test_display(tmp_path):
    transport = DistinctAnswers()
    with closing(GraphWorkflowService(
            tmp_path / "registered-fork.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        graph, _ = _registered_graph(service)
        first = run(service, _create(service, graph), "saved source input")
        assert first["status"] == "succeeded", first["chains"]
        saved_history = checkpoint_history(service, first)
        point = saved_history["latest"]
        assert point["schema_version"] == 4
        metadata = service.read_checkpoint(
            first["workflow_session_id"], checkpoint_id=point["checkpoint_id"],
        )
        assert metadata["schema_version"] == 4 and "state_storage" not in metadata and "state" not in metadata
        state = saved_history["state"]
        assert set(state["agent_contexts"]) == {"context-alpha", "context-beta"}
        assert all(set(entry) == {"value"} for entry in state["agent_contexts"].values())
        source = run(service, first, "later source input")
        assert source["status"] == "succeeded", source["chains"]
        before_source, calls = deepcopy(source["objects"]), len(transport.calls)
        result = service.fork_checkpoint(
            source["workflow_session_id"], checkpoint_id=point["checkpoint_id"],
            expected_session_revision=source["revision"],
            expected_data_revision=source["data_revision"],
            expected_head_revision=source["head_revision"], idempotency_key=str(uuid4()),
        )
        target = service.get_session(result["target_session_id"])
        assert len(transport.calls) == calls
        assert service.get_session(source["workflow_session_id"])["objects"] == before_source
        with closing(sqlite3.connect(service.database)) as connection:
            for kind in ("chain_run", "node_run", "workflow_output"):
                assert connection.execute(
                    "SELECT count(*) FROM records WHERE record_type=? AND json_extract(payload,'$.workflow_session_id')=?",
                    (kind, target["workflow_session_id"]),
                ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT count(*) FROM session_object_receipts WHERE session_id=?",
                (target["workflow_session_id"],),
            ).fetchone()[0] == 0
        for label in ("alpha", "beta"):
            key = "context-" + label
            assert target["objects"][key]["value"] == first["objects"][key]["value"]
            assert target["objects"][key]["revision_id"] != first["objects"][key]["revision_id"]
            assert target["objects"]["display-" + label]["value"] == {"entries": [], "view_ref": None}
        continued = run(service, target, "child input")
        assert continued["status"] == "succeeded", continued["chains"]
        assert "saved source input" in str(continued["objects"]["context-alpha"]["value"])
        assert "later source input" not in str(continued["objects"]["context-alpha"]["value"])
        assert service.get_session(source["workflow_session_id"])["objects"] == before_source
        assert len(transport.calls) == calls + 2


def test_agent_and_test_output_do_not_implicitly_write_registered_items(tmp_path):
    transport = DistinctAnswers()
    with closing(GraphWorkflowService(
            tmp_path / "registered-read-only.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        graph, branches = _registered_graph(service, write_context=False, write_display=False)
        initial = _create(service, graph)
        before = deepcopy(initial["objects"])
        final = run(service, initial, "uncommitted computation")
        assert final["status"] == "succeeded", final["chains"]
        assert final["objects"] == before
        for branch in branches.values():
            evidence = _output(final, branch["agent"], "receipts")
            assert "next_view" not in evidence and "basis_revision" not in evidence
            assert "object_key" not in json.dumps(evidence)
            assert _output(final, branch["output"], "output")["text"].endswith(" answer")
        history = checkpoint_history(service, final)
        assert history["state"]["agent_contexts"] == {}
        assert all(not item["binding"]["writers"] for key, item in before.items()
                   if key.startswith("context-"))
