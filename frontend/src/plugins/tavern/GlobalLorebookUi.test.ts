import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse as parseSfc } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { computed, createRenderer, createSSRApp, nextTick, reactive, ref, type App, type Component } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkbenchApiError } from "../../adapters/workbenchApi";
import { createWorkbenchFrontendHost } from "../../application/workflowFrontendPackage";
import { newLorebookEntry } from "../../domain/lorebook";
import { graphClone, newGraph, type GraphDocument, type GraphNode } from "../../domain/workflowGraph";
import LorebookKeywordFields from "../../components/LorebookKeywordFields.vue";
import RolePlacementFields from "../../components/RolePlacementFields.vue";
import LorebookFields from "../../components/LorebookFields.vue";
import ContentSidebar from "../../components/ContentSidebar.vue";
import { useWorkflowGraphStore } from "../../stores/workflowGraph";
import { promptFrontendExtensions } from "../promptFrontendManifest";
import { chatTavernFrontendExtensions, globalTavernFrontendExtensions, tavernFrontendExtensions } from "../tavernFrontendManifest";
import { workflowFrontendSdkKey, type WorkflowFrontendSdk, type WorkflowNodeConfigurationRequest } from "../workflowFrontendSdk";
import CurrentLorebookPanel from "./CurrentLorebookPanel.vue";
import GlobalLorebookFields from "./GlobalLorebookFields.vue";
import TavernChatPanel from "./TavernChatPanel.vue";
import * as tavernDemo from "./tavernDemo";
import LorebookEntries from "./LorebookEntries.vue";
import { createLorebookResources, lorebookResourceControllerKey } from "./lorebookResourceController";
import { globalTavernPackage, lorebookIdentity, lorebookIdentityKey, newLorebookResource } from "./lorebookResources";
import type { LorebookSaveRequest } from "./lorebookResourcesApi";

function fixture(initial = [newLorebookResource()], activate = false) {
  let raw: string | null = null;
  const state = { records: graphClone(initial), loseResponse: false };
  const save = vi.fn(async (request: LorebookSaveRequest) => {
    if (state.loseResponse) throw new WorkbenchApiError("unknown", "Response lost");
    state.records = [graphClone(request.record)];
  });
  const readReceipt = vi.fn(async (_request: LorebookSaveRequest) => ({}));
  const resources = createLorebookResources({
    list: async () => graphClone(state.records), save, readReceipt,
    readPending: () => raw, writePending: value => { raw = value; },
  });
  resources.records.value = graphClone(initial);
  const node = reactive<GraphNode>({
    node_binding_id: crypto.randomUUID(), component_id: activate ? "lorebook.global-activate" : "lorebook.global-reference",
    component_version: "1", title: "Lorebook", position: { x: 0, y: 0 },
    config: activate ? { object_keys: [] } : { reference: lorebookIdentity(initial[0] ?? newLorebookResource()) },
  });
  const document = reactive({ ...newGraph("Global lorebook"), nodes: [node], package_lock: [globalTavernPackage] });
  const locked = ref(false), lifecycle = ref("original");
  const configureNode = vi.fn((_request: WorkflowNodeConfigurationRequest) =>
    ({ workflowId: document.workflow_definition_id, nodeId: node.node_binding_id }));
  const sdk: WorkflowFrontendSdk = {
    workflowId: computed(() => document.workflow_definition_id), document: computed(() => document),
    catalog: computed(() => []), packages: computed(() => document.package_lock),
    session: computed(() => null), locked: computed(() => locked.value), lifecycle: computed(() => lifecycle.value),
    attachDisplay: () => false, readArtifact: vi.fn(), configureNode,
  };
  return { resources, state, save, readReceipt, node, document, locked, lifecycle, configureNode, sdk };
}

