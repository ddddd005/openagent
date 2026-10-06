"""Configuration persistence is non-executing, versioned and secret-free."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.model_configuration import (
    CHAT_MAX_TOKENS, DEFAULT_PROVIDER_ID, MODEL_WORKFLOW_ID, chat_parameter_diagnostic,
    default_provider, validate_model_configuration,
    validate_provider,
)
from phase1_agent.model_configuration_store import ModelConfigurationStore
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService
from test_server_prompt_configs import mutation, request, running_server


@pytest.fixture
def tmp_path():
    with TemporaryDirectory(prefix="model-configuration-", dir=Path(__file__).parent) as folder:
        yield Path(folder)


def uid(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def model_config():
    return {
        "schema_version": 1, "kind": "workflow_model_configuration",
        "config_id": uid(1), "revision": 1, "workflow_id": MODEL_WORKFLOW_ID,
        "nodes": [{
            "id": uid(2), "position": {"x": 20, "y": -100},
            "provider_ref": {"provider_id": DEFAULT_PROVIDER_ID, "revision": 1},
            "parameters": {"model": "deepseek-flash", "max_tokens": 2048, "temperature": 0.4},
        }],
        "edges": [
            {"id": uid(3), "source": uid(2), "target_binding_id": A_BINDING},
            {"id": uid(4), "source": uid(2), "target_binding_id": B_BINDING},
        ],
    }


def test_store_exact_versions_cas_idempotency_and_reopen(tmp_path):
    database = tmp_path / "model.sqlite"
    with closing(WorkflowService(database)) as service:
        first = service.list_model_providers()[0]
        second = {**first, "revision": 2, "name": "edited", "base_url": "https://example.test/v1"}
        saved = service.save_model_configuration("provider", **mutation(second, 1, "edit"))
        assert saved == second
        assert service.save_model_configuration("provider", **mutation(second, 1, "edit")) == second
        assert service.get_model_configuration("provider", DEFAULT_PROVIDER_ID, 1) == first
        assert service.get_model_configuration("provider", DEFAULT_PROVIDER_ID) == second
        for body, reason in [
            (mutation({**second, "name": "different"}, 1, "edit"), "idempotency_conflict"),
            (mutation({**second, "revision": 3}, 1, "stale"), "stale_revision"),
        ]:
            with pytest.raises(ContractValidationError) as error:
                service.save_model_configuration("provider", **body)
            assert error.value.reason_code == reason
    with closing(WorkflowService(database)) as service:
        assert service.list_model_providers() == [second]
        assert service.get_model_configuration("provider", DEFAULT_PROVIDER_ID, 1) == first


@pytest.mark.parametrize("change", [
    lambda value: value.update(api_key="PRIVATE-KEY"),
    lambda value: value.update(protocol="responses"),
    lambda value: value.update(credential_ref="PRIVATE-KEY"),
    lambda value: value.update(base_url="https://name:PRIVATE-KEY@example.test"),
    lambda value: value.update(base_url="https://example.test?api_key=PRIVATE-KEY"),
    lambda value: value.update(base_url="http://example.test/v1"),
    lambda value: value.update(base_url="https://example.test:INVALID"),
    lambda value: value.update(base_url="https://example.test:0"),
    lambda value: value.update(base_url="https://example.test\\path"),
    lambda value: value.update(base_url="https://example.test?"),
    lambda value: value.update(base_url="https://example.test#"),
    lambda value: value.update(base_url="https://example.test/\x7f"),
    lambda value: value.update(base_url="https:///example.test"),
    lambda value: value.update(base_url="http://127.1"),
    lambda value: value.update(name="PRIVATE\n"),
    lambda value: value.update(revision=True),
    lambda value: value.update(enabled=1),
])
def test_provider_contract_rejects_secrets_unknown_protocol_and_invalid_address(change):
    provider = default_provider()
    change(provider)
    with pytest.raises(ContractValidationError) as error:
        validate_provider(provider)
    assert error.value.reason_code == "invalid_request"


@pytest.mark.parametrize("change", [
    lambda value: value["nodes"][0]["parameters"].update(stream=True),
    lambda value: value["nodes"][0]["parameters"].update(temperature=float("nan")),
    lambda value: value["nodes"][0]["parameters"].update(temperature=2.1),
    lambda value: value["nodes"][0]["parameters"].update(max_tokens=True),
    lambda value: value["nodes"][0]["parameters"].update(model=""),
    lambda value: value["nodes"][0]["parameters"].update(model="PRIVATE\x7f"),
    lambda value: value["edges"][1].update(target_binding_id=A_BINDING),
    lambda value: value["edges"][0].update(source={}),
    lambda value: value["edges"][0].update(target_binding_id={}),
    lambda value: value["nodes"].append(deepcopy(value["nodes"][0])),
])
def test_model_contract_enforces_typed_dependencies_and_existing_parameters(change):
    record = model_config()
    change(record)
    with pytest.raises(ContractValidationError) as error:
        validate_model_configuration(record)
    assert error.value.reason_code == "invalid_request"


def test_missing_and_revoked_provider_preserve_graph_and_exact_reference(tmp_path):
    with closing(WorkflowService(tmp_path / "model.sqlite")) as service:
        record = model_config()
        assert service.save_model_configuration("model", **mutation(record)) == record
        assert service.diagnose_model_configuration(uid(1), 1)["diagnostics"] == []
        provider = {**default_provider(), "revision": 2, "enabled": False}
        service.save_model_configuration("provider", **mutation(provider, 1, "revoke"))
        diagnostic = service.diagnose_model_configuration(uid(1), 1)
        assert diagnostic == {
            "config_id": uid(1), "revision": 1, "execution_supported": False,
            "diagnostics": [{"target": uid(2), "code": "provider_unavailable"}],
        }
        assert service.get_model_configuration("model", uid(1), 1) == record
        missing = deepcopy(record)
        missing.update(revision=2, edges=[])
        missing["nodes"][0]["provider_ref"]["provider_id"] = uid(999)
        service.save_model_configuration("model", **mutation(missing, 1, "missing"))
        assert {row["code"] for row in service.diagnose_model_configuration(uid(1), 2)["diagnostics"]} == {
            "model_dependency_missing", "provider_missing",
        }


@pytest.mark.parametrize("address", [
    "https://api.deepseek.com", "https://api.deepseek.com/v1/",
    "HTTPS://api.deepseek.com", "http://localhost:8000/v1",
    "http://127.0.0.1:8000", "http://[::1]:8000/v1",
])
def test_provider_valid_https_and_literal_loopback_addresses(address):
    provider = {**default_provider(), "base_url": address}
    assert validate_provider(provider) == provider


@pytest.mark.parametrize("parameters", [
    {"model": "deepseek-flash"},
    {"model": "deepseek-flash", "max_tokens": 1, "temperature": 0},
    {"model": "deepseek-flash", "max_tokens": CHAT_MAX_TOKENS, "temperature": 2},
])
def test_current_chat_parameter_compatibility_preserves_optional_values(parameters):
    assert chat_parameter_diagnostic(parameters) is None


@pytest.mark.parametrize("parameters", [
    {"model": "deepseek-flash", "max_tokens": 8193},
    {"model": "deepseek-flash", "max_tokens": 2.5},
    {"model": "deepseek-flash", "temperature": float("inf")},
    {"model": "deepseek-flash", "stream": True},
])
def test_current_chat_parameter_compatibility_is_explicit_and_safe(parameters):
    assert chat_parameter_diagnostic(parameters) == "model_parameters_unsupported"


def test_diagnostics_detect_revoked_current_reference_without_rewriting_historical_provider(tmp_path):
    with closing(WorkflowService(tmp_path / "model.sqlite")) as service:
        record = model_config()
        service.save_model_configuration("model", **mutation(record))
        original = service.get_model_configuration("provider", DEFAULT_PROVIDER_ID, 1)
        revoked = {**original, "revision": 2, "credential_ref": None}
        service.save_model_configuration("provider", **mutation(revoked, 1, "credential-revoked"))
        assert service.diagnose_model_configuration(uid(1), 1)["diagnostics"] == [
            {"target": uid(2), "code": "credential_reference_missing"},
        ]
        assert service.get_model_configuration("model", uid(1), 1) == record
        assert service.get_model_configuration("provider", DEFAULT_PROVIDER_ID, 1) == original


def test_diagnostics_keep_old_parameter_config_readable_but_block_unsupported_execution(tmp_path):
    with closing(WorkflowService(tmp_path / "model.sqlite")) as service:
        record = model_config()
        record["nodes"][0]["parameters"]["max_tokens"] = 9000
        assert service.save_model_configuration("model", **mutation(record)) == record
        with running_server(service) as port:
            path = f"/api/model-configurations/model/{uid(1)}/revisions/1"
            assert request(port, "GET", path) == (200, record)
            status, body = request(port, "GET", path + "/diagnostics")
            assert status == 200
            assert body["diagnostics"] == [
                {"target": uid(2), "code": "model_parameters_unsupported"},
            ]
        assert service.get_model_configuration("model", uid(1), 1) == record


def test_configuration_transaction_rolls_back_and_same_request_can_retry(tmp_path):
    fail = True

    def inject(point):
        nonlocal fail
        if point == "model_configuration_before_commit" and fail:
            fail = False
            raise RuntimeError("PRIVATE failure")

    with closing(SqliteStore(tmp_path / "model.sqlite", fault_injector=inject)) as store:
        catalog = ModelConfigurationStore(store)
        with pytest.raises(RuntimeError):
            catalog.write("provider", default_provider(), expected_revision=0, idempotency_key="first")
        assert catalog.list_providers() == []
        assert catalog.write("provider", default_provider(), expected_revision=0, idempotency_key="first") == default_provider()


def test_http_roundtrip_origin_errors_and_no_workflow_dispatch(tmp_path):
    calls = []
    with closing(WorkflowService(tmp_path / "model.sqlite", model_factory=lambda stage: calls.append(stage))) as service:
        sid = service.create_session()["workflow_session_id"]
        before = service.get_session(sid)
        with running_server(service) as port:
            assert request(port, "GET", "/api/model-configurations/provider") == (200, [default_provider()])
            assert request(port, "POST", "/api/model-configurations/model", mutation(model_config())) == (201, model_config())
            path = f"/api/model-configurations/model/{uid(1)}/revisions/1"
            assert request(port, "GET", path) == (200, model_config())
            assert request(port, "GET", path + "/diagnostics")[1]["execution_supported"] is False
            assert request(port, "POST", "/api/model-configurations/model", mutation(model_config()), origin=False)[0] == 403
            assert request(port, "GET", path + "?latest=1")[0] == 404
            invalid = {**default_provider(), "api_key": "PRIVATE-KEY"}
            status, error = request(port, "POST", "/api/model-configurations/provider", mutation(invalid))
            assert status == 400 and error["error"]["reason_code"] == "invalid_request"
            assert "PRIVATE" not in str(error)
        assert service.get_session(sid) == before and calls == []


def test_corrupt_configuration_is_redacted_storage_error(tmp_path):
    database = tmp_path / "model.sqlite"
    with closing(WorkflowService(database)) as service:
        with closing(sqlite3.connect(database)) as raw:
            raw.execute("UPDATE model_configuration_revisions SET payload = ?", ('{"PRIVATE":true}',))
            raw.commit()
        with running_server(service) as port:
            status, body = request(port, "GET", "/api/model-configurations/provider")
            assert status == 500 and body["error"]["reason_code"] == "storage_contract_violation"
            assert "PRIVATE" not in str(body)


def test_migrate_v8_preserves_workflow_records_and_bootstrap_does_not_overwrite(tmp_path):
    database = tmp_path / "model.sqlite"
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        before = service.get_session(sid)
    with closing(sqlite3.connect(database)) as raw:
        raw.execute("DROP TABLE model_configuration_revisions")
        raw.execute("DROP TABLE model_configuration_mutations")
        raw.execute("PRAGMA user_version = 8")
        raw.commit()
    with closing(WorkflowService(database)) as service:
        assert service.get_session(sid) == before
        assert service.list_model_providers() == [default_provider()]
