"""Global configuration authority and one-action Key acceptance, without network."""

from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.global_resources import GlobalResourceStore
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_provider_host import ProviderConfigurationService
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.credential_store import PrivateCredentialStore
from phase1_agent.model_contract import validate_model_source_config
from test_model_package import BoundaryFixture, profile_config, source_config, uid
from credential_test_support import TestProtector, mutate_test_credential


def key_request(*, identity=None, sequence=0, generation=0, reference=None):
    return {
        "identity": identity or {**profile_config()["provider"], "resource_id": str(uuid4())},
        "expected_sequence": sequence, "credential_ref": reference or "local:" + str(uuid4()),
        "expected_generation": generation, "idempotency_key": str(uuid4()),
        "provider_seed": {"name": "Private provider", "protocol": "chat",
                          "base_url": "https://offline.invalid/v1", "enabled": True},
    }


def test_configuration_service_initializes_without_graph_runtime_and_key_save_is_effective(tmp_path):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    body = key_request()
    result = manager.save_provider_credential(**body, secret="dummy-private-key")
    assert result["outcome"] == "saved"
    assert result["provider"] == {"reference": body["identity"], "update_sequence": 1, "deleted": False}
    assert manager.get_global_resource(body["identity"])["value"]["credential_ref"] == body["credential_ref"]
    assert manager.credential_status(body["credential_ref"]) == result["credential"]
    assert manager.provider_credential_receipt(body) == result
    assert manager.save_provider_credential(**body, secret="dummy-private-key") == result
    assert b"dummy-private-key" not in (tmp_path / "config.sqlite").read_bytes()
    with pytest.raises(Exception) as failure:
        manager.save_provider_credential(**body, secret="different-private-key")
    assert failure.value.reason_code == "idempotency_conflict"


def test_configuration_operations_do_not_read_unrelated_damaged_workflow_records(tmp_path):
    manager = ProviderConfigurationService(tmp_path / "nested" / "config.sqlite",
                                           credential_protector=TestProtector())
    with closing(manager._store_factory()) as store:
        store._connection.execute("INSERT INTO records VALUES(?,?,?,1,'test')", (
            "workflow_session", str(uuid4()), '{"damaged":"unrelated test history"}'))
    body = key_request()
    result = manager.save_provider_credential(**body, secret="dummy-private-key")
    assert result["outcome"] == "saved"
    assert manager.provider_credential_receipt(body) == result


def test_provider_key_identity_rejects_a_binding_receipt_for_a_save_request(tmp_path):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    body = key_request(generation=1)
    mutate_test_credential(manager.credentials, body["credential_ref"], "original-private-key")
    accepted = manager.save_provider_credential(**body, secret="saved-private-key")
    assert accepted["outcome"] == "saved"
    before = manager.get_global_resource(body["identity"])
    with pytest.raises(Exception) as failure:
        manager.provider_binding_receipt(body)
    assert failure.value.reason_code == "idempotency_conflict"
    assert manager.get_global_resource(body["identity"]) == before
    assert manager.credentials.read(body["credential_ref"]) == ("saved-private-key", 2)


def test_existing_provider_save_applies_entire_draft_and_key_in_one_action(tmp_path):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    initial = key_request()
    manager.save_provider_credential(**initial, secret="first-private-key")
    before = manager.get_global_resource(initial["identity"])
    body = key_request(identity=initial["identity"], sequence=1)
    body["provider_seed"].update(name="Edited provider", base_url="https://edited.invalid/v1", enabled=False)
    result = manager.save_provider_credential(**body, secret="second-private-key")
    current = manager.get_global_resource(initial["identity"])
    assert result["outcome"] == "saved" and current["update_sequence"] == 2
    assert current["value"] == {**body["provider_seed"], "credential_ref": body["credential_ref"]}
    assert before["value"]["base_url"] == "https://offline.invalid/v1"
    assert manager.provider_credential_receipt(body) == result
    assert manager.get_global_resource(initial["identity"]) is not None


@pytest.mark.parametrize("configuration", [None, {}, {"name": "Incomplete"}])
def test_incomplete_existing_provider_save_is_rejected_before_key_mutation(tmp_path, configuration):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    initial = key_request()
    manager.save_provider_credential(**initial, secret="first-private-key")
    before = manager.get_global_resource(initial["identity"])
    body = key_request(identity=initial["identity"], sequence=1)
    body["provider_seed"] = configuration
    with pytest.raises(Exception) as failure:
        manager.save_provider_credential(**body, secret="must-not-be-written")
    assert failure.value.reason_code == "invalid_request"
    assert manager.get_global_resource(initial["identity"]) == before
    assert manager.credential_status(body["credential_ref"])["state"] == "missing"


