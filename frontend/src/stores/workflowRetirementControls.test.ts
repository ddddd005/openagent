import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { graphClone, type GraphDocument, type GraphSession } from "../domain/workflowGraph";
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
    execution_roots: [], control_edges: [],
  };
  graph.entries[id] = { document: graphClone(document), saved_document: graphClone(document),
    saved_revision: document.revision, session_id: null, pending: null };
  workspace.markWorkflowSaved(id, name);
  return { graph, workspace, id, document };
}

function session(document: GraphDocument, overrides: Partial<GraphSession> = {}): GraphSession {
  return {
    schema_version: 2, execution_model: "graph", workflow_session_id: crypto.randomUUID(),
    workflow_definition_id: document.workflow_definition_id, definition_revision: document.revision,
    revision: 6, data_revision: 3, head_revision: 4, status: "idle", can_submit: false,
    readonly: true, available_actions: [], nodes: [], chains: [], outputs: [],
    data: { revision: 3, values: {} },
    ...overrides,
  };
}

function attachSession(graph: ReturnType<typeof useWorkflowGraphStore>, workflowId: string, view: GraphSession) {
  graph.entries[workflowId]!.session_id = view.workflow_session_id;
  graph.views[view.workflow_session_id] = view;
}

async function initializeManagement(graph: ReturnType<typeof useWorkflowGraphStore>, workflowId: string) {
  await graph.reconcile(workflowId);
  await vi.waitFor(() => expect(graph.controlLocked).toBe(false));
}

function evidenceFor(operation: string, workflowId: string) {
  return [...evidence.records.values()].filter(row =>
    row.operation === operation && row.owner_id === workflowId);
}

