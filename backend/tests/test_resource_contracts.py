"""Resource validation and current storage do not load legacy resource hosts."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

from jsonschema import SchemaError, ValidationError
import pytest

from phase1_agent import resource_contracts
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.resource_contracts import (
    CORE_SESSION_NOTE, MAX_RESOURCE_BYTES, MAX_SESSION_VALUE_BYTES, require,
    resource_error, resource_id, resource_revision, session_data_entry,
    validate_data_definition, validate_data_value, validate_global_content,
    validate_session_data, workflow_identity,
)

from resource_fixtures import resource


UID = "7be319b8-30bd-4674-b7bf-d1cf54a1a121"


def definition(**changes):
    return {**deepcopy(CORE_SESSION_NOTE), "name": "Session value", **changes}


def test_constants_keep_exact_limits_namespace_pattern_and_core_definition():
    assert MAX_RESOURCE_BYTES == 60 * 1024
    assert MAX_SESSION_VALUE_BYTES == 256 * 1024
    assert resource_contracts._KEY.pattern == r"[A-Za-z][A-Za-z0-9_.-]{0,63}:[A-Za-z][A-Za-z0-9_.-]{0,63}"
    assert CORE_SESSION_NOTE == {
        "schema_version": 1, "definition_id": UID, "revision": 1,
        "key": "core:session_note", "name": "\u4f1a\u8bdd\u6587\u672c",
        "schema": {"type": "string", "maxLength": 65536},
        "writable": True, "public": True, "default": "",
    }


def test_error_helpers_keep_type_reason_status_message_and_success_result():
    failure = resource_error("custom_reason", "Original diagnostic", 409)
    assert type(failure) is ContractValidationError
    assert str(failure) == "Original diagnostic"
    assert failure.reason_code == "custom_reason" and failure.status_code == 409
    assert require(True) is None
    with pytest.raises(ContractValidationError) as caught:
        require(False)
    assert str(caught.value) == "Invalid resource request"
    assert caught.value.reason_code == "invalid_request" and caught.value.status_code == 400


@pytest.mark.parametrize("value", [
    None, True, 7, UUID(UID), "", UID.upper(), UID.replace("-", ""), " " + UID,
    "7be319b8-30bd-1674-b7bf-d1cf54a1a121", "not-a-uuid", {},
])
def test_resource_identity_rejects_noncanonical_or_non_v4_values(value):
    with pytest.raises(ContractValidationError) as caught:
        resource_id(value)
    assert caught.value.reason_code == "invalid_request" and caught.value.status_code == 400


@pytest.mark.parametrize("value", ["frontend:main-test", "frontend:empty-test", UID])
def test_workflow_identity_preserves_exact_legacy_scopes_and_ordinary_uuids(value):
    assert workflow_identity(value) == value
    if value == UID:
        assert resource_id(value) == value


def test_workflow_identity_does_not_expand_the_legacy_scope_allowlist():
    with pytest.raises(ContractValidationError):
        workflow_identity("frontend:other-test")


@pytest.mark.parametrize("value,zero", [(1, False), (2**53 - 1, False), (0, True), (1, True)])
def test_resource_revision_preserves_safe_integer_boundaries(value, zero):
    assert resource_revision(value, zero=zero) == value


@pytest.mark.parametrize("value,zero", [
    (0, False), (-1, False), (-1, True), (True, False), (False, True),
    (1.0, False), ("1", False), (2**53, False), (None, False),
])
def test_resource_revision_rejects_bool_coercion_and_out_of_range_values(value, zero):
    with pytest.raises(ContractValidationError) as caught:
        resource_revision(value, zero=zero)
    assert caught.value.reason_code == "invalid_request" and caught.value.status_code == 400


@pytest.mark.parametrize("kind", ["global_prompt", "role_card"])
@pytest.mark.parametrize("placement", ["before", "middle", "after"])
def test_global_content_keeps_roles_placements_depth_and_detached_return(kind, placement):
    value = resource("Original text")
    value["kind"] = kind
    value["members"][0].update(placement=placement, depth=2**53 if placement == "middle" else None,
                               order=-(2**53 - 1), name="", role="assistant", enabled=False)
    checked = validate_global_content(value)
    assert checked == value and checked is not value
    checked["members"][0]["text"] = "Detached text"
    assert value["members"][0]["text"] == "Original text"


@pytest.mark.parametrize("path,replacement", [
    (("schema_version",), True), (("schema_version",), 2), (("kind",), "other"),
    (("revision",), 0), (("name",), " "), (("name",), "x" * 129), (("enabled",), 1),
    (("members",), []), (("members",), {}), (("members", 0, "name"), "x" * 129),
    (("members", 0, "text"), 3), (("members", 0, "role"), "tool"),
    (("members", 0, "placement"), "other"), (("members", 0, "depth"), 0),
    (("members", 0, "order"), True), (("members", 0, "order"), 2**53),
    (("members", 0, "enabled"), 1),
])
def test_global_content_rejects_changed_shape_or_field_semantics(path, replacement):
    value = resource()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    with pytest.raises(ContractValidationError):
        validate_global_content(value)


def test_global_content_rejects_duplicate_members_extra_fields_and_wrong_middle_depth():
    value = resource()
    value["members"].append(deepcopy(value["members"][0]))
    with pytest.raises(ContractValidationError):
        validate_global_content(value)
    value = resource()
    value["members"][0]["extra"] = None
    with pytest.raises(ContractValidationError):
        validate_global_content(value)
    value = resource()
    value["members"][0].update(placement="middle", depth=True)
    with pytest.raises(ContractValidationError):
        validate_global_content(value)


@pytest.mark.parametrize("unit", ["x", "\u4e2d"])
def test_global_content_capacity_counts_canonical_utf8_bytes_exactly(unit):
    value = resource("")
    remaining = MAX_RESOURCE_BYTES - len(canonical_bytes(value))
    width = len(unit.encode("utf-8"))
    value["members"][0]["text"] = unit * (remaining // width) + "x" * (remaining % width)
    assert len(canonical_bytes(value)) == MAX_RESOURCE_BYTES
    assert validate_global_content(value) == value
    value["members"][0]["text"] += "x"
    with pytest.raises(ContractValidationError):
        validate_global_content(value)


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_data_schema_rejects_nested_reference_loaders(keyword):
    value = definition(schema={"allOf": [{keyword: "#/$defs/local"}]})
    with pytest.raises(ContractValidationError) as caught:
        validate_data_definition(value)
    assert caught.value.reason_code == "invalid_request"


def test_data_definition_preserves_schema_and_default_error_categories_and_causes():
    with pytest.raises(ContractValidationError) as malformed:
        validate_data_definition(definition(schema={"type": "not-a-type"}))
    assert malformed.value.reason_code == "invalid_request" and malformed.value.status_code == 400
    assert str(malformed.value) == "Session data schema is invalid"
    assert isinstance(malformed.value.__cause__, SchemaError)
    with pytest.raises(ContractValidationError) as mismatch:
        validate_data_definition(definition(schema={"type": "integer"}, default="wrong type"))
    assert mismatch.value.reason_code == "session_data_type_mismatch" and mismatch.value.status_code == 400
    assert str(mismatch.value) == "Value differs from registered schema"
    assert isinstance(mismatch.value.__cause__, ValidationError)


@pytest.mark.parametrize("key", ["plain", "a:", ":b", "1a:b", "a:1b", "a:b:c", "a" * 65 + ":b"])
def test_data_definition_keeps_namespaced_key_limits(key):
    with pytest.raises(ContractValidationError):
        validate_data_definition(definition(key=key))


def test_data_definition_entry_and_session_projection_preserve_detached_json_values():
    original = definition(schema={"type": "object"}, default={"items": [1]})
    validated = validate_data_definition(original)
    validated["default"]["items"].append(2)
    assert original["default"] == {"items": [1]}
    inherited = session_data_entry(original, object())
    assert inherited["value"] == {"items": [1]}
    assigned_value = {"items": [3]}
    assigned = session_data_entry(original, assigned_value, assigned=True)
    assigned["value"]["items"].append(4)
    assert assigned_value == {"items": [3]} and original["default"] == {"items": [1]}
    state = {original["key"]: inherited}
    projected = validate_session_data(state)
    projected[original["key"]]["value"]["items"].append(5)
    assert state[original["key"]]["value"] == {"items": [1]}
    missing_default = definition()
    del missing_default["default"]
    assert session_data_entry(missing_default) == {"definition": missing_default}
    assert validate_session_data({}) == {}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"python"}, ("tuple",), "\ud800"])
def test_data_values_preserve_strict_json_rejection(value):
    with pytest.raises(ContractValidationError):
        validate_data_value(definition(schema={}), value)


def test_data_value_capacity_uses_canonical_bytes_not_string_length():
    value = "x" * (MAX_SESSION_VALUE_BYTES - 2)
    unrestricted = definition(schema={"type": "string"})
    assert len(canonical_bytes(value)) == MAX_SESSION_VALUE_BYTES
    assert validate_data_value(unrestricted, value) is None
    with pytest.raises(ContractValidationError) as caught:
        validate_data_value(unrestricted, value + "x")
    assert caught.value.reason_code == "invalid_request"


def test_session_data_preserves_key_shape_count_and_aggregate_capacity():
    entries = {}
    for index in range(4):
        item = definition(key=f"test:item{index}", schema={"type": "string"})
        entries[item["key"]] = session_data_entry(item)
    remaining = 1_000_000 - len(canonical_bytes(entries))
    for entry in entries.values():
        length = min(remaining, MAX_SESSION_VALUE_BYTES - 2)
        entry["value"] = "x" * length
        remaining -= length
    assert remaining == 0 and len(canonical_bytes(entries)) == 1_000_000
    assert validate_session_data(entries) == entries
    entries["test:item3"]["value"] += "x"
    with pytest.raises(ContractValidationError):
        validate_session_data(entries)
    with pytest.raises(ContractValidationError):
        validate_session_data({"wrong:key": session_data_entry(definition())})
    with pytest.raises(ContractValidationError):
        validate_session_data({CORE_SESSION_NOTE["key"]: {"definition": definition(), "extra": None}})
    with pytest.raises(ContractValidationError):
        validate_session_data({f"test:item{index}": {} for index in range(129)})


@pytest.mark.parametrize("type_id", ["example.document", "example.numeric"])
def test_fresh_process_current_operations_and_types_do_not_import_legacy_store_or_execution(tmp_path, type_id):
    program = r"""
