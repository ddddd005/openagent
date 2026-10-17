"""Offline graph acceptance of canonical ordered context and prompt lifetimes."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4
import json

import pytest

from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import ObjectBinding, WriteIntent
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore

from workflow_test_support import run
from test_graph_service import create, document, edge, node
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

            def _generate(self, messages, tools):
                captured = {"messages": deepcopy(messages), "tools": deepcopy(tools)}
                fixture.calls.append(captured)
                last = messages[-1].get("content", "")
                if "SUMMARY_ONLY" in last:
                    fixture.summaries.append(captured)
                    return ModelResponse("stop", content="" if fixture.bad_summary else
                                         f"Checkpoint {len(fixture.summaries)}: task and constraints retained.")
                if fixture.generate_long_text and not fixture.generated:
                    fixture.generated = True
                    return ModelResponse("stop", content="GENERATED_LONG_TEXT " * 2000)
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final.provider", "final_answer", json.dumps({"answer": {"text": "Native accepted answer"}})),))

            def generate(self, messages, tools):
                return self._generate(messages, tools)

            def generate_prepared(self, prepared):
                request = prepared["transport_arguments"]
                return self._generate(request["messages"], request["tools"])

            def close(self):
                fixture.closes += 1

        return Transport()


def versioned_node(service, component, version, number):
    return node(service.registry, component, number, component_version=version)


def native_graph(service, *, policy=True, once=True, dual=False):
    entries, edges, bindings = {}, [], []
    current = versioned_node(service, "tools.current-input", "1", 4100)
    model = versioned_node(service, "models.source", "7", 4101)
    model["config"] = source_config()
    ModelDatabaseFixture.update_profile(service, capacity={
        "context_window_tokens": 100_000, "input_limit_tokens": 100_000,
        "max_output_tokens": 128,
    }, runtime={"context_length_tokens": 100_000, "output_limit_tokens": 128,
                "compaction_threshold_bps": 1000})
    shared = [current, model]
    if policy:
        strategy = versioned_node(service, "agents.compaction-policy", "2", 4102)
        strategy["config"].update(keep_depth=0, summary_prompt="SUMMARY_ONLY")
        shared.append(strategy)
        entries["policy"] = strategy
    if once:
        material = versioned_node(service, "prompts.item", "3", 4103)
        material["config"].update(text="ONCE_ORIGINAL_TEXT " * 2000, lifecycle="context_once", compaction="allowed")
        material["config"]["presentation"]["role"] = "user"
        shared.append(material)
        entries["once"] = material
    for index, suffix in enumerate(("a", "b") if dual else ("a",)):
        offset = 4200 + index * 20
        read, assembly, agent, merge, fixed, output = [
            versioned_node(service, component, version, offset + position)
            for position, (component, version) in enumerate((
                ("context.output", "5"), ("context.assembly", "5"), ("agents.execute", "10"),
                ("context.merge", "6"), ("prompts.item", "3"), ("tools.output", "1")))]
        fixed["config"]["text"] = "FIXED_RULE_" + suffix
        key = "context-" + suffix
        for entry in (read, merge):
            entry["config"].update(object_key=key)
        entries[suffix] = {"read": read, "assembly": assembly, "agent": agent, "merge": merge, "fixed": fixed}
        shared.extend([read, assembly, agent, merge, fixed, output])
        base = 4200 + index * 20
        edges.extend([
            edge(read, assembly, base, target_port="view"),
            edge(current, assembly, base + 1, target_port="current_input"),
            edge(model, agent, base + 2, target_port="model"),
            edge(assembly, agent, base + 3, target_port="prompt"),
            edge(read, merge, base + 4, target_port="view"),
            edge(agent, merge, base + 5, source_port="receipts", target_port="receipts"),
            edge(fixed, assembly, base + 6, target_port="materials"),
            edge(agent, output, base + 9, source_port="result"),
        ])
        if once:
            edges.append(edge(material, assembly, base + 7, target_port="materials", order=1))
        if policy:
            edges.append(edge(strategy, agent, base + 8, target_port="compaction_policy"))
        bindings.append(ObjectBinding(key, EFFECTIVE_CONTEXT_TYPE, 4, "shared",
                                      readers=(read["node_binding_id"], merge["node_binding_id"]),
                                      writers=(merge["node_binding_id"],),
                                      registrar_node_id=agent["node_binding_id"]).to_dict())
    doc = document(shared, edges)
    doc.update(package_lock=list(service.registry.package_lock), object_bindings=bindings)
    return doc, entries


def node_output(view, entry, port="output"):
    return next(item["outputs"][port] for item in view["nodes"]
                if item["node_binding_id"] == entry["node_binding_id"])


def agent_events(service, run_view, receipts):
    history = service.get_run(run_view["workflow_session_id"], run_view["selected_chain_run_id"])
    by_id = {fact["fact_id"]: fact["payload"] for fact in history["runtime_facts"]
             if fact.get("fact_id") in receipts["fact_ids"]}
    return [by_id[identity] for identity in receipts["fact_ids"]]


def agent_operations(service, run_view, receipts):
    operations = []
    for event in agent_events(service, run_view, receipts)[:-1]:
        if event["kind"] == "message_accepted":
            operations.append({"kind": "append", "message": deepcopy(event["payload"]["message"])})
        elif event["kind"] == "context_compaction_applied":
            operations.append(deepcopy(event["payload"]))
    return operations


def _message_text(message):
    if all(block["kind"] == "text" for block in message["blocks"]):
        return "\n".join(block["text"] for block in message["blocks"])
    return None


def injected_once_contents(prompt):
    """Role/text pairs of once materials injected into this prompt, not frozen history."""
    frozen = {message["message_id"] for message in prompt["context"]["messages"]}
    return {(message["role"], _message_text(message)) for message, item in
            zip(prompt["messages"], prompt["layout"])
            if item["kind"] == "once" and message["message_id"] not in frozen}


def once_content(entry):
    return (entry["config"]["presentation"]["role"], entry["config"]["text"])


def persisted_once_contents(value):
    return {(message["role"], _message_text(message)) for message, item in
            zip(value["messages"], value["layout"]) if item["kind"] == "once"}

def rebind(service, view, doc):
    """Reference-only helper for legacy cross-revision tests; not used by current cases."""
    doc = deepcopy(doc)
    doc["revision"] = view["definition_revision"] + 1
    service.save_definition(doc, expected_revision=view["definition_revision"], idempotency_key=str(uuid4()))
    return service.rebind_session(
        view["workflow_session_id"], definition_revision=doc["revision"], expected_revision=view["revision"],
        expected_data_revision=view["data_revision"], expected_head_revision=view["head_revision"],
        idempotency_key=str(uuid4())), doc


def test_two_agents_keep_separate_contexts_and_once_content(tmp_path, monkeypatch):
    fixture = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "dual.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service, dual=True)
        first = run(service, create(service, doc), "Shared current input.")
        assert first["status"] == "succeeded", first["chains"]
        assert len(fixture.summaries) == 2
        views = [node_output(first, entries[suffix]["merge"]) for suffix in ("a", "b")]
        assert views[0]["owner"]["object_key"] != views[1]["owner"]["object_key"]
        assert views[0]["accepted_delta_ids"] + views[0]["applied_delta_ids"] != (
            views[1]["accepted_delta_ids"] + views[1]["applied_delta_ids"])
        assert all(injected_once_contents(node_output(first, entries[suffix]["assembly"])) == {
            once_content(entries["once"])} for suffix in ("a", "b"))
        second = run(service, first, "Both continue independently.")
        assert second["status"] == "succeeded", second["chains"]
        assert len(fixture.summaries) == 4
        for suffix in ("a", "b"):
            # Each Agent compacts its own context, so each re-injects absent once content.
            assert injected_once_contents(node_output(second, entries[suffix]["assembly"])) == {
                once_content(entries["once"])}
            assert "ONCE_ORIGINAL_TEXT" not in str(
                node_output(second, entries[suffix]["merge"])["messages"])
            assert second["objects"]["context-" + suffix]["revision"] == 3


def test_bad_summary_leaves_context_value_and_once_content_unconsumed(tmp_path, monkeypatch):
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
    fixture = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "retry.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service)
        initial = create(service, doc)
        before = deepcopy(initial["objects"]["context-a"])
        apply, fail = SessionObjectStore.apply, [True]
        merge_node_id = entries["a"]["merge"]["node_binding_id"]
        apply_calls = []

        def fail_once(store, sid, *, node_id, writes, write_evidence=None):
            apply_calls.append((node_id, len(writes)))
            receipts = apply(
                store, sid, node_id=node_id, writes=writes, write_evidence=write_evidence)
            if writes and node_id == merge_node_id and fail[0]:
                fail[0] = False
                raise OSError("injected native context settlement fault")
            return receipts

        monkeypatch.setattr(SessionObjectStore, "apply", fail_once)
        pending = run(service, initial, "Current request.")
        assert pending["status"] == "archive_failed", pending["chains"]
        assert not fail[0], apply_calls
        assert pending["objects"]["context-a"] == before
        pending_history = service.get_run(pending["workflow_session_id"], pending["active_chain_run_id"])
        accepted_receipts = deepcopy(next(
            row["payload"] for row in pending_history["outputs"]
            if row["node_binding_id"] == entries["a"]["agent"]["node_binding_id"]
            and row["port_id"] == "receipts"))
        calls = len(fixture.calls)
        sid, chain = pending["workflow_session_id"], pending["active_chain_run_id"]
        service.control(sid, action="retry_acceptance", expected_revision=pending["revision"],
                        idempotency_key=str(uuid4()))
        service.wait(chain)
        final = service.get_session(sid)
        assert final["status"] == "succeeded", final["chains"]
        assert len(fixture.calls) == calls
        assert node_output(final, entries["a"]["agent"], "receipts") == accepted_receipts
        assert "next_view" not in accepted_receipts
        assert final["objects"]["context-a"]["revision"] == 2
        assert injected_once_contents(node_output(final, entries["a"]["assembly"])) == {
            once_content(entries["once"])}


def test_context_merge_rebuilds_only_from_exact_receipts_prompt_model_policy_and_facts(tmp_path):
    fixture = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "proof.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service)
        initial = create(service, doc)
        final = run(service, initial, "Preserve proof.")
        assert final["status"] == "succeeded", final["chains"]
        effective = node_output(final, entries["a"]["merge"])
        receipts = node_output(final, entries["a"]["agent"], "receipts")
        runtime_facts = service.get_run(
            final["workflow_session_id"], final["selected_chain_run_id"])["runtime_facts"]
        facts = [fact for fact in runtime_facts if fact.get("fact_id") in receipts["fact_ids"]]
        model_fact_records = {
            fact["fact_id"]: fact for fact in runtime_facts
            if fact.get("kind") == "workflow.model-fact"
        }
        model_facts = {"requests": [{
            "request_id": request["request_id"],
            "settings_id": request["settings_id"],
            "facts": [model_fact_records[identity] for identity in request["fact_ids"]],
        } for request in receipts["model_requests"]]}
        events = [fact["payload"] for fact in facts]
        snapshot = events[-1]["payload"]["snapshot"]
        prompt_ref = receipts["frozen_prompt_ref"]
        model_ref = receipts["model_ref"]
        policy_ref = snapshot["config"]["payload"]["frozen_compaction_policy_ref"]
        view_ref = node_output(final, entries["a"]["merge"], "commit")["view_ref"]
        with closing(SqliteStore(service.database)) as store:
            objects = SessionObjectStore(store, service.registry.data_types)
            from phase1_agent.context_contract import project_agent_context_value
            from phase1_agent.context_prompt_v6 import agent_computation_input
            from phase1_agent.context_update_v2 import prove_context_merge_execution_v2
            from phase1_agent.context_v4 import validate_native_context_artifacts_v5
            from phase1_agent.model_request_transport import resolve_model_request_transport
            from phase1_agent.runtime import CanonicalModelAdapter

            def resolve(reference):
                return objects._resolve_write_artifact(final["workflow_session_id"], reference)["value"]

            prompt = resolve(prompt_ref)
            def prompt_context_for(value):
                prompt_context = type("PromptContext", (), {})()
                prompt_context.workflow_session_id = final["workflow_session_id"]
                prompt_context.input_artifact_refs = lambda port: [
                    {"output_id": prompt_ref["output_id"]},
                ]

                def host_call(capability, operation, request):
                    assert (capability, operation, request) == (
                        "artifacts:read", "resolve-input", {"port": "prompt"})
                    return {"value": deepcopy(value), "producer": deepcopy(receipts["owner"]),
                            "output_id": prompt_ref["output_id"]}

                prompt_context.host_call = host_call
                return prompt_context

            prompt_context = prompt_context_for(prompt)
            computation_input = agent_computation_input(prompt_context, prompt)
            assert set(computation_input) == {"messages", "layout", "current_input_ref"}
            assert computation_input == {
                "messages": prompt["messages"], "layout": prompt["layout"],
                "current_input_ref": prompt["current_input_ref"],
            }
            forged_prompt = deepcopy(prompt)
            forged_prompt["context"]["owner"]["workflow_session_id"] = str(uuid4())
            with pytest.raises(ContractValidationError, match="another workflow session"):
                agent_computation_input(prompt_context_for(forged_prompt), forged_prompt)
            binding = resolve(model_ref)
            policy = resolve(policy_ref)
            def prove(current_receipts, current_facts, current_prompt, current_binding,
                      current_policy, current_policy_ref):
                return prove_context_merge_execution_v2(
                    current_prompt, current_binding, current_receipts, current_facts,
                    model_facts=model_facts, policy=current_policy, policy_ref=current_policy_ref,
                    project_messages=CanonicalModelAdapter._project,
                    resolve_transport=resolve_model_request_transport)

            proof = prove(receipts, facts, prompt, binding, policy, policy_ref)
            assert proof["snapshot_id"] == receipts["snapshot_id"]
            assert proof["operations"] == agent_operations(service, final, receipts)
            assert snapshot["s0"] == computation_input["messages"]
            assert snapshot["config"]["payload"]["context_compaction"]["layout"] == computation_input["layout"]
            assert "context" not in snapshot["config"]["payload"]
            assert "owner" not in snapshot["config"]["payload"]
            assert "object_key" not in snapshot["config"]["payload"]
            assert "basis" not in snapshot["config"]["payload"]
            assert validate_native_context_artifacts_v5(
                {"view": effective, "view_ref": view_ref}, resolve) == effective
            desired = final["objects"]["context-a"]["value"]
            assert project_agent_context_value(effective) == desired
            assert all("source" not in message and "schema_version" not in message
                       for message in desired["messages"])
            forged_receipts = deepcopy(receipts)
            forged_receipts["owner"]["chain_run_id"] = str(uuid4())
            with pytest.raises(ContractValidationError):
                prove(forged_receipts, facts, prompt, binding, policy, policy_ref)
            with pytest.raises(ContractValidationError):
                prove(receipts, list(reversed(facts)), prompt, binding, policy, policy_ref)
            forged_prompt = deepcopy(prompt)
            forged_prompt["items"][0]["text"] = "FORGED_ONCE_TEXT"
            with pytest.raises(ContractValidationError):
                prove(receipts, facts, forged_prompt, binding, policy, policy_ref)
            forged_binding = deepcopy(binding)
            forged_binding["reference"]["resource_id"] = str(uuid4())
            with pytest.raises(ContractValidationError):
                prove(receipts, facts, prompt, forged_binding, policy, policy_ref)
            forged_policy = deepcopy(policy)
            forged_policy["enabled"] = False
            with pytest.raises(ContractValidationError):
                prove(receipts, facts, prompt, binding, forged_policy, policy_ref)
            for counter in ("attempts", "model_requests"):
                forged_facts = deepcopy(facts)
                forged_facts[-1]["payload"]["payload"][counter] = True
                with pytest.raises(ContractValidationError):
                    prove(receipts, forged_facts, prompt, binding, policy, policy_ref)


def test_native_graph_without_compaction_stores_current_and_final_text_not_fixed_rules(tmp_path, monkeypatch):
    fixture = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "plain.sqlite", public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service, policy=False, once=False)
        final = run(service, create(service, doc), "An ordinary user input.")
        assert final["status"] == "succeeded", final["chains"]
        effective = node_output(final, entries["a"]["merge"])
        assert [message["role"] for message in effective["messages"]] == ["user", "assistant"]
        assert all(item["kind"] == "history" and item["compaction"] == "allowed" for item in effective["layout"])
        assert effective["generation"] == 0 and not persisted_once_contents(effective)
        assert len(fixture.calls) == 1


def test_generic_object_write_cannot_replace_native_context_root(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "generic-context-write.sqlite")) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service, policy=False, once=False)
        initial = create(service, doc)
        key = "context-a"
        record = initial["objects"][key]
        forged = deepcopy(record["value"])
        forged["messages"] = [{
            "message_id": str(uuid4()), "role": "user",
            "blocks": [{"kind": "text", "text": "unaccepted context"}],
        }]
        forged["layout"] = [{
            "kind": "history", "round_id": str(uuid4()), "compaction": "allowed",
        }]

        with pytest.raises(ContractValidationError) as denied:
            service.update_session_objects(
                initial["workflow_session_id"],
                node_id=entries["a"]["merge"]["node_binding_id"],
                writes=[WriteIntent(key, record["revision"], "generic-context-forge", forged).to_dict()],
                expected_revision=initial["revision"], idempotency_key="generic-context-forge",
            )

        assert denied.value.reason_code == "context_managed_write_denied"
        assert service.get_session(initial["workflow_session_id"]) == initial


@pytest.mark.parametrize("depth", [0, 2, 20])
def test_native_graph_middle_depth_reaches_model_and_does_not_persist_material(
    tmp_path, monkeypatch, depth,
):
    fixture = NativeTransport()
    database = tmp_path / "depth.sqlite"
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service, policy=False, once=False)
        entries["a"]["fixed"]["config"]["presentation"].update(placement="middle", depth=depth)
        first = run(service, create(service, doc), "First input.")
        assert first["status"] == "succeeded", first["chains"]
        second = run(service, first, "Second input.")
        assert second["status"] == "succeeded", second["chains"]
        third = run(service, second, "Current input.")
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
        assert not persisted_once_contents(effective)
        sid, chain = third["workflow_session_id"], third["selected_chain_run_id"]
    with closing(GraphWorkflowService(database, public_model_factory=fixture.factory)) as reopened:
        archived = reopened.get_run(sid, chain)
        frozen = next(output["payload"] for output in archived["outputs"]
                      if output["node_binding_id"] == entries["a"]["assembly"]["node_binding_id"])
        assert frozen == prompt
        assert reopened.get_session(sid)["status"] == "succeeded"
        assert len(fixture.calls) == 3


def test_context_merge_rejects_stale_candidate_without_replaying_or_adopting(tmp_path):
    fixture = NativeTransport()
    with closing(GraphWorkflowService(tmp_path / "stale-candidate.sqlite",
                                      public_model_factory=fixture.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc, entries = native_graph(service)
        first = run(service, create(service, doc), "First accepted request.")
        assert first["status"] == "succeeded", first["chains"]
        first_view = node_output(first, entries["a"]["merge"])
        first_view_ref = node_output(first, entries["a"]["merge"], "commit")["view_ref"]
        first_object = deepcopy(first["objects"]["context-a"])
        second = run(service, first, "Second accepted request.")
        assert second["status"] == "succeeded", second["chains"]
        calls = len(fixture.calls)
        from phase1_agent.context_v4 import prepare_native_context_adoption_v5
        with pytest.raises(ContractValidationError) as rejected:
            prepare_native_context_adoption_v5(
                workflow_session_id=second["workflow_session_id"], object_key="context-a",
                object_record=second["objects"]["context-a"],
                view_ref=first_view_ref, view=first_view)
        assert rejected.value.reason_code == "context_stale_basis"
        assert first["objects"]["context-a"] == first_object
        assert service.get_session(second["workflow_session_id"])["objects"]["context-a"] == \
            second["objects"]["context-a"]
        assert len(fixture.calls) == calls
