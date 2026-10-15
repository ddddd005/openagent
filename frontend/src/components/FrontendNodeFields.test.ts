import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { computed, createRenderer, createSSRApp, h, nextTick, ref, type App } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";
import FrontendNodeFields from "./FrontendNodeFields.vue";
import GraphNodeConfiguration from "./GraphNodeConfiguration.vue";
import { frontendNodeFixture } from "../testUtils/frontendNodeFixture";
import { frontendSession } from "../testUtils/frontendFixture";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";
import { workflowFrontendSdkKey, type WorkflowFrontendSdk } from "../plugins/workflowFrontendSdk";
import { frontendChatNodeFieldsExtension, frontendNodeFieldsExtension, workflowFrontendExtensions } from "../plugins/workflowFrontendManifest";
import { graphClone } from "../domain/workflowGraph";
import type { GraphConfigurationResult, GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { useWorkflowGraphStore } from "../stores/workflowGraph";

class HostNode {
  parent: HostNode | null = null;
  children: HostNode[] = [];
  props: Record<string, unknown> = {};
  text = "";
  constructor(readonly tag = "") {}
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
  remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); },
  setText: (node, text) => { node.text = text; },
  setElementText: (node, text) => { node.text = text; node.children = []; },
  patchProp: (node, key, _previous, value) => { node.props[key] = value; },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
});
const apps: App<HostNode>[] = [], instances: Pinia[] = [];
const elements = (root: HostNode): HostNode[] => [root, ...root.children.flatMap(elements)];
function control(root: HostNode, label: string) {
  const value = elements(root).find(node => node.props["aria-label"] === label);
  expect(value).toBeDefined(); return value!;
}
async function flush() { for (let count = 0; count < 10; count++) { await Promise.resolve(); await nextTick(); } }
async function trigger(node: HostNode, event: string, value?: string) {
  await (node.props[`on${event}`] as (event: unknown) => unknown)({ target: { value } });
  await flush();
}
function installRender() {
  const filename = "FrontendNodeFields.vue", { descriptor } = parse(
    readFileSync(new URL(filename, import.meta.url), "utf8"));
  const script = compileScript(descriptor, { id: "frontend-node-fields" });
  const template = compileTemplate({ source: descriptor.template!.content, filename, id: "frontend-node-fields",
    compilerOptions: { mode: "function", bindingMetadata: script.bindings } });
  const code = transpileModule(template.code, {
    compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None },
  }).outputText;
  Object.assign(FrontendNodeFields, { render: new Function("Vue", code)(Vue) });
}
async function fixture(version = "1") {
  vi.stubGlobal("window", { open: vi.fn(), confirm: () => false, addEventListener() {}, removeEventListener() {} });
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  const pinia = createPinia(); instances.push(pinia); setActivePinia(pinia);
  const graph = useWorkflowGraphStore(), sample = frontendNodeFixture(version);
  graph.catalog = graphClone(sample.catalog);
  graph.packageLock = [{ package_id: "workflow.frontend", version: "1.0.0" }];
  graph.frontendExtensions = graphClone(workflowFrontendExtensions);
  const id = graph.createWorkflow(), document = { ...sample.document, workflow_definition_id: id };
  Object.assign(graph.entries[id]!, { document, saved_document: graphClone(document), saved_revision: 1 });
  const host = createWorkbenchFrontendHost(() => graph.frontendExtensions, () => graph.packageLock);
  const requests: GraphConfigurationUpdateRequest[] = [];
  const update = vi.fn(async (request: GraphConfigurationUpdateRequest): Promise<GraphConfigurationResult> => {
    requests.push(graphClone(request));
    const doc = { ...graphClone(request.document), revision: request.document.revision + 1,
      edit_sequence: request.expected_edit_sequence + 1 };
    return { schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
      source_workflow_definition_id: id, workflow_definition_id: id,
      definition_revision: doc.revision, edit_sequence: doc.edit_sequence,
      source_has_effective_session: false, changed_categories: ["fixed"], session_id: null, document: doc,
      node_id_map: Object.fromEntries(doc.nodes.map(node => [node.node_binding_id, node.node_binding_id])) };
  });
  graph.installWorkflowConfigurationApplication(createWorkflowConfigurationApplication({
    evidence: evidence.ports, extensions: () => host.extensions.value, update,
  }));
  const configureNode = vi.fn((request: Parameters<typeof graph.patchNodeConfiguration>[0]) => graph.patchNodeConfiguration(request));
  const sdk: WorkflowFrontendSdk = {
    workflowId: computed(() => id), document: computed(() => graph.document),
    catalog: computed(() => graph.catalog), packages: computed(() => graph.packageLock),
    session: computed(() => graph.session), locked: computed(() => graph.editorLocked),
    lifecycle: computed(() => JSON.stringify([id, graph.session?.workflow_session_id])),
    configureNode, readArtifact: vi.fn(),
  };
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  const selectedId = ref(sample.nodes[0]!.node_binding_id);
  const node = computed(() => graph.document!.nodes.find(item => item.node_binding_id === selectedId.value)!);
  return { graph, sample, id, node, host, sdk, configureNode, update, requests, selectedId, pinia };
}
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount()); instances.splice(0).forEach(disposePinia);
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});
describe("whole frontend node workbench fields", () => {
  it("version 2 uses its own exact extension and preserves its logical chat registration", async () => {
    const f = await fixture("2");
    installRender();
    const root = new HostNode("root");
    const app = renderer.createApp({ setup: () => () => h(FrontendNodeFields, {
      node: f.node.value, document: f.graph.document!, catalog: f.graph.catalog,
    }) }).use(f.pinia).provide(workflowFrontendSdkKey, f.sdk)
      .provide(Vue.ssrContextKey, { modules: new Set() });
    apps.push(app); app.mount(root); await flush();
    await trigger(control(root, "前端节点输入绑定"), "Change", f.sample.inputs[1]!.node_binding_id);
    await vi.waitFor(() => expect(f.requests).toHaveLength(1));
    expect(f.configureNode).toHaveBeenCalledWith(expect.objectContaining({
      componentId: "frontend.node", componentVersion: "2",
      extensionId: frontendChatNodeFieldsExtension.extension_id,
    }));
    expect(f.graph.document!.object_bindings!.every(binding => binding.type_id === "workflow.chat-context")).toBe(true);
    expect(f.node.value.component_version).toBe("2");
  });
  it("owns only the precise input selector and leaves role and registered item fields in the normal editor", async () => {
    const f = await fixture();
    const html = await renderToString(createSSRApp(GraphNodeConfiguration, {
      node: f.node.value, document: f.graph.document!, definition: f.graph.catalog[1]!, catalog: f.graph.catalog,
    }).use(f.pinia).provide(workbenchFrontendHostKey, f.host).provide(workflowFrontendSdkKey, f.sdk));
    expect(html).toContain('aria-label="前端节点输入绑定"');
    expect(html).toContain("未绑定输入");
    for (const input of f.sample.inputs) expect(html).toContain(`<option value="${input.node_binding_id}"`);
    expect(html).not.toContain(`<option value="${f.sample.nodes[1]!.node_binding_id}"`);
    expect(html).toContain("role"); expect(html).toContain('class="object-access-field"');
    expect(html).not.toContain(">input_node_id<");
    expect(f.host.extensions.value.find(row => row.declaration.extension_id === frontendNodeFieldsExtension.extension_id)!
      .configurationFields).toEqual(["input_node_id"]);
    expect(f.update).not.toHaveBeenCalled();
  });
  it("formally saves the clicked node input binding through its existing SDK without editing another node", async () => {
    const f = await fixture();
    installRender();
    const root = new HostNode("root");
    const app = renderer.createApp({ setup: () => () => h(FrontendNodeFields, {
      node: f.node.value, document: f.graph.document!, catalog: f.graph.catalog,
    }) }).use(f.pinia).provide(workflowFrontendSdkKey, f.sdk)
      .provide(Vue.ssrContextKey, { modules: new Set() });
    apps.push(app); app.mount(root); await flush();
    const beforeOther = graphClone(f.graph.document!.nodes.find(node => node.node_binding_id === f.sample.nodes[1]!.node_binding_id)!);
    await trigger(control(root, "前端节点输入绑定"), "Change", f.sample.inputs[1]!.node_binding_id);
    await vi.waitFor(() => expect(f.requests).toHaveLength(1));
    expect(f.configureNode).toHaveBeenCalledWith(expect.objectContaining({
      workflowId: f.id, nodeId: f.sample.nodes[0]!.node_binding_id,
      componentId: "frontend.node", componentVersion: "1",
      extensionId: frontendNodeFieldsExtension.extension_id,
      patch: { input_node_id: f.sample.inputs[1]!.node_binding_id },
    }));
    expect(f.graph.entries[f.id]!.saved_revision).toBe(2);
    expect(f.node.value.config.input_node_id).toBe(f.sample.inputs[1]!.node_binding_id);
    expect(f.graph.document!.nodes.find(node => node.node_binding_id === beforeOther.node_binding_id)).toEqual(beforeOther);
  });
  it("keeps the open action enabled while runtime locks configuration editing, and never opens before formal save", async () => {
    const f = await fixture(), session = frontendSession(f.id);
    session.status = "running"; session.can_submit = false;
    f.graph.entries[f.id]!.session_id = session.workflow_session_id;
    f.graph.views[session.workflow_session_id] = session;
    const render = () => renderToString(createSSRApp(FrontendNodeFields, {
      node: f.node.value, document: f.graph.document!, catalog: f.graph.catalog, disabled: true,
    }).use(f.pinia).provide(workflowFrontendSdkKey, f.sdk));
    expect(await render()).toMatch(/<button type="button"(?![^>]*disabled)[^>]*aria-label="打开前端节点"/);
    expect(await render()).toMatch(/<select[^>]*disabled[^>]*aria-label="前端节点输入绑定"/);
    f.graph.entries[f.id]!.saved_revision = 0;
    expect(await render()).toContain("正式保存前端节点后打开");
    expect(await render()).toMatch(/<button type="button" disabled[^>]*aria-label="打开前端节点"/);
  });
  it("opens the selected node by one read-only entry query without session creation or selection changes", async () => {
    const f = await fixture();
    f.selectedId.value = f.sample.nodes[1]!.node_binding_id;
    const popup = { opener: {}, location: { href: "about:blank" }, close: vi.fn() };
    vi.mocked(window.open).mockReturnValue(popup as unknown as Window);
    const path = `${f.id}/1/${f.selectedId.value}/none`, owner = {
      workflow_definition_id: f.id, definition_revision: 1, node_binding_id: f.selectedId.value, session_id: null,
    };
    const entry = { schema_version: 1, kind: "workflow.frontend-node-entry", owner,
      page_url: `http://127.0.0.1:8765/frontend-node/${path}/`,
      channel_url: `http://127.0.0.1:8765/api/graph/frontend-nodes/${path}/channel`,
      events_url: `http://127.0.0.1:8765/api/graph/frontend-nodes/${path}/events` };
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(entry))); vi.stubGlobal("fetch", fetcher);
    installRender();
    const root = new HostNode("root");
    const app = renderer.createApp({ setup: () => () => h(FrontendNodeFields, {
      node: f.node.value, document: f.graph.document!, catalog: f.graph.catalog,
    }) }).use(f.pinia).provide(workflowFrontendSdkKey, f.sdk)
      .provide(Vue.ssrContextKey, { modules: new Set() });
    apps.push(app); app.mount(root); await flush();
    const before = graphClone(f.graph.entries[f.id]);
    await trigger(control(root, "打开前端节点"), "Click");
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(`/api/graph/frontend-nodes/${path}/entry`,
      expect.objectContaining({ method: "GET" }));
    expect(popup.location.href).toBe(entry.page_url); expect(popup.opener).toBeNull();
    expect(f.graph.entries[f.id]).toEqual(before);
    expect(f.update).not.toHaveBeenCalled(); expect(f.configureNode).not.toHaveBeenCalled();
  });
});
