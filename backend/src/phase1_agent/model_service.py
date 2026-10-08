"""Controlled model transport, private run frames and accepted request facts.

Callbacks belong to the application host: input resolution must verify the
consumer's exact accepted input refs; fact acceptance must persist before
returning its receipt. This service owns no database or Agent business state.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import os
import httpx
from threading import RLock
from uuid import uuid4

from jsonschema import Draft202012Validator
from openai import APIStatusError

from .adapter import DeepSeekAdapter, ProviderResponseError
from .contract_errors import ContractValidationError, ModelRequestError
from .contract_json import canonical_bytes, content_digest, validate_json_value
from .contracts import ModelResponse, ModelToolCall
from .frozen_model import FrozenConfiguredAdapter, FrozenModelParameters
from .gemini_adapter import GeminiAdapter, GeminiProtocolError
from .gemini_capabilities import binding_capabilities, validate_provider_parameters
from .graph_contracts import GraphDiagnosticError, require, uuid4_string
from .model_contract import (
    validate_any_model_source_config, validate_provider_record, validate_public_model_binding,
    validate_public_model_result,
)
from .prompt_contract import validate_ready_prompt


@dataclass(frozen=True, repr=False)
class CredentialLease:
    reference: str
    evidence: bytes = field(repr=False)


class EnvironmentCredentialBroker:
    """Only a controlled environment reference is supported in this slice."""

    def __init__(self, environ=None):
        self._environ = os.environ if environ is None else environ
        self._evidence_key = os.urandom(32)

    def _secret(self, reference):
        require(reference in ("env:DEEPSEEK_API_KEY", "env:GEMINI_API_KEY"),
                "model_credential_reference_denied",
                "Credential reference is outside the installed broker policy")
        secret = self._environ.get(reference.removeprefix("env:"))
        require(type(secret) is str and bool(secret.strip()), "model_credential_unavailable",
                "Controlled model credential is unavailable")
        return secret

    def _evidence(self, secret):
        return hmac.new(self._evidence_key, secret.encode("utf-8"), hashlib.sha256).digest()

    def freeze(self, reference) -> CredentialLease:
        return CredentialLease(reference, self._evidence(self._secret(reference)))

    def authorize(self, lease: CredentialLease) -> str:
        secret = self._secret(lease.reference)
        require(hmac.compare_digest(lease.evidence, self._evidence(secret)),
                "model_credential_changed", "Model credential changed after run preparation")
        return secret


def create_chat_transport(*, provider: dict, parameters: dict, api_key: str,
                          http_client=None):
    """Provider-specific transports with automatic retries explicitly off."""
    frozen = FrozenModelParameters.from_mapping(parameters)
    validate_provider_parameters(provider["protocol"], dict(frozen.as_mapping()))
    if provider["protocol"] == "gemini":
        adapter = GeminiAdapter(
            api_key=api_key, model=frozen.model, base_url=provider["base_url"],
            max_tokens=frozen.max_tokens, temperature=frozen.temperature,
            thinking=dict(frozen.as_mapping())["thinking"], http_client=http_client,
        )
        return FrozenConfiguredAdapter(adapter, frozen, provider_address=provider["base_url"])
    adapter = DeepSeekAdapter(
        api_key=api_key, model=frozen.model, base_url=provider["base_url"],
        max_tokens=frozen.max_tokens, temperature=frozen.temperature,
        max_retries=0, http_client=http_client,
    )
    return FrozenConfiguredAdapter(adapter, frozen, provider_address=provider["base_url"])


class ModelCapabilityAdapter:
    """Expose the controlled JSON capability as the existing model protocol.

    Callers supply already projected legacy messages. Canonical projection
    remains the consumer's responsibility; this facade has no Agent imports.
    Errors retain the logical key so a caller cannot accidentally redispatch a
    request whose outcome commit is pending or whose dispatch is unknown.
    """

    max_retries = 0

    def __init__(self, context, binding: dict, *, request_prefix="model"):
        self._context = context
        self._binding = validate_public_model_binding(binding)
        require(type(request_prefix) is str and 0 < len(request_prefix) <= 96,
                "model_request_invalid", "Model facade request prefix is invalid")
        self._prefix, self._index = request_prefix, 1

    @property
    def model_parameters(self):
        return deepcopy(self._binding["parameters"])

    def generate(self, messages, tools):
        return self._generate(messages, tools, "kernel-model")

    def generate_compaction(self, messages, tools):
        return self._generate(messages, tools, "kernel-compaction")

    def _generate(self, messages, tools, operation):
        suffix = str(self._index) if operation == "kernel-model" else "compaction:" + str(self._index)
        try:
            value = self._context.host_call("models:call", operation, {
                "binding_id": self._binding["binding_id"], "messages": deepcopy(list(messages)),
                "tools": deepcopy(list(tools)), "request_key": self._prefix + ":" + suffix,
            })
        except ContractValidationError as error:
            code = getattr(error, "reason_code", None)
            if code in {
                "model_provider_error", "model_dispatch_unknown",
                "model_not_dispatched", "model_response_invalid",
            } or ("capacity" in self._binding and code in {
                "model_fact_acceptance_failed", "context_compaction_cold_input_budget_exceeded",
            }):
                raise ModelRequestError(code) from None
            raise
        value = validate_public_model_result(value)
        self._index += 1
        return ModelResponse(
            finish_reason=value["finish_reason"], content=value["content"],
            tool_calls=tuple(ModelToolCall(**call) for call in value["tool_calls"]),
            usage=deepcopy(value["usage"]), response_id=value["response_id"], model=value["model"],
            thinking_summary=value.get("thinking_summary"),
            provider_metadata=deepcopy(value.get("provider_metadata")),
        )


@dataclass(repr=False)
class _Frame:
    owner: tuple[str, str]
    node_binding_id: str
    config: dict
    provider: dict = field(repr=False)
    credential: CredentialLease = field(repr=False)
    binding: dict | None = None
    producer_node_run_id: str | None = None
    output_id: str | None = None
    adapter: object | None = field(default=None, repr=False)
    compaction_adapter: object | None = field(default=None, repr=False)
    lock: object = field(default_factory=RLock, repr=False)
    active_calls: int = 0
    released: bool = False


@dataclass(repr=False)
class _Request:
    digest: str
    request_id: str
    binding_id: str
    attempt_id: str = field(default_factory=lambda: str(uuid4()))
    facts: list[dict] = field(default_factory=list)
    result: dict | None = None
    outcome: dict | None = None
    error_code: str | None = None
    dispatched: bool = False
    blocked: bool = False
    input_refs: dict = field(default_factory=dict)
    acceptance_retryable: bool = False


class PublicModelService:
    """Run-scoped model bindings and single-attempt request lifecycle.

    A request is keyed by consumer node run and a caller-supplied logical key.
    Reentering an identical completed or pending-outcome request reuses its
    response. It never retries a transport exception or unknown dispatch.
    """

    def __init__(self, *, provider_reader, accept_fact, resolve_input,
                 credential_broker=None, transport_factory=create_chat_transport):
        self._provider_reader = provider_reader
        self._accept_fact_callback = accept_fact
        self._resolve_input = resolve_input
        self._broker = credential_broker or EnvironmentCredentialBroker()
        self._factory = transport_factory or create_chat_transport
        self._frames: dict[tuple[str, str, str], _Frame] = {}
        self._bindings: dict[str, _Frame] = {}
        self._requests: dict[tuple[str, str, str, str], _Request] = {}
        self._lock = RLock()

    @staticmethod
    def _owner(context):
        return (uuid4_string(context.workflow_session_id), uuid4_string(context.chain_run_id))

    def prepare_run(self, *, workflow_session_id: str, chain_run_id: str,
                    node_configs: dict[str, dict], records: list[dict]) -> None:
        owner = uuid4_string(workflow_session_id), uuid4_string(chain_run_id)
        prepared = {}
        for node_id, config in node_configs.items():
            uuid4_string(node_id)
            config = validate_any_model_source_config(config)
            reference = config["reference"]
            record = next((record for record in records if all(
                record.get(key) == reference[key] for key in reference)), None)
            provider = validate_provider_record(record, reference)
            validate_provider_parameters(provider["protocol"], config["parameters"])
            lease = self._broker.freeze(provider["credential_ref"])
            prepared[(*owner, node_id)] = _Frame(owner, node_id, config, provider, lease)
        with self._lock:
            require(not any(key[:2] == owner for key in self._frames),
                    "model_run_already_prepared", "Run model inputs were already prepared")
            self._frames.update(prepared)

    def release_run(self, workflow_session_id: str, chain_run_id: str) -> None:
        owner = workflow_session_id, chain_run_id
        adapters = []
        with self._lock:
            frames = [self._frames.pop(key) for key in list(self._frames) if key[:2] == owner]
            for frame in frames:
                frame.released = True
                if frame.binding:
                    self._bindings.pop(frame.binding["binding_id"], None)
                if frame.active_calls == 0:
                    for field_name in ("adapter", "compaction_adapter"):
                        adapter = getattr(frame, field_name)
                        if adapter is not None:
                            adapters.append(adapter)
                            setattr(frame, field_name, None)
            for key in list(self._requests):
                if key[:2] == owner:
                    self._requests.pop(key)
        for adapter in adapters:
            self._close_adapter(adapter)

    @staticmethod
    def _close_adapter(adapter):
        try:
            adapter.close()
        except Exception:
            # Disposal errors cannot re-authorize a released frame.
            pass

    @contextmanager
    def _frame_operation(self, frame):
        adapters = []
        try:
            with frame.lock:
                with self._lock:
                    require(not frame.released, "model_recovery_unavailable",
                            "Active model frame has been released")
                    frame.active_calls += 1
                try:
                    yield
                finally:
                    with self._lock:
                        frame.active_calls -= 1
                        if frame.released and frame.active_calls == 0:
                            for field_name in ("adapter", "compaction_adapter"):
                                adapter = getattr(frame, field_name)
                                if adapter is not None:
                                    adapters.append(adapter)
                                    setattr(frame, field_name, None)
        finally:
            for adapter in adapters:
                self._close_adapter(adapter)

    @property
    def active_frame_count(self) -> int:
        with self._lock:
            return len(self._frames)

    def close(self) -> None:
        with self._lock:
            owners = {key[:2] for key in self._frames}
        for owner in owners:
            self.release_run(*owner)

    def __call__(self, context, capability: str, operation: str, payload: dict):
        require(capability in context.definition.capabilities, "graph_capability_denied",
                "Model operation requires a declared capability")
        if capability == "models:resolve" and operation in ("bind-model", "bind-native-model"):
            require(type(payload) is dict and ("capacity" in payload) == (operation == "bind-native-model"),
                    "model_source_invalid", "Model resolution operation differs from its source version")
            return self.bind_model(context, payload)
        if capability == "models:call" and operation == "chat":
            require(type(payload) is dict and set(payload) == {"binding_id"},
                    "model_call_invalid", "Chat requires only its accepted model binding identity")
            prompt = validate_ready_prompt(self._input(context, "prompt"))
            messages = [{"kind": "text", **message}
                        for message in prompt["assembly"]["messages"]]
            result = self.call_model(context, binding_id=payload["binding_id"],
                                     messages=messages, tools=[], request_key="chat")
            require(not result["tool_calls"], "model_unexpected_tool_calls",
                    "Ordinary Chat does not execute or publish tool calls")
            return result
        if capability == "models:call" and operation in ("kernel-model", "kernel-compaction"):
            require(type(payload) is dict and set(payload) == {
                "binding_id", "messages", "tools", "request_key",
            }, "model_call_invalid", "Model facade requires an explicit logical request")
            # The package owns its loop and ready-prompt semantics. The host
            # still resolves the exact frozen prompt of this invocation.
            self._input(context, "prompt")
            return self.call_model(context, **payload,
                                   purpose="compaction" if operation == "kernel-compaction" else "normal")
        require(False, "model_operation_denied", "Unknown controlled model operation")

    def _input(self, context, port):
        refs = context.input_artifact_refs(port)
        require(type(refs) is list and len(refs) == 1,
                "model_input_unaccepted", "Model calls require one exact accepted input per port")
        uuid4_string(refs[0]["output_id"])
        return deepcopy(self._resolve_input(context, port))

    def bind_model(self, context, config: dict) -> dict:
        require("models:resolve" in context.definition.capabilities,
                "graph_capability_denied", "Model source requires declared resolution capability")
        owner = self._owner(context)
        config = validate_any_model_source_config(config)
        with self._lock:
            frame = self._frames.get((*owner, context.node_binding_id))
            require(frame is not None, "model_recovery_unavailable",
                    "Prepared model frame is unavailable in this process")
            require(frame.config == config, "model_input_changed",
                    "Model source differs from the run preparation")
            if frame.binding is None:
                frame.producer_node_run_id = uuid4_string(context.node_run_id)
                frame.binding = validate_public_model_binding({
                    "schema_version": (4 if "capacity" in config else 3)
                    if frame.provider["protocol"] == "gemini" else (
                        2 if "capacity" in config else 1),
                    "kind": "workflow.model-binding",
                    "binding_id": str(uuid4()), "reference": config["reference"],
                    "parameters": config["parameters"],
                    "capabilities": binding_capabilities(
                        frame.provider["protocol"], config["parameters"]),
                    **({"capacity": config["capacity"]} if "capacity" in config else {}),
                })
                self._bindings[frame.binding["binding_id"]] = frame
            require(frame.producer_node_run_id == context.node_run_id, "model_owner_mismatch",
                    "Binding producer invocation changed")
            return deepcopy(frame.binding)

    def _authorize_binding(self, context, binding_id):
        require("models:call" in context.definition.capabilities, "graph_capability_denied",
                "Model call requires declared invocation capability")
        binding = validate_public_model_binding(self._input(context, "model"))
        require(binding["binding_id"] == binding_id, "model_binding_mismatch",
                "Call binding differs from the exact accepted model input")
        with self._lock:
            frame = self._bindings.get(binding_id)
            require(frame is not None, "model_recovery_unavailable",
                    "Active model binding is unavailable in this process")
            require(frame.owner == self._owner(context) and frame.binding == binding,
                    "model_owner_mismatch", "Model binding is outside this run or has been modified")
            require(frame.output_id is not None
                    and context.input_artifact_refs("model")[0]["output_id"] == frame.output_id,
                    "model_binding_unaccepted", "Call must consume the exact accepted producer binding output")
            return frame

    def accept_binding_output(self, *, workflow_session_id: str, chain_run_id: str,
                              node_run_id: str, binding_id: str, output_id: str) -> None:
        """Host registers a source output only after durable artifact acceptance."""
        uuid4_string(output_id)
        with self._lock:
            frame = self._bindings.get(binding_id)
            require(frame is not None and frame.owner == (workflow_session_id, chain_run_id)
                    and frame.producer_node_run_id == node_run_id,
                    "model_owner_mismatch", "Accepted binding output has the wrong producer")
            require(frame.output_id in (None, output_id), "model_binding_conflict",
                    "Binding was already accepted under another output identity")
            frame.output_id = output_id

    def _boundary(self, frame):
        current = self._provider_reader(deepcopy(frame.config["reference"]))
        provider = validate_provider_record(current, frame.config["reference"])
        require(provider["credential_ref"] == frame.provider["credential_ref"],
                "model_credential_changed", "Provider credential reference changed")
        return self._broker.authorize(frame.credential)

    def _fact(self, context, request, stage, details):
        return {"schema_version": 1, "kind": "workflow.model-fact", "fact_id": str(uuid4()),
                "session_id": context.workflow_session_id, "chain_run_id": context.chain_run_id,
                "node_binding_id": context.node_binding_id,
                "node_run_id": context.node_run_id, "binding_id": request.binding_id,
                "request_id": request.request_id, "stage": stage,
                "sequence": len(request.facts) + 1, "details": deepcopy(details)}

    def _accept(self, context, fact):
        validate_json_value(fact)
        try:
            receipt = self._accept_fact_callback(context, deepcopy(fact))
        except Exception as error:
            if getattr(error, "reason_code", None) is not None:
                raise
            raise GraphDiagnosticError(
                "model_fact_acceptance_failed", "Model fact could not be accepted") from None
        require(type(receipt) is dict and receipt == {"fact_id": fact["fact_id"]},
                "model_fact_receipt_invalid", "Model fact acceptance receipt does not match")
        return deepcopy(receipt)

    def _settle(self, context, request):
        if request.outcome is not None:
            try:
                receipt = self._accept(context, request.outcome)
            except Exception as error:
                request.acceptance_retryable = getattr(error, "reason_code", None) == "model_fact_acceptance_failed"
                raise
            request.facts.append(receipt)
            request.outcome = None
            request.acceptance_retryable = False
        require(request.error_code is None, request.error_code or "model_call_failed",
                "Model request ended without an accepted successful response")
        require(request.result is not None, "model_request_incomplete",
                "Model request has no reusable response")
        return validate_public_model_result({**request.result, "fact_refs": request.facts})

    def call_model(self, context, *, binding_id: str, messages: list[dict],
                   tools: list[dict], request_key: str, purpose: str = "normal") -> dict:
        """Public facade for authorized callers; tool schemas never execute tools."""
        require(type(request_key) is str and 0 < len(request_key) <= 128,
                "model_request_invalid", "Logical request key is invalid")
        validate_json_value([messages, tools])
        require(type(messages) is list and bool(messages) and type(tools) is list,
                "model_request_invalid", "Model request messages and tools must be arrays")
        require(len(tools) <= 128, "model_request_invalid", "Tool schema budget exceeded")
        names = []
        for tool in tools:
            require(type(tool) is dict and set(tool) == {"type", "function"}
                    and tool["type"] == "function" and type(tool["function"]) is dict
                    and set(tool["function"]) == {"name", "description", "parameters"}
                    and type(tool["function"]["name"]) is str and bool(tool["function"]["name"])
                    and type(tool["function"]["description"]) is str
                    and type(tool["function"]["parameters"]) is dict,
                    "model_request_invalid", "Tools require explicit registered function schemas")
            Draft202012Validator.check_schema(tool["function"]["parameters"])
            names.append(tool["function"]["name"])
        require(len(set(names)) == len(names), "model_request_invalid", "Tool names repeat")
        messages, tools = deepcopy(messages), deepcopy(tools)
        frame = self._authorize_binding(context, binding_id)
        with self._frame_operation(frame):
            require(purpose in ("normal", "compaction"), "model_request_invalid", "Unknown model purpose")
            parameters = deepcopy(frame.config["parameters"])
            if purpose == "compaction":
                require("capacity" in frame.binding, "model_capacity_unknown",
                        "Compaction requires an explicit native model capacity binding")
                parameters["max_tokens"] = frame.config["capacity"]["summary_max_tokens"]
            projector = GeminiAdapter if frame.provider["protocol"] == "gemini" else DeepSeekAdapter
            prepared = projector.prepare_request(messages, tools, parameters)
            if purpose == "compaction":
                cold_estimate = len(canonical_bytes(prepared["wire_request"]))
                require(cold_estimate <= frame.config["capacity"]["max_cold_input_tokens"],
                        "context_compaction_cold_input_budget_exceeded",
                        "Unconfirmed cache cannot exceed the explicit cold-input budget")
            basis = {"messages": messages, "tools": tools,
                     "wire_request": deepcopy(prepared["wire_request"]),
                     "transport_arguments": deepcopy(prepared["transport_arguments"]),
                     "projection": prepared["projection"],
                     "provider_reference": deepcopy(frame.config["reference"]),
                     "parameters": deepcopy(parameters),
                     "input_refs": {"model": context.input_artifact_refs("model"),
                                    "prompt": context.input_artifact_refs("prompt")}}
            if purpose == "compaction":
                basis["purpose"] = "context_compaction"
                basis["cold_input_estimate"] = {
                    "input_tokens": cold_estimate, "token_count_kind": "utf8_bytes_estimate",
                    "max_cold_input_tokens": frame.config["capacity"]["max_cold_input_tokens"],
                }
            digest = content_digest(basis)
            key = (*self._owner(context), uuid4_string(context.node_run_id), request_key)
            with self._lock:
                request = self._requests.get(key)
            if request is not None:
                require(request.digest == digest and request.binding_id == binding_id,
                        "model_request_conflict", "Logical request input changed")
                require(not request.blocked, "model_request_blocked",
                        "Request failed before dispatch and requires a new logical request")
                return self._settle(context, request)
            with self._lock:
                require(not frame.released, "model_recovery_unavailable",
                        "Model frame was released before request registration")
                request = _Request(digest, str(uuid4()), binding_id)
                request.input_refs = deepcopy(basis["input_refs"])
                self._requests[key] = request
            try:
                request.facts.append(self._accept(
                    context, self._fact(context, request, "request", basis)))
                request.facts.append(self._accept(context, self._fact(
                    context, request, "attempt",
                    {"attempt_id": request.attempt_id, "attempt_index": 1,
                     "dispatch_state": "intent", "attempts_consumed": 1})))
                secret = self._boundary(frame)
                adapter_field = "compaction_adapter" if purpose == "compaction" else "adapter"
                selected_adapter = getattr(frame, adapter_field)
                if selected_adapter is None:
                    implementation = self._factory(provider=deepcopy(frame.provider),
                                                   parameters=deepcopy(parameters),
                                                   api_key=secret)
                    selected_adapter = (implementation if isinstance(implementation, FrozenConfiguredAdapter)
                                        else FrozenConfiguredAdapter(
                                         implementation,
                                         FrozenModelParameters.from_mapping(parameters),
                                         provider_address=frame.provider["base_url"]))
                    setattr(frame, adapter_field, selected_adapter)
                selected_adapter.verify_settings()
                require(getattr(selected_adapter, "max_retries", 0) == 0,
                        "model_retry_policy_invalid", "Transport must disable SDK retries")
                # Recheck after construction and immediately before entering transport.
                self._boundary(frame)
                with self._lock:
                    require(not frame.released, "model_recovery_unavailable",
                            "Model frame was released before dispatch")
            except Exception:
                request.blocked = True
                if len(request.facts) == 2:
                    request.outcome = self._fact(context, request, "outcome",
                                               {"classification": "not_dispatched",
                                                "attempt_id": request.attempt_id})
                    self._accept(context, request.outcome)
                    request.outcome = None
                raise GraphDiagnosticError(
                    "model_not_dispatched", "Model request was blocked before transport entry") from None
            request.dispatched = True
            try:
                response = selected_adapter.generate_prepared(messages, tools, prepared)
                gemini = frame.provider["protocol"] == "gemini"
                result = {
                    "schema_version": 2 if gemini else 1, "kind": "workflow.model-result",
                    "binding_id": binding_id, "request_id": request.request_id,
                    "finish_reason": response.finish_reason, "content": response.content,
                    "tool_calls": [{"id": call.id, "name": call.name,
                                    "raw_arguments": call.raw_arguments, "type": call.type}
                                   for call in response.tool_calls],
                    "usage": deepcopy(response.usage), "response_id": response.response_id,
                    "model": response.model, "fact_refs": request.facts + [{"fact_id": str(uuid4())}],
                    **({"thinking_summary": response.thinking_summary,
                        "provider_metadata": deepcopy(response.provider_metadata)} if gemini else {}),
                }
                validate_public_model_result(result)
                result.pop("fact_refs")
                request.result = result
                request.outcome = self._fact(context, request, "outcome",
                    {"classification": "response_received", "response": result,
                     "attempt_id": request.attempt_id})
            except Exception as exc:
                if isinstance(exc, (APIStatusError, httpx.HTTPStatusError)):
                    classification, request.error_code = "provider_error", "model_provider_error"
                elif isinstance(exc, (ProviderResponseError, ContractValidationError)):
                    classification, request.error_code = "response_invalid", "model_response_invalid"
                else:
                    classification, request.error_code = "dispatch_unknown", "model_dispatch_unknown"
                details = {"classification": classification, "attempt_id": request.attempt_id}
                if isinstance(exc, GeminiProtocolError):
                    details["diagnostic"] = deepcopy(exc.diagnostic)
                if isinstance(exc, (APIStatusError, httpx.HTTPStatusError)):
                    status = exc.status_code if isinstance(exc, APIStatusError) else exc.response.status_code
                    if type(status) is int:
                        details["status_code"] = status
                request.outcome = self._fact(context, request, "outcome",
                                            details)
            return self._settle(context, request)

    def retry_acceptance(self, context, *, request_key="chat") -> dict:
        """Retry only a retained outcome commit; never perform external work."""
        with self._lock:
            key = (*self._owner(context), uuid4_string(context.node_run_id), request_key)
            request = self._requests.get(key)
            require(request is not None and request.dispatched and not request.blocked
                    and request.error_code is None and request.result is not None,
                    "model_recovery_unavailable",
                    "No retained successful response exists for this invocation")
        frame = self._authorize_binding(context, request.binding_id)
        with self._frame_operation(frame):
            refs = {"model": context.input_artifact_refs("model"),
                    "prompt": context.input_artifact_refs("prompt")}
            require(request.input_refs == refs, "model_request_conflict", "Retained response input binding changed")
            return self._settle(context, request)

    def has_pending_acceptance(self, context, *, request_key="chat") -> bool:
        with self._lock:
            key = (*self._owner(context), uuid4_string(context.node_run_id), request_key)
            request = self._requests.get(key)
            return bool(request is not None and request.dispatched and not request.blocked
                        and request.error_code is None and request.result is not None
                        and request.outcome is not None and request.acceptance_retryable)

    def validate_failed_retry(self, owner, evidence, *, authorize=False):
        """Confirm a rejected first request against its retained frozen frame."""
        coordinates = (owner["workflow_session_id"], owner["chain_run_id"], owner["node_run_id"])
        with self._lock:
            requests = [request for key, request in self._requests.items() if key[:3] == coordinates]
            require(len(requests) == 1, "failed_retry_unavailable",
                    "Only a single confirmed failed model request can restart")
            request = requests[0]
            frame = self._bindings.get(request.binding_id)
            require(frame is not None and not frame.released and frame.owner == coordinates[:2]
                    and request.request_id == evidence["request_id"]
                    and request.attempt_id == evidence["attempt_id"]
                    and request.binding_id == evidence["binding_id"]
                    and request.result is None and request.outcome is None
                    and evidence["classification"] in ("not_dispatched", "provider_error")
                    and [fact["fact_id"] for fact in request.facts] == (
                        evidence["fact_ids"][:2] if request.blocked else evidence["fact_ids"])
                    and (request.blocked and not request.dispatched
                         if evidence["classification"] == "not_dispatched"
                         else request.dispatched and request.error_code == "model_provider_error"),
                    "failed_retry_unavailable", "Original failed request frame is unavailable")
        if authorize:
            with self._frame_operation(frame):
                self._boundary(frame)
        return True
