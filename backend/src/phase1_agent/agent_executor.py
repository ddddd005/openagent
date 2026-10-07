"""SnapshotKernel adapter owned by the Agent package, using public capabilities."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from .agent_receipt_contract import (
    AGENT_EXECUTOR_REF, projection_id, receipts_references, validate_agent_receipts,
)

from .content_contracts import text_content
from .context_contract import artifact_ref, validate_context_unit
from .context_prompt import validate_any_context_prompt
from .contract_graph import validate_message_history
from .contract_json import canonical_bytes, loads_strict, validate_json_value
from .contracts_v2 import validate_record
from .execution_facts import ExecutionFactHistory
from .graph_contracts import require, uuid4_string
from .model_contract import validate_public_model_binding
from .runtime import CanonicalModelAdapter, KernelCheckpoint, KernelPaused, SnapshotKernel
from .workflow_tool_catalog import builtin_workflow_tools


AGENT_COMPONENT_ID = "9c5a4e10-c67c-4d3d-9df8-2c43cf9e19c4"
AGENT_OUTPUT_SCHEMA = {"type": "object", "properties": {"text": {"type": "string", "minLength": 1}},
                       "required": ["text"], "additionalProperties": False}


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


def _snapshot_messages(prompt: dict, prompt_ref: dict) -> tuple[list[dict], list[dict]]:
    """Freeze transport projection identities; these are not old production IDs."""
    basis = prompt_ref["output_id"]
    messages, mapping = [], []
    pending = {}
    historical_units = {entry["unit"]["unit_id"]: entry["unit"] for entry in prompt["context"]["units"]
                        if "unit" in entry}
    for index, (wire, provenance) in enumerate(zip(prompt["messages"], prompt["provenance"])):
        message_id = projection_id(basis, f"message:{index}")
        role = wire["role"]
        blocks = [{"kind": "text", "text": wire.get("content") or ""}]
        if role == "assistant":
            source = {"kind": "model", "request_id": projection_id(basis, f"request:{index}")}
            for call in wire.get("tool_calls", []):
                original_id = call["id"]
                call_id = projection_id(basis, "call:" + original_id)
                pending[original_id] = call_id
                raw = call["function"]["arguments"]
                arguments = loads_strict(raw)
                require(type(arguments) is dict, "agent_history_projection_invalid",
                        "Historical tool arguments must remain an object")
                blocks.append({"kind": "tool_call", "tool_call_id": call_id,
                               "tool_name": call["function"]["name"], "tool_definition_version": "1",
                               "raw_arguments": raw, "parsed_arguments": arguments})
        elif role == "tool":
            original_id = wire["tool_call_id"]
            require(original_id in pending, "agent_history_projection_invalid",
                    "Historical tool result must retain its original pairing")
            unit = historical_units.get(provenance.get("unit_id"))
            typed = next((message for message in unit["messages"]
                          if message["message_id"] == provenance.get("message_id")), None) if unit else None
            require(typed is not None and typed["role"] == "tool"
                    and typed["content"] == wire["content"] and typed["tool_call_id"] == original_id
                    and {"status", "is_error", "outcome_reason", "result"} <= set(typed),
                    "agent_history_projection_invalid", "Historical tool outcomes require their typed unit source")
            reason = typed["outcome_reason"]
            call_id = pending.pop(original_id)
            execution_id = None if reason == "never_started" else projection_id(basis, f"execution:{index}")
            source = ({"kind": "runtime_tool_observation", "tool_call_id": call_id,
                       "tool_execution_id": execution_id, "reason_code": reason}
                      if reason is not None else {"kind": "tool", "tool_execution_id": execution_id})
            blocks = [{"kind": "tool_result", "tool_call_id": call_id,
                       "tool_execution_id": execution_id, "status": typed["status"], "is_error": typed["is_error"],
                       "content": deepcopy(typed["result"]), "model_visible_text": wire["content"]}]
        elif role == "user" and provenance["kind"] == "current_input":
            source = {"kind": "upstream_node", "output_id": prompt["current_input_ref"]["output_id"]}
        else:
            source = {"kind": "prompt", "prompt_id": basis, "revision": 1}
        if role == "assistant" and not wire.get("tool_calls"):
            # Prompt sources can express assistant text without inventing a
            # model request. Protocol-bearing history uses the mapping above.
            message = {"schema_version": 3, "message_id": message_id, "role": role,
                       "source": {"kind": "prompt", "prompt_id": basis, "revision": 1,
                                  "item_instance_id": projection_id(basis, f"item:{index}"),
                                  "group_instance_id": None}, "blocks": blocks}
        else:
            message = {"schema_version": 2 if role == "tool" and source["kind"] == (
                "runtime_tool_observation") else 1, "message_id": message_id, "role": role,
                       "source": source, "blocks": blocks}
        messages.append(validate_record("agent_message", message))
        mapping.append({"canonical_message_id": message_id, "source_provenance": deepcopy(provenance),
                        "identity_policy": "frozen_transport_projection"})
    require(not pending, "agent_history_projection_invalid", "Historical protocol must close its tool batch")
    validate_message_history(messages)
    return messages, mapping


def make_public_agent_snapshot(context, config, prompt, binding, tools, *, policy=None, policy_ref=None) -> dict:
    native = prompt.get("schema_version") == 6
    if native:
        from .context_prompt_v6 import validate_native_context_prompt
        prompt = validate_native_context_prompt(prompt)
    else:
        prompt = validate_any_context_prompt(prompt)
    binding = validate_public_model_binding(binding)
    prompt_ref, model_ref = exact_input(context, "prompt"), exact_input(context, "model")
    s0, projection = (deepcopy(prompt["messages"]), []) if native else _snapshot_messages(prompt, prompt_ref)
    payload = {"public_agent": deepcopy(config), "executor_ref": AGENT_EXECUTOR_REF.to_dict(),
               "frozen_prompt_ref": prompt_ref, "model_ref": model_ref,
               "public_model_binding": binding, "transport_projection": projection}
    if native:
        require(binding["schema_version"] == 2, "model_capacity_unknown",
                "Native Agent requires an explicit model capacity binding")
        payload.update(frozen_compaction_policy_ref=deepcopy(policy_ref), context_compaction={
            **deepcopy(binding["capacity"]), "policy": deepcopy(policy), "layout": deepcopy(prompt["layout"])})
    return validate_record("input_snapshot", {
        "schema_version": 1, "snapshot_id": str(uuid4()),
        "workflow_session_id": context.workflow_session_id, "node_binding_id": context.node_binding_id,
        "input_id": prompt["current_input_ref"]["output_id"], "parent_turn_id": None,
        "component_id": AGENT_COMPONENT_ID, "component_version": "public-2" if native else "public-1",
        "config": {"owner_component_id": AGENT_COMPONENT_ID, "schema_version": 1,
                   "payload": payload},
        "s0": s0,
        "tool_definitions": [{"name": tool.name, "version": "1",
                              "description": tool.definition["function"]["description"],
                              "parameters_schema": deepcopy(tool.schema)} for tool in tools],
        "model_parameters": deepcopy(binding["parameters"]), "output_schema": deepcopy(AGENT_OUTPUT_SCHEMA),
        "projection_version": 1,
    })


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


def validate_execution_projection(unit, receipts, facts, prompt) -> None:
    unit, receipts = validate_context_unit(unit), validate_agent_receipts(receipts)
    prompt = validate_any_context_prompt(prompt)
    require(type(facts) is list and [fact["fact_id"] for fact in facts] == receipts["fact_ids"]
            and all(fact["owner"] == receipts["owner"]
                    and fact["executor_ref"] == receipts["executor_ref"] for fact in facts)
            and [fact["sequence"] for fact in facts] == list(range(1, len(facts) + 1)),
            "agent_receipt_fact_mismatch", "Agent receipts must name the exact accepted executor facts")
    events = [fact["payload"] for fact in facts]
    require(events[-1].get("kind") == "agent_result", "agent_result_unaccepted",
            "A context delta requires a complete accepted Agent result")
    result = events[-1]["payload"]
    require(result["snapshot_id"] == receipts["snapshot_id"] and result["unit_id"] == receipts["unit_id"],
            "agent_result_unaccepted", "Agent result differs from its receipt")
    messages = [event["payload"]["message"] for event in events if event["kind"] == "message_accepted"]
    validate_message_history(messages)
    requests = [event["payload"] for event in events if event["kind"] == "model_request"]
    require(bool(requests), "agent_result_unaccepted", "Agent result requires an actual accepted request")
    s0, _ = _snapshot_messages(prompt, receipts["frozen_prompt_ref"])
    frozen = {"s0": s0, "model_parameters": requests[0]["model_parameters"], "tool_definitions": [
        {"name": tool["function"]["name"], "version": "1",
         "description": tool["function"]["description"],
         "parameters_schema": tool["function"]["parameters"]} for tool in requests[0]["tools"]]}
    history = ExecutionFactHistory(frozen)
    for sequence, (envelope, event) in enumerate(zip(facts[:-1], events[:-1]), start=1):
        # Bridge public host integer generations to the legacy fact validator's
        # UUID representation without pretending they are production IDs.
        history.append({
            "schema_version": 1, "fact_id": envelope["fact_id"], "sequence": sequence,
            "workflow_session_id": receipts["owner"]["workflow_session_id"],
            "chain_run_id": receipts["owner"]["chain_run_id"],
            "node_binding_id": receipts["owner"]["node_binding_id"],
            "run_id": receipts["owner"]["node_run_id"], "snapshot_id": receipts["snapshot_id"],
            "generation": projection_id(receipts["snapshot_id"], f"generation:{envelope['generation']}"),
            "created_at": event["created_at"], "kind": event["kind"], "payload": event["payload"]})
    require(type(result["model_requests"]) is int and result["model_requests"] == len(requests)
            and type(result["attempts"]) is int
            and result["attempts"] == sum(event["kind"] == "model_attempt_started" for event in events),
            "agent_result_unaccepted", "Final counters must match the accepted request/attempt history")
    final = result["final"]
    control = next((message for message in messages if message["message_id"] == final["message_id"]), None)
    require(control is not None and control["role"] == "assistant" and messages[-1]["role"] == "tool",
            "agent_result_unaccepted", "Final answer requires its accepted control message and result")
    calls = [block for block in control["blocks"] if block["kind"] == "tool_call"]
    settled = messages[-1]["blocks"]
    require(len(calls) == 1 and calls[0]["tool_name"] == "final_answer"
            and calls[0]["parsed_arguments"].get("answer") == final["value"]
            and len(settled) == 1 and settled[0]["kind"] == "tool_result"
            and settled[0]["tool_call_id"] == calls[0]["tool_call_id"]
            and settled[0]["status"] == "success" and settled[0]["content"] == final["value"],
            "agent_result_unaccepted", "Derived answer must equal its accepted final tool settlement")
    require(unit["unit_id"] == receipts["unit_id"] and unit["source_kind"] == "accepted_execution"
            and unit["root"] == prompt["current_input"]
            and unit["source_refs"] == [receipts["frozen_prompt_ref"], prompt["current_input_ref"]]
            and canonical_bytes(unit["messages"]) == canonical_bytes(project_generated_messages(
                messages, result["final"], receipts["snapshot_id"])),
            "agent_projection_mismatch", "Unit differs from its accepted canonical execution and final answer")


def validate_execution_update(packet, facts, prompt, binding, *, policy=None, policy_ref=None) -> dict:
    """Prove ordered native updates from exact frozen inputs and accepted facts."""
    from .context_prompt_v6 import validate_native_context_prompt
    from .context_update import validate_context_update
    from .context_v4 import build_context_update_view
    packet = validate_context_update(packet)
    receipts = validate_agent_receipts(packet["receipts"])
    prompt = validate_native_context_prompt(prompt)
    binding = validate_public_model_binding(binding)
    require(type(facts) is list and bool(facts)
            and [fact["fact_id"] for fact in facts] == receipts["fact_ids"]
            and all(fact["owner"] == receipts["owner"]
                    and fact["executor_ref"] == receipts["executor_ref"] for fact in facts)
            and [fact["sequence"] for fact in facts] == list(range(1, len(facts) + 1)),
            "agent_receipt_fact_mismatch", "Native update requires its exact accepted executor facts")
    events = [fact["payload"] for fact in facts]
    require(events[-1].get("kind") == "agent_result", "agent_result_unaccepted",
            "Native update requires a complete accepted result")
    result = events[-1]["payload"]
    snapshot = validate_record("input_snapshot", result["snapshot"])
    payload = snapshot["config"]["payload"]
    descriptor = payload["context_compaction"]
    require(binding["schema_version"] == 2 and snapshot["component_id"] == AGENT_COMPONENT_ID
            and snapshot["component_version"] == "public-2"
            and snapshot["snapshot_id"] == result["snapshot_id"] == receipts["snapshot_id"]
            and result["unit_id"] == receipts["unit_id"]
            and snapshot["workflow_session_id"] == receipts["owner"]["workflow_session_id"]
            and snapshot["node_binding_id"] == receipts["owner"]["node_binding_id"]
            and snapshot["input_id"] == prompt["current_input_ref"]["output_id"]
            and snapshot["s0"] == prompt["messages"] and snapshot["output_schema"] == AGENT_OUTPUT_SCHEMA
            and payload["frozen_prompt_ref"] == receipts["frozen_prompt_ref"]
            and payload["model_ref"] == receipts["model_ref"]
            and payload["public_model_binding"] == binding
            and snapshot["model_parameters"] == binding["parameters"]
            and payload["public_agent"] == {}
            and payload["executor_ref"] == AGENT_EXECUTOR_REF.to_dict()
            and descriptor["layout"] == prompt["layout"]
            and all(descriptor[field] == binding["capacity"][field] for field in binding["capacity"]),
            "agent_snapshot_mismatch", "Native snapshot differs from its exact accepted inputs")
    if policy is None:
        require(policy_ref is None and descriptor["policy"] is None
                and payload["frozen_compaction_policy_ref"] is None
                and packet["compaction_policy_ref"] is None,
                "agent_snapshot_mismatch",
                "Absent accepted compaction policy cannot claim frozen policy evidence")
    else:
        from .context_compaction_policy import validate_compaction_policy
        policy = validate_compaction_policy(policy)
        policy_ref = artifact_ref(policy_ref)
        require(descriptor["policy"] == policy
                and payload["frozen_compaction_policy_ref"] == policy_ref
                and packet["compaction_policy_ref"] == policy_ref,
                "agent_snapshot_mismatch",
                "Native compaction policy differs from its exact accepted input artifact")
    history = ExecutionFactHistory(snapshot)
    operations, messages, requests, attempts = [], [], [], 0
    for sequence, (envelope, event) in enumerate(zip(facts[:-1], events[:-1]), start=1):
        history.append({
            "schema_version": 1, "fact_id": envelope["fact_id"], "sequence": sequence,
            "workflow_session_id": receipts["owner"]["workflow_session_id"],
            "chain_run_id": receipts["owner"]["chain_run_id"],
            "node_binding_id": receipts["owner"]["node_binding_id"],
            "run_id": receipts["owner"]["node_run_id"], "snapshot_id": receipts["snapshot_id"],
            "generation": projection_id(receipts["snapshot_id"], f"generation:{envelope['generation']}"),
            "created_at": event["created_at"], "kind": event["kind"], "payload": event["payload"],
        })
        if event["kind"] == "message_accepted":
            messages.append(event["payload"]["message"])
            operations.append({"kind": "append", "message": event["payload"]["message"]})
        elif event["kind"] == "context_compaction_applied":
            operations.append(event["payload"])
        elif event["kind"] == "model_request":
            requests.append(event["payload"])
        elif event["kind"] == "model_attempt_started":
            attempts += 1
    require(bool(requests) and result["model_requests"] == len(requests)
            and result["attempts"] == attempts
            and packet["operations"] == operations
            and packet["basis_view_ref"] == prompt["context_ref"]
            and packet["basis_revision"] == prompt["context"]["basis"],
            "agent_result_unaccepted", "Native operation order or result counters were modified")
    final = result["final"]
    project_generated_messages(messages, final, receipts["snapshot_id"])
    control = next((message for message in messages if message["message_id"] == final["message_id"]), None)
    calls = [block for block in control["blocks"] if block["kind"] == "tool_call"] if control else []
    settled = messages[-1]["blocks"] if messages else []
    require(control is not None and control["role"] == "assistant" and messages[-1]["role"] == "tool"
            and len(calls) == len(settled) == 1 and calls[0]["tool_name"] == "final_answer"
            and calls[0]["parsed_arguments"].get("answer") == final["value"]
            and settled[0]["tool_call_id"] == calls[0]["tool_call_id"]
            and settled[0]["status"] == "success" and settled[0]["content"] == final["value"],
            "agent_result_unaccepted", "Native answer differs from the accepted final settlement")
    require(packet["next_view"] == build_context_update_view(prompt, operations, receipts, final),
            "agent_projection_mismatch", "Persistent candidate differs from the accepted working view")
    return deepcopy(final)


class PublicAgentExecutor:
    """The kernel alone owns active messages and its opaque checkpoint."""

    def __init__(self, config, inputs, context, *, tools=None, kernel=None, direct_context=False):
        self.context, self.config = context, deepcopy(config)
        self.native_context = inputs["prompt"].get("schema_version") == 6
        if self.native_context:
            from .context_prompt_v6 import validate_native_context_prompt
            self.prompt = validate_native_context_prompt(inputs["prompt"])
        else:
            self.prompt = validate_any_context_prompt(inputs["prompt"])
        self.direct_context = direct_context
        require(not direct_context or (
            self.prompt["schema_version"] in (4, 5, 6)
            and self.prompt["context"]["owner"]["workflow_session_id"] == context.workflow_session_id
            and self.prompt["context"]["owner"]["agent_node_id"] == context.node_binding_id),
            "context_agent_binding_mismatch", "Prompt belongs to another bound Agent")
        self.binding = validate_public_model_binding(inputs["model"])
        resolve_input(context, "prompt", self.prompt)
        resolve_input(context, "model", self.binding)
        policy, policy_ref = None, None
        if self.native_context and "compaction_policy" in inputs:
            from .context_compaction_policy import validate_compaction_policy
            policy = validate_compaction_policy(inputs["compaction_policy"])
            policy_ref = exact_input(context, "compaction_policy")
            resolve_input(context, "compaction_policy", policy)
        self.tools = builtin_workflow_tools() if tools is None else tuple(tools)
        self.snapshot = make_public_agent_snapshot(
            context, config, self.prompt, self.binding, self.tools, policy=policy, policy_ref=policy_ref)
        self.kernel = SnapshotKernel() if kernel is None else kernel
        from .model_service import ModelCapabilityAdapter
        self.adapter = CanonicalModelAdapter(ModelCapabilityAdapter(
            context, self.binding, request_prefix="agent:" + context.node_run_id))
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
        self.kernel.validate_checkpoint(checkpoint, self.snapshot)
        return True

    def advance(self, callbacks, continuation):
        require(not self.disposed and (continuation is None or self.retains(continuation)),
                "agent_checkpoint_unavailable", "Agent requires its retained same-process checkpoint")

        def fact(event):
            identity = str(uuid4())
            observed = {**deepcopy(event), "created_at": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")}
            callbacks.publish_fact(identity, observed)
            self.fact_ids.append(identity)

        try:
            result = self.kernel.run(
                self.snapshot, self.tools, self.adapter,
                max_model_requests=None, max_model_attempts=None, checkpoint=continuation,
                # The legacy checkpoint does not retain an unaccepted response.
                # Finish its local acceptance before acknowledging user pause;
                # the next before_tool/before_request point retains that result.
                on_boundary=lambda boundary: boundary == "before_accept" or not callbacks.pause_requested,
                on_progress=callbacks.report_progress, on_fact=fact)
        except KernelPaused as exc:
            self.checkpoint = exc.checkpoint
            callbacks.pause_point(self.checkpoint)
            require(False, "agent_pause_unacknowledged", "Kernel yielded without public pause acknowledgment")
        self.checkpoint = None
        self._completed_result = result
        self._result_fact = (str(uuid4()), {"kind": "agent_result", "payload": {
            "snapshot_id": self.snapshot["snapshot_id"], "unit_id": self.unit_id,
            "final": deepcopy(result.final), "model_requests": result.model_requests, "attempts": result.attempts,
            **({"snapshot": deepcopy(self.snapshot)} if self.native_context else {})},
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
            callbacks.publish_fact(identity, deepcopy(event))
            self.fact_ids.append(identity)
            self._result_fact_accepted = True
        result = self._completed_result
        unit = None if self.native_context else validate_context_unit({
            "schema_version": 1, "kind": "workflow.context-unit", "unit_id": self.unit_id,
            "source_kind": "accepted_execution", "root": deepcopy(self.prompt["current_input"]),
            "messages": project_generated_messages(result.messages, result.final, self.snapshot["snapshot_id"]),
            "source_refs": [exact_input(self.context, "prompt"), deepcopy(self.prompt["current_input_ref"])]})
        receipts = validate_agent_receipts({
            "schema_version": 1, "kind": "workflow.agent-receipts",
            "owner": {"workflow_session_id": self.context.workflow_session_id,
                      "chain_run_id": self.context.chain_run_id, "node_binding_id": self.context.node_binding_id,
                      "node_run_id": self.context.node_run_id},
            "executor_ref": AGENT_EXECUTOR_REF.to_dict(), "snapshot_id": self.snapshot["snapshot_id"],
            "frozen_prompt_ref": exact_input(self.context, "prompt"), "model_ref": exact_input(self.context, "model"),
            "unit_id": self.unit_id, "fact_ids": list(self.fact_ids)})
        outputs = {"result": text_content(result.final["value"]["text"]), "unit": unit, "facts": receipts}
        if self.native_context:
            from .context_update import validate_context_update
            from .context_v4 import build_context_update_view
            packet = validate_context_update({
                "schema_version": 1, "kind": "workflow.agent-context-update",
                "update_id": projection_id(self.context.node_run_id, "context-update"),
                "owner": deepcopy(receipts["owner"]), "basis_view_ref": deepcopy(self.prompt["context_ref"]),
                "basis_revision": deepcopy(self.prompt["context"]["basis"]),
                "compaction_policy_ref": deepcopy(
                    self.snapshot["config"]["payload"]["frozen_compaction_policy_ref"]),
                "operations": deepcopy(result.context_operations), "receipts": receipts,
                "next_view": build_context_update_view(
                    self.prompt, result.context_operations, receipts, result.final),
            })
            outputs = {"result": outputs["result"], "context": packet}
        elif self.direct_context:
            from .context_v2 import validate_agent_context
            packet = validate_agent_context({
                "schema_version": 1, "kind": "workflow.agent-context",
                "delta_id": projection_id(self.context.node_run_id, "agent-context"),
                "owner": deepcopy(receipts["owner"]), "basis_view_ref": deepcopy(self.prompt["context_ref"]),
                "current_root_ref": deepcopy(self.prompt["current_input_ref"]),
                "unit": unit, "receipts": receipts})
            outputs = {"result": outputs["result"], "context": packet}
        self._pending_acceptance = False
        return outputs

    def dispose(self):
        if self.disposed:
            return
        self.adapter.close()
        self.checkpoint = None
        self.snapshot = None
        self._completed_result = None
        self._result_fact = None
        self._pending_acceptance = False
        self.disposed = True
