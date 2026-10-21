import { afterEach, describe, expect, it, vi } from "vitest";
import { createWorkflowConfigurationCopy } from "./workflowConfigurationCopy";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone, type GraphDocument } from "../domain/workflowGraph";
import { workflowConfigurationCopyAccepted,
  type WorkflowConfigurationCopyRequest, type WorkflowConfigurationCopyResult } from "../domain/workflowConfigurationCopy";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";

const sourceId = "3f0e3a2a-2f5f-4b7c-9d1e-5a6b7c8d9e0f";
const targetId = "8a7b6c5d-4e3f-4a2b-9c1d-0e9f8a7b6c5d";

function request(overrides: Partial<WorkflowConfigurationCopyRequest> = {}): WorkflowConfigurationCopyRequest {
  return { source_workflow_definition_id: sourceId, expected_source_definition_revision: 3,
    expected_source_edit_sequence: 5, target_workflow_definition_id: targetId,
    target_name: "Workflow（副本）", idempotency_key: crypto.randomUUID(), ...overrides };
}
function document(target: string, name: string): WorkflowConfigurationCopyResult["document"] {
  return { schema_version: 3, workflow_definition_id: target, revision: 1, edit_sequence: 1, name,
    nodes: [], edges: [], object_bindings: [] } as unknown as GraphDocument as never;
}
function result(requested: WorkflowConfigurationCopyRequest): WorkflowConfigurationCopyResult {
  return {
    schema_version: 1, kind: "workflow.configuration-copy-result", outcome: "copied",
    source_workflow_definition_id: requested.source_workflow_definition_id,
    source_definition_revision: requested.expected_source_definition_revision,
    source_edit_sequence: requested.expected_source_edit_sequence,
    workflow_definition_id: requested.target_workflow_definition_id,
    definition_revision: 1, edit_sequence: 1, session_id: null, node_id_map: {},
    document: document(requested.target_workflow_definition_id, requested.target_name),
  };
}
function fixture() {
  const evidence = memoryRequestEvidence();
  const send = vi.fn(async (sent: WorkflowConfigurationCopyRequest) => result(sent));
  const readReceipt = vi.fn(async (sent: WorkflowConfigurationCopyRequest) => result(sent));
  const accept = vi.fn(), completed = vi.fn();
  const application = createWorkflowConfigurationCopy({ evidence: evidence.ports, send, readReceipt,
    current: () => true, accept, completed });
  return { evidence, send, readReceipt, accept, completed,
    channel: application.forSource(sourceId) };
}
const writeRecord = (evidence: ReturnType<typeof memoryRequestEvidence>) =>
  [...evidence.records.values()][0];
afterEach(() => { vi.restoreAllMocks(); });