class HostNode extends EventTarget {
  parent: HostNode | null = null;
  children: HostNode[] = [];
  props: Record<string, unknown> = {};
  text = "";
  value: unknown = "";
  checked = false;
  selected = false;
  selectedIndex = -1;
  _value?: unknown;
  constructor(readonly tag = "") { super(); }
  getRootNode() { return { activeElement: null }; }
  get tagName() { return this.tag.toUpperCase(); }
  get type() { return String(this.props.type ?? ""); }
  get multiple() { return !!this.props.multiple; }
  get options(): HostNode[] { return this.children.flatMap(child => child.tag === "option" ? [child] : child.options); }
}
const renderer = createRenderer<HostNode, HostNode>({
  createElement: tag => new HostNode(tag), createText: text => Object.assign(new HostNode(), { text }),
  createComment: text => Object.assign(new HostNode(), { text }),
  insert(child, parent, anchor) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    if (index < 0) parent.children.push(child); else parent.children.splice(index, 0, child);
  },
  remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null; },
  setText: (node, text) => { node.text = text; },
  setElementText: (node, text) => { node.text = text; node.children = []; },
  patchProp(node, key, _previous, value) {
    node.props[key] = value;
    if (key === "value") { node.value = value; node._value = value; }
  },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
});
const apps: App<HostNode>[] = [];
async function flush() { await Promise.resolve(); await nextTick(); await Promise.resolve(); await nextTick(); }
function elements(root: HostNode): HostNode[] { return [root, ...root.children.flatMap(elements)]; }
function find(root: HostNode, predicate: (node: HostNode) => boolean) {
  const node = elements(root).find(predicate);
  expect(node, "rendered control").toBeDefined(); return node!;
}
const label = (root: HostNode, text: string) => find(root, node => node.props["aria-label"] === text);
const button = (root: HostNode, text: string) => find(root, node => node.tag === "button"
  && elements(node).map(child => child.text).join("").trim() === text);
async function trigger(node: HostNode, event: string, target = {}) {
  await (node.props[`on${event}`] as (event: unknown) => unknown)({ target, preventDefault() {} });
  await flush();
}
function model(node: HostNode, value: unknown) { (node.props["onUpdate:modelValue"] as (value: unknown) => void)(value); }
function installRender(component: object, filename: string) {
  const { descriptor } = parseSfc(readFileSync(new URL(filename, import.meta.url), "utf8"));
  const script = compileScript(descriptor, { id: "global-lorebook-ui" });
  const template = compileTemplate({ source: descriptor.template!.content, filename, id: "global-lorebook-ui",
    compilerOptions: { mode: "function", bindingMetadata: script.bindings } });
  expect(template.errors).toEqual([]);
  const code = transpileModule(template.code, {
    compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None },
  }).outputText;
  Object.assign(component, { render: new Function("Vue", code)(Vue) });
}
async function mount(component: Component, f: ReturnType<typeof fixture>) {
  vi.stubGlobal("document", { activeElement: null });
  vi.stubGlobal("Document", class {});
  vi.stubGlobal("ShadowRoot", class {});
  installRender(LorebookKeywordFields, "../../components/LorebookKeywordFields.vue");
  installRender(RolePlacementFields, "../../components/RolePlacementFields.vue");
  installRender(LorebookFields, "../../components/LorebookFields.vue");
  installRender(LorebookEntries, "LorebookEntries.vue");
  installRender(CurrentLorebookPanel, "CurrentLorebookPanel.vue");
  installRender(GlobalLorebookFields, "GlobalLorebookFields.vue");
  installRender(TavernChatPanel, "TavernChatPanel.vue");
  const root = new HostNode("root");
  const app = renderer.createApp(component, [CurrentLorebookPanel, TavernChatPanel].includes(component as typeof CurrentLorebookPanel)
    ? {} : { node: f.node, document: f.document })
    .provide(lorebookResourceControllerKey, f.resources).provide(workflowFrontendSdkKey, f.sdk)
    .provide(Vue.ssrContextKey, { modules: new Set() });
  apps.push(app); app.mount(root); await flush(); return root;
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.unstubAllGlobals(); });

