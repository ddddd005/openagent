import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clonePreparation,
  transformPreparedCollection,
  type CanonicalContextMessage,
} from "../domain/preparation";
import {
  decodeWorkbenchContextPreview,
  decodeWorkbenchContextRead,
  isWorkbenchContextScope,
  mapWorkbenchContextRead,
  readWorkbenchContext,
} from "./workbenchContext";
import { contextId, contextPreviewFixture, contextReadFixture, contextScope } from "./workbenchContext.fixtures";

afterEach(() => vi.unstubAllGlobals());
describe("selected archived context adapter", () => {
  it("requires exact CAS identity and validates an empty selected parent without guessing a chain", () => {
    const target = contextScope();
    expect(isWorkbenchContextScope(target)).toBe(true);
    expect(isWorkbenchContextScope({ ...target, headCommitId: null })).toBe(false);
    expect(isWorkbenchContextScope({ ...target, refRevision: 0 })).toBe(false);
    const read = contextReadFixture();
    read.messages = [];
    read.logical_floors = [];
    read.turns = [];
    read.protected_blocks = [];
    read.scope.parent_turn_id = null;
    read.scope.selected_chain_run_id = null;
    const result = decodeWorkbenchContextRead(read, target);
    const mapped = mapWorkbenchContextRead(result, target, contextId(100));
    expect(mapped.floors).toEqual([]);
    expect(mapped.reference?.selectedChainId).toBeNull();
    expect(mapped.reference?.basis?.parentTurnId).toBeNull();
    expect(() => decodeWorkbenchContextRead(read, { ...target, refRevision: 4 })).toThrow("版本不匹配");
  });
  it("maps ordinary text by block with complete frozen canonical provenance and preserves final text", () => {
    const target = contextScope();
    const read = decodeWorkbenchContextRead(contextReadFixture(), target);
    expect(Object.isFrozen(read.messages[0].blocks)).toBe(true);
    const node = contextId(100);
    const mapped = mapWorkbenchContextRead(read, target, node);
    expect(mapped.floors.map((floor) => floor.kind)).toEqual(["root", "delta"]);
    expect(mapped.floors[1].items).toHaveLength(3);
    expect(mapped.floors[1].items[0].source.context).toMatchObject({
      messageId: contextId(21), blockIndex: 0, turnId: contextId(6), workflowSessionId: target.sessionId,
      runId: contextId(12), inputId: contextId(13), snapshotId: contextId(14),
      canonicalMessage: read.messages[1],
    });
    expect(mapped.floors[1].items[1].source.context?.blockIndex).toBe(1);
    const items = mapped.floors.flatMap((floor) => floor.items);
    const original = clonePreparation(items);
    const derived = transformPreparedCollection({
      schemaVersion: 1, kind: "preparation-collection", source: "backend-read", items,
    }, "upper", (text) => text.toUpperCase());
    expect(derived.items[1].payload).toEqual({ kind: "text", text: "REASONING ONE" });
    expect(derived.items[1].source.context?.canonicalMessage).toEqual(read.messages[1]);
    expect(derived.items.at(-1)?.payload.kind).toBe("protected-message");
    expect(derived.items.at(-1)).toEqual(original.at(-1));
    expect(items).toEqual(original);
  });
  it("keeps mixed protocol and tool results as whole messages and rejects incomplete tool batches", () => {
    const target = contextScope();
    const read = contextReadFixture();
    read.messages[1] = {
      ...read.messages[1],
      blocks: [
        { kind: "text", text: "protocol" },
        {
          kind: "tool_call", tool_call_id: contextId(30), tool_name: "inspect_text",
          tool_definition_version: "1", raw_arguments: '{"text":"x"}', parsed_arguments: { text: "x" },
        },
      ],
    };
    const result: CanonicalContextMessage = {
      schema_version: 1, message_id: contextId(31), role: "tool", source: { kind: "tool", tool_execution_id: contextId(32) },
      blocks: [{
        kind: "tool_result", tool_call_id: contextId(30), tool_execution_id: contextId(32),
        status: "success", is_error: false, content: { characters: 1 }, model_visible_text: '{"characters":1}',
      }],
    };
    read.messages.splice(2, 0, result);
    read.logical_floors[1].splice(1, 0, result.message_id);
    read.turns[0].message_ids.splice(1, 0, result.message_id);
    read.protected_blocks.push({ message_id: contextId(21), block_index: 0 });
    const mapped = mapWorkbenchContextRead(decodeWorkbenchContextRead(read, target), target, contextId(100));
    expect(mapped.floors[1].items.every((item) => item.payload.kind === "protected-message")).toBe(true);
    expect(mapped.floors[1].items[0].source.context?.canonicalMessage).toEqual(read.messages[1]);
    read.messages.splice(2, 1);
    expect(() => decodeWorkbenchContextRead(read, target)).toThrow("工具调用与结果");
  });
  it("retains ancestor ownership for fork history while rejecting wrong bindings or release locators", () => {
    const read = contextReadFixture();
    read.turns[0].workflow_session_id = contextId(200);
    expect(decodeWorkbenchContextRead(read, contextScope()).turns[0].workflow_session_id).toBe(contextId(200));
    read.turns[0].result_ports!.final.delivery_id = `result-final:${contextId(99)}`;
    expect(() => decodeWorkbenchContextRead(read, contextScope())).toThrow("端口来源");
    read.turns[0].result_ports = null;
    read.turns[0].node_binding_id = contextScope("B").nodeBindingId;
    expect(() => decodeWorkbenchContextRead(read, contextScope())).toThrow("归档轮次来源");
  });
  it.each(["missing-final-protection", "duplicate-message", "bad-source", "bad-floor", "unknown-schema"] as const)(
    "rejects %s instead of manufacturing canonical data", (kind) => {
      const read = contextReadFixture();
      if (kind === "missing-final-protection") read.protected_blocks = [];
      if (kind === "duplicate-message") read.messages.push(read.messages[0]);
      if (kind === "bad-source") (read.messages[0].source as Record<string, unknown>).kind = "visible_message";
      if (kind === "bad-floor") read.logical_floors[1].reverse();
      if (kind === "unknown-schema") (read.messages[0] as unknown as Record<string, unknown>).schema_version = 5;
      expect(() => decodeWorkbenchContextRead(read, contextScope())).toThrow();
    },
  );
  it("posts read-only exact revision fences and never calls a messages endpoint", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(contextReadFixture())));
    vi.stubGlobal("fetch", fetcher);
    await readWorkbenchContext(contextScope());
    expect(fetcher.mock.calls[0][0]).toContain(`/nodes/${contextScope().nodeBindingId}/context/read`);
    expect(fetcher.mock.calls[0][1]).toMatchObject({
      method: "POST", cache: "no-store",
      body: JSON.stringify({ expected_session_revision: 2, expected_ref_revision: 3, expected_head_commit_id: contextId(4) }),
    });
  });
});
describe("ephemeral server context preview", () => {
  function advancedPreview() {
    const value = structuredClone(contextPreviewFixture());
    if (value.status !== "ready") throw new Error("Not ready");
    const preparation = value.preparation;
    preparation.schema_version = 2;
    const program = {
      schema_version: 1, kind: "prompt_preparation_program",
      nodes: [
        { node_id: contextId(70), kind: "context-source", inputs: {}, config: {} },
        { node_id: contextId(71), kind: "regex", inputs: { input: contextId(70) },
          config: { mode: "prompt", rule: { pattern: "reasoning", replacement: "derived", flags: "", mode: "all" } } },
      ],
      outputs: { prompt: null, context: contextId(71) },
    };
    const basis = { revision: 0, values: {} };
    preparation.config = { schema_version: 3, preparation: program, preparation_state: basis };
    preparation.send_view.overrides = [{ message_id: contextId(21), block_index: 0, text: "derived one" }];
    preparation.s0 = preparation.canonical_messages.map((message) => message.message_id === contextId(21)
      ? { ...message, blocks: message.blocks.map((block, index) => index === 0 ? { ...block, text: "derived one" } : block) }
      : message);
    preparation.assembly.messages = preparation.s0;
    preparation.program = {
      schema_version: 1, kind: "preparation_program_result", program, basis_state: basis, state: basis,
      stages: [], collection: preparation.collection, view: preparation.send_view,
      evidence_digest: `json-v1:sha256:${"b".repeat(64)}`,
    };
    return value;
  }
  it("accepts locatable v2 send-view text changes without changing the canonical history", () => {
    const preview = decodeWorkbenchContextPreview(advancedPreview(), contextScope());
    if (preview.status !== "ready") throw new Error("Not ready");
    expect(preview.preparation.canonical_messages[1].blocks[0].text).toBe("reasoning one");
    expect(preview.preparation.s0[1].blocks[0].text).toBe("derived one");
    expect(preview.preparation.display_view.overrides).toEqual([]);
  });
  it("rejects protected, duplicate or mismatched advanced evidence", () => {
    const protectedValue = advancedPreview();
    protectedValue.preparation.send_view.overrides[0].message_id = contextId(22);
    expect(() => decodeWorkbenchContextPreview(protectedValue, contextScope())).toThrow("受保护");
    const duplicate = advancedPreview();
    duplicate.preparation.send_view.overrides.push(duplicate.preparation.send_view.overrides[0]);
    expect(() => decodeWorkbenchContextPreview(duplicate, contextScope())).toThrow("受保护");
    const mismatched = advancedPreview();
    mismatched.preparation.program!.program = { kind: "unknown" };
    expect(() => decodeWorkbenchContextPreview(mismatched, contextScope())).toThrow("执行证据");
  });
  it("accepts schema-wrapped A current root and leaves it separate from archived context", () => {
    const preview = decodeWorkbenchContextPreview(contextPreviewFixture(), contextScope());
    expect(preview.status).toBe("ready");
    if (preview.status !== "ready") throw new Error("Not ready");
    expect(preview.input.kind).toBe("ephemeral");
    expect(preview.preparation.s0.at(-1)?.blocks[0].text).toBe('{\n  "text": "next"\n}');
    expect(preview.preparation.node_input).not.toHaveProperty("workflow_session_id");
  });
  it("accepts B pending but forbids an old A result or fake root from being sent", () => {
    expect(decodeWorkbenchContextPreview(contextPreviewFixture(contextScope("B")), contextScope("B"))).toMatchObject({
      status: "pending", preparation: null, input: null,
    });
    expect(() => decodeWorkbenchContextPreview(contextPreviewFixture(), contextScope("B"))).toThrow();
    const invalid = contextPreviewFixture(contextScope("B"));
    (invalid as unknown as Record<string, unknown>).input = { kind: "ephemeral" };
    expect(() => decodeWorkbenchContextPreview(invalid, contextScope("B"))).toThrow("不能伪造");
  });
  it("rejects native-input owner additions and preview changes to canonical history or root schema", () => {
    const preview = contextPreviewFixture();
    if (preview.status !== "ready") throw new Error("Not ready");
    preview.preparation.node_input.workflow_session_id = contextId(1);
    expect(() => decodeWorkbenchContextPreview(preview, contextScope())).toThrow("临时输入");
    delete preview.preparation.node_input.workflow_session_id;
    (preview.preparation.canonical_messages.at(-1)!.blocks[0] as Record<string, unknown>).text = "next";
    expect(() => decodeWorkbenchContextPreview(preview, contextScope())).toThrow("schema 输入包装");
  });
});
