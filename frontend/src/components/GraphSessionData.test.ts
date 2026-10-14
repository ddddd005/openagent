import { readFileSync } from "node:fs";
import { compileScript, compileTemplate, parse as parseSfc } from "@vue/compiler-sfc";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import * as Vue from "vue";
import { createRenderer, nextTick, type App } from "vue";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";
import GraphSessionData from "./GraphSessionData.vue";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";
import type { GraphSession } from "../domain/workflowGraph";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";

const descriptor = parseSfc(readFileSync(new URL("GraphSessionData.vue", import.meta.url), "utf8")).descriptor;
const script = compileScript(descriptor, { id: "session-data-edit-availability" });
const template = compileTemplate({
  source: descriptor.template!.content,
  filename: "GraphSessionData.vue",
  id: "session-data-edit-availability",
  compilerOptions: { mode: "function", bindingMetadata: script.bindings },
});
expect(template.errors).toEqual([]);
Object.assign(GraphSessionData, {
  render: new Function("Vue", transpileModule(template.code, {
    compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.None },
  }).outputText)(Vue),
});

class HostNode extends EventTarget {
  parent: HostNode | null = null;
  children: HostNode[] = [];
  props: Record<string, unknown> = {};
  text = "";
  value = "";
  constructor(readonly tag = "") { super(); }
  getRootNode() { return { activeElement: null }; }
  setCustomValidity(_message: string) {}
  reportValidity() {}
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
  remove(node) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    node.parent = null;
  },
  setText(node, text) { node.text = text; },
  setElementText(node, text) { node.text = text; node.children = []; },
  patchProp(node, key, _previous, value) {
    node.props[key] = value;
    if (key === "value") node.value = String(value);
  },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
});

const apps: App<HostNode>[] = [];
const instances: Pinia[] = [];

async function flush() {
  for (let index = 0; index < 8; index++) { await Promise.resolve(); await nextTick(); }
}
function elements(node: HostNode): HostNode[] {
  return [node, ...node.children.flatMap(elements)];
}
function textarea(root: HostNode) {
  const input = elements(root).find(node => node.tag === "textarea");
  expect(input).toBeDefined();
  return input!;
}
async function mount(sessionChanges: Partial<GraphSession> = {}) {
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const pinia = createPinia();
  instances.push(pinia);
  setActivePinia(pinia);
  const graph = useWorkflowGraphStore(pinia);
  const workspace = useWorkspaceStore(pinia);
  graph.createWorkflow();
  const workflowId = workspace.activeWorkflowId;
  await vi.waitFor(() => expect(graph.locked).toBe(false));
  const sessionId = crypto.randomUUID();
  const session: GraphSession = {
    schema_version: 2,
    execution_model: "graph",
    workflow_session_id: sessionId,
    workflow_definition_id: workflowId,
    definition_revision: 1,
    revision: 1,
    data_revision: 0,
    head_revision: 1,
    status: "idle",
    can_submit: true,
    nodes: [],
    chains: [],
    outputs: [],
    data: { revision: 0, values: { counter: { type: "number", value: 1 } } },
    ...sessionChanges,
  };
  graph.entries[workflowId]!.session_id = sessionId;
  graph.views[sessionId] = session;
  const writeData = vi.spyOn(graph, "writeData").mockResolvedValue(true);
  const host = createWorkbenchFrontendHost(() => graph.frontendExtensions, () => graph.packageLock, []);
  const root = new HostNode(), app = renderer.createApp(GraphSessionData);
  app.use(pinia).provide(workbenchFrontendHostKey, host)
    .provide(Vue.ssrContextKey, { modules: new Set() });
  apps.push(app);
  app.mount(root);
  await flush();
  return { root, graph, writeData };
}

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount());
  instances.splice(0).forEach(disposePinia);
  vi.restoreAllMocks();
});

describe("session data edit availability", () => {
  it("allows an idle submittable session to edit one legacy variable as JSON", async () => {
    const f = await mount();
    const input = textarea(f.root);
    expect(input.props.disabled).toBe(false);
    input.value = "7";
    (input.props.onChange as (event: Event) => void)({ target: input } as unknown as Event);
    await flush();
    expect(f.writeData).toHaveBeenCalledOnce();
    expect(f.writeData).toHaveBeenCalledWith({ counter: 7 });
  });

  it.each([
    ["not submittable", { can_submit: false }],
    ["archived", { archived: true }],
    ["read only", { readonly: true }],
  ] as const)("disables legacy variable edits when the session is %s", async (_label, state) => {
    const f = await mount(state);
    const input = textarea(f.root);
    expect(input.props.disabled).toBe(true);
    expect(f.writeData).not.toHaveBeenCalled();
  });
});
