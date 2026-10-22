import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import WorkflowWorkspace from "./WorkflowWorkspace.vue";
import { createWorkflowConfigurationCopy } from "../application/workflowConfigurationCopy";
import { workflowConfigurationCopyOperation, workflowConfigurationCopyOwnerKind,
  type WorkflowConfigurationCopyRequest } from "../domain/workflowConfigurationCopy";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { useWorkbenchNoticesStore } from "../stores/workbenchNotices";

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
  return vi.spyOn(graph, "copyPendingRequests").mockReturnValue(rows.map(row =>
    ({ ...row, busy: false as const, progress: "restored" as const, requestKey: row.workflowId })));
}

function copyButton(html: string) {
  return /<button[^>]*aria-label="复制工作流"[^>]*>/.exec(html)?.[0] ?? "";
}

describe("workflow workspace ownership", () => {
  it("owns the workflow business topbar, view tabs, copy, discard and run controls", async () => {
    const graph = useWorkflowGraphStore();
    stubCopyRows(graph);
    graph.createWorkflow();
    const html = await render();
    for (const label of ["工作流配置", "编辑工作流名称", "主流程", "运行历史",
      "复制工作流", "工作流设置尚未接入", "会话预加载数据", "配置尚未正式接纳"])
      expect(html).toContain(label);
    expect(html).toContain('data-region="run-controls"');
    expect(html).toContain('data-region="canvas"');
    expect(html).not.toContain('data-region="history"');
  });
  it("keeps original requests in top-level notices and a stable toolbar entry while the shell keeps legacy records", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    stubCopyRows(graph);
    const id = graph.createWorkflow();
    workspace.workflows[0]!.title = "带原请求的工作流";
    graph.receiptChecks[id] = "原请求待核实";
    graph.entries[id]!.pending = { action: "save", path: "/api/graph/definitions",
      body: { document: graph.entries[id]!.document, expected_revision: 0, idempotency_key: id } } as never;
    const html = await render();
    expect(html).toContain("带原请求的工作流");
    expect(html).toContain('aria-label="工作流原请求"');
    expect(html).not.toContain("model-pending-banner");
    expect(useWorkbenchNoticesStore().notices).toContainEqual(expect.objectContaining({
      title: "带原请求的工作流", reason: "原请求待核实", severity: "major",
    }));
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
  it("keeps copy available for a formally saved archived source (G-A)", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    stubCopyRows(graph);
    const id = graph.createWorkflow();
    workspace.markWorkflowSaved(id);
    workspace.setWorkflowManagement(id, true);
    const html = await render();
    // Editing stays read-only, but copying the formal current remains allowed.
    expect(html).toContain("已归档 · 只读");
    expect(graph.locked).toBe(true);
    expect(graph.copyLocked).toBe(false);
    expect(copyButton(html)).not.toContain("disabled");
  });
  it("disables copy without a formally saved current", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    stubCopyRows(graph);
    const id = graph.createWorkflow();
    expect(copyButton(await render())).toContain("disabled"); // local draft
    workspace.markWorkflowSaved(id);
    graph.entries[id]!.fixed_only = true;
    expect(copyButton(await render())).toContain("disabled"); // fixed-only source
    delete graph.entries[id]!.fixed_only;
    workspace.setWorkflowManagement(id, true, true);
    expect(copyButton(await render())).toContain("disabled"); // deletion still pending
  });
  it("disables copy from the new copy channel while a result is pending", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    const id = graph.createWorkflow();
    workspace.markWorkflowSaved(id);
    const evidence = memoryRequestEvidence();
    const request: WorkflowConfigurationCopyRequest = {
      source_workflow_definition_id: id, expected_source_definition_revision: 1,
      expected_source_edit_sequence: 1, target_workflow_definition_id: crypto.randomUUID(),
      target_name: "副本", idempotency_key: crypto.randomUUID(),
    };
    await evidence.ports.write({ operation: workflowConfigurationCopyOperation,
      idempotency_key: request.idempotency_key, owner_kind: workflowConfigurationCopyOwnerKind,
      owner_id: id, request: request as unknown as Record<string, unknown> });
    graph.installWorkflowConfigurationCopyApplication(
      createWorkflowConfigurationCopy({ evidence: evidence.ports }));
    graph.copyPendingRequests(); // lazily restores the durable pending channel for the source
    await vi.waitFor(() => expect(graph.copyPendingRequests()[0]?.pending).toBe(true));
    expect(graph.copyLocked).toBe(true);
    const html = await render();
    expect(useWorkbenchNoticesStore().notices.some(row => row.reason.includes("配置复制原请求证据尚未结案"))).toBe(true);
    expect(copyButton(html)).toContain("disabled");
  });
  it("presents a configuration-copy issue without inserting rows before the canvas", async () => {
    const graph = useWorkflowGraphStore();
    graph.createWorkflow();
    stubCopyRows(graph, [{ workflowId: crypto.randomUUID(), title: "复制来源工作流",
      error: "原复制已确认，目标现不可用", busy: false, pending: true }]);
    const html = await render();
    expect(useWorkbenchNoticesStore().notices).toContainEqual(expect.objectContaining({
      title: "复制来源工作流", reason: "原复制已确认，目标现不可用", severity: "major",
    }));
    expect(html).not.toContain("原复制已确认，目标现不可用");
    expect(html).not.toContain("model-pending-banner");
    const app = readFileSync(new URL("../App.vue", import.meta.url), "utf8");
    expect(app).not.toContain("配置复制原请求待核实");
  });
});