import importlib.abc
import json
import sys
from contextlib import closing
from copy import deepcopy
from uuid import uuid4

blocked = (
    "phase1_agent.workbench_resources", "phase1_agent.workbench_interfaces",
    "phase1_agent.workflow", "phase1_agent.workflow_host",
    "phase1_agent.graph_agent_host", "phase1_agent.graph_agent_runtime",
    "phase1_agent.graph_service", "phase1_agent.graph_runtime_host",
    "phase1_agent.runtime", "phase1_agent.kernel", "phase1_agent.adapter",
    "phase1_agent.prepared_context", "phase1_agent.bindings",
)
class NoLegacyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Current resource imported a legacy module: " + fullname)
sys.meta_path.insert(0, NoLegacyImports())

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.global_resources import (
    GlobalResourceStore, create_global_type_registry, global_resource_reference,
)
from phase1_agent.host_sdk import DataTypeDefinition, TypeRegistry
from phase1_agent.resource_contracts import (
    CORE_SESSION_NOTE, require, resource_error, resource_id, resource_revision,
    session_data_entry, validate_data_definition, validate_data_value,
    validate_global_content, validate_session_data, workflow_identity,
)
from phase1_agent.storage import SqliteStore
from phase1_agent.type_contract_store import TypeContractStore

source = json.load(sys.stdin)
assert resource_id(source["resource_id"]) == source["resource_id"]
assert resource_revision(0, zero=True) == 0
assert workflow_identity("frontend:main-test") == "frontend:main-test"
assert require(True) is None and resource_error("fixture", "fixture", 409).status_code == 409
assert validate_global_content(source) == source
assert validate_data_definition(CORE_SESSION_NOTE) == CORE_SESSION_NOTE
assert validate_data_value(CORE_SESSION_NOTE, "Stored note") is None
state = {CORE_SESSION_NOTE["key"]: session_data_entry(CORE_SESSION_NOTE, "Stored note", assigned=True)}
assert validate_session_data(state) == state

