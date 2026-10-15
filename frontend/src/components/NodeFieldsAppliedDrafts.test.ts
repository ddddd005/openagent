import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { computed, createRenderer, h, nextTick, shallowRef, type App } from "vue";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";
import ModelSourceFields from "./ModelSourceFields.vue";
import LorebookFields from "./LorebookFields.vue";
import LorebookKeywordFields from "./LorebookKeywordFields.vue";
import RolePlacementFields from "./RolePlacementFields.vue";
import RawNumberInput from "./RawNumberInput.vue";
import GraphNodeConfiguration from "./GraphNodeConfiguration.vue";
import GraphSchemaFields from "./GraphSchemaFields.vue";
import { graphClone, type GraphDocument, type GraphNode, type GraphNodeType } from "../domain/workflowGraph";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";
import { createProviderResources, modelResourceControllerKey } from "../application/workflowResources";
import { createModelProfiles, modelProfilesKey } from "../application/modelProfiles";
import { modelFrontendExtensions } from "../plugins/modelFrontendManifest";
import { tavernFrontendExtensions } from "../plugins/tavernFrontendManifest";
import { workflowFrontendSdkKey, type WorkflowFrontendSdk } from "../plugins/workflowFrontendSdk";
import { newProvider, newModelProfile, modelProfileIdentity } from "../domain/workflowModelResources";
import { lorebookPackage, newLorebookEntry } from "../domain/lorebook";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { editableInputType } from "../testUtils/editableInputFixture";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import type { GraphConfigurationResult, GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";

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
  const script = compileScript(descriptor, { id: "node-fields-configuration" });
  const template = compileTemplate({ source: descriptor.template!.content, filename, id: "node-fields-configuration",
    compilerOptions: { mode: "function", bindingMetadata: script.bindings } });
  const code = transpileModule(template.code, {
    compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None },
  }).outputText;
  Object.assign(component, { render: new Function("Vue", code)(Vue) });
}
function savedResult(request: GraphConfigurationUpdateRequest): GraphConfigurationResult {
  const revision = request.document.revision + 1, editSequence = request.expected_edit_sequence + 1;
  const document = { ...graphClone(request.document), revision, edit_sequence: editSequence };
  return { schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
    source_workflow_definition_id: request.workflow_definition_id,
    workflow_definition_id: request.workflow_definition_id, definition_revision: revision,
    edit_sequence: editSequence, source_has_effective_session: false, changed_categories: ["fixed"],
    node_id_map: Object.fromEntries(document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null, document };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
async function mount(kind: "model" | "lorebook") {
  vi.stubGlobal("window", { addEventListener() {}, removeEventListener() {}, confirm: () => false });
  vi.stubGlobal("document", { activeElement: null });
  vi.stubGlobal("Document", class {});
  vi.stubGlobal("ShadowRoot", class {});
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const pinia = createPinia(); piniaInstances.push(pinia); setActivePinia(pinia);
  const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
  const provider = newProvider();
  const profile = newModelProfile(provider), alternative = newModelProfile(provider);
  for (const [index, record] of [profile, alternative].entries()) {
    record.value.name = `Configuration ${index + 1}`;
    record.value.model.id = `chosen-model-${index + 1}`;
    record.value.model.capacity = { context_window_tokens: 128000, input_limit_tokens: 120000, max_output_tokens: 8192 };
    record.value.runtime.context_length_tokens = 128000;
    record.value.runtime.output_limit_tokens = 4096;
  }
  const entry = newLorebookEntry();
  entry.presentation = { ...entry.presentation, placement: "middle", depth: 0 };
  entry.probability_enabled = true;
  const makeNode = (): GraphNode => ({
    node_binding_id: crypto.randomUUID(), component_id: kind === "model" ? "models.source" : "lorebook.item",
    component_version: kind === "model" ? "7" : "1", title: kind, position: { x: 0, y: 0 },
    config: kind === "model" ? { reference: modelProfileIdentity(profile) } : { entry: graphClone(entry), object_keys: [] },
    configuration_policy: { schema_version: 1, prompt_mode: "fixed" },
  });
  const node = makeNode(), otherNode = { ...graphClone(node), node_binding_id: crypto.randomUUID() };
  const definition: GraphNodeType = { component_id: node.component_id, component_version: node.component_version,
    display_name: kind, category: kind, executable: true, is_output: false, input_storage: "references",
    default_config: graphClone(node.config), inputs: [], outputs: [],
    config_schema: { type: "object", properties: kind === "model"
      ? { reference: {} } : { entry: {}, object_keys: {} } } };
  const lock = kind === "model" ? [{ package_id: "workflow.models", version: "1.0.0" }] : [lorebookPackage];
  graph.catalog = [definition]; graph.packageLock = lock; graph.executionPackageLock = lock;
  graph.frontendExtensions = graphClone(kind === "model" ? modelFrontendExtensions : tavernFrontendExtensions);
  const workflowId = graph.createWorkflow();
  const document = { schema_version: 3 as const, workflow_definition_id: workflowId, revision: 1, edit_sequence: 1,
    name: kind, nodes: [node, otherNode], edges: [], object_bindings: [] };
  Object.assign(graph.entries[workflowId]!, { document: graphClone(document), saved_document: graphClone(document),
    saved_revision: 1 });
  workspace.markWorkflowSaved(workflowId, kind);
  const resources = createProviderResources({ list: async () => [provider], save: async () => ({}),
    readReceipt: vi.fn(), readPending: () => null, writePending: () => {} });
  resources.records.value = [provider];
  const profiles = createModelProfiles({ list: async () => [profile, alternative], save: vi.fn(),
    readReceipt: vi.fn(), readPending: () => null, writePending: () => {} });
  profiles.records.value = [profile, alternative];
  const host = createWorkbenchFrontendHost(() => graph.frontendExtensions, () => graph.packageLock);
  const requests: GraphConfigurationUpdateRequest[] = [];
  const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => {
    requests.push(graphClone(request));
    return savedResult(request);
  });
  const configuration = createWorkflowConfigurationApplication({
    evidence: evidence.ports, extensions: () => host.extensions.value, update,
    readReceipt: async request => savedResult(request),
  });
  graph.installWorkflowConfigurationApplication(configuration);
  const lifecycle = computed(() => JSON.stringify([workspace.activeWorkflowId,
    graph.session?.workflow_session_id ?? null, host.extensions.value.map(row => row.declaration)]));
  const sdk: WorkflowFrontendSdk = {
    workflowId: computed(() => workspace.activeWorkflowId),
    document: computed(() => graph.document), catalog: computed(() => graph.catalog),
    packages: computed(() => graph.packageLock), session: computed(() => graph.session),
    locked: computed(() => graph.editorLocked), lifecycle,
    configureNode: request => graph.patchNodeConfiguration(request),
  };
  installRender(RawNumberInput, "RawNumberInput.vue");
  installRender(RolePlacementFields, "RolePlacementFields.vue");
  installRender(LorebookKeywordFields, "LorebookKeywordFields.vue");
  installRender(LorebookFields, "LorebookFields.vue");
  installRender(ModelSourceFields, "ModelSourceFields.vue");
  const component = kind === "model" ? ModelSourceFields : LorebookFields;
  const selectedNode = shallowRef(node);
  const parent = { setup() {
    return () => h(component, { node: selectedNode.value, document: graph.document! });
  } };
  const root = new HostNode("root");
  const app = renderer.createApp(parent).use(pinia)
    .provide(workbenchFrontendHostKey, host).provide(workflowFrontendSdkKey, sdk)
    .provide(modelResourceControllerKey, resources).provide(modelProfilesKey, profiles)
    .provide(Vue.ssrContextKey, { modules: new Set() });
  apps.push(app); app.mount(root); await flush();
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  return { root, graph, evidence, requests, update, id: workflowId, node, otherNode,
    originalConfig: graphClone(node.config), kind, alternative, selectOther: () => { selectedNode.value = otherNode; } };
}
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount());
  piniaInstances.splice(0).forEach(disposePinia);
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});
async function edit(f: Awaited<ReturnType<typeof mount>>) {
  if (f.kind === "model") {
    const select = label(f.root, "全局模型配置");
    const selected = JSON.stringify([f.alternative.scope, f.alternative.resource_id]);
    select.options.forEach(option => { option.selected = option._value === selected; });
    select.value = selected;
    select.dispatchEvent(new Event("change"));
    await flush();
  } else {
    await trigger(label(f.root, "条目 1 扫描深度"), "Input", { value: "09" });
    await trigger(label(f.root, "条目 1 概率值"), "Input", { value: "42.5" });
    await trigger(label(f.root, "条目 1 插入深度"), "Input", { value: "03" });
    await trigger(label(f.root, "条目 1 装配顺序"), "Input", { value: "-2" });
  }
}
const formLabel = (kind: "model" | "lorebook") => kind === "model" ? "模型配置引用" : "Lorebook 配置";