describe("formal configuration copy request custody", () => {
  it("persists the exact original request before a single formal send and resolves after acceptance", async () => {
    const state = fixture(), requested = request();
    expect(await state.channel.submit(requested)).toBe(true);
    expect(state.evidence.ports.write).toHaveBeenCalledWith({
      operation: "definition.copy", idempotency_key: requested.idempotency_key,
      owner_kind: "workflow-configuration-copy", owner_id: sourceId, request: requested,
    });
    expect(state.send).toHaveBeenCalledOnce();
    expect(state.send.mock.calls[0]![0]).toEqual(requested);
    expect(state.accept).toHaveBeenCalledOnce();
    expect(state.accept.mock.calls[0]![0]).toEqual(requested);
    expect(state.accept.mock.calls[0]![2]).toBe(false);
    expect(writeRecord(state.evidence)).toMatchObject({ resolution: "verified" });
    expect(state.channel.pending.value).toBeNull();
    expect(state.completed).toHaveBeenCalledOnce();
  });
  it("does not send when the original evidence cannot be persisted", async () => {
    const state = fixture();
    state.evidence.ports.write = vi.fn(async () => {
      throw new WorkbenchApiError("unavailable", "evidence storage unavailable");
    });
    expect(await state.channel.submit(request())).toBe(false);
    expect(state.send).not.toHaveBeenCalled();
    expect(state.accept).not.toHaveBeenCalled();
    expect(state.channel.error.value).toContain("未提交");
  });
  it("keeps an unknown copy as a pending original request and only reads its receipt", async () => {
    const state = fixture(), requested = request();
    state.send.mockRejectedValueOnce(new WorkbenchApiError("unknown", "response lost"));
    expect(await state.channel.submit(requested)).toBe(false);
    expect(state.channel.pending.value).toEqual(requested);
    expect(state.channel.error.value).toContain("response lost");
    expect(writeRecord(state.evidence)).toMatchObject({ resolution: "pending", operation: "definition.copy" });
    expect(state.accept).not.toHaveBeenCalled();

    expect(await state.channel.reconcile()).toBe(true);
    expect(state.readReceipt).toHaveBeenCalledOnce();
    expect(state.readReceipt.mock.calls[0]![0]).toEqual(requested);
    expect(state.send).toHaveBeenCalledOnce();
    expect(state.accept.mock.calls[0]![2]).toBe(true);
    expect(state.evidence.records.size).toBe(1);
    expect(writeRecord(state.evidence)).toMatchObject({ resolution: "verified" });
    expect(state.channel.pending.value).toBeNull();
  });
  it("blocks a second copy from the same source while one original request is pending", async () => {
    const state = fixture();
    state.send.mockRejectedValueOnce(new WorkbenchApiError("unknown", "response lost"));
    expect(await state.channel.submit(request())).toBe(false);
    const second = request({ target_workflow_definition_id: crypto.randomUUID() });
    expect(await state.channel.submit(second)).toBe(false);
    expect(state.send).toHaveBeenCalledOnce();
    expect(state.evidence.records.size).toBe(1);
  });
  it("keeps the confirmed business fact when evidence closing fails", async () => {
    const state = fixture(), requested = request();
    const write = state.evidence.ports.write, resolve = state.evidence.ports.resolve;
    state.evidence.ports.write = vi.fn(async value => write(value));
    state.evidence.ports.resolve = vi.fn(async () => {
      throw new Error("evidence disk unavailable");
    });
    expect(await state.channel.submit(requested)).toBe(false);
    expect(state.accept).toHaveBeenCalledOnce();
    expect(state.channel.pending.value).toEqual(requested);
    expect(state.channel.error.value).toContain("evidence disk unavailable");
    expect(writeRecord(state.evidence)).toMatchObject({ resolution: "pending" });
    state.evidence.ports.resolve = vi.fn(async value => resolve(value));
    expect(await state.channel.reconcile()).toBe(true);
    expect(state.evidence.records.size).toBe(1);
    expect(writeRecord(state.evidence)).toMatchObject({ resolution: "verified" });
  });
  it("accepts a late matched receipt for the same original request without resending", async () => {
    const state = fixture(), requested = request();
    state.send.mockRejectedValueOnce(new WorkbenchApiError("unknown", "response lost"));
    expect(await state.channel.submit(requested)).toBe(false);
    const before = graphClone(state.channel.pending.value);
    expect(await state.channel.reconcile()).toBe(true);
    expect(graphClone(state.channel.pending.value)).toBeNull();
    expect(before).toEqual(requested);
    expect(state.readReceipt.mock.calls[0]![0]).toEqual(requested);
    expect(state.send).toHaveBeenCalledOnce();
  });
  it("projects the formal result only after the result shape was validated", async () => {
    const state = fixture(), requested = request();
    const invalid = { ...result(requested), session_id: crypto.randomUUID() } as never;
    state.send.mockResolvedValueOnce(invalid);
    expect(await state.channel.submit(requested)).toBe(false);
    expect(state.accept).not.toHaveBeenCalled();
    expect(state.channel.pending.value).toEqual(requested);
  });
});
