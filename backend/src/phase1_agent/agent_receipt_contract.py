"""Pure accepted Agent data identities and receipt shape, without a kernel import."""

from copy import deepcopy
from hashlib import sha256
from uuid import UUID

from .context_contract import artifact_ref
from .contract_json import validate_json_value
from .graph_contracts import require, uuid4_string
from .runtime_executor_contracts import ExecutorReference


AGENT_EXECUTOR_REF = ExecutorReference("agents.snapshot-kernel", "1.0.0")


def projection_id(basis: str, part: str) -> str:
    return str(UUID(bytes=sha256((basis + ":" + part).encode("utf-8")).digest()[:16], version=4))


def validate_agent_receipts(value):
    validate_json_value(value)
    require(type(value) is dict and set(value) == {
        "schema_version", "kind", "owner", "executor_ref", "snapshot_id", "frozen_prompt_ref",
        "model_ref", "unit_id", "fact_ids",
    } and type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "workflow.agent-receipts", "agent_invalid_receipts",
        "Agent receipts fields are invalid")
    from .runtime_hosting import InvocationOwner
    InvocationOwner.from_dict(value["owner"])
    require(value["executor_ref"] == AGENT_EXECUTOR_REF.to_dict(),
            "agent_invalid_receipts", "Receipts require the exact registered Agent executor")
    for field in ("snapshot_id", "unit_id"):
        uuid4_string(value[field])
    artifact_ref(value["frozen_prompt_ref"])
    artifact_ref(value["model_ref"])
    ids = value["fact_ids"]
    require(type(ids) is list and 0 < len(ids) <= 4096 and all(type(identity) is str for identity in ids)
            and len(ids) == len(set(ids)),
            "agent_invalid_receipts", "Agent receipts require distinct bounded facts")
    for identity in ids:
        uuid4_string(identity)
    return deepcopy(value)


def receipts_references(value):
    return [deepcopy(value["frozen_prompt_ref"]), deepcopy(value["model_ref"])]
