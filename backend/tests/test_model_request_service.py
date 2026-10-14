"""Synthetic reusable M2 harness; no product database, vault or network."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
from threading import Barrier, RLock
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from phase1_agent.adapter import DeepSeekAdapter
from phase1_agent.contract_errors import ContractValidationError, DiagnosticError, require
from phase1_agent.contract_json import canonical_bytes, loads_strict
from phase1_agent.contracts import ModelResponse
from phase1_agent.credential_contract import CredentialLease
from phase1_agent.gemini_adapter import GeminiAdapter
from phase1_agent.model_request_budget import validate_context_reduction
from phase1_agent.model_request_service import ModelRequestService, ModelRequestTransport
from test_model_request_contracts import IDS, reference, synthetic_capability, synthetic_profile


class FakeBroker:
    def __init__(self):
        self.values = {}
        self.freeze_calls, self.authorize_calls, self.release_calls = [], [], []
        self.on_freeze = self.on_authorize = None
        self.fail_release = False

    def save(self, reference, secret):
        generation = self.values.get(reference, ("", 0))[1] + 1
        self.values[reference] = (secret, generation)

    def freeze(self, reference):
        self.freeze_calls.append(reference)
        lease = CredentialLease(reference, str(self.values[reference][1]).encode())
        if self.on_freeze is not None:
            callback, self.on_freeze = self.on_freeze, None
            callback()
        return lease

    def authorize(self, lease):
        self.authorize_calls.append(lease.reference)
        secret, generation = self.values[lease.reference]
        if self.on_authorize is not None:
            callback, self.on_authorize = self.on_authorize, None
            callback()
        require(lease.evidence == str(generation).encode(), "model_credential_changed", secret)
        return secret

    def release(self, lease):
        if self.fail_release:
            raise DiagnosticError("credential_release_failed", "synthetic-private-key")
        self.release_calls.append(lease.reference)


class FakeTransport:
    max_retries = 0

    def __init__(self, harness, provider, parameters, api_key):
        self.harness = harness
        self.model_parameters = deepcopy(parameters)
        self.provider_address = str(httpx.URL(provider["base_url"])).rstrip("/")
        self.provider, self.api_key = deepcopy(provider), api_key
        self.closed = 0

    def generate_prepared(self, prepared):
        self.harness.communications.append({
            "provider": deepcopy(self.provider), "parameters": deepcopy(self.model_parameters),
            "api_key": self.api_key, "prepared": deepcopy(prepared),
        })
        if self.harness.on_communication is not None:
            self.harness.on_communication()
        if self.harness.transport_error is not None:
            raise self.harness.transport_error
        if self.harness.response is not None:
            return self.harness.response
        model = self.model_parameters["model"]
        metadata = None
        if self.provider["protocol"] == "gemini":
            metadata = {"schema_version": 1, "provider": "gemini", "protocol": "gemini-generate-content@1",
                        "model": model, "content": {"role": "model", "parts": [{"text": "answer"}]},
                        "tool_call_bindings": []}
        return ModelResponse("stop", "answer", model=model, provider_metadata=metadata)

    def close(self):
        if self.harness.fail_close:
            raise DiagnosticError("synthetic_close_failed", self.api_key)
        self.closed += 1


class ServiceHarness:
    """Constructor callbacks can be replaced with the real trusted SQLite reader."""

    def __init__(self, database=None):
        self.context = SimpleNamespace(**{key: str(uuid4()) for key in (
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")})
        self.owner = tuple(vars(self.context).values())
        self.binding = {"schema_version": 5, "kind": "workflow.model-binding", "binding_id": IDS[4],
                        "reference": reference("workflow.model-profile", IDS[1]),
                        "authorization": {"purposes": ["normal", "compaction"]}}
        self.output_id, self.producer_run_id = str(uuid4()), str(uuid4())
        self.input_refs = {"model": [{"output_id": self.output_id}], "prompt": [{"output_id": str(uuid4())}]}
        self.grants = {("models:call", operation) for operation in (
            "prepare-request", "dispatch-request", "discard-prepared-request")}
        self.grants.add(("model-projection:read", "measure-context"))
        self.profile = {**self.binding["reference"], "data_schema_version": 2,
                        "update_sequence": 1, "value": synthetic_profile()}
        credential_ref = "local:" + IDS[8]
        self.provider = {**self.profile["value"]["provider"], "data_schema_version": 3, "update_sequence": 1,
                         "value": {"name": "Synthetic", "protocol": "chat",
                                   "base_url": "https://first.example.invalid", "credential_ref": credential_ref,
                                   "enabled": True}}
        self.broker = FakeBroker()
        self.broker.save(credential_ref, "synthetic-private-key")
        self.reads, self.checks, self.projections, self.created, self.communications = [], [], [], [], []
        self.accepted, self.fact_calls = {}, []
        self.transport_error = self.response = self.on_communication = self.factory_hook = None
        self.fail_stage = self.controlled_fact_error = self.drop_ack_stage = None
        self.fail_close = False
        self.auth_barrier = None
        self.counter = self.estimator_id = self.estimator_version = None
        self.capability_override = None
        self._fact_lock = RLock()
        self.connection = sqlite3.connect(str(database), check_same_thread=False) if database is not None else None
        if self.connection is not None:
            self.connection.execute("CREATE TABLE model_test_facts(fact_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.service = self.make_service()

    def make_service(self, **overrides):
        callbacks = dict(authorize_binding=self.authorize_binding, read_resource_bundle=self.read_bundle,
                         check_resource_bundle=self.check_bundle, accept_fact=self.accept_fact,
                         credential_broker=self.broker, resolve_transport=self.resolve_transport)
        callbacks.update(overrides)
        return ModelRequestService(**callbacks)

    def authorize_binding(self, context, capability, operation, binding_id):
        require(tuple(vars(context).values()) == self.owner, "model_owner_mismatch", "Fixture owner differs")
        require((capability, operation) in self.grants, "graph_capability_denied", "Fixture grant missing")
        require(binding_id == self.binding["binding_id"], "model_binding_mismatch", "Fixture binding differs")
        if self.auth_barrier is not None:
            self.auth_barrier.wait(timeout=5)
        return {"binding": deepcopy(self.binding), "binding_output_id": self.output_id,
                "producer_node_run_id": self.producer_run_id, "input_refs": deepcopy(self.input_refs)}

    def read_bundle(self, context, root_reference, binding_output_id):
        require(root_reference == self.binding["reference"] and binding_output_id == self.output_id,
                "model_binding_unaccepted", "Fixture does not authorize this root")
        self.reads.append((deepcopy(root_reference), binding_output_id))
        return {"profile": deepcopy(self.profile), "provider": deepcopy(self.provider)}

    def check_bundle(self, context, root_reference, binding_output_id, revisions):
        self.checks.append(deepcopy(revisions))
        return (revisions["configuration"] == {
            "reference": self.binding["reference"], "update_sequence": self.profile["update_sequence"]}
            and revisions["provider"] == {
                "reference": self.profile["value"]["provider"], "update_sequence": self.provider["update_sequence"]})

    def resolve_transport(self, protocol, model):
        capability = (synthetic_capability(protocol, model) if self.capability_override is None
                      else self.capability_override)
        projector = GeminiAdapter if protocol == "gemini" else DeepSeekAdapter
        version = "gemini-generate-content@1" if protocol == "gemini" else "deepseek-chat@1"

        def project(messages, tools, parameters):
            self.projections.append((deepcopy(messages), deepcopy(tools), deepcopy(parameters)))
            return projector.prepare_request(messages, tools, parameters)

        return ModelRequestTransport(capability, version, project, self.create_transport,
                                     self.counter, self.estimator_id, self.estimator_version)

    def create_transport(self, *, provider, parameters, api_key):
        transport = FakeTransport(self, provider, parameters, api_key)
        self.created.append(transport)
        if self.factory_hook is not None:
            self.factory_hook(transport)
        return transport

    def accept_fact(self, context, fact):
        with self._fact_lock:
            self.fact_calls.append(deepcopy(fact))
            require(fact["session_id"] == context.workflow_session_id
                    and fact["chain_run_id"] == context.chain_run_id
                    and fact["node_binding_id"] == context.node_binding_id
                    and fact["node_run_id"] == context.node_run_id,
                    "runtime_fact_owner_mismatch", "Fixture fact owner differs")
            if fact["stage"] == self.fail_stage:
                self.fail_stage = None
                if self.controlled_fact_error is not None:
                    raise DiagnosticError(self.controlled_fact_error, "synthetic-private-key")
                raise OSError("synthetic-private-key")
            existing = self.accepted.get(fact["fact_id"])
            require(existing is None or canonical_bytes(existing) == canonical_bytes(fact),
                    "runtime_fact_conflict", "Fixture original fact differs")
            self.accepted[fact["fact_id"]] = deepcopy(fact)
            if self.connection is not None:
                self.connection.execute("INSERT OR IGNORE INTO model_test_facts VALUES (?, ?)",
                                        (fact["fact_id"], canonical_bytes(fact).decode()))
                self.connection.commit()
            if fact["stage"] == self.drop_ack_stage:
                self.drop_ack_stage = None
                raise OSError("synthetic committed fact acknowledgment loss")
            return {"fact_id": fact["fact_id"]}

    def prepare(self, key="normal:1", purpose="normal", messages=None, tools=None):
        return self.service(self.context, "models:call", "prepare-request", {
            "binding_id": self.binding["binding_id"], "request_key": key, "purpose": purpose,
            "messages": [{"kind": "text", "role": "user", "content": "fixture"}] if messages is None else messages,
            "tools": [] if tools is None else tools})

    def dispatch(self, prepared, key="normal:1", context=None):
        return self.service(context or self.context, "models:call", "dispatch-request", {
            "binding_id": self.binding["binding_id"], "request_key": key,
            "preparation_id": prepared["preparation_id"]})

    def measure(self, prepared, messages):
        return self.service(self.context, "model-projection:read", "measure-context", {
            "binding_id": self.binding["binding_id"], "preparation_id": prepared["preparation_id"],
            "messages": messages, "tools": []})

    def close(self):
        self.service.release_run(self.context.workflow_session_id, self.context.chain_run_id)
        if self.connection is not None:
            self.connection.close()


@pytest.fixture
def harness(tmp_path):
    instance = ServiceHarness(tmp_path / "synthetic-facts.sqlite")
    yield instance
    instance.fail_close = instance.broker.fail_release = False
    instance.close()


def test_two_new_keys_read_saved_parameter_provider_address_and_private_key(harness):
    first = harness.prepare()
    assert first["settings"]["parameters"]["max_tokens"] == 32_000
    assert not harness.accepted and not harness.communications
    original = deepcopy(first)
    harness.profile["update_sequence"] += 1
    harness.profile["value"]["runtime"]["output_limit_tokens"] = 16_000
    harness.profile["value"]["runtime"]["context_length_tokens"] = 250_000
    harness.provider["update_sequence"] += 1
    harness.provider["value"]["base_url"] = "https://second.example.invalid"
    first_result = harness.dispatch(first)
    second = harness.prepare(key="normal:2")
    harness.dispatch(second, key="normal:2")
    assert first == original
    assert first_result["settings_id"] == first["settings"]["settings_id"]
    assert harness.communications[0]["provider"]["base_url"] == "https://first.example.invalid"
    assert harness.communications[1]["provider"]["base_url"] == "https://second.example.invalid"
    assert harness.communications[1]["parameters"]["max_tokens"] == 16_000
    assert second["settings"]["configuration"]["update_sequence"] == 2
    assert second["settings"]["provider"]["update_sequence"] == 2
    assert len(harness.reads) == len(harness.checks) == 2
    assert all(adapter.closed == 1 for adapter in harness.created)
    assert len(harness.broker.release_calls) == 2


def test_provider_rebinding_reads_only_current_root_dependency_on_new_key(harness):
    first = harness.prepare()
    harness.dispatch(first)
    new_provider = reference("workflow.chat-provider", IDS[9])
    harness.profile["value"]["provider"] = new_provider
    harness.profile["update_sequence"] += 1
    harness.provider = {**harness.provider, **new_provider, "update_sequence": 1,
                        "value": {**harness.provider["value"], "credential_ref": "local:" + IDS[10],
                                  "base_url": "https://third.example.invalid"}}
    harness.broker.save("local:" + IDS[10], "synthetic-new-private-key")
    second = harness.prepare(key="normal:2")
    harness.dispatch(second, key="normal:2")
    assert second["settings"]["provider"]["reference"] == new_provider
    assert harness.communications[-1]["api_key"] == "synthetic-new-private-key"
    assert first["settings"]["provider"]["reference"] != second["settings"]["provider"]["reference"]


def test_key_generation_changes_block_old_lease_and_new_key_captures_new_lease(harness):
    first = harness.prepare()
    reference = harness.provider["value"]["credential_ref"]
    harness.broker.save(reference, "synthetic-generation-two")
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(first)
    assert error.value.reason_code == "model_not_dispatched"
    assert error.value.diagnostics[0]["cause_code"] == "model_credential_changed"
    assert not harness.communications
    assert [fact["details"].get("classification") for fact in harness.accepted.values()][-1] == "not_dispatched"
    with pytest.raises(DiagnosticError):
        harness.dispatch(first)
    second = harness.prepare(key="normal:2")
    harness.dispatch(second, key="normal:2")
    assert len(harness.communications) == 1
    assert harness.communications[0]["api_key"] == "synthetic-generation-two"
    assert len(harness.broker.freeze_calls) == 2


@pytest.mark.parametrize("moment", ["freeze", "authorize"])
def test_resource_revision_race_after_capture_rejects_without_rereading(harness, moment):
    def save():
        harness.profile["update_sequence"] += 1
        harness.profile["value"]["runtime"]["output_limit_tokens"] = 16_000

    setattr(harness.broker, "on_" + moment, save)
    with pytest.raises(DiagnosticError) as error:
        harness.prepare()
    assert error.value.reason_code == "model_request_configuration_changed_before_prepare"
    assert error.value.diagnostics[0]["step"] == "configuration_revision_check"
    assert len(harness.reads) == len(harness.checks) == len(harness.broker.freeze_calls) == 1
    assert len(harness.broker.release_calls) == 1
    with pytest.raises(DiagnosticError):
        harness.prepare()
    assert len(harness.reads) == 1 and not harness.communications


def test_old_completed_key_precedes_latest_configuration_and_projection(harness):
    prepared = harness.prepare()
    result = harness.dispatch(prepared)
    snapshot = (len(harness.reads), len(harness.projections), len(harness.broker.authorize_calls))
    harness.profile["value"]["runtime"]["output_limit_tokens"] = 0
    harness.provider["value"]["enabled"] = False
    harness.broker.values.clear()
    assert harness.prepare() == prepared
    assert harness.dispatch(prepared) == result
    assert snapshot == (len(harness.reads), len(harness.projections), len(harness.broker.authorize_calls))
    assert len(harness.communications) == 1


def test_original_intent_and_artifact_refs_are_immutable_for_old_key(harness):
    prepared = harness.prepare()
    with pytest.raises(DiagnosticError) as error:
        harness.prepare(messages=[{"kind": "text", "role": "user", "content": "changed"}])
    assert error.value.reason_code == "model_request_conflict"
    harness.input_refs["prompt"] = [{"output_id": str(uuid4())}]
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(prepared)
    assert error.value.reason_code == "model_request_conflict"
    assert len(harness.reads) == 1 and not harness.communications


@pytest.mark.parametrize("mutation", ["owner", "binding", "grant", "preparation"])
def test_owned_binding_exact_output_grant_and_preparation_are_required(harness, mutation):
    prepared = harness.prepare()
    if mutation == "owner":
        context = SimpleNamespace(**{**vars(harness.context), "chain_run_id": str(uuid4())})
        with pytest.raises(DiagnosticError):
            harness.dispatch(prepared, context=context)
    elif mutation == "binding":
        harness.binding["reference"] = reference("workflow.model-profile", IDS[9])
        with pytest.raises(DiagnosticError):
            harness.dispatch(prepared)
    elif mutation == "grant":
        harness.grants.remove(("models:call", "dispatch-request"))
        with pytest.raises(DiagnosticError):
            harness.dispatch(prepared)
    else:
        with pytest.raises(DiagnosticError) as error:
            harness.dispatch({**prepared, "preparation_id": str(uuid4())})
        assert error.value.reason_code == "model_recovery_unavailable"
    assert not harness.communications


def test_discard_closes_private_lease_and_never_accepts_reserved_fact_ids(harness):
    prepared = harness.prepare()
    payload = {"binding_id": harness.binding["binding_id"], "request_key": "normal:1",
               "preparation_id": prepared["preparation_id"]}
    result = harness.service(harness.context, "models:call", "discard-prepared-request", payload)
    assert result == {"request_id": prepared["request_id"], "discarded": True}
    assert harness.service(harness.context, "models:call", "discard-prepared-request", payload) == result
    assert not harness.accepted and not harness.created and not harness.communications
    assert len(harness.broker.release_calls) == 1
    with pytest.raises(DiagnosticError) as error:
        harness.prepare()
    assert error.value.reason_code == "model_prepared_request_discarded"
    with pytest.raises(DiagnosticError):
        harness.dispatch(prepared)


@pytest.mark.parametrize("ack_lost", [False, True])
def test_success_outcome_acceptance_retry_uses_original_response_and_three_exact_fact_ids(harness, ack_lost):
    prepared = harness.prepare()
    if ack_lost:
        harness.drop_ack_stage = "outcome"
    else:
        harness.fail_stage = "outcome"
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(prepared)
    assert error.value.reason_code == "model_fact_acceptance_failed"
    harness.profile["value"]["runtime"]["output_limit_tokens"] = 0
    harness.broker.values.clear()
    result = harness.dispatch(prepared)
    assert len(harness.communications) == 1 and len(harness.reads) == 1
    assert result["request_id"] == prepared["request_id"]
    assert result["settings_id"] == prepared["settings"]["settings_id"]
    assert result["fact_refs"] == [{"fact_id": prepared["reserved_model_fact_ids"][stage]}
                                  for stage in ("request", "attempt", "outcome")]
    outcome_calls = [fact for fact in harness.fact_calls if fact["stage"] == "outcome"]
    assert len(outcome_calls) == 2 and outcome_calls[0] == outcome_calls[1]
    assert len(harness.accepted) == 3
    stored = [loads_strict(row[0]) for row in harness.connection.execute("SELECT payload FROM model_test_facts")]
    assert {fact["fact_id"] for fact in stored} == {ref["fact_id"] for ref in result["fact_refs"]}


def test_host_recovery_retries_only_the_retained_model_fact_acceptance(harness):
    harness.fail_stage = "outcome"
    prepared = harness.prepare()
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(prepared)
    assert error.value.reason_code == "model_fact_acceptance_failed"
    assert harness.service.has_pending_acceptance(harness.context) is True
    assert len(harness.communications) == 1

    result = harness.service.retry_acceptance(harness.context)
    assert result["fact_refs"] == [{"fact_id": prepared["reserved_model_fact_ids"][stage]}
                                   for stage in ("request", "attempt", "outcome")]
    assert harness.service.has_pending_acceptance(harness.context) is False
    assert len(harness.communications) == 1


@pytest.mark.parametrize("stage", ["request", "attempt"])
def test_pre_dispatch_fact_failure_preserves_controlled_cause_and_never_communicates(harness, stage):
    prepared = harness.prepare()
    harness.fail_stage, harness.controlled_fact_error = stage, "runtime_fact_owner_mismatch"
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(prepared)
    assert error.value.reason_code == "model_not_dispatched"
    assert error.value.diagnostics[0]["cause_code"] == "runtime_fact_owner_mismatch"
    assert error.value.diagnostics[0]["step"] == stage + "_fact_acceptance"
    assert b"synthetic-private-key" not in canonical_bytes(error.value.diagnostics)
    with pytest.raises(DiagnosticError):
        harness.dispatch(prepared)
    assert not harness.communications


@pytest.mark.parametrize("classification", ["provider_error", "dispatch_unknown", "response_invalid"])
def test_terminal_external_failure_is_not_replayed_after_configuration_and_key_save(harness, classification):
    if classification == "provider_error":
        response = httpx.Response(503, request=httpx.Request("POST", "https://fixture.invalid"))
        harness.transport_error = httpx.HTTPStatusError("synthetic-private-key", request=response.request,
                                                       response=response)
        code = "model_provider_error"
    elif classification == "dispatch_unknown":
        harness.transport_error = OSError("synthetic-private-key")
        code = "model_dispatch_unknown"
    else:
        harness.response = ModelResponse(None, "unusable")
        code = "model_response_invalid"
    prepared = harness.prepare()
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(prepared)
    assert error.value.reason_code == code
    assert b"synthetic-private-key" not in canonical_bytes(error.value.diagnostics)
    harness.profile["update_sequence"] += 1
    harness.broker.save(harness.provider["value"]["credential_ref"], "replacement-private-key")
    harness.transport_error = harness.response = None
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(prepared)
    assert error.value.reason_code == code
    with pytest.raises(DiagnosticError):
        harness.prepare()
    assert len(harness.communications) == len(harness.reads) == 1
    outcome = next(fact for fact in harness.accepted.values() if fact["stage"] == "outcome")
    assert outcome["details"]["classification"] == classification
    assert "response" not in outcome["details"]
    assert harness.created[0].closed == 1


def test_compaction_measurements_keep_exact_settings_projector_estimator_without_private_work(harness):
    harness.counter, harness.estimator_id, harness.estimator_version = len, "fixture-byte-counter", "1"
    prepared = harness.prepare(key="compact:1", purpose="compaction")
    harness.dispatch(prepared, key="compact:1")
    snapshot = (len(harness.reads), len(harness.checks), len(harness.broker.freeze_calls),
                len(harness.broker.authorize_calls), len(harness.fact_calls), len(harness.communications))
    harness.profile["value"]["runtime"]["output_limit_tokens"] = 1
    harness.counter, harness.estimator_version = lambda wire: 999_999, "2"
    harness.broker.values.clear()
    before = harness.measure(prepared, [{"kind": "text", "role": "user", "content": "x" * 4000}])
    after = harness.measure(prepared, [{"kind": "text", "role": "user", "content": "summary"}])
    assert before["settings_id"] == after["settings_id"] == prepared["settings"]["settings_id"]
    assert before["measurement"]["output_reserve_tokens"] == 32_000
    assert before["measurement"]["estimator_version"] == after["measurement"]["estimator_version"] == "1"
    assert before["projection"]["projection_version"] == prepared["projection"]["projection_version"]
    assert snapshot == (len(harness.reads), len(harness.checks), len(harness.broker.freeze_calls),
                        len(harness.broker.authorize_calls), len(harness.fact_calls), len(harness.communications))
    assert validate_context_reduction(before, after, prepared["settings"]) == after
    assert harness.projections[-1][2]["max_tokens"] == 32_000


def test_measure_context_rejects_normal_preparation(harness):
    prepared = harness.prepare()
    with pytest.raises(DiagnosticError) as error:
        harness.measure(prepared, [{"kind": "text", "role": "user", "content": "fixture"}])
    assert error.value.reason_code == "model_request_measurement_invalid"


def test_concurrent_same_logical_key_has_one_prepare_and_one_transport_attempt(harness):
    harness.auth_barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        preparations = list(executor.map(lambda _: harness.prepare(), range(2)))
    assert preparations[0] == preparations[1]
    assert len(harness.reads) == len(harness.broker.freeze_calls) == 1
    harness.auth_barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: harness.dispatch(preparations[0]), range(2)))
    harness.auth_barrier = None
    assert results[0] == results[1]
    assert len(harness.communications) == 1 and len(harness.accepted) == 3
    assert len(harness.created) == 1 and harness.created[0].closed == 1


def test_protocol_switch_on_same_root_and_old_signed_history_is_not_silently_dropped(harness):
    first = harness.prepare()
    harness.dispatch(first)
    harness.profile["value"]["model"]["id"] = "gemini-2.5-flash"
    harness.profile["update_sequence"] += 1
    harness.provider["value"]["protocol"] = "gemini"
    harness.provider["update_sequence"] += 1
    second = harness.prepare(key="normal:2")
    gemini_result = harness.dispatch(second, key="normal:2")
    assert gemini_result["provider_metadata"]["provider"] == "gemini"
    harness.profile["value"]["model"]["id"] = "synthetic-chat"
    harness.profile["update_sequence"] += 1
    harness.provider["value"]["protocol"] = "chat"
    harness.provider["update_sequence"] += 1
    third = harness.prepare(key="normal:3")
    harness.dispatch(third, key="normal:3")
    assert [call["provider"]["protocol"] for call in harness.communications] == ["chat", "gemini", "chat"]
    incompatible = [{"kind": "text", "role": "assistant", "content": "answer",
                     "provider_metadata": gemini_result["provider_metadata"]}]
    with pytest.raises(ContractValidationError):
        harness.prepare(key="normal:4", messages=incompatible)
    assert len(harness.communications) == 3


def test_retry_policy_and_transport_parameter_mismatch_are_blocked_before_entry(harness):
    harness.factory_hook = lambda adapter: setattr(adapter, "max_retries", 1)
    first = harness.prepare()
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(first)
    assert error.value.reason_code == "model_not_dispatched"
    assert error.value.diagnostics[0]["cause_code"] == "model_retry_policy_invalid"
    harness.factory_hook = lambda adapter: adapter.model_parameters.update(max_tokens=1)
    second = harness.prepare(key="normal:2")
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(second, key="normal:2")
    assert error.value.diagnostics[0]["cause_code"] == "model_transport_settings_invalid"
    assert not harness.communications and all(adapter.closed == 1 for adapter in harness.created)


def test_normal_hard_capacity_failure_never_accepts_facts_or_creates_transport(harness):
    prepared = harness.prepare(messages=[{"kind": "text", "role": "user", "content": "x" * 400_000}])
    assert prepared["hard_capacity_ok"] is False
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(prepared)
    assert error.value.reason_code == "model_request_capacity_exceeded"
    assert error.value.diagnostics[0]["step"] == "capacity"
    assert error.value.diagnostics[0]["capacity_diagnostic"] == prepared["capacity_diagnostic"]
    with pytest.raises(DiagnosticError):
        harness.dispatch(prepared)
    assert not harness.accepted and not harness.created and not harness.communications
    assert len(harness.broker.release_calls) == 1


def test_key_generation_change_during_factory_is_blocked_at_dispatch_boundary(harness):
    prepared = harness.prepare()
    harness.factory_hook = lambda _: harness.broker.save(
        harness.provider["value"]["credential_ref"], "synthetic-generation-two")
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(prepared)
    assert error.value.reason_code == "model_not_dispatched"
    assert error.value.diagnostics[0]["cause_code"] == "model_credential_changed"
    assert error.value.diagnostics[0]["step"] == "dispatch_authorization"
    assert not harness.communications and harness.created[0].closed == 1
    outcome = next(fact for fact in harness.accepted.values() if fact["stage"] == "outcome")
    assert outcome["details"]["classification"] == "not_dispatched"


def test_saving_during_communication_does_not_change_original_wire_settings_or_facts(harness):
    prepared = harness.prepare()

    def save():
        harness.profile["update_sequence"] += 1
        harness.profile["value"]["runtime"]["output_limit_tokens"] = 16_000
        harness.provider["update_sequence"] += 1
        harness.provider["value"]["base_url"] = "https://second.example.invalid"
        harness.broker.save(harness.provider["value"]["credential_ref"], "synthetic-generation-two")

    harness.on_communication = save
    result = harness.dispatch(prepared)
    sent = harness.communications[0]
    request_fact = next(fact for fact in harness.accepted.values() if fact["stage"] == "request")
    assert result["settings_id"] == prepared["settings"]["settings_id"]
    assert sent["parameters"] == prepared["settings"]["parameters"]
    assert sent["provider"]["base_url"] == "https://first.example.invalid"
    assert sent["api_key"] == "synthetic-private-key"
    assert request_fact["details"]["settings"] == prepared["settings"]
    assert request_fact["details"]["wire_request"] == sent["prepared"]["wire_request"]
    assert harness.dispatch(prepared) == result and len(harness.communications) == 1
    harness.on_communication = None
    second = harness.prepare(key="normal:2")
    harness.dispatch(second, key="normal:2")
    assert second["settings"]["configuration"]["update_sequence"] == 2
    assert harness.communications[-1]["api_key"] == "synthetic-generation-two"


def test_controlled_outcome_acceptance_error_retains_response_without_new_transport(harness):
    prepared = harness.prepare()
    harness.fail_stage, harness.controlled_fact_error = "outcome", "runtime_fact_conflict"
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(prepared)
    assert error.value.reason_code == "runtime_fact_conflict"
    assert b"synthetic-private-key" not in canonical_bytes(error.value.diagnostics)
    first_outcome = deepcopy(harness.fact_calls[-1])
    harness.response = ModelResponse("stop", "replacement response")
    result = harness.dispatch(prepared)
    assert result["content"] == "answer"
    assert harness.fact_calls[-1] == first_outcome
    assert len(harness.communications) == len(harness.created) == 1


def test_malformed_returned_object_is_response_invalid_and_never_replayed(harness):
    harness.response = {"content": "invalid transport envelope"}
    prepared = harness.prepare()
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(prepared)
    assert error.value.reason_code == "model_response_invalid"
    outcome = next(fact for fact in harness.accepted.values() if fact["stage"] == "outcome")
    assert outcome["details"]["classification"] == "response_invalid"
    assert "response" not in outcome["details"]
    harness.response = None
    with pytest.raises(DiagnosticError):
        harness.dispatch(prepared)
    assert len(harness.communications) == 1


def test_transport_and_lease_cleanup_failure_preserves_original_response_facts(harness):
    harness.fail_close = harness.broker.fail_release = True
    prepared = harness.prepare()
    with pytest.raises(DiagnosticError) as error:
        harness.dispatch(prepared)
    assert error.value.reason_code == "model_private_release_failed"
    assert error.value.diagnostics[0]["cleanup_errors"] == {
        "adapter": "synthetic_close_failed", "lease": "credential_release_failed"}
    assert b"synthetic-private-key" not in canonical_bytes(error.value.diagnostics)
    assert len(harness.communications) == 1 and len(harness.accepted) == 3
    assert next(fact for fact in harness.accepted.values() if fact["stage"] == "outcome")["details"]["response"]["content"] == "answer"
    with pytest.raises(DiagnosticError):
        harness.dispatch(prepared)
    assert len(harness.communications) == 1
    harness.fail_close = harness.broker.fail_release = False
    harness.service.release_run(harness.context.workflow_session_id, harness.context.chain_run_id)
    assert harness.created[0].closed == 1 and len(harness.broker.release_calls) == 1


def test_release_prevents_new_preparation_dispatch_and_does_not_remove_durable_unknown_facts(harness):
    prepared = harness.prepare()
    harness.transport_error = OSError("unknown")
    with pytest.raises(DiagnosticError):
        harness.dispatch(prepared)
    durable = deepcopy(harness.accepted)
    undispatched = harness.prepare(key="normal:2")
    harness.service.release_run(harness.context.workflow_session_id, harness.context.chain_run_id)
    for callback in (lambda: harness.prepare(key="normal:3"),
                     lambda: harness.dispatch(undispatched, key="normal:2")):
        with pytest.raises(DiagnosticError) as error:
            callback()
        assert error.value.reason_code == "model_recovery_unavailable"
    assert harness.accepted == durable and len(harness.communications) == 1
    replacement_service = harness.make_service()
    with pytest.raises(DiagnosticError) as error:
        replacement_service(harness.context, "models:call", "dispatch-request", {
            "binding_id": harness.binding["binding_id"], "request_key": "normal:1",
            "preparation_id": prepared["preparation_id"]})
    assert error.value.reason_code == "model_recovery_unavailable"


def test_public_settings_facts_measurements_and_errors_contain_no_credential_values(harness):
    prepared = harness.prepare(key="compact:1", purpose="compaction")
    result = harness.dispatch(prepared, key="compact:1")
    measured = harness.measure(prepared, [{"kind": "text", "role": "user", "content": "fixture"}])
    public = canonical_bytes([harness.binding, prepared, result, measured, list(harness.accepted.values())])
    for forbidden in ("synthetic-private-key", "credential_ref", "base_url", "generation", "lease"):
        assert forbidden.encode() not in public
    request_fact = next(fact for fact in harness.accepted.values() if fact["stage"] == "request")
    assert request_fact["details"]["settings"] == prepared["settings"]
    assert request_fact["details"]["measurement"] == prepared["measurement"]
    assert request_fact["details"]["wire_request"] == harness.communications[0]["prepared"]["wire_request"]
    assert [fact["sequence"] for fact in harness.accepted.values()] == [1, 2, 3]
