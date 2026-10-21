import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import WorkflowWorkspace from "./WorkflowWorkspace.vue";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";

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

async function render() {
  return await renderToString(createSSRApp(WorkflowWorkspace).use(pinia));
}

function stubCopyRows(graph: ReturnType<typeof useWorkflowGraphStore>,
  rows: { workflowId: string; title: string; error: string; busy: boolean; pending: boolean }[] = []) {
  return vi.spyOn(graph, "copyPendingRequests").mockReturnValue(rows);
}

describe("workflow workspace ownership", () => {
  it("owns the workflow business topbar, view tabs, copy, discard and run controls", async () => {
    const graph = useWorkflowGraphStore();
    stubCopyRows(graph);
    graph.createWorkflow();
    const html = await render();
    for (const label of ["工作流配置", "编辑工作流名称", "主流程", "运行历史",
      "复制工作流", "工作流设置尚未接入", "全局变量注册尚未接入", "配置尚未正式接纳"])
      expect(html).toContain(label);
    expect(html).toContain('data-region="run-controls"');
    expect(html).toContain('data-region="canvas"');
    expect(html).not.toContain('data-region="history"');
  });
  it("owns the graph original-request verification banner while the project shell keeps legacy records", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    stubCopyRows(graph);
    const id = graph.createWorkflow();
    workspace.workflows[0]!.title = "带原请求的工作流";
    graph.receiptChecks[id] = "原请求待核实";
    graph.entries[id]!.pending = { action: "save", path: "/api/graph/definitions",
      body: { document: graph.entries[id]!.document, expected_revision: 0, idempotency_key: id } } as never;
    const html = await render();
    expect(html).toContain("带原请求的工作流");
    expect(html).toContain("再次只读核实");
    const app = readFileSync(new URL("../App.vue", import.meta.url), "utf8");
    expect(app).toContain("旧原请求");
    for (const businessLabel of ["复制工作流", "编辑工作流名称", "运行历史", "再次只读核实"])
      expect(app).not.toContain(businessLabel);
  });
  it("shows the editable name form and discard action only for a dirty draft", async () => {
    const graph = useWorkflowGraphStore();
    stubCopyRows(graph);
    graph.createWorkflow();
    const html = await render();
    expect(html).toContain("编辑工作流名称");
    expect(html).toContain("配置尚未正式接纳");
    expect(html).toContain("放弃工作流修改");
    expect(html).not.toContain('aria-label="取消修改名称"');
  });
  it("marks a formal fixed revision without current configuration as read-only", async () => {
    const graph = useWorkflowGraphStore();
    stubCopyRows(graph);
    const id = graph.createWorkflow();
    const entry = graph.entries[id]!;
    entry.fixed_only = true;
    const html = await render();
    expect(html).toContain("固定修订 · 只读");
  });
  it("shows the configuration-copy pending row in the workflow region only", async () => {
    const graph = useWorkflowGraphStore();
    graph.createWorkflow();
    stubCopyRows(graph, [{ workflowId: crypto.randomUUID(), title: "复制来源工作流",
      error: "原复制已确认，目标现不可用", busy: false, pending: true }]);
    const html = await render();
    expect(html).toContain("复制来源工作流");
    expect(html).toContain("原复制已确认，目标现不可用");
    expect(html).toContain("再次只读核实");
    const app = readFileSync(new URL("../App.vue", import.meta.url), "utf8");
    expect(app).not.toContain("配置复制原请求待核实");
  });
});
