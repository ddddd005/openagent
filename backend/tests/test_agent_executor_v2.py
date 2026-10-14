"""Explicit, uninstalled executor 2 and original accepted fact boundaries."""

from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from phase1_agent.agent_executor import PublicAgentExecutor, make_public_agent_snapshot
from phase1_agent.agent_receipt_contract import (
    AGENT_EXECUTOR_REF, receipts_references, validate_agent_receipts,
)
from phase1_agent.content_contracts import text_content
from phase1_agent.context_prompt_v6 import assemble_native_context_prompt
from phase1_agent.context_v4 import validate_native_context_view_v5
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.model_execution_contracts import AGENT_EXECUTOR_REF_V2
from phase1_agent.tools import final_answer_tool
from test_model_request_integration import IntegratedHost
from test_prepared_request_gate import public_snapshot


class Callbacks:
    def __init__(self, *, drop_kind=None):
        self.events, self.calls, self.progress = {}, [], []
        self.drop_kind = drop_kind
        self.pause_requested = False

    def publish_fact(self, identity, event):
        original = self.events.get(identity)
        assert original is None or original == event
        self.events[identity] = deepcopy(event)
        self.calls.append((identity, deepcopy(event)))
        if event["kind"] == self.drop_kind:
            self.drop_kind = None
            raise OSError("Synthetic original fact acknowledgment loss")

    def report_progress(self, event):
        self.progress.append(deepcopy(event))

    def pause_point(self, checkpoint):
        raise AssertionError("Unexpected pause")


