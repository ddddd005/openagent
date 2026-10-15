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
  const host = createWorkbenchFrontendHost(() => graph.frontendExtensions, () => graph.packageLock, []);
  const root = new HostNode(), app = renderer.createApp(GraphSessionData);
  app.use(pinia).provide(workbenchFrontendHostKey, host)
    .provide(Vue.ssrContextKey, { modules: new Set() });
  apps.push(app);
  app.mount(root);
  await flush();
  return { root };
}

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount());
  instances.splice(0).forEach(disposePinia);
  vi.restoreAllMocks();
});

describe("session object presentation", () => {
  it("shows authoritative object details without exposing legacy variable values or edits", async () => {
    const f = await mount({
      data: { revision: 0, values: { old: { type: "string", value: "legacy-only" } } },
      objects: { current: { revision_id: crypto.randomUUID(), revision: 1, type_id: "plugin.state",
        schema_version: 1, deleted: false, value: "session-current",
        binding: { object_key: "current", type_id: "plugin.state", schema_version: 1,
          scope: "shared", owner_node_id: null, readers: [], writers: [] } } },
    });
    const rendered = elements(f.root).map(node => node.text).join(" ");
    expect(rendered).toContain("会话对象");
    expect(rendered).toContain("session-current");
    expect(rendered).not.toContain("legacy-only");
    expect(rendered).not.toContain("旧变量");
    expect(elements(f.root).some(node => node.tag === "textarea")).toBe(false);
  });
});
