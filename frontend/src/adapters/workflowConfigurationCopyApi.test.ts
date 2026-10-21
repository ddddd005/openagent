import { afterEach, describe, expect, it, vi } from "vitest";
import { readConfigurationCopyReceipt, submitConfigurationCopy } from "./workflowConfigurationCopyApi";
import { graphClone, type GraphDocument } from "../domain/workflowGraph";
import {
  workflowConfigurationCopyAccepted, workflowConfigurationCopyTargetName,
  type WorkflowConfigurationCopyRequest, type WorkflowConfigurationCopyResult,
} from "../domain/workflowConfigurationCopy";

const sourceId = "3f0e3a2a-2f5f-4b7c-9d1e-5a6b7c8d9e0f";
const targetId = "8a7b6c5d-4e3f-4a2b-9c1d-0e9f8a7b6c5d";

function request(overrides: Partial<WorkflowConfigurationCopyRequest> = {}): WorkflowConfigurationCopyRequest {
  return { source_workflow_definition_id: sourceId, expected_source_definition_revision: 3,
    expected_source_edit_sequence: 5, target_workflow_definition_id: targetId,
    target_name: "Workflow（副本）", idempotency_key: crypto.randomUUID(), ...overrides };
}
function document(target: string, name: string): GraphDocument {
  return { schema_version: 3, workflow_definition_id: target, revision: 1, edit_sequence: 1, name,
    nodes: [], edges: [], object_bindings: [] } as unknown as GraphDocument;
}
function result(requested: WorkflowConfigurationCopyRequest): WorkflowConfigurationCopyResult {
  return {
    schema_version: 1, kind: "workflow.configuration-copy-result", outcome: "copied",
    source_workflow_definition_id: requested.source_workflow_definition_id,
    source_definition_revision: requested.expected_source_definition_revision,
    source_edit_sequence: requested.expected_source_edit_sequence,
    workflow_definition_id: requested.target_workflow_definition_id,
    definition_revision: 1, edit_sequence: 1, session_id: null, node_id_map: {},
    document: document(requested.target_workflow_definition_id, requested.target_name) as never,
  };
}
function envelope(requested: WorkflowConfigurationCopyRequest, copied = result(requested)) {
  return { schema_version: 1, kind: "workflow.application-command", result: graphClone(copied),
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: "definition.copy",
      operation_scope: "management", idempotency_key: requested.idempotency_key,
      request_sha256: "b".repeat(64), authority: "service_receipt", target: {},
      accepted: workflowConfigurationCopyAccepted(copied) } };
}
const response = (value: unknown) => new Response(JSON.stringify(value));
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("formal configuration copy transport", () => {
  it("sends only the six formal source/target fields through the generic command route", async () => {
    const requested = request(), calls: { path: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
      calls.push({ path, body: JSON.parse(String(init?.body)) });
      return response(envelope(requested));
    }));
    expect(await submitConfigurationCopy(requested)).toEqual(result(requested));
    expect(calls).toHaveLength(1);
    expect(calls[0]!.path).toBe("/api/graph/commands");
    expect(calls[0]!.body).toEqual({ operation: "definition.copy", parameters: requested });
    expect(Object.keys((calls[0]!.body as { parameters: object }).parameters).sort()).toEqual([
      "expected_source_definition_revision", "expected_source_edit_sequence", "idempotency_key",
      "source_workflow_definition_id", "target_name", "target_workflow_definition_id",
    ]);
  });
  it("rejects an accepted projection that omits or alters the source/target versions", async () => {
    const requested = request();
    const value = envelope(requested);
    value.receipt.accepted = { ...value.receipt.accepted, source_edit_sequence: 4 };
    vi.stubGlobal("fetch", vi.fn(async () => response(value)));
    await expect(submitConfigurationCopy(requested)).rejects.toMatchObject({ kind: "unknown" });
  });
  it("never accepts a copy result carrying a session or a non-1 initial count", async () => {
    for (const mutate of [
      (copied: WorkflowConfigurationCopyResult) => ({ ...copied, session_id: crypto.randomUUID() }),
      (copied: WorkflowConfigurationCopyResult) => ({ ...copied, edit_sequence: 2 }),
      (copied: WorkflowConfigurationCopyResult) => ({ ...copied, definition_revision: 2 }),
    ]) {
      const requested = request();
      vi.stubGlobal("fetch", vi.fn(async () => response(envelope(requested, mutate(result(requested)) as never))));
      await expect(submitConfigurationCopy(requested)).rejects.toMatchObject({ kind: "unknown" });
    }
  });
  it("rejects a request carrying a document or editor state before transport", async () => {
    const requested = { ...request(), document: document(targetId, "Workflow（副本）") };
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await expect(submitConfigurationCopy(requested as never)).rejects.toMatchObject({ kind: "rejected" });
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("reads a matched independent receipt without any other transport", async () => {
    const requested = request(), calls: { path: string; body: unknown }[] = [];
    const copied = result(requested);
    vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
      calls.push({ path, body: JSON.parse(String(init?.body)) });
      return response({ schema_version: 1, kind: "workflow.application-receipt-read",
        outcome: "matched", reason_code: "receipt_matched", receipt: envelope(requested, copied).receipt,
        result: copied });
    }));
    expect(await readConfigurationCopyReceipt(requested)).toEqual(copied);
    expect(calls).toEqual([{ path: "/api/graph/receipts/read",
      body: { operation: "definition.copy", parameters: requested } }]);
  });
  it("keeps an unresolved independent receipt as unknown with its reason code", async () => {
    const requested = request();
    vi.stubGlobal("fetch", vi.fn(async () => response({ schema_version: 1,
      kind: "workflow.application-receipt-read", outcome: "unresolved", reason_code: "receipt_not_found" })));
    await expect(readConfigurationCopyReceipt(requested)).rejects.toMatchObject({
      kind: "unknown", code: "receipt_not_found" });
  });
  it("never substitutes the accepted projection for the independent receipt result", async () => {
    const requested = request();
    vi.stubGlobal("fetch", vi.fn(async () => response({ schema_version: 1,
      kind: "workflow.application-receipt-read", outcome: "matched", reason_code: "receipt_matched",
      receipt: envelope(requested).receipt, result: workflowConfigurationCopyAccepted(result(requested)) })));
    await expect(readConfigurationCopyReceipt(requested)).rejects.toMatchObject({ kind: "unknown" });
  });
  it("builds a deterministic bounded copy name", () => {
    expect(workflowConfigurationCopyTargetName("工作流")).toBe("工作流（副本）");
    const long = "名".repeat(200);
    const name = workflowConfigurationCopyTargetName(long);
    expect([...name].length).toBe(128);
    expect(name.endsWith("（副本）")).toBe(true);
    expect(workflowConfigurationCopyTargetName("  空间  ")).toBe("空间（副本）");
  });
});
