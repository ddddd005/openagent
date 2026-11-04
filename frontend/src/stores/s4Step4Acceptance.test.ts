import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { graphClone, graphJsonSignature, type GraphDocument } from "../domain/workflowGraph";
import { isGraphConfigurationDocument,
  type GraphConfigurationDocument, type GraphConfigurationResult,
  type GraphConfigurationUpdateDocument, type GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { createWorkflowConfigurationApplication,
  type WorkflowConfigurationApplicationPorts } from "../application/workflowConfiguration";
import { readPendingEvidence, requestEvidencePorts, type PendingRequestEvidence,
  type RequestEvidencePorts, type RequestEvidenceRecord, type RequestEvidenceResolve,
  type RequestEvidenceWrite } from "../adapters/requestEvidenceApi";
import { WorkbenchApiError } from "../adapters/workbenchApi";

/**
 * Independent verifier acceptance for step 4 frontend (V4-02 evidence layering,
 * V4-04 read reuse/freshness).  The evidence port below is implemented here so the
 * layering, the five-step chain and the unknown path are observed directly; the
 * shipped adapter is additionally exercised against a stubbed transport.
 */

const ownerKind = "workflow-configuration";
const operation = "definition.update";
const digestOf = (request: unknown) =>
  createHash("sha256").update(graphJsonSignature(request)).digest("hex");

function updateDocument(workflowId: string, revision = 1): GraphConfigurationUpdateDocument {
  const value = {
    schema_version: 3 as const,
    workflow_definition_id: workflowId,
    revision,
    name: "S4 acceptance",
    nodes: [],
    edges: [],
    object_bindings: [],
    package_lock: [],
    control_edges: [],
  };
  return value as unknown as GraphConfigurationUpdateDocument;
}

function updateRequest(workflowId: string, key = crypto.randomUUID()): GraphConfigurationUpdateRequest {
  return { workflow_definition_id: workflowId, expected_edit_sequence: 0,
    document: updateDocument(workflowId), idempotency_key: key };
}

function savedResult(request: GraphConfigurationUpdateRequest): GraphConfigurationResult {
  const document = { ...graphClone(request.document), revision: 1, edit_sequence: 1 };
  return {
    schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
    source_workflow_definition_id: request.workflow_definition_id,
    workflow_definition_id: request.workflow_definition_id,
    definition_revision: 1, edit_sequence: 1, source_has_effective_session: false,
    changed_categories: ["fixed"],
    node_id_map: Object.fromEntries(document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null, document: document as unknown as GraphConfigurationDocument,
  };
}

/** An independent in-memory evidence store with the Q07 narrow reads. */
type NarrowEvidencePorts = RequestEvidencePorts & {
  read: ReturnType<typeof vi.fn>;
  readPending: ReturnType<typeof vi.fn>;
  readPoint: ReturnType<typeof vi.fn>;
  write: ReturnType<typeof vi.fn>;
  resolve: ReturnType<typeof vi.fn>;
};

function evidenceStore() {
  const records = new Map<string, RequestEvidenceRecord>();
  const identity = (operationValue: string, key: string) => JSON.stringify([operationValue, key]);
  const ports = {
    read: vi.fn(async (kind: string, id: string) => graphClone([...records.values()]
      .filter(record => record.owner_kind === kind && record.owner_id === id))),
    readPending: vi.fn(async (kind: string, id: string) => [...records.values()]
      .filter(record => record.owner_kind === kind && record.owner_id === id
        && record.resolution === "pending")
      .map(record => ({ owner_kind: record.owner_kind, owner_id: record.owner_id,
        operation: record.operation, idempotency_key: record.idempotency_key,
        request_digest: record.request_digest, resolution: "pending" as const }))),
    readPoint: vi.fn(async (kind: string, id: string, operationValue: string, key: string) =>
      graphClone([...records.values()].filter(record => record.owner_kind === kind
        && record.owner_id === id && record.operation === operationValue
        && record.idempotency_key === key))),
    write: vi.fn(async (request: RequestEvidenceWrite): Promise<RequestEvidenceRecord> => {
      const key = identity(request.operation, request.idempotency_key), prior = records.get(key);
      if (prior) {
        if (prior.owner_kind !== request.owner_kind || prior.owner_id !== request.owner_id
          || graphJsonSignature(prior.request) !== graphJsonSignature(request.request))
          throw new WorkbenchApiError("rejected", "original request differs", 409, "idempotency_conflict");
        const reopened: RequestEvidenceRecord = { ...prior, resolution: "pending" };
        records.set(key, reopened);
        return graphClone(reopened);
      }
      if ([...records.values()].some(record => record.owner_kind === request.owner_kind
        && record.owner_id === request.owner_id && record.resolution === "pending"))
        throw new WorkbenchApiError("rejected", "original request is unresolved", 409, "request_evidence_pending");
      const record: RequestEvidenceRecord = { ...graphClone(request), schema_version: 1,
        kind: "workflow.request-evidence", resolution: "pending",
        request_digest: digestOf(request.request) };
      records.set(key, graphClone(record));
      return graphClone(record);
    }),
    resolve: vi.fn(async (request: RequestEvidenceResolve): Promise<RequestEvidenceRecord> => {
      const record = records.get(identity(request.operation, request.idempotency_key));
      if (!record || record.request_digest !== request.request_digest
        || record.resolution !== "pending" && record.resolution !== request.resolution)
        throw new WorkbenchApiError("rejected", "original request differs", 409, "request_evidence_conflict");
      record.resolution = request.resolution;
      return graphClone(record);
    }),
  } as unknown as NarrowEvidencePorts;
  return { records, ports, identity };
}

function applicationFixture(ports: Partial<WorkflowConfigurationApplicationPorts> = {}) {
  const evidence = evidenceStore();
  const update = vi.fn(async (request: GraphConfigurationUpdateRequest) => savedResult(request));
  const readReceipt = vi.fn(async (request: GraphConfigurationUpdateRequest) => savedResult(request));
  const application = createWorkflowConfigurationApplication({
    extensions: () => [], evidence: evidence.ports, update, readReceipt, ...ports });
  return { application, evidence, update, readReceipt };
}

function freshWorkflowId() { return crypto.randomUUID(); }

function frozenDocument(identity: string, revision: number,
  title = "frozen title"): GraphDocument {
  return { schema_version: 2, workflow_definition_id: identity, revision, name: "frozen",
    nodes: [{ node_binding_id: crypto.randomUUID(), component_id: "tools.text",
      component_version: "1", title, position: { x: 0, y: 0 }, config: {} }],
    edges: [], object_bindings: [], package_lock: [], control_edges: [] } as unknown as GraphDocument;
}

type ApplicationRequest = { path: string; operation: string; parameters: Record<string, unknown> };

/** Evidence reads used only to initialize the management outbox in these tests. */
function evidenceInitialization(request_: ApplicationRequest): { status?: number; body: unknown } | null {
  if (request_.parameters.owner_kind !== "workflow-management") return null;
  if (request_.operation === "request-evidence.read") return { body: [] as unknown };
  if (request_.operation === "request-evidence.pending")
    return { body: { schema_version: 1, kind: "workflow.request-evidence-page",
      items: [], next_cursor: null } };
  return null;
}

function stubTransport(handler: (request: ApplicationRequest) => { status?: number; body: unknown }) {
  const requests: ApplicationRequest[] = [];
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    const request = { path: String(path), operation: String(body.operation),
      parameters: (body.parameters ?? {}) as Record<string, unknown> };
    requests.push(request);
    const response = evidenceInitialization(request) ?? handler(request);
    return new Response(JSON.stringify(response.body), { status: response.status ?? 200 });
  }));
  const calls = (operation: string) => requests.filter(request_ => request_.operation === operation);
  return { requests, calls };
}

