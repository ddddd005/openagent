import { afterEach, describe, expect, it, vi } from "vitest";
import { computed, createSSRApp } from "vue";
import { createPinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import GraphFrontendWiring from "./GraphFrontendWiring.vue";
import GraphNodeConfiguration from "./GraphNodeConfiguration.vue";
import GraphFrontendDisplay from "./GraphFrontendDisplay.vue";
import GraphSessionData from "./GraphSessionData.vue";
import { graphClone } from "../domain/workflowGraph";
import { frontendCatalog, frontendExecutionLock, frontendProjectLock, frontendSession } from "../testUtils/frontendFixture";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { workflowFrontendSdkKey } from "../plugins/workflowFrontendSdk";
import { readFrontendArtifact } from "../application/workflowFrontendDisplay";
import { workflowFrontendExtensions } from "../plugins/workflowFrontendManifest";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";

afterEach(() => vi.unstubAllGlobals());
function fixture() {
  const pinia = createPinia(), graph = useWorkflowGraphStore(pinia);
  graph.setPersistenceGuard(() => true); graph.catalog = graphClone(frontendCatalog);
  graph.packageLock = graphClone(frontendProjectLock); graph.executionPackageLock = graphClone(frontendExecutionLock);
  graph.frontendExtensions = graphClone(workflowFrontendExtensions);
  const id = graph.createWorkflow();
  return { pinia, graph, id, dispose: () => { graph.$dispose(); useWorkspaceStore(pinia).$dispose(); } };
}
function appFor(component: Parameters<typeof createSSRApp>[0], graph: ReturnType<typeof useWorkflowGraphStore>,
  pinia: ReturnType<typeof createPinia>, props?: Record<string, unknown>) {
  return createSSRApp(component, props).use(pinia)
    .provide(workbenchFrontendHostKey, createWorkbenchFrontendHost(() => graph.frontendExtensions, () => graph.packageLock))
    .provide(workflowFrontendSdkKey, {
    workflowId: computed(() => useWorkspaceStore(pinia).activeWorkflowId),
    document: computed(() => graph.document), catalog: computed(() => graph.catalog),
    packages: computed(() => graph.packageLock), session: computed(() => graph.session),
    locked: computed(() => graph.locked), lifecycle: computed(() => "ssr"),
    attachDisplay: request => !!graph.attachFrontendDisplay(request), readArtifact: readFrontendArtifact,
    configureNode: request => graph.patchNodeConfiguration(request),
  });
}
describe("frontend workbench controls and display", () => {
  it("shows an explicit missing-package state and never silently creates display nodes", async () => {
    const { pinia, graph, dispose } = fixture(); graph.packageLock = [];
    const before = graphClone(graph.document);
    const html = await renderToString(appFor(GraphFrontendWiring, graph, pinia));
    expect(html).toContain("workflow.frontend-business@1.0.0"); expect(html).toContain("packages.configure");
    expect(html).toContain("当前图没有 TEXT@2 输出");
    expect(graph.document).toEqual(before); dispose();
  });
  it("reuses the schema editor for role/object fields and exposes the actual TEXT@2 source edges", async () => {
    const { pinia, graph, dispose } = fixture();
    const source = graph.addNode("tools.current-input@1", { x: 0, y: 0 })!;
    graph.attachFrontendDisplay({ sourceNodeId: source, sourcePortId: "output", objectKey: "frontend", role: "user" });
    const node = graph.document!.nodes.find(row => row.component_id === "frontend.state.append")!;
    const html = await renderToString(appFor(GraphNodeConfiguration, graph, pinia, { node,
      definition: graph.typeFor(node)!, document: graph.document!, catalog: graph.catalog }));
    expect(html).toContain("object_key"); expect(html).toContain("role");
    expect(html).toContain("展示文本来源"); expect(html).toContain("Input / output");
    expect(html).toContain("移除此文本来源"); expect(html).toContain("添加文本来源");
    expect(html).toContain('value="&quot;user&quot;"');
    dispose();
  });
  it("keeps generic fields and objects available without declared UI or an exact target match", async () => {
    const { pinia, graph, id, dispose } = fixture();
    const source = graph.addNode("tools.current-input@1", { x: 0, y: 0 })!;
    graph.attachFrontendDisplay({ sourceNodeId: source, sourcePortId: "output", objectKey: "frontend", role: "user" });
    const node = graph.document!.nodes.find(row => row.component_id === "frontend.state.append")!;
    graph.frontendExtensions = [];
    const fields = await renderToString(appFor(GraphNodeConfiguration, graph, pinia, { node,
      definition: graph.typeFor(node)!, document: graph.document!, catalog: graph.catalog }));
    expect(fields).toContain("object_key"); expect(fields).toContain("role");
    expect(fields).not.toContain("展示文本来源");
    const session = frontendSession(id);
    graph.entries[id].session_id = session.workflow_session_id; graph.views[session.workflow_session_id] = session;
    let html = await renderToString(appFor(GraphSessionData, graph, pinia));
    expect(html).toContain("会话对象"); expect(html).not.toContain("正式前端展示");
    graph.frontendExtensions = graphClone(workflowFrontendExtensions);
    session.objects!.frontend.schema_version = 2;
    html = await renderToString(appFor(GraphSessionData, graph, pinia));
    expect(html).toContain("workflow.frontend-state@2"); expect(html).not.toContain("正式前端展示");
    dispose();
  });
  it.each(["deleted", "invalid", "empty", "no-reader"] as const)(
    "shows %s display state without issuing an artifact read or any mutation", async state => {
      const { pinia, graph, id, dispose } = fixture(), session = frontendSession(id), object = session.objects!.frontend;
      if (state === "deleted") object.deleted = true;
      else if (state === "invalid") object.value = { messages: ["inline fallback"] };
      else if (state === "empty") object.value = { entries: [], view_ref: null };
      else object.binding.readers = [];
      graph.entries[id].session_id = session.workflow_session_id; graph.views[session.workflow_session_id] = session;
      const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
      const html = await renderToString(appFor(GraphFrontendDisplay, graph, pinia, { objectKey: "frontend" }));
      expect(html).toContain({ deleted: "已删除", invalid: "契约无效", empty: "尚无展示条目", "no-reader": "没有已授权" }[state]);
      expect(fetcher).not.toHaveBeenCalled(); dispose();
    });
});