def test_same_save_identity_cannot_be_reused_for_a_changed_draft(tmp_path):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    body = key_request()
    manager.save_provider_credential(**body, secret="dummy-private-key")
    changed = deepcopy(body)
    changed["provider_seed"]["base_url"] = "https://different.invalid"
    with pytest.raises(Exception) as failure:
        manager.save_provider_credential(**changed, secret="dummy-private-key")
    assert failure.value.reason_code == "idempotency_conflict"
    assert manager.get_global_resource(body["identity"])["value"]["base_url"] == body["provider_seed"]["base_url"]


@pytest.mark.parametrize("receipt_store", ["credential", "provider"])
def test_original_acceptance_rejects_boolean_version_lookalikes(tmp_path, receipt_store):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    body = key_request()
    manager.save_provider_credential(**body, secret="dummy-private-key")
    path = manager.credentials.path if receipt_store == "credential" else manager.database
    with closing(sqlite3.connect(path)) as connection:
        if receipt_store == "credential":
            value = json.loads(connection.execute(
                "SELECT result FROM receipts WHERE key=?", (body["idempotency_key"],)).fetchone()[0])
            value["generation"] = True
            connection.execute("UPDATE receipts SET result=? WHERE key=?", (json.dumps(value), body["idempotency_key"]))
        else:
            value = json.loads(connection.execute(
                "SELECT payload FROM global_resource_receipts WHERE idempotency_key=?",
                (body["idempotency_key"],)).fetchone()[0])
            value["update_sequence"] = True
            connection.execute("UPDATE global_resource_receipts SET payload=? WHERE idempotency_key=?",
                               (json.dumps(value), body["idempotency_key"]))
        connection.commit()
    with pytest.raises(Exception) as failure:
        manager.provider_credential_receipt(body)
    assert failure.value.reason_code == (
        "credential_receipt_invalid" if receipt_store == "credential" else "provider_binding_receipt_invalid")


@pytest.mark.parametrize("missing", ["vault", "credential_receipt", "provider_receipt"])
def test_saved_key_result_is_not_reused_when_authoritative_receipt_disappears(tmp_path, missing):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    body = key_request()
    accepted = manager.save_provider_credential(**body, secret="dummy-private-key")
    assert accepted["outcome"] == "saved"
    if missing == "vault":
        manager.credentials = PrivateCredentialStore(tmp_path / "missing-vault.sqlite", TestProtector())
    else:
        path = manager.credentials.path if missing == "credential_receipt" else manager.database
        with closing(sqlite3.connect(path)) as connection:
            if missing == "credential_receipt":
                connection.execute("DELETE FROM receipts WHERE key=?", (body["idempotency_key"],))
            else:
                connection.execute("DELETE FROM global_resource_receipts WHERE idempotency_key=?",
                                   (body["idempotency_key"],))
            connection.commit()
    before = manager.get_global_resource(body["identity"])
    result = manager.provider_credential_receipt(body)
    assert result["outcome"] == "unresolved"
    assert result["reason_code"] == (
        "provider_binding_receipt_missing" if missing == "provider_receipt" else "credential_receipt_missing")
    repeated_secret = "dummy-private-key" if missing == "provider_receipt" else "must-not-be-written-key"
    assert manager.save_provider_credential(**body, secret=repeated_secret) == result
    assert manager.get_global_resource(body["identity"]) == before
    if missing == "vault":
        assert manager.credential_status(body["credential_ref"])["state"] == "missing"
        assert not manager.credentials.path.exists()
    else:
        assert manager.credentials.read(body["credential_ref"]) == ("dummy-private-key", 1)


@pytest.mark.parametrize("missing", ["vault", "provider_receipt"])
def test_saved_bind_result_is_not_reused_when_current_key_or_binding_receipt_disappears(tmp_path, missing):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    body = key_request(generation=1)
    mutate_test_credential(manager.credentials, body["credential_ref"], "dummy-private-key")
    record = {**body["identity"], "data_schema_version": 3, "update_sequence": 1,
              "value": {**body["provider_seed"], "credential_ref": body["credential_ref"]}}
    manager.save_global_resource(record, expected_sequence=0, idempotency_key=body["idempotency_key"])
    # Simulate original evidence from the retired mutation, not a new product route.
    with closing(manager._store_factory()) as store:
        manager._reserve(store, body, "provider.credential.bind", lambda: record)
    accepted = manager.provider_binding_receipt(body)
    assert accepted["outcome"] == "saved"
    with closing(manager._store_factory()) as store:
        store._connection.execute("UPDATE provider_key_requests SET result=? WHERE idempotency_key=?", (
            json.dumps(accepted), body["idempotency_key"]))
    if missing == "vault":
        manager.credentials = PrivateCredentialStore(tmp_path / "missing-vault.sqlite", TestProtector())
    else:
        with closing(sqlite3.connect(manager.database)) as connection:
            connection.execute("DELETE FROM global_resource_receipts WHERE idempotency_key=?",
                               (body["idempotency_key"],))
            connection.commit()
    before = manager.get_global_resource(body["identity"])
    result = manager.provider_binding_receipt(body)
    assert result["outcome"] != "saved"
    assert result["reason_code"] == (
        "provider_credential_changed" if missing == "vault" else "provider_binding_receipt_missing")
    assert manager.provider_binding_receipt(body) == result
    assert not hasattr(manager, "bind_provider_credential")
    assert manager.get_global_resource(body["identity"]) == before


