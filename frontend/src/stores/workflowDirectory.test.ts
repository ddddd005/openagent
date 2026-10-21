import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import type { GraphDocument, GraphNodeType, GraphSession } from "../domain/workflowGraph";
import { sessionSummaryFromView, type WorkflowDefinitionSummary,
  type WorkflowSessionSummary } from "../domain/workflowDirectory";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";

let pinia: Pinia;
let evidence: ReturnType<typeof memoryRequestEvidence>;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); evidence = memoryRequestEvidence(); });
afterEach(() => { disposePinia(pinia); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const textType: GraphNodeType = { component_id: "sample.text", component_version: "1", display_name: "Text",
  category: "Test", config_schema: { type: "object" }, default_config: {}, inputs: [], outputs: [],
  executable: true, is_output: false };

const workflowId = () => crypto.randomUUID();

function definitionRow(overrides: Partial<WorkflowDefinitionSummary> = {}): WorkflowDefinitionSummary {
  return { workflow_definition_id: workflowId(), display_name: "Formal workflow",
    fixed_definition_revision: 1, edit_sequence: 1, archived: false, node_count: 2, session_count: 0,
    has_current_configuration: true, ...overrides };
}
function sessionRow(view: GraphSession): WorkflowSessionSummary {
  return sessionSummaryFromView(view);
}
function currentDocument(id: string, editSequence = 1): GraphDocument {
  return { schema_version: 3, workflow_definition_id: id, revision: 1, edit_sequence: editSequence,
    name: "Formal workflow", nodes: [], edges: [], object_bindings: [] } as unknown as GraphDocument;
}
function fixedDocument(id: string, revision = 3): GraphDocument {
  return { schema_version: 3, workflow_definition_id: id, revision, name: "Legacy fixed workflow",
    nodes: [], edges: [], object_bindings: [] } as unknown as GraphDocument;
}
function sessionView(id: string, sid: string, overrides: Partial<GraphSession> = {}): GraphSession {
  return { schema_version: 2, execution_model: "graph", workflow_session_id: sid,
    workflow_definition_id: id, definition_revision: 1, revision: 2, data_revision: 0, head_revision: 1,
    status: "idle", can_submit: true, name: "Session", metadata_sequence: 0, archived: false,
    nodes: [], chains: [], outputs: [], data: { revision: 0, values: {} }, ...overrides };
}

type QueryCall = { operation: string; parameters: Record<string, unknown> };
function stubQueries(handler: (operation: string, parameters: Record<string, unknown>) => unknown) {
  const calls: QueryCall[] = [];
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    if (path !== "/api/graph/queries") throw new Error(`Unexpected transport: ${path}`);
    const body = JSON.parse(String(init?.body)) as QueryCall;
    calls.push(body);
    const value = handler(body.operation, body.parameters);
    return new Response(JSON.stringify(value === undefined ? [] : value));
  }));
  return calls;
}
function installCleanConfiguration(graph: ReturnType<typeof useWorkflowGraphStore>) {
  const application = createWorkflowConfigurationApplication({
    evidence: evidence.ports, extensions: () => [],
    update: vi.fn(async () => { throw new Error("No configuration write expected"); }),
    readReceipt: vi.fn(async () => { throw new Error("No configuration receipt expected"); }),
  });
  graph.installWorkflowConfigurationApplication(application);
  return application;
}

