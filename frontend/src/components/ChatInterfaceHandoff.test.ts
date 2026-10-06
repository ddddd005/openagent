import { readFileSync } from "node:fs";
import { compileScript, parse as parseSfc } from "@vue/compiler-sfc";
import { parse as parseHtml } from "@vue/compiler-dom";
import type { ElementNode, TemplateChildNode } from "@vue/compiler-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSSRApp } from "vue";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import App from "../App.vue";
import { DEFAULT_LEGACY_UI_URL } from "../adapters/legacyUi";
import type { ExactPromptSelection } from "../adapters/workbenchSessions";
import { graphClone } from "../domain/workflowGraph";
import { exposureSignature } from "../domain/exposureConfiguration";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { legacyWorkbenchRaw } from "../testUtils/legacyWorkbenchStorage";
import { useWorkspaceStore } from "../stores/workspace";
import { useWorkbenchRuntimeStore } from "../stores/workbenchRuntime";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useExposuresStore } from "../stores/exposures";
import { useModelConfigurationStore } from "../stores/modelConfiguration";

vi.mock("@vue-flow/core", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" }, Position: { Left: "left", Right: "right" },
  Handle: { render: () => null }, VueFlow: { render: () => null },
}));
vi.mock("@vue-flow/background", () => ({ Background: { render: () => null } }));

const definitionId = "00000000-0000-4000-8000-000000000021";
const sessionId = "00000000-0000-4000-8000-000000000022";
const priorWorkflowId = "00000000-0000-4000-8000-000000000023";
const priorSessionId = "00000000-0000-4000-8000-000000000024";
const promptSelection: ExactPromptSelection = {
  schema_version: 1, kind: "workflow_prompt_selection", nodes: {
    A: { config_id: "00000000-0000-4000-8000-000000000025", revision: 2 },
    B: { config_id: "00000000-0000-4000-8000-000000000026", revision: 3 },
  },
};
const modelSelection = { schema_version: 1 as const, kind: "workflow_model_selection" as const,
  config_id: "00000000-0000-4000-8000-000000000027", revision: 4 };
let pinia: Pinia;
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia); vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn());
});
afterEach(() => {
  disposePinia(pinia); vi.clearAllTimers(); vi.useRealTimers();
  vi.unstubAllGlobals(); vi.restoreAllMocks();
});
function binding(id: string | null) {
  return { sessionId: id, view: null, pending: null, busy: null, requiresRefresh: false,
    promptSelection: null, modelSelection: null };
}
function elements(nodes: readonly TemplateChildNode[]): ElementNode[] {
  return nodes.flatMap(node => node.type === 1 ? [node, ...elements(node.children)] : []);
}
function chatAnchor(html: string) {
  const anchors = elements(parseHtml(html).children).filter(node => node.tag === "a"
    && node.props.some(prop => prop.type === 6 && prop.name === "aria-label" && prop.value?.content === "打开聊天前端"));
  expect(anchors).toHaveLength(1);
  return Object.fromEntries(anchors[0]!.props.flatMap(prop => prop.type === 6
    ? [[prop.name, prop.value?.content ?? ""]] : []));
}
function fixture(legacy = false) {
  let raw = legacy ? legacyWorkbenchRaw() : null;
  const storage = { getItem: vi.fn(() => raw), setItem: vi.fn((_key: string, value: string) => { raw = value; }) };
  const persistence = useWorkbenchPersistenceStore(); persistence.initialize(storage);
  const workspace = useWorkspaceStore(), graph = useWorkflowGraphStore(), runtime = useWorkbenchRuntimeStore();
  vi.spyOn(graph, "activate").mockResolvedValue();
  vi.spyOn(runtime, "activateWorkflow").mockResolvedValue();
  runtime.workflowId = priorWorkflowId;
  runtime.runtimes[priorWorkflowId] = binding(priorSessionId);
  const creation = [
    vi.spyOn(graph, "createSession"), vi.spyOn(graph, "selectSession"), vi.spyOn(graph, "submitPrimary"),
    vi.spyOn(runtime, "createSession"), vi.spyOn(runtime, "selectSession"), vi.spyOn(runtime, "submitPrimary"),
  ];
  const render = async () => chatAnchor(await renderToString(createSSRApp(App).use(pinia)));
  const expectPassive = () => {
    expect(fetch).not.toHaveBeenCalled();
    creation.forEach(spy => expect(spy).not.toHaveBeenCalled());
  };
  return { workspace, graph, runtime, persistence, storage, render, expectPassive };
}
function savedGraph(f: ReturnType<typeof fixture>, session: string | null = null) {
  const entry = f.graph.active!;
  entry.document.workflow_definition_id = definitionId;
  entry.document.revision = 2; entry.saved_revision = 2; entry.session_id = session;
  return entry;
}

