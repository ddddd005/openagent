import { afterEach, describe, expect, it, vi } from "vitest";
import { graphCommandOperation, readGraphReceipt, sendGraphCommand } from "./workflowApplicationApi";
import { readCurrentProviderReceipt } from "./workflowResourcesApi";
import { readCurrentPromptReceipt } from "./promptResourcesApi";
import { graphClone } from "../domain/workflowGraph";
import { newProvider, providerIdentity } from "../domain/workflowModelResources";
import { newPromptResource, promptIdentity } from "../domain/workflowPromptResources";
import { graphReceiptReadResponse, stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";

afterEach(() => vi.unstubAllGlobals());

function fixture() {
  const sessionId = crypto.randomUUID();
  const path = `/api/graph/sessions/${sessionId}/runs`;
  const body = { expected_revision: 1, idempotency_key: crypto.randomUUID(),
    inputs: { original: { metadata: [true, null, 1.25], text: "frozen" } } };
  const command = graphCommandOperation(path, body);
  const result = { workflow_definition_id: crypto.randomUUID(), definition_revision: 1,
    workflow_session_id: sessionId, revision: 2, data_revision: 0, head_revision: 1,
    status: "running", active_chain_run_id: crypto.randomUUID() };
  return { path, body, command, result };
}

describe("receipt-only graph transport", () => {
  it("refuses new rebind writes but reads the frozen retired request only through receipts", async () => {
    const sessionId = crypto.randomUUID(), definitionId = crypto.randomUUID();
    const path = `/api/graph/sessions/${sessionId}/rebind`;
    const body = { definition_revision: 2, expected_revision: 3, expected_data_revision: 2,
      expected_head_revision: 1, mappings: [{ source_node_id: "old", target_node_id: "new", action: "reset" }],
      idempotency_key: crypto.randomUUID() };
    const original = graphClone(body);
    const operation = "session.rebind";
    const result = { workflow_definition_id: definitionId, definition_revision: 2,
      workflow_session_id: sessionId, revision: 4, data_revision: 2, head_revision: 2, status: "idle" };
    const fetcher = vi.fn(async (route: string, options: RequestInit) => {
      expect(route).toBe("/api/graph/receipts/read");
      expect(JSON.parse(String(options.body))).toEqual({ operation,
        parameters: { ...body, session_id: sessionId } });
      return graphReceiptReadResponse(operation, { ...body, session_id: sessionId }, result);
    });
    vi.stubGlobal("fetch", fetcher);

    expect(() => graphCommandOperation(path, body)).toThrow("已退役");
    await expect(sendGraphCommand(path, body)).rejects.toMatchObject({ kind: "rejected" });
    expect(await readGraphReceipt(path, body)).toEqual(result);
    expect(body).toEqual(original);
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it.each(["session.rebind", "candidate.select", "candidate.fork"])(
    "does not let the application test fixture invoke retired %s writes", async operation => {
    const handler = vi.fn(async () => new Response("{}"));
    stubGraphApplicationFetch(handler);
    const request = { operation, parameters: {
      session_id: crypto.randomUUID(), idempotency_key: crypto.randomUUID(),
    } };

    await expect(fetch("/api/graph/commands", { method: "POST", body: JSON.stringify(request) }))
      .rejects.toThrow(`Retired ${operation} writes`);
    expect(handler).not.toHaveBeenCalled();
  });

  it("refuses new candidate select/fork commands while retaining their old receipt coordinates", async () => {
    const sessionId = crypto.randomUUID(), candidateId = crypto.randomUUID();
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    for (const action of ["select", "fork"]) {
      const path = `/api/graph/sessions/${sessionId}/candidates/${action}`;
      const body = { candidate_id: candidateId, expected_revision: 3, expected_data_revision: 2,
        expected_head_revision: 1, idempotency_key: crypto.randomUUID() };
      expect(() => graphCommandOperation(path, body)).toThrow("已退役");
      await expect(sendGraphCommand(path, body)).rejects.toMatchObject({ kind: "rejected" });
      expect(fetcher).not.toHaveBeenCalled();
    }
  });

  it.each(["select", "fork"] as const)("maps an old candidate %s request only for read-only receipt reconciliation", async action => {
    const sessionId = crypto.randomUUID(), definitionId = crypto.randomUUID(), candidateId = crypto.randomUUID();
    const operation = `candidate.${action}`, path = `/api/graph/sessions/${sessionId}/candidates/${action}`;
    const body = { candidate_id: candidateId, expected_revision: 3, expected_data_revision: 2,
      expected_head_revision: 1, idempotency_key: crypto.randomUUID() };
    const result = { workflow_definition_id: definitionId, definition_revision: 1,
      workflow_session_id: action === "fork" ? crypto.randomUUID() : sessionId,
      revision: action === "fork" ? 1 : 4, data_revision: action === "fork" ? 0 : 3,
      head_revision: action === "fork" ? 1 : 2, status: "succeeded",
      selected_chain_run_id: crypto.randomUUID(),
      ...(action === "fork" ? { source: { kind: "fork_candidate", workflow_session_id: sessionId,
        candidate_commit_id: candidateId } } : {}) };
    const fetcher = vi.fn(async (route: string, options: RequestInit) => {
      expect(route).toBe("/api/graph/receipts/read");
      expect(JSON.parse(String(options.body))).toEqual({ operation,
        parameters: { ...body, session_id: sessionId } });
      return graphReceiptReadResponse(operation, { ...body, session_id: sessionId }, result);
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await readGraphReceipt(path, body)).toEqual(result);
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it("reads the exact frozen command without sending a mutation or altering its inputs", async () => {
    const { path, body, command, result } = fixture(), before = graphClone(body);
    const fetcher = vi.fn(async (route: string, options: RequestInit) => {
      expect(route).toBe("/api/graph/receipts/read");
      expect(options.method).toBe("POST"); expect(JSON.parse(String(options.body))).toEqual(command);
      return graphReceiptReadResponse(command.operation, command.parameters, result);
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await readGraphReceipt(path, body)).toEqual(result);
    expect(body).toEqual(before); expect(fetcher).toHaveBeenCalledOnce();
  });

  it.each(["application_identity_missing", "not_found", "request_mismatch", "insufficient_evidence",
    "unsupported", "invalid_receipt"])("does not turn %s into confirmation of non-acceptance", async reason_code => {
    const { path, body } = fixture();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.application-receipt-read", outcome: "unresolved", reason_code,
    }))));
    await expect(readGraphReceipt(path, body)).rejects.toMatchObject({ kind: "unknown", code: reason_code });
  });

  it.each(["extra", "reason", "result", "receipt-extra", "operation", "scope", "key",
    "target", "digest", "authority", "accepted"])("rejects a malformed matched %s", async defect => {
    const { path, body, command, result } = fixture();
    const value = await graphReceiptReadResponse(command.operation, command.parameters, result).json();
    if (defect === "extra") value.extra = true;
    if (defect === "reason") value.reason_code = "not_found";
    if (defect === "result") delete value.result;
    if (defect === "receipt-extra") value.receipt.extra = true;
    if (defect === "operation") value.receipt.operation = "run.control";
    if (defect === "scope") value.receipt.operation_scope = "consumer";
    if (defect === "key") value.receipt.idempotency_key = crypto.randomUUID();
    if (defect === "target") value.receipt.target.session_id = crypto.randomUUID();
    if (defect === "digest") value.receipt.request_sha256 = "invalid";
    if (defect === "authority") value.receipt.authority = "service_result";
    if (defect === "accepted") value.receipt.accepted.revision++;
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value))));
    await expect(readGraphReceipt(path, body)).rejects.toMatchObject({ kind: "unknown" });
  });

  it.each([
    { schema_version: 1, kind: "workflow.application-receipt-read", outcome: "unresolved", reason_code: "not_found", extra: true },
    { schema_version: 1, kind: "workflow.application-receipt-read", outcome: "unresolved", reason_code: "" },
    { schema_version: 1, kind: "workflow.application-receipt-read", outcome: "unresolved" },
    { schema_version: 1, kind: "workflow.application-receipt-read", outcome: "rejected", reason_code: "not_found" },
  ])("requires the exact unresolved envelope", async value => {
    const { path, body } = fixture();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value))));
    await expect(readGraphReceipt(path, body)).rejects.toMatchObject({ kind: "unknown" });
  });

  it.each([403, 404, 409, 410, 422, 500])("treats HTTP %s as a read failure, never a fresh command rejection", async status => {
    const { path, body } = fixture();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { reason_code: "stale_revision" } }), { status })));
    await expect(readGraphReceipt(path, body)).rejects.toMatchObject({ kind: "unavailable", status });
  });

  it.each(["network", "json"])("preserves read semantics on %s failure", async failure => {
    const { path, body } = fixture();
    vi.stubGlobal("fetch", vi.fn(async () => {
      if (failure === "network") throw new Error("disconnected");
      return new Response("not json");
    }));
    await expect(readGraphReceipt(path, body)).rejects.toMatchObject({ kind: "unavailable" });
  });

  it("does not dispatch malformed frozen coordinates", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    await expect(readGraphReceipt("/api/graph/sessions/not-a-uuid/runs", { idempotency_key: "original" }))
      .rejects.toMatchObject({ kind: "unknown" });
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("current resource receipt identity", () => {
  it.each(["provider", "prompt"] as const)("reads the original %s identity and CAS without saving again", async kind => {
    const record = kind === "provider" ? newProvider() : newPromptResource();
    const request = { record, expected_sequence: 0, idempotency_key: crypto.randomUUID() };
    const result = { reference: kind === "provider" ? providerIdentity(record as ReturnType<typeof newProvider>)
      : promptIdentity(record as ReturnType<typeof newPromptResource>), update_sequence: 1, deleted: false };
    const fetcher = vi.fn(async (path: string, options: RequestInit) => {
      expect(path).toBe("/api/graph/receipts/read");
      expect(JSON.parse(String(options.body))).toEqual({ operation: "resource.save", parameters: request });
      return graphReceiptReadResponse("resource.save", request, result);
    });
    vi.stubGlobal("fetch", fetcher);
    const value = kind === "provider"
      ? await readCurrentProviderReceipt(request as Parameters<typeof readCurrentProviderReceipt>[0])
      : await readCurrentPromptReceipt(request as Parameters<typeof readCurrentPromptReceipt>[0]);
    expect(value).toEqual(result); expect(fetcher).toHaveBeenCalledOnce();
  });

  it.each(["scope", "uuid", "type", "schema", "sequence", "deleted"])(
    "does not accept a matched response whose resource %s is wrong", async defect => {
      const record = newPromptResource(), request = { record, expected_sequence: 0, idempotency_key: crypto.randomUUID() };
      const result = { reference: promptIdentity(record), update_sequence: 1, deleted: false };
      const wrong = graphClone(result);
      if (defect === "scope") wrong.reference.scope = "project";
      if (defect === "uuid") wrong.reference.resource_id = crypto.randomUUID();
      if (defect === "type") Object.assign(wrong.reference, { type_id: "workflow.chat-provider" });
      if (defect === "schema") Object.assign(wrong.reference, { envelope_version: 2 });
      if (defect === "sequence") wrong.update_sequence++;
      if (defect === "deleted") wrong.deleted = true;
      vi.stubGlobal("fetch", vi.fn(async () => graphReceiptReadResponse("resource.save", request, wrong)));
      await expect(readCurrentPromptReceipt(request)).rejects.toMatchObject({ kind: "unknown" });
    });
});