describe("declared node configuration modes and ordinary input fields", () => {
  it("renders generic policy controls and emits original text through the schema editor", async () => {
    const definition = shallowRef(graphClone(editableInputType));
    const node: GraphNode = { node_binding_id: crypto.randomUUID(), component_id: editableInputType.component_id,
      component_version: "1", title: "Input", position: { x: 0, y: 0 }, config: { text: "" },
      configuration_policy: { schema_version: 1, prompt_mode: "adjustable" } };
    const document: GraphDocument = { schema_version: 3, workflow_definition_id: crypto.randomUUID(),
      revision: 1, name: "Input configuration", nodes: [node], edges: [], object_bindings: [] };
    const changes = vi.fn(), policies = vi.fn();
    installRender(GraphNodeConfiguration, "GraphNodeConfiguration.vue");
    installRender(GraphSchemaFields, "GraphSchemaFields.vue");
    const root = new HostNode("root");
    const app = renderer.createApp({ setup() {
      return () => h(GraphNodeConfiguration, { node, definition: definition.value, document,
        onChange: changes, onPolicyChange: policies });
    } }).provide(workbenchFrontendHostKey, createWorkbenchFrontendHost(() => [], () => []))
      .provide(Vue.ssrContextKey, { modules: new Set() });
    apps.push(app); app.mount(root); await flush();
    expect(label(root, "配置模式").props.role).toBe("group");
    const fixed = find(root, value => value.tag === "button" && value.text === "固定");
    const adjustable = find(root, value => value.tag === "button" && value.text === "可调");
    expect(adjustable.props["aria-pressed"]).toBe(true);
    await trigger(fixed, "Click");
    expect(policies).toHaveBeenCalledWith("fixed");
    const textarea = find(root, value => value.tag === "textarea");
    const original = "  first line\nsecond line  ";
    await trigger(textarea, "Input", { value: original });
    expect(changes).not.toHaveBeenCalled();
    await trigger(textarea, "Change", { value: original });
    expect(changes).toHaveBeenCalledWith({ text: original });
    await trigger(textarea, "Change", { value: "" });
    expect(changes).toHaveBeenLastCalledWith({ text: "" });
    expect(node.config.text).toBe("");

    definition.value = { ...definition.value, component_version: "2" }; await flush();
    expect(elements(root).some(value => value.props["aria-label"] === "配置模式")).toBe(false);
    definition.value = { ...definition.value, component_version: "1", adjustable_configuration: undefined }; await flush();
    expect(elements(root).some(value => value.props["aria-label"] === "配置模式")).toBe(false);
  });
});

