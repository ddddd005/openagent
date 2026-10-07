import { describe, expect, it } from "vitest";
import { contextMaintenanceDiagnosis, contextMaintenanceSummary } from "./contextMaintenance";

const before = { input_tokens: 9000, output_reserve_tokens: 1000, total_tokens: 10000,
  context_window_tokens: 11000, token_count_kind: "utf8_bytes_estimate" };
describe("context maintenance presentation", () => {
  it("distinguishes start, response and applied state with explicit estimates and unknown usage", () => {
    const started = contextMaintenanceSummary({ schema_version: 1, kind: "context_compaction_started",
      payload: { covered_message_ids: ["a", "b"], capacity: before } })!;
    expect(started.title).toBe("上下文精简开始");
    expect(started.lines.join(" ")).toContain("UTF-8 字节估算");
    const finished = contextMaintenanceSummary({ schema_version: 1, kind: "context_compaction_finished",
      payload: { outcome: "responded", result: { usage: null } } })!;
    expect(finished.title).not.toContain("已精简");
    expect(finished.lines).toContain("供应商 usage：未提供");
    const applied = contextMaintenanceSummary({ schema_version: 1, kind: "context_compaction_applied",
      payload: { capacity: { before, after: { ...before, input_tokens: 4000, total_tokens: 5000 } },
        result: { usage: { prompt_cache_hit_tokens: 8000, prompt_cache_miss_tokens: 1000 } } } })!;
    expect(applied.lines.join(" ")).toContain("精简前");
    expect(applied.lines.join(" ")).toContain("精简后");
    expect(applied.lines.join(" ")).toContain('"prompt_cache_hit_tokens":8000');
    expect(applied.lines.join(" ")).not.toContain("费用");
  });
  it("does not mistake unrelated information or failure for a successful replacement", () => {
    expect(contextMaintenanceSummary({ kind: "context_compaction_applied", payload: {} })).toBeNull();
    expect(contextMaintenanceSummary({ schema_version: 1, kind: "tool_settled", payload: {} })).toBeNull();
    expect(contextMaintenanceSummary({ schema_version: 1, kind: "context_compaction_finished",
      payload: { outcome: "model_error", result: null } })!.title).toBe("上下文精简未完成");
    expect(contextMaintenanceDiagnosis({ code: "context_compaction_cold_input_budget_exceeded" })).toContain("未发起摘要调用");
    expect(contextMaintenanceDiagnosis({ code: "context_stale_basis" })).toContain("未覆盖");
    expect(contextMaintenanceDiagnosis({ code: "unknown" })).toBeNull();
    expect(contextMaintenanceDiagnosis({ reason_code: "context_capacity_exceeded" })).toContain("已停止");
  });
  it("unwraps the actual executor information envelope without losing raw usage", () => {
    const fact = { schema_version: 1, kind: "context_compaction_applied",
      payload: { capacity: { before, after: { ...before, input_tokens: 1000, total_tokens: 2000 } },
        result: { usage: null } } };
    const envelope = { executor_ref: { executor_id: "agents.snapshot-kernel", exact_version: "1.0.0" },
      fact_id: "id", owner: {}, generation: 1, payload: fact };
    expect(contextMaintenanceSummary(envelope)).toEqual(contextMaintenanceSummary(fact));
    expect(contextMaintenanceSummary({ payload: { schema_version: 2, kind: fact.kind, payload: fact.payload } })).toBeNull();
  });
  it("recognizes the public fact body without treating arbitrary nested content as versioned evidence", () => {
    const fact = { kind: "context_compaction_started", created_at: "2026-10-07T00:00:00Z",
      payload: { covered_message_ids: ["a"], capacity: before } };
    const envelope = { executor_ref: { executor_id: "agents.snapshot-kernel", exact_version: "1.0.0" },
      fact_id: "public-id", owner: {}, generation: 1, payload: fact };
    expect(contextMaintenanceSummary(envelope)?.title).toBe("上下文精简开始");
    expect(contextMaintenanceSummary({ payload: fact })).toBeNull();
    expect(contextMaintenanceSummary({ ...envelope, generation: 0 })).toBeNull();
    expect(contextMaintenanceSummary({ ...envelope, payload: { ...fact, schema_version: 2 } })).toBeNull();
  });
});
