import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse as parseSfc } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { computed, createRenderer, createSSRApp, nextTick, reactive, ref, type App } from "vue";
import { createPinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import { afterEach, describe, expect, it, vi } from "vitest";
import CurrentPromptPanel from "./CurrentPromptPanel.vue";
import PromptReferenceFields from "./PromptReferenceFields.vue";
import GraphNodeConfiguration from "./GraphNodeConfiguration.vue";
import { createPromptResources, promptResourceControllerKey } from "../application/promptResources";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone, newGraph, type GraphNode, type GraphNodeType } from "../domain/workflowGraph";
import { newPromptMember, newPromptResource, promptIdentity,
  type CurrentPromptResource } from "../domain/workflowPromptResources";
import { promptFrontendExtensions } from "../plugins/promptFrontendManifest";
import { workflowFrontendSdkKey, type WorkflowFrontendSdk,
  type WorkflowNodeConfigurationRequest } from "../plugins/workflowFrontendSdk";

function fixture(initial = [newPromptResource()]) {
  const state = { records: graphClone(initial), failSave: false };
  const list = vi.fn(async () => graphClone(state.records));
  const writePending = vi.fn();
  const save = vi.fn(async (request: Parameters<Parameters<typeof createPromptResources>[0]["save"]>[0]) => {
    if (state.failSave) throw new WorkbenchApiError("unknown", "Controlled response loss");
    state.records = state.records.filter(record => JSON.stringify(promptIdentity(record))
      !== JSON.stringify(promptIdentity(request.record)));
    state.records.push(graphClone(request.record));
    return {};
  });
  const readReceipt = vi.fn(async (_request: Parameters<typeof save>[0]) => ({}));
  const resources = createPromptResources({ list, save, readReceipt, readPending: () => null, writePending });
  resources.records.value = graphClone(initial);
  const record = initial[0] ?? newPromptResource();
  const node = reactive<GraphNode>({
    node_binding_id: crypto.randomUUID(), component_id: "prompts.global-reference", component_version: "2",
    title: "Prompt reference", position: { x: 0, y: 0 }, config: { reference: promptIdentity(record) },
  });
  const document = { ...newGraph("Prompt reference fields"), nodes: [node],
    package_lock: [{ package_id: "workflow.prompts", version: "1.0.0" }] };
  const locked = ref(false), lifecycle = ref("original");
  const configureNode = vi.fn((_request: WorkflowNodeConfigurationRequest) =>
    ({ workflowId: document.workflow_definition_id, nodeId: node.node_binding_id }));
  const sdk: WorkflowFrontendSdk = {
    workflowId: computed(() => document.workflow_definition_id), document: computed(() => document),
    catalog: computed(() => []), packages: computed(() => document.package_lock), session: computed(() => null),
    locked: computed(() => locked.value), lifecycle: computed(() => lifecycle.value),
    attachDisplay: () => false, readArtifact: vi.fn(), configureNode,
  };
  const definition: GraphNodeType = {
    component_id: node.component_id, component_version: node.component_version, display_name: "Prompt reference",
    category: "Prompts", default_config: node.config, inputs: [],
    outputs: [{ port_id: "output", data_type: "GLOBAL_RESOURCE_REF", required: true, multiple: false }],
    executable: true, is_output: false, input_storage: "references", capabilities: ["resources:read"],
    config_schema: { type: "object", required: ["reference"], additionalProperties: false,
      properties: { reference: { type: "object" } } },
  };
  return { state, list, save, readReceipt, writePending, resources, node, document, definition, sdk, configureNode, locked, lifecycle };
}

