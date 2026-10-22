import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { graphClone, type GraphDocument, type GraphSession } from "../domain/workflowGraph";
import { workflowConfigurationCopyAccepted, workflowConfigurationCopyTargetName,
  type WorkflowConfigurationCopyRequest, type WorkflowConfigurationCopyResult } from "../domain/workflowConfigurationCopy";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";

let pinia: Pinia;
let evidence: ReturnType<typeof memoryRequestEvidence>;
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia);
  evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
});
afterEach(() => {
  useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose();
  disposePinia(pinia); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

function currentDocument(id: string, editSequence = 3, name = "Source workflow"): GraphDocument {
  return { schema_version: 3, workflow_definition_id: id, revision: 1, edit_sequence: editSequence,
    name, nodes: [], edges: [], object_bindings: [] } as unknown as GraphDocument;
}
function result(request: WorkflowConfigurationCopyRequest): WorkflowConfigurationCopyResult {
  return {
    schema_version: 1, kind: "workflow.configuration-copy-result", outcome: "copied",
    source_workflow_definition_id: request.source_workflow_definition_id,
    source_definition_revision: request.expected_source_definition_revision,
    source_edit_sequence: request.expected_source_edit_sequence,
    workflow_definition_id: request.target_workflow_definition_id,
    definition_revision: 1, edit_sequence: 1, session_id: null, node_id_map: {},
    document: currentDocument(request.target_workflow_definition_id, 1, request.target_name) as never,
  };
}
function commandResponse(request: WorkflowConfigurationCopyRequest) {
  const copied = result(request);
  return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-command",
    result: copied, receipt: { schema_version: 1, kind: "workflow.application-receipt",
      operation: "definition.copy", operation_scope: "management",
      idempotency_key: request.idempotency_key, request_sha256: "c".repeat(64),
      authority: "service_receipt", target: {}, accepted: workflowConfigurationCopyAccepted(copied) } }));
}
function receiptResponse(request: WorkflowConfigurationCopyRequest) {
  const copied = result(request);
  return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-receipt-read",
    outcome: "matched", reason_code: "receipt_matched",
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: "definition.copy",
      operation_scope: "management", idempotency_key: request.idempotency_key,
      request_sha256: "c".repeat(64), authority: "service_receipt", target: {},
      accepted: workflowConfigurationCopyAccepted(copied) },
    result: copied }));
}
function notFound() {
  return new Response(JSON.stringify({ error: { reason_code: "not_found", message: "Workflow not found" } }),
    { status: 404 });
}
type Transport = {
  requests: WorkflowConfigurationCopyRequest[];
  receiptRequests: WorkflowConfigurationCopyRequest[];
  readDefinition: string[];
};
function stubTransport(options: {
  send?: (request: WorkflowConfigurationCopyRequest) => Promise<Response>;
  receipt?: (request: WorkflowConfigurationCopyRequest) => Promise<Response>;
  definitionRead?: (identity: string) => Promise<Response>;
} = {}): Transport {
  const transport: Transport = { requests: [], receiptRequests: [], readDefinition: [] };
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    let body: { operation: string; parameters: Record<string, unknown> };
    try { body = JSON.parse(String(init?.body)); } catch { throw new Error(`Unexpected body for ${path}`); }
    if (path === "/api/graph/commands") {
      if (body.operation !== "definition.copy") throw new Error(`Unexpected command: ${body.operation}`);
      const request = body.parameters as unknown as WorkflowConfigurationCopyRequest;
      transport.requests.push(request);
      return options.send ? options.send(request) : commandResponse(request);
    }
    if (path === "/api/graph/receipts/read") {
      const request = body.parameters as unknown as WorkflowConfigurationCopyRequest;
      transport.receiptRequests.push(request);
      return options.receipt ? options.receipt(request) : receiptResponse(request);
    }
    if (path === "/api/graph/queries" && body.operation === "definition.read") {
      const identity = String(body.parameters.identity);
      transport.readDefinition.push(identity);
      return options.definitionRead ? options.definitionRead(identity) : notFound();
    }
    throw new Error(`Unexpected transport: ${path} ${body.operation}`);
  }));
  return transport;
}
function seedSource(graph = useWorkflowGraphStore(), workspace = useWorkspaceStore()) {
  const id = crypto.randomUUID(), document = currentDocument(id);
  graph.entries[id] = { document: graphClone(document), saved_document: graphClone(document),
    saved_revision: 1, latest_published_revision: 1, session_id: null, pending: null };
  workspace.registerGraphWorkflow({ id, title: "Source workflow", description: "", nodeCount: 0,
    state: "saved", fixedDefinitionRevision: 1, editSequence: 3, hasCurrentConfiguration: true });
  workspace.openWorkflow(id);
  return { graph, workspace, id, document };
}
function sessionView(id: string, sid = crypto.randomUUID()): GraphSession {
  return { schema_version: 2, execution_model: "graph", workflow_session_id: sid,
    workflow_definition_id: id, definition_revision: 1, revision: 4, data_revision: 0, head_revision: 1,
    status: "succeeded", can_submit: true, nodes: [], chains: [], outputs: [],
    data: { revision: 0, values: {} } };
}
const notice = () => useWorkbenchNoticesStore().error;

