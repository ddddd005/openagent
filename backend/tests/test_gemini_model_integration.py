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
from test_model_package import source_config, uid
from test_models_service_integration import ModelDatabaseFixture


LEGACY_CONTRACT_DIGESTS = {
    ("global", "workflow.chat-provider", 1):
        "json-v1:sha256:f34776baa4de4121f07669a828d71cac5bf01c228f44727e5e0516940c0e0bcb",
    ("content", "MODEL_BINDING", 1):
        "json-v1:sha256:6abfc3359c03f054fbe9ba6fb2a3f638efa78af947d730bd0d09ac6422969cf6",
    ("content", "MODEL_BINDING", 2):
        "json-v1:sha256:d19506a06bca810eab3450dc8103ed6b75fefde0dade03db63d3c9e505aefc96",
    ("content", "MODEL_RESULT", 1):
        "json-v1:sha256:cbcadeba1191e3dfe7516b8d3ff47ff5ce3740f29a11482862e88820e22c69ed",
}


def write_provider(service):
    record = {**source_config()["reference"], "data_schema_version": 2, "update_sequence": 1,
              "value": {"name": "Offline Gemini", "protocol": "gemini",
                        "base_url": "https://generativelanguage.googleapis.com/v1beta",
                        "credential_ref": "env:GEMINI_API_KEY", "enabled": True}}
    service.save_global_resource(record, expected_sequence=0, idempotency_key=str(uuid4()))
    return record


def document(service, **kwargs):
    doc = ModelDatabaseFixture.document(service, **kwargs)
    for node in doc["nodes"]:
        if node["component_id"] == "models.source":
            node["component_version"] = "3"
            node["config"]["parameters"] = deepcopy(PARAMETERS)
        if node["component_id"] == "models.chat":
            node["component_version"] = "2"
        if node["component_id"] == "prompts.group":
            node["config"]["members"][0]["presentation"]["role"] = "user"
    return doc


def test_registered_gemini_chat_thinking_result_and_facts_survive_sqlite_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "private-gemini-fixture-secret")
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
        assert service.registry.get("models.source", "3") is not None
        assert service.registry.get("models.source", "4") is not None
        assert service.registry.get("models.chat", "2") is not None
        write_provider(service)
        final = ModelDatabaseFixture.execute(service, ModelDatabaseFixture.create(service, document(service)))
        assert final["status"] == "succeeded", final
        result = ModelDatabaseFixture.result(final)
        assert result["schema_version"] == 2
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


def test_legacy_registered_type_declarations_are_unchanged_and_existing_evidence_reopens(tmp_path):
    # These digests are the stable fc225390 declarations, not newly computed expectations.
    definitions = {(value.scope, value.type_id, value.schema_version): value
                   for value in model_type_definitions()}
    path = tmp_path / "old-contracts.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        initialize_type_contract_tables(connection)
        for key, digest in LEGACY_CONTRACT_DIGESTS.items():
            declaration = definitions[key].to_dict()
            assert content_digest(declaration) == digest
            connection.execute("INSERT INTO registered_type_contracts VALUES(?,?,?,?,?)",
                               (*key, digest, canonical_bytes(declaration).decode("utf-8")))
        connection.commit()
    with closing(GraphWorkflowService(path)) as service:
        assert service.registry.data_types.get("workflow.chat-provider", 1, scope="global") is not None
        assert service.registry.data_types.get("workflow.chat-provider", 2, scope="global") is not None
        assert service.registry.data_types.get("MODEL_BINDING", 4) is not None
    with closing(sqlite3.connect(path)) as connection:
        assert {(scope, type_id, version): digest for scope, type_id, version, digest
                in connection.execute("SELECT scope,type_id,schema_version,digest FROM registered_type_contracts")
                if (scope, type_id, version) in LEGACY_CONTRACT_DIGESTS} == LEGACY_CONTRACT_DIGESTS


def test_gemini_same_process_pause_before_dispatch_resumes_without_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "private-gemini-fixture-secret")
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


def test_source_protocol_mismatch_preflight_prevents_all_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "private-gemini-fixture-secret")
    effects = []
    with closing(GraphWorkflowService(tmp_path / "wrong-source.sqlite")) as service:
        write_provider(service)
        doc = document(service, side_effect=effects)
        doc["nodes"][0]["component_version"] = "1"
        # A source-one/chat-one graph still compiles, but cannot dispatch Gemini under old schemas.
        for node in doc["nodes"]:
            if node["component_id"] == "models.chat":
                node["component_version"] = "1"
        initial = ModelDatabaseFixture.create(service, doc)
        with pytest.raises(ContractValidationError) as error:
            service.start(initial["workflow_session_id"], expected_revision=initial["revision"],
                          idempotency_key=str(uuid4()))
        assert error.value.reason_code == "model_source_protocol_mismatch"
        assert not effects


def test_gemini_native_binding_and_compaction_use_same_actual_wire_estimate():
    observed = []
    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json=response([{"text": "short summary"}]))
    value = fixture(handler)
    value.service.release_run(uid(2), uid(3))
    value.config["capacity"] = {
        "context_window_tokens": 32768, "output_reserve_tokens": 1024,
        "summary_max_tokens": 128, "max_cold_input_tokens": 10000,
    }
    value.service.prepare_run(workflow_session_id=uid(2), chain_run_id=uid(3),
                              node_configs={uid(10): value.config}, records=[value.record])
    value.binding = value.service.bind_model(value.source, value.config)
    value.service.accept_binding_output(workflow_session_id=uid(2), chain_run_id=uid(3),
                                       node_run_id=uid(11), binding_id=value.binding["binding_id"],
                                       output_id=uid(30))
    result = value.service.call_model(
        value.consumer, binding_id=value.binding["binding_id"],
        messages=[{"kind": "text", "role": "user", "content": "Summarize"}],
        tools=[], request_key="compact", purpose="compaction")
    assert value.binding["schema_version"] == 4
    assert result["content"] == "short summary"
    details = value.facts[0]["details"]
    assert observed == [details["wire_request"]]
    assert details["cold_input_estimate"]["input_tokens"] == len(canonical_bytes(observed[0]))
    assert observed[0]["generationConfig"]["maxOutputTokens"] == 128
    assert observed[0]["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "low", "includeThoughts": True}
    value.service.close()
