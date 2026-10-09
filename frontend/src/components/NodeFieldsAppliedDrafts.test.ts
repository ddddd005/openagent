import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { createRenderer, h, nextTick, type App } from "vue";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";
import ModelSourceFields from "./ModelSourceFields.vue";
import LorebookFields from "./LorebookFields.vue";
import LorebookKeywordFields from "./LorebookKeywordFields.vue";
import RolePlacementFields from "./RolePlacementFields.vue";
import RawNumberInput from "./RawNumberInput.vue";
import FormDraftNotice from "./FormDraftNotice.vue";
import { graphClone, type GraphNode, type GraphNodeType } from "../domain/workflowGraph";
import { newProvider, providerIdentity } from "../domain/workflowModelResources";
import { lorebookPackage, newLorebookEntry } from "../domain/lorebook";
import { modelFrontendExtensions } from "../plugins/modelFrontendManifest";
import { tavernFrontendExtensions } from "../plugins/tavernFrontendManifest";
import { createProviderResources, modelResourceControllerKey } from "../application/workflowResources";
import { useWorkbenchFrontendHost } from "../composables/useWorkbenchFrontendHost";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { useFormDraftsStore } from "../stores/formDrafts";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { localDraftPorts } from "../adapters/localDraftsApi";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";

