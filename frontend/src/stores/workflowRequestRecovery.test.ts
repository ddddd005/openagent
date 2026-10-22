import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import * as evidenceApi from "../adapters/requestEvidenceApi";
import * as commandApi from "../adapters/workflowApplicationApi";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { createWorkflowConfigurationCopy } from "../application/workflowConfigurationCopy";
import { graphClone } from "../domain/workflowGraph";
import type { GraphConfigurationResult, GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import type { WorkflowConfigurationCopyRequest, WorkflowConfigurationCopyResult } from "../domain/workflowConfigurationCopy";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkspaceStore } from "./workspace";

let pinia: Pinia;
beforeEach(() => { pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => { disposePinia(pinia); vi.restoreAllMocks(); });
function summary(record: evidenceApi.RequestEvidenceRecord): evidenceApi.PendingRequestEvidence {
  const { owner_kind, owner_id, operation, idempotency_key, request_digest } = record;
  return { owner_kind, owner_id, operation, idempotency_key, request_digest, resolution: "pending" };
}
function fixture() {
  const evidence = memoryRequestEvidence();
  vi.spyOn(evidenceApi.requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(evidenceApi.requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(evidenceApi.requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
  const discovery = vi.spyOn(evidenceApi, "readPendingWorkflowEvidence");
  return { evidence, graph, workspace, discovery };
}
function configurationRequest(owner = crypto.randomUUID()): GraphConfigurationUpdateRequest {
  return { workflow_definition_id: owner, expected_edit_sequence: 0, idempotency_key: crypto.randomUUID(),
    document: { schema_version: 3, workflow_definition_id: owner, revision: 1,
      name: "Historical", nodes: [], edges: [], object_bindings: [] } };
}
function configurationResult(request: GraphConfigurationUpdateRequest): GraphConfigurationResult {
  return { schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
    source_workflow_definition_id: request.workflow_definition_id, workflow_definition_id: request.workflow_definition_id,
    definition_revision: 1, edit_sequence: 1, source_has_effective_session: false, changed_categories: ["fixed"],
    node_id_map: {}, session_id: null, document: { ...graphClone(request.document), edit_sequence: 1 } };
}
describe("workflow original request discovery and recovery", () => {
  it("discovers bounded pages, preserves unsupported operations, and allows refresh after a failure", async () => {
    const state = fixture(), request = configurationRequest();
    const record = await state.evidence.ports.write({ owner_kind: "workflow-configuration",
      owner_id: request.workflow_definition_id, operation: "future.operation", idempotency_key: request.idempotency_key,
      request: { ...request } });
    const item = summary(record), { resolution: _resolution, request_digest: _digest, ...cursor } = item;
    state.discovery.mockRejectedValueOnce(new Error("summary unavailable"));
    expect(await state.graph.discoverPendingRequests()).toBe(false);
    expect(state.graph.pendingDiscoveryError).toBe("summary unavailable");
    state.discovery.mockResolvedValueOnce({ schema_version: 1, kind: "workflow.request-evidence-page",
      items: [item], next_cursor: cursor });
    expect(await state.graph.discoverPendingRequests()).toBe(true);
    expect(state.graph.pendingDiscoveryError).toBe("");
    expect(state.graph.supportedDiscoveredRequest(item)).toBe(false);
    expect(await state.graph.reconcileDiscoveredRequest(item)).toBe(false);
    expect(state.evidence.ports.resolve).not.toHaveBeenCalled();
    state.discovery.mockResolvedValueOnce({ schema_version: 1, kind: "workflow.request-evidence-page",
      items: [], next_cursor: null });
    expect(await state.graph.discoverPendingRequests(true)).toBe(true);
    expect(state.discovery).toHaveBeenLastCalledWith({ limit: 50, cursor });
    expect(state.graph.discoveredRequests).toEqual([item]);
  });
  it("verifies a first-save owner absent from the directory without reconstructing its historical document", async () => {
    const state = fixture(), request = configurationRequest(), send = vi.fn();
    const receipt = vi.fn(async () => configurationResult(request));
    const app = createWorkflowConfigurationApplication({ evidence: state.evidence.ports, extensions: () => [],
      update: send, readReceipt: receipt });
    state.graph.installWorkflowConfigurationApplication(app);
    const record = await state.evidence.ports.write({ owner_kind: "workflow-configuration",
      owner_id: request.workflow_definition_id, operation: "definition.update",
      idempotency_key: request.idempotency_key, request: { ...request } });
    state.graph.discoveredRequests = [summary(record)];
    expect(await state.graph.reconcileDiscoveredRequest(summary(record))).toBe(true);
    expect(receipt).toHaveBeenCalledWith(request);
    expect(send).not.toHaveBeenCalled();
    expect(app.accepted(request.workflow_definition_id)).toBeNull();
    expect(state.graph.entries[request.workflow_definition_id]).toBeUndefined();
    expect(state.workspace.workflows.some(row => row.id === request.workflow_definition_id)).toBe(false);
    expect(app.forWorkflow(request.workflow_definition_id).pending.value).toBeNull();
    expect([...state.evidence.records.values()][0]).toMatchObject({
      idempotency_key: record.idempotency_key, request_digest: record.request_digest, resolution: "verified",
    });
  });
  it("verifies an isolated deleted owner without loading or reviving an editor entry", async () => {
    const owner = crypto.randomUUID(), key = crypto.randomUUID();
    const parameters = { identity: owner, expected_revision: 3, idempotency_key: key };
    const receipt = vi.spyOn(commandApi, "readGraphReceipt").mockResolvedValue({
      schema_version: 1, kind: "workflow.definition-management", workflow_definition_id: owner,
      revision: 3, archived: false, deleted: true,
    });
    const send = vi.spyOn(commandApi, "sendGraphCommand");
    const state = fixture();
    const record = await state.evidence.ports.write({ owner_kind: "workflow-management", owner_id: owner,
      operation: "definition.delete", idempotency_key: key, request: parameters });
    expect(await state.graph.reconcileDiscoveredRequest(summary(record))).toBe(true);
    expect(receipt).toHaveBeenCalledWith(`/api/graph/definitions/${owner}/delete`,
      { expected_revision: 3, idempotency_key: key });
    expect(send).not.toHaveBeenCalled();
    expect(state.graph.entries[owner]).toBeUndefined();
    expect(state.workspace.workflows.some(row => row.id === owner)).toBe(false);
  });
  it("verifies a copy whose source is absent without restoring source or target history", async () => {
    const state = fixture(), sourceId = crypto.randomUUID(), targetId = crypto.randomUUID();
    const request: WorkflowConfigurationCopyRequest = { source_workflow_definition_id: sourceId,
      expected_source_definition_revision: 3, expected_source_edit_sequence: 5,
      target_workflow_definition_id: targetId, target_name: "Original copy", idempotency_key: crypto.randomUUID() };
    const result: WorkflowConfigurationCopyResult = { schema_version: 1, kind: "workflow.configuration-copy-result",
      outcome: "copied", source_workflow_definition_id: sourceId, source_definition_revision: 3,
      source_edit_sequence: 5, workflow_definition_id: targetId, definition_revision: 1, edit_sequence: 1,
      session_id: null, node_id_map: {}, document: { schema_version: 3, workflow_definition_id: targetId,
        revision: 1, edit_sequence: 1, name: request.target_name, nodes: [], edges: [], object_bindings: [] } };
    const send = vi.fn(), accept = vi.fn(), receipt = vi.fn(async () => result);
    state.graph.installWorkflowConfigurationCopyApplication(
      createWorkflowConfigurationCopy({ evidence: state.evidence.ports, send, readReceipt: receipt, accept }));
    const record = await state.evidence.ports.write({ owner_kind: "workflow-configuration-copy", owner_id: sourceId,
      operation: "definition.copy", idempotency_key: request.idempotency_key, request: { ...request } });
    expect(await state.graph.reconcileDiscoveredRequest(summary(record))).toBe(true);
    expect(send).not.toHaveBeenCalled();
    expect(receipt).toHaveBeenCalledWith(request);
    expect(state.graph.entries[sourceId]).toBeUndefined();
    expect(state.graph.entries[targetId]).toBeUndefined();
    expect(state.workspace.workflows.some(row => row.id === sourceId || row.id === targetId)).toBe(false);
  });
  it("refuses a stale A summary after B became pending and leaves B unchanged", async () => {
    const state = fixture(), first = configurationRequest();
    const a = await state.evidence.ports.write({ owner_kind: "workflow-configuration",
      owner_id: first.workflow_definition_id, operation: "definition.update",
      idempotency_key: first.idempotency_key, request: { ...first } });
    await state.evidence.ports.resolve({ operation: a.operation, idempotency_key: a.idempotency_key,
      request_digest: a.request_digest, resolution: "verified" });
    const second = { ...first, idempotency_key: crypto.randomUUID() };
    const b = await state.evidence.ports.write({ owner_kind: a.owner_kind, owner_id: a.owner_id,
      operation: a.operation, idempotency_key: second.idempotency_key, request: second });
    vi.mocked(state.evidence.ports.resolve).mockClear();
    const receipt = vi.spyOn(commandApi, "readGraphReceipt");
    expect(await state.graph.reconcileDiscoveredRequest(summary(a))).toBe(false);
    expect(receipt).not.toHaveBeenCalled();
    expect(state.evidence.ports.resolve).not.toHaveBeenCalled();
    expect([...state.evidence.records.values()].find(row => row.idempotency_key === b.idempotency_key)).toEqual(b);
    expect(Object.values(state.graph.recoveryErrors)[0]).toContain("坐标不同");
  });
  it("keeps body corruption visible and does not resolve or resend it", async () => {
    const state = fixture(), request = configurationRequest();
    const record = await state.evidence.ports.write({ owner_kind: "workflow-configuration",
      owner_id: request.workflow_definition_id, operation: "definition.update",
      idempotency_key: request.idempotency_key, request: { ...request } });
    vi.mocked(evidenceApi.requestEvidencePorts.read).mockRejectedValue(new Error("原请求正文损坏"));
    expect(await state.graph.reconcileDiscoveredRequest(summary(record))).toBe(false);
    expect(Object.values(state.graph.recoveryErrors)[0]).toBe("原请求正文损坏");
    expect(state.evidence.ports.resolve).not.toHaveBeenCalled();
    expect([...state.evidence.records.values()][0]).toEqual(record);
  });
  it("rechecks the digest loaded by the channel before reading any receipt", async () => {
    const state = fixture(), request = configurationRequest(), receipt = vi.fn();
    const app = createWorkflowConfigurationApplication({ evidence: state.evidence.ports, extensions: () => [],
      readReceipt: receipt });
    state.graph.installWorkflowConfigurationApplication(app);
    const record = await state.evidence.ports.write({ owner_kind: "workflow-configuration",
      owner_id: request.workflow_definition_id, operation: "definition.update",
      idempotency_key: request.idempotency_key, request: { ...request } });
    vi.mocked(evidenceApi.requestEvidencePorts.read).mockResolvedValueOnce([record]);
    vi.mocked(state.evidence.ports.read).mockResolvedValueOnce([{ ...record, request_digest: "f".repeat(64) }]);
    expect(await state.graph.reconcileDiscoveredRequest(summary(record))).toBe(false);
    expect(receipt).not.toHaveBeenCalled();
    expect(state.evidence.ports.resolve).not.toHaveBeenCalled();
  });
  it("loads and verifies a later B on an already initialized owner after A was settled", async () => {
    const state = fixture(), first = configurationRequest(), send = vi.fn();
    const receipt = vi.fn(async (request: GraphConfigurationUpdateRequest) => configurationResult(request));
    const app = createWorkflowConfigurationApplication({ evidence: state.evidence.ports, extensions: () => [],
      update: send, readReceipt: receipt });
    state.graph.installWorkflowConfigurationApplication(app);
    const a = await state.evidence.ports.write({ owner_kind: "workflow-configuration",
      owner_id: first.workflow_definition_id, operation: "definition.update",
      idempotency_key: first.idempotency_key, request: { ...first } });
    expect(await state.graph.reconcileDiscoveredRequest(summary(a))).toBe(true);
    const second = { ...first, idempotency_key: crypto.randomUUID() };
    const b = await state.evidence.ports.write({ owner_kind: a.owner_kind, owner_id: a.owner_id,
      operation: a.operation, idempotency_key: second.idempotency_key, request: second });
    state.graph.discoveredRequests = [summary(b)];
    expect(state.graph.discoveredPendingRequests()).toEqual([summary(b)]);
    expect(await state.graph.reconcileDiscoveredRequest(summary(b))).toBe(true);
    expect(receipt).toHaveBeenLastCalledWith(second);
    expect(send).not.toHaveBeenCalled();
    expect(app.accepted(first.workflow_definition_id)).toBeNull();
    expect([...state.evidence.records.values()].every(row => row.resolution === "verified")).toBe(true);
  });
});
