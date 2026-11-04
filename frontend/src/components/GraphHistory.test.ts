import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as Vue from "vue";
import { createRenderer, nextTick } from "vue";
import { readFileSync } from "node:fs";
import { parse, compileScript } from "@vue/compiler-sfc";
import ts from "typescript";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import * as graphModule from "../stores/workflowGraph";
import * as graphDomain from "../domain/workflowGraph";
import * as contextMaintenanceDomain from "../domain/contextMaintenance";
import * as modelPresentationDomain from "../domain/modelPresentation";
import * as nodeCatalogDomain from "../domain/nodeCatalog";
import * as resultPresentationDomain from "../domain/resultPresentation";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { graphClone, type GraphDocument, type GraphSession } from "../domain/workflowGraph";

// Node-mode Vite compiles SFCs for SSR. Compile the saved source for the client
// renderer so reactive watchers and DOM updates actually execute between reads.
function compileClient(filename: string, additionalModules: Record<string, unknown> = {}) {
  const source = readFileSync(new URL(filename, import.meta.url), "utf8");
  const { descriptor } = parse(source);
  const client = compileScript(descriptor, { id: "history-render", inlineTemplate: true });
  const output = ts.transpileModule(client.content, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const modules: Record<string, unknown> = { vue: Vue, "../stores/workflowGraph": graphModule,
    "../domain/workflowGraph": graphDomain, "../domain/contextMaintenance": contextMaintenanceDomain,
    "../domain/modelPresentation": modelPresentationDomain, "../domain/nodeCatalog": nodeCatalogDomain,
    "../domain/resultPresentation": resultPresentationDomain, ...additionalModules };
  const exports: Record<string, any> = {};
  new Function("require", "exports", output)((name: string) => {
    if (!Object.hasOwn(modules, name)) throw new Error(`Unexpected client import ${name}`);
    return modules[name];
  }, exports);
  return exports.default;
}
const stub = { default: { render: () => Vue.h("div") } };
const GraphHistory = compileClient("./GraphHistory.vue", {
  "./GraphInformation.vue": stub, "./LorebookEvaluationFacts.vue": stub,
  "./GraphResult.vue": stub, "./GraphDiagnosticList.vue": stub,
});

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
function renderedText(root: Element): string { return all(root).map(node => node.text).join("|"); }
function click(root: Element, label: string) {
  const node = all(root).find(node => node.props["aria-label"] === label);
  if (!node) throw new Error(`Missing ${label}`);
  node.props.onClick();
}

const historyNodeId = crypto.randomUUID();
const frozenTitle = "冻结定义节点标题";
const currentTitle = "当前编辑图节点标题";
const json = (value: unknown) => new Response(JSON.stringify(value));

function frozenDocument(identity: string, revision: number): GraphDocument {
  return { schema_version: 2, workflow_definition_id: identity, revision, name: "冻结定义名称",
    nodes: [{ node_binding_id: historyNodeId, component_id: "tools.text", component_version: "1",
      title: frozenTitle, position: { x: 0, y: 0 }, config: {},
      configuration_policy: { schema_version: 1, prompt_mode: "fixed" } }],
    edges: [], object_bindings: [], package_lock: [], control_edges: [] };
}
function sessionView(definitionId: string, sessionId: string, chainId: string): GraphSession {
  return { schema_version: 2, execution_model: "graph", workflow_session_id: sessionId,
    workflow_definition_id: definitionId, definition_revision: 1, revision: 1, data_revision: 0,
    head_revision: 1, status: "succeeded", can_submit: true, nodes: [], outputs: [],
    data: { revision: 0, values: {} },
    chains: [{ chain_run_id: chainId, status: "succeeded", revision: 1, targets: [],
      completed_nodes: [], node_run_ids: [], diagnostic: null,
      workflow_definition_id: definitionId, definition_revision: 1 }] };
}
function runDetail(session: GraphSession, chain: GraphSession["chains"][number]) {
  return { chain, node_runs: [{ run_id: crypto.randomUUID(), chain_run_id: chain.chain_run_id,
    node_binding_id: historyNodeId, workflow_session_id: session.workflow_session_id,
    status: "succeeded", input_values: {}, input_refs: {}, output_refs: {}, reads: [], effects: [],
    diagnostic: null }], outputs: [] };
}

let pinia: Pinia, unmount: (() => void) | null = null;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => { unmount?.(); unmount = null; disposePinia(pinia); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

type Fixture = {
  graph: ReturnType<typeof useWorkflowGraphStore>; id: string; sid: string; chainId: string;
  session: GraphSession; root: Element; requests: string[]; setDefinitionStatus: (status: number) => void;
};
async function mountHistory(initialDefinitionStatus = 200): Promise<Fixture> {
  const graph = useWorkflowGraphStore();
  const id = await graph.createWorkflow();
  const sid = crypto.randomUUID(), chainId = crypto.randomUUID();
  const session = sessionView(id, sid, chainId);
  graph.entries[id]!.saved_revision = 1;
  graph.entries[id]!.saved_document = graphClone(frozenDocument(id, 1));
  graph.entries[id]!.session_id = sid;
  graph.views[sid] = session;
  // The current edit graph deliberately carries the same node identity with another
  // title: a failed frozen read must never fall back to it.
  graph.entries[id]!.document.nodes = [{ node_binding_id: historyNodeId, component_id: "tools.text",
    component_version: "1", title: currentTitle, position: { x: 0, y: 0 }, config: {},
    configuration_policy: { schema_version: 1, prompt_mode: "fixed" } }];
  let definitionStatus = initialDefinitionStatus;
  const requests: string[] = [];
  stubGraphApplicationFetch(vi.fn(async (path: string) => {
    requests.push(path);
    if (path.includes("/runs/")) {
      const view = graph.views[sid]!;
      return json(runDetail(view, view.chains[0]!));
    }
    if (path.includes("/revisions/")) {
      if (definitionStatus !== 200)
        return new Response(JSON.stringify({ error: { code: "internal_error", message: "history definition unavailable" } }),
          { status: definitionStatus });
      const revision = Number(path.split("/revisions/").at(-1));
      return json(frozenDocument(id, revision));
    }
    throw new Error(`unexpected fixture path ${path}`);
  }));
  const root = element("root"), app = renderer.createApp(GraphHistory);
  app.use(pinia); app.mount(root); unmount = () => app.unmount();
  return { graph, id, sid, chainId, session, root, requests,
    setDefinitionStatus: (status: number) => { definitionStatus = status; } };
}
const runReads = (fixture: Fixture) => fixture.requests.filter(path => path.includes("/runs/")).length;
const revisionReads = (fixture: Fixture) => fixture.requests.filter(path => path.includes("/revisions/"));

describe("history frozen definition reuse (Q09)", () => {
  it("reuses the frozen definition when only the chain revision advances", async () => {
    const fixture = await mountHistory();
    await vi.waitFor(() => expect(revisionReads(fixture)).toHaveLength(1));
    await vi.waitFor(() => expect(renderedText(fixture.root)).toContain(frozenTitle));
    expect(runReads(fixture)).toBe(1);
    // run.read still follows the selected chain's revision...
    fixture.graph.views[fixture.sid]!.chains[0]!.revision = 2;
    await vi.waitFor(() => expect(runReads(fixture)).toBe(2));
    await nextTick();
    // ...but the same formal (definition id, revision) is not downloaded again.
    expect(revisionReads(fixture)).toHaveLength(1);
    expect(renderedText(fixture.root)).toContain(frozenTitle);
    expect(renderedText(fixture.root)).not.toContain(currentTitle);
  });

  it("reads a different frozen revision when the formal identity changes", async () => {
    const fixture = await mountHistory();
    await vi.waitFor(() => expect(revisionReads(fixture)).toHaveLength(1));
    expect(revisionReads(fixture)[0]!.endsWith("/revisions/1")).toBe(true);
    fixture.graph.views[fixture.sid]!.chains[0]!.definition_revision = 2;
    fixture.graph.views[fixture.sid]!.chains[0]!.revision = 3;
    await vi.waitFor(() => expect(revisionReads(fixture)).toHaveLength(2));
    expect(revisionReads(fixture)[1]!.endsWith("/revisions/2")).toBe(true);
    expect(renderedText(fixture.root)).toContain(frozenTitle);
  });

  it("shows a missing frozen definition instead of the current edit graph and retries", async () => {
    const fixture = await mountHistory(500);
    await vi.waitFor(() => expect(renderedText(fixture.root)).toContain("未使用当前编辑图替代"));
    expect(renderedText(fixture.root)).toContain("历史节点（定义未读取）");
    expect(renderedText(fixture.root)).not.toContain(currentTitle);
    expect(revisionReads(fixture)).toHaveLength(1);
    // A failed read is never cached: the explicit retry issues a real new read.
    fixture.setDefinitionStatus(200);
    click(fixture.root, "重试读取历史定义");
    await vi.waitFor(() => expect(renderedText(fixture.root)).toContain(frozenTitle));
    expect(renderedText(fixture.root)).not.toContain("历史节点（定义未读取）");
    expect(renderedText(fixture.root)).not.toContain("未使用当前编辑图替代");
    expect(revisionReads(fixture)).toHaveLength(2);
  });
});