// This small renderer runs actual mounted hooks and form handlers without a browser or live backend.
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
  get options(): HostNode[] {
    return this.children.flatMap(child => child.tag === "option" ? [child] : child.options);
  }
}
function insert(child: HostNode, parent: HostNode, anchor: HostNode | null = null) {
  if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
  child.parent = parent;
  const index = anchor ? parent.children.indexOf(anchor) : -1;
  if (index < 0) parent.children.push(child); else parent.children.splice(index, 0, child);
}
const renderer = createRenderer<HostNode, HostNode>({
  createElement: tag => new HostNode(tag), createText: text => Object.assign(new HostNode(), { text }),
  createComment: text => Object.assign(new HostNode(), { text }),
  insert, remove: node => {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    node.parent = null;
  },
  setText: (node, text) => { node.text = text; },
  setElementText: (node, text) => { node.text = text; node.children = []; },
  patchProp: (node, key, _previous, value) => {
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
  const found = elements(root).find(predicate);
  expect(found, "rendered control").toBeDefined();
  return found!;
}
function label(root: HostNode, text: string) { return find(root, node => node.props["aria-label"] === text); }
function button(root: HostNode, text: string) {
  return find(root, node => node.tag === "button" && elements(node).map(child => child.text).join("").trim() === text);
}
function model(node: HostNode, value: unknown) {
  (node.props["onUpdate:modelValue"] as (value: unknown) => void)(value);
}
async function trigger(node: HostNode, event: "Click" | "Submit" | "Change") {
  await (node.props[`on${event}`] as (event: { preventDefault(): void }) => unknown)({ preventDefault() {} });
  await flush();
}
async function mount(component: typeof CurrentPromptPanel | typeof PromptReferenceFields, f: ReturnType<typeof fixture>) {
  vi.stubGlobal("document", { activeElement: null });
  vi.stubGlobal("Document", class {});
  vi.stubGlobal("ShadowRoot", class {});
  const filename = component === CurrentPromptPanel ? "CurrentPromptPanel.vue" : "PromptReferenceFields.vue";
  const { descriptor } = parseSfc(readFileSync(new URL(filename, import.meta.url), "utf8"));
  const script = compileScript(descriptor, { id: "current-prompt-interactions" });
  const template = compileTemplate({ source: descriptor.template!.content, filename, id: "current-prompt-interactions",
    compilerOptions: { mode: "function", bindingMetadata: script.bindings } });
  expect(template.errors).toEqual([]);
  const code = transpileModule(template.code, {
    compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None },
  }).outputText;
  const clientComponent = { ...component, render: new Function("Vue", code)(Vue) };
  const root = new HostNode("root");
  const app = renderer.createApp(clientComponent, component === PromptReferenceFields ? { node: f.node, document: f.document } : {});
  app.provide(promptResourceControllerKey, f.resources).provide(workflowFrontendSdkKey, f.sdk);
  app.provide(Vue.ssrContextKey, {});
  apps.push(app);
  app.mount(root);
  await flush();
  return root;
}
function renderFields(f: ReturnType<typeof fixture>) {
  return renderToString(createSSRApp(PromptReferenceFields, { node: f.node, document: f.document })
    .provide(promptResourceControllerKey, f.resources).provide(workflowFrontendSdkKey, f.sdk));
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.unstubAllGlobals(); });