/** Let the lazily created management request outbox finish its evidence read. */
async function settledLocks(graph: ReturnType<typeof useWorkflowGraphStore>) {
  await vi.waitFor(() => expect(graph.locked).toBe(false));
}

let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => {
  useWorkflowGraphStore().$dispose(); useWorkspaceStore().$dispose();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// V4-02: layered evidence initialization, complete point reads, five-step chain,
// unknown verification without replay, explicit failures for anomalies.
// ---------------------------------------------------------------------------
describe("V4-02 layered evidence initialization", () => {
  it("ends initialization from the bodyless summary when no request is pending", async () => {
    const { application, evidence } = applicationFixture();
    const workflowId = freshWorkflowId(), entry = application.forWorkflow(workflowId);
    await entry.initialize();
    expect(evidence.ports.readPending).toHaveBeenCalledTimes(1);
    expect(evidence.ports.readPoint).not.toHaveBeenCalled();
    expect(evidence.ports.read).not.toHaveBeenCalled();
    expect(entry.locked.value).toBe(false);
    expect(entry.pending.value).toBeNull();
    expect(entry.progress.value).toBe("ready");
  });

  it("restores a pending request from a complete point read, without reading history", async () => {
    const { application, evidence } = applicationFixture();
    const workflowId = freshWorkflowId(), request = updateRequest(workflowId);
    const written = await evidence.ports.write({ operation, idempotency_key: request.idempotency_key,
      owner_kind: ownerKind, owner_id: workflowId, request: { ...graphClone(request) } });
    const entry = application.forWorkflow(workflowId);
    await entry.initialize();
    expect(evidence.ports.readPending).toHaveBeenCalledTimes(1);
    expect(evidence.ports.readPoint).toHaveBeenCalledWith(ownerKind, workflowId,
      operation, request.idempotency_key);
    expect(evidence.ports.read).not.toHaveBeenCalled();
    expect(entry.pending.value).toEqual(request);
    expect(entry.locked.value).toBe(true);
    expect(entry.progress.value).toBe("restored");
    // The point read returned the complete original body, not a summary.
    const point = await evidence.ports.readPoint(ownerKind, workflowId, operation, request.idempotency_key);
    expect(point).toHaveLength(1);
    expect(point[0]).toEqual(written);
    expect(point[0]).toMatchObject({ operation, owner_kind: ownerKind, owner_id: workflowId,
      request_digest: written.request_digest, resolution: "pending" });
    expect(point[0].request).toEqual(request);
  });

  it("treats an untrustworthy summary as an explicit failure, never as no pending", async () => {
    const { application, evidence } = applicationFixture();
    const workflowId = freshWorkflowId(), base = { owner_kind: ownerKind, owner_id: workflowId,
      operation, request_digest: "a".repeat(64), resolution: "pending" as const };
    vi.mocked(evidence.ports.readPending!).mockResolvedValueOnce([
      { ...base, idempotency_key: crypto.randomUUID() },
      { ...base, idempotency_key: crypto.randomUUID() }]);
    const entry = application.forWorkflow(workflowId);
    await expect(entry.initialize()).rejects.toThrow(/多条未结案|摘要/);
    expect(entry.locked.value).toBe(true);
    expect(entry.pending.value).toBeNull();
    expect(evidence.ports.readPoint).not.toHaveBeenCalled();
    expect(evidence.ports.read).not.toHaveBeenCalled();

    // A malformed summary item is a failure as well, not an empty owner.
    vi.mocked(evidence.ports.readPending!).mockResolvedValueOnce([
      { ...base, idempotency_key: "k", request_digest: "x" }]);
    await expect(entry.initialize(true)).rejects.toThrow(/摘要/);
    expect(entry.pending.value).toBeNull();
  });

  it("rejects a summary/point mismatch instead of restoring the wrong request", async () => {
    const { application, evidence } = applicationFixture();
    const workflowId = freshWorkflowId(), request = updateRequest(workflowId);
    const written = await evidence.ports.write({ operation, idempotency_key: request.idempotency_key,
      owner_kind: ownerKind, owner_id: workflowId, request: { ...graphClone(request) } });
    vi.mocked(evidence.ports.readPoint!).mockResolvedValueOnce([
      { ...written, request_digest: "b".repeat(64) }]);
    const entry = application.forWorkflow(workflowId);
    await expect(entry.initialize()).rejects.toThrow(/摘要与精确证据不同|无法核对/);
    expect(entry.locked.value).toBe(true);
    expect(entry.pending.value).toBeNull();
    expect(evidence.ports.read).not.toHaveBeenCalled();
  });

  it("keeps the five-step save chain with point reads for both confirmations", async () => {
    const { application, evidence, update } = applicationFixture();
    const workflowId = freshWorkflowId(), request = updateRequest(workflowId);
    const entry = application.forWorkflow(workflowId);
    await entry.initialize();
    const result = await entry.submit(request);
    expect(result).toMatchObject({ outcome: "saved", definition_revision: 1, edit_sequence: 1 });
    expect(evidence.ports.readPending).toHaveBeenCalledTimes(1);
    expect(evidence.ports.read).not.toHaveBeenCalled();
    expect(evidence.ports.write).toHaveBeenCalledTimes(1);
    expect(evidence.ports.readPoint).toHaveBeenCalledTimes(2);
    expect(update).toHaveBeenCalledTimes(1);
    expect(evidence.ports.resolve).toHaveBeenCalledTimes(1);
    const written = await vi.mocked(evidence.ports.write).mock.results[0]!.value;
    for (const call of vi.mocked(evidence.ports.readPoint).mock.calls)
      expect(call.slice(2)).toEqual([operation, written.idempotency_key]);
    const order = [
      ...vi.mocked(evidence.ports.write).mock.invocationCallOrder.map(value => ({ value, name: "write" })),
      ...vi.mocked(evidence.ports.readPoint).mock.invocationCallOrder
        .map((value, index) => ({ value, name: `point:${index}` })),
      ...update.mock.invocationCallOrder.map(value => ({ value, name: "update" })),
      ...vi.mocked(evidence.ports.resolve).mock.invocationCallOrder.map(value => ({ value, name: "resolve" })),
    ].sort((left, right) => left.value - right.value).map(event => event.name);
    expect(order).toEqual(["write", "point:0", "update", "point:1", "resolve"]);
    expect(entry.pending.value).toBeNull();
    expect(entry.progress.value).toBe("completed");
    expect(application.accepted(workflowId)).toMatchObject({ edit_sequence: 1 });
  });

  it("keeps a lost business response on the receipt route and never replays it", async () => {
    const { application, evidence, update, readReceipt } = applicationFixture();
    const workflowId = freshWorkflowId(), request = updateRequest(workflowId);
    const entry = application.forWorkflow(workflowId);
    await entry.initialize();
    update.mockRejectedValueOnce(new WorkbenchApiError("unknown", "response lost"));
    await expect(entry.submit(request)).rejects.toMatchObject({ kind: "unknown" });
    expect(update).toHaveBeenCalledTimes(1);
    expect(evidence.ports.read).not.toHaveBeenCalled();
    // The original request/key stays visible and the gate stays closed.
    expect(entry.pending.value).toEqual(request);
    expect(entry.locked.value).toBe(true);
    expect(entry.progress.value).toBe("result-unknown");
    // Verification is a read of the accepted receipt: no second business call.
    const reconciled = await entry.reconcile();
    expect(reconciled).toMatchObject({ outcome: "saved" });
    expect(update).toHaveBeenCalledTimes(1);
    expect(readReceipt).toHaveBeenCalledTimes(1);
    expect(evidence.ports.resolve).toHaveBeenCalledTimes(1);
    expect(entry.pending.value).toBeNull();
    expect(entry.progress.value).toBe("completed");
    expect(evidence.ports.read).not.toHaveBeenCalled();
  });

  it("keeps the accepted result when only evidence closure fails, without resubmitting", async () => {
    const { application, evidence, update } = applicationFixture();
    const workflowId = freshWorkflowId(), request = updateRequest(workflowId);
    const entry = application.forWorkflow(workflowId);
    await entry.initialize();
    evidence.ports.resolve.mockRejectedValueOnce(
      new WorkbenchApiError("unavailable", "closure unavailable"));
    await expect(entry.submit(request)).rejects.toBeInstanceOf(WorkbenchApiError);
    expect(update).toHaveBeenCalledTimes(1);
    expect(entry.progress.value).toBe("closure-failed");
    expect(entry.pending.value).toEqual(request);
    expect(entry.locked.value).toBe(true);
    expect(entry.lastResult.value).toMatchObject({ outcome: "saved" });
    expect(application.accepted(workflowId)).toMatchObject({ edit_sequence: 1 });
  });

  it("keeps the legacy full read for injected ports without narrow methods", async () => {
    const memory = evidenceStore();
    const legacy: RequestEvidencePorts = { read: memory.ports.read,
      write: memory.ports.write, resolve: memory.ports.resolve };
    const workflowId = freshWorkflowId(), request = updateRequest(workflowId);
    await memory.ports.write({ operation, idempotency_key: request.idempotency_key,
      owner_kind: ownerKind, owner_id: workflowId, request: { ...graphClone(request) } });
    const restored = await readPendingEvidence(legacy, ownerKind, workflowId);
    expect(restored).toMatchObject({ idempotency_key: request.idempotency_key,
      resolution: "pending" });
    expect(restored!.request).toEqual(request);
    expect(memory.ports.read).toHaveBeenCalledTimes(1);
  });

  it("uses the narrow reads through the shipped adapter transport", async () => {
    const workflowId = freshWorkflowId(), request = updateRequest(workflowId);
    const record: RequestEvidenceRecord = { schema_version: 1, kind: "workflow.request-evidence",
      operation, idempotency_key: request.idempotency_key, owner_kind: ownerKind,
      owner_id: workflowId, request_digest: digestOf(request), resolution: "pending",
      request: { ...graphClone(request) } };
    const pending: PendingRequestEvidence = { owner_kind: ownerKind, owner_id: workflowId,
      operation, idempotency_key: request.idempotency_key,
      request_digest: record.request_digest, resolution: "pending" };
    const transport = stubTransport(request_ => {
      if (request_.operation === "request-evidence.pending")
        return { body: { schema_version: 1, kind: "workflow.request-evidence-page",
          items: [pending], next_cursor: null } };
      if (request_.operation === "request-evidence.read")
        return { body: [record] };
      throw new Error(`unexpected operation ${request_.operation}`);
    });
    const restored = await readPendingEvidence(requestEvidencePorts, ownerKind, workflowId);
    expect(restored).toEqual(record);
    const evidenceCalls = transport.requests.filter(request_ =>
      request_.operation.startsWith("request-evidence."));
    expect(evidenceCalls.map(request_ => request_.operation)).toEqual([
      "request-evidence.pending", "request-evidence.read"]);
    expect(evidenceCalls[0]!.parameters).toMatchObject({ owner_kind: ownerKind,
      owner_id: workflowId, limit: 2 });
    expect(evidenceCalls[1]!.parameters).toMatchObject({ owner_kind: ownerKind,
      owner_id: workflowId, operation, idempotency_key: request.idempotency_key });
    expect(evidenceCalls[0]!.path).toBe("/api/graph/queries");
  });
});

// ---------------------------------------------------------------------------
// V4-04: frozen definition reuse by formal identity and fresh summary reads.
// ---------------------------------------------------------------------------
function seedEntry(graph: ReturnType<typeof useWorkflowGraphStore>, workflowId: string,
  document: GraphDocument) {
  const workspace = useWorkspaceStore();
  (workspace.workflows as unknown as Record<string, unknown>[]).push({
    id: workflowId, title: "S4 acceptance", description: "", nodeCount: document.nodes.length,
    state: "saved" });
  workspace.activeWorkflowId = workflowId;
  workspace.selectedWorkflowId = workflowId;
  (graph.entries as unknown as Record<string, unknown>)[workflowId] = {
    document: graphClone(document), saved_document: graphClone(document),
    saved_revision: document.revision, latest_published_revision: document.revision,
    session_id: null };
  return graph.entries[workflowId]!;
}

describe("V4-04 frozen definition reuse and summary freshness", () => {
  it("reuses a successful frozen read by (definition id, revision) and never caches failures", async () => {
    const graph = useWorkflowGraphStore();
    const identity = crypto.randomUUID();
    let failRevision: number | null = null;
    const transport = stubTransport(request_ => {
      if (request_.operation !== "definition.read")
        throw new Error(`unexpected operation ${request_.operation}`);
      const revision = Number(request_.parameters.revision);
      const documentIdentity = String(request_.parameters.identity);
      if (failRevision === revision) throw new Error("transport failed");
      return { body: frozenDocument(documentIdentity, revision, `title r${revision}`) };
    });
    const first = await graph.readFrozenDefinition(identity, 1);
    expect(first.revision).toBe(1);
    expect(transport.calls("definition.read")).toHaveLength(1);
    // The same formal identity is served from the reuse cache: no second download.
    const again = await graph.readFrozenDefinition(identity, 1);
    expect(again).toEqual(first);
    expect(transport.calls("definition.read")).toHaveLength(1);
    // A later mutation of the returned copy cannot poison the cached document.
    again.nodes[0]!.title = "mutated copy";
    expect((await graph.readFrozenDefinition(identity, 1)).nodes[0]!.title).toBe("title r1");
    // A different revision or definition is a different formal identity.
    expect((await graph.readFrozenDefinition(identity, 2)).revision).toBe(2);
    const otherIdentity = crypto.randomUUID();
    expect((await graph.readFrozenDefinition(otherIdentity, 1)).workflow_definition_id)
      .toBe(otherIdentity);
    expect(transport.calls("definition.read")).toHaveLength(3);
    // A failure is displayed as missing and retried later; it is never cached.
    failRevision = 3;
    await expect(graph.readFrozenDefinition(identity, 3)).rejects.toThrow();
    failRevision = null;
    expect((await graph.readFrozenDefinition(identity, 3)).revision).toBe(3);
    expect(transport.calls("definition.read").filter(request_ =>
      request_.parameters.revision === 3)).toHaveLength(2);
  });

  it("keeps the frozen definition cache bounded and evicts the oldest identity", async () => {
    const graph = useWorkflowGraphStore();
    const identity = crypto.randomUUID();
    const transport = stubTransport(request_ => ({ body: frozenDocument(
      String(request_.parameters.identity), Number(request_.parameters.revision)) }));
    for (let revision = 1; revision <= 8; revision += 1)
      await graph.readFrozenDefinition(identity, revision);
    expect(transport.calls("definition.read")).toHaveLength(8);
    // Re-reading the most recent identity is still a cache hit.
    await graph.readFrozenDefinition(identity, 8);
    expect(transport.calls("definition.read")).toHaveLength(8);
    // A ninth distinct identity evicts the oldest entry, so it is read again later.
    await graph.readFrozenDefinition(identity, 9);
    expect(transport.calls("definition.read")).toHaveLength(9);
    await graph.readFrozenDefinition(identity, 1);
    expect(transport.calls("definition.read")).toHaveLength(10);
  });

  it("reads the latest fixed revision from a fresh summary and never keeps a stale value", async () => {
    const graph = useWorkflowGraphStore();
    const workflowId = crypto.randomUUID();
    const document = frozenDocument(workflowId, 3);
    let fixedRevision = 5;
    const transport = stubTransport(request_ => {
      if (request_.operation !== "definition.summaries")
        throw new Error(`unexpected operation ${request_.operation}`);
      return { body: [{ workflow_definition_id: workflowId, display_name: "S4 acceptance",
        fixed_definition_revision: fixedRevision, edit_sequence: 7, archived: false,
        node_count: 1, session_count: 0, has_current_configuration: true }] };
    });
    const entry = seedEntry(graph, workflowId, document);
    await settledLocks(graph);
    expect(await graph.refreshPublishedRevision(workflowId)).toBe(true);
    expect(entry.latest_published_revision).toBe(5);
    expect(transport.calls("definition.summaries")).toHaveLength(1);
    // A "latest" question always issues a summary read; the local value is not reused.
    fixedRevision = 6;
    expect(await graph.refreshPublishedRevision(workflowId)).toBe(true);
    expect(entry.latest_published_revision).toBe(6);
    expect(transport.calls("definition.summaries")).toHaveLength(2);
    expect(transport.calls("definition.summaries")
      .every(request_ => request_.parameters.include_archived === true)).toBe(true);
  });

  it("reports summary read failures without moving the local fixed revision", async () => {
    const graph = useWorkflowGraphStore();
    const workflowId = crypto.randomUUID();
    const document = frozenDocument(workflowId, 4);
    const transport = stubTransport(request_ => {
      if (request_.operation !== "definition.summaries")
        throw new Error(`unexpected operation ${request_.operation}`);
      return { status: 503, body: { error: { code: "storage_unavailable", message: "busy" } } };
    });
    const entry = seedEntry(graph, workflowId, document);
    entry.latest_published_revision = 9;
    await settledLocks(graph);
    const notices = useWorkbenchNoticesStore();
    const before = notices.notices.length;
    expect(await graph.refreshPublishedRevision(workflowId)).toBe(false);
    expect(entry.latest_published_revision).toBe(9);
    expect(notices.notices.length).toBeGreaterThan(before);
    expect(transport.calls("definition.summaries")).toHaveLength(1);
  });

  it("does not apply a late summary response to a different accepted entry", async () => {
    const graph = useWorkflowGraphStore();
    const workflowId = crypto.randomUUID();
    const document = frozenDocument(workflowId, 2);
    // The management outbox initializes its evidence read before the delayed read.
    stubTransport(() => ({ body: [] }));
    const entry = seedEntry(graph, workflowId, document);
    await settledLocks(graph);
    let resolveResponse: (() => void) | null = null;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => {
      resolveResponse = () => resolve(new Response(JSON.stringify([{
        workflow_definition_id: workflowId, display_name: "S4 acceptance",
        fixed_definition_revision: 8, edit_sequence: 9, archived: false,
        node_count: 1, session_count: 0, has_current_configuration: true }])));
    })));
    const refreshing = graph.refreshPublishedRevision(workflowId);
    await vi.waitFor(() => expect(resolveResponse).not.toBeNull());
    // The accepted entry was replaced while the read was in flight (switch/refresh).
    (graph.entries as unknown as Record<string, unknown>)[workflowId] = {
      document: graphClone(document), saved_document: graphClone(document), saved_revision: 2,
      latest_published_revision: 2, session_id: null };
    resolveResponse!();
    expect(await refreshing).toBe(false);
    expect(entry.latest_published_revision).toBe(2);
    expect(graph.entries[workflowId]!.latest_published_revision).toBe(2);
  });

  it("wires the history panel to the reuse path and keeps the missing/retry display", () => {
    const source = readFileSync(new URL("../components/GraphHistory.vue", import.meta.url), "utf8");
    expect(source).toContain("readFrozenDefinition");
    expect(source).toContain("run(sessionId, chainId)");
    expect(source).toContain("definitionRetry");
    expect(source).toContain("未使用当前编辑图替代");
    expect(source).toContain("frozenDefinition.value?.revision !== revision");
  });
});
