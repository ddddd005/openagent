import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse as parseSfc } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { computed, createRenderer, createSSRApp, nextTick, reactive, ref, type App, type Component } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkbenchApiError } from "../../adapters/workbenchApi";
import { localDraftPorts } from "../../adapters/localDraftsApi";
import { memoryLocalDraftStorage } from "../../testUtils/localDraftStorage";
import { createWorkbenchFrontendHost } from "../../application/workflowFrontendPackage";
import { newLorebookEntry } from "../../domain/lorebook";
import { graphClone, newGraph, type GraphNode } from "../../domain/workflowGraph";
import LorebookKeywordFields from "../../components/LorebookKeywordFields.vue";
import RolePlacementFields from "../../components/RolePlacementFields.vue";
import RawNumberInput from "../../components/RawNumberInput.vue";
import LorebookFields from "../../components/LorebookFields.vue";
import ContentSidebar from "../../components/ContentSidebar.vue";
import { useWorkflowGraphStore } from "../../stores/workflowGraph";
import { promptFrontendExtensions } from "../promptFrontendManifest";
import { currentTavernFrontendExtensions, globalTavernFrontendExtensions, tavernFrontendExtensions } from "../tavernFrontendManifest";
import { workflowFrontendSdkKey, type WorkflowFrontendSdk, type WorkflowNodeConfigurationRequest } from "../workflowFrontendSdk";
import CurrentLorebookPanel from "./CurrentLorebookPanel.vue";
import GlobalLorebookFields from "./GlobalLorebookFields.vue";
import LorebookEntries from "./LorebookEntries.vue";
import { createLorebookResources, lorebookResourceControllerKey } from "./lorebookResourceController";
import { globalTavernPackage, lorebookIdentity, lorebookIdentityKey, newLorebookResource } from "./lorebookResources";
import type { LorebookSaveRequest } from "./lorebookResourcesApi";