describe("current prompt panel form interactions", () => {
  it("saves an empty enabled workspace resource with read-only identity and no schema-1 name or kind fields", async () => {
    const f = fixture([]), root = await mount(CurrentPromptPanel, f);
    await trigger(label(root, "新增提示词资源"), "Click");
    const form = label(root, "提示词当前资源表单");
    expect(elements(form).filter(node => node.tag === "output").map(node => node.text)).toContain("workspace");
    expect(elements(form).some(node => node.tag === "textarea")).toBe(false);
    await trigger(form, "Submit");
    expect(f.save).toHaveBeenCalledTimes(1);
    expect(f.save.mock.calls[0]![0]).toMatchObject({
      expected_sequence: 0, record: { scope: "workspace", data_schema_version: 2,
        update_sequence: 1, value: { enabled: true, members: [] } },
    });
    expect(Object.keys(f.save.mock.calls[0]![0].record.value)).toEqual(["enabled", "members"]);
    expect(f.resources.records.value).toHaveLength(1);
    expect(elements(root).some(node => node.props["aria-label"] === "提示词当前资源表单")).toBe(false);
  });
  it("saves schema-two lifecycle settings and clears incompatible compaction permission on role change", async () => {
    const record = newPromptResource(2);
    record.value.members = [newPromptMember("Long-running background", 2)];
    const f = fixture([record]), root = await mount(CurrentPromptPanel, f);
    await trigger(find(root, node => node.tag === "button" && node.children.some(child => child.tag === "strong")), "Click");
    const role = label(root, "条目 1 Role"), lifecycle = label(root, "条目 1 生命周期");
    model(role, "user"); await trigger(role, "Change");
    model(lifecycle, "context_once"); await trigger(lifecycle, "Change");
    const permission = label(root, "条目 1 精简许可");
    expect(permission.props.disabled).toBe(false);
    model(permission, "allowed");
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    expect(f.save.mock.calls[0]![0].record.value.members[0]).toMatchObject({
      lifecycle: "context_once", compaction: "allowed", presentation: { role: "user" } });
    await trigger(find(root, node => node.tag === "button" && node.children.some(child => child.tag === "strong")), "Click");
    const nextRole = label(root, "条目 1 Role");
    model(nextRole, "system"); await trigger(nextRole, "Change");
    expect(label(root, "条目 1 精简许可").props.disabled).toBe(true);
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    expect(f.save.mock.calls[1]![0].record.value.members[0]).toMatchObject({
      lifecycle: "context_once", compaction: "never", presentation: { role: "system" } });
  });

  it.each([1, 2] as const)("copies a schema-%i group into an unsaved independent draft without rebinding the node", async version => {
    const record = newPromptResource(version), member = newPromptMember("Original background", version);
    record.scope = "project:story"; record.update_sequence = 7; record.value.enabled = false;
    member.presentation = { role: "user", placement: "middle", depth: 2, order: -8, enabled: false };
    member.metadata = { custom: { retained: ["yes", true] } };
    if (version === 2) { member.lifecycle = "context_once"; member.compaction = "allowed"; }
    record.value.members = [member];
    const original = graphClone(record), f = fixture([record]), root = await mount(CurrentPromptPanel, f);
    await trigger(label(root, `复制提示词组 ${record.scope} ${record.resource_id}`), "Click");
    expect(f.save).not.toHaveBeenCalled(); expect(f.configureNode).not.toHaveBeenCalled();
    expect(f.resources.records.value).toEqual([original]);
    const form = label(root, "提示词当前资源表单");
    const output = elements(form).filter(node => node.tag === "output");
    expect(output[0]!.text).toBe(record.scope);
    expect(output[1]!.text).not.toBe(record.resource_id);
    await trigger(form, "Submit");
    const submitted = f.save.mock.calls[0]![0];
    expect(submitted.expected_sequence).toBe(0);
    expect(submitted.record.resource_id).not.toBe(record.resource_id);
    expect(submitted.record.update_sequence).toBe(1);
    expect(submitted.record.data_schema_version).toBe(version);
    expect(submitted.record.value.enabled).toBe(false);
    expect(submitted.record.value.members[0]!.id).not.toBe(member.id);
    expect({ ...submitted.record.value.members[0], id: member.id }).toEqual(member);
    expect(f.resources.records.value).toContainEqual(original);
    expect(f.resources.records.value).toHaveLength(2);
    expect(f.configureNode).not.toHaveBeenCalled();
    expect(f.node.config).toEqual({ reference: promptIdentity(original) });
  });

  it("moves member ranks while preserving UUIDs, lifecycle, metadata and placement", async () => {
    const record = newPromptResource(2);
    const first = newPromptMember("Rule", 2), second = newPromptMember("Background", 2);
    first.presentation.order = -8; second.presentation.order = 4;
    second.presentation.role = "user"; second.presentation.placement = "middle"; second.presentation.depth = 2;
    second.lifecycle = "context_once"; second.compaction = "allowed"; second.metadata = { unknown: ["keep"] };
    record.value.members = [first, second];
    const f = fixture([record]), root = await mount(CurrentPromptPanel, f);
    await trigger(find(root, node => node.props.class === "resource-edit"), "Click");
    expect(label(root, "条目 1 上移").props.disabled).toBe(true);
    expect(label(root, "条目 2 下移").props.disabled).toBe(true);
    await trigger(label(root, "条目 2 上移"), "Click");
    expect(label(root, "条目 1 正文").value).toBe(second.text);
    expect(label(root, "条目 1 装配顺序").value).toBe(-8);
    expect(label(root, "条目 2 装配顺序").value).toBe(4);
    expect(f.resources.records.value).toEqual([record]);
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    expect(f.save.mock.calls[0]![0].record.value.members).toEqual([
      { ...second, presentation: { ...second.presentation, order: -8 } },
      { ...first, presentation: { ...first.presentation, order: 4 } },
    ]);
  });

  it("appends after the largest existing rank without normalizing negative or unsorted ranks", async () => {
    const record = newPromptResource(2);
    record.value.members = [newPromptMember("First", 2), newPromptMember("Second", 2)];
    record.value.members[0]!.presentation.order = 9; record.value.members[1]!.presentation.order = -4;
    const f = fixture([record]), root = await mount(CurrentPromptPanel, f);
    await trigger(find(root, node => node.props.class === "resource-edit"), "Click");
    await trigger(button(root, "新增条目"), "Click");
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    const members = f.save.mock.calls[0]![0].record.value.members;
    expect(members.map(member => member.presentation.order)).toEqual([9, -4, 10]);
    expect(members.slice(0, 2)).toEqual(record.value.members);
    expect(members[2]).toMatchObject({ lifecycle: "per_request", compaction: "never" });
  });

  it("blocks rank overflow and allows appending after an explicit rank adjustment", async () => {
    const record = newPromptResource(2); record.value.members = [newPromptMember("At limit", 2)];
    record.value.members[0]!.presentation.order = Number.MAX_SAFE_INTEGER;
    const f = fixture([record]), root = await mount(CurrentPromptPanel, f);
    await trigger(find(root, node => node.props.class === "resource-edit"), "Click");
    await trigger(button(root, "新增条目"), "Click");
    expect(elements(root).filter(node => node.tag === "textarea")).toHaveLength(1);
    expect(elements(root).some(node => node.text.includes("条目顺序已达上限"))).toBe(true);
    model(label(root, "条目 1 装配顺序"), -3);
    await trigger(button(root, "新增条目"), "Click");
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    expect(f.save.mock.calls[0]![0].record.value.members.map(member => member.presentation.order)).toEqual([-3, -2]);
  });

  it("preserves unknown metadata and negative order while editing text, all presentation fields and resource enabled", async () => {
    const record = newPromptResource(), member = newPromptMember("Original prompt");
    member.presentation.order = -8;
    member.metadata = { unknown: { nested: ["preserve", 2, false] }, custom: null };
    record.value.members = [member]; record.update_sequence = 9;
    const original = graphClone(record);
    const f = fixture([record]), root = await mount(CurrentPromptPanel, f);
    await trigger(find(root, node => node.tag === "button" && node.children.some(child => child.tag === "strong")), "Click");
    model(label(root, "条目 1 正文"), "Edited prompt");
    model(label(root, "条目 1 Role"), "assistant");
    await trigger(find(root, node => node.tag === "button" && node.text === "上下文中"), "Click");
    model(label(root, "条目 1 中区深度"), 3);
    const order = label(root, "条目 1 装配顺序");
    expect(order.props.min).toBeUndefined();
    model(order, -12);
    const checks = elements(root).filter(node => node.tag === "input" && node.props.type === "checkbox");
    model(checks[0]!, false); model(checks[1]!, false);
    expect(f.resources.records.value).toEqual([original]);
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    expect(f.save.mock.calls[0]![0]).toMatchObject({
      expected_sequence: 9, record: { update_sequence: 10, value: { enabled: false, members: [{
        id: member.id, text: "Edited prompt", metadata: member.metadata,
        presentation: { role: "assistant", placement: "middle", depth: 3, order: -12, enabled: false },
      }] } },
    });
  });

  it("allows draft member addition and removal down to an empty group and normalizes non-middle depth", async () => {
    const f = fixture([]), root = await mount(CurrentPromptPanel, f);
    await trigger(label(root, "新增提示词资源"), "Click");
    const add = () => button(root, "新增条目");
    await trigger(add(), "Click");
    await trigger(find(root, node => node.tag === "button" && node.text === "上下文中"), "Click");
    model(label(root, "条目 1 中区深度"), 6);
    await trigger(find(root, node => node.tag === "button" && node.text === "上下文后"), "Click");
    expect(elements(root).some(node => node.props["aria-label"] === "条目 1 中区深度")).toBe(false);
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    expect(f.save.mock.calls[0]![0].record.value.members[0]!.presentation)
      .toMatchObject({ placement: "after", depth: null });
    await trigger(find(root, node => node.tag === "button" && node.children.some(child => child.tag === "strong")), "Click");
    await trigger(add(), "Click");
    await trigger(label(root, "移除草稿条目"), "Click");
    await trigger(label(root, "移除草稿条目"), "Click");
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    expect(f.save.mock.calls[1]![0].record.value.members).toEqual([]);
  });

  it("allows exactly 1024 members but blocks adding a 1025th draft member", async () => {
    const record = newPromptResource();
    record.value.members = Array.from({ length: 1024 }, () => newPromptMember());
    const f = fixture([record]), root = await mount(CurrentPromptPanel, f);
    await trigger(find(root, node => node.tag === "button" && node.children.some(child => child.tag === "strong")), "Click");
    const add = button(root, "新增条目");
    expect(add.props.disabled).toBe(true);
    await trigger(add, "Click");
    expect(elements(root).filter(node => node.tag === "textarea")).toHaveLength(1024);
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    expect(f.save).toHaveBeenCalledTimes(1);
    expect(f.save.mock.calls[0]![0].record.value.members).toHaveLength(1024);
  });

  it("rejects invalid presentation without a resource request", async () => {
    const f = fixture([]), root = await mount(CurrentPromptPanel, f);
    await trigger(label(root, "新增提示词资源"), "Click");
    await trigger(button(root, "新增条目"), "Click");
    model(label(root, "条目 1 装配顺序"), 1.5);
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    expect(f.save).not.toHaveBeenCalled();
    expect(elements(root).some(node => node.text.includes("呈现字段或成员身份无效"))).toBe(true);
  });

  it("freezes an unknown request independently of SDK locking and reconciles the unchanged body/key explicitly", async () => {
    const f = fixture([]), root = await mount(CurrentPromptPanel, f);
    f.locked.value = true;
    await trigger(label(root, "新增提示词资源"), "Click");
    f.state.failSave = true;
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    const submitted = graphClone(f.save.mock.calls[0]![0]);
    expect(f.resources.pending.value).toEqual(submitted);
    expect(f.resources.locked.value).toBe(true);
    expect(label(root, "新增提示词资源").props.disabled).toBe(true);
    expect(find(root, node => node.tag === "fieldset").props.disabled).toBe(true);
    expect(f.save).toHaveBeenCalledTimes(1);
    f.state.failSave = false;
    await trigger(button(root, "核实原提示词请求"), "Click");
    expect(f.save).toHaveBeenCalledTimes(1); expect(f.readReceipt.mock.calls[0]![0]).toEqual(submitted);
    expect(f.resources.pending.value).toBeNull();
    expect(f.locked.value).toBe(true);
    expect(label(root, "新增提示词资源").props.disabled).toBe(false);
    expect(elements(root).some(node => node.props["aria-label"] === "提示词当前资源表单")).toBe(false);
    await trigger(label(root, "新增提示词资源"), "Click");
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    expect(f.save.mock.calls[1]![0].expected_sequence).toBe(0);
    expect(f.save.mock.calls[1]![0].record.resource_id).not.toBe(submitted.record.resource_id);
  });

  it("locks copy and move handlers during an unknown request and preserves the original submitted body", async () => {
    const record = newPromptResource(2);
    record.value.members = [newPromptMember("One", 2), newPromptMember("Two", 2)];
    const f = fixture([record]), root = await mount(CurrentPromptPanel, f);
    const copy = label(root, `复制提示词组 ${record.scope} ${record.resource_id}`);
    await trigger(find(root, node => node.props.class === "resource-edit"), "Click");
    f.state.failSave = true;
    await trigger(label(root, "提示词当前资源表单"), "Submit");
    const pending = graphClone(f.resources.pending.value);
    expect(copy.props.disabled).toBe(true);
    expect(label(root, "条目 1 下移").props.disabled).toBe(true);
    await trigger(copy, "Click");
    await trigger(label(root, "条目 1 下移"), "Click");
    expect(label(root, "条目 1 正文").value).toBe("One");
    expect(f.resources.pending.value).toEqual(pending);
    expect(f.save).toHaveBeenCalledTimes(1);
    await trigger(button(root, "核实原提示词请求"), "Click");
    expect(f.readReceipt.mock.calls[0]![0]).toEqual(pending);
    expect(f.save).toHaveBeenCalledTimes(1);
    expect(elements(root).some(node => node.props["aria-label"] === "提示词当前资源表单")).toBe(false);
  });
});

