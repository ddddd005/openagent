"""Frozen graph Agent archives validate without any execution implementation."""

from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_graph import validate_bundle
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.graph_agent_contracts import (
    GRAPH_AGENT_COMPONENT, GRAPH_AGENT_OUTPUT_SCHEMA, GraphAgentIdentity,
    _check_request_capacity, _uuid, _validate_limits, _validated_facts,
    validate_graph_agent_accepted,
)
from phase1_agent.graph_contracts import text_value
from phase1_agent.graph_prompt import archive_materials
from phase1_agent.graph_records import (
    graph_record, validate_graph_bundle, validate_graph_record, validate_graph_transition,
)
from phase1_agent.model_configuration import default_provider
from phase1_agent.prompt_assembly import (
    PromptAssemblyLimits, assemble_prompt_collection, message_content_chars,
)
from phase1_agent.prompt_errors import PromptProcessingError


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contracts_v2" / "success.json"


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def accepted_package(*, frozen_requests=8, requests=8, attempts=32):
    example = loads_strict(EXAMPLE.read_text(encoding="utf-8"))
    snapshot, turn = example["input_snapshot"][0], example["turn"][0]
    identity = GraphAgentIdentity(uid(4), uid(3), uid(8), uid(7), uid(30))
    config = {"max_model_requests": frozen_requests, "max_model_attempts": frozen_requests * 4}
    parameters = {"model": "deepseek-flash", "max_tokens": 2048, "thinking": "disabled", "stream": False}
    assembly = assemble_prompt_collection(
        {"schema_version": 1, "kind": "prompt_collection", "items": []}, snapshot["s0"],
        logical_floors=[[snapshot["s0"][0]["message_id"]]], prompt_message_ids=[],
    )
    preparation = {"schema_version": 1, "kind": "workflow.prompt-assembly", "prompt": assembly,
                   "current_root": deepcopy(snapshot["s0"][0]), "context_basis": []}
    snapshot.update(
        component_id=GRAPH_AGENT_COMPONENT, component_version="graph-2",
        model_parameters=parameters, output_schema=deepcopy(GRAPH_AGENT_OUTPUT_SCHEMA),
        config={"owner_component_id": GRAPH_AGENT_COMPONENT, "schema_version": 1, "payload": {
            "graph_agent": config, "graph_identity": asdict(identity), "graph_preparation": preparation,
            "model_binding": {"node_id": uid(40), "provider": default_provider(),
                              "credential_evidence": "1" * 64, "parameters": deepcopy(parameters)},
        }},
    )
    facts, messages = [], []
    for index in range(2):
        assistant, result = turn["messages"][index * 2:index * 2 + 2]
        request_id = assistant["source"]["request_id"]
        attempt_id, block = uid(2000 + index), result["blocks"][0]
        steps = [
            ("model_request", {
                "request_id": request_id, "request_index": index + 1,
                "messages": deepcopy(snapshot["s0"] + messages),
                "tools": [{"type": "function", "function": {
                    "name": item["name"], "parameters": deepcopy(item["parameters_schema"]),
                }} for item in snapshot["tool_definitions"]],
                "model_parameters": deepcopy(parameters),
            }),
            ("model_attempt_started", {
                "request_id": request_id, "attempt_id": attempt_id,
                "request_index": index + 1, "attempt_index": index + 1, "retry_index": 0,
            }),
            ("model_attempt_finished", {
                "request_id": request_id, "attempt_id": attempt_id, "outcome": "responded",
                "usage": None, "response_id": None, "model": None,
            }),
            ("message_accepted", {"message": deepcopy(assistant)}),
            ("tool_dispatch", {
                "tool_call_id": block["tool_call_id"], "tool_execution_id": block["tool_execution_id"],
            }),
            ("tool_settled", {
                "tool_call_id": block["tool_call_id"], "tool_execution_id": block["tool_execution_id"],
                "outcome": "success", "message": deepcopy(result),
            }),
            ("message_accepted", {"message": deepcopy(result)}),
        ]
        for kind, payload in steps:
            facts.append({
                "schema_version": 1, "fact_id": uid(1000 + len(facts)),
                "run_id": identity.node_run_id, "chain_run_id": identity.chain_run_id,
                "workflow_session_id": identity.workflow_session_id, "node_binding_id": identity.node_binding_id,
                "snapshot_id": snapshot["snapshot_id"], "generation": uid(900), "sequence": len(facts) + 1,
                "created_at": "2026-09-29T00:00:00.000Z", "kind": kind, "payload": payload,
            })
        messages.extend((assistant, result))
    return {
        "schema_version": 1, "kind": "workflow.agent-accepted", "identity": asdict(identity),
        "snapshot": snapshot, "turn": turn, "facts": facts,
        "limits": {"max_model_requests": requests, "max_model_attempts": attempts},
        "progress": {"model_requests": 2, "attempts": 2, "accepted_messages": 4},
    }


