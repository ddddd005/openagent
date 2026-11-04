import { describe, expect, it } from "vitest";
import { diagnosticExpectation, diagnosticFailureStage, diagnosticLocation, diagnosticPhaseLabel,
  diagnosticValueSummary, occupyingSessionLabels } from "./diagnosticPresentation";

describe("workflow diagnostic presentation", () => {
  it("labels every round-3 phase and shows the locatable fields without whole payloads", () => {
    const diagnostic = { reason_code: "graph_missing_edge_node", code: "graph_missing_edge_node",
      message: "数据边目标节点不存在", phase: "save", path: "/edges/2/target_node_id",
      edge_id: crypto.randomUUID(), node_id: crypto.randomUUID(), port_id: "input",
      object_key: "shared/main", expected: "existing-node", actual: null } as const;
    expect(diagnosticPhaseLabel("save")).toBe("保存校验");
    expect(diagnosticPhaseLabel("prepare")).toBe("运行准备");
    expect(diagnosticPhaseLabel("settlement")).toBe("结算");
    expect(diagnosticPhaseLabel(undefined)).toBe("阶段未标注");
    expect(diagnosticLocation(diagnostic)).toContain("graph_missing_edge_node");
    expect(diagnosticLocation(diagnostic)).toContain("/edges/2/target_node_id");
    expect(diagnosticLocation(diagnostic)).toContain(`连线 ${diagnostic.edge_id}`);
    expect(diagnosticLocation(diagnostic)).toContain(`节点 ${diagnostic.node_id}`);
    expect(diagnosticLocation(diagnostic)).toContain("对象键 shared/main");
    expect(diagnosticExpectation(diagnostic)).toBe('期望 "existing-node"；实际 null');
    expect(diagnosticFailureStage({ ...diagnostic, failure_stage: "validation" })).toBe("保存校验 / validation");
  });
  it("bounds expected/actual summaries and never renders arbitrarily long values", () => {
    expect(diagnosticValueSummary("x".repeat(500))).toHaveLength(121);
    expect(diagnosticValueSummary({ secret: "y".repeat(500) })).toContain("object{");
    expect(diagnosticValueSummary(undefined)).toBe("");
    expect(diagnosticValueSummary(true)).toBe("true");
  });
  it("renders G21 occupant sessions with optional labels and active chain identity", () => {
    const session = crypto.randomUUID(), chain = crypto.randomUUID(), idle = crypto.randomUUID();
    expect(occupyingSessionLabels([{ workflow_session_id: session, active_chain_run_id: chain },
      { workflow_session_id: idle, active_chain_run_id: null }], { [session]: "长跑测试" })).toEqual([
      `长跑测试 会话 ${session} · 活动链 ${chain}`,
      `会话 ${idle} · 无活动链`,
    ]);
    expect(occupyingSessionLabels(undefined)).toEqual([]);
  });
});
