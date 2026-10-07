import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { describe, expect, it } from "vitest";
import GraphSchemaFields from "./GraphSchemaFields.vue";
import ContextMaintenanceFact from "./ContextMaintenanceFact.vue";

describe("compaction controls and evidence", () => {
  it("renders target_tokens as reserved read-only output and the summary prompt as an editable field", async () => {
    const html = await renderToString(createSSRApp(GraphSchemaFields, {
      schema: { properties: { target_tokens: { type: "null", readOnly: true, title: "目标 tokens" },
        summary_prompt: { type: "string", title: "精简提示词" }, keep_depth: { type: "integer", minimum: 0 } } },
      value: { target_tokens: null, summary_prompt: "", keep_depth: 0 },
    }));
    expect(html).toMatch(/<output[^>]*>未启用<\/output>/);
    expect(html.match(/<textarea/g)).toHaveLength(1);
    expect(html).toContain('type="number"'); expect(html).toContain('min="0"');
  });
  it("displays maintenance evidence while keeping the original facts available", async () => {
    const item = { kind: "context_compaction_finished", created_at: "2026-10-07T00:00:00Z",
      payload: { compaction_id: "evidence-id", outcome: "responded", result: { usage: null } } };
    const html = await renderToString(createSSRApp(ContextMaintenanceFact, {
      item: { executor_ref: { executor_id: "agents.snapshot-kernel" }, fact_id: "public-id",
        owner: {}, generation: 1, payload: item } }));
    expect(html).toContain("上下文精简响应已收到"); expect(html).toContain("未提供");
    expect(html).toContain("原始证据"); expect(html).toContain("evidence-id");
    const fallback = await renderToString(createSSRApp(ContextMaintenanceFact, { item: { arbitrary: "raw" } }));
    expect(fallback).not.toContain("上下文维护信息"); expect(fallback).toContain("arbitrary");
  });
});