def test_provider_cas_rejection_after_key_save_is_partial_and_original_receipt_never_replays(tmp_path, monkeypatch):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    initial = key_request()
    manager.save_provider_credential(**initial, secret="first-private-key")
    body = key_request(identity=initial["identity"], sequence=1)
    write = GlobalResourceStore.write
    attempted = []

    def conflict(store, record, **options):
        if options["idempotency_key"] == body["idempotency_key"]:
            attempted.append(True)
            changed = deepcopy(record)
            changed["value"]["name"] = "Concurrent change"
            changed["value"]["credential_ref"] = initial["credential_ref"]
            write(store, changed, expected_sequence=1, idempotency_key=str(uuid4()))
        return write(store, record, **options)

    monkeypatch.setattr(GlobalResourceStore, "write", conflict)
    result = manager.save_provider_credential(**body, secret="second-private-key")
    assert result["outcome"] == "partial"
    assert result["reason_code"] == "provider_binding_conflict"
    assert result["credential"]["state"] == "saved" and result["provider"] is None
    assert manager.get_global_resource(body["identity"])["value"]["name"] == "Concurrent change"
    assert manager.provider_credential_receipt(body) == result
    assert manager.save_provider_credential(**body, secret="second-private-key") == result
    assert attempted == [True]


def test_unknown_provider_outcome_requires_receipt_and_does_not_replay(tmp_path, monkeypatch):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    body, calls = key_request(), []
    def fail(store, record, **options):
        calls.append(True)
        raise OSError("commit result was lost")
    monkeypatch.setattr(GlobalResourceStore, "write", fail)
    result = manager.save_provider_credential(**body, secret="dummy-private-key")
    assert result["outcome"] == "unresolved"
    assert result["reason_code"] == "provider_binding_result_unknown"
    assert manager.provider_credential_receipt(body) == result
    assert manager.save_provider_credential(**body, secret="dummy-private-key") == result
    assert calls == [True]


def test_lost_provider_return_after_actual_commit_is_verified_without_replaying(tmp_path, monkeypatch):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    body = key_request()
    write = GlobalResourceStore.write
    def lost(store, record, **options):
        write(store, record, **options)
        raise OSError("lost return after commit")
    monkeypatch.setattr(GlobalResourceStore, "write", lost)
    result = manager.save_provider_credential(**body, secret="dummy-private-key")
    assert result["outcome"] == "saved"
    assert manager.provider_credential_receipt(body) == result


def test_interrupted_private_intent_is_not_replayed_and_raw_exception_is_sanitized(tmp_path, monkeypatch):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    body, calls = key_request(), []
    def fail(store, operation, request, secret=None):
        calls.append(True)
        raise OSError("dummy-private-key must not escape")
    monkeypatch.setattr(PrivateCredentialStore, "mutate", fail)
    result = manager.save_provider_credential(**body, secret="dummy-private-key")
    assert result["outcome"] == "unresolved" and result["credential"] is None
    assert "dummy-private-key" not in str(result)
    assert manager.provider_credential_receipt(body) == result
    assert manager.save_provider_credential(**body, secret="dummy-private-key") == result
    assert calls == [True]
    assert manager.get_global_resource(body["identity"]) is None


def test_known_private_rejection_keeps_its_reason_when_original_request_is_reconciled(tmp_path):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    body = key_request()
    mutate_test_credential(manager.credentials, body["credential_ref"], "already-saved-key")
    result = manager.save_provider_credential(**body, secret="later-private-key")
    assert result["outcome"] == "unresolved"
    assert result["reason_code"] == "stale_credential_generation"
    assert manager.provider_credential_receipt(body) == result
    assert manager.save_provider_credential(**body, secret="later-private-key") == result
    assert manager.credentials.read(body["credential_ref"]) == ("already-saved-key", 1)


def test_lost_private_return_reports_saved_key_without_applying_provider_step(tmp_path, monkeypatch):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    body = key_request()
    mutate = PrivateCredentialStore.mutate
    def lost(store, operation, request, secret=None):
        mutate(store, operation, request, secret)
        raise OSError("return was lost after private commit")
    monkeypatch.setattr(PrivateCredentialStore, "mutate", lost)
    result = manager.save_provider_credential(**body, secret="dummy-private-key")
    assert result["outcome"] == "partial" and result["reason_code"] == "provider_binding_not_applied"
    assert result["credential"]["state"] == "saved" and result["provider"] is None
    assert manager.provider_credential_receipt(body) == result
    assert manager.get_global_resource(body["identity"]) is None


