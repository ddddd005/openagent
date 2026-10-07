import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse as parseSfc } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { computed, createRenderer, createSSRApp, nextTick, reactive, ref, type App } from "vue";
import { renderToString } from "@vue/server-renderer";
import { afterEach, describe, expect, it, vi } from "vitest";
import LorebookFields from "./LorebookFields.vue";
import LorebookKeywordFields from "./LorebookKeywordFields.vue";
import LorebookEvaluationFacts from "./LorebookEvaluationFacts.vue";
import RolePlacementFields from "./RolePlacementFields.vue";
import GraphNodeConfiguration from "./GraphNodeConfiguration.vue";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";
import { graphClone, type GraphDocument, type GraphNode, type GraphNodeType } from "../domain/workflowGraph";
import { lorebookPackage, newLorebookEntry, type LorebookEntry } from "../domain/lorebook";
import { tavernFrontendExtensions } from "../plugins/tavernFrontendManifest";
import { workflowFrontendSdkKey, type WorkflowFrontendSdk, type WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";

function fixture(group = false, entries = [newLorebookEntry()]) {
  const node = reactive<GraphNode>({
    node_binding_id: crypto.randomUUID(), component_id: group ? "lorebook.group" : "lorebook.item", component_version: "1",
    title: "Lorebook", position: { x: 0, y: 0 }, config: group
      ? { entries: graphClone(entries), object_keys: [] } : { entry: graphClone(entries[0]!), object_keys: [] },
  });
  const document = reactive<GraphDocument>({ schema_version: 2, workflow_definition_id: crypto.randomUUID(),
    revision: 1, name: "Lorebook", nodes: [node], edges: [], object_bindings: [],
    package_lock: [lorebookPackage], execution_roots: [], control_edges: [] });
  const locked = ref(false), lifecycle = ref("original");
  const configureNode = vi.fn((_request: WorkflowNodeConfigurationRequest) =>
    ({ workflowId: document.workflow_definition_id, nodeId: node.node_binding_id }));
  const definition: GraphNodeType = {
    component_id: node.component_id, component_version: "1", category: "类酒馆", display_name: "Lorebook",
    default_config: node.config, executable: true, is_output: false, input_storage: "references",
    config_schema: { type: "object", properties: group
      ? { entries: { type: "array" }, object_keys: { type: "array" } }
      : { entry: { type: "object" }, object_keys: { type: "array" } } },
    inputs: [{ port_id: "input", data_type: "PROMPT", data_schema_version: 6, required: true, multiple: false }],
    outputs: [{ port_id: "output", data_type: "PROMPT_MATERIALS", data_schema_version: 1, required: true, multiple: false }],
  };
  const sdk: WorkflowFrontendSdk = {
    workflowId: computed(() => document.workflow_definition_id), document: computed(() => document),
    catalog: computed(() => [definition]), packages: computed(() => document.package_lock ?? []),
    session: computed(() => null), locked: computed(() => locked.value), lifecycle: computed(() => lifecycle.value),
    attachDisplay: () => false, readArtifact: vi.fn(), configureNode,
  };
  return { node, document, definition, sdk, configureNode, locked, lifecycle };
}

class HostNode {
  parent: HostNode | null = null;
  children: HostNode[] = [];
  props: Record<string, unknown> = {};
  text = "";
  constructor(readonly tag = "") {}
}
function insert(child: HostNode, parent: HostNode, anchor: HostNode | null = null) {
  if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
  child.parent = parent;
  const at = anchor ? parent.children.indexOf(anchor) : -1;
  if (at < 0) parent.children.push(child); else parent.children.splice(at, 0, child);
}
const renderer = createRenderer<HostNode, HostNode>({
  createElement: tag => new HostNode(tag), createText: text => Object.assign(new HostNode(), { text }),
  createComment: text => Object.assign(new HostNode(), { text }), insert,
  remove: node => {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    node.parent = null;
  },
  setText: (node, text) => { node.text = text; },
  setElementText: (node, text) => { node.text = text; node.children = []; },
  patchProp: (node, key, _previous, value) => { node.props[key] = value; },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
});
const apps: App<HostNode>[] = [];
async function flush() { await Promise.resolve(); await nextTick(); await Promise.resolve(); await nextTick(); }
function elements(root: HostNode): HostNode[] { return [root, ...root.children.flatMap(elements)]; }
function find(root: HostNode, predicate: (node: HostNode) => boolean) {
  const found = elements(root).find(predicate);
  expect(found, "rendered control").toBeDefined(); return found!;
}
function label(root: HostNode, text: string) { return find(root, node => node.props["aria-label"] === text); }
function button(root: HostNode, text: string) {
  return find(root, node => node.tag === "button" && elements(node).map(child => child.text).join("").trim() === text);
}
async function trigger(node: HostNode, event: "Click" | "Submit" | "Change" | "Input",
  target: Record<string, unknown> = {}) {
  await (node.props[`on${event}`] as (event: unknown) => unknown)({ target, preventDefault() {} });
  await flush();
}
function installRender(component: object, filename: string) {
  const { descriptor } = parseSfc(readFileSync(new URL(filename, import.meta.url), "utf8"));
  const script = compileScript(descriptor, { id: "lorebook-field-interactions" });
  const template = compileTemplate({ source: descriptor.template!.content, filename, id: "lorebook-field-interactions",
    compilerOptions: { mode: "function", bindingMetadata: script.bindings } });
  expect(template.errors).toEqual([]);
  const code = transpileModule(template.code, { compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None } }).outputText;
  Object.assign(component, { render: new Function("Vue", code)(Vue) });
}
async function mount(f: ReturnType<typeof fixture>) {
  installRender(LorebookKeywordFields, "LorebookKeywordFields.vue");
  installRender(RolePlacementFields, "RolePlacementFields.vue");
  installRender(LorebookFields, "LorebookFields.vue");
  const root = new HostNode("root");
  const app = renderer.createApp(LorebookFields, { node: f.node, document: f.document })
    .provide(workflowFrontendSdkKey, f.sdk).provide(Vue.ssrContextKey, { modules: new Set() });
  apps.push(app); app.mount(root); await flush(); return root;
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.unstubAllGlobals(); });
const renderFields = (f: ReturnType<typeof fixture>) => renderToString(createSSRApp(LorebookFields,
  { node: f.node, document: f.document }).provide(workflowFrontendSdkKey, f.sdk));