def node_run(package, *, version=4, paused=False):
    snapshot = package["snapshot"]
    value = graph_record(
        "node_run", profile="node", run_id=uid(7), workflow_session_id=uid(4),
        node_binding_id=uid(3), chain_run_id=uid(8), status="paused" if paused else "succeeded", revision=3,
        input_refs={}, input_values={"prompt": {"assembly": deepcopy(snapshot["config"]["payload"]["graph_preparation"])}},
        output_refs={}, reads=[], effects=[], config=deepcopy(snapshot["config"]["payload"]["graph_agent"]),
        diagnostic=None, agent={
            "snapshot": deepcopy(snapshot), "facts": deepcopy(package["facts"][:7] if paused else package["facts"]),
            "progress": {"model_requests": 1, "attempts": 1, "accepted_messages": 2} if paused else deepcopy(package["progress"]),
            "limits": deepcopy(package["limits"]), "accepted": None if paused else deepcopy(package),
        },
    )
    value["schema_version"] = version
    if version < 4:
        value.pop("input_storage")
        value.pop("control_refs")
    if version == 2:
        value.pop("agent")
        value["input_values"] = {}
    return value


def archive_bundle(package, *, version=4):
    run = node_run(package, version=version)
    node_id, sid, definition_id = uid(3), uid(4), uid(2)
    node = {"node_binding_id": node_id, "component_id": "workflow.agent", "component_version": "2",
            "title": "Frozen Agent", "position": {"x": 0, "y": 0}, "config": deepcopy(run["config"]),
            "public_outputs": ["output"]}
    lock = [{"package_id": "workflow.compat", "version": "1.0.0"}]
    document = {"schema_version": 2, "workflow_definition_id": definition_id, "revision": 1,
                "name": "Frozen archive", "nodes": [node], "edges": [], "object_bindings": [], "package_lock": lock}
    outputs = {
        "output": text_value(package["turn"]["final"]["value"]["text"]),
        "output_json": text_value(json.dumps(package["turn"]["final"]["value"])),
        "context_delta": archive_materials([{"turn": package["turn"], "root": package["snapshot"]["s0"]}]),
    }
    run["output_refs"] = {port: uid(50 + index) for index, port in enumerate(outputs)}
    return {
        "workflow_definition_revision": [graph_record(
            "workflow_definition_revision", workflow_definition_id=definition_id, revision=1,
            bindings=[node_id], edges=[], document=document)],
        "node_binding": [graph_record(
            "node_binding", workflow_definition_id=definition_id, workflow_definition_revision=1,
            node_binding_id=node_id, component_id=node["component_id"], component_version="2", config=deepcopy(run["config"]))],
        "workflow_session": [graph_record(
            "workflow_session", workflow_session_id=sid, workflow_definition_id=definition_id, definition_revision=1,
            revision=1, source={"kind": "new"}, active_chain_run_id=None)],
        "state_snapshot": [graph_record(
            "state_snapshot", state_snapshot_id=uid(31), workflow_session_id=sid,
            workflow_definition_id=definition_id, definition_revision=1,
            node_states={}, data_revision=0, history_refs=[])],
        "workflow_commit": [graph_record(
            "workflow_commit", commit_id=uid(30), workflow_session_id=sid,
            state_snapshot_id=uid(31), parent_commit_id=None, source={"kind": "initial"})],
        "workflow_ref": [graph_record(
            "workflow_ref", workflow_ref_id=uid(32), workflow_session_id=sid, head_commit_id=uid(30), revision=1)],
        "node_run": [run],
        "chain_run": [graph_record(
            "chain_run", chain_run_id=uid(8), workflow_session_id=sid, workflow_definition_id=definition_id,
            definition_revision=1, status="succeeded", revision=3, targets=[node_id], ordered_nodes=[node_id],
            node_run_ids=[uid(7)], completed_nodes=[node_id], next_node_index=1,
            inputs={}, outputs={node_id: deepcopy(run["output_refs"])}, diagnostic=None)],
        "workflow_output": [graph_record(
            "workflow_output", output_id=run["output_refs"][port], workflow_session_id=sid, chain_run_id=uid(8),
            run_id=uid(7), node_binding_id=node_id, port_id=port, payload=payload) for port, payload in outputs.items()],
    }


