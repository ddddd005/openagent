import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as Vue from "vue";
import { createRenderer, nextTick } from "vue";
import * as icons from "lucide-vue-next";
import { readFileSync } from "node:fs";
import { parse, compileScript } from "@vue/compiler-sfc";
import ts from "typescript";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import * as graphModule from "../stores/workflowGraph";
import * as graphDomain from "../domain/workflowGraph";
import * as preloadsDomain from "../domain/sessionPreloads";
import * as workspaceModule from "../stores/workspace";
import * as volatileFormModule from "../composables/useVolatileFormState";
import * as noticesModule from "../stores/workbenchNotices";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { preloadPlainType, preloadVariableType } from "../testUtils/sessionPreloadFixture";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { frontendExecutionLock } from "../testUtils/frontendFixture";
import { variableCatalog } from "../testUtils/variableFixture";

type Element = {
  tag: string; tagName: string; text: string; children: Element[]; parent: Element | null;
  props: Record<string, any>; listeners: Record<string, Function>; value: any; checked: boolean;
  style: Record<string, unknown>;
  options: Element[]; selected: boolean;
  addEventListener: (name: string, callback: Function) => void;
  removeEventListener: (name: string) => void; getAttribute: (name: string) => unknown;
  getRootNode: () => { activeElement: null };
};
function element(tag: string): Element {
  const node: Element = { tag, tagName: tag.toUpperCase(), text: "", children: [], parent: null, props: {},
    listeners: {}, style: {}, value: "", checked: false, selected: false,
    get options() { return this.children.filter(child => child.tag === "option"); },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    removeEventListener(name) { delete this.listeners[name]; }, getAttribute(name) { return this.props[name]; },
    getRootNode: () => ({ activeElement: null }) };
  return node;
}
const portal = element("body");
const renderer = createRenderer<Element, Element>({
  querySelector: () => portal,
  createElement: element, createText: text => Object.assign(element("#text"), { text }),
  createComment: text => Object.assign(element("#comment"), { text }),
  insert(node, parent, anchor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    node.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    if (index < 0) parent.children.push(node); else parent.children.splice(index, 0, node);
  },
  remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); },
  setText(node, text) { node.text = text; }, setElementText(node, text) { node.text = text; node.children = []; },
  parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
  patchProp(node, key, _old, value) { node.props[key] = value; if (key === "value") node.value = value; },
});
function all(root: Element): Element[] { return [root, ...root.children.flatMap(all)]; }
function label(root: Element, name: string) {
  const node = all(root).find(node => node.props["aria-label"] === name);
  if (!node) throw new Error(`Missing ${name}`);
  return node;
}
async function model(root: Element, name: string, value: unknown) {
  const node = label(root, name);
  if (node.tag === "select") {
    for (const option of node.options) option.selected = option.value === value;
    node.listeners.change!({ target: node });
  } else if (node.props.type === "checkbox") {
    node.checked = value === true; node.listeners.change!({ target: node });
  } else {
    node.value = value; node.listeners.input!({ target: node });
  }
  await nextTick();
}
async function click(root: Element, name: string) { label(root, name).props.onClick(); await nextTick(); }
async function submit(root: Element) {
  all(root).find(node => node.tag === "form")!.props.onSubmit({ preventDefault() {} }); await nextTick();
}
let pinia: Pinia, unmount: (() => void) | null = null;
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia);
  portal.children = [];
  vi.stubGlobal("Document", class Document {}); vi.stubGlobal("ShadowRoot", class ShadowRoot {});
});
afterEach(() => { unmount?.(); unmount = null; disposePinia(pinia); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function compileClient(filename: string, additionalModules: Record<string, unknown> = {}) {
  // Node-mode Vite compiles SFCs for SSR. Compile the same saved source for the
  // client renderer so native model directives and real event handlers execute.
  const source = readFileSync(new URL(filename, import.meta.url), "utf8");
  const { descriptor } = parse(source);
  const client = compileScript(descriptor, { id: "preload-interaction", inlineTemplate: true });
  const output = ts.transpileModule(client.content, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const modules: Record<string, unknown> = { vue: Vue, "lucide-vue-next": icons,
    "../stores/workflowGraph": graphModule, "../domain/workflowGraph": graphDomain, "../domain/sessionPreloads": preloadsDomain,
    "../stores/workspace": workspaceModule, "../stores/workbenchNotices": noticesModule,
    "../composables/useVolatileFormState": volatileFormModule, ...additionalModules };
  const exports: Record<string, any> = {};
  new Function("require", "exports", output)((name: string) => {
    if (!Object.hasOwn(modules, name)) throw new Error(`Unexpected client import ${name}`);
    return modules[name];
  }, exports);
  return exports.default;
}
async function mount(inWorkspace = false) {
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const graph = useWorkflowGraphStore();
  graph.dataTypes = [preloadVariableType, preloadPlainType]; graph.executionPackageLock = [...frontendExecutionLock];
  const id = graph.createWorkflow(); await vi.waitFor(() => expect(graph.editorLocked).toBe(false));
  const preloads = compileClient("./GraphSessionPreloads.vue");
  const stub = { default: { render: () => Vue.h("div") } };
  const component = inWorkspace ? compileClient("./WorkflowWorkspace.vue", {
    "./GraphSessionPreloads.vue": { default: preloads }, "./GraphWorkbench.vue": stub,
    "./GraphHistory.vue": stub, "./GraphRunControls.vue": stub,
  }) : preloads;
  const root = element("root"), app = renderer.createApp(component);
  app.use(pinia); app.mount(root); unmount = () => app.unmount();
  return { root, graph, id };
}
describe("preload editor interactions", () => {
  it("keeps request verification available after dismissing a notice and does not merge different request keys", async () => {
    const { root, graph, id } = await mount(true);
    const notices = noticesModule.useWorkbenchNoticesStore();
    const original = { action: "create", path: "/api/graph/sessions",
      body: { workflow_definition_id: id, definition_revision: 1, idempotency_key: crypto.randomUUID() } };
    graph.entries[id]!.pending = original;
    await nextTick();
    const first = notices.notices.find(row => row.severity === "major")!;
    expect(first).toBeDefined();
    expect(all(root).some(node => String(node.props.class).includes("model-pending-banner"))).toBe(false);
    notices.dismiss(first.id);
    expect(graph.entries[id]!.pending).toEqual(original);
    expect(graph.editorLocked).toBe(true);
    await click(root, "工作流原请求");
    expect(label(portal, "工作流原请求核实")).toBeDefined();
    await click(portal, "关闭原请求面板");
    expect(graph.entries[id]!.pending).toEqual(original);
    graph.receiptChecks[id] = "原请求待核实";
    await nextTick();
    const before = notices.notices.length;
    graph.entries[id]!.pending = { ...original, body: { ...original.body, idempotency_key: crypto.randomUUID() } };
    await nextTick();
    expect(notices.notices.length).toBe(before + 1);
    const after = notices.notices.length;
    graph.selectedNodeIds = [];
    await nextTick();
    expect(notices.notices.length).toBe(after);
  });
  it("opens the data view from the Workspace toolbar without requiring a session", async () => {
    const { root, graph } = await mount(true);
    expect(all(root).some(node => node.props["aria-label"] === "新增预加载项")).toBe(false);
    await click(root, "会话预加载数据");
    expect(label(root, "新增预加载项").props.disabled).toBe(false);
    expect(graph.session).toBeNull();
    await click(root, "新增预加载项");
    expect(label(root, "预加载对象键")).toBeTruthy();
  });
  it("creates a complete variable through controls without creating a session, then deletes it", async () => {
    const { root, graph } = await mount();
    await click(root, "新增预加载项");
    await model(root, "预加载对象键", "shared/score");
    await model(root, "变量名称", "score");
    await model(root, "变量标量类型", "integer");
    await model(root, "变量初值", "42");
    await submit(root);
    expect(graph.document!.session_preloads).toEqual([{ object_key: "shared/score",
      value: { registered: true, name: "score", value_type: "integer", assigned: true, value: 42 } }]);
    expect(graph.session).toBeNull();
    const row = all(root).find(node => node.tag === "button" && node.text === "shared/score")!;
    row.props.onClick(); await nextTick();
    expect(label(root, "预加载对象键").props.readonly).toBe(true);
    await click(root, "删除预加载项");
    expect(graph.document!.session_preloads).toEqual([]);
    expect(graph.document!.object_bindings).toEqual([]);
  });
  it("reports JSON errors, accepts explicit values without a type default, and resets on workflow switching", async () => {
    const { root, graph } = await mount();
    await click(root, "新增预加载项");
    await model(root, "预加载对象键", "shared/plain");
    await model(root, "预加载类型", "sample.structure@1");
    await model(root, "预加载 JSON 初值", "{broken");
    await submit(root);
    expect(all(root).some(node => node.props.role === "alert")).toBe(true);
    expect(graph.document!.session_preloads).toBeUndefined();
    await model(root, "预加载 JSON 初值", '{"label":"explicit"}');
    await submit(root);
    expect(graph.document!.session_preloads![0]!.value).toEqual({ label: "explicit" });
    await click(root, "新增预加载项");
    const staleForm = all(root).find(node => node.tag === "form")!;
    graph.createWorkflow();
    staleForm.props.onSubmit({ preventDefault() {} });
    expect(graph.document!.session_preloads).toBeUndefined();
    await nextTick();
    expect(all(root).some(node => node.tag === "form")).toBe(false);
  });
  it("keeps archived configuration visible and blocks add events even when called directly", async () => {
    const { root, graph, id } = await mount();
    useWorkspaceStore().workflows.find(row => row.id === id)!.archived = true;
    await nextTick();
    expect(label(root, "新增预加载项").props.disabled).toBe(true);
    await click(root, "新增预加载项");
    expect(all(root).some(node => node.tag === "form")).toBe(false);
    expect(graph.document!.session_preloads).toBeUndefined();
  });
  it("writes private ownership and checked ACL, and preserves an unassigned variable", async () => {
    const { root, graph } = await mount();
    graph.catalog = [...variableCatalog];
    const owner = graph.addNode("tools.variable-to-content@1", { x: 0, y: 0 })!;
    await nextTick();
    await click(root, "新增预加载项");
    await model(root, "预加载对象键", "private/score");
    await model(root, "变量名称", "score");
    await model(root, "赋初值", false);
    await model(root, "预加载归属", "private");
    await model(root, "预加载所有者", owner);
    await model(root, `${graph.document!.nodes[0]!.title} 读取`, true);
    await submit(root);
    expect(graph.document!.object_bindings![0]).toMatchObject({ scope: "private", owner_node_id: owner,
      readers: [owner], writers: [] });
    expect(graph.document!.session_preloads![0]!.value).toMatchObject({ registered: true, assigned: false, value: null });
  });
});