class ExecutorHarness:
    def __init__(self, directory):
        self.host = IntegratedHost(directory)
        self.mock = self.host.mock
        owner = {field: getattr(self.mock.context, field) for field in (
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")}
        self.context = SimpleNamespace(**owner)
        view = validate_native_context_view_v5({
            "schema_version": 5, "kind": "workflow.context-view",
            "owner": {"workflow_session_id": owner["workflow_session_id"],
                      "object_key": "context"},
            "basis": {"revision_id": str(uuid4()), "head_revision": 1},
            "messages": [], "layout": [],
            "accepted_delta_ids": [], "applied_delta_ids": [],
            "generation": 0,
            "derivation": {"operation": "read", "input_view_ref": None, "receipts_ref": None},
        })
        self.prompt = assemble_native_context_prompt(
            [], view, text_content("Current task"),
            context_ref={"scope": "artifact", "output_id": str(uuid4())},
            current_input_ref={"scope": "artifact", "output_id": str(uuid4())})
        self.inputs = {"prompt": self.prompt, "model": self.mock.binding}
        self.references = {
            "prompt": [{"output_id": str(uuid4())}],
            "model": deepcopy(self.mock.input_refs["model"]),
        }
        self.context.input_artifact_refs = lambda port: deepcopy(self.references[port])
        self.context.host_call = self.host_call
        self.handles = []
        self.mock.response = ModelResponse("tool_calls", tool_calls=(
            ModelToolCall("provider-final", "final_answer",
                          json.dumps({"answer": {"text": "Accepted answer"}})),))

    def host_call(self, capability, operation, payload):
        if (capability, operation) == ("artifacts:read", "resolve-input"):
            port = payload["port"]
            return {"value": deepcopy(self.inputs[port]), "producer": {},
                    "output_id": self.references[port][0]["output_id"]}
        return self.mock.service(self.mock.context, capability, operation, payload)

    def executor(self, **options):
        handle = PublicAgentExecutor(
            {}, self.inputs, self.context, schema_version=2, **options)
        self.handles.append(handle)
        return handle

    def close(self):
        for handle in self.handles:
            handle.dispose()
        self.mock.close()


@pytest.fixture
def executor_host(tmp_path):
    host = ExecutorHarness(tmp_path)
    yield host
    host.close()


def schema2_snapshot(host):
    prompt = {field: deepcopy(host.prompt[field]) for field in (
        "messages", "layout", "current_input_ref")}
    return make_public_agent_snapshot(
        host.context, {}, prompt, host.mock.binding, (final_answer_tool(),), schema_version=2)


def schema2_receipts(host):
    return {
        "schema_version": 2, "kind": "workflow.agent-receipts",
        "owner": {field: getattr(host.context, field) for field in (
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")},
        "executor_ref": deepcopy(AGENT_EXECUTOR_REF_V2),
        "snapshot_id": str(uuid4()), "unit_id": str(uuid4()),
        "frozen_prompt_ref": {"scope": "artifact", "output_id": host.references["prompt"][0]["output_id"]},
        "model_ref": {"scope": "artifact", "output_id": host.references["model"][0]["output_id"]},
        "compaction_policy_ref": None,
        "fact_ids": [str(uuid4())],
        "model_requests": [{"request_id": str(uuid4()), "settings_id": str(uuid4()),
                            "fact_ids": [str(uuid4()) for _ in range(3)]}],
    }


def test_generic_snapshot_dispatch_preserves_both_versions_and_detaches_values(executor_host):
    host = executor_host
    snapshot = schema2_snapshot(host)
    checked = validate_record("input_snapshot", snapshot)
    assert checked == snapshot and checked is not snapshot
    assert "model_parameters" not in checked
    assert set(checked["config"]["payload"]["context_compaction"]) == {"policy", "layout"}
    checked["config"]["payload"]["context_compaction"]["layout"].clear()
    assert snapshot["config"]["payload"]["context_compaction"]["layout"] == host.prompt["layout"]
    legacy, _ = public_snapshot()
    assert validate_record("input_snapshot", legacy) == legacy
    assert legacy["schema_version"] == 1 and "model_parameters" in legacy


@pytest.mark.parametrize("field", ["model_parameters", "transport_projection", "capacity", "executor_ref"])
def test_generic_snapshot2_rejects_whole_run_or_wrong_executor_fields(executor_host, field):
    snapshot = schema2_snapshot(executor_host)
    payload = snapshot["config"]["payload"]
    if field == "model_parameters":
        snapshot[field] = {"max_tokens": 32}
    elif field == "capacity":
        payload["context_compaction"]["context_window_tokens"] = 4096
    elif field == "executor_ref":
        payload[field] = AGENT_EXECUTOR_REF.to_dict()
    else:
        payload[field] = []
    with pytest.raises(ContractValidationError):
        validate_record("input_snapshot", snapshot)


def test_generic_receipts_dispatch_retains_v2_requests_and_v1_regression(executor_host):
    receipts = schema2_receipts(executor_host)
    checked = validate_agent_receipts(receipts)
    assert checked == receipts and checked is not receipts
    checked["model_requests"][0]["fact_ids"].clear()
    assert len(receipts["model_requests"][0]["fact_ids"]) == 3
    assert receipts_references(receipts) == [receipts["frozen_prompt_ref"], receipts["model_ref"]]
    legacy = deepcopy(receipts)
    legacy.pop("model_requests")
    legacy.pop("compaction_policy_ref")
    legacy.update(schema_version=1, executor_ref=AGENT_EXECUTOR_REF.to_dict())
    assert validate_agent_receipts(legacy) == legacy


@pytest.mark.parametrize("fault", ["missing_requests", "missing_stage", "wrong_executor", "version3", "duplicate"])
def test_generic_receipts_rejects_incomplete_or_wrong_identity_evidence(executor_host, fault):
    receipts = schema2_receipts(executor_host)
    if fault == "missing_requests":
        receipts.pop("model_requests")
    elif fault == "missing_stage":
        receipts["model_requests"][0]["fact_ids"].pop()
    elif fault == "wrong_executor":
        receipts["executor_ref"] = AGENT_EXECUTOR_REF.to_dict()
    elif fault == "version3":
        receipts["schema_version"] = 3
    else:
        receipts["model_requests"].append(deepcopy(receipts["model_requests"][0]))
    with pytest.raises(ContractValidationError):
        validate_agent_receipts(receipts)


def test_executor_is_schema2_only_and_requires_the_real_host_bridge(executor_host):
    host = executor_host
    with pytest.raises(ContractValidationError):
        PublicAgentExecutor({}, host.inputs, host.context, schema_version=1)
    with pytest.raises(ContractValidationError):
        PublicAgentExecutor({}, host.inputs, host.context, schema_version=True)
    assert host.mock.communications == [] and host.host.read_count == 0


def test_executor2_rejects_changed_accepted_prompt_before_any_request(executor_host):
    host = executor_host
    changed = deepcopy(host.inputs)
    changed["prompt"]["messages"][-1]["blocks"][0]["text"] = "Changed immutable task"
    changed["prompt"]["current_input"]["blocks"][0]["text"] = "Changed immutable task"
    with pytest.raises(ContractValidationError) as error:
        PublicAgentExecutor({}, changed, host.context, schema_version=2)
    assert error.value.reason_code == "agent_input_mismatch"
    assert host.mock.communications == [] and host.host.read_count == 0


def test_original_fact_retry_reuses_timestamp_and_rejects_changed_body(executor_host):
    handle, callbacks = executor_host.executor(), Callbacks(drop_kind="model_request")
    identity, event = str(uuid4()), {"kind": "model_request", "payload": {"original": True}}
    with pytest.raises(OSError):
        handle._publish_original(identity, event, callbacks)
    assert handle.fact_ids == []
    original = deepcopy(callbacks.calls[-1])
    handle._publish_original(identity, event, callbacks)
    handle._publish_original(identity, event, callbacks)
    assert callbacks.calls[-1] == callbacks.calls[-2] == original
    assert handle.fact_ids == [identity]
    with pytest.raises(ContractValidationError) as error:
        handle._publish_original(identity, {**event, "payload": {"original": False}}, callbacks)
    assert error.value.reason_code == "agent_fact_conflict" and len(callbacks.calls) == 3


def test_executor2_dispose_does_not_release_the_shared_chain_model_service(executor_host):
    host = executor_host
    handle = host.executor()
    handle.advance(Callbacks(), None)
    handle.dispose()
    assert handle.disposed and handle.snapshot is None
    assert len(host.mock.communications) == 1

    later = host.executor()
    later.advance(Callbacks(), None)
    later.dispose()
    assert len(host.mock.communications) == 2
