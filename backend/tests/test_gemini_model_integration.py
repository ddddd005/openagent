"""Registered Gemini source/Chat and run facts roundtrip through fresh SQLite reads."""

from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from threading import Event
from uuid import uuid4

import httpx
import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, content_digest
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.model_service import create_chat_transport
from phase1_agent.model_contract import model_type_definitions
from phase1_agent.type_contract_store import initialize_type_contract_tables
from test_gemini_adapter import PARAMETERS, fixture, response
from test_model_package import profile_config, source_config, uid
from test_models_service_integration import ModelDatabaseFixture
from credential_test_support import seed_service_credential


LEGACY_CONTRACT_DIGESTS = {
    ("content", "MODEL_BINDING", 2):
        "json-v1:sha256:d19506a06bca810eab3450dc8103ed6b75fefde0dade03db63d3c9e505aefc96",
    ("content", "MODEL_RESULT", 1):
        "json-v1:sha256:cbcadeba1191e3dfe7516b8d3ff47ff5ce3740f29a11482862e88820e22c69ed",
}
CURRENT_CONTRACT_DIGESTS = {
    ("content", "MODEL_BINDING", 5):
        "json-v1:sha256:5b01975a9c13d1219a5bf852c403c6f486a4d79f59d242d0e1af42f8bdff9a02",
    ("content", "MODEL_RESULT", 3):
        "json-v1:sha256:bdc67eb66fe94990402a8865c4f6ecb969f4ed902bfa520fca8db2dfb8304955",
}


def write_provider(service):
    reference = seed_service_credential(service, secret="private-gemini-fixture-secret")
    record = {**profile_config()["provider"], "data_schema_version": 3, "update_sequence": 1,
              "value": {"name": "Offline Gemini", "protocol": "gemini",
                        "base_url": "https://generativelanguage.googleapis.com/v1beta",
                        "credential_ref": reference, "enabled": True}}
    service.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
    profile = profile_config()
    profile["model"]["id"] = PARAMETERS["model"]
    profile["model"]["capacity"]["max_output_tokens"] = PARAMETERS["max_tokens"]
    profile["runtime"]["thinking"] = deepcopy(PARAMETERS["thinking"])
    profile["runtime"]["output_limit_tokens"] = PARAMETERS["max_tokens"]
    service.save_global_resource({
        **source_config()["reference"], "data_schema_version": 2, "update_sequence": 1, "value": profile,
    }, expected_sequence=0, idempotency_key=str(uuid4()))
    return record


def document(service, **kwargs):
    doc = ModelDatabaseFixture.document(service, **kwargs)
    for node in doc["nodes"]:
        if node["component_id"] == "prompts.group":
            node["config"]["members"][0]["presentation"]["role"] = "user"
    return doc


