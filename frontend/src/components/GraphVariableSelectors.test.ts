import { afterEach, describe, expect, it, vi } from "vitest";
import { computed, createSSRApp } from "vue";
import { createPinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import GraphNodeConfiguration from "./GraphNodeConfiguration.vue";
import GraphVariableRegistration from "./GraphVariableRegistration.vue";
import GraphWorkbench from "./GraphWorkbench.vue";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";
import { graphClone, newGraph, type GraphNode, type GraphNodeType } from "../domain/workflowGraph";
import { lorebookPackage, newLorebookEntry } from "../domain/lorebook";
import { variableRegistrationEvent } from "../domain/workflowVariableRegistration";
import { tavernFrontendExtensions, globalTavernFrontendExtensions } from "../plugins/tavernFrontendManifest";
import { workflowFrontendSdkKey, type WorkflowFrontendSdk } from "../plugins/workflowFrontendSdk";
import { frontendSession } from "../testUtils/frontendFixture";
import { variableCatalog } from "../testUtils/variableFixture";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";

vi.mock("@vue-flow/core", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" },
  Handle: { render: () => null }, VueFlow: { render: () => null },
}));
vi.mock("@vue-flow/background", () => ({ Background: { render: () => null } }));

afterEach(() => vi.restoreAllMocks());
describe("exact variable selectors and local registration state", () => {
  it.each(["read", "write", "read_write"] as const)("filters %s by exact type, version and explicit permission", async access => {
    const nodeId = crypto.randomUUID(), other = crypto.randomUUID(), document = newGraph("Variable selectors");
    const node = { node_binding_id: nodeId, component_id: "tools.variable-assign", component_version: "1",
      title: "Assign", config: { object_key: "item/no-acl" }, position: { x: 0, y: 0 } };
    document.nodes = [node];
    const base = { object_key: "item/both", type_id: "workflow.variable", schema_version: 1,
      scope: "shared" as const, owner_node_id: null, readers: [nodeId], writers: [nodeId] };
    document.object_bindings = [base,
      { ...base, object_key: "item/read", writers: [other] },
      { ...base, object_key: "item/write", readers: [other] },
      { ...base, object_key: "item/no-acl", readers: [other], writers: [other] },
      { ...base, object_key: "item/wrong-version", schema_version: 2 },
      { ...base, object_key: "item/context", type_id: "workflow.effective-context" }];
    const definition = graphClone(variableCatalog[1]!);
    definition.object_accesses![0]!.access = access;
    const original = graphClone(document);
    const html = await renderToString(createSSRApp(GraphNodeConfiguration, { node, document, definition })
      .provide(workbenchFrontendHostKey, createWorkbenchFrontendHost(() => [], () => [])));
    expect(html).toContain('value="item/both"');
    expect(html.includes('value="item/read"')).toBe(access === "read");
    expect(html.includes('value="item/write"')).toBe(access === "write");
    for (const key of ["item/no-acl", "item/wrong-version", "item/context"])
      expect(html).not.toContain(`<option value="${key}"`);
    expect(html).toContain("当前键没有匹配的登记项和明确权限");
    expect(document).toEqual(original);
  });

  it.each(["lorebook.item", "lorebook.group", "lorebook.global-activate"] as const)(
    "lets the installed %s editor own object_keys once while preserving its invalid-key diagnosis", async componentId => {
      const selectedPackage = componentId === "lorebook.global-activate"
        ? { ...lorebookPackage, version: "1.1.0" } : lorebookPackage;
      const document = newGraph("Lorebook variables", [selectedPackage]);
      const node = { node_binding_id: crypto.randomUUID(), component_id: componentId, component_version: "1",
        title: "Lorebook", position: { x: 0, y: 0 },
        config: componentId === "lorebook.item" ? { entry: newLorebookEntry(), object_keys: ["item/stale"] }
          : componentId === "lorebook.group" ? { entries: [], object_keys: ["item/stale"] }
          : { object_keys: ["item/stale"] } };
      document.nodes = [node];
      document.object_bindings = [{ object_key: "item/allowed", type_id: "workflow.variable", schema_version: 1,
        scope: "shared", owner_node_id: null, readers: [node.node_binding_id], writers: [] }];
      const definition: GraphNodeType = { component_id: componentId, component_version: "1",
        display_name: "Lorebook", category: "Tavern", default_config: node.config, config_schema: {
          properties: { object_keys: { type: "array" } } }, executable: true, is_output: false, inputs: [], outputs: [],
        object_accesses: [{ config_field: "object_keys", multiple: true, access: "read",
          type_id: "workflow.variable", schema_version: 1 }] };
      const sdk: WorkflowFrontendSdk = {
        workflowId: computed(() => document.workflow_definition_id), document: computed(() => document),
        catalog: computed(() => [definition]), packages: computed(() => [selectedPackage]),
        session: computed(() => null), locked: computed(() => false), lifecycle: computed(() => "current"),
        configureNode: vi.fn(),
      };
      const declarations = componentId === "lorebook.global-activate" ? globalTavernFrontendExtensions : tavernFrontendExtensions;
      const host = createWorkbenchFrontendHost(() => declarations, () => [selectedPackage]);
      const html = await renderToString(createSSRApp(GraphNodeConfiguration, { node, document, definition })
        .provide(workbenchFrontendHostKey, host).provide(workflowFrontendSdkKey, sdk));
      expect(html).toContain('aria-label="读取变量对象 item/allowed"');
      expect(html).toContain("item/stale");
      expect(html).toContain("无读取权限或类型不匹配");
      expect(html).not.toContain('class="object-access-field"');
      expect(html).not.toContain("会话数据项（可多选）");
    });

  it("shows an explicit disabled registration button without a session, then displays false and zero in the selected session", async () => {
    vi.spyOn(requestEvidencePorts, "read").mockResolvedValue([]);
    const pinia = createPinia(), graph = useWorkflowGraphStore(pinia);
    const id = graph.createWorkflow();
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    graph.catalog = graphClone(variableCatalog);
    const nodeId = graph.addNode("tools.variable-register@2", { x: 0, y: 0 })!;
    const render = () => renderToString(createSSRApp(GraphVariableRegistration, { nodeId }).use(pinia));
    try {
      const noSession = await render();
      expect(noSession).toContain("注册变量及初值"); expect(noSession).toContain("disabled");
      expect(noSession).toContain("尚无当前修订的会话");
      const binding = graph.document!.object_bindings![0]!, session = frontendSession(id);
      session.objects = { [binding.object_key]: { revision: 1, revision_id: crypto.randomUUID(), deleted: false,
        type_id: binding.type_id, schema_version: binding.schema_version, binding: graphClone(binding),
        value: { registered: true, assigned: true, name: "flag", value_type: "boolean", value: false } } };
      graph.entries[id]!.session_id = session.workflow_session_id;
      graph.views[session.workflow_session_id] = session;
      graph.entries[id]!.saved_revision = 1; graph.entries[id]!.saved_document = graphClone(graph.document!);
      expect(await render()).toContain("已赋值：false");
      graph.views[session.workflow_session_id]!.objects![binding.object_key]!.value = {
        registered: true, assigned: true, name: "counter", value_type: "integer", value: 0 };
      expect(await render()).toContain("已赋值：0");
      graph.views[session.workflow_session_id]!.status = "running";
      expect(await render()).toContain("disabled");
      const ownEvent = variableRegistrationEvent(nodeId);
      expect(graph.document!.event_bindings).toContainEqual(ownEvent);
    } finally { graph.$dispose(); useWorkspaceStore(pinia).$dispose(); }
  });

  it("renders registration initial values as typed fields and reports invalid names and values inline", async () => {
    const document = newGraph("Variable fields"), definition = graphClone(variableCatalog[0]!);
    definition.config_schema = { type: "object", properties: {
      name: { type: "string" }, value_type: { enum: ["string", "integer", "number", "boolean"] },
      has_initial: { type: "boolean" }, initial_value: { type: ["string", "integer", "number", "boolean", "null"] },
    } };
    const node: GraphNode = { node_binding_id: crypto.randomUUID(), component_id: definition.component_id,
      component_version: definition.component_version, title: "Variable", position: { x: 0, y: 0 },
      config: { ...graphClone(definition.default_config), name: "1", has_initial: true, initial_value: 123 } };
    document.nodes = [node];
    const render = () => renderToString(createSSRApp(GraphNodeConfiguration, { node, document, definition })
      .provide(workbenchFrontendHostKey, createWorkbenchFrontendHost(() => [], () => [])));
    expect(await render()).toContain("不能以数字开头");
    expect(await render()).toContain("初值必须是字符串");
    node.config = { ...node.config, name: "counter", value_type: "integer", initial_value: 0 };
    expect(await render()).toContain('inputmode="decimal"');
    expect(await render()).not.toContain("初值必须");
    node.config = { ...node.config, value_type: "boolean", initial_value: false };
    expect((await render()).match(/type="checkbox"/g)).toHaveLength(2);
  });

  it("presents registration as active interaction without exposing execution-root controls", async () => {
    vi.spyOn(requestEvidencePorts, "read").mockResolvedValue([]);
    const pinia = createPinia(), graph = useWorkflowGraphStore(pinia);
    graph.catalog = graphClone(variableCatalog); graph.createWorkflow();
    await vi.waitFor(() => expect(graph.locked).toBe(false));
    const render = () => renderToString(createSSRApp(GraphWorkbench).use(pinia).provide(workbenchFrontendHostKey,
      createWorkbenchFrontendHost(() => [], () => [])));
    try {
      graph.addNode("tools.variable-register@2", { x: 0, y: 0 });
      const registration = await render();
      expect(registration).toContain("主动交互");
      expect(registration).toContain("注册变量及初值");
      expect(registration).not.toContain("作为运行目标");
      expect(registration).not.toContain("运行目标");
      graph.addNode("tools.variable-assign@1", { x: 200, y: 0 });
      const assignment = await render();
      expect(assignment).not.toContain("主动交互");
      expect(assignment).not.toContain("注册变量及初值");
      expect(assignment).toContain("前序节点");
      expect(assignment).not.toContain("作为运行目标");
    } finally { graph.$dispose(); useWorkspaceStore(pinia).$dispose(); }
  });
});
