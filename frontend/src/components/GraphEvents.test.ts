import { createSSRApp } from "vue";
import { createPinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import { describe, expect, it, vi } from "vitest";
import GraphEvents from "./GraphEvents.vue";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";

describe("minimum event workbench", () => {
  it("renders declaration and payload JSON without implicit session creation or network requests", async () => {
    const pinia = createPinia(), graph = useWorkflowGraphStore(pinia);
    graph.setPersistenceGuard(() => true); graph.createWorkflow();
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    try {
      const before = graph.storeSnapshot(), html = await renderToString(createSSRApp(GraphEvents).use(pinia));
      expect(html).toContain("事件绑定 JSON"); expect(html).toContain("事件载荷 JSON");
      expect(html).toContain("提交局部事件"); expect(html).toContain("尚未选择工作流会话");
      expect(fetcher).not.toHaveBeenCalled(); expect(graph.storeSnapshot()).toEqual(before);
    } finally { graph.$dispose(); useWorkspaceStore(pinia).$dispose(); vi.unstubAllGlobals(); }
  });
});