describe("actual App chat interface handoff", () => {
  it("disables a fresh unsaved ordinary graph without creating or borrowing a legacy session", async () => {
    const f = fixture(), before = graphClone(f.graph.storeSnapshot());
    const anchor = await f.render();
    expect(anchor.href).toBeUndefined(); expect(anchor["aria-disabled"]).toBe("true");
    expect(anchor.title).toBe("保存工作流后打开聊天前端");
    expect(f.graph.storeSnapshot()).toEqual(before);
    expect(f.runtime.runtimes[f.workspace.activeWorkflowId]).toBeUndefined();
    f.expectPassive();
  });

  it("opens the saved graph definition without a session and leaves locks, bindings and outboxes untouched", async () => {
    const f = fixture(); savedGraph(f);
    const models = useModelConfigurationStore(), exposures = useExposuresStore();
    const graphBefore = graphClone(f.graph.storeSnapshot()), runtimeBefore = f.runtime.exportRuntimeSnapshot();
    const idsBefore = [f.workspace.activeWorkflowId, f.workspace.selectedWorkflowId, f.runtime.workflowId];
    const locksBefore = [f.graph.locked, f.runtime.locked, models.locked, exposures.locked];
    const writesBefore = f.storage.setItem.mock.calls.length;
    const anchor = await f.render(), url = new URL(anchor.href!);
    expect(anchor["aria-disabled"]).toBe("false"); expect(anchor.title).toBe("聊天前端");
    expect(url.origin).toBe(new URL(DEFAULT_LEGACY_UI_URL).origin);
    expect([...url.searchParams]).toEqual([["graph_workflow", definitionId]]);
    expect(f.graph.storeSnapshot()).toEqual(graphBefore);
    expect(f.runtime.exportRuntimeSnapshot()).toEqual(runtimeBefore);
    expect([f.workspace.activeWorkflowId, f.workspace.selectedWorkflowId, f.runtime.workflowId]).toEqual(idsBefore);
    expect([f.graph.locked, f.runtime.locked, models.locked, exposures.locked]).toEqual(locksBefore);
    expect(models.pending).toBeNull(); expect(exposures.pending).toBeNull();
    expect(f.storage.setItem).toHaveBeenCalledTimes(writesBefore);
    f.expectPassive();
  });

  it("uses the document definition identity and exact graph session rather than the workspace alias or global legacy row", async () => {
    const f = fixture(); savedGraph(f, sessionId);
    expect(f.workspace.activeWorkflowId).not.toBe(definitionId);
    const anchor = await f.render(), url = new URL(anchor.href!);
    expect([...url.searchParams]).toEqual([["graph_workflow", definitionId], ["graph_session", sessionId]]);
    expect(f.runtime.sessionId).toBe(priorSessionId);
    f.expectPassive();
  });

  it("keeps the original pending graph request and disables the link until that request is reconciled", async () => {
    const f = fixture(), entry = savedGraph(f, sessionId);
    entry.pending = { action: "save", path: "/api/graph/definitions", body: {
      document: graphClone(entry.document), expected_revision: 1,
      idempotency_key: "00000000-0000-4000-8000-000000000028",
    } };
    const pending = entry.pending, original = graphClone(pending);
    const anchor = await f.render();
    expect(anchor.href).toBeUndefined(); expect(anchor["aria-disabled"]).toBe("true");
    expect(anchor.title).toBe("核实原请求后打开聊天前端");
    expect(entry.pending).toBe(pending); expect(entry.pending).toEqual(original);
    expect(f.graph.locked).toBe(true);
    f.expectPassive();
  });

  it.each(["definition", "session"] as const)("disables an invalid graph %s identity instead of opening an anonymous page", async field => {
    const f = fixture(), entry = savedGraph(f, sessionId);
    if (field === "definition") entry.document.workflow_definition_id = "invalid";
    else entry.session_id = "invalid";
    const anchor = await f.render();
    expect(anchor.href).toBeUndefined(); expect(anchor["aria-disabled"]).toBe("true");
    expect(anchor.title).toBe("聊天入口地址或工作流身份不可用");
    expect(f.runtime.sessionId).toBe(priorSessionId);
    f.expectPassive();
  });

  it.each(["missing", "null"] as const)("never borrows a previous runtime session for a %s active legacy binding", async mode => {
    const f = fixture(true);
    if (mode === "null") f.runtime.runtimes[MAIN_WORKFLOW_ID] = binding(null);
    const runtimeBefore = f.runtime.exportRuntimeSnapshot();
    const anchor = await f.render();
    expect(anchor.href).toBeUndefined(); expect(anchor["aria-disabled"]).toBe("true");
    expect(anchor.title).toBe("选择已有会话后打开聊天前端");
    expect(f.runtime.exportRuntimeSnapshot()).toEqual(runtimeBefore);
    expect(f.runtime.workflowId).toBe(priorWorkflowId); expect(f.runtime.sessionId).toBe(priorSessionId);
    f.expectPassive();
  });

  it("uses only the active legacy workflow's exact session, prompt, model and exposure references", async () => {
    const f = fixture(true);
    f.runtime.runtimes[MAIN_WORKFLOW_ID] = { ...binding(sessionId), promptSelection, modelSelection };
    const exposures = useExposuresStore();
    const reference = { config_id: "00000000-0000-4000-8000-000000000029", revision: 5 };
    exposures.publications = [{ ...reference, workflowId: MAIN_WORKFLOW_ID, signature: exposureSignature([]) }];
    const anchor = await f.render(), url = new URL(anchor.href!);
    expect(anchor["aria-disabled"]).toBe("false"); expect(anchor.title).toBe("聊天前端");
    expect([...url.searchParams]).toEqual([
      ["session", sessionId], ["exposure_config", reference.config_id], ["exposure_revision", "5"],
      ["model_config", modelSelection.config_id], ["model_revision", "4"],
      ["prompt_a", promptSelection.nodes.A.config_id], ["prompt_a_revision", "2"],
      ["prompt_b", promptSelection.nodes.B.config_id], ["prompt_b_revision", "3"],
    ]);
    expect(url.href).not.toContain(priorSessionId);
    expect(f.runtime.workflowId).toBe(priorWorkflowId);
    expect(f.runtime.runtimes[MAIN_WORKFLOW_ID]!.promptSelection).toEqual(promptSelection);
    expect(f.runtime.runtimes[MAIN_WORKFLOW_ID]!.modelSelection).toEqual(modelSelection);
    f.expectPassive();
  });

  it("opens a known legacy session without requiring a confirmed prompt selection", async () => {
    const f = fixture(true); f.runtime.runtimes[MAIN_WORKFLOW_ID] = binding(sessionId);
    const anchor = await f.render();
    expect(anchor["aria-disabled"]).toBe("false");
    expect([...new URL(anchor.href!).searchParams]).toEqual([["session", sessionId]]);
    f.expectPassive();
  });

  it("disables an invalid active legacy identity even when another runtime row is valid", async () => {
    const f = fixture(true); f.runtime.runtimes[MAIN_WORKFLOW_ID] = binding("invalid");
    const anchor = await f.render();
    expect(anchor.href).toBeUndefined(); expect(anchor["aria-disabled"]).toBe("true");
    expect(anchor.title).toBe("聊天入口地址或会话身份不可用");
    f.expectPassive();
  });
});

