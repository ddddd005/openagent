"""Actual offline tavern graph settlement, direct reads and persistent history."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore
from phase1_agent.tavern.chat_contracts import TAVERN_CHAT_STATE_TYPE

from workflow_test_support import run
from test_context_native_integration import NativeTransport, native_graph, node_output
from test_graph_service import create, document, edge, node
from test_models_service_integration import ModelDatabaseFixture


def add_chat(service, doc, current, reply, *, after=None):
    read_user, append_user, read_assistant, append_assistant, presentation = [
        node(service.registry, component, 6100 + index) for index, component in enumerate((
            "tavern.chat.output", "tavern.chat.append", "tavern.chat.output",
            "tavern.chat.append", "tavern.chat.presentation"))]
    append_user["config"]["role"] = "user"
    presentation["public_outputs"] = ["display"]
    doc["nodes"] += [read_user, append_user, read_assistant, append_assistant, presentation]
    doc["edges"] += [
        edge(read_user, append_user, 6100, source_port="view", target_port="view"),
        edge(current, append_user, 6101, target_port="content"),
        edge(read_assistant, append_assistant, 6102, source_port="view", target_port="view"),
        edge(reply, append_assistant, 6103,
             source_port="result" if reply["component_id"] == "agents.execute" else "output",
             target_port="content"),
        edge(append_assistant, presentation, 6104, source_port="view", target_port="view"),
    ]
    controls = doc.setdefault("control_edges", [])
    controls.append({"edge_id": str(uuid4()), "source_node_id": append_user["node_binding_id"],
                     "target_node_id": read_assistant["node_binding_id"]})
    if after is not None:
        controls.append({"edge_id": str(uuid4()), "source_node_id": after["node_binding_id"],
                         "target_node_id": read_user["node_binding_id"]})
    doc["object_bindings"].append(ObjectBinding(
        "tavern-chat", TAVERN_CHAT_STATE_TYPE, 1, "shared",
        readers=tuple(row["node_binding_id"] for row in (
            read_user, append_user, read_assistant, append_assistant)),
        writers=(append_user["node_binding_id"], append_assistant["node_binding_id"]),
    ).to_dict())
    doc.setdefault("execution_roots", []).append(presentation["node_binding_id"])
    return {"user": append_user, "assistant": append_assistant, "presentation": presentation,
            "read_user": read_user}


def chat_graph(service):
    current = node(service.registry, "tools.current-input", 6001)
    reply = node(service.registry, "tools.regex", 6002, pattern="^", replacement="Reply: ")
    doc = document([current, reply], [edge(current, reply, 6001)])
    doc["package_lock"] = list(service.registry.package_lock)
    return doc, add_chat(service, doc, current, reply)


def public_read(service, final, doc, entries):
    sid = final["workflow_session_id"]
    output = service.read_public_output(
        sid, workflow_definition_id=doc["workflow_definition_id"], definition_revision=1,
        node_id=entries["presentation"]["node_binding_id"], port_id="display")["output"]
    parameters = {
        "workflow_definition_id": doc["workflow_definition_id"], "definition_revision": 1,
        "node_id": entries["presentation"]["node_binding_id"], "port_id": "display",
        "run_id": output["run_id"],
    }
    texts = [service.read_public_artifact(
        sid, **parameters, reference=entry["source_ref"])["value"]["text"]
        for entry in output["payload"]["entries"]]
    return output, parameters, texts


def test_real_user_and_reply_append_in_order_and_survive_cold_reopen(tmp_path):
    database = tmp_path / "tavern-rounds.sqlite"
    with closing(GraphWorkflowService(database)) as service:
        doc, entries = chat_graph(service)
        initial = create(service, doc)
        assert initial["objects"]["tavern-chat"]["value"] == {"entries": [], "view_ref": None}
        first = run(service, initial, "First user text.")
        assert first["status"] == "succeeded", first["chains"]
        state1 = deepcopy(first["objects"]["tavern-chat"])
        assert state1["revision"] == 3
        assert set(first["objects"]) == {"tavern-chat"}
        assert [entry["role"] for entry in state1["value"]["entries"]] == ["user", "assistant"]
        assert all(set(entry) == {"entry_id", "role", "source_ref"}
                   for entry in state1["value"]["entries"])
        public1, _, texts = public_read(service, first, doc, entries)
        assert texts == ["First user text.", "Reply: First user text."]
        assert public1["data_type"] == "TAVERN_CHAT_DISPLAY"
        assert public1["payload"] == {
            "schema_version": 1, "kind": "workflow.tavern-chat-display",
            "entries": state1["value"]["entries"]}
        second = run(service, first, "Second user text.")
        assert second["status"] == "succeeded", second["chains"]
        assert second["objects"]["tavern-chat"]["revision"] == 5
        assert second["objects"]["tavern-chat"]["value"]["entries"][:2] == state1["value"]["entries"]
        _, _, texts = public_read(service, second, doc, entries)
        assert texts == ["First user text.", "Reply: First user text.",
                         "Second user text.", "Reply: Second user text."]
        history = service.public_output_history(
            second["workflow_session_id"], workflow_definition_id=doc["workflow_definition_id"],
            definition_revision=1, node_id=entries["presentation"]["node_binding_id"], port_id="display")
        assert history["outputs"][0]["payload"] == public1["payload"]
        saved_state = deepcopy(second["objects"]["tavern-chat"])
        sid, chain = second["workflow_session_id"], second["selected_chain_run_id"]
        archived = service.get_run(sid, chain)
    with closing(GraphWorkflowService(database)) as reopened:
        cold = reopened.get_session(sid)
        assert cold["objects"]["tavern-chat"] == saved_state
        assert reopened.get_run(sid, chain) == archived
        _, _, texts = public_read(reopened, cold, doc, entries)
        assert texts == ["First user text.", "Reply: First user text.",
                         "Second user text.", "Reply: Second user text."]
        third = run(reopened, cold, "After reopening.")
        assert third["status"] == "succeeded", third["chains"]
        assert len(third["objects"]["tavern-chat"]["value"]["entries"]) == 6


def test_public_direct_reads_deny_foreign_unaccepted_and_nonexported_proof_artifacts(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "tavern-scope.sqlite")) as service:
        doc, entries = chat_graph(service)
        first = run(service, create(service, doc), "Own text.")
        assert first["status"] == "succeeded", first["chains"]
        _, parameters, texts = public_read(service, first, doc, entries)
        assert texts == ["Own text.", "Reply: Own text."]
        second = run(service, service.create_session(
            doc["workflow_definition_id"], 1, idempotency_key=str(uuid4())), "Foreign text.")
        assert second["status"] == "succeeded", second["chains"]
        sid = first["workflow_session_id"]
        foreign = second["objects"]["tavern-chat"]["value"]["entries"][0]["source_ref"]
        proof_ref = first["objects"]["tavern-chat"]["value"]["view_ref"]
        unaccepted = {"scope": "artifact", "output_id": str(uuid4())}
        for reference in (foreign, proof_ref, unaccepted):
            with pytest.raises(ContractValidationError) as denied:
                service.read_public_artifact(sid, **parameters, reference=reference)
            assert denied.value.reason_code == "output_reference_denied"
        with pytest.raises(ContractValidationError):
            service.read_public_artifact(
                sid, **{**parameters, "run_id": str(uuid4())},
                reference=first["objects"]["tavern-chat"]["value"]["entries"][0]["source_ref"])
        app = GraphApplication(service).for_consumer()
        with pytest.raises(ContractValidationError) as denied:
            app.query("object.artifact.read", {
                "session_id": sid, "object_key": "tavern-chat",
                "node_id": entries["assistant"]["node_binding_id"], "reference": proof_ref})
        assert denied.value.reason_code == "application_scope_denied"
        with closing(SqliteStore(service.database)) as store:
            objects = SessionObjectStore(store, service.registry.data_types)
            for reference in (foreign, unaccepted):
                with pytest.raises(ContractValidationError):
                    objects._resolve_write_artifact(sid, reference)


def test_tavern_settlement_rollback_reuses_accepted_business_output(tmp_path, monkeypatch):
    with closing(GraphWorkflowService(tmp_path / "tavern-retry.sqlite")) as service:
        doc, entries = chat_graph(service)
        assistant = service.registry.get("tavern.chat.append", "1")
        calls = []

        def execute(config, inputs, context):
            calls.append(context.node_run_id)
            return assistant.executor(config, inputs, context)

        service.registry._nodes[("tavern.chat.append", "1")] = replace(assistant, executor=execute)
        save_effects, attempts = service._save_effects, []

        def fail_after_write(repo, sid, document, event):
            result = save_effects(repo, sid, document, event)
            if event["node_binding_id"] == entries["assistant"]["node_binding_id"]:
                attempts.append(deepcopy(event["object_writes"]))
                if len(attempts) == 1:
                    raise OSError("tavern acceptance transaction interrupted")
            return result

        monkeypatch.setattr(service, "_save_effects", fail_after_write)
        initial = create(service, doc)
        pending = run(service, initial, "Only once.")
        assert pending["status"] == "archive_failed", pending["chains"]
        assert pending["available_actions"] == ["retry_acceptance", "close"]
        assert pending["objects"]["tavern-chat"]["revision"] == 2
        assert [entry["role"] for entry in pending["objects"]["tavern-chat"]["value"]["entries"]] == ["user"]
        assert pending["head_commit_id"] == initial["head_commit_id"]
        assert len(calls) == 2
        resumed = service.control(
            pending["workflow_session_id"], action="retry_acceptance",
            expected_revision=pending["revision"], idempotency_key=str(uuid4()))
        service.wait(resumed["active_chain_run_id"])
        final = service.get_session(pending["workflow_session_id"])
        assert final["status"] == "succeeded", final["chains"]
        assert len(calls) == 2
        assert attempts[0] == attempts[1]
        assert final["objects"]["tavern-chat"]["revision"] == 3
        _, _, texts = public_read(service, final, doc, entries)
        assert texts == ["Only once.", "Reply: Only once."]


def test_native_agent_uses_normal_context_then_tavern_only_displays_text(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-tavern-chat")
    fixture = NativeTransport()
    database = tmp_path / "tavern-native.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, native = native_graph(service, policy=False, once=False)
        current = next(row for row in doc["nodes"] if row["component_id"] == "tools.current-input")
        entries = add_chat(
            service, doc, current, native["a"]["agent"], after=native["a"]["merge"])
        first = run(service, create(service, doc), "First actual Agent input.")
        assert first["status"] == "succeeded", first["chains"]
        _, _, texts = public_read(service, first, doc, entries)
        assert texts == ["First actual Agent input.", "Native accepted answer"]
        assert first["objects"]["context-a"]["revision"] == 2
        assert first["objects"]["tavern-chat"]["revision"] == 3
        effective = node_output(first, native["a"]["merge"])
        assert [message["role"] for message in effective["messages"]] == ["user", "assistant"]
        assert [message["blocks"][0]["text"] for message in effective["messages"]] == texts
        assert "FIXED_RULE_a" not in str(first["objects"]["tavern-chat"]["value"])
        second = run(service, first, "Second actual Agent input.")
        assert second["status"] == "succeeded", second["chains"]
        assert second["objects"]["context-a"]["revision"] == 3
        assert second["objects"]["tavern-chat"]["revision"] == 5
        assert len(fixture.calls) == 2
        assert "First actual Agent input." in str(fixture.calls[-1]["messages"])
        history = service.get_run(second["workflow_session_id"], second["selected_chain_run_id"])
        ordered = [item["node_binding_id"] for item in history["node_runs"]]
        assert ordered.index(native["a"]["merge"]["node_binding_id"]) < ordered.index(
            entries["read_user"]["node_binding_id"])
        sid = second["workflow_session_id"]
        saved_context = deepcopy(second["objects"]["context-a"])
        saved_chat = deepcopy(second["objects"]["tavern-chat"])
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as reopened:
        cold = reopened.get_session(sid)
        assert cold["objects"]["context-a"] == saved_context
        assert cold["objects"]["tavern-chat"] == saved_chat
        assert len(fixture.calls) == 2
