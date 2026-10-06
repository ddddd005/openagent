import { AGENT_BINDINGS } from "./workbenchApi";
import type { CanonicalContextMessage } from "../domain/preparation";
import type {
  WorkbenchContextPreview, WorkbenchContextRead, WorkbenchContextScope,
} from "./workbenchContext";

export const contextId = (index: number) => `00000000-0000-4000-8000-${index.toString().padStart(12, "0")}`;
export function contextScope(stage: "A" | "B" = "A"): WorkbenchContextScope {
  return {
    workflowId: "main", stage, sessionId: contextId(1), nodeBindingId: AGENT_BINDINGS[stage],
    sessionRevision: 2, refRevision: 3, headCommitId: contextId(4),
  };
}
export function contextReadFixture(target = contextScope()): WorkbenchContextRead {
  const text = (index: number, role: "user" | "assistant", blocks: string[]): CanonicalContextMessage => ({
    schema_version: 1, message_id: contextId(index), role,
    source: role === "user" ? { kind: "human", visible_message_id: contextId(10) }
      : { kind: "model", request_id: contextId(11) },
    blocks: blocks.map((text) => ({ kind: "text", text })),
  });
  return {
    schema_version: 1, kind: "workflow_context_read",
    scope: {
      workflow_session_id: target.sessionId, node_binding_id: target.nodeBindingId,
      session_revision: target.sessionRevision, ref_revision: target.refRevision, head_commit_id: target.headCommitId,
      state_snapshot_id: contextId(5), parent_turn_id: contextId(6), selected_chain_run_id: contextId(7),
    },
    messages: [
      text(20, "user", ['{\n  "text": "old"\n}']),
      text(21, "assistant", ["reasoning one", "reasoning two"]),
      text(22, "assistant", ['{"text":"final"}']),
    ],
    logical_floors: [[contextId(20)], [contextId(21), contextId(22)]],
    protected_blocks: [{ message_id: contextId(22), block_index: 0 }],
    turns: [{
      workflow_session_id: target.sessionId, node_binding_id: target.nodeBindingId,
      turn_id: contextId(6), parent_turn_id: null, run_id: contextId(12), input_id: contextId(13),
      snapshot_id: contextId(14), chain_run_id: contextId(7), root_message_id: contextId(20),
      message_ids: [contextId(21), contextId(22)], final_message_id: contextId(22),
      result_ports: {
        final: { route_id: `fixed-node-final:${target.nodeBindingId}`, delivery_id: `result-final:${contextId(12)}` },
        context_delta: { route_id: `fixed-node-context:${target.nodeBindingId}`, delivery_id: `result-context:${contextId(12)}` },
      },
    }],
  };
}
export function contextPreviewFixture(target = contextScope(), text = "next"): WorkbenchContextPreview {
  const read = contextReadFixture(target);
  if (target.stage === "B") return {
    schema_version: 1, kind: "workflow_context_preview", scope: read.scope,
    status: "pending", reason_code: "upstream_input_pending", input: null, preparation: null,
  };
  const source = { kind: "visible_message" as const, visible_message_id: contextId(90) };
  const root: CanonicalContextMessage = {
    schema_version: 1, message_id: contextId(91), role: "user",
    source: { kind: "human", visible_message_id: source.visible_message_id },
    blocks: [{ kind: "text", text: JSON.stringify({ text }, null, 2) }],
  };
  const messages = [...read.messages, root];
  const view = {
    schema_version: 1 as const, kind: "context_view" as const, workflow_session_id: target.sessionId,
    node_binding_id: target.nodeBindingId, parent_turn_id: read.scope.parent_turn_id, projection_version: 1 as const,
    messages, overrides: [], protected_blocks: read.protected_blocks,
  };
  return {
    schema_version: 1, kind: "workflow_context_preview", scope: read.scope,
    status: "ready", reason_code: null, input: { kind: "ephemeral", input_id: contextId(92), source },
    preparation: {
      schema_version: 1, kind: "context_preparation",
      node_input: {
        schema_version: 1, input_id: contextId(92), port_id: "request",
        payload_schema_ref: { schema_id: "writing_text", version: 1 }, source, payload: { text },
      },
      canonical_messages: messages,
      send_view: { ...view, purpose: "send" }, display_view: { ...view, purpose: "display" },
      root_locator: { message_id: root.message_id, block_index: 0 },
      logical_floors: [...read.logical_floors, [root.message_id]], protected_blocks: read.protected_blocks,
      prompt_message_ids: [], s0: messages,
      assembly: { schema_version: 1, kind: "prompt_assembly", messages, manifest: {} },
      evidence_digest: `json-v1:sha256:${"a".repeat(64)}`,
      collection: { schema_version: 1, kind: "prompt_collection", items: [] },
      variables: null, config: {}, processing: {}, lorebook: [], context_regex: {},
    },
  };
}
