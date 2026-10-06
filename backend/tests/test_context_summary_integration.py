"""Offline public Chat summaries followed by actual Agent facts and atomic context merges."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4
import json

import pytest

from phase1_agent.context_contract import EFFECTIVE_CONTEXT_TYPE
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_contracts import GraphDiagnosticError
from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.graph_nodes import create_package_registry
from phase1_agent.session_objects import SessionObjectStore
from phase1_agent.storage import SqliteStore

from test_agent_integration import output, run
from test_graph_service import copy_current, create, document, edge, node
from test_model_package import source_config
from test_models_service_integration import ModelDatabaseFixture


class SummaryTransport:
    def __init__(self, *, summary_text=None):
        self.calls, self.summary_calls, self.summary_text = [], 0, summary_text

    def factory(self, *, provider, parameters, api_key):
        fixture = self

        class Transport:
            max_retries = 0
            model_parameters = parameters
            provider_address = provider["base_url"].rstrip("/")

            def generate(self, messages, tools):
                fixture.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
                if not tools:
                    fixture.summary_calls += 1
                    return ModelResponse("stop", content=fixture.summary_text or (
                        f"S{fixture.summary_calls}: ORION approved; preserve decisions and pending tasks."))
                return ModelResponse("tool_calls", tool_calls=(ModelToolCall(
                    "final.provider", "final_answer", json.dumps({"answer": {"text": "accepted ORION answer"}})),))

            def close(self):
                pass

        return Transport()


def base_graph(service):
    registry = service.registry
    read, current, model, assembly, execute, merge, display = [
        node(registry, component, 1301 + index) for index, component in enumerate((
            "context.output", "tools.current-input", "models.source", "context.assembly",
            "agents.execute", "context.merge", "tools.output"))]
    for entry, version in ((read, "2"), (assembly, "3"), (execute, "3"), (merge, "2")):
        entry["component_version"] = version
        entry["config"] = deepcopy(registry.get(entry["component_id"], version).definition.default_config)
    for entry in (read, merge):
        entry["config"]["agent_node_id"] = execute["node_binding_id"]
    model["config"] = source_config()
    doc = document([read, current, model, assembly, execute, merge, display], [
        edge(read, assembly, 1301, target_port="view"),
        edge(current, assembly, 1302, target_port="current_input"),
        edge(model, execute, 1303, target_port="model"),
        edge(assembly, execute, 1304, target_port="prompt"),
        edge(read, merge, 1305, target_port="view"),
        edge(execute, merge, 1306, source_port="context", target_port="context"),
        edge(execute, display, 1307, source_port="result"),
    ])
    doc.update(schema_version=2, execution_roots=[merge["node_binding_id"]],
               package_lock=list(registry.package_lock), object_bindings=[
                   ObjectBinding("context", EFFECTIVE_CONTEXT_TYPE, 3, "shared",
                                 readers=(read["node_binding_id"], merge["node_binding_id"]),
                                 writers=(merge["node_binding_id"],)).to_dict()])
    return doc


def compression_graph(service, doc, *, count=1, wrong_prompt=False, mismatch=False):
    doc = deepcopy(doc)
    read = next(entry for entry in doc["nodes"] if entry["component_id"] == "context.output")
    assembly = next(entry for entry in doc["nodes"] if entry["component_id"] == "context.assembly")
    merge = next(entry for entry in doc["nodes"] if entry["component_id"] == "context.merge")
    model = next(entry for entry in doc["nodes"] if entry["component_id"] == "models.source")
    plan, request, chat, summary, replace = [
        node(service.registry, component, 1401 + index) for index, component in enumerate((
            "context.plan", "context.summary-prompt", "models.chat", "context.summary", "context.replace"))]
    plan["config"].update(prefix_units=count, required_terms=["ORION"], max_chars=200)
    doc["nodes"] += [plan, request, chat, summary, replace]
    doc["edges"] = [connection for connection in doc["edges"]
                    if not (connection["source_node_id"] == read["node_binding_id"]
                            and connection["target_node_id"] in (assembly["node_binding_id"], merge["node_binding_id"]))]
    chat_request = request
    if wrong_prompt:
        chat_request = node(service.registry, "context.summary-prompt", 1499)
        doc["nodes"].append(chat_request)
        doc["edges"].append(edge(plan, chat_request, 1499, target_port="plan"))
    doc["edges"] += [
        edge(read, plan, 1401, target_port="view"), edge(plan, request, 1402, target_port="plan"),
        edge(model, chat, 1403, target_port="model"),
        edge(chat_request, chat, 1404, target_port="prompt"),
        edge(plan, summary, 1405, target_port="plan"), edge(request, summary, 1406, target_port="prompt"),
        edge(chat, summary, 1407, target_port="result"),
        edge(read, replace, 1408, target_port="view"), edge(plan, replace, 1409, target_port="plan"),
        edge(summary, replace, 1410, target_port="summary"),
        edge(replace, assembly, 1411, target_port="view"),
        edge(read if mismatch else replace, merge, 1412, target_port="view"),
    ]
    return doc


def rebind(service, view, doc):
    doc = deepcopy(doc)
    doc["revision"] = view["definition_revision"] + 1
    service.save_definition(doc, expected_revision=view["definition_revision"], idempotency_key=str(uuid4()))
    return service.rebind_session(
        view["workflow_session_id"], definition_revision=doc["revision"], expected_revision=view["revision"],
        expected_data_revision=view["data_revision"], expected_head_revision=view["head_revision"],
        idempotency_key=str(uuid4())), doc


def test_ten_round_prefix_replacement_recompression_and_next_round_never_restore_originals(
        tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-summary-test")
    transport = SummaryTransport()
    with closing(GraphWorkflowService(tmp_path / "summary.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        original_doc = base_graph(service)
        view = create(service, original_doc)
        for index in range(10):
            view = run(service, view, f"R{index + 1:02}: ORION secret source text " * 10)
            assert view["status"] == "succeeded", view["chains"]
        before = deepcopy(view["objects"]["context"])
        old_chain_id = view["selected_chain_run_id"]
        compressed_doc = compression_graph(service, original_doc, count=10)
        view, compressed_doc = rebind(service, view, compressed_doc)
        first = run(service, view, "R11: ORION new question")
        assert first["status"] == "succeeded", first["chains"]
        effective, summary = output(first, "context.merge"), output(first, "context.summary")
        assert len(effective["units"]) == 2 and "summary" in effective["units"][0]
        assert len(summary["covered_round_refs"]) == 10
        frozen = output(first, "context.assembly")
        assert frozen["context_ref"] == output(first, "agents.execute", "context")["basis_view_ref"]
        assert frozen["messages"][0]["content"] == summary["text"]
        assert "R01" not in json.dumps(frozen["messages"])
        assert len(first["objects"]["context"]["value"]["accepted_delta_ids"]) == 11
        assert output(first, "context.merge", "commit")["receipt"]["revision_id"] == (
            first["objects"]["context"]["revision_id"])
        copied_doc = deepcopy(compressed_doc)
        copied_doc["workflow_definition_id"] = str(uuid4())
        copied_doc["revision"] = 1
        # A child inherits S1 before the parent creates S2.
        child = copy_current(service, first, copied_doc)
        compressed_doc = deepcopy(compressed_doc)
        next(entry for entry in compressed_doc["nodes"]
             if entry["component_id"] == "context.plan")["config"]["prefix_units"] = 2
        view, compressed_doc = rebind(service, first, compressed_doc)
        second = run(service, view, "R12: ORION second new question")
        assert second["status"] == "succeeded", second["chains"]
        summary2 = output(second, "context.summary")
        assert len(summary2["direct_unit_refs"]) == 2 and len(summary2["covered_round_refs"]) == 11
        assert output(second, "context.assembly")["messages"][0]["content"].startswith("S2:")
        assert "S1:" not in json.dumps(output(second, "context.assembly")["messages"])
        # No compression on the next execution: the selected pointer alone retains S2.
        view, _ = rebind(service, second, original_doc)
        third = run(service, view, "R13: ORION later")
        assert third["status"] == "succeeded", third["chains"]
        assert output(third, "context.assembly")["messages"][0]["content"].startswith("S2:")
        assert "R01" not in json.dumps(output(third, "context.assembly")["messages"])
        assert service.get_session(child["workflow_session_id"])["objects"]["context"]["value"] == (
            first["objects"]["context"]["value"])
        # Rebind child to plain lifecycle; it reads inherited S1, not parent latest S2.
        child_plain = deepcopy(original_doc)
        child_plain["workflow_definition_id"] = copied_doc["workflow_definition_id"]
        child, _ = rebind(service, child, child_plain)
        child_final = run(service, child, "child ORION later")
        assert child_final["status"] == "succeeded", child_final["chains"]
        assert output(child_final, "context.assembly")["messages"][0]["content"].startswith("S1:")
        assert "S2:" not in json.dumps(output(child_final, "context.assembly")["messages"])
        read_id = next(entry["node_binding_id"] for entry in original_doc["nodes"]
                       if entry["component_id"] == "context.output")
        old_revision = service.read_session_object(first["workflow_session_id"], object_key="context",
                                                    node_id=read_id, revision_id=before["revision_id"])
        assert old_revision["value"] == before["value"]
        old_history = service.get_run(first["workflow_session_id"], old_chain_id)
        assert old_history  # Original records remain readable through their exact history.
        assert transport.summary_calls == 2
        child_sid = child_final["workflow_session_id"]
    with closing(GraphWorkflowService(tmp_path / "summary.sqlite", public_model_factory=transport.factory)) as reopened:
        child = run(reopened, reopened.get_session(child_sid), "child ORION after reopen")
        assert child["status"] == "succeeded", child["chains"]
        assert output(child, "context.assembly")["messages"][0]["content"].startswith("S1:")
        assert "S2:" not in json.dumps(output(child, "context.assembly")["messages"])
        assert transport.summary_calls == 2


@pytest.mark.parametrize("case,expected", [
    ("quality", "context_summary_quality_failed"),
    ("length", "context_summary_length_failed"),
    ("origin", "context_summary_generation_unproven"),
    ("basis", "context_delta_prompt_mismatch"),
])
def test_failed_summary_or_wrong_basis_keeps_current_context_and_does_not_recall_agent(
        tmp_path, monkeypatch, case, expected):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-summary-test")
    transport = SummaryTransport(summary_text=(
        "missing task entity" if case == "quality" else "ORION " * 100 if case == "length" else None))
    with closing(GraphWorkflowService(tmp_path / f"{case}.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc = base_graph(service)
        first = run(service, create(service, doc), "R01 ORION original " * 30)
        before = deepcopy(first["objects"]["context"])
        doc = compression_graph(service, doc, wrong_prompt=case == "origin", mismatch=case == "basis")
        view, _ = rebind(service, first, doc)
        failed = run(service, view, "R02 ORION new question")
        assert failed["status"] == "failed", failed["chains"]
        assert failed["objects"]["context"] == before
        history = service.get_run(failed["workflow_session_id"], failed["active_chain_run_id"])
        assert any(item["diagnostic"] and item["diagnostic"]["code"] == expected for item in history["node_runs"])
        assert transport.summary_calls == 1
        assert len(transport.calls) == (3 if case == "basis" else 2)


def test_explicit_empty_history_compression_rejects_before_any_model_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-summary-test")
    transport = SummaryTransport()
    with closing(GraphWorkflowService(tmp_path / "empty.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        doc = compression_graph(service, base_graph(service))
        final = run(service, create(service, doc), "ORION current input")
        assert final["status"] == "failed", final["chains"]
        assert transport.calls == []
        assert final["objects"]["context"]["revision"] == 1
        history = service.get_run(final["workflow_session_id"], final["active_chain_run_id"])
        assert any(entry["diagnostic"] and entry["diagnostic"]["code"] == "context_compression_range_missing"
                   for entry in history["node_runs"])


def test_compression_registration_and_execution_do_not_require_agent_dispatch(tmp_path, monkeypatch):
    loaded = create_package_registry(enabled={"workflow.context-compression": "1.0.0"})
    assert loaded.registry.get("agents.execute", "3") is None
    assert loaded.registry.get("context.summary", "1")
    assert loaded.registry.data_types.get("CONTEXT_VIEW", 3, scope="content")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-summary-test")
    transport = SummaryTransport()
    with closing(GraphWorkflowService(tmp_path / "zero-agent.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        original = base_graph(service)
        initial = run(service, create(service, original), "ORION existing closed history " * 30)
        doc = compression_graph(service, original)
        # Keep the Agent identity declaration bound to the context, while the
        # independent replacement branch alone is an execution root.
        doc["nodes"] = [entry for entry in doc["nodes"] if entry["component_id"] != "tools.output"]
        surviving = {entry["node_binding_id"] for entry in doc["nodes"]}
        doc["edges"] = [connection for connection in doc["edges"]
                        if connection["target_node_id"] in surviving
                        and connection["source_node_id"] in surviving]
        doc["execution_roots"] = [next(entry["node_binding_id"] for entry in doc["nodes"]
                                        if entry["component_id"] == "context.replace")]
        before = deepcopy(initial["objects"]["context"])
        view, _ = rebind(service, initial, doc)
        final = run(service, view, "unused current input")
        assert final["status"] == "succeeded", final["chains"]
        assert final["objects"]["context"] == before
        assert len(transport.calls) == 2 and transport.summary_calls == 1
        assert output(final, "context.replace")["units"][0]["summary"]["text"].startswith("S1:")
        history = service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])
        assert not any(entry["node_binding_id"] == next(node["node_binding_id"] for node in doc["nodes"]
                       if node["component_id"] == "agents.execute") for entry in history["node_runs"])


def test_summary_merge_rollback_and_acceptance_retry_reuse_summary_and_agent_results(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-summary-test")
    transport = SummaryTransport()
    with closing(GraphWorkflowService(tmp_path / "retry-summary.sqlite", public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        original = base_graph(service)
        first = run(service, create(service, original), "ORION original source " * 30)
        before = deepcopy(first["objects"]["context"])
        view, _ = rebind(service, first, compression_graph(service, original))
        apply, fail = SessionObjectStore.apply, [True]

        def fail_after_write(store, sid, *, node_id, writes):
            receipts = apply(store, sid, node_id=node_id, writes=writes)
            if writes and fail[0]:
                fail[0] = False
                raise OSError("injected summary merge transaction fault")
            return receipts

        monkeypatch.setattr(SessionObjectStore, "apply", fail_after_write)
        pending = run(service, view, "ORION next question")
        assert pending["status"] == "archive_failed", pending["chains"]
        assert pending["objects"]["context"] == before
        assert len(transport.calls) == 3 and transport.summary_calls == 1
        accepted_summary = deepcopy(output(pending, "context.summary"))
        sid, chain = pending["workflow_session_id"], pending["active_chain_run_id"]
        service.control(sid, action="retry_acceptance", expected_revision=pending["revision"],
                        idempotency_key=str(uuid4()))
        service.wait(chain)
        final = service.get_session(sid)
        assert final["status"] == "succeeded", final["chains"]
        assert len(transport.calls) == 3 and transport.summary_calls == 1
        assert output(final, "context.merge")["units"][0]["summary"] == accepted_summary
        assert output(final, "context.merge", "commit")["receipt"]["revision_id"] == (
            final["objects"]["context"]["revision_id"])


def test_persistent_type_write_proof_rejects_forged_summary_and_agent_origins(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-summary-test")
    transport = SummaryTransport()
    with closing(GraphWorkflowService(tmp_path / "write-proof.sqlite",
                                      public_model_factory=transport.factory)) as service:
        ModelDatabaseFixture.write(service, 1)
        original = base_graph(service)
        first = run(service, create(service, original), "ORION immutable original source " * 30)
        before = deepcopy(first["objects"]["context"])
        view, _ = rebind(service, first, compression_graph(service, original))
        final = run(service, view, "ORION follow-up")
        assert final["status"] == "succeeded", final["chains"]
        desired, sid = final["objects"]["context"]["value"], final["workflow_session_id"]
        summary = output(final, "context.summary")
        packet = output(final, "agents.execute", "context")
        effective = output(final, "context.merge")
        with closing(SqliteStore(service.database)) as store:
            objects = SessionObjectStore(store, service.registry.data_types)

            def check(mutation=None):
                def resolve(reference):
                    detail = objects._resolve_write_artifact(sid, reference)
                    if mutation:
                        mutation(reference, detail)
                    return detail
                return service.registry.data_types.validate_write(EFFECTIVE_CONTEXT_TYPE, 3, desired, {
                    "workflow_session_id": sid, "object_key": "context", "current_record": before,
                    "operation": "put", "resolve_artifact": resolve,
                })

            assert check() == desired
            attacks = [
                (summary["generation"]["result_ref"], "context_summary_generation_unproven",
                 lambda detail: detail["input_refs"]["prompt"][0].update(
                     output_id=summary["plan_ref"]["output_id"])),
                (effective["derivation"]["round_ref"], "context_agent_binding_mismatch",
                 lambda detail: detail["producer"].update(node_binding_id=str(uuid4()))),
                (effective["units"][0]["summary_ref"], "context_adoption_source_invalid",
                 lambda detail: detail.update(component_id="tools.text")),
                (packet["receipts"]["frozen_prompt_ref"], "context_delta_prompt_mismatch",
                 lambda detail: detail["value"].update(context_ref=summary["basis_view_ref"])),
            ]
            for target, expected, forge in attacks:
                def mutation(reference, detail):
                    if reference == target:
                        forge(detail)
                with pytest.raises(GraphDiagnosticError) as rejected:
                    check(mutation)
                assert rejected.value.reason_code == expected
            assert packet["owner"]["workflow_session_id"] == sid
        merge_id = next(entry["node_binding_id"] for entry in original["nodes"]
                        if entry["component_id"] == "context.merge")
        adopted = service.adopt_context_view(
            sid, node_id=merge_id, view_output_id=desired["view_ref"]["output_id"],
            expected_revision=final["revision"], idempotency_key=str(uuid4()))
        assert adopted["results"] == []
        assert adopted["session"]["objects"]["context"] == final["objects"]["context"]
        assert len(transport.calls) == 3