describe("node field consumers through formal configuration", () => {
  it.each(["model", "lorebook"] as const)("submits the exact %s node identity and approved fields", async kind => {
    const f = await mount(kind);
    await edit(f);
    await trigger(label(f.root, formLabel(kind)), "Submit");
    await vi.waitFor(() => expect(f.requests).toHaveLength(1));
    await vi.waitFor(() => expect(f.graph.entries[f.id]!.document.revision).toBe(2));

    const request = f.requests[0]!;
    expect(request.workflow_definition_id).toBe(f.id);
    expect(request.document.schema_version).toBe(3);
    expect(request.document.nodes.map(row => row.node_binding_id)).toEqual([f.node.node_binding_id, f.otherNode.node_binding_id]);
    expect(request.document.nodes[1]!.config).toEqual(f.otherNode.config);
    expect(request.document.nodes[0]).toMatchObject({
      node_binding_id: f.node.node_binding_id, component_id: f.node.component_id,
      component_version: f.node.component_version,
    });
    expect(f.graph.entries[f.id]!.document.nodes[0]!.config).not.toEqual(f.originalConfig);
    expect(f.graph.entries[f.id]!.saved_revision).toBe(2);
    expect([...f.evidence.records.values()].filter(row => row.operation === "definition.update"))
      .toMatchObject([{ owner_kind: "workflow-configuration", owner_id: f.id, resolution: "verified",
        request: { workflow_definition_id: f.id } }]);
    if (kind === "model") {
      expect(label(f.root, "全局模型配置").value).toBe(JSON.stringify([f.alternative.scope, f.alternative.resource_id]));
      expect(f.graph.entries[f.id]!.document.nodes[0]!.config)
        .toEqual({ reference: modelProfileIdentity(f.alternative) });
      expect(request.document.nodes[0]!.config).toEqual({ reference: modelProfileIdentity(f.alternative) });
      expect(elements(f.root).some(node => node.text.includes("模型引用已保存到当前工作流"))).toBe(true);
    } else {
      expect(request.document.nodes[0]!.config.entry).toMatchObject({
        scan_depth: 9, probability: 42.5, presentation: { depth: 3, order: -2 },
      });
      expect(f.graph.entries[f.id]!.document.nodes[0]!.config.entry).toMatchObject({
        scan_depth: 9, probability: 42.5, presentation: { depth: 3, order: -2 },
      });
      expect(elements(f.root).some(node => node.text.includes("Lorebook 配置已保存到当前工作流"))).toBe(true);
    }
  });

  it.each(["model", "lorebook"] as const)("keeps a late %s receipt from changing the newly selected node status", async kind => {
    const f = await mount(kind), pending = deferred<GraphConfigurationResult>();
    f.update.mockImplementation(async request => {
      f.requests.push(graphClone(request));
      return pending.promise;
    });
    if (kind === "model") await edit(f);
    else await trigger(label(f.root, "条目 1 正文"), "Input", { value: "submitted" });
    const submitting = trigger(label(f.root, formLabel(kind)), "Submit");
    await vi.waitFor(() => expect(f.requests).toHaveLength(1));
    const originalRequest = f.requests[0]!;
    f.selectOther(); await flush();
    pending.resolve(savedResult(originalRequest));
    await submitting;
    await vi.waitFor(() => expect(f.graph.entries[f.id]!.document.revision).toBe(2));

    expect(originalRequest.workflow_definition_id).toBe(f.id);
    expect(originalRequest.document.nodes.find(row => row.node_binding_id === f.node.node_binding_id)!.config)
      .not.toEqual(f.originalConfig);
    expect(f.graph.entries[f.id]!.document.nodes[1]!.node_binding_id).toBe(f.otherNode.node_binding_id);
    expect(f.graph.entries[f.id]!.document.nodes[1]!.config).toEqual(f.otherNode.config);
    expect(elements(f.root).some(node => node.text.includes(kind === "model"
      ? "模型引用已保存" : "Lorebook 配置已保存"))).toBe(false);
  });
});
