import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse as parseSfc } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { createRenderer, nextTick, type App } from "vue";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";
import GraphSessions from "./GraphSessions.vue";
import { createSessionMetadata, sessionMetadataKey } from "../application/sessionMetadata";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone, type GraphSession } from "../domain/workflowGraph";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import type { SessionMetadata, SessionMetadataSaveRequest } from "../adapters/sessionMetadataApi";

class HostNode extends EventTarget {
  parent: HostNode | null = null; children: HostNode[] = [];
  props: Record<string, unknown> = {}; text = ""; value = "";
  constructor(readonly tag = "") { super(); }
  getRootNode() { return { activeElement: null }; }
  get tagName() { return this.tag.toUpperCase(); }
}
const renderer = createRenderer<HostNode, HostNode>({
  createElement: tag => new HostNode(tag),
  createText: text => Object.assign(new HostNode(), { text }),
  createComment: text => Object.assign(new HostNode(), { text }),
  insert(node, parent, anchor = null) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    node.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    if (index < 0) parent.children.push(node); else parent.children.splice(index, 0, node);
  },
  remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null; },
  setText(node, text) { node.text = text; },
  setElementText(node, text) { node.text = text; node.children = []; },
  patchProp(node, key, _previous, value) { node.props[key] = value; if (key === "value") node.value = String(value); },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
});
const apps: App<HostNode>[] = [], instances: Pinia[] = [];
async function flush() { for (let index = 0; index < 12; index++) { await Promise.resolve(); await nextTick(); } }
function elements(node: HostNode): HostNode[] { return [node, ...node.children.flatMap(elements)]; }
function label(root: HostNode, name: string) {
  const node = elements(root).find(node => node.props["aria-label"] === name);
  expect(node).toBeDefined(); return node!;
}
async function mount() {
  vi.stubGlobal("window", { addEventListener() {}, removeEventListener() {}, confirm: () => true });
  vi.stubGlobal("document", { activeElement: null });
  vi.stubGlobal("Document", class {}); vi.stubGlobal("ShadowRoot", class {});
  const pinia = createPinia(); instances.push(pinia); setActivePinia(pinia);
  const graph = useWorkflowGraphStore(pinia), workspace = useWorkspaceStore(pinia);
  graph.setPersistenceGuard(() => true);
  const id = graph.createWorkflow(), sid = crypto.randomUUID();
  const view: GraphSession = { schema_version: 2, execution_model: "graph", workflow_session_id: sid,
    workflow_definition_id: id, definition_revision: 1, name: "Original", metadata_sequence: 0,
    revision: 5, data_revision: 0, head_revision: 2, status: "running", can_submit: false,
    nodes: [], chains: [], outputs: [], data: { revision: 0, values: {} }, archived: false };
  graph.entries[id].session_id = sid; graph.views[sid] = view; graph.sessions[id] = [view];
  let saved: SessionMetadata = { schema_version: 1, kind: "workflow.session-metadata",
    workflow_definition_id: id, workflow_session_id: sid, name: "Original", metadata_sequence: 0 };
  let pending: string | null = null, unknown = false;
  const save = vi.fn(async (request: SessionMetadataSaveRequest) => {
    saved = { ...saved, name: request.name.trim(), metadata_sequence: request.expected_metadata_sequence + 1 };
    if (unknown) throw new WorkbenchApiError("unknown", "Controlled response loss");
    return graphClone(saved);
  });
  const readReceipt = vi.fn(async (_request: SessionMetadataSaveRequest) => graphClone(saved));
  const metadata = createSessionMetadata(() => ({
    read: async () => graphClone(saved), save, readReceipt, readPending: () => pending,
    writePending: value => { pending = value; },
  }));
  const descriptor = parseSfc(readFileSync(new URL("GraphSessions.vue", import.meta.url), "utf8")).descriptor;
  const script = compileScript(descriptor, { id: "session-name-interactions" });
  const template = compileTemplate({ source: descriptor.template!.content, filename: "GraphSessions.vue",
    id: "session-name-interactions", compilerOptions: { mode: "function", bindingMetadata: script.bindings } });
  expect(template.errors).toEqual([]);
  Object.assign(GraphSessions, { render: new Function("Vue", transpileModule(template.code, {
    compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None },
  }).outputText)(Vue) });
  const root = new HostNode(), app = renderer.createApp(GraphSessions);
  app.use(pinia).provide(sessionMetadataKey, metadata).provide(Vue.ssrContextKey, { modules: new Set() });
  apps.push(app); app.mount(root); await flush();
  return { root, graph, workspace, id, sid, metadata, save, readReceipt,
    set unknown(value: boolean) { unknown = value; } };
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); instances.splice(0).forEach(disposePinia); vi.unstubAllGlobals(); });
describe("session formal name editing", () => {
  it("saves on blur while execution is locked and leaves runtime revisions untouched", async () => {
    const f = await mount(); f.graph.busy = "run"; await flush();
    const before = graphClone(f.graph.session), input = label(f.root, "会话名称");
    expect(f.graph.locked).toBe(true); expect(input.props.disabled).toBe(false);
    (input.props["onUpdate:modelValue"] as (text: string) => void)("Renamed");
    expect(f.save).not.toHaveBeenCalled();
    await (input.props.onBlur as () => Promise<void>)(); await flush();
    expect(f.save.mock.calls[0]![0]).toMatchObject({ session_id: f.sid, name: "Renamed", expected_metadata_sequence: 0 });
    expect(f.graph.session).toEqual(before);
    expect(elements(f.root).some(node => node.text === "Renamed")).toBe(true);
  });
  it("keeps ordinary typing through unchanged runtime observations without label drafts", async () => {
    const f = await mount(), input = label(f.root, "会话名称");
    (input.props["onUpdate:modelValue"] as (text: string) => void)("Typing"); await flush();
    f.graph.views[f.sid] = { ...f.graph.views[f.sid]!, revision: 6 };
    f.graph.sessions[f.id] = [f.graph.views[f.sid]!]; await flush();
    expect(label(f.root, "会话名称").value).toBe("Typing");
    expect(f.save).not.toHaveBeenCalled();
    const source = readFileSync(new URL("GraphSessions.vue", import.meta.url), "utf8");
    expect(source).not.toContain("ManagementLabelFields");
    expect(source).not.toContain("useManagementLabelsStore");
  });
  it("makes archived session and workflow names read-only", async () => {
    const f = await mount();
    f.graph.views[f.sid]!.archived = true; await flush();
    expect(label(f.root, "会话名称").props.disabled).toBe(true);
    await (label(f.root, "会话名称").props.onBlur as () => Promise<void>)();
    expect(f.save).not.toHaveBeenCalled();
    f.graph.views[f.sid]!.archived = false; f.workspace.activeWorkflow.archived = true; await flush();
    expect(label(f.root, "会话名称").props.disabled).toBe(true);
  });
  it("locks an unknown name request and explicitly reads its receipt without another save", async () => {
    const f = await mount(); f.unknown = true;
    const input = label(f.root, "会话名称");
    (input.props["onUpdate:modelValue"] as (text: string) => void)("Unknown");
    await (input.props.onBlur as () => Promise<void>)(); await flush();
    const original = graphClone(f.metadata.forSession(f.sid).pending.value);
    expect(label(f.root, "会话名称").props.disabled).toBe(true);
    await (label(f.root, "核实原会话名称请求").props.onClick as () => Promise<void>)(); await flush();
    expect(f.readReceipt.mock.calls[0]![0]).toEqual(original);
    expect(f.save).toHaveBeenCalledTimes(1);
    expect(label(f.root, "会话名称").value).toBe("Unknown");
  });
});