describe("global lorebook workbench UI", () => {
  it("resolves only the exact new package, keeping legacy declarations separate", () => {
    const host = createWorkbenchFrontendHost(() => globalTavernFrontendExtensions, () => [globalTavernPackage]);
    expect(host.issues.value).toEqual([]);
    expect(host.extensions.value).toHaveLength(5);
    expect(host.extensions.value[3]!.configurationFields).toEqual(["reference"]);
    expect(host.extensions.value[4]!.configurationFields).toEqual(["object_keys"]);
    expect(createWorkbenchFrontendHost(() => globalTavernFrontendExtensions,
      () => [{ ...globalTavernPackage, version: "1.0.0" }]).extensions.value).toEqual([]);
    expect(createWorkbenchFrontendHost(() => tavernFrontendExtensions,
      () => [{ ...globalTavernPackage, version: "1.0.0" }]).extensions.value).toHaveLength(2);
  });
  it("renders both content panels only with the matching installed tavern version", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const graph = useWorkflowGraphStore(), f = fixture();
    graph.frontendExtensions = [...promptFrontendExtensions, ...globalTavernFrontendExtensions];
    graph.packageLock = [{ package_id: "workflow.prompts", version: "1.0.0" }, globalTavernPackage];
    try {
      const render = () => renderToString(createSSRApp(ContentSidebar).use(pinia)
        .provide(lorebookResourceControllerKey, f.resources));
      const html = await render();
      expect(html).toContain('aria-label="新版提示词当前资源"');
      expect(html).toContain('aria-label="全局 Lorebook 资源"');
      graph.frontendExtensions = [...promptFrontendExtensions, ...chatTavernFrontendExtensions];
      graph.packageLock = [{ package_id: "workflow.prompts", version: "1.0.0" },
        { package_id: "workflow.tavern", version: "1.2.0" }];
      const tavernHtml = await render();
      expect(tavernHtml).toContain('aria-label="全局 Lorebook 资源"');
      expect(tavernHtml).toContain('aria-label="酒馆聊天入口"');
      graph.frontendExtensions = [...promptFrontendExtensions, ...tavernFrontendExtensions];
      graph.packageLock = [{ package_id: "workflow.prompts", version: "1.0.0" },
        { package_id: "workflow.tavern", version: "1.0.0" }];
      expect(await render()).not.toContain('aria-label="全局 Lorebook 资源"');
    } finally { pinia._s.forEach(store => store.$dispose()); }
  });
  it("retains the inline node contract under the new exact frontend extension identity", async () => {
    const f = fixture();
    f.node.component_id = "lorebook.item";
    f.node.config = { entry: newLorebookEntry(), object_keys: [] };
    const root = await mount(LorebookFields, f);
    await trigger(label(root, "Lorebook 配置"), "Submit");
    expect(f.configureNode.mock.calls[0]![0]).toMatchObject({
      extensionId: "workflow.tavern.item-fields-v1-1",
      expectedConfig: f.node.config, patch: f.node.config,
    });
  });
  it("uses exact1.2 declarations for both inline and global configuration without upgrading old bindings", async () => {
    const inline = fixture();
    inline.document.package_lock = [{ package_id: "workflow.tavern", version: "1.2.0" }];
    inline.node.component_id = "lorebook.item";
    inline.node.config = { entry: newLorebookEntry(), object_keys: [] };
    const inlineRoot = await mount(LorebookFields, inline);
    await trigger(label(inlineRoot, "Lorebook 配置"), "Submit");
    expect(inline.configureNode.mock.calls[0]![0].extensionId).toBe("workflow.tavern.item-fields-v1-1-v1-2");
    const global = fixture();
    global.document.package_lock = [{ package_id: "workflow.tavern", version: "1.2.0" }];
    const globalRoot = await mount(GlobalLorebookFields, global);
    await trigger(label(globalRoot, "全局 Lorebook 节点配置"), "Submit");
    expect(global.configureNode.mock.calls[0]![0].extensionId).toBe("workflow.tavern.global-reference-fields-v1-2");
    global.document.package_lock = [globalTavernPackage]; await flush();
    await trigger(label(globalRoot, "全局 Lorebook 节点配置"), "Submit");
    expect(global.configureNode.mock.calls[1]![0].extensionId).toBe("workflow.tavern.global-reference-fields");
  });
  it("creates an independent tavern draft from the mounted panel and never publishes or replaces the original", async () => {
    const pinia = createPinia(); setActivePinia(pinia);
    const graph = useWorkflowGraphStore(), f = fixture(), fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    graph.setPersistenceGuard(() => true); graph.catalog = [{
      component_id: "tavern.chat.presentation", component_version: "1", display_name: "Display", category: "Tavern",
      config_schema: {}, default_config: {}, executable: true, is_output: true, inputs: [],
      outputs: [{ port_id: "display", data_type: "TAVERN_CHAT_DISPLAY", data_schema_version: 1, required: true, multiple: false }],
    }, { component_id: "tools.current-input", component_version: "1", display_name: "Input", category: "Tools",
      config_schema: {}, default_config: {}, executable: true, is_output: false, inputs: [], outputs: [],
      capabilities: ["external:read"],
    }];
    graph.packageLock = [{ package_id: "workflow.tavern", version: "1.2.0" }];
    const originalId = graph.createWorkflow(), before = graphClone(graph.entries[originalId]);
    const doc: GraphDocument = { ...newGraph("酒馆样例"), schema_version: 2, package_lock: graphClone(graph.packageLock),
      object_bindings: [], control_edges: [], execution_roots: [], nodes: [{
        node_binding_id: crypto.randomUUID(), component_id: "tools.current-input", component_version: "1",
        config: { input_name: "text" }, position: { x: 0, y: 0 }, title: "Input",
      }, { node_binding_id: crypto.randomUUID(), component_id: "tavern.chat.presentation", component_version: "1",
        config: {}, position: { x: 300, y: 0 }, title: "Display", public_outputs: ["display"] }] };
    const builder = vi.spyOn(tavernDemo, "createTavernDemo").mockReturnValue(doc);
    try {
      const root = await mount(TavernChatPanel, f);
      expect(label(root, "打开酒馆前端").props.href).toBeUndefined();
      await trigger(label(root, "创建酒馆示例"), "Click");
      expect(builder).toHaveBeenCalledOnce(); expect(graph.active!.document.workflow_definition_id).toBe(doc.workflow_definition_id);
      expect(graph.entries[originalId]).toEqual(before); expect(graph.active!.saved_revision).toBe(0);
      expect(label(root, "打开酒馆前端").props.href).toBeUndefined(); expect(fetcher).not.toHaveBeenCalled();
      graph.active!.saved_document = graphClone(doc); graph.active!.saved_revision = 1; await flush();
      expect(label(root, "打开酒馆前端").props.href).toContain("/tavern/?graph_workflow=");
      graph.active!.pending = { path: "/api/graph/definitions", action: "save", body: { idempotency_key: crypto.randomUUID() } };
      await flush();
      expect(label(root, "打开酒馆前端").props.href).toBeUndefined();
      await trigger(label(root, "创建酒馆示例"), "Click");
      expect(builder).toHaveBeenCalledOnce(); expect(fetcher).not.toHaveBeenCalled();
      expect(graph.entries[originalId]).toEqual(before);
    } finally { builder.mockRestore(); pinia._s.forEach(store => store.$dispose()); }
  });
  it("edits content, keywords, presentation and ordering without adding session authorization", async () => {
    const record = newLorebookResource(), first = newLorebookEntry(), second = newLorebookEntry();
    first.metadata = { keep: { flags: [true, null] } };
    record.value.entries = [first, second];
    record.update_sequence = 7;
    const original = graphClone(record), f = fixture([record]), root = await mount(CurrentLorebookPanel, f);
    await trigger(find(root, node => node.tag === "button" && node.children.some(child => child.tag === "strong")), "Click");
    model(label(root, "Lorebook 资源名称"), "Global setting");
    await trigger(label(root, "条目 1 正文"), "Input", { value: "Shared text" });
    await trigger(label(root, "条目 1 新增主关键词"), "Click");
    await trigger(label(root, "条目 1 主关键词 1"), "Input", { value: "{{place}}" });
    await trigger(label(root, "条目 1 递归触发"), "Change", { checked: true });
    await trigger(label(root, "条目 2 上移"), "Click");
    await trigger(label(root, "全局 Lorebook 表单"), "Submit");
    expect(f.save).toHaveBeenCalledTimes(1);
    const request = f.save.mock.calls[0]![0];
    expect(request.expected_sequence).toBe(7);
    expect(request.record.update_sequence).toBe(8);
    expect(request.record.value.entries.map(value => value.id)).toEqual([second.id, first.id]);
    expect(request.record.value.entries[1]).toMatchObject({
      text: "Shared text", primary_keywords: ["{{place}}"], recursive: true, metadata: first.metadata,
    });
    expect(Object.keys(request.record.value)).toEqual(["name", "enabled", "entries"]);
    expect(record).toEqual(original);
    expect(f.configureNode).not.toHaveBeenCalled();
  });
  it("supports empty resources and rejects invalid numeric drafts before dispatch", async () => {
    const f = fixture([]), root = await mount(CurrentLorebookPanel, f);
    await trigger(label(root, "新增 Lorebook 资源"), "Click");
    await trigger(button(root, "新增条目"), "Click");
    await trigger(label(root, "条目 1 扫描深度"), "Input", { valueAsNumber: 0.5 });
    await trigger(label(root, "全局 Lorebook 表单"), "Submit");
    expect(f.save).not.toHaveBeenCalled();
    await trigger(label(root, "条目 1 移除"), "Click");
    await trigger(label(root, "全局 Lorebook 表单"), "Submit");
    expect(f.save.mock.calls[0]![0].record.value.entries).toEqual([]);
  });
  it("retains missing full identity instead of selecting another scope, then applies an explicit reference", async () => {
    const workspace = newLorebookResource(), project = { ...graphClone(workspace), scope: "project:one" };
    const f = fixture([project]);
    f.node.config = { reference: lorebookIdentity(workspace) };
    const original = graphClone(f.node.config), root = await mount(GlobalLorebookFields, f);
    expect(elements(root).some(node => node.text.includes("缺失引用 · workspace"))).toBe(true);
    await trigger(label(root, "全局 Lorebook 节点配置"), "Submit");
    expect(f.configureNode).not.toHaveBeenCalled();
    model(label(root, "全局 Lorebook 选择"), lorebookIdentityKey(lorebookIdentity(project)));
    await trigger(label(root, "全局 Lorebook 节点配置"), "Submit");
    expect(f.configureNode).toHaveBeenCalledWith(expect.objectContaining({
      expectedConfig: original, patch: { reference: lorebookIdentity(project) },
      extensionId: "workflow.tavern.global-reference-fields", lifecycle: "original",
    }));
    expect(f.node.config).toEqual(original);
  });
  it("keeps variables local to the activation and refuses unauthorized binding or SDK-locked updates", async () => {
    const f = fixture([], true);
    f.node.config = { object_keys: ["secret"] };
    f.document.object_bindings = [{
      object_key: "variable/place", type_id: "workflow.variable", schema_version: 1, scope: "shared",
      readers: [f.node.node_binding_id], writers: [], owner_node_id: null,
    }];
    const root = await mount(GlobalLorebookFields, f);
    await trigger(label(root, "全局 Lorebook 节点配置"), "Submit");
    expect(f.configureNode).not.toHaveBeenCalled();
    await trigger(label(root, "移除无效变量对象 secret"), "Change", { checked: false });
    await trigger(label(root, "读取变量对象 variable/place"), "Change", { checked: true });
    await trigger(label(root, "全局 Lorebook 节点配置"), "Submit");
    expect(f.configureNode.mock.calls[0]![0].patch).toEqual({ object_keys: ["variable/place"] });
    f.locked.value = true; await flush();
    await trigger(label(root, "全局 Lorebook 节点配置"), "Submit");
    expect(f.configureNode).toHaveBeenCalledTimes(1);
  });
  it("locks unknown resource mutations and reconciles without a second save or stale form resubmission", async () => {
    const f = fixture([]), root = await mount(CurrentLorebookPanel, f);
    await trigger(label(root, "新增 Lorebook 资源"), "Click");
    f.state.loseResponse = true;
    await trigger(label(root, "全局 Lorebook 表单"), "Submit");
    expect(label(root, "新增 Lorebook 资源").props.disabled).toBe(true);
    const request = graphClone(f.save.mock.calls[0]![0]);
    await trigger(button(root, "核实原 Lorebook 请求"), "Click");
    expect(f.readReceipt).toHaveBeenCalledWith(request);
    expect(f.save).toHaveBeenCalledTimes(1);
    expect(elements(root).some(node => node.props["aria-label"] === "全局 Lorebook 表单")).toBe(false);
  });
  it("preserves invalid source config and updates the SDK lifecycle basis on external changes", async () => {
    const f = fixture();
    f.node.config = { reference: lorebookIdentity(newLorebookResource()), extra: true };
    const html = await renderToString(createSSRApp(GlobalLorebookFields, { node: f.node, document: f.document })
      .provide(lorebookResourceControllerKey, f.resources).provide(workflowFrontendSdkKey, f.sdk));
    expect(html).toContain("Lorebook 引用配置无效");
    const root = await mount(GlobalLorebookFields, f);
    await trigger(label(root, "全局 Lorebook 节点配置"), "Submit");
    expect(f.configureNode).not.toHaveBeenCalled();
    f.node.config = { reference: lorebookIdentity(f.state.records[0]!) };
    f.lifecycle.value = "replacement"; await flush();
    await trigger(label(root, "全局 Lorebook 节点配置"), "Submit");
    expect(f.configureNode.mock.calls[0]![0]).toMatchObject({ expectedConfig: f.node.config, lifecycle: "replacement" });
  });
});
