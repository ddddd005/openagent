import { afterEach, describe, expect, it, vi } from "vitest";
import { listProviderModels, providerConfigurationPorts } from "./providerConfigurationApi";
import { newProvider, providerIdentity } from "../domain/workflowModelResources";

const request = { credential_ref: `local:${crypto.randomUUID()}` as const,
  expected_generation: 0, idempotency_key: crypto.randomUUID() };
const result = { schema_version: 1, kind: "workflow.credential-status", credential_ref: request.credential_ref,
  generation: 1, state: "saved" };
function envelope() {
  return { schema_version: 1, kind: "workflow.application-command", result, receipt: {
    schema_version: 1, kind: "workflow.application-receipt", operation: "credential.save",
    operation_scope: "management", idempotency_key: request.idempotency_key, authority: "service_receipt",
    request_sha256: "a".repeat(64), target: {}, accepted: { ...result },
  } };
}
function response(value: unknown) { vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value)))); }
afterEach(() => vi.unstubAllGlobals());
describe("provider configuration API contracts", () => {
  it("accepts structurally identical evidence independent of object-key order", async () => {
    const value = envelope();
    value.receipt.accepted = { state: "saved", generation: 1, credential_ref: request.credential_ref,
      kind: "workflow.credential-status", schema_version: 1 };
    response(value);
    expect(await providerConfigurationPorts.save(request, "dummy-key")).toEqual(result);
  });
  it.each(["schema", "kind", "target", "extra", "accepted", "generation", "identity", "scope"])("refuses a %s receipt mismatch", async mode => {
    const value = envelope();
    if (mode === "schema") value.receipt.schema_version = 9;
    if (mode === "kind") value.receipt.kind = "forged";
    if (mode === "target") Object.assign(value.receipt.target, { session_id: crypto.randomUUID() });
    if (mode === "extra") Object.assign(value.receipt, { secret: "unexpected" });
    if (mode === "accepted") value.receipt.accepted.state = "missing";
    if (mode === "generation") value.receipt.accepted.generation = 2;
    if (mode === "identity") value.receipt.idempotency_key = crypto.randomUUID();
    if (mode === "scope") value.receipt.operation_scope = "consumer";
    response(value);
    await expect(providerConfigurationPorts.save(request, "dummy-key")).rejects.toMatchObject({ kind: "unknown" });
  });
  it("queries only the non-secret original request on reconciliation", async () => {
    response(result); await providerConfigurationPorts.receipt(request);
    const fetcher = vi.mocked(fetch), body = JSON.parse(String(fetcher.mock.calls[0]![1]!.body));
    expect(body).toEqual({ operation: "credential.receipt", parameters: { operation: "credential.save", request } });
    expect(JSON.stringify(body)).not.toContain("secret");
  });
  it("rejects models from another provider sequence or protocol and preserves independent limits", async () => {
    const identity = providerIdentity(newProvider());
    const value = { schema_version: 1, kind: "workflow.provider-models", reference: identity, update_sequence: 3,
      protocol: "gemini", models: [{ model: "gemini-2.5-flash", adapted: true, input_token_limit: 1000,
        output_token_limit: 100, context_window_tokens: null }] };
    response(value);
    expect(await listProviderModels(identity, 3, "gemini")).toEqual(value.models);
    await expect(listProviderModels(identity, 4, "gemini")).rejects.toThrow();
    await expect(listProviderModels(identity, 3, "chat")).rejects.toThrow();
  });
});