describe("formal configuration copy in the workflow store", () => {
  it("copies the saved current into a zero-session formal target and opens it only for the captured view", async () => {
    const { graph, workspace, id, document } = seedSource();
    const transport = stubTransport();
    const copiedTargetId = await graph.copyWorkflow(id);
    expect(copiedTargetId).not.toBeNull();
    expect(transport.requests).toHaveLength(1);
    const request = transport.requests[0]!;
    expect(Object.keys(request).sort()).toEqual(["expected_source_definition_revision",
      "expected_source_edit_sequence", "idempotency_key", "source_workflow_definition_id",
      "target_name", "target_workflow_definition_id"]);
    expect(request).toMatchObject({ source_workflow_definition_id: id,
      expected_source_definition_revision: 1, expected_source_edit_sequence: 3,
      target_workflow_definition_id: copiedTargetId, target_name: "Source workflow（副本）" });
    expect(request).not.toHaveProperty("document");
    expect(request).not.toHaveProperty("session_id");
    // Formal target projection: saved directory row, current entry, no session rows.
    expect(workspace.workflows.find(row => row.id === copiedTargetId)).toMatchObject({
      title: "Source workflow（副本）", state: "saved", sessionCount: 0, editSequence: 1,
      fixedDefinitionRevision: 1, hasCurrentConfiguration: true });
    expect(graph.entries[copiedTargetId!]!.saved_revision).toBe(1);
    expect(graph.entries[copiedTargetId!]!.session_id).toBeNull();
    expect(graph.sessionRows[copiedTargetId!]).toBeUndefined();
    expect(workspace.formalWorkflowCount()).toBe(2);
    expect(workspace.activeWorkflowId).toBe(copiedTargetId);
    expect(document.edit_sequence).toBe(3);
  });
  it("uses the saved current, never the selected session or unsaved editor state", async () => {
    const { graph, workspace, id, document } = seedSource();
    const view = sessionView(id);
    graph.entries[id]!.session_id = view.workflow_session_id;
    graph.views[view.workflow_session_id] = view;
    graph.sessionRows[id] = [{ workflow_session_id: view.workflow_session_id,
      workflow_definition_id: id, definition_revision: 1, revision: 4, name: "Session",
      metadata_sequence: 0, archived: false, active_chain_run_id: null, status: "succeeded" }];
    // Unsubmitted editor edit: neither the document nor its counters may be sent.
    graph.entries[id]!.document = { ...graphClone(document), edit_sequence: 9 };
    graph.entries[id]!.document.name = "Unsaved edit";
    const transport = stubTransport();
    expect(await graph.copyWorkflow(id)).not.toBeNull();
    expect(transport.requests[0]).toMatchObject({ expected_source_definition_revision: 1,
      expected_source_edit_sequence: 3, target_name: "Source workflow（副本）" });
    expect(transport.requests[0]).not.toHaveProperty("document");
    expect(transport.requests[0]).not.toHaveProperty("session_id");
    expect(graph.sessionRows[transport.requests[0]!.target_workflow_definition_id]).toBeUndefined();
    expect(workspace.activeWorkflowId).toBe(transport.requests[0]!.target_workflow_definition_id);
  });
  it("refuses a source without a formal current instead of cloning an editor document", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    const id = crypto.randomUUID();
    graph.entries[id] = { document: { schema_version: 2, workflow_definition_id: id, revision: 1,
      name: "draft", nodes: [], edges: [] } as unknown as GraphDocument, saved_revision: 0,
      session_id: null, pending: null };
    workspace.registerGraphWorkflow({ id, title: "draft", description: "", nodeCount: 0, state: "draft" });
    workspace.openWorkflow(id);
    const transport = stubTransport();
    expect(await graph.copyWorkflow(id)).toBeNull();
    expect(transport.requests).toHaveLength(0);
    expect(notice()).toMatchObject({ kind: "rejected" });
    expect(notice()?.reason).toContain("正式 current");
  });
  it("keeps an unknown copy pending without a formal row and confirms an unavailable target after reconcile", async () => {
    const { graph, workspace, id } = seedSource();
    stubTransport({ send: async () => { throw new TypeError("lost response"); } });
    const targetId = await graph.copyWorkflow(id);
    expect(targetId).not.toBeNull();
    const pendingRecord = [...evidence.records.values()][0]!;
    const pendingTarget = (pendingRecord.request as unknown as WorkflowConfigurationCopyRequest)
      .target_workflow_definition_id;
    expect(workspace.workflows.some(row => row.id === pendingTarget)).toBe(false);
    expect(workspace.formalWorkflowCount()).toBe(1);
    expect(graph.copyPendingRequests()).toMatchObject([{ workflowId: id, title: "Source workflow",
      pending: true, busy: false }]);
    // The new copy channel owns the copy gate while its request is unresolved,
    // independently of the editor/run locks exercised below.
    expect(graph.copyLocked).toBe(true);
    // A pending copy does not lock the source editor or run controls (the management
    // channel is initialized separately; the copy request itself is not a global lock).
    await graph.reconcile(id);
    expect(graph.locked).toBe(false);
    expect(evidence.records.size).toBe(1);
    expect([...evidence.records.values()][0]).toMatchObject({ operation: "definition.copy",
      owner_kind: "workflow-configuration-copy", owner_id: id, resolution: "pending" });

    // Reconcile: receipt matched, but the target was deleted since the copy.
    expect(await graph.reconcileCopy(id)).toBe(true);
    const reconciled = graph.copyPendingRequests()[0];
    expect(reconciled?.pending).toBe(false);
    expect(reconciled?.error).toContain("原复制已确认，目标现不可用");
    expect(graph.copyLocked).toBe(false);
    expect(workspace.workflows.some(row => row.id === pendingTarget)).toBe(false);
    expect([...evidence.records.values()][0]).toMatchObject({ resolution: "verified" });
  });
  it("keeps the current authoritative target after a matched receipt instead of restoring the historical copy", async () => {
    const { graph, workspace, id } = seedSource();
    stubTransport({ send: async () => { throw new TypeError("lost response"); } });
    await graph.copyWorkflow(id);
    const pending = [...evidence.records.values()][0]!.request as unknown as WorkflowConfigurationCopyRequest;
    const targetId = pending.target_workflow_definition_id;
    // The target was updated after the copy: the read returns the current edit_sequence 3,
    // not the historical edit_sequence 1 carried by the copy result.
    const transport = stubTransport({ definitionRead: async identity => new Response(
      JSON.stringify(currentDocument(identity, 3, "Renamed target"))) });
    expect(await graph.reconcileCopy(id)).toBe(true);
    expect(transport.requests).toHaveLength(0);
    expect(transport.readDefinition).toEqual([targetId]);
    expect(workspace.workflows.find(row => row.id === targetId)).toMatchObject({
      title: "Renamed target", state: "saved", hasCurrentConfiguration: true, editSequence: 3 });
    expect(graph.entries[targetId]!.saved_revision).toBe(1);
    expect(graph.entries[targetId]!.document.name).toBe("Renamed target");
    expect(workspace.formalWorkflowCount()).toBe(2);
    expect(graph.copyPendingRequests()).toEqual([]);
    expect([...evidence.records.values()][0]).toMatchObject({ resolution: "verified" });
  });
  it("keeps a late copy result on its own target without stealing the later view", async () => {
    const { graph, workspace, id } = seedSource();
    const otherId = crypto.randomUUID();
    graph.entries[otherId] = { document: currentDocument(otherId, 1, "Other"), saved_revision: 1,
      session_id: null, pending: null };
    workspace.registerGraphWorkflow({ id: otherId, title: "Other", description: "", nodeCount: 0,
      state: "saved", fixedDefinitionRevision: 1, editSequence: 1, hasCurrentConfiguration: true });
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const transport = stubTransport({ send: async request => { await gate; return commandResponse(request); } });
    const copying = graph.copyWorkflow(id);
    await vi.waitFor(() => expect(transport.requests).toHaveLength(1));
    workspace.openWorkflow(otherId);
    release();
    const targetId = await copying;
    expect(workspace.activeWorkflowId).toBe(otherId);
    expect(workspace.workflows.find(row => row.id === targetId)).toMatchObject({ state: "saved" });
  });
  it("blocks a second copy from the same source while the first is pending, and allows one after closing", async () => {
    const { graph, id } = seedSource();
    const transport = stubTransport({ send: async () => { throw new TypeError("lost response"); } });
    expect(await graph.copyWorkflow(id)).not.toBeNull();
    expect(await graph.copyWorkflow(id)).toBeNull();
    expect(transport.requests).toHaveLength(1);
    expect(evidence.records.size).toBe(1);
    // Closing the original request allows a new copy with a fresh target identity.
    const retry = stubTransport();
    expect(await graph.reconcileCopy(id)).toBe(true);
    expect(retry.receiptRequests).toHaveLength(1);
    const again = await graph.copyWorkflow(id);
    expect(again).not.toBeNull();
    expect(retry.requests).toHaveLength(1);
    expect(retry.requests[0]!.target_workflow_definition_id).not.toBe(
      retry.receiptRequests[0]!.target_workflow_definition_id);
  });
  it("refuses a concurrent copy while the first submission is still in flight", async () => {
    const { graph, id } = seedSource();
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const transport = stubTransport({ send: async request => { await gate; return commandResponse(request); } });
    const first = graph.copyWorkflow(id);
    // The second call must be refused before it can overwrite the first call's
    // in-flight open token or send a second business request.
    expect(await graph.copyWorkflow(id)).toBeNull();
    await vi.waitFor(() => expect(transport.requests).toHaveLength(1));
    release();
    expect(await first).not.toBeNull();
  });
  it("builds the target name deterministically within the workflow name limit", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    const id = crypto.randomUUID(), longName = "名".repeat(128);
    const document = currentDocument(id, 1, longName);
    graph.entries[id] = { document: graphClone(document), saved_document: graphClone(document),
      saved_revision: 1, session_id: null, pending: null };
    workspace.registerGraphWorkflow({ id, title: longName, description: "", nodeCount: 0,
      state: "saved", fixedDefinitionRevision: 1, editSequence: 1, hasCurrentConfiguration: true });
    workspace.openWorkflow(id);
    const transport = stubTransport();
    await graph.copyWorkflow(id);
    const name = transport.requests[0]!.target_name;
    expect([...name].length).toBe(128);
    expect(name).toBe(workflowConfigurationCopyTargetName(longName));
    expect(name.endsWith("（副本）")).toBe(true);
  });
});
