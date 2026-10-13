import { afterEach, describe, expect, it, vi } from "vitest";
import { listProviderModels, providerConfigurationPorts, type ProviderCredentialRequest } from "./providerConfigurationApi";
import { newProvider, providerIdentity } from "../domain/workflowModelResources";

const provider = newProvider();
const { credential_ref: _reference, ...configuration } = provider.value;
const request: ProviderCredentialRequest = { identity: providerIdentity(provider), expected_sequence: 1,
  provider_seed: configuration, credential_ref: `local:${crypto.randomUUID()}`, expected_generation: 0, idempotency_key: crypto.randomUUID() };
const credential = { schema_version: 1, kind: "workflow.credential-status", credential_ref: request.credential_ref,
  generation: 1, state: "saved" };
const result = { schema_version: 1, kind: "workflow.provider-credential-save", outcome: "saved",
  reason_code: "provider_credential_bound", credential, provider: { reference: request.identity, update_sequence: 2, deleted: false } };
const requestDigest = "a".repeat(64);
function envelope(operation = "provider.credential.save", value = result) {
  return { schema_version: 1, kind: "workflow.application-command", result: structuredClone(value), receipt: {
    schema_version: 1, kind: "workflow.application-receipt", operation,
    operation_scope: "management", idempotency_key: request.idempotency_key, authority: "service_receipt",
    request_sha256: requestDigest, target: {}, accepted: structuredClone(value),
  } };
}
function response(value: unknown) { vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value)))); }
afterEach(() => vi.unstubAllGlobals());
describe("provider configuration API contracts", () => {
  it("accepts combined evidence independent of object-key order and sends the Key only in the command", async () => {
    const value = envelope();
    value.receipt.accepted.credential = { state: "saved", generation: 1, credential_ref: request.credential_ref,
      kind: "workflow.credential-status", schema_version: 1 };
    response(value);
    expect(await providerConfigurationPorts.saveBound(request, "dummy-key", requestDigest)).toEqual(result);
    const body = JSON.parse(String(vi.mocked(fetch).mock.calls[0]![1]!.body));
    expect(body).toEqual({ operation: "provider.credential.save", parameters: { ...request, secret: "dummy-key" } });
  });
  it.each(["schema", "kind", "target", "extra", "accepted", "generation", "identity", "scope", "operation", "provider", "digest"])(
    "refuses a %s receipt mismatch", async mode => {
      const value = envelope();
      if (mode === "schema") value.receipt.schema_version = 9;
      if (mode === "kind") value.receipt.kind = "forged";
      if (mode === "target") Object.assign(value.receipt.target, { session_id: crypto.randomUUID() });
      if (mode === "extra") Object.assign(value.receipt, { secret: "unexpected" });
      if (mode === "accepted") value.receipt.accepted.credential.state = "missing";
      if (mode === "generation") value.receipt.accepted.credential.generation = 2;
      if (mode === "identity") value.receipt.idempotency_key = crypto.randomUUID();
      if (mode === "scope") value.receipt.operation_scope = "consumer";
      if (mode === "operation") value.receipt.operation = "provider.credential.bind";
      if (mode === "provider") value.receipt.accepted.provider.update_sequence = 3;
      if (mode === "digest") value.receipt.request_sha256 = "b".repeat(64);
      response(value);
      await expect(providerConfigurationPorts.saveBound(request, "dummy-key", requestDigest)).rejects.toMatchObject({ kind: "unknown" });
    });
  it("keeps a partial or unresolved combined outcome distinct from success", async () => {
    for (const outcome of ["partial", "unresolved"]) {
      const value = { ...result, outcome, reason_code: "provider_binding_not_applied", provider: null };
      response(envelope("provider.credential.save", value as unknown as typeof result));
      expect(await providerConfigurationPorts.saveBound(request, "dummy-key", requestDigest)).toMatchObject({ outcome, provider: null });
    }
    response(envelope("provider.credential.save", { ...result, provider: null } as unknown as typeof result));
    await expect(providerConfigurationPorts.saveBound(request, "dummy-key", requestDigest)).rejects.toMatchObject({ kind: "unknown" });
  });
  it("queries only the non-secret original request on reconciliation", async () => {
    response(result);
    await providerConfigurationPorts.boundReceipt(request);
    const body = JSON.parse(String(vi.mocked(fetch).mock.calls[0]![1]!.body));
    expect(body).toEqual({ operation: "provider.credential.receipt", parameters: { request } });
    expect(JSON.stringify(body)).not.toContain("secret");
  });
  it("keeps only read-only evidence lookup for an earlier binding request", async () => {
    const saved = { ...request, expected_generation: 1 };
    response(result);
    expect(await providerConfigurationPorts.boundReceipt(saved, true)).toEqual(result);
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0]![1]!.body)))
      .toEqual({ operation: "provider.credential.bind-receipt", parameters: { request: saved } });
    expect(vi.mocked(fetch).mock.calls[0]![0]).toBe("/api/graph/queries");
    expect("bind" in providerConfigurationPorts).toBe(false);
  });
  it("rejects invalid or incomplete provider bases before dispatch", async () => {
    response(result);
    await expect(providerConfigurationPorts.saveBound({ ...request, provider_seed: provider.value }, "dummy-key", requestDigest))
      .rejects.toMatchObject({ kind: "rejected" });
    await expect(providerConfigurationPorts.saveBound({ ...request, provider_seed: null }, "dummy-key", requestDigest))
      .rejects.toMatchObject({ kind: "rejected" });
    expect(fetch).not.toHaveBeenCalled();
  });
  it("retains a strict delete-only receipt path", async () => {
    const { credential_ref, expected_generation, idempotency_key } = request;
    const key = { credential_ref, expected_generation, idempotency_key }, deleted = { ...credential, state: "missing" };
    const value = { ...envelope("credential.delete"), result: deleted, receipt: { ...envelope("credential.delete").receipt, accepted: deleted } };
    response(value);
    expect(await providerConfigurationPorts.delete(key, requestDigest)).toEqual(deleted);
    response(deleted);
    await providerConfigurationPorts.receipt(key);
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0]![1]!.body)))
      .toEqual({ operation: "credential.receipt", parameters: { operation: "credential.delete", request: key } });
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
