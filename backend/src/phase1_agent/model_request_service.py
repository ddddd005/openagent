"""Uninstalled per-request model lifecycle with explicitly trusted host callbacks.

authorize_binding verifies the grant, owner and exact accepted model artifact.
read_resource_bundle and check_resource_bundle own coherent current reads and
root dependency authorization. accept_fact persists before returning a receipt.
This module has no resource store, credential vault, node or service registry.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from openai import APIStatusError

from .adapter import ProviderResponseError
from .contract_errors import ContractValidationError, DiagnosticError, require, uuid4_string
from .contract_json import content_digest, validate_json_value
from .credential_contract import CredentialLease
from .frozen_model import FrozenConfiguredAdapter, FrozenModelParameters
from .model_contract import validate_provider_record
from .model_request_budget import measure_context, measure_prepared_request
from .model_request_contracts import (
    build_model_settings, validate_model_binding, validate_model_result,
    validate_operation_payload,
)
from .model_request_transport import ModelRequestTransport


@dataclass(repr=False)
class _PreparedRequest:
    owner: tuple[str, str, str, str]
    intent_digest: str
    authorization_digest: str
    binding: dict
    input_refs: dict
    preparation_id: str = field(default_factory=lambda: str(uuid4()))
    request_id: str = field(default_factory=lambda: str(uuid4()))
    attempt_id: str = field(default_factory=lambda: str(uuid4()))
    reserved_fact_ids: dict = field(default_factory=lambda: {
        stage: str(uuid4()) for stage in ("request", "attempt", "outcome")})
    state: str = "preparing"
    error_code: str | None = None
    diagnostic: dict = field(default_factory=dict)
    settings: dict | None = None
    settings_digest: str | None = None
    prepared: dict | None = None
    public_preparation: dict | None = None
    messages: list | None = None
    tools: list | None = None
    definition: ModelRequestTransport | None = field(default=None, repr=False)
    provider: dict | None = field(default=None, repr=False)
    lease: CredentialLease | None = field(default=None, repr=False)
    adapter: object = field(default=None, repr=False)
    accepted_facts: dict = field(default_factory=dict)
    outcome: dict | None = None
    result: dict | None = None
    measurements: dict = field(default_factory=dict)
    cleanup_errors: dict = field(default_factory=dict)


class ModelRequestService:
    """Four strict operations; no implicit retries or reconstruction of old leases."""

    _CAPABILITIES = {
        "prepare-request": "models:call", "dispatch-request": "models:call",
        "discard-prepared-request": "models:call", "measure-context": "model-projection:read",
    }

    def __init__(self, *, authorize_binding, read_resource_bundle, check_resource_bundle,
                 accept_fact, credential_broker, resolve_transport):
        require(all(callable(callback) for callback in (
            authorize_binding, read_resource_bundle, check_resource_bundle, accept_fact, resolve_transport)),
            "model_host_callbacks_unavailable", "Model requests require trusted host callbacks", 500)
        require(callable(getattr(credential_broker, "freeze", None))
                and callable(getattr(credential_broker, "authorize", None)),
                "model_credential_broker_unavailable", "Model requests require a private credential broker", 500)
        self._authorize_binding = authorize_binding
        self._read_bundle, self._check_bundle = read_resource_bundle, check_resource_bundle
        self._accept_fact_callback = accept_fact
        self._broker, self._resolve_transport = credential_broker, resolve_transport
        self._requests: dict[tuple, _PreparedRequest] = {}
        self._preparations: dict[str, tuple] = {}
        self._locks: dict[tuple, RLock] = {}
        self._released: set[tuple[str, str]] = set()
        self._lock = RLock()

    @staticmethod
    def _owner(context) -> tuple:
        return tuple(uuid4_string(getattr(context, key, None)) for key in (
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"))

    def _require_active(self, owner):
        with self._lock:
            require(owner[:2] not in self._released, "model_recovery_unavailable",
                    "Model request run was released")

    def _request_lock(self, key):
        with self._lock:
            return self._locks.setdefault(key, RLock())

    def _authorization(self, context, capability, operation, binding_id):
        owner = self._owner(context)
        self._require_active(owner)
        authorization = self._authorize_binding(context, capability, operation, binding_id)
        validate_json_value(authorization)
        require(type(authorization) is dict and set(authorization) == {
            "binding", "binding_output_id", "producer_node_run_id", "input_refs"},
            "model_binding_unaccepted", "Trusted model authorization is incomplete")
        binding = validate_model_binding(authorization["binding"])
        require(binding["binding_id"] == binding_id and type(authorization["input_refs"]) is dict,
                "model_binding_mismatch", "Call does not name the exact accepted model binding")
        for key in ("binding_output_id", "producer_node_run_id"):
            uuid4_string(authorization[key])
        return owner, deepcopy(authorization)

    def __call__(self, context, capability, operation, payload):
        require(self._CAPABILITIES.get(operation) == capability,
                "model_request_operation_invalid", "Model request capability or operation is invalid")
        payload = validate_operation_payload(operation, payload)
        owner, authorization = self._authorization(
            context, capability, operation, payload["binding_id"])
        if operation == "prepare-request":
            return self._prepare(context, owner, authorization, payload)
        with self._lock:
            key = self._preparations.get(payload["preparation_id"])
        require(key is not None, "model_recovery_unavailable",
                "Preparation is unavailable; persisted facts cannot reconstruct a dispatchable lease")
        with self._request_lock(key):
            request = self._requests[key]
            require(request.owner == owner and request.binding["binding_id"] == payload["binding_id"],
                    "model_owner_mismatch", "Preparation belongs to another invocation")
            require(request.authorization_digest == content_digest(authorization),
                    "model_request_conflict", "Accepted model input or request artifact references changed")
            if operation == "measure-context":
                return self._measure(request, payload)
            require(key[-1] == payload["request_key"], "model_request_conflict",
                    "Logical request key differs from its preparation")
            if operation == "discard-prepared-request":
                return self._discard(request)
            return self._dispatch(context, request)

    @staticmethod
    def _validate_tools(tools):
        require(len(tools) <= 128, "model_request_invalid", "Tool schema budget exceeded")
        names = []
        for tool in tools:
            require(set(tool) == {"type", "function"} and tool["type"] == "function"
                    and type(tool["function"]) is dict
                    and set(tool["function"]) == {"name", "description", "parameters"},
                    "model_request_invalid", "Tools require registered function schemas")
            function = tool["function"]
            require(type(function["name"]) is str and 0 < len(function["name"]) <= 256
                    and type(function["description"]) is str and type(function["parameters"]) is dict,
                    "model_request_invalid", "Tool function fields are invalid")
            Draft202012Validator.check_schema(function["parameters"])
            names.append(function["name"])
        require(len(set(names)) == len(names), "model_request_invalid", "Tool names repeat")

    @staticmethod
    def _raise_request(request):
        code = request.error_code or "model_request_incomplete"
        raise DiagnosticError(code, "Model request retains its original terminal state",
                              request_id=request.request_id, preparation_id=request.preparation_id,
                              request_state=request.state, **request.diagnostic,
                              cleanup_errors=deepcopy(request.cleanup_errors)) from None

    def _prepare(self, context, owner, authorization, payload):
        binding = authorization["binding"]
        require(payload["purpose"] in binding["authorization"]["purposes"],
                "model_purpose_denied", "Binding does not authorize this request purpose")
        key = (*owner[:2], owner[3], payload["request_key"])
        intent = content_digest({**payload, "input_refs": authorization["input_refs"]})
        with self._request_lock(key):
            self._require_active(owner)
            with self._lock:
                request = self._requests.get(key)
            if request is not None:
                require(request.owner == owner and request.intent_digest == intent
                        and request.authorization_digest == content_digest(authorization),
                        "model_request_conflict", "Logical request input or accepted model binding changed")
                if request.error_code is not None:
                    self._raise_request(request)
                return deepcopy(request.public_preparation)
            request = _PreparedRequest(owner, intent, content_digest(authorization),
                                       deepcopy(binding), deepcopy(authorization["input_refs"]))
            with self._lock:
                self._requests[key] = request
            step = "tool_validation"
            try:
                self._validate_tools(payload["tools"])
                step = "resource_bundle_read"
                bundle = self._read_bundle(
                    context, deepcopy(binding["reference"]), authorization["binding_output_id"])
                require(type(bundle) is dict and set(bundle) == {"profile", "provider"},
                        "model_resource_bundle_invalid", "Trusted resource bundle fields differ")
                step = "resource_bundle_validation"
                record = bundle["profile"]
                require(type(record) is dict and all(record.get(key) == value for key, value in binding["reference"].items())
                        and type(record.get("data_schema_version")) is int and record["data_schema_version"] == 2,
                        "model_profile_unavailable", "Current model profile has a different root or schema")
                provider_ref = record["value"]["provider"]
                provider = validate_provider_record(bundle["provider"], provider_ref)
                step = "transport_resolution"
                definition = self._resolve_transport(provider["protocol"], record["value"]["model"]["id"])
                require(isinstance(definition, ModelRequestTransport)
                        and callable(definition.projector) and callable(definition.create_transport),
                        "model_transport_capability_unavailable", "Exact trusted transport is unavailable")
                # Capture a detached declaration and the exact callable implementations.
                request.definition = ModelRequestTransport(
                    deepcopy(definition.capability), definition.projection_version,
                    definition.projector, definition.create_transport, definition.counter,
                    definition.estimator_id, definition.estimator_version)
                step = "settings_validation"
                request.settings = build_model_settings(
                    record["value"],
                    configuration={"reference": binding["reference"], "update_sequence": record["update_sequence"]},
                    provider={"reference": provider_ref, "update_sequence": bundle["provider"]["update_sequence"]},
                    protocol=provider["protocol"], transport_capability=request.definition.capability,
                    settings_id=str(uuid4()), request_id=request.request_id, binding_id=binding["binding_id"],
                    purpose=payload["purpose"], requires_tools=bool(payload["tools"]))
                request.settings_digest = content_digest(request.settings)
                request.provider = deepcopy(provider)
                step = "credential_freeze"
                request.lease = self._broker.freeze(provider["credential_ref"])
                require(isinstance(request.lease, CredentialLease)
                        and request.lease.reference == provider["credential_ref"],
                        "model_credential_unavailable", "Broker did not freeze the selected private credential")
                step = "credential_authorization"
                self._authorize_lease(request)
                revisions = {key: deepcopy(request.settings[key]) for key in ("configuration", "provider")}
                step = "configuration_revision_check"
                require(self._check_bundle(context, deepcopy(binding["reference"]),
                                           authorization["binding_output_id"], revisions) is True,
                        "model_request_configuration_changed_before_prepare",
                        "Current resource revisions changed while preparing the request")
                request.messages, request.tools = deepcopy(payload["messages"]), deepcopy(payload["tools"])
                step = "projection"
                request.prepared = self._project(request, request.messages, request.tools,
                                                 request.settings["parameters"])
                step = "measurement"
                measured = measure_prepared_request(
                    request.prepared, request.settings, **self._estimator(request))
                self._require_active(owner)
                request.public_preparation = validate_operation_payload("prepare-request", {
                    "request_id": request.request_id, "preparation_id": request.preparation_id,
                    "settings": request.settings, **measured,
                    "reserved_model_fact_ids": request.reserved_fact_ids,
                }, response=True)
                request.state = "prepared"
                with self._lock:
                    self._preparations[request.preparation_id] = key
                return deepcopy(request.public_preparation)
            except Exception as error:
                request.state = "rejected"
                request.error_code = getattr(error, "reason_code", None) or "model_request_prepare_failed"
                request.diagnostic = {"cause_code": getattr(error, "reason_code", None) or "internal_error",
                                      "step": step}
                self._close_private(request)
                self._raise_request(request)

    @staticmethod
    def _estimator(request):
        return {"counter": request.definition.counter, "estimator_id": request.definition.estimator_id,
                "estimator_version": request.definition.estimator_version}

    @staticmethod
    def _project(request, messages, tools, parameters):
        try:
            prepared = request.definition.projector(deepcopy(messages), deepcopy(tools), deepcopy(parameters))
        except ValueError as error:
            if getattr(error, "reason_code", None) is not None:
                raise
            raise DiagnosticError("model_request_projection_invalid",
                                  "Current protocol cannot project the supplied history") from None
        require(type(prepared) is dict and prepared.get("projection") == request.definition.projection_version,
                "model_request_projection_invalid", "Projector differs from the captured exact version")
        return deepcopy(prepared)

    def _authorize_lease(self, request):
        secret = self._broker.authorize(request.lease)
        require(type(secret) is str and bool(secret), "model_credential_unavailable",
                "Private credential authorization failed")
        return secret

    @staticmethod
    def _fact(request, stage, details):
        sid, chain, node, run = request.owner
        return {"schema_version": 2, "kind": "workflow.model-fact",
                "fact_id": request.reserved_fact_ids[stage], "session_id": sid, "chain_run_id": chain,
                "node_binding_id": node, "node_run_id": run, "binding_id": request.binding["binding_id"],
                "request_id": request.request_id, "stage": stage,
                "sequence": ("request", "attempt", "outcome").index(stage) + 1, "details": deepcopy(details)}

    def _accept(self, context, request, stage, fact):
        validate_json_value(fact)
        try:
            receipt = self._accept_fact_callback(context, deepcopy(fact))
        except Exception as error:
            code = getattr(error, "reason_code", None) or "model_fact_acceptance_failed"
            raise DiagnosticError(code, "Original model fact could not be accepted",
                                  request_id=request.request_id, stage=stage) from None
        require(type(receipt) is dict and receipt == {"fact_id": fact["fact_id"]},
                "model_fact_receipt_invalid", "Fact acceptance receipt differs from the original fact")
        request.accepted_facts[stage] = deepcopy(receipt)

    def _settle(self, context, request):
        if request.outcome is not None:
            self._accept(context, request, "outcome", request.outcome)
            request.outcome = None
        if request.error_code is not None:
            request.state = "failed"
            self._raise_request(request)
        require(request.result is not None and set(request.accepted_facts) == {"request", "attempt", "outcome"},
                "model_request_incomplete", "Model response has not been durably accepted")
        request.state = "succeeded"
        require(not request.cleanup_errors, "model_private_release_failed",
                "Model result was retained but its private resources could not be released",
                request_id=request.request_id, cleanup_errors=deepcopy(request.cleanup_errors))
        return validate_model_result({
            **request.result, "fact_refs": [request.accepted_facts[stage]
                                          for stage in ("request", "attempt", "outcome")],
        }, settings=request.settings)

    def _dispatch(self, context, request):
        self._require_active(request.owner)
        if request.state in ("succeeded", "outcome_pending", "failed"):
            return self._settle(context, request)
        if request.error_code is not None:
            self._raise_request(request)
        require(request.state == "prepared", "model_request_already_dispatched",
                "Preparation may enter transport only once")
        require(content_digest(request.settings) == request.settings_digest
                and content_digest(request.prepared["wire_request"])
                    == request.public_preparation["projection"]["wire_digest"],
                "model_request_conflict", "Captured settings or wire changed")
        step = "capacity"
        try:
            require(request.public_preparation["hard_capacity_ok"],
                    "model_request_capacity_exceeded", "Prepared wire exceeds the model hard capacity")
            step = "request_fact_acceptance"
            self._accept(context, request, "request", self._fact(request, "request", {
                "messages": request.messages, "tools": request.tools,
                "input_refs": request.input_refs, "wire_request": request.prepared["wire_request"],
                "projection": request.definition.projection_version, "settings": request.settings,
                "preparation_id": request.preparation_id, "intent_digest": request.intent_digest,
                "wire_digest": request.public_preparation["projection"]["wire_digest"],
                "measurement": request.public_preparation["measurement"],
            }))
            step = "attempt_fact_acceptance"
            self._accept(context, request, "attempt", self._fact(request, "attempt", {
                "attempt_id": request.attempt_id, "attempt_index": 1,
                "dispatch_state": "intent", "attempts_consumed": 1,
            }))
            step = "credential_authorization"
            secret = self._authorize_lease(request)
            step = "transport_setup"
            adapter = request.definition.create_transport(
                provider=deepcopy(request.provider), parameters=deepcopy(request.settings["parameters"]),
                api_key=secret)
            request.adapter = adapter
            target = adapter.implementation if isinstance(adapter, FrozenConfiguredAdapter) else adapter
            require(callable(getattr(target, "generate_prepared", None))
                    and type(getattr(target, "max_retries", None)) is int and target.max_retries == 0,
                    "model_retry_policy_invalid", "Transport requires exact prepared-wire dispatch and zero retries")
            parameters = FrozenModelParameters.from_mapping(request.settings["parameters"])
            require(FrozenModelParameters.from_mapping(getattr(target, "model_parameters", None)) == parameters,
                    "model_transport_settings_invalid", "Transport differs from captured settings")
            if not isinstance(adapter, FrozenConfiguredAdapter):
                request.adapter = FrozenConfiguredAdapter(adapter, parameters,
                                                         provider_address=request.provider["base_url"])
            request.adapter.verify_settings()
            step = "dispatch_authorization"
            self._authorize_lease(request)
            with self._lock:
                self._require_active(request.owner)
                request.state = "dispatched"
        except Exception as error:
            request.error_code = "model_not_dispatched"
            request.state = "failed"
            if set(request.accepted_facts) == {"request", "attempt"}:
                request.outcome = self._fact(request, "outcome", {
                    "classification": "not_dispatched", "attempt_id": request.attempt_id,
                    "diagnostic": {"cause_code": getattr(error, "reason_code", None) or "internal_error",
                                   "step": step, "request_id": request.request_id,
                                   "settings_id": request.settings["settings_id"]},
                })
            request.diagnostic = {"cause_code": getattr(error, "reason_code", None) or "internal_error",
                                  "step": step}
            if step == "capacity":
                request.error_code = "model_request_capacity_exceeded"
                request.diagnostic["capacity_diagnostic"] = deepcopy(
                    request.public_preparation["capacity_diagnostic"])
            self._close_private(request)
            return self._settle(context, request)
        response_received = False
        try:
            response = request.adapter.generate_prepared(request.messages, request.tools, deepcopy(request.prepared))
            response_received = True
            result = {
                "schema_version": 3, "kind": "workflow.model-result",
                "binding_id": request.binding["binding_id"], "request_id": request.request_id,
                "settings_id": request.settings["settings_id"],
                "finish_reason": response.finish_reason, "content": response.content,
                "tool_calls": [{"id": call.id, "name": call.name, "raw_arguments": call.raw_arguments,
                                "type": call.type} for call in response.tool_calls],
                "usage": deepcopy(response.usage), "response_id": response.response_id, "model": response.model,
                "thinking_summary": response.thinking_summary, "provider_metadata": deepcopy(response.provider_metadata),
                "fact_refs": [{"fact_id": request.reserved_fact_ids[stage]}
                              for stage in ("request", "attempt", "outcome")],
            }
            result = validate_model_result(result, settings=request.settings)
            result.pop("fact_refs")
            request.result = result
            request.outcome = self._fact(request, "outcome", {
                "classification": "response_received", "response": result, "attempt_id": request.attempt_id,
            })
        except Exception as error:
            if response_received:
                classification, request.error_code = "response_invalid", "model_response_invalid"
            elif isinstance(error, (APIStatusError, httpx.HTTPStatusError)):
                classification, request.error_code = "provider_error", "model_provider_error"
            elif isinstance(error, (ProviderResponseError, ContractValidationError)):
                classification, request.error_code = "response_invalid", "model_response_invalid"
            else:
                classification, request.error_code = "dispatch_unknown", "model_dispatch_unknown"
            details = {"classification": classification, "attempt_id": request.attempt_id}
            if isinstance(error, (APIStatusError, httpx.HTTPStatusError)):
                details["status_code"] = (error.status_code if isinstance(error, APIStatusError)
                                          else error.response.status_code)
            request.outcome = self._fact(request, "outcome", details)
        finally:
            request.state = "outcome_pending"
            self._close_private(request)
        return self._settle(context, request)

    def _discard(self, request):
        if request.state == "discarded":
            require(not request.cleanup_errors, "model_private_release_failed",
                    "Discard retains private resources whose cleanup failed",
                    cleanup_errors=deepcopy(request.cleanup_errors))
            return {"request_id": request.request_id, "discarded": True}
        require(request.state == "prepared", "model_request_already_dispatched",
                "Only an undispatched preparation may be discarded")
        request.state, request.error_code = "discarded", "model_prepared_request_discarded"
        self._close_private(request)
        require(not request.cleanup_errors, "model_private_release_failed",
                "Discard retains private resources whose cleanup failed",
                cleanup_errors=deepcopy(request.cleanup_errors))
        return {"request_id": request.request_id, "discarded": True}

    def _measure(self, request, payload):
        require(request.settings is not None and request.settings["purpose"] == "compaction"
                and request.state in ("prepared", "outcome_pending", "succeeded"),
                "model_request_measurement_invalid", "Measurement requires an owned compaction preparation")
        self._validate_tools(payload["tools"])
        require(not payload["tools"] or request.settings["capabilities"]["tools"],
                "model_tools_unsupported", "Captured transport does not support context tools")
        parameters = deepcopy(request.settings["parameters"])
        parameters["max_tokens"] = request.settings["budget"]["normal_output_limit_tokens"]
        prepared = self._project(request, payload["messages"], payload["tools"], parameters)
        result = measure_context(prepared, request.settings, messages=payload["messages"], tools=payload["tools"],
                                 **self._estimator(request))
        result = validate_operation_payload("measure-context", result, response=True)
        request.measurements[content_digest(result)] = deepcopy(result)
        return result

    def has_pending_acceptance(self, context) -> bool:
        owner = self._owner(context)
        with self._lock:
            return any(request.owner == owner and request.outcome is not None
                       and set(request.accepted_facts) == {"request", "attempt"}
                       for request in self._requests.values())

    def retry_acceptance(self, context):
        owner = self._owner(context)
        with self._lock:
            matches = [(key, request) for key, request in self._requests.items()
                       if request.owner == owner and request.outcome is not None
                       and set(request.accepted_facts) == {"request", "attempt"}]
        require(len(matches) == 1, "model_recovery_unavailable",
                "No unique accepted model request outcome is retained")
        key, request = matches[0]
        _, authorization = self._authorization(
            context, "models:call", "dispatch-request", request.binding["binding_id"])
        with self._request_lock(key):
            require(request.authorization_digest == content_digest(authorization),
                    "model_request_conflict", "Accepted model input changed before outcome recovery")
            return self._settle(context, request)

    def _close_private(self, request):
        adapter, lease = request.adapter, request.lease
        request.provider = None
        try:
            if adapter is not None and callable(getattr(adapter, "close", None)):
                adapter.close()
            request.adapter = None
            request.cleanup_errors.pop("adapter", None)
        except Exception as error:
            request.cleanup_errors["adapter"] = getattr(error, "reason_code", None) or "internal_error"
        try:
            release = getattr(self._broker, "release", None)
            if lease is not None and callable(release):
                release(lease)
            request.lease = None
            request.cleanup_errors.pop("lease", None)
        except Exception as error:
            request.cleanup_errors["lease"] = getattr(error, "reason_code", None) or "internal_error"

    def release_run(self, workflow_session_id, chain_run_id):
        owner = uuid4_string(workflow_session_id), uuid4_string(chain_run_id)
        with self._lock:
            self._released.add(owner)
            requests = [(key, request) for key, request in self._requests.items() if request.owner[:2] == owner]
        for key, request in requests:
            with self._request_lock(key):
                self._close_private(request)
                if request.state in ("prepared", "preparing"):
                    request.state, request.error_code = "released", "model_recovery_unavailable"
        failed = {request.request_id: deepcopy(request.cleanup_errors)
                  for _, request in requests if request.cleanup_errors}
        require(not failed, "model_private_release_failed",
                "Run release retains private resources whose cleanup failed", cleanup_errors=failed)

    @property
    def active_frame_count(self):
        with self._lock:
            return len({request.owner[:2] for request in self._requests.values()
                        if request.owner[:2] not in self._released})
