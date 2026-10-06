import { afterEach, describe, expect, it, vi } from "vitest";
import { listCurrentProviders, readCurrentProvider, saveCurrentProvider } from "./workflowResourcesApi";
import { newProvider, providerIdentity } from "../domain/workflowModelResources";
afterEach(() => vi.unstubAllGlobals());
describe("public current provider resources API", () => {
  it("queries only current chat-provider resources and validates exact read identities", async () => {
    const record = newProvider(), identity = providerIdentity(record);
    const fetcher = vi.fn(async (_path: string, options: RequestInit) => {
      const request = JSON.parse(String(options.body));
      return new Response(JSON.stringify(request.operation === "resource.list" ? [record] : record));
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await listCurrentProviders()).toEqual([record]); expect(await readCurrentProvider(identity)).toEqual(record);
    expect(JSON.parse(String(fetcher.mock.calls[0]![1].body))).toEqual({
      operation: "resource.list", parameters: { type_id: "workflow.chat-provider" } });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ...record, resource_id: crypto.randomUUID() }))));
    await expect(readCurrentProvider(identity)).rejects.toThrow("身份");
  });
  it("uses the named public resource operation and requires the original identity/key/sequence receipt", async () => {
    const record = newProvider(), request = { record, expected_sequence: 0, idempotency_key: crypto.randomUUID() };
    const result = { reference: providerIdentity(record), update_sequence: 1, deleted: false };
    let wrong = false;
    const fetcher = vi.fn(async (_path: string, options: RequestInit) => {
      expect(JSON.parse(String(options.body))).toEqual({ operation: "resource.save", parameters: request });
      return new Response(JSON.stringify({ schema_version: 1, kind: "workflow.application-command", result,
        receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: "resource.save",
          operation_scope: "management", idempotency_key: wrong ? crypto.randomUUID() : request.idempotency_key,
          authority: "service_receipt", request_sha256: "a".repeat(64), target: {}, accepted: result } }));
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await saveCurrentProvider(request)).toEqual(result);
    expect(fetcher.mock.calls[0]![0]).toBe("/api/graph/commands");
    wrong = true; await expect(saveCurrentProvider(request)).rejects.toMatchObject({ kind: "unknown" });
  });
});