describe("missing-document handoff guard", () => {
  it("returns null for a generic workflow without its document before considering any legacy binding", () => {
    const { descriptor, errors } = parseSfc(readFileSync(new URL("../App.vue", import.meta.url), "utf8"));
    expect(errors).toEqual([]);
    const ast = compileScript(descriptor, { id: "chat-interface-guard" }).scriptSetupAst!;
    const declaration = ast.flatMap(node => node.type === "VariableDeclaration" ? node.declarations : [])
      .find(node => node.id.type === "Identifier" && node.id.name === "userInterfaceUrl");
    if (declaration?.init?.type !== "CallExpression"
      || declaration.init.arguments[0]?.type !== "ArrowFunctionExpression"
      || declaration.init.arguments[0].body.type !== "BlockStatement") throw new Error("Chat URL computed guard missing");
    const first = declaration.init.arguments[0].body.body[0];
    expect(first).toMatchObject({
      type: "IfStatement", test: { type: "CallExpression",
        callee: { type: "MemberExpression", object: { name: "graph" }, property: { name: "isGeneric" } },
        arguments: [{ type: "MemberExpression", object: { name: "workspace" }, property: { name: "activeWorkflowId" } }],
      },
      consequent: { type: "ReturnStatement", argument: { type: "ConditionalExpression",
        test: { type: "MemberExpression", object: { name: "graph" }, property: { name: "document" } },
        consequent: { type: "CallExpression", callee: { name: "graphChatInterfaceUrl" } },
        alternate: { type: "NullLiteral" },
      } },
    });
  });
});