types = create_global_type_registry()
DOCUMENT_TYPE = "example.document"
types.register(DataTypeDefinition(DOCUMENT_TYPE, 1, {
    "type": "object", "additionalProperties": False, "required": ["name", "body"],
    "properties": {"name": {"type": "string"}, "body": {"type": "string"}},
}, scope="global"))
types.register(DataTypeDefinition("example.numeric", 1, {"type": "integer"}, scope="global"))
original = {"envelope_version": 1, "scope": "workspace", "type_id": DOCUMENT_TYPE,
            "resource_id": source["resource_id"], "data_schema_version": 1, "update_sequence": 1,
            "value": {"name": "Current document", "body": source["members"][0]["text"]}}
if sys.argv[2] == "example.numeric":
    original.update(type_id="example.numeric", value=42)
reference = {field: original[field] for field in ("envelope_version", "scope", "type_id", "resource_id")}
output = global_resource_reference(reference)
assert types.references("GLOBAL_RESOURCE_REF", 1, output, scope="content") == [reference]
original_key, update_key, delete_key = (str(uuid4()) for _ in range(3))
command = {"expected_sequence": 0, "idempotency_key": original_key}
with closing(SqliteStore(sys.argv[1])) as store:
    catalog = GlobalResourceStore(store, types)
    first = catalog.write(original, **command)
    assert first == {"reference": reference, "update_sequence": 1, "deleted": False}
    assert catalog.get(reference) == original
    assert catalog.list(type_id=original["type_id"]) == [original]
    changed = deepcopy(original)
    changed["update_sequence"] = 2
    if original["type_id"] == DOCUMENT_TYPE:
        changed["value"]["body"] = "Current replacement"
    else:
        changed["value"] = 7
    catalog.write(changed, expected_sequence=1, idempotency_key=update_key)
    assert catalog.write(original, **command) == first
    assert catalog.read_many([reference]) == [changed]
    invalid = deepcopy(changed)
    invalid["update_sequence"] = 3
    if original["type_id"] == DOCUMENT_TYPE:
        invalid["value"]["body"] = 3
    else:
        invalid["value"] = "not an integer"
    try:
        catalog.write(invalid, expected_sequence=2, idempotency_key=str(uuid4()))
    except ContractValidationError:
        pass
    else:
        raise AssertionError("Invalid registered value was accepted")
    assert catalog.get(reference) == changed
    for request, expected_reason in (
        ({**original, "value": changed["value"]}, "idempotency_conflict"),
        (original, "stale_revision"),
    ):
        try:
            catalog.write(request, expected_sequence=0,
                          idempotency_key=original_key if expected_reason == "idempotency_conflict" else str(uuid4()))
        except ContractValidationError as error:
            assert error.reason_code == expected_reason and error.status_code == 409
        else:
            raise AssertionError("Conflicting request was accepted")
    evidence = TypeContractStore(store).get(original["type_id"], 1, "global")
    assert evidence is not None
    assert store._connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'workbench_resource_%'").fetchall() == []
