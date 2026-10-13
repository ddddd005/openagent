from contextlib import closing
from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from phase1_agent.contract_json import canonical_bytes, content_digest
from phase1_agent.global_resources import GlobalResourceStore
from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_application_identity import application_command_digest
from phase1_agent.graph_provider_host import ProviderConfigurationService
from phase1_agent.workflow_host import WorkflowHost
from credential_test_support import TestProtector


def public_request():
    return {
        "identity": {"envelope_version": 1, "scope": "workspace", "type_id": "workflow.chat-provider",
                     "resource_id": str(uuid4())},
        "expected_sequence": 0, "credential_ref": "local:" + str(uuid4()),
        "expected_generation": 0, "idempotency_key": str(uuid4()),
        "provider_seed": {"name": "Evidence provider", "protocol": "chat",
                          "base_url": "https://offline.invalid/v1", "enabled": True},
    }


def evidence(request):
    return {"operation": "provider.credential.save", "idempotency_key": request["idempotency_key"],
            "owner_kind": "provider-credential",
            "owner_id": request["identity"]["scope"] + ":" + request["identity"]["resource_id"],
            "request": request}


def host_with_vault(path):
    host = WorkflowHost(path)
    host._providers = ProviderConfigurationService(path, credential_protector=TestProtector())
    return host, GraphApplication(host)


def test_provider_evidence_is_exact_public_identity_without_secret_or_runtime(tmp_path):
    host, request = WorkflowHost(tmp_path / "public.sqlite"), public_request()
    app = GraphApplication(host)
    saved = app.command("request-evidence.write", evidence(request))["result"]
    assert saved["request"] == request
    assert saved["request_digest"] == application_command_digest("provider.credential.save", request)
    assert host._graph is None and host._providers is None
    assert not (tmp_path / "public.sqlite.credentials.sqlite").exists()
    assert app.query("request-evidence.read", {"owner_kind": saved["owner_kind"],
                                              "owner_id": saved["owner_id"]}) == [saved]


@pytest.mark.parametrize("change", [
    {"secret": ""}, {"secret": "fake-do-not-store"}, {"provider_seed": None},
    {"expected_generation": False}, {"credential_ref": "env:FAKE_KEY"},
])
def test_provider_evidence_rejects_secret_placeholders_and_invalid_public_fields(tmp_path, change):
    request = {**public_request(), **change}
    host = WorkflowHost(tmp_path / "public.sqlite")
    app = GraphApplication(host)
    with pytest.raises(Exception):
        app.command("request-evidence.write", evidence(request))
    assert app.query("request-evidence.read", {"owner_kind": "provider-credential",
                                              "owner_id": evidence(request)["owner_id"]}) == []
    assert host._providers is None and not (tmp_path / "public.sqlite.credentials.sqlite").exists()


def test_public_digest_matches_provider_and_vault_receipts_without_retyping_secret(tmp_path, monkeypatch):
    host, app = host_with_vault(tmp_path / "public.sqlite")
    request = public_request()
    original = app.command("request-evidence.write", evidence(request))["result"]
    accepted = app.command("provider.credential.save", {**request, "secret": "fake-private-evidence-key"})
    assert accepted["receipt"]["request_sha256"] == original["request_digest"]
    public_identity = {"operation": "provider.credential.save", "parameters": request}
    with closing(sqlite3.connect(host.database)) as connection:
        stored = connection.execute("SELECT digest,request FROM provider_key_requests").fetchone()
        assert stored == (content_digest(public_identity), canonical_bytes(public_identity).decode())
    assert b"fake-private-evidence-key" not in host.database.read_bytes()
    assert "secret" not in str(accepted)

    def forbidden(*args, **kwargs):
        pytest.fail("Reconciliation must never call the private mutation")

    monkeypatch.setattr(host.provider_configuration.credentials, "mutate", forbidden)
    assert app.query("provider.credential.receipt", {"request": deepcopy(original["request"])}) == accepted["result"]
    changed = deepcopy(request)
    changed["provider_seed"]["name"] = "Changed request"
    with pytest.raises(Exception) as mismatch:
        app.query("provider.credential.receipt", {"request": changed})
    assert mismatch.value.reason_code == "idempotency_conflict"
    assert host._graph is None


def test_evidence_without_dispatch_remains_unknown_and_never_creates_private_key(tmp_path):
    host, app = host_with_vault(tmp_path / "public.sqlite")
    request = public_request()
    saved = app.command("request-evidence.write", evidence(request))["result"]
    result = app.query("provider.credential.receipt", {"request": saved["request"]})
    assert result["outcome"] == "unresolved"
    assert result["reason_code"] == "provider_credential_request_missing"
    assert not host.provider_configuration.credentials.path.exists()
    assert app.query("request-evidence.read", {"owner_kind": saved["owner_kind"],
                                              "owner_id": saved["owner_id"]}) == [saved]


def test_partial_provider_request_stays_exact_pending_and_read_only(tmp_path, monkeypatch):
    host, app = host_with_vault(tmp_path / "public.sqlite")
    request, calls = public_request(), []
    saved = app.command("request-evidence.write", evidence(request))["result"]

    def fail_write(*args, **kwargs):
        calls.append(True)
        raise OSError("Controlled unknown provider write")

    monkeypatch.setattr(GlobalResourceStore, "write", fail_write)
    accepted = app.command("provider.credential.save", {**request, "secret": "fake-private-partial-key"})["result"]
    assert accepted["outcome"] == "unresolved" and accepted["credential"]["generation"] == 1
    assert app.query("provider.credential.receipt", {"request": request}) == accepted
    assert calls == [True]
    assert app.query("request-evidence.read", {"owner_kind": saved["owner_kind"],
                                              "owner_id": saved["owner_id"]}) == [saved]