describe("workflow directory consumer separation", () => {
  it("refreshes only the directory projection from definition summaries", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    graph.catalog = [textType];
    const draftId = graph.createWorkflow();
    workspace.updateWorkflowProjection(draftId, "Local draft title", 0);
    installCleanConfiguration(graph);
    const formal = definitionRow({ display_name: "Backend title", node_count: 4, session_count: 2 });
    const archived = definitionRow({ archived: true, display_name: "Archived backend" });
    const calls = stubQueries((operation, parameters) => {
      expect(operation).toBe("definition.summaries");
      expect(parameters).toEqual({ include_archived: true });
      return [formal, archived];
    });

    expect(await graph.loadDefinitions()).toBe(true);
    expect(calls).toHaveLength(1);
    // Summaries never become configuration entries.
    expect(graph.entries[formal.workflow_definition_id]).toBeUndefined();
    expect(graph.entries[archived.workflow_definition_id]).toBeUndefined();
    const row = workspace.workflows.find(item => item.id === formal.workflow_definition_id)!;
    expect(row).toMatchObject({ title: "Backend title", nodeCount: 4, state: "saved",
      archived: false, fixedDefinitionRevision: 1, editSequence: 1,
      hasCurrentConfiguration: true, sessionCount: 2, fromSummary: true });
    expect(workspace.workflows.find(item => item.id === archived.workflow_definition_id)?.archived).toBe(true);
    // The local draft row survives and keeps its local title.
    const draft = workspace.workflows.find(item => item.id === draftId)!;
    expect(draft).toMatchObject({ state: "draft", title: "Local draft title" });
    // Formal counts only include formal summary rows; archived is filtered by request.
    expect(workspace.formalWorkflowCount()).toBe(1);
    expect(workspace.formalWorkflowCount(true)).toBe(2);
  });

  it("retains local drafts, unconfirmed copies and verified deletions during a directory refresh", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    const draft = workflowId(), copy = workflowId(), deleted = workflowId(), formal = workflowId();
    workspace.registerGraphWorkflow({ id: draft, title: "Local draft", description: "", nodeCount: 0, state: "draft" });
    workspace.registerGraphWorkflow({ id: copy, title: "Unconfirmed copy", description: "", nodeCount: 0,
      state: "draft", copyPending: true, sourceId: formal });
    workspace.registerGraphWorkflow({ id: deleted, title: "Deleted", description: "", nodeCount: 0,
      state: "saved", deleted: true });
    installCleanConfiguration(graph);
    stubQueries(() => [
      definitionRow({ workflow_definition_id: draft, display_name: "Backend draft shadow" }),
      definitionRow({ workflow_definition_id: copy, display_name: "Backend copy shadow" }),
      definitionRow({ workflow_definition_id: deleted, display_name: "Deleted shadow" }),
      definitionRow({ workflow_definition_id: formal, display_name: "Formal", node_count: 3 }),
    ]);
    expect(await graph.loadDefinitions()).toBe(true);
    expect(workspace.workflows.find(row => row.id === draft)).toMatchObject({
      state: "draft", title: "Local draft", nodeCount: 0 });
    expect(workspace.workflows.find(row => row.id === copy)).toMatchObject({
      copyPending: true, title: "Unconfirmed copy" });
    expect(workspace.workflows.find(row => row.id === deleted)).toMatchObject({
      deleted: true, title: "Deleted" });
    expect(workspace.workflows.find(row => row.id === formal)).toMatchObject({
      title: "Formal", state: "saved", nodeCount: 3 });
    expect(workspace.formalWorkflowCount()).toBe(1);
  });

  it("opens a formal current through definition.read instead of the directory row", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    graph.catalog = [textType];
    const id = workflowId(), sid = crypto.randomUUID();
    const row = definitionRow({ workflow_definition_id: id, node_count: 0 });
    const document = currentDocument(id, 2);
    const listRow = sessionRow(sessionView(id, sid, { archived: true, name: "Archived session" }));
    const idleRow = sessionRow(sessionView(id, crypto.randomUUID()));
    const calls = stubQueries((operation, parameters) => {
      if (operation === "definition.summaries") return [row];
      if (operation === "definition.read") {
        expect(parameters).toEqual({ identity: id });
        return document;
      }
      if (operation === "session.summaries") {
        expect(parameters).toEqual({ workflow_definition_id: id, include_archived: true });
        return [listRow, idleRow];
      }
      if (operation === "request-evidence.read") return [];
      throw new Error(`Unexpected query: ${operation}`);
    });
    installCleanConfiguration(graph);
    expect(await graph.loadDefinitions()).toBe(true);
    // The directory row alone does not create an entry or read a full document.
    expect(graph.entries[id]).toBeUndefined();
    expect(calls.map(call => call.operation)).toEqual(["definition.summaries"]);
    workspace.openWorkflow(id);
    await graph.activate(id);

    // Opening read the formal current and then the session summaries only.
    const operations = calls.map(call => call.operation);
    expect(operations.filter(operation => operation === "definition.read")).toHaveLength(1);
    expect(operations.filter(operation => operation === "session.summaries")).toHaveLength(1);
    expect(operations).not.toContain("definition.list");
    expect(graph.entries[id]).toMatchObject({ saved_revision: 1 });
    expect(graph.entries[id]!.document).toEqual(document);
    expect(graph.entries[id]!.fixed_only).toBeUndefined();
    expect(graph.sessionRows[id]).toEqual([listRow, idleRow]);
    expect(graph.views[sid]).toBeUndefined();
  });

  it("keeps a formal fixed revision without current read-only and never fabricates edit_sequence", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    graph.catalog = [textType];
    const id = workflowId();
    const fixed = fixedDocument(id, 3);
    const row = definitionRow({ workflow_definition_id: id, fixed_definition_revision: 3,
      edit_sequence: null, has_current_configuration: false, node_count: 0 });
    stubQueries((operation, parameters) => {
      if (operation === "definition.summaries") return [row];
      if (operation === "definition.read") { expect(parameters).toEqual({ identity: id }); return fixed; }
      if (operation === "session.summaries") return [];
      if (operation === "request-evidence.read") return [];
      throw new Error(`Unexpected query: ${operation}`);
    });
    installCleanConfiguration(graph);
    expect(await graph.loadDefinitions()).toBe(true);
    workspace.openWorkflow(id);
    await graph.activate(id);

    expect(graph.entries[id]).toMatchObject({ saved_revision: 3, fixed_only: true });
    expect(graph.entries[id]!.document.edit_sequence).toBeUndefined();
    expect(graph.entries[id]!.saved_document!.edit_sequence).toBeUndefined();
    expect(graph.fixedOnly).toBe(true);
    expect(graph.locked).toBe(true);
    expect(graph.renameWorkflow("Should not be accepted")).toBeNull();
    expect(useWorkbenchNoticesStore().error?.reason).toContain("没有正式 current 配置");
  });

  it("selects a session through session.read while the list stays on summaries", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    graph.catalog = [textType];
    const id = workflowId(), sid = crypto.randomUUID();
    const row = definitionRow({ workflow_definition_id: id, node_count: 0 });
    const detail = sessionView(id, sid, { revision: 4, status: "succeeded", can_submit: true });
    const summary = sessionRow(sessionView(id, sid, { revision: 3 }));
    const calls = stubQueries((operation, parameters) => {
      if (operation === "definition.summaries") return [row];
      if (operation === "definition.read") return currentDocument(id);
      if (operation === "session.summaries") return [summary];
      if (operation === "session.read") {
        expect(parameters).toEqual({ session_id: sid });
        return detail;
      }
      if (operation === "request-evidence.read") return [];
      throw new Error(`Unexpected query: ${operation}`);
    });
    installCleanConfiguration(graph);
    await graph.loadDefinitions();
    workspace.openWorkflow(id);
    await graph.activate(id);
    expect(graph.sessionRows[id]).toEqual([summary]);
    expect(graph.views[sid]).toBeUndefined();

    await vi.waitFor(() => expect(graph.locked).toBe(false));
    await graph.selectSession(sid);
    expect(graph.views[sid]).toEqual(detail);
    expect(graph.sessionRows[id]).toEqual([summary]);
    expect(calls).toContainEqual({ operation: "session.read", parameters: { session_id: sid } });
  });
});