def test_partial_save_preserves_original_evidence_without_a_binding_mutation(tmp_path, monkeypatch):
    manager = ProviderConfigurationService(tmp_path / "config.sqlite", credential_protector=TestProtector())
    original = key_request()
    write = GlobalResourceStore.write
    def fail(store, record, **options):
        raise OSError("provider acceptance unknown")
    monkeypatch.setattr(GlobalResourceStore, "write", fail)
    partial = manager.save_provider_credential(**original, secret="dummy-private-key")
    assert partial["credential"]["generation"] == 1
    assert manager.get_global_resource(original["identity"]) is None
    monkeypatch.setattr(GlobalResourceStore, "write", write)
    assert not hasattr(manager, "bind_provider_credential")
    assert manager.credentials.read(original["credential_ref"]) == ("dummy-private-key", 1)
    assert manager.get_global_resource(original["identity"]) is None
    assert manager.provider_credential_receipt(original) == partial


def test_management_application_key_request_is_secret_free_and_consumer_denied(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "config.sqlite", credential_protector=TestProtector())) as service:
        app, body = GraphApplication(service), key_request()
        accepted = app.command("provider.credential.save", {**body, "secret": "dummy-private-key"})
        assert accepted["result"]["outcome"] == "saved"
        assert accepted["receipt"]["accepted"] == accepted["result"]
        assert "dummy-private-key" not in str(accepted)
        assert app.query("provider.credential.receipt", {"request": body}) == accepted["result"]
        recovery = {**body, "expected_sequence": 1, "expected_generation": 1,
                    "provider_seed": None, "idempotency_key": str(uuid4())}
        with pytest.raises(Exception) as removed:
            app.command("provider.credential.bind", recovery)
        assert removed.value.reason_code == "not_found"
        assert app.query("provider.credential.bind-receipt", {"request": recovery})["outcome"] == "unresolved"
        with pytest.raises(Exception) as failure:
            app.for_consumer().query("provider.credential.receipt", {"request": body})
        assert failure.value.reason_code == "application_scope_denied"


def test_workflow_source_rejects_per_workflow_parameters_capacity_and_provider_reference():
    for changes in ({"parameters": profile_config()["parameters"]},
                    {"capacity": profile_config()["capacity"]},
                    {"reference": profile_config()["provider"]}):
        with pytest.raises(Exception):
            validate_model_source_config({**source_config(), **changes})


def test_named_profiles_share_provider_and_new_runs_adopt_current_without_changing_old_frame():
    fixture = BoundaryFixture()
    second_profile = deepcopy(fixture.profile)
    second_profile["resource_id"] = uid(90)
    second_profile["value"]["name"] = "Another temperature"
    second_profile["value"]["parameters"]["temperature"] = 1
    fixture.profile["value"]["parameters"]["temperature"] = 0.25
    fixture.profile["value"]["capacity"]["summary_max_tokens"] = 64
    fixture.profile["update_sequence"] = 2
    fixture.service.prepare_run(
        workflow_session_id=uid(2), chain_run_id=uid(4),
        node_configs={uid(10): fixture.config, uid(12): {
            "reference": {key: second_profile[key] for key in (
                "envelope_version", "scope", "type_id", "resource_id")}}},
        records=[fixture.profile, second_profile, fixture.record])
    source = fixture.context(10, 15, ("models:resolve",))
    source.chain_run_id = uid(4)
    current = fixture.service.bind_model(source, fixture.config)
    source.node_binding_id, source.node_run_id = uid(12), uid(16)
    other = fixture.service.bind_model(source, {"reference": {
        key: second_profile[key] for key in ("envelope_version", "scope", "type_id", "resource_id")}})
    assert fixture.binding["parameters"]["temperature"] == 0.5
    assert fixture.binding["capacity"]["summary_max_tokens"] == 128
    assert current["parameters"]["temperature"] == 0.25
    assert current["capacity"]["summary_max_tokens"] == 64
    assert other["parameters"]["temperature"] == 1
    fixture.chat()
    assert fixture.factory_args[0]["parameters"]["temperature"] == 0.5


def test_missing_profile_reports_configuration_failure_before_freezing_credentials():
    fixture = BoundaryFixture()
    with pytest.raises(Exception) as failure:
        fixture.service.prepare_run(workflow_session_id=uid(2), chain_run_id=uid(4),
                                    node_configs={uid(10): fixture.config}, records=[fixture.record])
    assert failure.value.reason_code == "model_profile_unavailable"
