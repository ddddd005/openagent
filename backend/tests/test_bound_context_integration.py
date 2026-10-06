"""Default bound context chain with offline Agent and atomic object receipts."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore

from test_agent_integration import AgentTransportFixture, output, run
from test_graph_service import copy_current, create, document, edge, node
from test_model_package import source_config
from test_models_service_integration import ModelDatabaseFixture


def graph(service, *, window=True, mismatched=False, root=True):
    registry = service.registry
    names = ("context.output", "context.window", "tools.current-input", "models.source",
             "context.assembly", "agents.execute", "context.merge", "tools.output")
    nodes = [node(registry, name, index) for index, name in enumerate(names, start=701)]
    read, selected, current, model, assembly, execute, merge, display = nodes
    for entry in (selected, assembly, execute):
        entry["component_version"] = "2"
        entry["config"] = deepcopy(registry.get(entry["component_id"], "2").definition.default_config)
    read["config"]["agent_node_id"] = execute["node_binding_id"]
    merge["config"]["agent_node_id"] = execute["node_binding_id"]
    selected["config"]["last_units"] = 1
    model["config"] = source_config()
    basis = selected if window else read
    edges = [
        edge(read, selected, 1, target_port="view"),
        edge(basis, assembly, 2, target_port="view"),
        edge(current, assembly, 3, target_port="current_input"),
        edge(model, execute, 4, target_port="model"),
        edge(assembly, execute, 5, target_port="prompt"),
        edge(read if mismatched else basis, merge, 6, target_port="view"),
        edge(execute, merge, 7, source_port="context", target_port="context"),
        edge(execute, display, 8, source_port="result"),
    ]
    doc = document(nodes, edges)
    doc.update(schema_version=2, execution_roots=[merge["node_binding_id"]] if root else [],
               package_lock=list(registry.package_lock), object_bindings=[
                   ObjectBinding("context", EFFECTIVE_CONTEXT_TYPE, 2, "shared",
                                 readers=(read["node_binding_id"], merge["node_binding_id"]),
                                 writers=(merge["node_binding_id"],)).to_dict()])
    return doc


def test_default_chain_direct_output_actual_receipt_and_window_both_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-bound-context")
    fixture = AgentTransportFixture(batch=True)
    with closing(GraphWorkflowService(tmp_path / "bound.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        view = create(service, graph(service))
        for index in range(3):
            view = run(service, view, f"question {index}")
            assert view["status"] == "succeeded", view["chains"]
            packet = output(view, "agents.execute", "context")
            agent_outputs = next(item for item in view["nodes"] if item["label"] == "agents.execute")["outputs"]
            assert set(agent_outputs) == {"result", "context"}
            candidate, commit = output(view, "context.merge"), output(view, "context.merge", "commit")
            prompt = output(view, "context.assembly")
            assert packet["basis_view_ref"] == prompt["context_ref"]
            assert candidate["units"][-1]["unit"] == packet["unit"]
            assert candidate["units"][-1]["unit_ref"] != packet["receipts"]["frozen_prompt_ref"]
            assert len(candidate["units"]) == min(index + 1, 2)
            obj = view["objects"]["context"]
            assert commit["receipt"]["revision_id"] == obj["revision_id"]
            assert commit["receipt"]["revision"] == obj["revision"] == index + 2
            assert commit["view_ref"] == obj["value"]["view_ref"]
            assert commit["adopted_delta_ids"] == obj["value"]["accepted_delta_ids"]
            assert len(commit["adopted_delta_ids"]) == index + 1
            assert not any(item["label"] in ("agents.delta", "context.advance", "context.save")
                           for item in view["nodes"])
        assert len(fixture.calls) == 6
        assert service._runtime_hosts == {} and not service._service_runs


def test_merge_rejects_other_selected_view_without_second_agent_call(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-bound-context")
    fixture = AgentTransportFixture()
    with closing(GraphWorkflowService(tmp_path / "mismatch.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        final = run(service, create(service, graph(service, mismatched=True)), "question")
        assert final["status"] == "failed", final["chains"]
        history = service.get_run(final["workflow_session_id"], final["active_chain_run_id"])
        assert any(item["diagnostic"] and item["diagnostic"]["code"] == "context_delta_prompt_mismatch"
                   for item in history["node_runs"])
        assert final["objects"]["context"]["revision"] == 1
        assert len(fixture.calls) == 1


def test_merge_is_not_implicit_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-bound-context")
    fixture = AgentTransportFixture()
    with closing(GraphWorkflowService(tmp_path / "noroot.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        final = run(service, create(service, graph(service, root=False)), "question")
        assert final["status"] == "succeeded", final["chains"]
        assert final["objects"]["context"]["revision"] == 1
        assert output(final, "agents.execute", "context")
        assert not next(item for item in final["nodes"] if item["label"] == "context.merge")["outputs"]


def test_merge_acceptance_failure_rolls_back_and_retry_reuses_agent_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-bound-context")
    fixture = AgentTransportFixture()
    with closing(GraphWorkflowService(tmp_path / "retry.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        original = SessionObjectStore.apply
        failing = [True]

        def injected(store, sid, *, node_id, writes):
            result = original(store, sid, node_id=node_id, writes=writes)
            if writes and failing[0]:
                failing[0] = False
                raise RuntimeError("injected failure after actual write before commit")
            return result

        monkeypatch.setattr(SessionObjectStore, "apply", injected)
        failed = run(service, create(service, graph(service)), "question")
        assert failed["status"] == "archive_failed", failed["chains"]
        assert failed["objects"]["context"]["revision"] == 1
        assert not next(item for item in failed["nodes"] if item["label"] == "context.merge")["outputs"]
        with closing(SqliteStore(service.database)) as store:
            assert store._connection.execute(
                "SELECT COUNT(*) FROM session_object_receipts WHERE session_id=?",
                (failed["workflow_session_id"],)).fetchone()[0] == 0
        assert len(fixture.calls) == 1
        sid, chain_id = failed["workflow_session_id"], failed["active_chain_run_id"]
        response = service.control(sid, action="retry_acceptance", expected_revision=failed["revision"],
                                   idempotency_key=str(uuid4()))
        service.wait(chain_id)
        final = service.get_session(sid)
        assert final["status"] == "succeeded", final["chains"]
        assert len(fixture.calls) == 1
        assert final["objects"]["context"]["revision"] == 2
        assert output(final, "context.merge", "commit")["receipt"]["revision_id"] == (
            final["objects"]["context"]["revision_id"])


def test_copy_maps_bound_agent_and_keeps_parent_future_outside_child_history(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-bound-context")
    fixture = AgentTransportFixture()
    database = tmp_path / "fork.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc = graph(service)
        parent = run(service, create(service, doc), "before fork")
        assert parent["status"] == "succeeded", parent["chains"]
        inherited = deepcopy(parent["objects"]["context"]["value"])
        copied = deepcopy(doc)
        copied["workflow_definition_id"] = str(uuid4())
        mapping = {entry["node_binding_id"]: str(uuid4()) for entry in copied["nodes"]}
        for entry in copied["nodes"]:
            entry["node_binding_id"] = mapping[entry["node_binding_id"]]
            if "agent_node_id" in entry["config"]:
                entry["config"]["agent_node_id"] = mapping[entry["config"]["agent_node_id"]]
        for connection in copied["edges"]:
            connection["edge_id"] = str(uuid4())
            for key in ("source_node_id", "target_node_id"):
                connection[key] = mapping[connection[key]]
        copied["execution_roots"] = [mapping[identity] for identity in copied["execution_roots"]]
        for binding in copied["object_bindings"]:
            for key in ("readers", "writers"):
                binding[key] = [mapping[identity] for identity in binding[key]]
        child = copy_current(service, parent, copied, mappings=[
            {"source_node_id": source, "target_node_id": target, "action": "copy"}
            for source, target in mapping.items()])
        child_sid, parent_sid = child["workflow_session_id"], parent["workflow_session_id"]
        assert child["objects"]["context"]["value"] == inherited
        parent = run(service, parent, "parent future")
        assert parent["status"] == "succeeded", parent["chains"]
        assert service.get_session(child_sid)["objects"]["context"]["value"] == inherited
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as service:
        child = run(service, service.get_session(child_sid), "child future")
        assert child["status"] == "succeeded", child["chains"]
        read = output(child, "context.output")
        execute_id = next(entry["node_binding_id"] for entry in copied["nodes"]
                          if entry["component_id"] == "agents.execute")
        assert read["owner"]["agent_node_id"] == execute_id
        assert read["owner"]["workflow_session_id"] == child_sid
        roots = [entry["unit"]["root"]["content"] for entry in output(child, "context.merge")["units"]]
        assert roots == ["before fork", "child future"]
        parent_roots = [entry["unit"]["root"]["content"]
                        for entry in output(service.get_session(parent_sid), "context.merge")["units"]]
        assert parent_roots == ["before fork", "parent future"]
        assert len(fixture.calls) == 3


def test_agent_rejects_context_explicitly_bound_to_a_different_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-bound-context")
    fixture = AgentTransportFixture()
    with closing(GraphWorkflowService(tmp_path / "wrong-agent.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc = graph(service)
        other = deepcopy(next(item for item in doc["nodes"] if item["component_id"] == "agents.execute"))
        other["node_binding_id"] = str(uuid4())
        other["title"] = "Other Agent"
        doc["nodes"].append(other)
        read = next(item for item in doc["nodes"] if item["component_id"] == "context.output")
        read["config"]["agent_node_id"] = other["node_binding_id"]
        final = run(service, create(service, doc), "question")
        assert final["status"] == "failed", final["chains"]
        assert final["objects"]["context"]["revision"] == 1
        assert len(fixture.calls) == 0
        history = service.get_run(final["workflow_session_id"], final["active_chain_run_id"])
        assert any(item["diagnostic"] and item["diagnostic"]["code"] == "context_agent_binding_mismatch"
                   for item in history["node_runs"])