def test_registered_gemini_chat_thinking_result_and_facts_survive_sqlite_reopen(tmp_path, monkeypatch):
    observed = []
    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json=response([
            {"text": "Visible summary", "thought": True, "thoughtSignature": "fixture-summary-signature"},
            {"text": "Answer", "thoughtSignature": "fixture-answer-signature"},
        ]))
    def factory(**kwargs):
        return create_chat_transport(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    path = tmp_path / "gemini.sqlite"
    with closing(GraphWorkflowService(path, public_model_factory=factory)) as service:
        assert service.registry.get("models.source", "7") is not None
        assert service.registry.get("models.chat", "5") is not None
        write_provider(service)
        final = ModelDatabaseFixture.execute(service, ModelDatabaseFixture.create(service, document(service)))
        assert final["status"] == "succeeded", final
        result = ModelDatabaseFixture.result(final)
        assert result["schema_version"] == 3
        assert result["content"] == "Answer"
        assert result["thinking_summary"] == "Visible summary"
        history = service.get_run(final["workflow_session_id"], final["selected_chain_run_id"])
        assert observed == [history["runtime_facts"][0]["details"]["wire_request"]]
        assert history["runtime_facts"][0]["details"]["projection"] == "gemini-generate-content@1"
        assert not service._service_runs
        assert "private-gemini-fixture-secret" not in canonical_bytes(history).decode("utf-8")
    with closing(GraphWorkflowService(path, public_model_factory=factory)) as reader:
        reread = reader.get_run(final["workflow_session_id"], final["selected_chain_run_id"])
        assert reread == history
        assert reread["runtime_facts"][-1]["details"]["response"]["provider_metadata"] == result["provider_metadata"]
        assert not reader._service_runs
    assert len(observed) == 1


def test_current_registered_type_declarations_roundtrip_through_sqlite(tmp_path):
    definitions = {(value.scope, value.type_id, value.schema_version): value
                   for value in model_type_definitions()}
    assert all(key not in definitions for key in LEGACY_CONTRACT_DIGESTS)
    path = tmp_path / "old-contracts.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        initialize_type_contract_tables(connection)
        for key, digest in CURRENT_CONTRACT_DIGESTS.items():
            declaration = definitions[key].to_dict()
            assert content_digest(declaration) == digest
            connection.execute("INSERT INTO registered_type_contracts VALUES(?,?,?,?,?)",
                               (*key, digest, canonical_bytes(declaration).decode("utf-8")))
        connection.commit()
    with closing(GraphWorkflowService(path)) as service:
        assert service.registry.data_types.get("workflow.chat-provider", 1, scope="global") is None
        assert service.registry.data_types.get("workflow.chat-provider", 2, scope="global") is None
        assert service.registry.data_types.get("workflow.chat-provider", 3, scope="global") is not None
        assert service.registry.data_types.get("MODEL_BINDING", 5) is not None
        assert service.registry.data_types.get("MODEL_RESULT", 3) is not None
    with closing(sqlite3.connect(path)) as connection:
        assert {(scope, type_id, version): digest for scope, type_id, version, digest
                in connection.execute("SELECT scope,type_id,schema_version,digest FROM registered_type_contracts")
                if (scope, type_id, version) in CURRENT_CONTRACT_DIGESTS} == CURRENT_CONTRACT_DIGESTS


def test_gemini_same_process_pause_before_dispatch_resumes_without_replay(tmp_path, monkeypatch):
    entered, released = Event(), Event()
    observed = []
    def factory(**kwargs):
        def handler(request):
            observed.append(request)
            return httpx.Response(200, json=response([{"text": "Answer"}]))
        return create_chat_transport(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with closing(GraphWorkflowService(tmp_path / "pause.sqlite", public_model_factory=factory)) as service:
        write_provider(service)
        initial = ModelDatabaseFixture.create(service, document(service, gate=(entered, released)))
        started = service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                                idempotency_key=str(uuid4()))
        try:
            assert entered.wait(10)
            current = service.get_session(initial["workflow_session_id"])
            service.control(current["workflow_session_id"], action="pause",
                            expected_revision=current["revision"], idempotency_key=str(uuid4()))
        finally:
            released.set()
        service.wait(started["active_chain_run_id"])
        paused = service.get_session(initial["workflow_session_id"])
        assert paused["status"] == "paused"
        assert not observed
        resumed = service.control(paused["workflow_session_id"], action="resume",
                                  expected_revision=paused["revision"], idempotency_key=str(uuid4()))
        service.wait(resumed["active_chain_run_id"])
        assert service.get_session(initial["workflow_session_id"])["status"] == "succeeded"
    assert len(observed) == 1


def test_missing_current_model_profile_reference_prevents_graph_effects(tmp_path, monkeypatch):
    effects = []
    with closing(GraphWorkflowService(tmp_path / "wrong-source.sqlite")) as service:
        write_provider(service)
        doc = document(service, side_effect=effects)
        source = next(node for node in doc["nodes"] if node["component_id"] == "models.source")
        source["config"]["reference"]["resource_id"] = uid(90)
        initial = ModelDatabaseFixture.create(service, doc)
        with pytest.raises(ContractValidationError) as error:
            service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                          idempotency_key=str(uuid4()))
        assert error.value.reason_code == "global_resource_missing"
        assert not effects


def test_gemini_native_binding_and_compaction_use_same_actual_wire_estimate():
    observed = []
    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json=response([{"text": "short summary"}]))
    value = fixture(handler)
    value.profile["value"]["model"]["capacity"] = {
        "context_window_tokens": 32768, "input_limit_tokens": 10000, "max_output_tokens": 1024,
    }
    value.profile["value"]["runtime"].update(context_length_tokens=32768, output_limit_tokens=128)
    prepared = value.prepare(key="compact", purpose="compaction",
                             messages=[{"kind": "text", "role": "user", "content": "Summarize"}])
    result = value.dispatch(prepared, key="compact")
    assert value.binding["schema_version"] == 5
    assert result["content"] == "short summary"
    details = next(fact for fact in value.accepted.values()
                   if fact["stage"] == "request")["details"]
    assert observed == [details["wire_request"]]
    assert details["measurement"]["input_tokens"] == len(canonical_bytes(observed[0]))
    assert observed[0]["generationConfig"]["maxOutputTokens"] == 128
    assert observed[0]["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "low", "includeThoughts": True}
    value.close()