with closing(SqliteStore(sys.argv[1])) as store:
    catalog = GlobalResourceStore(store, types)
    assert catalog.get(reference) == changed
    assert catalog.write(original, **command) == first
    assert TypeContractStore(store).get(original["type_id"], 1, "global") == evidence
    before = [dict(row) for row in store._connection.execute("SELECT * FROM global_resource_current")]
    try:
        GlobalResourceStore(store, TypeRegistry()).get(reference)
    except ContractValidationError as error:
        assert error.reason_code == "host_unknown_type"
    else:
        raise AssertionError("Missing type was silently replaced")
    assert [dict(row) for row in store._connection.execute("SELECT * FROM global_resource_current")] == before
    deleted = catalog.delete(reference, expected_sequence=2, idempotency_key=delete_key)
    assert deleted == {"reference": reference, "update_sequence": 3, "deleted": True}
    assert catalog.delete(reference, expected_sequence=2, idempotency_key=delete_key) == deleted
    assert catalog.get(reference) is None and catalog.list() == []
    assert catalog.head(reference) == deleted
    assert store._connection.execute("SELECT payload FROM global_resource_current").fetchone()[0] is None
    receipts = [dict(row) for row in store._connection.execute("SELECT * FROM global_resource_receipts")]
    assert len(receipts) == 3 and len(canonical_bytes(receipts)) < 4096
    if original["type_id"] == DOCUMENT_TYPE:
        assert source["members"][0]["text"] not in canonical_bytes(receipts).decode("utf-8")
assert not any(name == entry or name.startswith(entry + ".") for name in sys.modules for entry in blocked)
print("pure current resource operations, type evidence and original receipts preserved")
"""
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [source, environment.get("PYTHONPATH", "")]))
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "current-resource.sqlite"), type_id],
        input=json.dumps(resource("Original current resource body")), text=True, capture_output=True,
        env=environment, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "pure current resource operations, type evidence and original receipts preserved"
