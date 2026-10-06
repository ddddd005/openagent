"""Closed legacy archives stay readable without any legacy execution imports."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import dumps_pretty
from phase1_agent.legacy_archive import read_closed_legacy_archive
from phase1_agent.legacy_context_contracts import (
    BASIC_CONTEXT_COMPONENT, BASIC_CONTEXT_VERSION,
    PREPARED_CONTEXT_COMPONENT, PREPARED_CONTEXT_VERSION,
    project_basic_turn, project_prepared_turn, validate_frozen_preparation,
)
from phase1_agent.prepared_request import PREPARATION_CAPABILITY


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"
FAMILIES = ("basic", "prepared1", "prepared2", "rederived2", "prepared3")


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def archive_records(family="basic"):
    bundle = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    snapshot, turn, node_input, run = (
        bundle[kind][0] for kind in ("input_snapshot", "turn", "node_input", "run_record")
    )
    snapshot["s0"][0]["blocks"] = [{"kind": "text", "text": dumps_pretty(node_input["payload"])}]
    descriptor = {
        "component_id": BASIC_CONTEXT_COMPONENT, "component_version": BASIC_CONTEXT_VERSION,
        "capabilities": [],
    }
    snapshot["config"]["payload"]["resolved"] = {"context": descriptor}
    if family != "basic":
        from phase1_agent.prompt_preparation import make_prompt_context_config, prepare_prompt_context
        from phase1_agent.prompt_variables import create_variable_registry, create_variable_snapshot
        from phase1_agent.variable_preparation import (
            make_variable_assignment_plan, prepare_variable_assignments, rederive_root_variable_assignments,
        )

        kwargs, transaction = {}, None
        if family in ("prepared2", "rederived2"):
            registry = create_variable_registry(
                workflow_id=uid(700), revision=1,
                definitions=[{"name": "root", "type": "string", "default": "before"}],
            )
            basis = create_variable_snapshot(
                registry, workflow_session_id=snapshot["workflow_session_id"],
                node_binding_id=snapshot["node_binding_id"],
            )
            plan = make_variable_assignment_plan([{
                "node_id": "root-node", "name": "root", "source": {"kind": "root_input_text"},
            }])
            original_input = deepcopy(node_input)
            if family == "rederived2":
                original_input["input_id"] = uid(701)
                original_input["payload"] = {"text": "original root"}
            frozen = prepare_variable_assignments(basis, plan, original_input)
            rederived = (
                rederive_root_variable_assignments(frozen, node_input)
                if family == "rederived2" else None
            )
            kwargs = {
                "variables": deepcopy(rederived["snapshot"] if rederived else frozen["snapshot"]),
                "variable_plan": plan,
            }
            transaction = {
                "schema_version": 1, "kind": "workflow_variable_preparation",
                "state_ref": {
                    "workflow_session_id": snapshot["workflow_session_id"],
                    "workflow_id": registry["workflow_id"], "registry_revision": 1, "revision": 2,
                },
                "receipt_key": "frozen-variable-result", "frozen": frozen,
                "rederivation": rederived, "seed": None,
            }
        elif family == "prepared3":
            kwargs = {"preparation": {
                "schema_version": 1, "kind": "prompt_preparation_program", "nodes": [],
                "outputs": {"prompt": None, "context": None},
            }}
        config = make_prompt_context_config(
            {"schema_version": 1, "kind": "prompt_collection", "items": []}, **kwargs,
        )
        evidence = prepare_prompt_context(
            node_input, [], workflow_session_id=snapshot["workflow_session_id"],
            node_binding_id=snapshot["node_binding_id"], parent_turn_id=snapshot["parent_turn_id"],
            logical_floors=[], protected_blocks=[], config=config, root_message_id=uid(702),
        )
        descriptor.update(
            component_id=PREPARED_CONTEXT_COMPONENT, component_version=PREPARED_CONTEXT_VERSION,
            capabilities=[PREPARATION_CAPABILITY],
        )
        snapshot["projection_version"] = 2
        snapshot["s0"] = deepcopy(evidence["s0"])
        snapshot["config"]["payload"].update(context=config, context_preparation=evidence)
        if transaction is not None:
            snapshot["config"]["payload"]["variable_preparation"] = transaction
        if family == "prepared3":
            snapshot["config"]["payload"]["program_variable_preparation"] = {
                "schema_version": 1, "kind": "program_variable_preparation",
                "session_id": snapshot["workflow_session_id"], "revision": 1,
                "receipt_key": "frozen-program-result",
            }
    return {"turn": turn, "input_snapshot": snapshot, "node_input": node_input, "run_record": run}


class Records:
    def __init__(self, records, *, fallback=False):
        self.records = records
        self.fallback = fallback
        self.reads = []

    def get_record(self, kind, identity):
        self.reads.append((kind, deepcopy(identity)))
        if kind == "run_record" and self.fallback:
            return None
        if kind == "node_run" and self.fallback:
            return deepcopy(self.records["run_record"])
        return deepcopy(self.records.get(kind))


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("fallback", [False, True])
def test_closed_reader_retains_exact_root_delta_and_frozen_records(family, fallback):
    records = archive_records(family)
    original = deepcopy(records)
    reader = Records(records, fallback=fallback)
    result = read_closed_legacy_archive(reader, records["turn"]["turn_id"])
    assert result["turn"] == records["turn"]
    assert result["snapshot"] == records["input_snapshot"]
    assert result["root"][0]["blocks"] == [{
        "kind": "text", "text": dumps_pretty(records["node_input"]["payload"]),
    }]
    assert records == original
    assert [kind for kind, _ in reader.reads] == [
        "turn", "input_snapshot", "node_input", "run_record", *(["node_run"] if fallback else []),
    ]
    result["root"][0]["blocks"][0]["text"] = "local edit"
    result["snapshot"]["s0"][0]["blocks"][0]["text"] = "local edit"
    result["turn"]["messages"].clear()
    assert records == original


@pytest.mark.parametrize("family", FAMILIES)
def test_compatibility_context_classes_delegate_to_same_pure_projection(family):
    from phase1_agent.bindings import BasicContext
    from phase1_agent.prepared_context import PreparedPromptContext, validate_frozen_preparation as compatibility_validator

    records = archive_records(family)
    turn, snapshot, node_input = (records[key] for key in ("turn", "input_snapshot", "node_input"))
    project = project_basic_turn if family == "basic" else project_prepared_turn
    context = BasicContext() if family == "basic" else PreparedPromptContext()
    assert context.project_turn(turn, node_input, snapshot) == project(turn, node_input, snapshot)
    assert compatibility_validator(snapshot) == validate_frozen_preparation(snapshot)


@pytest.mark.parametrize("field,value", [
    ("status", "paused"), ("result_turn_id", uid(999)), ("snapshot_id", uid(999)),
    ("workflow_session_id", uid(999)), ("node_binding_id", uid(999)),
])
def test_reader_requires_original_closed_owner(field, value):
    records = archive_records()
    records["run_record"][field] = value
    with pytest.raises(ContractValidationError) as invalid:
        read_closed_legacy_archive(Records(records), records["turn"]["turn_id"])
    assert invalid.value.reason_code == "graph_legacy_history_invalid"


@pytest.mark.parametrize("component,version", [
    (uid(999), BASIC_CONTEXT_VERSION), (BASIC_CONTEXT_COMPONENT, "2.0.0"),
    (PREPARED_CONTEXT_COMPONENT, "2.0.0"),
])
def test_custom_or_unknown_context_is_not_substituted(component, version):
    records = archive_records()
    records["input_snapshot"]["config"]["payload"]["resolved"]["context"].update(
        component_id=component, component_version=version,
    )
    with pytest.raises(ContractValidationError) as unsupported:
        read_closed_legacy_archive(Records(records), records["turn"]["turn_id"])
    assert unsupported.value.reason_code == "graph_legacy_context_unsupported"


@pytest.mark.parametrize("family", FAMILIES)
def test_input_and_snapshot_identity_mismatch_is_rejected(family):
    records = archive_records(family)
    records["node_input"]["input_id"] = uid(999)
    with pytest.raises(ContractValidationError, match="identities"):
        read_closed_legacy_archive(Records(records), records["turn"]["turn_id"])


@pytest.mark.parametrize("family", FAMILIES)
def test_frozen_root_cannot_be_replaced_by_current_input(family):
    records = archive_records(family)
    records["node_input"]["payload"] = {"text": "a later input"}
    with pytest.raises(ContractValidationError):
        read_closed_legacy_archive(Records(records), records["turn"]["turn_id"])


@pytest.mark.parametrize("family", FAMILIES[1:])
@pytest.mark.parametrize("defect", ["projection", "config", "s0", "parent", "binding"])
def test_frozen_correspondence_checks_survive_contract_extraction(family, defect):
    records = archive_records(family)
    snapshot = records["input_snapshot"]
    if defect == "projection":
        snapshot["projection_version"] = 1
    elif defect == "config":
        snapshot["config"]["payload"]["context"]["limits"]["assembly"]["max_messages"] -= 1
    elif defect == "s0":
        snapshot["s0"][0]["blocks"][0]["text"] = "changed frozen S0"
    elif defect == "parent":
        snapshot["parent_turn_id"] = uid(999)
    else:
        snapshot["node_binding_id"] = uid(999)
        records["run_record"]["node_binding_id"] = uid(999)
    with pytest.raises(ContractValidationError):
        read_closed_legacy_archive(Records(records), records["turn"]["turn_id"])


@pytest.mark.parametrize("family", ["prepared2", "rederived2"])
@pytest.mark.parametrize("defect", ["owner", "receipt", "seed", "snapshot", "plan"])
def test_variable_transaction_evidence_is_not_relaxed_for_archive_reading(family, defect):
    records = archive_records(family)
    payload = records["input_snapshot"]["config"]["payload"]
    variables = payload["variable_preparation"]
    if defect == "owner":
        variables["state_ref"]["workflow_session_id"] = uid(999)
    elif defect == "receipt":
        variables["receipt_key"] = ""
    elif defect == "seed":
        variables["seed"] = {
            "commit_id": uid(999), "state_ref": deepcopy(variables["state_ref"]),
        }
    elif defect == "snapshot":
        payload["context"]["variables"]["values"]["root"]["value"] = "a later value"
    else:
        payload["context"]["variable_plan"]["assignments"].clear()
    with pytest.raises(ContractValidationError):
        read_closed_legacy_archive(Records(records), records["turn"]["turn_id"])


@pytest.mark.parametrize("defect", ["missing", "revision", "kind"])
def test_program_result_still_requires_saved_variable_evidence(defect):
    records = archive_records("prepared3")
    payload = records["input_snapshot"]["config"]["payload"]
    if defect == "missing":
        del payload["program_variable_preparation"]
    elif defect == "revision":
        payload["program_variable_preparation"]["revision"] = 0
    else:
        payload["program_variable_preparation"]["kind"] = "unrelated"
    with pytest.raises(ContractValidationError):
        read_closed_legacy_archive(Records(records), records["turn"]["turn_id"])


@pytest.mark.parametrize("family", FAMILIES)
def test_fresh_process_reads_with_runtime_storage_and_context_classes_blocked(family):
    program = r'''
import importlib.abc
import json
import sys

blocked = {
    "phase1_agent.workflow", "phase1_agent.workflow_host",
    "phase1_agent.graph_agent_runtime", "phase1_agent.prepared_context",
    "phase1_agent.bindings", "phase1_agent.kernel", "phase1_agent.runtime",
    "phase1_agent.storage", "phase1_agent.workbench_resources",
}
if sys.argv[1] == "basic":
    blocked.update({"phase1_agent.prompt_preparation", "phase1_agent.variable_preparation"})

class DenyExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError("Archive read imported execution: " + fullname)

sys.meta_path.insert(0, DenyExecution())
from phase1_agent.legacy_archive import read_closed_legacy_archive

def never_process(*args, **kwargs):
    raise AssertionError("Archive validation reran macro or context regex")

if sys.argv[1] != "basic":
    from phase1_agent import prompt_preparation
    prompt_preparation.process_prompt_collection = never_process
    prompt_preparation.apply_context_regex = never_process
records = json.loads(sys.stdin.read())

class ReadOnlyRecords:
    def get_record(self, kind, identity):
        return records.get(kind)

archive = read_closed_legacy_archive(ReadOnlyRecords(), records["turn"]["turn_id"])
assert archive["turn"] == records["turn"]
assert archive["snapshot"] == records["input_snapshot"]
assert not blocked.intersection(sys.modules)
print("closed legacy archive read without execution imports or writes")
'''
    records = archive_records(family)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", program, family], input=json.dumps(records), text=True,
        capture_output=True, env=environment, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "closed legacy archive read without execution imports or writes"
