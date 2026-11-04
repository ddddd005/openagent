import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import WorkflowWorkspace from "./WorkflowWorkspace.vue";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { installDebtCleanupConfiguration } from "../testUtils/debtCleanupConfiguration";
import { installDebtCleanupEvidence } from "../testUtils/debtCleanupEvidence";
import type { GraphNodeType } from "../domain/workflowGraph";
import { useWorkflowGraphStore } from "../stores/workflowGraph";

// S1-03 follow-up: the whole edit-state block is a floating overlay. A normal save
// must stay silent and must never appear in the layout flow, so an ordinary node move
// can no longer resize or move the canvas.
const nodeType: GraphNodeType = { component_id: "sample.node", component_version: "1",
  display_name: "Sample", category: "Test", default_config: { text: "before" },
  config_schema: { type: "object", properties: { text: { type: "string" } } },
  inputs: [], outputs: [], executable: true, is_output: false };

vi.mock("./GraphRunControls.vue", async () => {
  const { h } = await import("vue");
  return { default: { render: () => h("span", { "data-region": "run-controls" }) } };
});
vi.mock("./GraphWorkbench.vue", async () => {
  const { h } = await import("vue");
  return { default: { render: () => h("section", { "data-region": "canvas" }) } };
});
vi.mock("./GraphHistory.vue", async () => {
  const { h } = await import("vue");
  return { default: { render: () => h("section", { "data-region": "history" }) } };
});

let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => { disposePinia(pinia); vi.restoreAllMocks(); });

const render = async () => await renderToString(createSSRApp(WorkflowWorkspace).use(pinia));

describe("workflow edit state overlay", () => {
  it("keeps a normal node move silent: no edit-state block before, during or after the save", async () => {
    const graph = useWorkflowGraphStore();
    graph.catalog = [nodeType];
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const workflowId = await graph.createWorkflow();
    await configuration.settled(workflowId);
    const nodeId = (await graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    await configuration.settled(workflowId);

    expect(await render()).not.toContain("workflow-edit-state");

    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const original = configuration.update.getMockImplementation()!;
    configuration.update.mockImplementationOnce(async request => { await gate; return original(request); });
    const edit = graph.moveNodes([{ id: nodeId, position: { x: 40, y: 50 } }]);
    await vi.waitFor(() => expect(graph.activeConfigurationRequest?.progress).toBe("request-in-flight"));

    const during = await render();
    expect(during).not.toContain("workflow-edit-state");
    for (const silent of ["原请求正在提交", "业务已确认", "workflow-edit-state-pill"])
      expect(during).not.toContain(silent);

    release();
    expect(await edit).toMatchObject({ outcome: "accepted" });
    expect(await render()).not.toContain("workflow-edit-state");
  });

  it("renders a real failure in an absolutely positioned overlay with a collapse control", async () => {
    const graph = useWorkflowGraphStore();
    graph.catalog = [nodeType];
    const configuration = installDebtCleanupConfiguration(graph, installDebtCleanupEvidence());
    const workflowId = await graph.createWorkflow();
    await configuration.settled(workflowId);
    const nodeId = (await graph.addNode("sample.node@1", { x: 0, y: 0 }))!;
    await configuration.settled(workflowId);
    configuration.update.mockRejectedValueOnce(new WorkbenchApiError(
      "rejected", "工作流正在修改，请等待当前请求结束后按原基线重试", 409, "workflow_modification_busy"));
    expect(await graph.patchNode(nodeId, { title: "rejected" })).toMatchObject({ outcome: "failed" });

    const html = await render();
    expect(html).toContain("workflow-edit-state");
    expect(html).toContain("本次修改未生效（业务未发送）");
    expect(html).toContain("工作流正在修改");
    expect(html).toContain("收起工作流修改状态");
    expect(html).toContain("查看失败提案");
  });

  it("declares the edit-state block and the collapsed pill as layout-free floating layers", () => {
    const source = readFileSync(new URL("./WorkflowWorkspace.vue", import.meta.url), "utf8");
    expect(source).toMatch(/\.workflow-edit-state \{[^}]*position:\s*absolute/);
    expect(source).toMatch(/\.workflow-edit-state \{[^}]*z-index:\s*120/);
    expect(source).toMatch(/\.workflow-edit-state-pill \{[^}]*position:\s*absolute/);
    // The floating layer is anchored to the workspace, which must provide the containing block.
    expect(source).toMatch(/\.workflow-workspace \{[^}]*position:\s*relative/);
    // A collapsed panel stays discoverable through the expandable pill.
    expect(source).toContain('aria-label="展开工作流修改状态"');
  });
});