class HostNode extends EventTarget {
  parent: HostNode | null = null;
  children: HostNode[] = [];
  props: Record<string, unknown> = {};
  text = "";
  value: unknown = "";
  selected = false;
  checked = false;
  selectedIndex = -1;
  _value?: unknown;
  constructor(readonly tag = "") { super(); }
  getRootNode() { return { activeElement: null }; }
  get tagName() { return this.tag.toUpperCase(); }
  get type() { return String(this.props.type ?? ""); }
  get multiple() { return !!this.props.multiple; }
  get options(): HostNode[] { return this.children.flatMap(child => child.tag === "option" ? [child] : child.options); }
  setCustomValidity() {}
}
const renderer = createRenderer<HostNode, HostNode>({
  createElement: tag => new HostNode(tag), createText: text => Object.assign(new HostNode(), { text }),
  createComment: text => Object.assign(new HostNode(), { text }),
  insert(child, parent, anchor) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = parent;
    const at = anchor ? parent.children.indexOf(anchor) : -1;
    if (at < 0) parent.children.push(child); else parent.children.splice(at, 0, child);
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
const apps: App<HostNode>[] = [], piniaInstances: Pinia[] = [];
async function flush() { for (let index = 0; index < 12; index++) { await Promise.resolve(); await nextTick(); } }
function elements(root: HostNode): HostNode[] { return [root, ...root.children.flatMap(elements)]; }
function find(root: HostNode, predicate: (node: HostNode) => boolean) {
  const found = elements(root).find(predicate);
  expect(found, "rendered control").toBeDefined(); return found!;
}
const label = (root: HostNode, text: string) => find(root, node => node.props["aria-label"] === text);
async function trigger(node: HostNode, event: string, target = {}) {
  await (node.props[`on${event}`] as (value: unknown) => unknown)({ target, preventDefault() {} });
  await flush();
}
function installRender(component: object, filename: string) {
  const { descriptor } = parse(readFileSync(new URL(filename, import.meta.url), "utf8"));
  const script = compileScript(descriptor, { id: "applied-node-drafts" });
  const template = compileTemplate({ source: descriptor.template!.content, filename, id: "applied-node-drafts",
    compilerOptions: { mode: "function", bindingMetadata: script.bindings } });
  const code = transpileModule(template.code, {
    compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None },
  }).outputText;
  Object.assign(component, { render: new Function("Vue", code)(Vue) });
}
async function mount(kind: "model" | "lorebook", saved = false) {
  vi.stubGlobal("window", { addEventListener() {}, removeEventListener() {} });
  vi.stubGlobal("document", { activeElement: null });
  vi.stubGlobal("Document", class {});
  vi.stubGlobal("ShadowRoot", class {});
  const pinia = createPinia(); piniaInstances.push(pinia); setActivePinia(pinia);
  const memory = memoryLocalDraftStorage();
  vi.spyOn(localDraftPorts, "read").mockImplementation(memory.ports.read);
  vi.spyOn(localDraftPorts, "save").mockImplementation(memory.ports.save);
  vi.spyOn(localDraftPorts, "receipt").mockImplementation(memory.ports.receipt);
  const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore(), forms = useFormDraftsStore();
  graph.setPersistenceGuard(() => true);
  const provider = newProvider();
  const entry = newLorebookEntry();
  entry.presentation = { ...entry.presentation, placement: "middle", depth: 0 };
  entry.probability_enabled = true;
  const node: GraphNode = { node_binding_id: crypto.randomUUID(), component_id: kind === "model" ? "models.source" : "lorebook.item",
    component_version: kind === "model" ? "2" : "1", title: kind, position: { x: 0, y: 0 },
    config: kind === "model" ? { reference: providerIdentity(provider),
      parameters: { model: "chosen-model", thinking: "disabled", stream: false, max_tokens: 1024 },
      capacity: { context_window_tokens: 128000, output_reserve_tokens: 1024,
        summary_max_tokens: 128, max_cold_input_tokens: 128000 } } : { entry, object_keys: [] } };
  const definition: GraphNodeType = { component_id: node.component_id, component_version: node.component_version,
    display_name: kind, category: kind, executable: true, is_output: false, input_storage: "references",
    default_config: graphClone(node.config), inputs: [], outputs: [],
    config_schema: { type: "object", properties: kind === "model"
      ? { reference: {}, parameters: {}, capacity: {} } : { entry: {}, object_keys: {} } } };
  const lock = kind === "model" ? [{ package_id: "workflow.models", version: "1.0.0" }] : [lorebookPackage];
  graph.catalog = [definition]; graph.packageLock = lock; graph.executionPackageLock = lock;
  graph.frontendExtensions = graphClone(kind === "model" ? modelFrontendExtensions : tavernFrontendExtensions);
  const id = graph.createWorkflow();
  graph.entries[id]!.document = { ...graph.entries[id]!.document, schema_version: 2, nodes: [node],
    object_bindings: [], control_edges: [], execution_roots: [], package_lock: graphClone(lock) };
  if (saved) workspace.markWorkflowSaved(id);
  const originalConfig = graphClone(node.config);
  const resources = createProviderResources({ list: async () => [provider], save: async () => ({}),
    readReceipt: vi.fn(), readPending: () => null, writePending: () => {} });
  resources.records.value = [provider];
  vi.spyOn(useWorkbenchPersistenceStore(), "flushAll").mockImplementation(() => forms.flushAll());
  installRender(RawNumberInput, "RawNumberInput.vue");
  installRender(FormDraftNotice, "FormDraftNotice.vue");
  installRender(RolePlacementFields, "RolePlacementFields.vue");
  installRender(LorebookKeywordFields, "LorebookKeywordFields.vue");
  installRender(LorebookFields, "LorebookFields.vue");
  installRender(ModelSourceFields, "ModelSourceFields.vue");
  const component = kind === "model" ? ModelSourceFields : LorebookFields;
  const parent = { setup() {
    useWorkbenchFrontendHost();
    return () => h(component, { node: graph.document!.nodes[0]!, document: graph.document! });
  } };
  const root = new HostNode("root");
  const app = renderer.createApp(parent).use(pinia).provide(modelResourceControllerKey, resources)
    .provide(Vue.ssrContextKey, { modules: new Set() });
  apps.push(app); app.mount(root); await flush();
  return { root, graph, forms, memory, id, nodeId: node.node_binding_id, originalConfig, kind, workspace };
}
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount());
  piniaInstances.splice(0).forEach(disposePinia);
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});
async function edit(f: Awaited<ReturnType<typeof mount>>) {
  if (f.kind === "model") {
    await trigger(label(f.root, "最大输出 tokens"), "Input", { value: "02048" });
    await trigger(label(f.root, "输出预留 tokens"), "Input", { value: "2048" });
  } else {
    await trigger(label(f.root, "条目 1 扫描深度"), "Input", { value: "09" });
    await trigger(label(f.root, "条目 1 概率值"), "Input", { value: "42.5" });
    await trigger(label(f.root, "条目 1 插入深度"), "Input", { value: "03" });
    await trigger(label(f.root, "条目 1 装配顺序"), "Input", { value: "-2" });
  }
}
const formLabel = (kind: "model" | "lorebook") => kind === "model" ? "模型源配置" : "Lorebook 配置";

