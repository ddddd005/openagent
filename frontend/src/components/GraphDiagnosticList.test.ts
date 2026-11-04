import { describe, expect, it } from "vitest";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import GraphDiagnosticList from "./GraphDiagnosticList.vue";
import type { GraphDiagnostic } from "../domain/workflowGraph";

async function render(diagnostics: GraphDiagnostic[], sessionLabels?: Record<string, string>) {
  return await renderToString(createSSRApp(GraphDiagnosticList, { diagnostics, sessionLabels }));
}

describe("workflow diagnostic presentation", () => {
  it("shows phase, code, JSON Pointer, node/edge/port/object-key identity and bounded expected/actual", async () => {
    const diagnostic: GraphDiagnostic = { reason_code: "graph_missing_edge_node", code: "graph_missing_edge_node",
      message: "数据边目标节点不存在", phase: "save", path: "/edges/2/target_node_id",
      node_id: crypto.randomUUID(), edge_id: crypto.randomUUID(), port_id: "input",
      object_key: "shared/main", expected: "existing-node", actual: null };
    const html = await render([diagnostic]);
    expect(html).toContain("保存校验");
    expect(html).toContain("graph_missing_edge_node");
    expect(html).toContain("/edges/2/target_node_id");
    expect(html).toContain(`节点 ${diagnostic.node_id}`);
    expect(html).toContain(`连线 ${diagnostic.edge_id}`);
    expect(html).toContain("端口 input");
    expect(html).toContain("对象键 shared/main");
    expect(html).toContain("期望 &quot;existing-node&quot;；实际 null");
  });
  it("shows G21 occupant sessions with titles and marks the prepare phase as major", async () => {
    const session = crypto.randomUUID(), chain = crypto.randomUUID();
    const html = await render([{ reason_code: "configuration_run_locked", code: "configuration_run_locked",
      message: "工作流会话正在运行", phase: "prepare",
      occupying_sessions: [{ workflow_session_id: session, active_chain_run_id: chain }] }],
    { [session]: "长跑测试" });
    expect(html).toContain("运行准备");
    expect(html).toContain("major");
    expect(html).toContain("占用：长跑测试");
    expect(html).toContain(`会话 ${session} · 活动链 ${chain}`);
  });
  it("states explicitly when no structured diagnostics are present", async () => {
    expect(await render([])).toContain("无结构化诊断");
  });
});
