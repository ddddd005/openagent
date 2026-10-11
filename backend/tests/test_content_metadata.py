"""Prompt display metadata has independent authority, CAS and original receipts."""

from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.content_configuration import ContentConfigurationService
from phase1_agent.graph_application import GraphApplication
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow_host import WorkflowHost
from test_content_configuration import lorebook_record, prompt_record, provider_record, reference


def saved(tmp_path, *, fault=None):
    path = tmp_path / "content.sqlite"
    service = ContentConfigurationService(path, store_factory=lambda: SqliteStore(path, fault_injector=fault))
    record = prompt_record()
    service.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
    return service, record, path


def request(record, name="Named prompt", sequence=0):
    return {"reference": reference(record), "display_name": name,
            "expected_metadata_sequence": sequence, "idempotency_key": str(uuid4())}


def test_name_persists_independently_without_changing_resource_envelope(tmp_path):
    service, record, path = saved(tmp_path)
    assert service.read_content_metadata(reference(record)) == {
        "reference": reference(record), "display_name": "", "metadata_sequence": 0}
    accepted = service.update_content_metadata(**request(record, "  Formal name  "))
    assert accepted == {"reference": reference(record), "display_name": "Formal name", "metadata_sequence": 1}
    assert ContentConfigurationService(path).read_content_metadata(reference(record)) == accepted
    assert service.get_global_resource(reference(record)) == record
    assert set(record) == {"envelope_version", "scope", "type_id", "resource_id",
                           "data_schema_version", "update_sequence", "value"}


def test_name_limit_counts_unicode_characters_and_empty_name_is_an_explicit_clear(tmp_path):
    service, record, _ = saved(tmp_path)
    name = "\U0001f680" * 128
    assert service.update_content_metadata(**request(record, name))["display_name"] == name
    cleared = service.update_content_metadata(**request(record, "", 1))
    assert cleared["display_name"] == "" and cleared["metadata_sequence"] == 2
    assert service.get_global_resource(reference(record)) == record


def test_scope_coordinates_and_metadata_cas_are_independent(tmp_path):
    service, record, _ = saved(tmp_path)
    peer = deepcopy(record)
    peer["scope"] = "project:story"
    service.save_global_resource(peer, expected_sequence=0, idempotency_key=str(uuid4()))
    original = request(record)
    first = service.update_content_metadata(**original)
    second = service.update_content_metadata(**request(record, "Changed", 1))
    service.update_content_metadata(**request(peer, "Peer"))
    assert service.update_content_metadata(**original) == first
    assert service.read_content_metadata_receipt(**original) == first
    assert service.read_content_metadata(reference(record)) == second
    assert service.read_content_metadata(reference(peer))["display_name"] == "Peer"
    with pytest.raises(Exception) as stale:
        service.update_content_metadata(**request(record, "Stale", 0))
    assert stale.value.reason_code == "stale_metadata_sequence"
    with pytest.raises(Exception) as conflict:
        service.update_content_metadata(**{**original, "display_name": "Different original"})
    assert conflict.value.reason_code == "idempotency_conflict"


def test_original_receipt_survives_resource_deletion_without_allowing_new_names(tmp_path):
    service, record, _ = saved(tmp_path)
    original = request(record)
    accepted = service.update_content_metadata(**original)
    service.delete_global_resource(reference(record), expected_sequence=1, idempotency_key=str(uuid4()))
    assert service.read_content_metadata_receipt(**original) == accepted
    assert service.update_content_metadata(**original) == accepted
    for action in (lambda: service.read_content_metadata(reference(record)),
                   lambda: service.update_content_metadata(**request(record, "New", 1))):
        with pytest.raises(Exception) as missing:
            action()
        assert missing.value.reason_code == "global_resource_missing"


@pytest.mark.parametrize("record_factory", [lorebook_record, provider_record])
def test_name_authority_denies_other_business_types(tmp_path, record_factory):
    service = ContentConfigurationService(tmp_path / "content.sqlite")
    record = record_factory()
    for action in (lambda: service.read_content_metadata(reference(record)),
                   lambda: service.update_content_metadata(**request(record))):
        with pytest.raises(Exception) as denied:
            action()
        assert denied.value.reason_code in ("content_resource_type_denied", "content_metadata_type_denied")


@pytest.mark.parametrize("change", [
    {"display_name": "x" * 129}, {"display_name": "\ud800"}, {"display_name": None},
    {"expected_metadata_sequence": True}, {"expected_metadata_sequence": -1},
    {"expected_metadata_sequence": 0.5}, {"idempotency_key": "invalid"},
])
def test_invalid_names_and_request_coordinates_do_not_write(tmp_path, change):
    service, record, _ = saved(tmp_path)
    with pytest.raises(Exception):
        service.update_content_metadata(**{**request(record), **change})
    assert service.read_content_metadata(reference(record))["metadata_sequence"] == 0


@pytest.mark.parametrize("point", ["after_content_metadata_write", "before_commit"])
def test_metadata_and_receipt_roll_back_together(tmp_path, point):
    active = [False]
    def fault(current):
        if active[0] and current == point:
            raise RuntimeError("Controlled metadata transaction fault")
    service, record, _ = saved(tmp_path, fault=fault)
    original = request(record)
    active[0] = True
    with pytest.raises(RuntimeError):
        service.update_content_metadata(**original)
    active[0] = False
    assert service.read_content_metadata(reference(record))["metadata_sequence"] == 0
    assert service.read_content_metadata_receipt(**original) is None
    assert service.update_content_metadata(**original)["metadata_sequence"] == 1


def test_application_name_command_and_original_receipt_use_content_authority(tmp_path):
    host = WorkflowHost(tmp_path / "content.sqlite")
    try:
        app, record = GraphApplication(host), prompt_record()
        app.command("resource.save", {"record": record, "expected_sequence": 0, "idempotency_key": str(uuid4())})
        original = request(record)
        first = app.command("content.metadata.update", original)
        app.command("content.metadata.update", request(record, "Changed", 1))
        receipt = app.query("receipt.read", {"operation": "content.metadata.update", "parameters": original})
        assert receipt["outcome"] == "matched"
        assert receipt["result"] == first["result"]
        assert receipt["receipt"]["accepted"] == first["result"]
        assert app.query("content.metadata.read", {"reference": reference(record)})["display_name"] == "Changed"
        assert app.command("content.metadata.update", original) == first
        different = app.query("receipt.read", {
            "operation": "content.metadata.update", "parameters": {**original, "display_name": "Different"}})
        assert different["outcome"] == "unresolved"
        assert host._graph is None and host._providers is None
    finally:
        host.close()