describe("node form drafts after actual SDK configuration", () => {
  it.each(["model", "lorebook"] as const)("adopts exactly the applied %s basis after the store replaces the document", async kind => {
    const f = await mount(kind), originalDocument = f.graph.document;
    await edit(f);
    await trigger(label(f.root, formLabel(kind)), "Submit");
    expect(f.graph.document).not.toBe(originalDocument);
    expect(f.graph.document!.workflow_definition_id).toBe(f.id);
    expect(f.graph.document!.nodes[0]!.config).not.toEqual(f.originalConfig);
    expect(await f.forms.flushAll()).toBe(true);
    const drafts = [...f.memory.records.values()].filter(record => record.address.key.startsWith(`node:${f.id}:${f.nodeId}:`));
    expect(drafts.length).toBe(kind === "model" ? 1 : 5);
    for (const record of drafts) {
      expect(record.value).toMatchObject({ basis: { workflowId: f.id, nodeId: f.nodeId,
        config: f.graph.document!.nodes[0]!.config } });
      expect(f.forms.statuses[record.address.key]).toBe("saved");
      expect(f.forms.errors[record.address.key]).toBe("");
    }
    expect(elements(f.root).some(node => node.text.includes("旧表单草稿保留"))).toBe(false);
    expect(label(f.root, kind === "model" ? "最大输出 tokens" : "条目 1 扫描深度").props.value)
      .toBe(kind === "model" ? "02048" : "09");
    const originalRecords = graphClone(drafts);
    const externalConfig = kind === "model" ? { ...f.graph.document!.nodes[0]!.config,
      parameters: { ...f.graph.document!.nodes[0]!.config.parameters as object, model: "external-model" } }
      : { ...f.graph.document!.nodes[0]!.config,
        entry: { ...f.graph.document!.nodes[0]!.config.entry as object, text: "external-text" } };
    f.graph.patchNode(f.nodeId, { config: externalConfig }); await flush();
    expect(elements(f.root).some(node => node.text.includes("旧表单草稿保留"))).toBe(true);
    expect([...f.memory.records.values()].filter(record => record.address.key.startsWith(`node:${f.id}:${f.nodeId}:`)))
      .toEqual(originalRecords);
  });
  it.each(["model", "lorebook"] as const)("does not rebase old %s draft identities when the SDK creates a copy", async kind => {
    const f = await mount(kind, true);
    await edit(f);
    await trigger(label(f.root, formLabel(kind)), "Submit");
    expect(f.workspace.activeWorkflowId).not.toBe(f.id);
    expect(f.graph.entries[f.id]!.document.nodes[0]!.config).toEqual(f.originalConfig);
    const drafts = [...f.memory.records.values()].filter(record => record.address.key.startsWith(`node:${f.id}:${f.nodeId}:`));
    expect(drafts.length).toBe(kind === "model" ? 1 : 5);
    for (const record of drafts) expect(record.value).toMatchObject({ basis: { workflowId: f.id, config: f.originalConfig } });
  });
});
