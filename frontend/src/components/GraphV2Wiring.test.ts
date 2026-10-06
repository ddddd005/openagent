import { describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { createPinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import GraphObjectBindings from "./GraphObjectBindings.vue";
import GraphSessionData from "./GraphSessionData.vue";
import GraphSchemaFields from "./GraphSchemaFields.vue";
import GraphCanvasNode from "./GraphCanvasNode.vue";
import GraphWorkbench from "./GraphWorkbench.vue";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { newGraph, type GraphNodeType, type GraphSession } from "../domain/workflowGraph";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";

vi.mock("@vue-flow/core", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" },
  Handle: { render: () => null }, VueFlow: { render: () => null },
}));
vi.mock("@vue-flow/background", () => ({ Background: { render: () => null } }));

describe("v2 workflow controls and object presentation", () => {
  it("renders both structural transform scopes from schema choices", async () => {
    const html = await renderToString(createSSRApp(GraphSchemaFields, { value: { scope: "all" },
      schema: { properties: { scope: { enum: ["body", "all"] } } } }));
    expect(html).toContain("仅文本"); expect(html).toContain("全部");
    expect(html).toContain('value="&quot;all&quot;"');
  });
  it("renders generic binding JSON and node UUIDs without requiring a variable-specific editor", async () => {
    const document = newGraph("plugin objects");
    document.nodes = [{ node_binding_id: crypto.randomUUID(), component_id: "plugin.state",
      component_version: "1", title: "Plugin state", position: { x: 0, y: 0 }, config: {} }];
    const html = await renderToString(createSSRApp(GraphObjectBindings, { document,
      types: [{ type_id: "plugin.custom-state", schema_version: 7, scope: "session", schema: {} }] }));
    expect(html).toContain("plugin.custom-state@7");
    expect(html).toContain("对象绑定 JSON");
    expect(html).toContain(document.nodes[0].node_binding_id);
    expect(html).toContain("默认值初始化");
  });
  it("presents authoritative session objects separately from legacy variables without issuing writes", async () => {
    const pinia = createPinia(); const graph = useWorkflowGraphStore(pinia);
    graph.setPersistenceGuard(() => true); const id = graph.createWorkflow();
    const sid = crypto.randomUUID(); const revision = crypto.randomUUID();
    const session: GraphSession = { schema_version: 2, execution_model: "graph", workflow_session_id: sid,
      workflow_definition_id: id, definition_revision: 1, revision: 1, data_revision: 0, head_revision: 1,
      status: "idle", can_submit: true, nodes: [], chains: [], outputs: [],
      data: { revision: 0, values: { same: { type: "string", value: "legacy-only" } } },
      objects: { same: { revision_id: revision, revision: 3, type_id: "plugin.object", schema_version: 1,
        deleted: false, value: "session-current", binding: { object_key: "same", type_id: "plugin.object",
          schema_version: 1, scope: "shared", owner_node_id: null, readers: [], writers: [] } } } };
    graph.entries[id].session_id = sid; graph.views[sid] = session;
    const write = vi.spyOn(graph, "writeData");
    const html = await renderToString(createSSRApp(GraphSessionData).use(pinia).provide(workbenchFrontendHostKey,
      createWorkbenchFrontendHost(() => graph.frontendExtensions, () => graph.packageLock)));
    expect(html).toContain("会话对象"); expect(html).toContain("session-current");
    expect(html).toContain("旧变量（历史机制）"); expect(html).toContain("legacy-only");
    expect(html).toContain(revision); expect(write).not.toHaveBeenCalled();
    graph.$dispose(); useWorkspaceStore(pinia).$dispose();
  });
  it("shows root and predecessor controls for a node with no content output handles", async () => {
    const pinia = createPinia(); const graph = useWorkflowGraphStore(pinia);
    const definition: GraphNodeType = { component_id: "plugin.register", component_version: "1",
      display_name: "register", category: "plugin", config_schema: {}, default_config: {},
      inputs: [], outputs: [], is_output: false, executable: true, input_storage: "references",
      object_accesses: [{ config_field: "object_key", multiple: false, access: "write",
        type_id: "plugin.state", schema_version: 1 }] };
    graph.setPersistenceGuard(() => true); graph.catalog = [definition]; graph.createWorkflow();
    const nodeId = graph.addNode("plugin.register@1", { x: 0, y: 0 })!;
    graph.setExecutionRoot(nodeId, true);
    const html = await renderToString(createSSRApp(GraphWorkbench).use(pinia));
    expect(html).toContain("显式执行根"); expect(html).toContain("控制依赖");
    expect(html).toContain("会话绑定"); expect(html).toContain(nodeId);
    expect(html).toContain("会话对象访问契约"); expect(html).toContain("plugin.state");
    const nodeHtml = await renderToString(createSSRApp(GraphCanvasNode, {
      data: { node: graph.document!.nodes[0], definition, status: "idle", inputs: [], outputs: [],
        error: false, executionRoot: true },
    }).use(pinia));
    expect(nodeHtml).toContain("执行根");
    expect(nodeHtml).not.toContain("TEXT"); expect(nodeHtml).not.toContain("PROMPT");
    graph.$dispose(); useWorkspaceStore(pinia).$dispose();
  });
});