function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (failure: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe("retired node definition workflow controls", () => {
  it("keeps old exhausted-budget state readable but cannot construct a new budget-extension action", async () => {
    const { graph, id, document } = seedSavedWorkflow("Legacy exhausted budget");
    await initializeManagement(graph, id);
    const old = session(document, { status: "budget_exhausted", can_submit: false,
      available_actions: ["extend_budget"], nodes: [{
        node_binding_id: crypto.randomUUID(), label: "Old Agent", status: "budget_exhausted",
        run_id: crypto.randomUUID(), revision: 2, outputs: {}, diagnostic: null,
        budget: { max_model_requests: 8, max_model_attempts: 32, model_requests: 8, attempts: 8 },
      }] });
    attachSession(graph, id, old);

    const legacyCall = graph.submitAgentAction as unknown as
      (action: string, requests?: number, attempts?: number) => Promise<boolean>;
    expect(await legacyCall("extend_budget", 1, 4)).toBe(false);
    await expect(graph.management.commands.control({ workflowId: id }, "extend_budget" as never))
      .rejects.toMatchObject({ kind: "rejected" });
    expect(graph.session?.status).toBe("budget_exhausted");
    expect(graph.session?.nodes[0]?.budget).toMatchObject({ model_requests: 8, attempts: 8 });
    expect(graphTransport.send).not.toHaveBeenCalled();
    expect(evidence.records.size).toBe(0);
  });

  it("keeps readonly sessions readonly while pause, close, and result acceptance use management evidence", async () => {
    const { graph, id, document } = seedSavedWorkflow("Readonly controls");
    await initializeManagement(graph, id);
    const running = session(document, { status: "running", available_actions: ["pause", "close"] });
    attachSession(graph, id, running);
    expect(graph.locked).toBe(true);
    expect(graph.controlLocked).toBe(false);

    const paused = session(document, { ...running, revision: running.revision + 1,
      status: "paused", available_actions: ["resume", "close"] });
    graphTransport.send.mockResolvedValueOnce(paused);
    await graph.submitPrimary();
    expect(graphTransport.send).toHaveBeenCalledWith(
      `/api/graph/sessions/${running.workflow_session_id}/control`,
      expect.objectContaining({ action: "pause", expected_revision: running.revision }),
    );
    expect(evidenceFor("run.control", id)).toHaveLength(1);
    expect(evidenceFor("run.control", id)[0]).toMatchObject({ resolution: "verified" });
    expect(graph.session).toMatchObject({ readonly: true, status: "paused" });

    const closed = session(document, { ...paused, revision: paused.revision + 1,
      status: "closed", available_actions: [] });
    graphTransport.send.mockResolvedValueOnce(closed);
    await graph.closeRun();
    expect(graphTransport.send).toHaveBeenCalledTimes(2);
    expect(graphTransport.send.mock.calls[1]).toMatchObject([
      `/api/graph/sessions/${running.workflow_session_id}/control`,
      expect.objectContaining({ action: "close", expected_revision: paused.revision }),
    ]);
    expect(evidenceFor("run.control", id)).toHaveLength(2);
    expect(evidenceFor("run.control", id).every(row => row.resolution === "verified")).toBe(true);

    const resultTarget = seedSavedWorkflow("Readonly result acceptance");
    await initializeManagement(resultTarget.graph, resultTarget.id);
    const awaitingAcceptance = session(resultTarget.document, {
      status: "archive_failed", available_actions: ["retry_acceptance"],
    });
    attachSession(resultTarget.graph, resultTarget.id, awaitingAcceptance);
    graphTransport.send.mockResolvedValueOnce(session(resultTarget.document, {
      ...awaitingAcceptance, revision: awaitingAcceptance.revision + 1,
      status: "running", available_actions: ["pause", "close"],
    }));
    expect(await resultTarget.graph.submitAgentAction("retry_acceptance")).toBe(true);
    expect(graphTransport.send.mock.calls[2]).toMatchObject([
      `/api/graph/sessions/${awaitingAcceptance.workflow_session_id}/control`,
      expect.objectContaining({ action: "retry_acceptance",
        expected_revision: awaitingAcceptance.revision }),
    ]);
    expect(evidenceFor("run.control", resultTarget.id)).toMatchObject([
      { resolution: "verified", request: expect.objectContaining({ action: "retry_acceptance" }) },
    ]);

    const resumeTarget = seedSavedWorkflow("Readonly continuation");
    await initializeManagement(resumeTarget.graph, resumeTarget.id);
    const retainedPause = session(resumeTarget.document, {
      status: "paused", available_actions: ["resume"],
    });
    attachSession(resumeTarget.graph, resumeTarget.id, retainedPause);
    graphTransport.send.mockResolvedValueOnce(session(resumeTarget.document, {
      ...retainedPause, revision: retainedPause.revision + 1,
      status: "running", available_actions: ["pause"],
    }));
    await resumeTarget.graph.submitPrimary();
    expect(graphTransport.send.mock.calls[3]).toMatchObject([
      `/api/graph/sessions/${retainedPause.workflow_session_id}/control`,
      expect.objectContaining({ action: "resume", expected_revision: retainedPause.revision }),
    ]);
    expect(evidenceFor("run.control", resumeTarget.id)).toMatchObject([
      { resolution: "verified", request: expect.objectContaining({ action: "resume" }) },
    ]);
  });

  it("blocks edits and new start on readonly sessions, and requires the advertised action", async () => {
    const { graph, id, document } = seedSavedWorkflow("Readonly start");
    await initializeManagement(graph, id);
    graph.entries[id]!.external_inputs = { retained: "before" };
    const readonlyIdle = session(document, { status: "idle", can_submit: false, available_actions: [] });
    attachSession(graph, id, readonlyIdle);

    expect(graph.locked).toBe(true);
    expect(graph.editorLocked).toBe(true);
    expect(graph.controlLocked).toBe(false);
    graph.setInputs({ retained: "after" });
    await graph.submitPrimary();
    await graph.closeRun();

    expect(graph.entries[id]!.external_inputs).toEqual({ retained: "before" });
    expect(graphTransport.send).not.toHaveBeenCalled();
    expect(evidence.records.size).toBe(0);

    const runningWithoutPause = session(document, { status: "running", available_actions: ["close"] });
    attachSession(graph, id, runningWithoutPause);
    await graph.submitPrimary();
    expect(graphTransport.send).not.toHaveBeenCalled();
  });

  it("retains mutation, unknown-request, archive, and session-selection locks for controls", async () => {
    const { graph, id, document } = seedSavedWorkflow("Readonly pending");
    await initializeManagement(graph, id);
    const running = session(document, { status: "running", available_actions: ["pause", "close"] });
    attachSession(graph, id, running);

    const sendGate = deferred<unknown>();
    graphTransport.send.mockImplementationOnce(() => sendGate.promise);
    const submitting = graph.submitPrimary();
    await vi.waitFor(() => expect(graphTransport.send).toHaveBeenCalledOnce());
    expect(graph.controlLocked).toBe(true);
    sendGate.reject(new WorkbenchApiError("unknown", "run control response lost"));
    await submitting;

    expect(graph.pending).toMatchObject({ action: "pause" });
    expect(graph.controlLocked).toBe(true);
    await graph.submitPrimary();
    expect(graphTransport.send).toHaveBeenCalledOnce();
    expect(evidenceFor("run.control", id)).toMatchObject([
      { resolution: "pending", request: expect.objectContaining({ action: "pause" }) },
    ]);

    const archivedTarget = seedSavedWorkflow("Archived readonly");
    await initializeManagement(archivedTarget.graph, archivedTarget.id);
    const archivedSession = session(archivedTarget.document, {
      status: "running", available_actions: ["pause", "close"],
    });
    attachSession(archivedTarget.graph, archivedTarget.id, archivedSession);
    archivedTarget.workspace.setWorkflowManagement(archivedTarget.id, true);
    expect(archivedTarget.graph.controlLocked).toBe(true);
    await archivedTarget.graph.submitPrimary();
    await archivedTarget.graph.closeRun();
    expect(graphTransport.send).toHaveBeenCalledOnce();

    const deletedTarget = seedSavedWorkflow("Deleted readonly");
    await initializeManagement(deletedTarget.graph, deletedTarget.id);
    const deletedSession = session(deletedTarget.document, {
      status: "running", available_actions: ["pause", "close"],
    });
    attachSession(deletedTarget.graph, deletedTarget.id, deletedSession);
    deletedTarget.workspace.workflows.find(row => row.id === deletedTarget.id)!.deleted = true;
    expect(deletedTarget.graph.controlLocked).toBe(true);
    await deletedTarget.graph.submitPrimary();
    await deletedTarget.graph.closeRun();
    expect(graphTransport.send).toHaveBeenCalledOnce();

    const nextId = crypto.randomUUID();
    const selection = deferred<GraphSession>();
    const switching = seedSavedWorkflow("Selecting readonly session");
    await initializeManagement(switching.graph, switching.id);
    const current = session(switching.document, { status: "running", available_actions: ["pause"] });
    attachSession(switching.graph, switching.id, current);
    const sessionQuery = vi.spyOn(switching.graph.management.queries, "session")
      .mockReturnValueOnce(selection.promise);
    const selecting = switching.graph.selectSession(nextId);
    expect(sessionQuery).toHaveBeenCalledWith(nextId, switching.id);
    await vi.waitFor(() => expect(switching.graph.controlLocked).toBe(true));
    await switching.graph.submitPrimary();
    expect(graphTransport.send).toHaveBeenCalledOnce();
    selection.resolve(session(switching.document, {
      workflow_session_id: nextId, status: "idle", available_actions: [],
    }));
    await selecting;
    expect(switching.graph.session?.workflow_session_id).toBe(nextId);
    expect(switching.graph.controlLocked).toBe(false);
  });
});
