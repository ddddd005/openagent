import { afterEach, describe, expect, it, vi } from "vitest";
import { isModelProfileSaveRequest, listModelProfiles, readModelProfileReceipt, saveModelProfile,
  type ModelProfileSaveRequest } from "./workflowModelProfilesApi";
import { modelProfileIdentity, modelProfileType, newModelProfile, newProvider,
  type CurrentModelProfile } from "../domain/workflowModelResources";

afterEach(() => vi.unstubAllGlobals());

function profile(): CurrentModelProfile {
  const value = newModelProfile(newProvider());
  value.value.name = "Shared model configuration";
  value.value.parameters.model = "offline-test-chat";
  value.value.capacity.context_window_tokens = 4096;
  value.value.capacity.max_cold_input_tokens = 2048;
  return value;
}
function request(): ModelProfileSaveRequest {
  return { record: profile(), expected_sequence: 0, idempotency_key: crypto.randomUUID() };
}
function acceptance(body: ModelProfileSaveRequest) {
  return { reference: modelProfileIdentity(body.record), update_sequence: body.record.update_sequence, deleted: false };
}
function command(body: ModelProfileSaveRequest, result: unknown = acceptance(body),
  receiptChanges: Record<string, unknown> = {}) {
  return { schema_version: 1, kind: "workflow.application-command", result,
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: "resource.save",
      operation_scope: "management", idempotency_key: body.idempotency_key,
      request_sha256: "a".repeat(64), authority: "service_receipt", target: {},
      accepted: result, ...receiptChanges } };
}
function matched(body: ModelProfileSaveRequest, result: unknown = acceptance(body),
  receiptChanges: Record<string, unknown> = {}) {
  const value = command(body, result, receiptChanges);
  return { schema_version: 1, kind: "workflow.application-receipt-read", outcome: "matched",
    reason_code: "receipt_matched", result: value.result, receipt: value.receipt };
}
function respond(value: unknown) {
  const fetcher = vi.fn(async (_path: string, _options: RequestInit) => new Response(JSON.stringify(value)));
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

describe("global named model configuration API", () => {
  it("lists only model profiles and preserves same-UUID configurations in different scopes", async () => {
    const workspace = profile(), project = { ...profile(), scope: "project", resource_id: workspace.resource_id };
    const fetcher = respond([workspace, project]);
    expect(await listModelProfiles()).toEqual([workspace, project]);
    expect(fetcher).toHaveBeenCalledOnce();
    expect(fetcher.mock.calls[0]).toEqual(["/api/graph/queries", expect.objectContaining({
      method: "POST", body: JSON.stringify({ operation: "resource.list", parameters: { type_id: modelProfileType } }),
    })]);
  });

  it.each(["provider", "mixed", "old-schema", "unconfigured-capacity", "non-array"])(
    "rejects an invalid model configuration list: %s", async kind => {
      const current = profile();
      const values: Record<string, unknown> = {
        provider: [newProvider()], mixed: [current, newProvider()],
        "old-schema": [{ ...current, data_schema_version: 2 }],
        "unconfigured-capacity": [newModelProfile(newProvider())], "non-array": current,
      };
      const fetcher = respond(values[kind]);
      await expect(listModelProfiles()).rejects.toMatchObject({ kind: "unavailable" });
      expect(fetcher).toHaveBeenCalledOnce();
    });

  it("saves the complete manual configuration with an exact CAS body and no discovery call", async () => {
    const body = request();
    body.record.scope = "project";
    body.record.update_sequence = 8;
    body.expected_sequence = 7;
    body.record.value.parameters.temperature = 0.25;
    body.record.value.capacity = {
      context_window_tokens: 32000, output_reserve_tokens: 2048,
      summary_max_tokens: 512, max_cold_input_tokens: 12345,
    };
    const original = JSON.stringify(body), fetcher = respond(command(body));
    expect(await saveModelProfile(body)).toEqual(acceptance(body));
    expect(JSON.stringify(body)).toBe(original);
    expect(fetcher).toHaveBeenCalledOnce();
    expect(fetcher.mock.calls[0]).toEqual(["/api/graph/commands", expect.objectContaining({
      method: "POST", body: JSON.stringify({ operation: "resource.save", parameters: body }),
    })]);
  });

  it.each(["expected-negative", "expected-fraction", "sequence", "idempotency", "uppercase-idempotency",
    "extra", "provider-type", "capacity", "inline-secret"])(
    "rejects malformed original requests before save or receipt dispatch: %s", async field => {
      const body = request();
      const changed = JSON.parse(JSON.stringify(body)) as Record<string, any>;
      if (field === "expected-negative") changed.expected_sequence = -1;
      if (field === "expected-fraction") changed.expected_sequence = 0.5;
      if (field === "sequence") changed.expected_sequence = 1;
      if (field === "idempotency") changed.idempotency_key = "not-a-uuid";
      if (field === "uppercase-idempotency") changed.idempotency_key = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA";
      if (field === "extra") changed.extra = true;
      if (field === "provider-type") changed.record.type_id = "workflow.chat-provider";
      if (field === "capacity") changed.record.value.capacity.context_window_tokens = 0;
      if (field === "inline-secret") changed.record.value.secret = "not-a-configuration-field";
      const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
      expect(isModelProfileSaveRequest(changed)).toBe(false);
      await expect(saveModelProfile(changed as ModelProfileSaveRequest)).rejects.toMatchObject({ kind: "rejected" });
      await expect(readModelProfileReceipt(changed as ModelProfileSaveRequest)).rejects.toMatchObject({ kind: "rejected" });
      expect(fetcher).not.toHaveBeenCalled();
    });

  it.each(["envelope", "scope", "type", "uuid", "sequence", "boolean-sequence", "deleted", "missing", "extra"])(
    "does not accept a mismatched resource result: %s", async field => {
      const body = request(), original = acceptance(body);
      const wrong: Record<string, unknown> = { ...original };
      if (field === "envelope") wrong.reference = { ...original.reference, envelope_version: 2 };
      if (field === "scope") wrong.reference = { ...original.reference, scope: "project" };
      if (field === "type") wrong.reference = { ...original.reference, type_id: "workflow.chat-provider" };
      if (field === "uuid") wrong.reference = { ...original.reference, resource_id: crypto.randomUUID() };
      if (field === "sequence") wrong.update_sequence = 2;
      if (field === "boolean-sequence") wrong.update_sequence = true;
      if (field === "deleted") wrong.deleted = true;
      if (field === "missing") delete wrong.deleted;
      if (field === "extra") wrong.extra = true;
      let calls = 0;
      vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(
        ++calls === 1 ? command(body, wrong) : matched(body, wrong)))));
      await expect(saveModelProfile(body)).rejects.toMatchObject({ kind: "unknown" });
      await expect(readModelProfileReceipt(body)).rejects.toMatchObject({ kind: "unknown" });
      expect(calls).toBe(2);
    });

  it.each(["schema_version", "kind", "operation", "operation_scope", "idempotency_key",
    "request_sha256", "authority", "target", "accepted"])(
    "requires every application receipt field: %s", async field => {
      const body = request(), value = command(body);
      delete (value.receipt as Record<string, unknown>)[field];
      const fetcher = respond(value);
      await expect(saveModelProfile(body)).rejects.toMatchObject({ kind: "unknown" });
      expect(fetcher).toHaveBeenCalledOnce();
    });

  it.each(["operation", "scope", "key", "digest", "authority", "target", "accepted"])(
    "rejects inconsistent application receipt evidence: %s", async field => {
      const body = request();
      const changes: Record<string, Record<string, unknown>> = {
        operation: { operation: "resource.delete" }, scope: { operation_scope: "consumer" },
        key: { idempotency_key: crypto.randomUUID() }, digest: { request_sha256: "invalid" },
        authority: { authority: "service_result" }, target: { target: { session_id: crypto.randomUUID() } },
        accepted: { accepted: { ...acceptance(body), update_sequence: 2 } },
      };
      respond(matched(body, acceptance(body), changes[field]));
      await expect(readModelProfileReceipt(body)).rejects.toMatchObject({ kind: "unknown" });
    });

  it("reads only the original receipt and accepts normalized nested object key order", async () => {
    const body = request(), result = acceptance(body), reference = result.reference;
    const reordered = { deleted: false, reference: { resource_id: reference.resource_id,
      type_id: reference.type_id, scope: reference.scope, envelope_version: reference.envelope_version },
      update_sequence: result.update_sequence };
    const fetcher = respond(matched(body, result, { accepted: reordered }));
    expect(await readModelProfileReceipt(body)).toEqual(result);
    expect(fetcher).toHaveBeenCalledOnce();
    expect(fetcher.mock.calls[0]).toEqual(["/api/graph/receipts/read", expect.objectContaining({
      method: "POST", body: JSON.stringify({ operation: "resource.save", parameters: body }),
    })]);
  });

  it("preserves an unresolved original request without issuing a mutation", async () => {
    const body = request(), original = JSON.stringify(body);
    const fetcher = respond({ schema_version: 1, kind: "workflow.application-receipt-read",
      outcome: "unresolved", reason_code: "application_identity_missing" });
    await expect(readModelProfileReceipt(body)).rejects.toMatchObject({
      kind: "unknown", code: "application_identity_missing",
    });
    expect(JSON.stringify(body)).toBe(original);
    expect(fetcher).toHaveBeenCalledOnce();
    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/graph/receipts/read");
  });

  it.each(["save", "read"] as const)("does not retry a failed %s transport", async operation => {
    const body = request(), fetcher = vi.fn(async () => { throw new Error("offline"); });
    vi.stubGlobal("fetch", fetcher);
    await expect((operation === "save" ? saveModelProfile : readModelProfileReceipt)(body))
      .rejects.toMatchObject({ kind: operation === "save" ? "unknown" : "unavailable" });
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it.each([400, 409, 500])("retains definitive rejection versus unknown submission at HTTP %s", async status => {
    const body = request(), fetcher = vi.fn(async () => new Response(JSON.stringify({
      error: { reason_code: "stale_revision" },
    }), { status }));
    vi.stubGlobal("fetch", fetcher);
    await expect(saveModelProfile(body)).rejects.toMatchObject({
      kind: status >= 500 ? "unknown" : "rejected", status, code: "stale_revision",
    });
    expect(fetcher).toHaveBeenCalledOnce();
  });
});
