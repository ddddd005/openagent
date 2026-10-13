import { afterEach, describe, expect, it, vi } from "vitest";
import { updateGraphConfiguration, readGraphConfigurationReceipt } from "./workflowConfigurationApi";
import { graphClone } from "../domain/workflowGraph";
import type { GraphConfigurationResult, GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";

function fixture() {
  const workflowId = crypto.randomUUID();
  const request: GraphConfigurationUpdateRequest = {
    workflow_definition_id: workflowId, expected_edit_sequence: 0, idempotency_key: crypto.randomUUID(),
    document: { schema_version: 3, workflow_definition_id: workflowId, revision: 1, name: "Incomplete",
      nodes: [], edges: [], object_bindings: [], package_lock: [{ package_id: "missing.example", version: "1.0.0" }] },
  };
  const result: GraphConfigurationResult = {
    schema_version: 1, kind: "workflow.configuration-result", outcome: "saved",
    source_workflow_definition_id: workflowId, workflow_definition_id: workflowId,
    definition_revision: 1, edit_sequence: 1, source_has_effective_session: false,
    changed_categories: ["fixed"], node_id_map: {}, session_id: null,
    document: { ...graphClone(request.document), edit_sequence: 1 },
  };
  const accepted = Object.fromEntries(["outcome", "source_workflow_definition_id", "workflow_definition_id",
    "definition_revision", "edit_sequence", "source_has_effective_session", "changed_categories", "node_id_map", "session_id"]
    .map(key => [key, result[key as keyof GraphConfigurationResult]]));
  const receipt = { schema_version: 1, kind: "workflow.application-receipt", operation: "definition.update",
    operation_scope: "management", idempotency_key: request.idempotency_key, request_sha256: "a".repeat(64),
    authority: "service_receipt", target: {}, accepted };
  return { request, result, receipt };
}
afterEach(() => vi.unstubAllGlobals());
describe("formal configuration application transport", () => {
  it("submits one complete incomplete/missing-package document through definition.update", async () => {
    const { request, result, receipt } = fixture();
    const fetch = vi.fn(async (_path: string, _init?: RequestInit) => new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.application-command", result, receipt,
    })));
    vi.stubGlobal("fetch", fetch);
    await expect(updateGraphConfiguration(request)).resolves.toEqual(result);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]?.[0]).toBe("/api/graph/commands");
    const captured = JSON.parse(String((fetch.mock.calls[0] as unknown as [string, RequestInit])[1].body));
    expect(captured).toEqual({ operation: "definition.update", parameters: request });
  });
  it("reads the exact original receipt without resubmitting or consulting current configuration", async () => {
    const { request, result, receipt } = fixture();
    const fetch = vi.fn(async (_path: string, _init?: RequestInit) => new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.application-receipt-read", outcome: "matched",
      reason_code: "receipt_matched", receipt, result,
    })));
    vi.stubGlobal("fetch", fetch);
    await expect(readGraphConfigurationReceipt(request)).resolves.toEqual(result);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]?.[0]).toBe("/api/graph/receipts/read");
  });
  it("preserves an unresolved request and never resends it", async () => {
    const { request } = fixture();
    const fetch = vi.fn(async () => new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.application-receipt-read", outcome: "unresolved",
      reason_code: "native_receipt_missing",
    })));
    vi.stubGlobal("fetch", fetch);
    await expect(readGraphConfigurationReceipt(request)).rejects.toMatchObject({ kind: "unknown" });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("rejects a receipt whose accepted edit sequence differs from its result", async () => {
    const { request, result, receipt } = fixture();
    receipt.accepted.edit_sequence = 9;
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.application-command", result, receipt,
    }))));
    await expect(updateGraphConfiguration(request)).rejects.toMatchObject({ kind: "unknown" });
  });
  it("rejects a truthful envelope with a result belonging to another source", async () => {
    const { request, result, receipt } = fixture();
    result.source_workflow_definition_id = crypto.randomUUID();
    receipt.accepted.source_workflow_definition_id = result.source_workflow_definition_id;
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.application-command", result, receipt,
    }))));
    await expect(updateGraphConfiguration(request)).rejects.toMatchObject({ kind: "unknown" });
  });
  it("reports a genuine CAS rejection without another write or read", async () => {
    const { request } = fixture();
    const fetch = vi.fn(async () => new Response(JSON.stringify({
      error: { reason_code: "stale_edit_sequence",
        diagnostics: [{ code: "stale_edit_sequence", message: "Configuration changed" }] },
    }), { status: 409 }));
    vi.stubGlobal("fetch", fetch);
    await expect(updateGraphConfiguration(request)).rejects.toMatchObject({
      kind: "rejected", code: "stale_edit_sequence", diagnostics: [{ message: "Configuration changed", reason_code: "stale_edit_sequence" }],
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
