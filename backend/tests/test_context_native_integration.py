"""Offline graph acceptance of canonical ordered context and prompt lifetimes."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4
import json

import pytest

from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_contracts import GraphDiagnosticError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore

from workflow_test_support import run
from test_graph_service import copy_current, create, document, edge, node
from test_model_package import source_config
from test_models_service_integration import ModelDatabaseFixture


class NativeTransport:
    def __init__(self, *, generate_long_text=False, bad_summary=False):
        self.calls, self.summaries, self.closes = [], [], 0
        self.generate_long_text, self.bad_summary, self.generated = generate_long_text, bad_summary, False

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                captured = {"messages": deepcopy(messages), "tools": deepcopy(tools)}
                fixture.calls.append(captured)
                last = messages[-1].get("content", "")
                if "SUMMARY_ONLY" in last:
                    fixture.summaries.append(captured)
                    return ModelResponse("stop", content="" if fixture.bad_summary else
                                         f"Checkpoint {len(fixture.summaries)}: task and constraints retained.")
                if fixture.generate_long_text and not fixture.generated:
                    fixture.generated = True
                    return ModelResponse("stop", content="GENERATED_LONG_SOURCE " * 2000)
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final.provider", "final_answer", json.dumps({"answer": {"text": "Native accepted answer"}})),))

            def close(self):
                fixture.closes += 1

        return Transport()


def versioned_node(service, component, version, number):
    entry = node(service.registry, component, number)
    entry["component_version"] = version
    entry["config"] = deepcopy(service.registry.get(component, version).definition.default_config)
    return entry


def native_graph(service, *, policy=True, once=True, dual=False):
    entries, edges, bindings, roots = {}, [], [], []
    current = versioned_node(service, "tools.current-input", "1", 4100)
    model = versioned_node(service, "models.source", "2", 4101)
    model["config"] = {**source_config(), "capacity": {
        "context_window_tokens": 100_000, "output_reserve_tokens": 128,
        "summary_max_tokens": 128, "max_cold_input_tokens": 100_000}}
    model["config"]["parameters"]["max_tokens"] = 128
    shared = [current, model]
    if policy:
        strategy = versioned_node(service, "agents.compaction-policy", "1", 4102)
        strategy["config"].update(trigger_tokens=10_000, keep_depth=0, summary_prompt="SUMMARY_ONLY")
        shared.append(strategy)
        entries["policy"] = strategy
    if once:
        material = versioned_node(service, "prompts.item", "2", 4103)
        material["config"].update(text="ONCE_ORIGINAL_SOURCE " * 2000, lifecycle="context_once", compaction="allowed")
        material["config"]["presentation"]["role"] = "user"
        shared.append(material)
        entries["once"] = material
    for index, suffix in enumerate(("a", "b") if dual else ("a",)):
        offset = 4200 + index * 20
        read, assembly, agent, merge, fixed = [
            versioned_node(service, component, version, offset + position)
            for position, (component, version) in enumerate((
                ("context.output", "4"), ("context.assembly", "4"), ("agents.execute", "4"),
                ("context.merge", "4"), ("prompts.item", "2")))]
        fixed["config"]["text"] = "FIXED_RULE_" + suffix
        key = "context-" + suffix
        for entry in (read, merge):
            entry["config"].update(object_key=key, agent_node_id=agent["node_binding_id"])
        entries[suffix] = {"read": read, "assembly": assembly, "agent": agent, "merge": merge, "fixed": fixed}
        shared.extend([read, assembly, agent, merge, fixed])
        base = 4200 + index * 20
        edges.extend([
            edge(read, assembly, base, target_port="view"),
            edge(current, assembly, base + 1, target_port="current_input"),
            edge(model, agent, base + 2, target_port="model"),
            edge(assembly, agent, base + 3, target_port="prompt"),
            edge(read, merge, base + 4, target_port="view"),
            edge(agent, merge, base + 5, source_port="context", target_port="context"),
            edge(fixed, assembly, base + 6, target_port="materials"),
        ])
        if once:
            edges.append(edge(material, assembly, base + 7, target_port="materials", order=1))
        if policy:
            edges.append(edge(strategy, agent, base + 8, target_port="compaction_policy"))
        bindings.append(ObjectBinding(key, EFFECTIVE_CONTEXT_TYPE, 4, "shared",
                                      readers=(read["node_binding_id"], merge["node_binding_id"]),
                                      writers=(merge["node_binding_id"],)).to_dict())
        roots.append(merge["node_binding_id"])
    doc = document(shared, edges)
    doc.update(package_lock=list(service.registry.package_lock), object_bindings=bindings, execution_roots=roots)
    return doc, entries


def node_output(view, entry, port="output"):
    return next(item["outputs"][port] for item in view["nodes"]
                if item["node_binding_id"] == entry["node_binding_id"])


def rebind(service, view, doc):
    doc = deepcopy(doc)
    doc["revision"] = view["definition_revision"] + 1
    service.save_definition(doc, expected_revision=view["definition_revision"], idempotency_key=str(uuid4()))
    return service.rebind_session(
        view["workflow_session_id"], definition_revision=doc["revision"], expected_revision=view["revision"],
        expected_data_revision=view["data_revision"], expected_head_revision=view["head_revision"],
        idempotency_key=str(uuid4())), doc


def test_native_graph_two_compactions_project_then_append_and_survive_copy_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-context")
    fixture = NativeTransport(generate_long_text=True)
    database = tmp_path / "native.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service)
        first = run(service, create(service, doc), "Current protected task.")
        assert first["status"] == "succeeded", first["chains"]
        effective = node_output(first, entries["a"]["merge"])
        update = node_output(first, entries["a"]["agent"], "context")
        assert effective["schema_version"] == 4 and effective["generation"] == 2
        assert len(fixture.summaries) == 2
        assert any(operation["kind"] == "append" and "GENERATED_LONG_SOURCE" in str(operation)
                   for operation in update["operations"])
        assert any(operation["kind"] == "compact" for operation in update["operations"])
        assert "GENERATED_LONG_SOURCE" not in str(effective["messages"])
        assert "ONCE_ORIGINAL_SOURCE" not in str(effective["messages"])
        assert "FIXED_RULE_" not in str(effective["messages"])
        assert effective["messages"][-1]["role"] == "assistant"
        assert effective["messages"][-1]["blocks"][0]["text"] == "Native accepted answer"
        assert all(block["kind"] != "tool_call" for message in effective["messages"] for block in message["blocks"])
        assert len(effective["once_injected_item_ids"]) == 1
        frozen = node_output(first, entries["a"]["assembly"])
        assert len(frozen["once_pending_item_ids"]) == 1
        assert update["basis_view_ref"] == frozen["context_ref"]
        assert first["objects"]["context-a"]["revision"] == 2
        saved = deepcopy(first["objects"]["context-a"])
        copied_doc = deepcopy(doc)
        copied_doc.update(workflow_definition_id=str(uuid4()), revision=1)
        child = copy_current(service, first, copied_doc)
        child_sid = child["workflow_session_id"]
        second = run(service, first, "Next parent request.")
        assert second["status"] == "succeeded", second["chains"]
        assert len(fixture.summaries) == 2
        second_prompt = node_output(second, entries["a"]["assembly"])
        assert second_prompt["once_pending_item_ids"] == []
        assert "ONCE_ORIGINAL_SOURCE" not in str(second_prompt["messages"])
        assert any(message["blocks"][0].get("text") == "Current protected task."
                   and layout["kind"] == "history" and layout["compaction"] == "allowed"
                   for message, layout in zip(second_prompt["messages"], second_prompt["layout"]))
        assert service.get_session(child_sid)["objects"]["context-a"]["value"] == saved["value"]
        history = service.get_run(first["workflow_session_id"], first["selected_chain_run_id"])
        assert "ONCE_ORIGINAL_SOURCE" in str(history["runtime_facts"])
        assert "GENERATED_LONG_SOURCE" in str(history["runtime_facts"])
        assert len(fixture.calls) == 5
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as reopened:
        child_final = run(reopened, reopened.get_session(child_sid), "Next child request after reopen.")
        assert child_final["status"] == "succeeded", child_final["chains"]
        assert len(fixture.summaries) == 2
        child_prompt = node_output(child_final, entries["a"]["assembly"])
        assert child_prompt["once_pending_item_ids"] == []
        assert "ONCE_ORIGINAL_SOURCE" not in str(child_prompt["messages"])
        assert "Next parent request." not in str(child_prompt["messages"])


def test_two_agents_keep_separate_once_ledgers_contexts_and_owners(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-context")
    fixture = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "dual.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service, dual=True)
        first = run(service, create(service, doc), "Shared current input.")
        assert first["status"] == "succeeded", first["chains"]
        assert len(fixture.summaries) == 2
        views = [node_output(first, entries[suffix]["merge"]) for suffix in ("a", "b")]
        assert views[0]["once_injected_item_ids"] == views[1]["once_injected_item_ids"]
        assert views[0]["owner"]["agent_node_id"] != views[1]["owner"]["agent_node_id"]
        assert views[0]["accepted_delta_ids"] + views[0]["applied_delta_ids"] != (
            views[1]["accepted_delta_ids"] + views[1]["applied_delta_ids"])
        second = run(service, first, "Both continue independently.")
        assert second["status"] == "succeeded", second["chains"]
        assert len(fixture.summaries) == 2
        for suffix in ("a", "b"):
            assert node_output(second, entries[suffix]["assembly"])["once_pending_item_ids"] == []
            assert second["objects"]["context-" + suffix]["revision"] == 3


def test_bad_summary_leaves_context_pointer_and_once_ledger_unconsumed(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-context")
    fixture = NativeTransport(bad_summary=True)
    with closing(GraphWorkflowService(tmp_path / "bad-summary.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, _ = native_graph(service)
        initial = create(service, doc)
        before = deepcopy(initial["objects"]["context-a"])
        failed = run(service, initial, "Do not lose this input.")
        assert failed["status"] == "failed", failed["chains"]
        assert failed["objects"]["context-a"] == before
        assert len(fixture.calls) == len(fixture.summaries) == 1


def test_context_acceptance_rollback_retries_without_reexecuting_summary_or_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-context")
    fixture = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "retry.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service)
        initial = create(service, doc)
        before = deepcopy(initial["objects"]["context-a"])
        apply, fail = SessionObjectStore.apply, [True]

        def fail_once(store, sid, *, node_id, writes):
            receipts = apply(store, sid, node_id=node_id, writes=writes)
            if writes and fail[0]:
                fail[0] = False
                raise OSError("injected native context settlement fault")
            return receipts

        monkeypatch.setattr(SessionObjectStore, "apply", fail_once)
        pending = run(service, initial, "Current request.")
        assert pending["status"] == "archive_failed", pending["chains"]
        assert pending["objects"]["context-a"] == before
        accepted_update = deepcopy(node_output(pending, entries["a"]["agent"], "context"))
        calls = len(fixture.calls)
        sid, chain = pending["workflow_session_id"], pending["active_chain_run_id"]
        service.control(sid, action="retry_acceptance", expected_revision=pending["revision"],
                        idempotency_key=str(uuid4()))
        service.wait(chain)
        final = service.get_session(sid)
        assert final["status"] == "succeeded", final["chains"]
        assert len(fixture.calls) == calls
        assert node_output(final, entries["a"]["agent"], "context") == accepted_update
        assert final["objects"]["context-a"]["revision"] == 2
        assert len(node_output(final, entries["a"]["merge"])["once_injected_item_ids"]) == 1


def test_native_write_proof_rejects_wrong_agent_artifact_inputs_and_changed_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-context")
    fixture = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "proof.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service)
        initial = create(service, doc)
        before = deepcopy(initial["objects"]["context-a"])
        final = run(service, initial, "Preserve proof.")
        assert final["status"] == "succeeded", final["chains"]
        desired = final["objects"]["context-a"]["value"]
        effective = node_output(final, entries["a"]["merge"])
        update = node_output(final, entries["a"]["agent"], "context")
        with closing(SqliteStore(service.database)) as store:
            objects = SessionObjectStore(store, service.registry.data_types)

            def check(mutation=None):
                def resolve(reference):
                    detail = objects._resolve_write_artifact(final["workflow_session_id"], reference)
                    if mutation:
                        mutation(reference, detail)
                    return detail
                return service.registry.data_types.validate_write(EFFECTIVE_CONTEXT_TYPE, 4, desired, {
                    "workflow_session_id": final["workflow_session_id"], "object_key": "context-a",
                    "current_record": before, "operation": "put", "resolve_artifact": resolve})

            assert check() == desired
            attacks = [
                (effective["derivation"]["update_ref"],
                 lambda detail: detail["producer"].update(node_binding_id=str(uuid4()))),
                (effective["derivation"]["update_ref"],
                 lambda detail: detail["input_refs"]["model"][0].update(output_id=str(uuid4()))),
                (effective["derivation"]["update_ref"],
                 lambda detail: detail["value"]["next_view"].update(once_injected_item_ids=[])),
                (update["receipts"]["frozen_prompt_ref"],
                 lambda detail: detail["value"].update(context_ref={"scope": "artifact", "output_id": str(uuid4())})),
                (effective["derivation"]["update_ref"],
                 lambda detail: detail["input_refs"]["compaction_policy"][0].update(output_id=str(uuid4()))),
                (effective["derivation"]["update_ref"],
                 lambda detail: detail["value"].update(compaction_policy_ref={"scope": "artifact", "output_id": str(uuid4())})),
                (update["compaction_policy_ref"],
                 lambda detail: detail["value"].update(summary_prompt="A different frozen summary instruction.")),
                (update["compaction_policy_ref"],
                 lambda detail: detail["value"].update(enabled=False)),
            ]
            for target, forge in attacks:
                def mutation(reference, detail):
                    if reference == target:
                        forge(detail)
                with pytest.raises(GraphDiagnosticError):
                    check(mutation)


def test_native_graph_without_compaction_stores_current_and_final_text_not_fixed_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-context")
    fixture = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "plain.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service, policy=False, once=False)
        final = run(service, create(service, doc), "An ordinary user input.")
        assert final["status"] == "succeeded", final["chains"]
        effective = node_output(final, entries["a"]["merge"])
        assert [message["role"] for message in effective["messages"]] == ["user", "assistant"]
        assert all(item["kind"] == "history" and item["compaction"] == "allowed" for item in effective["layout"])
        assert effective["generation"] == 0 and effective["once_injected_item_ids"] == []
        assert len(fixture.calls) == 1


@pytest.mark.parametrize("depth", [0, 2, 20])
def test_native_graph_middle_depth_reaches_model_and_does_not_persist_material(
    tmp_path, monkeypatch, depth,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-depth")
    fixture = NativeTransport()
    database = tmp_path / "depth.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service, policy=False, once=False)
        first = run(service, create(service, doc), "First input.")
        assert first["status"] == "succeeded", first["chains"]
        second = run(service, first, "Second input.")
        assert second["status"] == "succeeded", second["chains"]
        entries["a"]["fixed"]["config"]["presentation"].update(placement="middle", depth=depth)
        rebound, doc = rebind(service, second, doc)
        third = run(service, rebound, "Current input.")
        assert third["status"] == "succeeded", third["chains"]
        prompt = node_output(third, entries["a"]["assembly"])
        expected = ["First input.", "Native accepted answer", "Second input.",
                    "Native accepted answer", "Current input."]
        expected.insert(max(0, len(expected) - depth), "FIXED_RULE_a")
        assert [message["blocks"][0]["text"] for message in prompt["messages"]] == expected
        visible = [message.get("content") for message in fixture.calls[-1]["messages"]
                   if message.get("content") in expected]
        assert visible == expected
        effective = node_output(third, entries["a"]["merge"])
        assert [message["blocks"][0]["text"] for message in effective["messages"]] == [
            "First input.", "Native accepted answer", "Second input.",
            "Native accepted answer", "Current input.", "Native accepted answer"]
        assert not effective["once_injected_item_ids"]
        sid, chain = third["workflow_session_id"], third["selected_chain_run_id"]
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as reopened:
        archived = reopened.get_run(sid, chain)
        frozen = next(output["payload"] for output in archived["outputs"]
                      if output["node_binding_id"] == entries["a"]["assembly"]["node_binding_id"])
        assert frozen == prompt
        assert reopened.get_session(sid)["status"] == "succeeded"
        assert len(fixture.calls) == 3


def test_native_context_adoption_is_idempotent_and_rejects_stale_compacted_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-native-context")
    fixture = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "adoption.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service)
        first = run(service, create(service, doc), "First accepted request.")
        assert first["status"] == "succeeded", first["chains"]
        sid, writer = first["workflow_session_id"], entries["a"]["merge"]["node_binding_id"]
        old_pointer = deepcopy(first["objects"]["context-a"])
        output_id = old_pointer["value"]["view_ref"]["output_id"]
        key = str(uuid4())
        adopted = service.adopt_context_view(
            sid, node_id=writer, view_output_id=output_id,
            expected_revision=first["revision"], idempotency_key=key)
        replay = service.adopt_context_view(
            sid, node_id=writer, view_output_id=output_id,
            expected_revision=first["revision"], idempotency_key=key)
        assert replay == adopted
        assert adopted["results"] == []
        assert adopted["session"]["objects"]["context-a"] == old_pointer
        assert len(fixture.calls) == 2
        second = run(service, adopted["session"], "Accepted text after compaction.")
        assert second["status"] == "succeeded", second["chains"]
        new_pointer = deepcopy(second["objects"]["context-a"])
        assert new_pointer["revision"] == old_pointer["revision"] + 1
        calls = len(fixture.calls)
        with pytest.raises(GraphDiagnosticError) as rejected:
            service.adopt_context_view(
                sid, node_id=writer, view_output_id=output_id,
                expected_revision=second["revision"], idempotency_key=str(uuid4()))
        assert rejected.value.reason_code == "context_stale_basis"
        assert service.get_session(sid)["objects"]["context-a"] == new_pointer
        assert len(fixture.calls) == calls
