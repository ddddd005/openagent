"""A completed reroll starts from the original frozen A input, never live heads."""

import copy
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.reroll_preparation import inspect_completed_reroll_origin
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import WorkflowService


@pytest.fixture
def completed():
    with TemporaryDirectory(prefix="reroll-preparation-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(database)) as service:
            sid = service.create_session()["workflow_session_id"]
            chain_id = service.submit(sid, "Keep the first frozen prompt", "submit")["chain_run_id"]
            service.wait_for_idle(sid)
            with closing(SqliteStore(database)) as store:
                yield service, store.read_bundle(), sid, chain_id


def test_completed_origin_clones_input_and_entire_frozen_s0_without_mutating_source(completed):
    _, bundle, sid, chain_id = completed
    unchanged = copy.deepcopy(bundle)
    origin = inspect_completed_reroll_origin(bundle, sid, chain_id)
    new_input_id, new_snapshot_id = str(uuid4()), str(uuid4())
    node_input, snapshot = origin.clone_frozen_start(
        input_id=new_input_id, snapshot_id=new_snapshot_id,
        created_at="2026-09-29T00:00:00.000Z",
    )

    assert origin.workflow_session_id == sid
    assert origin.source_chain_run_id == chain_id
    assert origin.base_commit_id != bundle["workflow_ref"][0]["head_commit_id"]
    assert node_input["input_id"] == snapshot["input_id"] == new_input_id
    assert snapshot["snapshot_id"] == new_snapshot_id
    assert node_input["source"] == origin.node_input["source"]
    for field in ("port_id", "payload_schema_ref", "payload"):
        assert canonical_bytes(node_input[field]) == canonical_bytes(origin.node_input[field])
    for field in (
        "parent_turn_id", "component_id", "component_version", "config", "s0",
        "tool_definitions", "model_parameters", "output_schema", "projection_version",
    ):
        assert canonical_bytes(snapshot[field]) == canonical_bytes(origin.input_snapshot[field])
    node_input["payload"]["text"] = "changed only in the clone"
    snapshot["s0"].clear()
    assert bundle == unchanged
    assert origin.node_input == unchanged["node_input"][0]
    assert origin.input_snapshot == unchanged["input_snapshot"][0]


def test_origin_rejects_stale_chain_and_unsettled_delivery(completed):
    service, bundle, sid, first_chain = completed
    service.submit(sid, "Another completed chain", "second")
    service.wait_for_idle(sid)
    with closing(SqliteStore(service.database)) as store:
        later = store.read_bundle()
    with pytest.raises(ContractValidationError, match="selected Head result"):
        inspect_completed_reroll_origin(later, sid, first_chain)

    unsettled = copy.deepcopy(bundle)
    unsettled["output_delivery"][0]["status"] = "pending"
    with pytest.raises(ContractValidationError, match="unsettled delivery"):
        inspect_completed_reroll_origin(unsettled, sid, first_chain)


def test_origin_rejects_head_that_does_not_select_source(completed):
    _, bundle, sid, chain_id = completed
    head_elsewhere = copy.deepcopy(bundle)
    head_elsewhere["workflow_ref"][0]["head_commit_id"] = head_elsewhere["chain_run"][0]["base_commit_id"]
    with pytest.raises(ContractValidationError, match="selected Head result"):
        inspect_completed_reroll_origin(head_elsewhere, sid, chain_id)


def test_origin_requires_exact_completed_assistant_checkpoint(completed):
    _, bundle, sid, chain_id = completed
    missing = copy.deepcopy(bundle)
    assistant = next(ref for ref in missing["visible_message_ref"] if ref["role"] == "assistant")
    assistant["boundary"]["after_checkpoint_id"] = None
    with pytest.raises(ContractValidationError, match="terminal formal reply"):
        inspect_completed_reroll_origin(missing, sid, chain_id)

    different = copy.deepcopy(bundle)
    checkpoint = copy.deepcopy(different["workflow_checkpoint"][-1])
    checkpoint["checkpoint_id"] = str(uuid4())
    different["workflow_checkpoint"].append(checkpoint)
    assistant = next(ref for ref in different["visible_message_ref"] if ref["role"] == "assistant")
    assistant["boundary"]["after_checkpoint_id"] = checkpoint["checkpoint_id"]
    with pytest.raises(ContractValidationError, match="terminal formal reply"):
        inspect_completed_reroll_origin(different, sid, chain_id)


@pytest.mark.parametrize("delivery", ["failed", "missing"])
def test_origin_requires_successful_delivery_for_selected_output(completed, delivery):
    _, bundle, sid, chain_id = completed
    unavailable = copy.deepcopy(bundle)
    if delivery == "failed":
        unavailable["output_delivery"][0]["status"] = "failed"
    else:
        unavailable["output_delivery"].clear()
    with pytest.raises(ContractValidationError, match="successful UI delivery"):
        inspect_completed_reroll_origin(unavailable, sid, chain_id)
