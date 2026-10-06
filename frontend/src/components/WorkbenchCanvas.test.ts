import { describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { createPinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import WorkbenchCanvas from "./WorkbenchCanvas.vue";
import PreparationWorkbench from "./PreparationWorkbench.vue";
import { usePreparationStore } from "../stores/preparation";
import { useWorkspaceStore } from "../stores/workspace";
import { EMPTY_WORKFLOW_ID, MAIN_WORKFLOW_ID } from "../fixtures/workflows";

vi.mock("@vue-flow/core", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Left: "left", Right: "right" },
  Handle: { render: () => null },
  VueFlow: { render: () => null },
}));
vi.mock("@vue-flow/background", () => ({ Background: { render: () => null } }));

describe("workbench menu integration rendering", () => {
  it("keeps the fixed main flow unchanged when the menu is closed", async () => {
    const pinia = createPinia();
    const html = await renderToString(createSSRApp(WorkbenchCanvas).use(pinia));
    expect(html).toContain("默认测试工作流画布");
    expect(html).toContain("3 节点");
    expect(useWorkspaceStore(pinia).nodes).toHaveLength(3);
    expect(html).not.toContain("data-canvas-node-menu");
  });

  it("does not initialize preparation drafts for the empty switch-testing workflow", async () => {
    const pinia = createPinia();
    useWorkspaceStore(pinia).openWorkflow(EMPTY_WORKFLOW_ID);
    const html = await renderToString(createSSRApp(WorkbenchCanvas).use(pinia));
    expect(html).toContain("0 节点");
    expect(usePreparationStore(pinia).drafts).toEqual({});
  });

  it("selects a node added through the main canvas when entering its preparation graph", async () => {
    const pinia = createPinia();
    const preparation = usePreparationStore(pinia);
    const id = preparation.addNode(MAIN_WORKFLOW_ID, "B", "prompt-item");
    preparation.updateNodeTitle(MAIN_WORKFLOW_ID, "B", id!, "菜单新增条目");
    const html = await renderToString(createSSRApp(PreparationWorkbench, {
      workflowId: MAIN_WORKFLOW_ID, stage: "B", initialSelectedId: id,
    }).use(pinia));
    expect(html).toContain("Agent B 准备画布");
    expect(html).toContain("菜单新增条目");
    expect(html).toContain('aria-keyshortcuts="Shift+A"');
    expect(html).not.toContain("data-canvas-node-menu");
  });
});
