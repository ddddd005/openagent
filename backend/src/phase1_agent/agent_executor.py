"""SnapshotKernel adapter owned by the Agent package, using public capabilities."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from .agent_receipt_contract import (
    AGENT_EXECUTOR_REF, projection_id, validate_agent_receipts,
)

from .content_contracts import text_content
from .context_contract import artifact_ref
from .contract_json import canonical_bytes
from .contracts_v2 import validate_record
from .graph_contracts import require, uuid4_string
from .model_contract import validate_public_model_binding
from .provider_metadata import canonical_message_metadata
from .runtime import KernelCheckpoint, KernelPaused, SnapshotKernel
from .tools import final_answer_tool, register_bound_tool
from .workflow_tool_catalog import builtin_workflow_tools


AGENT_COMPONENT_ID = "9c5a4e10-c67c-4d3d-9df8-2c43cf9e19c4"
AGENT_OUTPUT_SCHEMA = {"type": "object", "properties": {"text": {"type": "string", "minLength": 1}},
                       "required": ["text"], "additionalProperties": False}


def _agent_tools(tools, bound=()):
    """Freeze usage guidance and parameters together for this Agent's tools."""
    registered = builtin_workflow_tools() if tools is None else tuple(tools)
    final = final_answer_tool(output_schema=AGENT_OUTPUT_SCHEMA)
    if not any(tool.name == final.name for tool in registered):
        registered = (*registered, final)
    else:
        registered = tuple(final if tool.name == final.name else tool for tool in registered)
    combined = (*registered, *tuple(bound))
    if bound:
        names = [tool.name for tool in combined]
        require(len(names) == len(set(names)), "agent_tool_conflict",
                "Agent tools require distinct model-facing names")
    return combined


def _bound_tools(context):
    """Read this run's prepared business tools; absence is an empty binding set."""
    if not callable(getattr(context, "bound_tool_specs", None)):
        return ()
    specs = context.bound_tool_specs(context.node_binding_id)
    result = []
    for spec in specs:
        def invoke(arguments, invocation, *, exact=spec):
            return context.invoke_bound_tool(
                binding_id=exact["binding_id"], operation=exact["operation"],
                arguments=arguments, tool_call_id=invocation.tool_call_id,
                tool_execution_id=invocation.tool_execution_id)
        result.append(register_bound_tool(
            spec["name"], spec["description"], spec["parameters"], invoke))
    return tuple(result)


def exact_input(context, port: str) -> dict:
    refs = context.input_artifact_refs(port)
    require(len(refs) == 1, "agent_input_unbound", "Agent requires one exact accepted artifact per input")
    return artifact_ref({"scope": "artifact", "output_id": refs[0]["output_id"]})


def resolve_input(context, port: str, expected: dict) -> dict:
    record = context.host_call("artifacts:read", "resolve-input", {"port": port})
    require(type(record) is dict and set(record) == {"value", "producer", "output_id"}
            and record["output_id"] == exact_input(context, port)["output_id"]
            and canonical_bytes(record["value"]) == canonical_bytes(expected),
            "agent_input_mismatch", "Agent input differs from its accepted immutable artifact")
    return record


def make_public_agent_snapshot(context, config, prompt, binding, tools, *, policy=None, policy_ref=None,
                               schema_version=2) -> dict:
    require(type(schema_version) is int and schema_version in (1, 2), "agent_snapshot_invalid",
            "Agent snapshot version must be explicitly selected")
    require(type(prompt) is dict and set(prompt) == {"messages", "layout", "current_input_ref"}
            and type(prompt["messages"]) is list and type(prompt["layout"]) is list
            and len(prompt["layout"]) == len(prompt["messages"]),
            "agent_snapshot_invalid", "Agent computation requires its narrow assembled prompt projection")
    current_input_ref = artifact_ref(prompt["current_input_ref"])
    if schema_version == 2:
        from .model_request_contracts import validate_model_binding
        from .model_execution_contracts import AGENT_EXECUTOR_REF_V2
        binding = validate_model_binding(binding)
        executor_ref = AGENT_EXECUTOR_REF_V2
    else:
        binding = validate_public_model_binding(binding)
        executor_ref = AGENT_EXECUTOR_REF.to_dict()
    prompt_ref, model_ref = exact_input(context, "prompt"), exact_input(context, "model")
    s0 = deepcopy(prompt["messages"])
    payload = {"public_agent": deepcopy(config), "executor_ref": deepcopy(executor_ref),
               "frozen_prompt_ref": prompt_ref, "model_ref": model_ref,
               "public_model_binding": binding}
    if schema_version == 1:
        require(binding["schema_version"] in (2, 4), "model_capacity_unknown",
                "Native Agent requires an explicit model capacity binding")
        payload["transport_projection"] = []
    payload.update(frozen_compaction_policy_ref=deepcopy(policy_ref), context_compaction={
        **(deepcopy(binding["capacity"]) if schema_version == 1 else {}),
        "policy": deepcopy(policy), "layout": deepcopy(prompt["layout"])})
    snapshot = {
        "schema_version": schema_version, "snapshot_id": str(uuid4()),
        "workflow_session_id": context.workflow_session_id, "node_binding_id": context.node_binding_id,
        "input_id": current_input_ref["output_id"], "parent_turn_id": None,
        "component_id": AGENT_COMPONENT_ID, "component_version": "public-3" if schema_version == 2 else "public-2",
        "config": {"owner_component_id": AGENT_COMPONENT_ID, "schema_version": 1,
                   "payload": payload},
        "s0": s0,
        "tool_definitions": [{"name": tool.name, "version": "1",
                              "description": tool.definition["function"]["description"],
                              "parameters_schema": deepcopy(tool.schema)} for tool in tools],
        "output_schema": deepcopy(AGENT_OUTPUT_SCHEMA),
        "projection_version": 1,
    }
    if schema_version == 1:
        snapshot["model_parameters"] = deepcopy(binding["parameters"])
    return validate_record("input_snapshot", snapshot)


