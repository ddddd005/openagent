import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { workflowManagementQueries } from "../application/workflowManagement";
import { graphClone, isGraphDocument, type GraphDocument, type GraphSession } from "../domain/workflowGraph";
import { sessionSummaryFromView } from "../domain/workflowDirectory";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";

const graphTransport = vi.hoisted(() => ({ send: vi.fn(), readReceipt: vi.fn() }));
vi.mock("../adapters/workflowApplicationApi", async importOriginal => {
  const original = await importOriginal<typeof import("../adapters/workflowApplicationApi")>();
  return { ...original, sendGraphCommand: graphTransport.send, readGraphReceipt: graphTransport.readReceipt };
});

let pinia: Pinia;
let evidence: ReturnType<typeof memoryRequestEvidence>;

beforeEach(() => {
  pinia = createPinia();
  setActivePinia(pinia);
  evidence = memoryRequestEvidence();
  graphTransport.send.mockReset();
  graphTransport.readReceipt.mockReset();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
});

afterEach(() => {
  disposePinia(pinia);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function seedSavedWorkflow(name: string) {
  const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
  const id = graph.createWorkflow();
  const document: GraphDocument = {
    schema_version: 3, workflow_definition_id: id, revision: 4, edit_sequence: 8,
    name, nodes: [], edges: [], object_bindings: [], package_lock: [],
    control_edges: [],
  };
  expect(isGraphDocument(document)).toBe(true);
  graph.entries[id] = { document: graphClone(document), saved_document: graphClone(document),
    saved_revision: document.revision, session_id: null, pending: null };
  workspace.markWorkflowSaved(id, name);
  return { graph, workspace, id, document };
}

function session(document: GraphDocument, overrides: Partial<GraphSession> = {}): GraphSession {
  return {
    schema_version: 2, execution_model: "graph", workflow_session_id: crypto.randomUUID(),
    workflow_definition_id: document.workflow_definition_id, definition_revision: document.revision,
    revision: 6, data_revision: 3, head_revision: 4, status: "idle", can_submit: true,
    nodes: [], chains: [], outputs: [], data: { revision: 3, values: { retained: "before" } },
    ...overrides,
  };
}

function managementResult(
  document: GraphDocument,
  action: "archive" | "delete",
  sessionId?: string,
  revision = document.revision,
) {
  return {
    schema_version: 1 as const,
    kind: sessionId ? "workflow.session-management" as const : "workflow.definition-management" as const,
    workflow_definition_id: document.workflow_definition_id,
    ...(sessionId ? { workflow_session_id: sessionId } : {}),
    revision,
    archived: action === "archive",
    deleted: action === "delete",
  };
}

function transport() {
  return graphTransport;
}

async function initializeManagement(graph: ReturnType<typeof useWorkflowGraphStore>, workflowId: string) {
  await graph.reconcile(workflowId);
  if (graph.active?.document.workflow_definition_id === workflowId)
    await vi.waitFor(() => expect(graph.locked).toBe(false));
}

function storedEvidence(operation: string, ownerId: string) {
  const matches = [...evidence.records.values()].filter(record =>
    record.operation === operation && record.owner_id === ownerId);
  expect(matches).toHaveLength(1);
  return matches[0]!;
}

describe("workflow management requests installed in the graph store", () => {
  it("creates a session for the exact saved definition and binds that accepted target", async () => {
    const { graph, id, document } = seedSavedWorkflow("Saved schema 3");
    await initializeManagement(graph, id);
    const created = session(document);
    const { send } = transport();
    send.mockResolvedValue(created);

    expect(await graph.createSession(id)).toBe(true);

    const [path, body] = send.mock.calls[0]!;
    expect(path).toBe("/api/graph/sessions");
    expect(body).toMatchObject({ workflow_definition_id: id, definition_revision: document.revision });
    expect(Object.keys(body).sort()).toEqual(["definition_revision", "idempotency_key", "workflow_definition_id"]);
    expect(graph.entries[id].session_id).toBe(created.workflow_session_id);
    expect(graph.session).toEqual(created);
    expect(storedEvidence("session.create", id)).toMatchObject({
      request: body, resolution: "verified", owner_kind: "workflow-management", owner_id: id,
    });
    expect(evidence.ports.write).toHaveBeenCalledWith({
      operation: "session.create", idempotency_key: body.idempotency_key,
      owner_kind: "workflow-management", owner_id: id, request: body,
    });
  });

  it("archives only the requested definition using its saved revision and original evidence", async () => {
    const { graph, workspace, id, document } = seedSavedWorkflow("Archive target");
    await initializeManagement(graph, id);
    const { send } = transport();
    send.mockResolvedValue(managementResult(document, "archive"));

    expect(await graph.manageWorkflow(id, "archive")).toBe(true);

    const [path, body] = send.mock.calls[0]!;
    expect(path).toBe(`/api/graph/definitions/${id}/archive`);
    expect(body).toMatchObject({ expected_revision: document.revision });
    expect(Object.keys(body).sort()).toEqual(["expected_revision", "idempotency_key"]);
    expect(storedEvidence("definition.archive", id)).toMatchObject({
      request: { ...body, identity: id }, resolution: "verified",
    });
    expect(workspace.workflows.find(row => row.id === id)?.archived).toBe(true);
  });

  it("deletes only the requested definition and leaves a different workflow selected", async () => {
    const target = seedSavedWorkflow("Delete target");
    const other = seedSavedWorkflow("Other workflow");
    target.workspace.openWorkflow(target.id);
    await initializeManagement(target.graph, other.id);
    await initializeManagement(target.graph, target.id);
    const { send } = transport();
    send.mockResolvedValue(managementResult(target.document, "delete"));

    expect(await target.graph.manageWorkflow(target.id, "delete")).toBe(true);

    const [path, body] = send.mock.calls[0]!;
    expect(path).toBe(`/api/graph/definitions/${target.id}/delete`);
    expect(body).toMatchObject({ expected_revision: target.document.revision });
    expect(storedEvidence("definition.delete", target.id)).toMatchObject({
      request: { ...body, identity: target.id }, resolution: "verified",
    });
    expect(target.graph.entries[target.id]).toBeUndefined();
    expect(target.workspace.workflows.some(row => row.id === target.id)).toBe(false);
    expect(target.workspace.activeWorkflowId).toBe(other.id);
    expect(target.graph.entries[other.id]).toBeDefined();
  });

  it("archives the exact session queried for the target workflow", async () => {
    const { graph, id, document } = seedSavedWorkflow("Session archive target");
    await initializeManagement(graph, id);
    const target = session(document);
    const archived = session(document, { ...target, archived: true, readonly: true,
      can_submit: false, available_actions: [] });
    graph.entries[id].session_id = target.workflow_session_id;
    graph.views[target.workflow_session_id] = target;
    const { send } = transport();
    const sessionRead = vi.spyOn(workflowManagementQueries, "session")
      .mockResolvedValueOnce(target).mockResolvedValueOnce(archived);
    vi.spyOn(workflowManagementQueries, "sessions").mockResolvedValue([archived]);
    send.mockResolvedValue(managementResult(document, "archive", target.workflow_session_id, target.revision));

    expect(await graph.manageSession(target.workflow_session_id, "archive", id)).toBe(true);

    const [path, body] = send.mock.calls[0]!;
    expect(path).toBe(`/api/graph/sessions/${target.workflow_session_id}/archive`);
    expect(body).toMatchObject({ expected_revision: target.revision });
    expect(storedEvidence("session.archive", id)).toMatchObject({
      request: { ...body, session_id: target.workflow_session_id }, resolution: "verified",
    });
    expect(sessionRead).toHaveBeenCalledWith(target.workflow_session_id, id);
    expect(graph.views[target.workflow_session_id]).toMatchObject({ archived: true, readonly: true, can_submit: false });
    expect(graph.session?.workflow_session_id).toBe(target.workflow_session_id);
  });

  it("deletes only the requested session and retains its sibling session", async () => {
    const { graph, id, document } = seedSavedWorkflow("Session delete target");
    await initializeManagement(graph, id);
    const target = session(document), sibling = session(document);
    graph.entries[id].session_id = target.workflow_session_id;
    graph.views[target.workflow_session_id] = target;
    graph.views[sibling.workflow_session_id] = sibling;
    graph.sessionRows[id] = [target, sibling].map(sessionSummaryFromView);
    const { send } = transport();
    vi.spyOn(workflowManagementQueries, "session").mockResolvedValue(target);
    send.mockResolvedValue(managementResult(document, "delete", target.workflow_session_id, target.revision));

    expect(await graph.manageSession(target.workflow_session_id, "delete", id)).toBe(true);

    const [path, body] = send.mock.calls[0]!;
    expect(path).toBe(`/api/graph/sessions/${target.workflow_session_id}/delete`);
    expect(body).toMatchObject({ expected_revision: target.revision });
    expect(storedEvidence("session.delete", id)).toMatchObject({
      request: { ...body, session_id: target.workflow_session_id }, resolution: "verified",
    });
    expect(graph.entries[id].session_id).toBeNull();
    expect(graph.views[target.workflow_session_id]).toBeUndefined();
    expect(graph.views[sibling.workflow_session_id]).toEqual(sibling);
    expect(graph.sessionRows[id]).toEqual([sibling].map(sessionSummaryFromView));
  });

  it("does not expose the retired session data write wrapper", async () => {
    const { graph, id } = seedSavedWorkflow("Data target");
    await initializeManagement(graph, id);
    expect("writeData" in graph).toBe(false);
    expect([...evidence.records.values()].filter(record =>
      record.operation === "session.data.update" && record.owner_id === id)).toEqual([]);
  });

  it("reopens an unknown create under the same owner and reads its original receipt without resending", async () => {
    const first = seedSavedWorkflow("Unknown create");
    const firstRows = graphClone(first.workspace.workflows);
    await initializeManagement(first.graph, first.id);
    const savedEntry = graphClone(first.graph.entries[first.id]);
    const created = session(first.document);
    const { send, readReceipt } = transport();
    send.mockRejectedValueOnce(new WorkbenchApiError("unknown", "lost create response"));
    readReceipt.mockResolvedValue(created);

    expect(await first.graph.createSession(first.id)).toBe(false);
    const original = storedEvidence("session.create", first.id);
    expect(original.resolution).toBe("pending");
    expect(send).toHaveBeenCalledOnce();

    disposePinia(pinia);
    pinia = createPinia();
    setActivePinia(pinia);
    const reopenedWorkspace = useWorkspaceStore(), reopened = useWorkflowGraphStore();
    reopenedWorkspace.restoreCatalog(firstRows, first.id, first.id);
    reopened.entries[first.id] = savedEntry;
    reopened.history[first.id] = { past: [], future: [] };

    await reopened.reconcile(first.id);

    expect(readReceipt).toHaveBeenCalledOnce();
    expect(readReceipt).toHaveBeenCalledWith("/api/graph/sessions", original.request);
    expect(send).toHaveBeenCalledOnce();
    expect(storedEvidence("session.create", first.id).resolution).toBe("verified");
    expect(reopened.entries[first.id].session_id).toBeNull();
    expect(reopened.session).toBeNull();
    expect(reopened.sessionRows[first.id]).toContainEqual(sessionSummaryFromView(created));
  });

  it("binds a late create receipt to its source workflow without changing another page or session", async () => {
    const source = seedSavedWorkflow("Create source");
    const other = seedSavedWorkflow("Displayed workflow");
    await initializeManagement(source.graph, source.id);
    await initializeManagement(other.graph, other.id);
    const otherSession = session(other.document);
    other.graph.entries[other.id].session_id = otherSession.workflow_session_id;
    other.graph.views[otherSession.workflow_session_id] = otherSession;
    source.workspace.openWorkflow(source.id);
    await initializeManagement(source.graph, source.id);
    const created = session(source.document);
    let finish!: (value: GraphSession) => void;
    const { send } = transport();
    send.mockImplementation(() => new Promise(resolve => { finish = resolve as (value: GraphSession) => void; }));

    const creating = source.graph.createSession(source.id);
    await vi.waitFor(() => expect(send).toHaveBeenCalledOnce());
    source.workspace.openWorkflow(other.id);
    finish(created);
    expect(await creating).toBe(true);

    expect(source.graph.entries[source.id].session_id).toBeNull();
    expect(source.graph.sessionRows[source.id]).toContainEqual(sessionSummaryFromView(created));
    expect(source.graph.entries[other.id].session_id).toBe(otherSession.workflow_session_id);
    expect(source.workspace.activeWorkflowId).toBe(other.id);
    expect(source.graph.session?.workflow_session_id).toBe(otherSession.workflow_session_id);
    expect(source.graph.views[created.workflow_session_id]).toEqual(created);
    expect(storedEvidence("session.create", source.id).resolution).toBe("verified");
  });
});
