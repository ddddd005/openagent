import { describe, expect, it, vi } from "vitest";
import { createProviderCredentialEditor, type CredentialIntent } from "./providerCredentialEditing";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import type { CredentialRequest, CredentialStatus, ProviderCredentialRequest, ProviderCredentialResult } from "../adapters/providerConfigurationApi";
import type { RequestEvidenceRecord } from "../adapters/requestEvidenceApi";
import { graphClone } from "../domain/workflowGraph";
import { newProvider, providerIdentity } from "../domain/workflowModelResources";

const reference = `local:${crypto.randomUUID()}` as const;
const status = (generation = 1, state: CredentialStatus["state"] = "saved"): CredentialStatus => ({
  schema_version: 1, kind: "workflow.credential-status", credential_ref: reference, generation, state,
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
function fixture() {
  let record: RequestEvidenceRecord | null = null;
  const provider = newProvider();
  provider.value.credential_ref = reference;
  const { credential_ref: _reference, ...configuration } = provider.value;
  const basis = { identity: providerIdentity(provider), expected_sequence: 1, provider_seed: configuration };
  const result = (outcome: ProviderCredentialResult["outcome"] = "saved"): ProviderCredentialResult => ({
    schema_version: 1, kind: "workflow.provider-credential-save", outcome,
    reason_code: outcome === "saved" ? "provider_credential_saved" : "provider_binding_conflict",
    credential: status(2), provider: outcome === "saved"
      ? { reference: providerIdentity(provider), update_sequence: 2, deleted: false } : null,
  });
  const writes: unknown[] = [], saved = vi.fn();
  const channel = {
    read: vi.fn(async () => record?.resolution === "verified" ? null : record),
    archive: vi.fn(async (_value: CredentialIntent) => { if (record) record.resolution = "archived"; }),
    write: vi.fn(async (value: CredentialIntent | null, _format = 2) => {
      writes.push(graphClone(value));
      if (value === null) {
        if (!record) throw new Error("Missing original evidence");
        if (record.resolution === "pending") record.resolution = "verified";
        return graphClone(record);
      }
      record = { schema_version: 1, kind: "workflow.request-evidence",
        operation: value.kind === "delete" ? "credential.delete"
          : value.kind === "bind" ? "provider.credential.bind" : "provider.credential.save",
        idempotency_key: value.request.idempotency_key, owner_kind: "provider-credential", owner_id: "one",
        request: graphClone(value.request) as unknown as Record<string, unknown>,
        request_digest: "a".repeat(64), resolution: "pending" };
      return record;
    }),
  };
  const ports = {
    protection: vi.fn(async () => true), status: vi.fn(async () => status()),
    provider: vi.fn(async () => ({ ...graphClone(provider), update_sequence: 2 })),
    delete: vi.fn(async (_request: CredentialRequest, _digest: string) => status(2, "missing")),
    receipt: vi.fn(async (_request: CredentialRequest) => status(2, "missing")),
    saveBound: vi.fn(async (_request: ProviderCredentialRequest, _secret: string, _digest: string) => result()),
    boundReceipt: vi.fn(async (_request: ProviderCredentialRequest, _bind = false) => result()),
  };
  const editor = createProviderCredentialEditor(channel, saved, ports);
  return { editor, channel, ports, writes, saved, basis, provider, result,
    reopen: () => createProviderCredentialEditor(channel, saved, ports) };
}
describe("single-action private Key and effective provider binding", () => {
  it("persists accepted form basis before releasing custody and accepts only its own basis update", async () => {
    const f = fixture(), gate = deferred<void>();
    await f.editor.load(reference, f.basis);
    f.ports.status.mockResolvedValue(status(2));
    f.saved.mockImplementation(async () => {
      expect(f.editor.pending.value).not.toBeNull();
      expect(f.editor.adoptSavedBasis(reference, { ...f.basis, expected_sequence: 2 })).toBe(true);
      expect(f.editor.adoptSavedBasis(reference, { ...f.basis, expected_sequence: 3 })).toBe(false);
      await gate.promise;
    });
    f.editor.secret.value = "dummy-memory-key";
    const saving = f.editor.save();
    await vi.waitFor(() => expect(f.saved).toHaveBeenCalledOnce());
    expect(f.editor.blocked.value).toBe(true);
    expect(f.writes.at(-1)).not.toBeNull();
    gate.resolve();
    await saving;
    expect(f.editor.pending.value).toBeNull();
    expect(f.editor.error.value).toBe("供应商配置与 Key 已保存");
  });
  it("keeps a saved Key intent when accepted form persistence fails, then verifies without re-saving", async () => {
    const f = fixture();
    await f.editor.load(reference, f.basis);
    f.ports.status.mockResolvedValue(status(2));
    f.saved.mockRejectedValueOnce(new Error("Accepted form could not persist"));
    f.editor.secret.value = "dummy-memory-key";
    await f.editor.save();
    expect(f.editor.pending.value).not.toBeNull();
    expect(f.editor.error.value).toContain("could not persist");
    await f.editor.reconcile();
    expect(f.ports.saveBound).toHaveBeenCalledOnce();
    expect(f.editor.pending.value).toBeNull();
  });
  it("persists only non-secret intent then verifies Key and provider before reporting success", async () => {
    const f = fixture();
    await f.editor.load(reference, f.basis); f.editor.secret.value = "dummy-memory-key";
    f.ports.status.mockResolvedValue(status(2));
    await f.editor.save();
    expect(f.ports.saveBound).toHaveBeenCalledWith(expect.objectContaining({
      identity: f.basis.identity, expected_sequence: 1, provider_seed: f.basis.provider_seed,
      credential_ref: reference, expected_generation: 1,
    }), "dummy-memory-key", "a".repeat(64));
    expect(JSON.stringify(f.writes)).not.toContain("dummy-memory-key");
    expect(f.writes[0]).toMatchObject({ schema_version: 2, kind: "save", basis_reference: reference });
    expect(f.writes.at(-1)).toBeNull();
    expect(f.editor.secret.value).toBe(""); expect(f.editor.blocked.value).toBe(false);
    expect(f.saved).toHaveBeenCalledWith(reference, expect.objectContaining({ update_sequence: 2 }), expect.anything());
    expect(f.editor.error.value).toBe("供应商配置与 Key 已保存");
  });
  it("keeps the memory-only Key while editing the same draft and saves its latest whole configuration", async () => {
    const f = fixture();
    await f.editor.load(reference, f.basis);
    f.editor.secret.value = "dummy-memory-key";
    const changed = { ...f.basis, provider_seed: {
      ...f.basis.provider_seed, name: "Edited after Key", base_url: "https://edited.invalid/v1", enabled: false,
    } };
    expect(f.editor.updateDraftBasis(reference, changed)).toBe(true);
    expect(f.editor.secret.value).toBe("dummy-memory-key");
    f.ports.provider.mockResolvedValue({ ...f.provider, update_sequence: 2,
      value: { ...changed.provider_seed, credential_ref: reference } });
    f.ports.status.mockResolvedValue(status(2));
    await f.editor.save();
    expect(f.ports.saveBound).toHaveBeenCalledWith(expect.objectContaining({
      provider_seed: changed.provider_seed,
    }), "dummy-memory-key", "a".repeat(64));
    expect(f.saved).toHaveBeenCalledOnce();
    expect(JSON.stringify(f.writes)).not.toContain("dummy-memory-key");
  });
  it("refuses draft basis changes for a different owner, saved version or active submission", async () => {
    const f = fixture(), response = deferred<ProviderCredentialResult>();
    await f.editor.load(reference, f.basis);
    f.editor.secret.value = "dummy-memory-key";
    expect(f.editor.updateDraftBasis(null, f.basis)).toBe(false);
    expect(f.editor.updateDraftBasis(reference, { ...f.basis, expected_sequence: 2 })).toBe(false);
    expect(f.editor.updateDraftBasis(reference, { ...f.basis, identity: providerIdentity(newProvider()) })).toBe(false);
    f.ports.saveBound.mockReturnValueOnce(response.promise);
    const saving = f.editor.save();
    await vi.waitFor(() => expect(f.ports.saveBound).toHaveBeenCalledOnce());
    expect(f.editor.updateDraftBasis(reference, { ...f.basis,
      provider_seed: { ...f.basis.provider_seed, name: "Later edit" } })).toBe(false);
    response.resolve(f.result("partial"));
    await saving;
    expect(f.editor.pending.value!.request).toMatchObject({ provider_seed: f.basis.provider_seed });
  });
  it("never claims the whole configuration was saved when only its Key reference matches", async () => {
    const f = fixture();
    await f.editor.load(reference, f.basis);
    f.editor.secret.value = "dummy-memory-key";
    f.ports.status.mockResolvedValue(status(2));
    f.ports.provider.mockResolvedValue({ ...f.provider, update_sequence: 2,
      value: { ...f.provider.value, base_url: "https://wrong.invalid" } });
    await f.editor.save();
    expect(f.saved).not.toHaveBeenCalled();
    expect(f.editor.pending.value).not.toBeNull();
    expect(f.editor.blocked.value).toBe(true);
  });
  it("reopens unknown intent and reads only the exact original receipt without re-sending Key", async () => {
    const f = fixture(); await f.editor.load(reference, f.basis);
    f.ports.saveBound.mockRejectedValue(new WorkbenchApiError("unknown", "lost"));
    f.editor.secret.value = "dummy-memory-key"; await f.editor.save();
    const original = graphClone(f.editor.pending.value!.request);
    const reopened = f.reopen(); await reopened.load(reference, f.basis);
    expect(reopened.secret.value).toBe(""); expect(reopened.blocked.value).toBe(true);
    f.ports.status.mockResolvedValue(status(2));
    await reopened.reconcile();
    expect(f.ports.boundReceipt).toHaveBeenCalledWith(original, false);
    expect(f.ports.saveBound).toHaveBeenCalledOnce();
    expect(reopened.pending.value).toBeNull(); expect(f.saved).toHaveBeenCalledOnce();
  });
  it("retains confirmed originals when durable clearing fails, then reconciles without mutation", async () => {
    const f = fixture(); await f.editor.load(reference, f.basis);
    f.ports.status.mockResolvedValue(status(2));
    const write = f.channel.write.getMockImplementation()!;
    f.channel.write.mockImplementation(async (value, format) => {
      if (value === null) throw new Error("disk unavailable");
      return write(value, format);
    });
    f.editor.secret.value = "dummy-key"; await f.editor.save();
    expect(f.editor.pending.value).not.toBeNull(); expect(f.editor.blocked.value).toBe(true);
    f.channel.write.mockImplementation(write);
    await f.editor.reconcile();
    expect(f.ports.saveBound).toHaveBeenCalledOnce(); expect(f.saved).toHaveBeenCalledTimes(2);
  });
  it("does not dispatch when saved evidence differs from the original public request", async () => {
    const f = fixture(); await f.editor.load(reference, f.basis);
    const write = f.channel.write.getMockImplementation()!;
    f.channel.write.mockImplementationOnce(async (value, format) => ({
      ...await write(value, format), request_digest: "invalid",
    }));
    f.editor.secret.value = "dummy-key"; await f.editor.save();
    expect(f.ports.saveBound).not.toHaveBeenCalled();
    expect(f.saved).not.toHaveBeenCalled(); expect(f.editor.pending.value).not.toBeNull();
  });
  it("locks an unknown evidence write and only reads its exact identity without sending the Key", async () => {
    const f = fixture(); await f.editor.load(reference, f.basis);
    const write = f.channel.write.getMockImplementation()!;
    f.channel.write.mockImplementationOnce(async (value, format) => {
      await write(value, format);
      throw new WorkbenchApiError("unknown", "Controlled evidence write response loss");
    });
    f.ports.boundReceipt.mockRejectedValueOnce(new WorkbenchApiError("unknown", "Original request was never dispatched"));
    f.editor.secret.value = "dummy-key"; await f.editor.save();
    const original = graphClone(f.editor.pending.value!.request);
    expect(f.editor.blocked.value).toBe(true); expect(f.editor.secret.value).toBe("");
    await f.editor.reconcile();
    expect(f.ports.boundReceipt).toHaveBeenCalledWith(original, false);
    expect(f.ports.saveBound).not.toHaveBeenCalled(); expect(f.saved).not.toHaveBeenCalled();
    expect(f.editor.pending.value!.request).toEqual(original);
  });
  it("loads the existing owner request after another page refuses new evidence and never sends the new Key", async () => {
    const f = fixture(); await f.editor.load(reference, f.basis);
    const existing: CredentialIntent = { schema_version: 2, kind: "save", basis_reference: reference,
      request: { ...f.basis, credential_ref: reference, expected_generation: 1, idempotency_key: crypto.randomUUID() } };
    await f.channel.write(existing);
    f.channel.write.mockRejectedValueOnce(new WorkbenchApiError("rejected", "Other page has an original request", 409, "request_evidence_pending"));
    f.editor.secret.value = "dummy-new-key"; await f.editor.save();
    expect(f.editor.pending.value!.request).toEqual(existing.request);
    expect(f.editor.error.value).toContain("新 Key 未发送");
    expect(f.editor.blocked.value).toBe(true);
    expect(f.ports.saveBound).not.toHaveBeenCalled(); expect(f.saved).not.toHaveBeenCalled();
  });
  it("does not dispatch after context changes while the intent is being persisted", async () => {
    const f = fixture(), write = deferred<RequestEvidenceRecord>();
    await f.editor.load(reference, f.basis); f.editor.secret.value = "dummy-key";
    f.channel.write.mockReturnValueOnce(write.promise);
    const saving = f.editor.save();
    await f.editor.load(null, { ...f.basis, identity: providerIdentity(newProvider()) });
    write.resolve({} as RequestEvidenceRecord); await saving;
    expect(f.ports.saveBound).not.toHaveBeenCalled(); expect(f.saved).not.toHaveBeenCalled();
  });
  it("does not clear a new context's memory-only Key when an old accepted response arrives", async () => {
    const f = fixture(), response = deferred<ProviderCredentialResult>();
    await f.editor.load(reference, f.basis); f.editor.secret.value = "old-key";
    f.ports.saveBound.mockReturnValueOnce(response.promise);
    const saving = f.editor.save();
    await vi.waitFor(() => expect(f.ports.saveBound).toHaveBeenCalledOnce());
    await f.editor.load(null, { ...f.basis, identity: providerIdentity(newProvider()) });
    f.editor.secret.value = "new-key";
    response.resolve(f.result()); await saving;
    expect(f.saved).not.toHaveBeenCalled(); expect(f.editor.secret.value).toBe("new-key");
  });
  it.each(["stale_provider_sequence", "idempotency_conflict"])("handles definite refusal %s without losing unknown evidence", async code => {
    const f = fixture(); await f.editor.load(reference, f.basis);
    f.ports.saveBound.mockRejectedValue(new WorkbenchApiError("rejected", "conflict", 409, code));
    f.editor.secret.value = "dummy-key"; await f.editor.save();
    expect(f.editor.pending.value !== null).toBe(code === "idempotency_conflict");
    expect(f.saved).not.toHaveBeenCalled();
  });
  it("reports a partial Key save without providing a supplementary binding mutation", async () => {
    const f = fixture(); await f.editor.load(reference, f.basis);
    f.ports.saveBound.mockResolvedValue(f.result("partial"));
    f.editor.secret.value = "dummy-key"; await f.editor.save();
    expect(f.editor.pending.value).not.toBeNull(); expect(f.saved).not.toHaveBeenCalled();
    expect(f.editor.error.value).toContain("保存未完成");
    expect(f.editor.error.value).not.toContain("绑定已保存");
    expect("bindSaved" in f.editor).toBe(false);
    expect("canBind" in f.editor).toBe(false);
    expect(f.ports.saveBound).toHaveBeenCalledOnce();
    await f.editor.reconcile();
    expect(f.ports.saveBound).toHaveBeenCalledOnce();
    expect(f.editor.pending.value).not.toBeNull(); expect(f.saved).not.toHaveBeenCalled();
  });
  it("retains read-only reconciliation for an earlier unknown binding request without replaying it", async () => {
    const f = fixture(), request = { ...f.basis, credential_ref: reference,
      expected_generation: 2, idempotency_key: crypto.randomUUID() };
    await f.channel.write({ schema_version: 2, kind: "bind", request, basis_reference: reference });
    await f.editor.load(reference, f.basis);
    expect(f.editor.pending.value?.kind).toBe("bind");
    f.ports.status.mockResolvedValue(status(2));
    await f.editor.reconcile();
    expect(f.ports.boundReceipt).toHaveBeenCalledExactlyOnceWith(request, true);
    expect(f.ports.saveBound).not.toHaveBeenCalled();
    expect(f.editor.pending.value).toBeNull(); expect(f.saved).toHaveBeenCalledOnce();
  });
  it("archives an unknown request only after explicit confirmation and does not replay it", async () => {
    const f = fixture(); await f.editor.load(reference, f.basis);
    f.ports.saveBound.mockRejectedValue(new WorkbenchApiError("unknown", "lost"));
    f.editor.secret.value = "dummy-key"; await f.editor.save();
    await f.editor.abandon(false); expect(f.channel.archive).not.toHaveBeenCalled();
    await f.editor.abandon(true);
    expect(f.channel.archive).toHaveBeenCalledOnce(); expect(f.editor.pending.value).toBeNull();
    expect(f.ports.saveBound).toHaveBeenCalledOnce();
  });
  it("keeps mismatched present Key/provider state locked rather than claiming success", async () => {
    const f = fixture(); await f.editor.load(reference, f.basis);
    f.editor.secret.value = "dummy-key"; await f.editor.save();
    expect(f.editor.pending.value).not.toBeNull(); expect(f.saved).not.toHaveBeenCalled();
    f.channel.read.mockResolvedValue({ schema_version: 1, kind: "workflow.request-evidence",
      operation: "provider.credential.save", idempotency_key: f.editor.pending.value!.request.idempotency_key,
      owner_kind: "provider-credential", owner_id: "one", resolution: "pending",
      request: { ...f.editor.pending.value!.request, secret: "bad" }, request_digest: "a".repeat(64) });
    const writes = f.writes.length, reopened = f.reopen(); await reopened.load(reference, f.basis);
    expect(reopened.blocked.value).toBe(true); expect(f.writes.length).toBe(writes);
  });
  it("clears memory on disposal and ignores late protection status", async () => {
    const f = fixture(), protection = deferred<boolean>();
    f.ports.protection.mockReturnValueOnce(protection.promise);
    const loading = f.editor.load(reference, f.basis); f.editor.secret.value = "memory";
    f.editor.dispose(); protection.resolve(true); await loading;
    expect(f.editor.secret.value).toBe(""); expect(f.editor.ready.value).toBe(false);
  });
});
