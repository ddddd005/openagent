import { describe, expect, it, vi } from "vitest";
import { createProviderCredentialEditor } from "./providerCredentialEditing";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import type { CredentialRequest, CredentialStatus } from "../adapters/providerConfigurationApi";
import type { LocalDraftRecord } from "../adapters/localDraftsApi";
import { graphClone } from "../domain/workflowGraph";

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
  let record: LocalDraftRecord | null = null;
  const writes: unknown[] = [], saved = vi.fn();
  const channel = {
    read: vi.fn(async () => record),
    write: vi.fn(async (value: unknown) => {
      writes.push(graphClone(value));
      record = { schema_version: 1, kind: "workflow.local-draft", address: {
        scope: "management", kind: "intent", key: "credential:one", workflow_definition_id: null, workflow_session_id: null,
      }, format_version: 1, sequence: (record?.sequence ?? 0) + 1, updated_at: "2026-10-09T00:00:00Z",
      value: graphClone(value), deleted: value === null };
      return record;
    }),
  };
  const ports = {
    protection: vi.fn(async () => true), status: vi.fn(async () => status()),
    save: vi.fn(async (_request: CredentialRequest, _secret: string, _remove = false) => status(2)),
    receipt: vi.fn(async (_request: CredentialRequest, _remove = false) => status(2)),
  };
  const editor = createProviderCredentialEditor(channel, saved, ports);
  return { editor, channel, ports, writes, saved, reopen: () => createProviderCredentialEditor(channel, saved, ports) };
}
describe("private credential editing custody", () => {
  it("stores only non-secret intent before sending the memory-only Key and rereads its current generation", async () => {
    const f = fixture();
    await f.editor.load(reference); f.editor.secret.value = "dummy-memory-key";
    f.ports.status.mockResolvedValue(status(2));
    await f.editor.save();
    expect(f.ports.save).toHaveBeenCalledWith(expect.objectContaining({
      credential_ref: reference, expected_generation: 1,
    }), "dummy-memory-key", false);
    expect(JSON.stringify(f.writes)).not.toContain("dummy-memory-key");
    expect(f.writes[0]).toMatchObject({ basis_reference: reference, remove: false });
    expect(f.writes.at(-1)).toBeNull();
    expect(f.editor.secret.value).toBe(""); expect(f.editor.blocked.value).toBe(false);
    expect(f.saved).toHaveBeenCalledWith(reference);
  });
  it("reloads an unknown request and only queries its original receipt without replaying a Key", async () => {
    const f = fixture(); await f.editor.load(reference);
    f.ports.save.mockRejectedValue(new WorkbenchApiError("unknown", "lost"));
    f.editor.secret.value = "dummy-memory-key"; await f.editor.save();
    const original = graphClone(f.editor.pending.value!.request);
    const reopened = f.reopen(); await reopened.load(reference);
    expect(reopened.secret.value).toBe(""); expect(reopened.blocked.value).toBe(true);
    f.ports.status.mockResolvedValue(status(4, "missing"));
    await reopened.reconcile();
    expect(f.ports.receipt).toHaveBeenCalledWith(original, false);
    expect(f.ports.save).toHaveBeenCalledTimes(1);
    expect(reopened.state.value).toEqual(status(4, "missing"));
    expect(reopened.pending.value).toBeNull(); expect(f.saved).not.toHaveBeenCalled();
  });
  it("keeps cleanup failures locked and reconciles without another mutation", async () => {
    const f = fixture(); await f.editor.load(reference);
    f.ports.status.mockResolvedValue(status(2));
    f.channel.write.mockImplementationOnce(async value => {
      f.writes.push(value); return {} as LocalDraftRecord;
    }).mockRejectedValueOnce(new Error("disk unavailable"));
    f.editor.secret.value = "dummy-key"; await f.editor.save();
    expect(f.editor.pending.value).not.toBeNull(); expect(f.editor.blocked.value).toBe(true);
    expect(f.saved).not.toHaveBeenCalled();
    await f.editor.reconcile();
    expect(f.ports.save).toHaveBeenCalledTimes(1); expect(f.saved).toHaveBeenCalledWith(reference);
  });
  it("does not submit a Key when the context changes while intent is being written", async () => {
    const f = fixture(), write = deferred<LocalDraftRecord>();
    await f.editor.load(reference); f.editor.secret.value = "dummy-key";
    f.channel.write.mockReturnValueOnce(write.promise);
    const saving = f.editor.save();
    await f.editor.load("env:GEMINI_API_KEY");
    write.resolve({} as LocalDraftRecord); await saving;
    expect(f.ports.save).not.toHaveBeenCalled(); expect(f.saved).not.toHaveBeenCalled();
    expect(f.editor.secret.value).toBe("");
  });
  it("does not bind a late accepted mutation to another context", async () => {
    const f = fixture(), response = deferred<CredentialStatus>();
    await f.editor.load(reference); f.editor.secret.value = "dummy-key";
    f.ports.save.mockReturnValueOnce(response.promise);
    const saving = f.editor.save();
    await vi.waitFor(() => expect(f.ports.save).toHaveBeenCalledOnce());
    await f.editor.load("env:GEMINI_API_KEY"); response.resolve(status(2)); await saving;
    await f.editor.reconcile();
    expect(f.saved).not.toHaveBeenCalled();
  });
  it("clears definitively rejected requests but never treats idempotency conflicts as unsent", async () => {
    for (const code of ["stale_credential_generation", "idempotency_conflict"]) {
      const f = fixture(); await f.editor.load(reference);
      f.ports.save.mockRejectedValue(new WorkbenchApiError("rejected", "conflict", 409, code));
      f.editor.secret.value = "dummy-key"; await f.editor.save();
      expect(f.editor.pending.value !== null).toBe(code === "idempotency_conflict");
      expect(f.saved).not.toHaveBeenCalled();
    }
  });
  it("cannot abandon an unknown request with no original receipt or submit again", async () => {
    const f = fixture(); await f.editor.load(reference);
    f.ports.save.mockRejectedValue(new WorkbenchApiError("unknown", "lost"));
    f.ports.receipt.mockRejectedValue(new WorkbenchApiError("unknown", "no receipt"));
    f.editor.secret.value = "dummy-key"; await f.editor.save(); await f.editor.reconcile();
    await f.editor.save();
    expect(f.editor.blocked.value).toBe(true); expect(f.ports.save).toHaveBeenCalledOnce();
  });
  it("rejects a stale status below the accepted receipt and malformed persisted intent without overwriting", async () => {
    const f = fixture(); await f.editor.load(reference);
    f.editor.secret.value = "dummy-key"; await f.editor.save();
    expect(f.editor.pending.value).not.toBeNull(); expect(f.saved).not.toHaveBeenCalled();
    await f.channel.write({ request: f.editor.pending.value!.request, remove: false, basis_reference: reference, secret: "bad" });
    const writes = f.writes.length, reopened = f.reopen(); await reopened.load(reference);
    expect(reopened.blocked.value).toBe(true); expect(f.writes.length).toBe(writes);
  });
  it("clears memory on disposal and ignores late protection status", async () => {
    const f = fixture(), protection = deferred<boolean>();
    f.ports.protection.mockReturnValueOnce(protection.promise);
    const loading = f.editor.load(reference); f.editor.secret.value = "memory";
    f.editor.dispose(); protection.resolve(true); await loading;
    expect(f.editor.secret.value).toBe(""); expect(f.editor.ready.value).toBe(false);
  });
});