describe("lorebook trusted workbench fields", () => {
  it("loads only the exact package declarations and owns the correct fields", async () => {
    const f = fixture(), host = createWorkbenchFrontendHost(() => tavernFrontendExtensions, () => f.sdk.packages.value);
    expect(host.issues.value).toEqual([]);
    expect(host.extensions.value.map(row => row.declaration)).toEqual(tavernFrontendExtensions);
    expect(host.extensions.value.map(row => row.configurationFields)).toEqual([["entry", "object_keys"], ["entries", "object_keys"]]);
    const html = await renderToString(createSSRApp(GraphNodeConfiguration,
      { node: f.node, document: f.document, definition: f.definition })
      .provide(workbenchFrontendHostKey, host).provide(workflowFrontendSdkKey, f.sdk));
    expect(html).toContain('aria-label="Lorebook 配置"');
    expect(html).toContain("per_request / never");
    expect(html).not.toContain('aria-label="对象绑定 JSON"');
    expect(html).not.toContain("&quot;primary_keywords&quot;");
    expect(f.configureNode).not.toHaveBeenCalled();
    expect(createWorkbenchFrontendHost(() => tavernFrontendExtensions, () => []).extensions.value).toEqual([]);
    expect(createWorkbenchFrontendHost(() => tavernFrontendExtensions,
      () => [{ ...lorebookPackage, version: "2.0.0" }]).extensions.value).toEqual([]);
    const forged = tavernFrontendExtensions.map(row => ({ ...row, entrypoint: row.entrypoint + ".forged" }));
    expect(createWorkbenchFrontendHost(() => forged, () => f.sdk.packages.value).extensions.value).toEqual([]);
  });
  it("preserves the original graph as generic fallback when the trusted package is absent", async () => {
    const f = fixture(), original = graphClone(f.node.config);
    const host = createWorkbenchFrontendHost(() => tavernFrontendExtensions, () => []);
    const html = await renderToString(createSSRApp(GraphNodeConfiguration,
      { node: f.node, document: f.document, definition: f.definition, disabled: true })
      .provide(workbenchFrontendHostKey, host).provide(workflowFrontendSdkKey, f.sdk));
    expect(html).not.toContain('aria-label="Lorebook 配置"');
    expect(html).toContain("&quot;primary_keywords&quot;");
    expect(html).toContain("disabled");
    expect(f.node.config).toEqual(original); expect(f.configureNode).not.toHaveBeenCalled();
  });
  it("applies the single entry with the original config/lifecycle and no graph or variable side effects", async () => {
    const f = fixture(), entry = f.node.config.entry as LorebookEntry;
    entry.metadata = { preserved: { nested: "value" } };
    const original = graphClone(f.node.config), root = await mount(f);
    await trigger(label(root, "条目 1 名称"), "Input", { value: "山谷" });
    await trigger(label(root, "条目 1 正文"), "Input", { value: "古老山谷的正文" });
    await trigger(label(root, "条目 1 新增主关键词"), "Click");
    await trigger(label(root, "条目 1 主关键词 1"), "Input", { value: "{{地点}}" });
    await trigger(label(root, "条目 1 新增副关键词"), "Click");
    await trigger(label(root, "条目 1 副关键词 1"), "Input", { value: "Moon" });
    await trigger(label(root, "条目 1 组合规则"), "Change", { value: "primary_and_secondary" });
    await trigger(label(root, "条目 1 递归触发"), "Change", { checked: true });
    await trigger(label(root, "条目 1 概率开关"), "Change", { checked: true });
    await trigger(label(root, "条目 1 概率值"), "Input", { valueAsNumber: 42.5 });
    await trigger(label(root, "条目 1 扫描深度"), "Input", { valueAsNumber: 20 });
    await trigger(label(root, "Lorebook 配置"), "Submit");
    expect(f.configureNode).toHaveBeenCalledWith({
      workflowId: f.document.workflow_definition_id, nodeId: f.node.node_binding_id,
      componentId: "lorebook.item", componentVersion: "1", expectedConfig: original,
      extensionId: "workflow.tavern.item-fields", lifecycle: "original",
      patch: { object_keys: [], entry: { ...original.entry as LorebookEntry,
        name: "山谷", text: "古老山谷的正文", primary_keywords: ["{{地点}}"], secondary_keywords: ["Moon"],
        keyword_rule: "primary_and_secondary", recursive: true, probability_enabled: true, probability: 42.5 } },
    });
    expect(f.node.config).toEqual(original);
  });
  it("supports constant mode and distinct scanning/insertion depth without changing lifecycle", async () => {
    const f = fixture(), root = await mount(f);
    await trigger(label(root, "条目 1 模式"), "Change", { value: "constant" });
    expect(elements(root).some(node => node.props["aria-label"] === "条目 1 新增主关键词")).toBe(false);
    await trigger(label(root, "条目 1 扫描深度"), "Input", { valueAsNumber: 7 });
    const middle = find(root, node => node.tag === "button" && node.text === "上下文中");
    await trigger(middle, "Click");
    await trigger(label(root, "条目 1 插入深度"), "Input", { valueAsNumber: 2 });
    await trigger(label(root, "条目 1 装配顺序"), "Input", { valueAsNumber: -3 });
    await trigger(label(root, "条目 1 启用"), "Change", { checked: false });
    await trigger(label(root, "Lorebook 配置"), "Submit");
    expect(f.configureNode.mock.calls[0]![0].patch).toMatchObject({
      entry: { mode: "constant", scan_depth: 7,
        presentation: { placement: "middle", depth: 2, order: -3, enabled: false } },
    });
    expect((f.configureNode.mock.calls[0]![0].patch.entry as LorebookEntry).metadata).toEqual({});
    expect(Object.hasOwn(f.configureNode.mock.calls[0]![0].patch.entry as object, "lifecycle")).toBe(false);
  });
  it("adds, renames, reorders equal ranks and deletes group entries while retaining stable identities", async () => {
    const entries = [newLorebookEntry(), newLorebookEntry()], f = fixture(true, entries), root = await mount(f);
    await trigger(label(root, "条目 1 名称"), "Input", { value: "Renamed" });
    await trigger(label(root, "条目 2 上移"), "Click");
    await trigger(button(root, "新增条目"), "Click");
    await trigger(label(root, "Lorebook 配置"), "Submit");
    const saved = f.configureNode.mock.calls[0]![0].patch.entries as LorebookEntry[];
    expect(saved).toHaveLength(3);
    expect(saved.map(entry => entry.id).slice(0, 2)).toEqual([entries[1]!.id, entries[0]!.id]);
    expect(saved.map(entry => entry.presentation.order)).toEqual([0, 1, 2]);
    expect(saved[1]!.name).toBe("Renamed");
    expect(new Set(saved.map(entry => entry.id)).size).toBe(3);
    await trigger(label(root, "条目 1 移除"), "Click");
    await trigger(label(root, "Lorebook 配置"), "Submit");
    expect((f.configureNode.mock.calls[1]![0].patch.entries as LorebookEntry[]).map(entry => entry.id))
      .toEqual([entries[0]!.id, saved[2]!.id]);
  });
  it("allows empty groups to apply as an explicit configuration", async () => {
    const f = fixture(true, []), root = await mount(f);
    await trigger(label(root, "Lorebook 配置"), "Submit");
    expect(f.configureNode.mock.calls[0]![0].patch).toEqual({ entries: [], object_keys: [] });
  });
  it("offers only reader-authorized variables and removes invalid selections explicitly", async () => {
    const f = fixture(), base = { object_key: "shared/place", type_id: "workflow.variable", schema_version: 1,
      scope: "shared" as const, readers: [f.node.node_binding_id], writers: [], owner_node_id: null };
    f.document.object_bindings = [base, { ...base, object_key: "shared/secret", readers: [] }];
    f.node.config.object_keys = ["shared/secret"];
    const original = graphClone(f.document), root = await mount(f);
    expect(elements(root).some(node => node.props["aria-label"] === "读取变量对象 shared/secret")).toBe(false);
    await trigger(label(root, "Lorebook 配置"), "Submit");
    expect(f.configureNode).not.toHaveBeenCalled();
    await trigger(label(root, "移除无效变量对象 shared/secret"), "Change", { checked: false });
    await trigger(label(root, "读取变量对象 shared/place"), "Change", { checked: true });
    await trigger(label(root, "Lorebook 配置"), "Submit");
    expect(f.configureNode.mock.calls[0]![0].patch.object_keys).toEqual(["shared/place"]);
    expect(f.document).toEqual(original);
  });
  it("rejects invalid draft numbers and keeps invalid source contracts unchanged", async () => {
    const f = fixture(), root = await mount(f);
    await trigger(label(root, "条目 1 扫描深度"), "Input", { valueAsNumber: 1.5 });
    await trigger(label(root, "Lorebook 配置"), "Submit");
    expect(f.configureNode).not.toHaveBeenCalled();
    expect(elements(root).some(node => node.text.includes("尚未应用"))).toBe(true);
    const invalid = fixture(); invalid.node.config.extra = true;
    const original = graphClone(invalid.node.config), html = await renderFields(invalid);
    expect(html).toContain("原配置保持不变");
    const other = await mount(invalid);
    await trigger(label(other, "Lorebook 配置"), "Submit");
    expect(invalid.configureNode).not.toHaveBeenCalled(); expect(invalid.node.config).toEqual(original);
  });
  it("uses current lifecycle/config and refuses mutation while locked or stale", async () => {
    const f = fixture(), root = await mount(f);
    f.locked.value = true; await flush();
    await trigger(label(root, "条目 1 正文"), "Input", { value: "blocked" });
    await trigger(label(root, "Lorebook 配置"), "Submit");
    expect(f.configureNode).not.toHaveBeenCalled();
    f.locked.value = false; f.lifecycle.value = "replacement";
    f.node.config = { ...f.node.config, entry: { ...f.node.config.entry as LorebookEntry, text: "external" } };
    await flush();
    await trigger(label(root, "Lorebook 配置"), "Submit");
    expect(f.configureNode.mock.calls[0]![0]).toMatchObject({
      expectedConfig: f.node.config, lifecycle: "replacement", patch: { entry: { text: "external" } },
    });
    f.configureNode.mockReturnValueOnce(null as unknown as ReturnType<typeof f.configureNode>);
    await trigger(label(root, "Lorebook 配置"), "Submit");
    expect(elements(root).some(node => node.text.includes("配置依据已变化"))).toBe(true);
  });
});

