"""Graph prompt assembly preserves closed history while processing editable text."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse
from phase1_agent.graph_contracts import validate_content_value
from phase1_agent.graph_nodes import create_default_registry
from phase1_agent.graph_prompt import archive_materials, assemble_materials, merge_materials, validate_ready_prompt
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.workbench_resources import WorkbenchResourceStore
from test_graph_agent_runtime import Harness, final
from test_graph_agent_service import graph, harness
from test_graph_service import create, document, edge, node, run
from test_workbench_resources import resource


LIMITS = {"max_messages": 4096, "max_total_chars": 4_000_000}


def archive(text="editable old", final_text="protected old"):
    native = Harness([ModelResponse("stop", content=text), final(final_text)])
    package = native.execute().to_dict()
    return {"root": [package["snapshot"]["config"]["payload"]["graph_preparation"]["current_root"]],
            "turn": package["turn"], "snapshot": package["snapshot"]}


def ready(archives, *, values=None):
    index = {item["turn"]["turn_id"]: item for item in archives}
    return assemble_materials(
        values or [archive_materials(archives)], "current input", input_id=str(uuid4()),
        limits=LIMITS, resolve_archive=index.__getitem__,
    )


def test_closed_context_has_complete_root_delta_pairs_and_one_current_input():
    original = archive()
    before = deepcopy(original)
    assembled = ready([original])
    evidence = assembled["assembly"]
    messages = evidence["prompt"]["messages"]
    floors = evidence["prompt"]["manifest"]["logical_floors"]
    assert floors == [[original["root"][0]["message_id"]],
                      [message["message_id"] for message in original["turn"]["messages"]],
                      [evidence["current_root"]["message_id"]]]
    assert messages[:-1] == original["root"] + original["turn"]["messages"]
    assert messages[-1]["blocks"][0]["text"] == "current input"
    assert evidence["context_basis"] == [{"turn_id": original["turn"]["turn_id"],
                                           "run_id": original["turn"]["run_id"]}]
    assert validate_ready_prompt(assembled) == assembled
    assert original == before


def test_multiple_history_sources_merge_shared_exact_ancestors_once():
    first, second = archive("first source"), archive("second source")
    first_value = archive_materials([first])
    cumulative_value = archive_materials([first, second])
    assembled = ready([first, second], values=[first_value, cumulative_value, deepcopy(first_value)])
    messages = assembled["assembly"]["prompt"]["messages"]
    expected = first["root"] + first["turn"]["messages"] + second["root"] + second["turn"]["messages"]
    assert messages[:-1] == expected
    assert len({message["message_id"] for message in messages}) == len(messages)
    assert len(assembled["assembly"]["context_basis"]) == 2
    assert len(assembled["assembly"]["prompt"]["manifest"]["logical_floors"]) == 5


def test_conflicting_duplicate_edits_and_incomplete_history_are_rejected():
    original = archive()
    source = archive_materials([original])
    changed = deepcopy(source)
    changed["items"][0]["text"] = "different root edit"
    with pytest.raises(ContractValidationError) as conflict:
        merge_materials([source, changed])
    assert conflict.value.reason_code == "graph_context_conflict"
    incomplete = deepcopy(source)
    incomplete["items"].pop()
    with pytest.raises(ContractValidationError) as missing:
        ready([original], values=[incomplete])
    assert missing.value.reason_code == "graph_context_incomplete"


def test_prompt_regex_edits_plain_context_preserves_tool_and_final_and_invalidates_assembly():
    original = archive()
    assembled = ready([original])
    entry = create_default_registry().get("workflow.regex", "1")
    config = {**entry.definition.default_config, "mode": "prompt", "pattern": "old", "replacement": "new"}
    processed = entry.executor(config, {"input": assembled}, SimpleNamespace(node_binding_id=str(uuid4())))["output"]
    assert processed["stage"] == "materials" and processed["assembly"] is None
    protected = [item for item in assembled["items"] if item["protected"]]
    assert [item for item in processed["items"] if item["protected"]] == protected
    assert any(item["text"] == "editable new" for item in processed["items"])
    with pytest.raises(ContractValidationError) as unready:
        validate_ready_prompt(processed)
    assert unready.value.reason_code == "graph_prompt_not_ready"
    rebuilt = ready([original], values=[processed])
    assert any(message["blocks"] == [{"kind": "text", "text": "editable new"}]
               for message in rebuilt["assembly"]["prompt"]["messages"])
    assert original["turn"]["messages"][0]["blocks"][0]["text"] == "editable old"


@pytest.mark.parametrize("change", [
    lambda item: item.update(protected=False),
    lambda item: item.update(text="changed protected final"),
    lambda item: item["metadata"]["context"]["message"]["blocks"][0].update(raw_arguments="{}"),
])
def test_protected_context_cannot_be_unprotected_or_rewritten(change):
    original = archive()
    value = archive_materials([original])
    final_id = original["turn"]["final"]["message_id"]
    protected = next(item for item in value["items"] if item["item_instance_id"] == final_id)
    change(protected)
    with pytest.raises(ContractValidationError):
        ready([original], values=[value])


def test_dispatch_rebuild_rejects_changed_materials_with_unchanged_ready_evidence(harness):
    service, scripts, requests, _ = harness
    registered = service.registry.get("workflow.prompt-assembly", "1")

    def corrupt(config, inputs, context):
        returned = registered.executor(config, inputs, context)
        returned["output"]["items"][0]["text"] = "tampered after assembly"
        # The envelope still carries the original, internally valid manifest.
        validate_content_value(returned["output"], "PROMPT")
        return returned

    service.registry._nodes[("workflow.prompt-assembly", "1")] = replace(registered, executor=corrupt)
    scripts.append([final()])
    finished = run(service, create(service, graph(service)))
    assert finished["status"] == "failed"
    assert finished["chains"][-1]["diagnostic"]["code"] == "graph_prompt_not_ready"
    assert requests == []
    assert service._native_runtime is None


def test_global_update_changes_next_run_without_changing_saved_workflow_or_past_results(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "global.sqlite")) as service:
        first_resource = resource("first global")
        with closing(service._store()) as store:
            WorkbenchResourceStore(store).write("content", first_resource, expected_revision=0, idempotency_key=str(uuid4()))
        source = node(service.registry, "global-content", 1, resource_id=first_resource["resource_id"])
        output = node(service.registry, "output", 2, mode="prompt")
        doc = document([source, output], [edge(source, output, 1)])
        initial = create(service, doc)
        first = run(service, initial)
        old_chain = service.get_run(first["workflow_session_id"], first["chains"][-1]["chain_run_id"])
        updated = deepcopy(first_resource)
        updated["revision"] = 2
        updated["members"][0]["text"] = "second global"
        with closing(service._store()) as store:
            WorkbenchResourceStore(store).write("content", updated, expected_revision=1, idempotency_key=str(uuid4()))
        second = run(service, first)
        assert second["status"] == "succeeded"
        assert second["nodes"][-1]["outputs"]["output"]["items"][0]["text"] == "second global"
        assert first["nodes"][-1]["outputs"]["output"]["items"][0]["text"] == "first global"
        assert service.get_definition(doc["workflow_definition_id"]) == doc
        assert service.get_run(first["workflow_session_id"], first["chains"][-1]["chain_run_id"]) == old_chain
        latest = service.get_run(second["workflow_session_id"], second["chains"][-1]["chain_run_id"])
        assert latest["node_runs"][0]["reads"] == [{"kind": "global_content_read",
            "resource_id": first_resource["resource_id"], "revision": 2}]
        assert service._native_runtime is None
