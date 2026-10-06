"""Event view schemas remain distinct from durable records and source payloads."""

from copy import deepcopy
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from jsonschema import Draft202012Validator
import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import loads_strict
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.event_contracts import (
    EVENT_STREAM_SCHEMAS, core_payload_schema_ref, validate_event_projection,
)


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def projection():
    return {
        "schema_version": 1, "kind": "run_event_projection", "source_schema_version": 2,
        "event_id": uid(1), "workflow_session_id": uid(2), "node_binding_id": uid(3),
        "run_id": uid(4), "sequence": 7, "event_type": "run_state",
        "source_payload_schema_ref": core_payload_schema_ref("run_state"),
        "payload": {"status": "paused"}, "visibility": "business_candidate", "generation": uid(5),
        "projection_ref": "workflow-business", "projection_revision": "a" * 64,
    }


def test_projected_view_preserves_source_identity_without_impersonating_exact_source_payload():
    candidate = projection()
    assert validate_event_projection(candidate) == candidate
    assert Draft202012Validator(EVENT_STREAM_SCHEMAS["run_event_projection"]).is_valid(candidate)
    assert "payload_schema_ref" not in candidate
    with pytest.raises(ContractValidationError):
        validate_record("run_event", candidate)
    source_shape = deepcopy(candidate)
    for field in ("kind", "source_schema_version", "projection_ref", "projection_revision"):
        source_shape.pop(field)
    source_shape["schema_version"] = 2
    source_shape["payload_schema_ref"] = source_shape.pop("source_payload_schema_ref")
    with pytest.raises(ContractValidationError):
        validate_record("run_event", source_shape)


@pytest.mark.parametrize("change", [
    lambda p: p.update(payload_schema_ref=core_payload_schema_ref("run_state")),
    lambda p: p.update(projection_revision="1"),
    lambda p: p.update(source_schema_version=1),
    lambda p: p.update(sequence=True),
    lambda p: p.update(group_path=["fake-group"]),
])
def test_projection_shape_rejects_source_masquerade_or_invalid_version(change):
    candidate = projection()
    change(candidate)
    with pytest.raises(ContractValidationError):
        validate_event_projection(candidate)


def test_event_schema_export_matches_runtime_without_expanding_durable_record_registry():
    package = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(dir=package / "tests", prefix="event-export-") as directory:
        output = Path(directory) / "events.json"
        subprocess.run(
            [sys.executable, str(package / "scripts" / "export_event_schemas.py"), "--output", str(output)],
            cwd=package, check=True, capture_output=True, text=True,
        )
        exported = loads_strict(output.read_text(encoding="utf-8"))
        assert exported["$defs"] == EVENT_STREAM_SCHEMAS
        stored = loads_strict((package / "schemas" / "run-events-v1.schema.json").read_text(encoding="utf-8"))
        assert exported == stored
        for schema in EVENT_STREAM_SCHEMAS.values():
            Draft202012Validator.check_schema(schema)