describe("current prompt reference configuration", () => {
  it("uses the exact prompt package editor to own only reference without duplicate generic JSON fields", async () => {
    const f = fixture(), pinia = createPinia();
    const host = createWorkbenchFrontendHost(() => promptFrontendExtensions, () => f.sdk.packages.value);
    try {
      const html = await renderToString(createSSRApp(GraphNodeConfiguration,
        { node: f.node, document: f.document, definition: f.definition }).use(pinia)
        .provide(workbenchFrontendHostKey, host).provide(workflowFrontendSdkKey, f.sdk)
        .provide(promptResourceControllerKey, f.resources));
      expect(host.extensions.value.map(row => row.declaration.extension_id))
        .toEqual(["workflow.prompts.workbench-panel", "workflow.prompts.node-fields-v2"]);
      expect(host.extensions.value[1]!.configurationFields).toEqual(["reference"]);
      expect(html).toContain('aria-label="提示词引用配置"');
      expect(html).not.toContain("<textarea");
      expect(f.configureNode).not.toHaveBeenCalled();
      expect(createWorkbenchFrontendHost(() => promptFrontendExtensions, () => []).extensions.value).toEqual([]);
    } finally { pinia._s.forEach(store => store.$dispose()); }
  });

  it("keeps a missing full-identity reference rather than selecting the first record or an equal UUID in another scope", async () => {
    const other = newPromptResource(); other.scope = "shared";
    const f = fixture([other]);
    f.node.config = { reference: { ...promptIdentity(other), scope: "workspace" } };
    const original = graphClone(f.node.config);
    const html = await renderFields(f);
    expect(html).toContain("缺失引用 · workspace");
    expect(html).toContain("资源缺失或尚未读取");
    const root = await mount(PromptReferenceFields, f);
    await trigger(label(root, "提示词引用配置"), "Submit");
    expect(f.configureNode).not.toHaveBeenCalled();
    expect(f.node.config).toEqual(original);
    const select = label(root, "提示词当前资源");
    const selected = select.options[select.selectedIndex]!;
    expect(String(selected.props.value)).toContain('"workspace"');
  });

  it("keeps same-UUID resources in different scopes distinct and applies only reference with lifecycle and expected config", async () => {
    const workspace = newPromptResource(), shared = graphClone(workspace); shared.scope = "shared";
    const f = fixture([workspace, shared]), root = await mount(PromptReferenceFields, f);
    const select = label(root, "提示词当前资源");
    const value = JSON.stringify([1, shared.scope, shared.type_id, shared.resource_id]);
    const original = graphClone(f.node.config);
    model(select, value);
    await trigger(label(root, "提示词引用配置"), "Submit");
    expect(f.configureNode).toHaveBeenCalledWith({
      workflowId: f.document.workflow_definition_id, nodeId: f.node.node_binding_id,
      componentId: "prompts.global-reference", componentVersion: "2",
      expectedConfig: original, patch: { reference: promptIdentity(shared) },
      extensionId: "workflow.prompts.node-fields-v2", lifecycle: "original",
    });
    f.lifecycle.value = "replacement";
    f.node.config = { reference: promptIdentity(shared) };
    await flush();
    await trigger(label(root, "提示词引用配置"), "Submit");
    expect(f.configureNode.mock.calls[1]![0]).toMatchObject({
      lifecycle: "replacement", expectedConfig: { reference: promptIdentity(shared) },
    });
  });

  it("diagnoses disabled resources without losing the selected identity", async () => {
    const record = newPromptResource(); record.value.enabled = false;
    const f = fixture([record]), html = await renderFields(f);
    expect(html).toContain("所选提示词资源已停用");
    expect(html).toContain("（已停用）");
    expect(html).not.toContain("缺失引用");
  });

  it("selects schema-2 resources only through the explicit version-2 prompt editor", async () => {
    const old = newPromptResource(1), current = newPromptResource(2);
    const f = fixture([old, current]);
    f.node.component_version = "2";
    f.node.config = { reference: promptIdentity(current) };
    const root = await mount(PromptReferenceFields, f);
    const options = label(root, "提示词当前资源").options;
    expect(options.some(option => String(option.props.value).includes(old.resource_id))).toBe(false);
    expect(options.some(option => String(option.props.value).includes(current.resource_id))).toBe(true);
    await trigger(label(root, "提示词引用配置"), "Submit");
    expect(f.configureNode).toHaveBeenCalledWith(expect.objectContaining({
      componentVersion: "2", extensionId: "workflow.prompts.node-fields-v2",
      patch: { reference: promptIdentity(current) },
    }));
  });

  it.each(["type", "envelope", "extra config", "missing config"] as const)(
    "rejects invalid %s configuration without issuing an SDK patch", async mode => {
      const f = fixture(), identity = promptIdentity(f.resources.records.value[0]!);
      if (mode === "type") f.node.config = { reference: { ...identity, type_id: "legacy.prompt" } };
      if (mode === "envelope") f.node.config = { reference: { ...identity, envelope_version: 2 } };
      if (mode === "extra config") f.node.config = { reference: identity, unexpected: true };
      if (mode === "missing config") f.node.config = {};
      expect(await renderFields(f)).toContain("提示词引用配置无效");
      const root = await mount(PromptReferenceFields, f);
      await trigger(label(root, "提示词引用配置"), "Submit");
      expect(f.configureNode).not.toHaveBeenCalled();
      expect(label(root, "提示词当前资源").props.disabled).toBe(true);
    },
  );

  it("uses SDK locking for node mutations and does not borrow the independent resource mutation lock", async () => {
    const f = fixture(), root = await mount(PromptReferenceFields, f);
    f.resources.busy.value = true;
    await flush();
    expect(label(root, "提示词当前资源").props.disabled).toBe(false);
    await trigger(label(root, "提示词引用配置"), "Submit");
    expect(f.configureNode).toHaveBeenCalledTimes(1);
    f.locked.value = true;
    await flush();
    expect(label(root, "提示词当前资源").props.disabled).toBe(true);
    await trigger(label(root, "提示词引用配置"), "Submit");
    expect(f.configureNode).toHaveBeenCalledTimes(1);
  });
});
