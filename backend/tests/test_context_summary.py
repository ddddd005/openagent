"""Explicit coverage, separate task gates and protected effective prompt projection."""

from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest

from phase1_agent.content_contracts import text_content
from phase1_agent.context_prompt import assemble_summary_context_prompt
from phase1_agent.context_summary import (
    accept_summary_candidate, plan_compression, replace_context_prefix, summary_prompt,
)
from phase1_agent.context_v3 import read_effective_view, validate_effective_view
from phase1_agent.graph_contracts import GraphDiagnosticError

from test_context_package import ref, record, uid, unit


def source():
    state = record()
    state["schema_version"] = 3
    view = read_effective_view(state, uid(1), "context", uid(3))
    view["units"] = [{"unit_ref": ref(index + 50), "unit": unit(index + 10)}
                     for index in range(4)]
    for index, entry in enumerate(view["units"]):
        entry["unit"]["root"]["content"] = f"R{index} ORION important source detail " * 20
    return validate_effective_view(view)


def policy(**changes):
    return {"prefix_units": 2, "min_chars": 5, "max_chars": 150, "max_ratio": 0.5,
            "required_terms": ["ORION"], **changes}


def result(text):
    return {"schema_version": 1, "kind": "workflow.model-result", "binding_id": uid(100),
            "request_id": uid(101), "finish_reason": "stop", "content": text, "tool_calls": [],
            "response_id": "offline", "model": "offline", "usage": None,
            "fact_refs": [{"fact_id": uid(102 + index)} for index in range(3)]}


def summary_fixture(view=None, **changes):
    view = source() if view is None else view
    plan = plan_compression(view, ref(70), policy(**changes), uid(71))
    prompt = summary_prompt(plan, ref(72), source_output_refs=[
        {"edge_id": uid(73), "output_id": uid(72), "order": 0}])
    summary = accept_summary_candidate(plan, ref(72), prompt, ref(74),
                                       result("ORION decisions preserved."), ref(75), uid(76))
    return view, plan, summary


def test_summary_replaces_exact_prefix_without_changing_consumption_or_recreating_turn():
    view, plan, summary = summary_fixture()
    changed = replace_context_prefix(view, ref(70), plan, ref(72), summary, ref(77))
    assert view["units"] == source()["units"]
    assert changed["units"][1:] == view["units"][2:]
    assert set(changed["units"][0]) == {"summary_ref", "summary"}
    assert changed["accepted_delta_ids"] == view["accepted_delta_ids"]
    prompt = assemble_summary_context_prompt([], changed, text_content("new question"),
                                             context_ref=ref(78), current_input_ref=ref(79))
    assert prompt["messages"][0] == {"role": "user", "content": summary["text"]}
    assert prompt["provenance"][0]["kind"] == "summary"
    assert all("R0 " not in message["content"] and "R1 " not in message["content"]
               for message in prompt["messages"])
    assert prompt["messages"][-1]["content"] == "new question"


@pytest.mark.parametrize("text,changes,reason", [
    ("missing declared entity", {}, "context_summary_quality_failed"),
    ("ORION", {"min_chars": 10}, "context_summary_length_failed"),
    ("ORION " * 100, {}, "context_summary_length_failed"),
])
def test_task_quality_and_length_fail_separately(text, changes, reason):
    view = source()
    plan = plan_compression(view, ref(70), policy(**changes), uid(71))
    prompt = summary_prompt(plan, ref(72), source_output_refs=[
        {"edge_id": uid(73), "output_id": uid(72), "order": 0}])
    with pytest.raises(GraphDiagnosticError) as rejected:
        accept_summary_candidate(plan, ref(72), prompt, ref(74), result(text), ref(75), uid(76))
    assert rejected.value.reason_code == reason


def test_summary_recompression_expands_original_coverage_and_rejects_overlap_or_other_basis():
    view, plan, summary = summary_fixture()
    first = replace_context_prefix(view, ref(70), plan, ref(72), summary, ref(77))
    second_plan = plan_compression(first, ref(78), policy(), uid(81))
    assert second_plan["selected_refs"] == [ref(77), view["units"][2]["unit_ref"]]
    assert second_plan["covered_round_refs"] == [entry["unit_ref"] for entry in view["units"][:3]]
    with pytest.raises(GraphDiagnosticError) as rejected:
        replace_context_prefix(view, ref(79), plan, ref(72), summary, ref(77))
    assert rejected.value.reason_code == "context_compression_basis_mismatch"
    overlap = deepcopy(first)
    overlap["units"].append(view["units"][0])
    with pytest.raises(GraphDiagnosticError) as rejected:
        validate_effective_view(overlap)
    assert rejected.value.reason_code == "context_summary_overlap"
    with pytest.raises(GraphDiagnosticError) as rejected:
        replace_context_prefix(view, ref(70), plan, ref(72), text_content("covers R0 R1"), ref(77))
    assert rejected.value.reason_code == "context_invalid_contract"


def test_full_actual_prompt_character_budget_fails_without_dropping_tail_or_current_input():
    view, plan, summary = summary_fixture()
    changed = replace_context_prefix(view, ref(70), plan, ref(72), summary, ref(77))
    with pytest.raises(GraphDiagnosticError) as rejected:
        assemble_summary_context_prompt([], changed, text_content("new question"),
                                        context_ref=ref(78), current_input_ref=ref(79), character_budget=25)
    assert rejected.value.reason_code == "context_prompt_length_failed"
    assert len(changed["units"]) == 3


def test_summary_data_and_composition_import_without_agent_kernel():
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    script = """
import importlib
import sys
class BlockKernel:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('phase1_agent.agent_executor', 'phase1_agent.runtime'):
            raise AssertionError('data contracts imported kernel: ' + fullname)
sys.meta_path.insert(0, BlockKernel())
for module in ('agent_receipt_contract', 'context_v2', 'context_v3',
               'context_summary', 'context_compression_package'):
    importlib.import_module('phase1_agent.' + module)
"""
    checked = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {source_root!r})\n" + script],
                             capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stderr