def project_generated_messages(messages: list[dict], final: dict, snapshot_id: str) -> list[dict]:
    """Preserve every canonical call/result in its accepted order."""
    projected = []
    for raw in messages:
        message = validate_record("agent_message", raw)
        if message["role"] == "user" and message["source"]["kind"] == "protocol_feedback":
            # Correction guidance remains in canonical request/acceptance facts;
            # it is not another human root in effective historical context.
            continue
        require(message["role"] in ("assistant", "tool"), "agent_projection_invalid",
                "Generated history requires model/tool messages or explicit protocol feedback")
        blocks = message["blocks"]
        value = {"message_id": message["message_id"], "role": message["role"],
                 "content": "\n".join(block["text"] for block in blocks if block["kind"] == "text")}
        if message["role"] == "tool":
            require(len(blocks) == 1, "agent_projection_invalid", "Tool result projection requires one result")
            value.update(content=blocks[0]["model_visible_text"], tool_call_id=blocks[0]["tool_call_id"],
                         status=blocks[0]["status"], is_error=blocks[0]["is_error"],
                         outcome_reason=message["source"].get("reason_code"),
                         result=deepcopy(blocks[0]["content"]))
        else:
            calls = [{"id": block["tool_call_id"], "name": block["tool_name"],
                      "arguments": block["raw_arguments"]} for block in blocks if block["kind"] == "tool_call"]
            if calls:
                value["tool_calls"] = calls
            value.update(canonical_message_metadata(message))
        projected.append(value)
    require(type(final) is dict and set(final) == {"message_id", "value"}
            and type(final["value"]) is dict and set(final["value"]) == {"text"}
            and type(final["value"]["text"]) is str and bool(final["value"]["text"]),
            "agent_projection_invalid", "Final output must match the frozen answer schema")
    # Kernel final_answer closes with an actual tool result. Keep it, then
    # derive the visible assistant answer explicitly from the accepted final.
    projected.append({"message_id": projection_id(snapshot_id, "final-answer-projection"),
                      "role": "assistant", "content": final["value"]["text"]})
    return projected


