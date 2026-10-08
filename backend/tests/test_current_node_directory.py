"""Default catalog advertises current routes, not retired implementations."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.content_contracts import make_prompt_item, prompt_content, text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.prompt_contract import validate_ready_prompt
from phase1_agent.prompt_lifecycle import lifecycle_prompt_content, make_lifecycle_prompt_item
from test_model_package import uid


def test_default_directory_has_only_current_routes_and_protocol_options():
    registry = create_package_registry().registry
    versions = {}
    for entry in registry.catalog():
        if entry["executable"]:
            versions.setdefault(entry["component_id"], set()).add(entry["component_version"])
    assert len(versions) == 41
    assert sum(map(len, versions.values())) == 44
    assert versions["models.source"] == {"2", "4"}
    assert versions["models.chat"] == {"3", "4"}
    assert versions["agents.execute"] == {"4", "8"}
    assert all(values == {"4"} for key, values in versions.items() if key.startswith("context."))
    assert all(values == {"2"} for key, values in versions.items() if key.startswith("prompts."))
    assert "agents.delta" not in versions
    assert not {"CONTEXT_UNIT", "AGENT_DELTA", "AGENT_CONTEXT", "CONTEXT_SUMMARY",
                "CONTEXT_COMPRESSION_PLAN"} & {row["type_id"] for row in registry.data_types.catalog()}
    assert not any(row["package_id"] == "workflow.context-compression" for row in registry.package_lock)


def test_current_chat_consumes_lifecycle_and_explicit_raw_material_without_losing_references():
    registry = create_package_registry().registry
    presentation = {"role": "system", "placement": "before", "depth": None, "order": 0, "enabled": True}
    lifecycle = lifecycle_prompt_content([make_lifecycle_prompt_item(
        uid(1), uid(2), "Current rule", presentation, lifecycle="context_once", compaction="never")])
    raw = prompt_content([make_prompt_item(uid(3), uid(4), "Raw converter rule", presentation)])
    inputs = {"input": [lifecycle], "raw_prompt": [raw], "current_input": text_content("Current task")}
    before = deepcopy(inputs)
    refs = {port: [{"edge_id": uid(20 + index), "output_id": uid(30 + index), "order": 0}]
            for index, port in enumerate(inputs)}
    result = registry.get("prompts.assembly", "2").executor(
        {}, inputs, SimpleNamespace(input_artifact_refs=lambda port: deepcopy(refs.get(port, []))))["output"]
    assert validate_ready_prompt(result) == result
    assert result["assembly"]["manifest"]["source_output_refs"] == [*refs["input"], *refs["raw_prompt"]]
    assert result["assembly"]["manifest"]["current_input_refs"] == refs["current_input"]
    assert {message["content"] for message in result["assembly"]["messages"]} == {
        "Current rule", "Raw converter rule", "Current task"}
    assert inputs == before
    with pytest.raises(ContractValidationError):
        registry.get("prompts.assembly", "2").executor(
            {}, {"input": [raw]}, SimpleNamespace(input_artifact_refs=lambda port: []))