describe("lorebook history presentation", () => {
  it("shows activation, keyword miss, probability rejection and disabled evidence without output changes", async () => {
    const reads = [{ kind: "lorebook_evaluation", recursive_rounds: 3, entries: [
      { entry_id: crypto.randomUUID(), name: "Activated", status: "activated", activation_round: 3,
        probability_passed: true, primary_matched: true, secondary_matched: false },
      { entry_id: crypto.randomUUID(), name: "Miss", status: "keyword_miss", activation_round: null,
        probability_passed: true, primary_matched: false, secondary_matched: false },
      { entry_id: crypto.randomUUID(), name: "Rejected", status: "probability_rejected", activation_round: null,
        probability_passed: false, primary_matched: null, secondary_matched: null },
      { entry_id: crypto.randomUUID(), name: "Disabled", status: "disabled", activation_round: null,
        probability_passed: null, primary_matched: null, secondary_matched: null },
    ] }];
    const html = await renderToString(createSSRApp(LorebookEvaluationFacts, { reads }));
    for (const text of ["已触发", "关键词未命中", "概率未通过", "已禁用", "递归第 3 轮", "未检查"])
      expect(html).toContain(text);
    expect(html).not.toContain("变量值");
    const irrelevant = await renderToString(createSSRApp(LorebookEvaluationFacts, { reads: [{ kind: "other", entries: [] }] }));
    expect(irrelevant).not.toContain("Lorebook 触发结果");
  });
});