class PublicAgentExecutor:
    """The kernel alone owns active messages and its opaque checkpoint."""

    def __init__(self, config, inputs, context, *, tools=None, kernel=None, schema_version=2):
        require(type(schema_version) is int and schema_version == 2, "agent_snapshot_invalid",
                "Only the installed schema 2 Agent executor is executable")
        self.schema_version = 2
        self.context, self.config = context, deepcopy(config)
        from .context_prompt_v6 import agent_computation_input
        self.prompt = agent_computation_input(context, inputs["prompt"])
        from .model_request_contracts import validate_compaction_policy, validate_model_binding
        self.binding = validate_model_binding(inputs["model"])
        resolve_input(context, "model", self.binding)
        policy, policy_ref = None, None
        if "compaction_policy" in inputs:
            policy = validate_compaction_policy(inputs["compaction_policy"])
            policy_ref = exact_input(context, "compaction_policy")
            resolve_input(context, "compaction_policy", policy)
        self.tools = _agent_tools(tools, _bound_tools(context))
        self.snapshot = make_public_agent_snapshot(
            context, config, self.prompt, self.binding, self.tools, policy=policy, policy_ref=policy_ref,
            schema_version=schema_version)
        self.kernel = SnapshotKernel() if kernel is None else kernel
        self._callbacks = None
        self._original_events = {}
        from .model_execution_requests import ModelExecutionRequests
        self.request_bridge = ModelExecutionRequests(
            self.snapshot, host_call=context.host_call,
            publish_fact=lambda identity, event: self._publish_original(identity, event, self._callbacks))
        self.checkpoint = None
        self.fact_ids = []
        self.disposed = False
        self.unit_id = projection_id(context.node_run_id, "context-unit")
        self._completed_result = None
        self._result_fact = None
        self._result_fact_accepted = False
        self._pending_acceptance = False

    def retains(self, checkpoint) -> bool:
        if self.disposed or type(checkpoint) is not KernelCheckpoint or checkpoint is not self.checkpoint:
            return False
        self.kernel.validate_checkpoint(checkpoint, self.snapshot, request_bridge=self.request_bridge)
        return True

    def _publish_original(self, identity, event, callbacks):
        require(callbacks is not None, "agent_fact_publisher_unavailable",
                "Executor facts require active invocation callbacks")
        uuid4_string(identity)
        if identity not in self._original_events:
            observed = deepcopy(event)
            observed.setdefault("created_at", datetime.now(timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"))
            self._original_events[identity] = (deepcopy(event), observed)
        original, observed = self._original_events[identity]
        require(canonical_bytes(original) == canonical_bytes(event), "agent_fact_conflict",
                "Original executor fact identity was reused with changed content")
        callbacks.publish_fact(identity, deepcopy(observed))
        if identity not in self.fact_ids:
            self.fact_ids.append(identity)

    def advance(self, callbacks, continuation):
        require(not self.disposed and (continuation is None or self.retains(continuation)),
                "agent_checkpoint_unavailable", "Agent requires its retained same-process checkpoint")

        self._callbacks = callbacks
        def fact(event):
            self._publish_original(str(uuid4()), event, callbacks)

        try:
            result = self.kernel.run(
                self.snapshot, self.tools,
                checkpoint=continuation,
                on_boundary=lambda boundary: not callbacks.pause_requested,
                on_progress=callbacks.report_progress, on_fact=fact,
                request_bridge=self.request_bridge)
        except KernelPaused as exc:
            self.checkpoint = exc.checkpoint
            callbacks.pause_point(self.checkpoint)
            require(False, "agent_pause_unacknowledged", "Kernel yielded without public pause acknowledgment")
        self.checkpoint = None
        self._completed_result = result
        self._result_fact = (str(uuid4()), {"kind": "agent_result", "payload": {
            "snapshot_id": self.snapshot["snapshot_id"], "unit_id": self.unit_id,
            "final": deepcopy(result.final), "model_requests": result.model_requests, "attempts": result.attempts,
            "snapshot": deepcopy(self.snapshot)},
            "created_at": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")})
        self._pending_acceptance = True
        return self.recover_acceptance(callbacks)

    def has_pending_acceptance(self) -> bool:
        return not self.disposed and self._pending_acceptance and self._completed_result is not None

    def recover_acceptance(self, callbacks):
        """Settle the retained terminal fact and projection without driving the kernel."""
        require(self.has_pending_acceptance(), "agent_result_unavailable",
                "Agent has no retained terminal result awaiting acceptance")
        if not self._result_fact_accepted:
            identity, event = self._result_fact
            self._publish_original(identity, event, callbacks)
            self._result_fact_accepted = True
        result = self._completed_result
        project_generated_messages(result.messages, result.final, self.snapshot["snapshot_id"])
        from .model_execution_contracts import AGENT_EXECUTOR_REF_V2
        model_requests = []
        for identity in self.fact_ids:
            event = self._original_events[identity][1]
            if event["kind"] == "model_result":
                payload = event["payload"]
                model_requests.append({
                    "request_id": payload["request_id"], "settings_id": payload["settings_id"],
                    "fact_ids": [reference["fact_id"] for reference in payload["result"]["fact_refs"]],
                })
        receipts = validate_agent_receipts({
            "schema_version": 2, "kind": "workflow.agent-receipts",
            "owner": {"workflow_session_id": self.context.workflow_session_id,
                      "chain_run_id": self.context.chain_run_id, "node_binding_id": self.context.node_binding_id,
                      "node_run_id": self.context.node_run_id},
            "executor_ref": deepcopy(AGENT_EXECUTOR_REF_V2), "snapshot_id": self.snapshot["snapshot_id"],
            "frozen_prompt_ref": exact_input(self.context, "prompt"), "model_ref": exact_input(self.context, "model"),
            "compaction_policy_ref": deepcopy(
                self.snapshot["config"]["payload"]["frozen_compaction_policy_ref"]),
            "unit_id": self.unit_id, "fact_ids": list(self.fact_ids),
            "model_requests": model_requests})
        outputs = {"result": text_content(result.final["value"]["text"]), "receipts": receipts}
        self._pending_acceptance = False
        return outputs

    def dispose(self):
        if self.disposed:
            return
        self.request_bridge.close()
        self.checkpoint = None
        self.snapshot = None
        self._completed_result = None
        self._result_fact = None
        self._pending_acceptance = False
        self._callbacks = None
        self._original_events.clear()
        self.disposed = True