def replace_at(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = deepcopy(replacement)


def test_accepted_archive_is_exact_detached_and_keeps_unknown_usage():
    package = accepted_package()
    original = canonical_bytes(package)
    validated = validate_graph_agent_accepted(package)
    assert validated == package and validated is not package
    assert all(fact["payload"]["usage"] is None for fact in validated["facts"]
               if fact["kind"] == "model_attempt_finished")
    validated["turn"]["final"]["value"]["text"] = "changed caller copy"
    validated["facts"][0]["payload"]["messages"][0]["blocks"][0]["text"] = "changed caller fact"
    assert canonical_bytes(package) == original


def test_runtime_reexports_the_exact_shared_contract_objects():
    from phase1_agent import graph_agent_contracts, graph_agent_runtime

    for name in (
        "GRAPH_AGENT_COMPONENT", "GRAPH_AGENT_OUTPUT_SCHEMA", "GraphAgentIdentity",
        "_validated_facts", "_validate_limits", "_check_request_capacity",
        "validate_graph_agent_accepted", "_uuid", "_require",
    ):
        assert getattr(graph_agent_runtime, name) is getattr(graph_agent_contracts, name), name


@pytest.mark.parametrize("length", range(15))
def test_fact_validator_accepts_honestly_unfinished_tails_without_inventing_results(length):
    package = accepted_package()
    facts = deepcopy(package["facts"][:length])
    original = canonical_bytes(facts)
    validated = _validated_facts(facts, package["snapshot"], GraphAgentIdentity(**package["identity"]))
    assert validated == facts
    if validated:
        validated[0]["payload"]["messages"][0]["blocks"][0]["text"] = "detached"
    assert canonical_bytes(facts) == original


def test_limits_and_identity_are_detached_and_canonical():
    limits = {"max_model_requests": 64, "max_model_attempts": 256}
    validated = _validate_limits(limits)
    validated["max_model_requests"] = 1
    assert limits["max_model_requests"] == 64
    identity = GraphAgentIdentity(*(uid(number) for number in range(5)))
    assert asdict(identity) == dict(zip(
        ("workflow_session_id", "node_binding_id", "chain_run_id", "node_run_id", "base_commit_id"),
        (uid(number) for number in range(5)),
    ))
    assert _uuid(uid(1)) is None


@pytest.mark.parametrize("value", [None, True, 1, "", "invalid", uid(1000).upper(),
                                  uid(1).replace("-4000-", "-1000-"), uid(1) + "\n"])
def test_uuid_validator_preserves_strict_uuid4_spelling(value):
    with pytest.raises(ContractValidationError, match="canonical UUID4"):
        _uuid(value)
    with pytest.raises(ContractValidationError, match="canonical UUID4"):
        GraphAgentIdentity(value, uid(2), uid(3), uid(4), uid(5))


@pytest.mark.parametrize("field,value", [
    ("max_model_requests", True), ("max_model_requests", 0), ("max_model_requests", 65),
    ("max_model_requests", 1.0), ("max_model_requests", "1"), ("max_model_requests", None),
    ("max_model_attempts", False), ("max_model_attempts", 0), ("max_model_attempts", 257),
    ("max_model_attempts", -1), ("max_model_attempts", 1.0),
])
def test_limits_reject_inexact_or_out_of_contract_budgets(field, value):
    limits = {"max_model_requests": 8, "max_model_attempts": 32}
    limits[field] = value
    with pytest.raises(ContractValidationError, match="execution budget"):
        _validate_limits(limits)


@pytest.mark.parametrize("value", [None, [], {}, {"max_model_requests": 8},
                                  {"max_model_requests": 8, "max_model_attempts": 32, "extra": 1}])
def test_limits_reject_missing_or_extra_fields(value):
    with pytest.raises(ContractValidationError, match="execution budget"):
        _validate_limits(value)


@pytest.mark.parametrize("path,replacement", [
    (("schema_version",), True), (("kind",), "workflow.other"),
    (("identity", "node_run_id"), uid(999)),
    (("snapshot", "component_id"), uid(999)), (("snapshot", "component_version"), "graph-3"),
    (("snapshot", "config", "owner_component_id"), uid(999)),
    (("snapshot", "config", "payload", "graph_identity", "chain_run_id"), uid(999)),
    (("snapshot", "model_parameters", "model"), "changed"),
    (("snapshot", "output_schema", "properties", "text", "minLength"), 0),
    (("turn", "run_id"), uid(999)), (("turn", "snapshot_id"), uid(999)),
    (("turn", "input_id"), uid(999)), (("turn", "parent_turn_id"), uid(999)),
    (("turn", "projection_version"), 2), (("turn", "final", "value", "text"), "forged"),
    (("facts", 0, "sequence"), 2), (("facts", 0, "run_id"), uid(999)),
    (("facts", 0, "chain_run_id"), uid(999)), (("facts", 0, "workflow_session_id"), uid(999)),
    (("facts", 0, "node_binding_id"), uid(999)), (("facts", 0, "snapshot_id"), uid(999)),
    (("facts", 0, "payload", "messages", 0, "blocks", 0, "text"), "forged S0"),
    (("facts", 0, "payload", "tools"), []),
    (("facts", 0, "payload", "model_parameters", "model"), "changed"),
    (("facts", 4, "payload", "tool_execution_id"), uid(999)),
    (("progress", "model_requests"), True), (("progress", "accepted_messages"), 3),
    (("limits", "max_model_requests"), 1), (("limits", "max_model_attempts"), 1),
])
def test_accepted_archive_rejects_owner_contract_causality_and_result_tampering(path, replacement):
    package = accepted_package()
    replace_at(package, path, replacement)
    with pytest.raises(ContractValidationError):
        validate_graph_agent_accepted(package)


def test_accepted_archive_rejects_extra_fields_and_missing_attempt_completion():
    package = accepted_package()
    package["extra"] = None
    with pytest.raises(ContractValidationError):
        validate_graph_agent_accepted(package)
    package = accepted_package()
    package["facts"] = package["facts"][:9]
    with pytest.raises(ContractValidationError):
        validate_graph_agent_accepted(package)


def test_extended_limits_do_not_rewrite_frozen_initial_config():
    package = accepted_package(frozen_requests=1, requests=2, attempts=8)
    assert validate_graph_agent_accepted(package) == package
    assert validate_graph_record("node_run", node_run(package))["config"] == {
        "max_model_requests": 1, "max_model_attempts": 4,
    }
    assert package["limits"] == {"max_model_requests": 2, "max_model_attempts": 8}


@pytest.mark.parametrize("capacity", ["messages", "characters"])
def test_request_capacity_includes_incremental_messages_and_frozen_tools(capacity):
    package = accepted_package()
    snapshot = package["snapshot"]
    limits = (PromptAssemblyLimits(max_messages=1) if capacity == "messages" else
              PromptAssemblyLimits(max_total_chars=message_content_chars(snapshot["s0"])
                                   + len(canonical_bytes(snapshot["tool_definitions"]).decode("utf-8")) - 1))
    snapshot["config"]["payload"]["graph_preparation"]["prompt"] = assemble_prompt_collection(
        {"schema_version": 1, "kind": "prompt_collection", "items": []}, snapshot["s0"],
        logical_floors=[[snapshot["s0"][0]["message_id"]]], prompt_message_ids=[], limits=limits,
    )
    with pytest.raises(PromptProcessingError) as caught:
        if capacity == "messages":
            _validated_facts(package["facts"], snapshot, GraphAgentIdentity(**package["identity"]))
        else:
            _check_request_capacity(snapshot, snapshot["s0"])
    assert caught.value.code == "prompt_capacity_exceeded"


@pytest.mark.parametrize("version", [2, 3, 4])
def test_historical_node_run_schemas_preserve_their_original_shape(version):
    package = accepted_package()
    record = node_run(package, version=version)
    original = canonical_bytes(record)
    validated = validate_graph_record("node_run", record)
    assert canonical_bytes(validated) == original
    validated["config"]["max_model_requests"] = 1
    assert canonical_bytes(record) == original
    assert ("agent" in record) == (version >= 3)
    assert ("input_storage" in record) == (version == 4)
    assert ("control_refs" in record) == (version == 4)
    bundle = archive_bundle(package, version=version)
    assert validate_bundle(bundle) == bundle


@pytest.mark.parametrize("version", [3, 4])
def test_paused_record_keeps_partial_facts_without_accepted_turn(version):
    record = node_run(accepted_package(), version=version, paused=True)
    assert validate_graph_record("node_run", record) == record
    assert record["agent"]["accepted"] is None
    assert len(record["agent"]["facts"]) == 7


@pytest.mark.parametrize("path,replacement", [
    (("workflow_session_id",), uid(999)), (("node_binding_id",), uid(999)),
    (("chain_run_id",), uid(999)), (("run_id",), uid(999)),
    (("config", "max_model_requests"), 4),
    (("input_values", "prompt", "assembly", "kind"), "changed"),
    (("agent", "accepted", "snapshot", "s0", 0, "blocks", 0, "text"), "changed"),
])
def test_node_record_keeps_snapshot_input_and_accepted_ownership_checks(path, replacement):
    record = node_run(accepted_package())
    replace_at(record, path, replacement)
    with pytest.raises(ContractValidationError):
        validate_graph_record("node_run", record)


def test_bundle_keeps_original_baseline_commit_ownership():
    bundle = archive_bundle(accepted_package())
    foreign = deepcopy(bundle["workflow_session"][0])
    foreign["workflow_session_id"] = uid(999)
    bundle["workflow_session"].append(foreign)
    bundle["workflow_commit"][0]["workflow_session_id"] = uid(999)
    bundle["state_snapshot"][0]["workflow_session_id"] = uid(999)
    with pytest.raises(ContractValidationError, match="Agent baseline commit"):
        validate_graph_bundle(bundle)


def test_frozen_transition_accepts_append_only_facts_and_preserves_snapshot():
    previous = node_run(accepted_package(), paused=True)
    previous["status"] = "running"
    current = deepcopy(previous)
    current.update(status="paused", revision=previous["revision"] + 1)
    current["agent"]["facts"].append(deepcopy(accepted_package()["facts"][7]))
    assert validate_graph_record("node_run", current) == current
    validate_graph_transition("node_run", previous, current)


@pytest.mark.parametrize("change", ["snapshot", "fact-prefix", "accepted"])
def test_frozen_transition_rejects_rewritten_snapshot_prior_facts_or_accepted_package(change):
    previous = node_run(accepted_package(), paused=change != "accepted")
    previous["status"] = "running"
    current = deepcopy(previous)
    current.update(status="succeeded" if change == "accepted" else "paused", revision=previous["revision"] + 1)
    if change == "snapshot":
        current["agent"]["snapshot"]["snapshot_id"] = uid(999)
        for fact in current["agent"]["facts"]:
            fact["snapshot_id"] = uid(999)
    elif change == "fact-prefix":
        current["agent"]["facts"][0]["fact_id"] = uid(999)
    else:
        current["agent"]["accepted"]["turn"]["turn_id"] = uid(999)
    assert validate_graph_record("node_run", current) == current
    with pytest.raises(ContractValidationError, match="Frozen Agent evidence"):
        validate_graph_transition("node_run", previous, current)


@pytest.mark.parametrize("version", [2, 3, 4])
def test_fresh_process_reads_and_reopens_archives_with_execution_modules_blocked(tmp_path, version):
    bundle = archive_bundle(accepted_package(frozen_requests=1, requests=2, attempts=8), version=version)
    program = r"""
import importlib.abc
import json
import sys
from contextlib import closing

blocked = (
    "phase1_agent.graph_agent_runtime", "phase1_agent.graph_agent_host",
    "phase1_agent.adapter", "phase1_agent.prepared_context", "phase1_agent.workflow",
    "phase1_agent.runtime",
)
class NoExecutionImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Archive validation imported execution module: " + fullname)
sys.meta_path.insert(0, NoExecutionImports())

from phase1_agent.contract_graph import validate_bundle
from phase1_agent.graph_records import validate_graph_record, validate_graph_bundle
from phase1_agent.graph_store import GraphRecordStore
from phase1_agent.storage import SqliteStore

bundle = json.load(sys.stdin)
for kind, records in bundle.items():
    for record in records:
        assert validate_graph_record(kind, record) == record
validate_graph_bundle(bundle)
assert validate_bundle(bundle) == bundle
with closing(SqliteStore(sys.argv[1])) as store:
    repository = GraphRecordStore(store)
    def seed(repo):
        for kind, records in bundle.items():
            for record in records:
                repo.put(kind, record)
        return {"saved": True}
    assert repository.atomic("test.archive.seed", "original-receipt", {"version": 1}, seed) == {"saved": True}
    original = store.read_bundle(include_graph=True)
    assert validate_bundle(original) == original
with closing(SqliteStore(sys.argv[1])) as store:
    restored = store.read_bundle(include_graph=True)
    assert restored == original
    assert validate_bundle(restored) == restored
    assert GraphRecordStore(store).rows("node_run") == bundle["node_run"]
    def never_replay(repo):
        raise AssertionError("An original archive receipt replayed its writes")
    assert GraphRecordStore(store).atomic(
        "test.archive.seed", "original-receipt", {"version": 1}, never_replay) == {"saved": True}
    assert store.read_bundle(include_graph=True) == original
assert not any(name == entry or name.startswith(entry + ".") for name in sys.modules for entry in blocked)
print("frozen archive read/reopen and original receipt preserved")
"""
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [source, environment.get("PYTHONPATH", "")]))
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / f"archive-v{version}.sqlite")],
        input=json.dumps(bundle), text=True, capture_output=True, env=environment, timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "frozen archive read/reopen and original receipt preserved"