function fixture(initial = [newLorebookResource()], activate = false) {
  let raw: string | null = null;
  const state = { records: graphClone(initial), loseResponse: false, saveFailure: null as WorkbenchApiError | null };
  const save = vi.fn(async (request: LorebookSaveRequest) => {
    if (state.loseResponse) throw new WorkbenchApiError("unknown", "Response lost");
    if (state.saveFailure) throw state.saveFailure;
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
  const configureNode = vi.fn(async (_request: WorkflowNodeConfigurationRequest) =>
    ({ workflowId: document.workflow_definition_id, nodeId: node.node_binding_id, outcome: "saved" as const }));
  const sdk: WorkflowFrontendSdk = {
    workflowId: computed(() => document.workflow_definition_id), document: computed(() => document),
    catalog: computed(() => []), packages: computed(() => document.package_lock),
    session: computed(() => null), locked: computed(() => locked.value), lifecycle: computed(() => lifecycle.value),
    configureNode,
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
const piniaInstances: Pinia[] = [];
async function flush() { for (let index = 0; index < 12; index++) { await Promise.resolve(); await nextTick(); } }
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
async function mount(component: Component, f: ReturnType<typeof fixture>,
  drafts?: ReturnType<typeof memoryLocalDraftStorage>) {
  if (drafts) {
    vi.useFakeTimers();
    vi.stubGlobal("window", { addEventListener() {}, removeEventListener() {} });
    vi.spyOn(localDraftPorts, "read").mockImplementation(drafts.ports.read);
    vi.spyOn(localDraftPorts, "save").mockImplementation(drafts.ports.save);
    vi.spyOn(localDraftPorts, "receipt").mockImplementation(drafts.ports.receipt);
  }
  vi.stubGlobal("document", { activeElement: null });
  vi.stubGlobal("Document", class {});
  vi.stubGlobal("ShadowRoot", class {});
  installRender(LorebookKeywordFields, "../../components/LorebookKeywordFields.vue");
  installRender(RolePlacementFields, "../../components/RolePlacementFields.vue");
  installRender(RawNumberInput, "../../components/RawNumberInput.vue");
  installRender(LorebookFields, "../../components/LorebookFields.vue");
  installRender(LorebookEntries, "LorebookEntries.vue");
  installRender(CurrentLorebookPanel, "CurrentLorebookPanel.vue");
  installRender(GlobalLorebookFields, "GlobalLorebookFields.vue");
  const root = new HostNode("root");
  const app = renderer.createApp(component, component === CurrentLorebookPanel
    ? {} : { node: f.node, document: f.document })
    .provide(lorebookResourceControllerKey, f.resources).provide(workflowFrontendSdkKey, f.sdk)
    .provide(Vue.ssrContextKey, { modules: new Set() });
  if (drafts) {
    const pinia = createPinia(); piniaInstances.push(pinia); setActivePinia(pinia); app.use(pinia);
  }
  apps.push(app); app.mount(root); await flush(); return root;
}
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount()); piniaInstances.splice(0).forEach(disposePinia);
  vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe("global lorebook workbench UI", () => {
  it("keeps named entries independently collapsible through edits and reordering without losing partial input", async () => {
    const record = newLorebookResource(), first = newLorebookEntry(), second = newLorebookEntry();
    first.name = "First setting"; second.name = " ";
    record.value.entries = [first, second];
    const original = graphClone(record), f = fixture([record]), root = await mount(CurrentLorebookPanel, f);
    await trigger(find(root, node => node.props.class === "resource-edit"), "Click");
    const section = label(root, "条目 1 收起").parent!.parent!;
    const body = find(section, node => node.props.class === "entry-body");
    const scan = label(root, "条目 1 扫描深度");
    expect(body.props.hidden).toBe(false);
    expect(elements(label(root, "条目 2 收起")).some(node => node.text === "条目 2")).toBe(true);
    await trigger(label(root, "条目 1 名称"), "Input", { value: "Renamed setting" });
    await trigger(label(root, "条目 1 正文"), "Input", { value: "Unsaved content" });
    await trigger(scan, "Input", { value: "-" });
    await trigger(label(root, "条目 1 收起"), "Click");
    const collapsed = label(root, "条目 1 展开");
    expect(collapsed.props["aria-expanded"]).toBe(false);
    expect(collapsed.props["aria-controls"]).toBe(body.props.id);
    expect(elements(collapsed).some(node => node.text === "Renamed setting")).toBe(true);
    expect(body.props.hidden).toBe(true);
    expect(label(root, "条目 2 收起").props["aria-expanded"]).toBe(true);
    await trigger(label(root, "条目 2 上移"), "Click");
    expect(label(root, "条目 2 展开").parent!.parent).toBe(section);
    await trigger(label(root, "全局 Lorebook 表单"), "Submit");
    expect(f.save).not.toHaveBeenCalled();
    await trigger(label(root, "条目 2 展开"), "Click");
    expect(body.props.hidden).toBe(false);
    expect(label(root, "条目 2 扫描深度")).toBe(scan);
    expect(scan.value).toBe("-");
    expect(label(root, "条目 2 正文").value).toBe("Unsaved content");
    await trigger(scan, "Input", { value: "9" });
    await trigger(label(root, "条目 2 收起"), "Click");
    await trigger(label(root, "全局 Lorebook 表单"), "Submit");
    expect(f.save).toHaveBeenCalledOnce();
    const saved = f.save.mock.calls[0]![0].record.value.entries;
    expect(saved.map(entry => entry.id)).toEqual([second.id, first.id]);
    expect(saved[1]).toEqual({ ...first, name: "Renamed setting", text: "Unsaved content", scan_depth: 9,
      presentation: { ...first.presentation, order: 1 } });
    expect(record).toEqual(original);
  });

  it("copies a listed resource and submits it as new without touching the source or node binding", async () => {
    const record = newLorebookResource(), entry = newLorebookEntry();
    record.scope = "project:one"; record.update_sequence = 7;
    record.value.name = "Shared setting"; record.value.entries = [entry];
    entry.text = "Source lore"; entry.primary_keywords = ["world"];
    const original = graphClone(record), f = fixture([record]), root = await mount(CurrentLorebookPanel, f);
    await trigger(label(root, `复制 Lorebook ${record.scope} ${record.resource_id}`), "Click");
    expect(f.save).not.toHaveBeenCalled();
    expect(label(root, "Lorebook 资源名称").value).toBe("Shared setting");
    await trigger(label(root, "条目 1 正文"), "Input", { value: "Copy lore" });
    await trigger(label(root, "全局 Lorebook 表单"), "Submit");
    const request = f.save.mock.calls[0]![0];
    expect(request.expected_sequence).toBe(0);
    expect(request.record.update_sequence).toBe(1);
    expect(request.record.resource_id).not.toBe(record.resource_id);
    expect(request.record.scope).toBe(record.scope);
    expect(request.record.value.name).toBe(record.value.name);
    expect(request.record.value.entries[0]!.id).not.toBe(entry.id);
    expect(request.record.value.entries[0]!.text).toBe("Copy lore");
    expect(record).toEqual(original);
    expect(f.resources.records.value).toEqual(f.state.records);
    expect(f.configureNode).not.toHaveBeenCalled();
    expect(elements(root).some(node => node.props["aria-label"] === "全局 Lorebook 表单")).toBe(false);
  });
  it("disables copying while an unknown resource request still owns the mutation lock", async () => {
    const record = newLorebookResource(), f = fixture([record]), root = await mount(CurrentLorebookPanel, f);
    await trigger(label(root, "新增 Lorebook 资源"), "Click");
    f.state.loseResponse = true;
    await trigger(label(root, "全局 Lorebook 表单"), "Submit");
    const copy = label(root, `复制 Lorebook ${record.scope} ${record.resource_id}`);
    expect(copy.props.disabled).toBe(true);
    const original = graphClone(f.resources.pending.value!);
    await trigger(copy, "Click");
    expect(f.resources.pending.value).toEqual(original);
    expect(f.save).toHaveBeenCalledOnce();
    expect(label(root, "Lorebook 资源名称").value).toBe("");
  });
  it("saves memory numeric edits without consulting any persistent draft gate", async () => {
    const record = newLorebookResource(), entry = newLorebookEntry(); record.value.entries = [entry];
    const f = fixture([record]), drafts = memoryLocalDraftStorage(), root = await mount(CurrentLorebookPanel, f, drafts);
    await trigger(find(root, node => node.tag === "button" && node.children.some(child => child.tag === "strong")), "Click");
    await trigger(label(root, "条目 1 扫描深度"), "Input", { value: "009" });
    await trigger(label(root, "全局 Lorebook 表单"), "Submit");
    expect(f.save).toHaveBeenCalledOnce(); expect(f.save.mock.calls[0]![0].record.value.entries[0].scan_depth).toBe(9);
    expect(drafts.ports.read).not.toHaveBeenCalled(); expect(drafts.ports.save).not.toHaveBeenCalled();
  });
  it("retains ordinary text and numeric input after body CAS rejection without saving a draft", async () => {
    const record = newLorebookResource(), entry = newLorebookEntry(); record.value.entries = [entry];
    const f = fixture([record]), drafts = memoryLocalDraftStorage(), root = await mount(CurrentLorebookPanel, f, drafts);
    await trigger(find(root, node => node.tag === "button" && node.children.some(child => child.tag === "strong")), "Click");
    await trigger(label(root, "条目 1 扫描深度"), "Input", { value: "009" });
    await trigger(label(root, "条目 1 正文"), "Input", { value: "Memory lore remains" });
    f.state.saveFailure = new WorkbenchApiError("rejected", "body CAS changed", 409, "stale_revision");
    await trigger(label(root, "全局 Lorebook 表单"), "Submit");
    expect(f.save).toHaveBeenCalledOnce(); expect(label(root, "条目 1 扫描深度").value).toBe("009");
    expect(label(root, "条目 1 正文").value).toBe("Memory lore remains");
    expect(f.resources.locked.value).toBe(false);
    expect(elements(root).some(node => node.text.includes("body CAS changed"))).toBe(true);
    expect(drafts.ports.read).not.toHaveBeenCalled(); expect(drafts.ports.save).not.toHaveBeenCalled();
    f.state.saveFailure = null;
    await trigger(label(root, "全局 Lorebook 表单"), "Submit");
    expect(f.save).toHaveBeenCalledTimes(2);
  });
  it("discards unsaved memory edits on resource switch and cancel without leaking invalid numeric text", async () => {
    const first = newLorebookResource(); first.value.name = "First"; first.value.entries = [newLorebookEntry()];
    const second = graphClone(first); second.scope = "project:story"; second.value.name = "Second";
    second.value.entries[0]!.scan_depth = 9;
    const f = fixture([first, second]), drafts = memoryLocalDraftStorage(), root = await mount(CurrentLorebookPanel, f, drafts);
    await trigger(elements(root).filter(node => node.props.class === "resource-edit")[0]!, "Click");
    model(label(root, "Lorebook 资源名称"), "Unsaved name");
    await trigger(label(root, "条目 1 扫描深度"), "Input", { value: "-" });
    await trigger(elements(root).filter(node => node.props.class === "resource-edit")[1]!, "Click");
    expect(label(root, "Lorebook 资源名称").value).toBe("Second");
    expect(label(root, "条目 1 扫描深度").value).toBe("9");
    expect(label(root, "条目 1 扫描深度").props["aria-invalid"]).toBeUndefined();
    await trigger(elements(root).filter(node => node.props.class === "resource-edit")[0]!, "Click");
    expect(label(root, "Lorebook 资源名称").value).toBe("First");
    await trigger(label(root, "取消 Lorebook 编辑"), "Click");
    expect(elements(root).some(node => node.props["aria-label"] === "全局 Lorebook 表单")).toBe(false);
    expect(elements(root).some(node => node.props["aria-label"] === "未提交世界书草稿")).toBe(false);
    expect(f.save).not.toHaveBeenCalled(); expect(f.state.records).toEqual([first, second]);
    expect(drafts.ports.read).not.toHaveBeenCalled(); expect(drafts.ports.save).not.toHaveBeenCalled();
  });
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
  it("keeps prompt and world-book content independent of the active graph without a Tavern builder", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const graph = useWorkflowGraphStore(), f = fixture();
    graph.frontendExtensions = [...promptFrontendExtensions, ...globalTavernFrontendExtensions];
    graph.packageLock = [{ package_id: "workflow.prompts", version: "1.0.0" }, globalTavernPackage];
    try {
      const render = () => renderToString(createSSRApp(ContentSidebar).use(pinia)
        .provide(lorebookResourceControllerKey, f.resources));
      graph.frontendExtensions = []; graph.packageLock = [];
      graph.catalogError = "Controlled graph catalog failure";
      const html = await render();
      expect(html).toContain('aria-label="新版提示词当前资源"');
      expect(html).toContain('aria-label="全局 Lorebook 资源"');
      expect(html).not.toContain("Controlled graph catalog failure");
      expect(html).not.toContain('aria-label="酒馆聊天入口"');
      graph.frontendExtensions = [...promptFrontendExtensions, ...currentTavernFrontendExtensions];
      graph.packageLock = [{ package_id: "workflow.prompts", version: "1.0.0" },
        { package_id: "workflow.tavern", version: "1.2.0" }];
      const tavernHtml = await render();
      expect(tavernHtml).toContain('aria-label="全局 Lorebook 资源"');
      expect(tavernHtml).toContain('aria-label="新版提示词当前资源"');
      expect(tavernHtml).not.toContain('aria-label="酒馆聊天入口"');
      expect(tavernHtml).not.toContain("创建酒馆工作流");
      graph.frontendExtensions = [...promptFrontendExtensions, ...tavernFrontendExtensions];
      graph.packageLock = [{ package_id: "workflow.prompts", version: "1.0.0" },
        { package_id: "workflow.tavern", version: "1.0.0" }];
      const oldGraphHtml = await render();
      expect(oldGraphHtml).toContain('aria-label="全局 Lorebook 资源"');
      expect(oldGraphHtml).not.toContain('aria-label="酒馆聊天入口"');
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
    const host = createWorkbenchFrontendHost(() => currentTavernFrontendExtensions,
      () => [{ package_id: "workflow.tavern", version: "1.2.0" }]);
    expect(host.issues.value).toEqual([]);
    expect(host.extensions.value).toHaveLength(5);
    expect(host.extensions.value.map(row => row.declaration.extension_id)).toEqual([
      "workflow.tavern.item-fields-v1-1-v1-2", "workflow.tavern.group-fields-v1-1-v1-2",
      "workflow.tavern.lorebook-resources-v1-2", "workflow.tavern.global-reference-fields-v1-2",
      "workflow.tavern.global-activate-fields-v1-2",
    ]);
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
    await trigger(label(root, "条目 1 扫描深度"), "Input", { value: "0.5" });
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
