import { describe, expect, it, vi } from "vitest";
import { createProviderCredentialEvidenceChannel } from "./providerCredentialEvidence";
import type { CredentialIntent } from "../application/providerCredentialEditing";
import type { LocalDraftAddress, LocalDraftRecord } from "./localDraftsApi";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { graphClone } from "../domain/workflowGraph";
import { newProvider, providerIdentity } from "../domain/workflowModelResources";
import { WorkbenchApiError } from "./workbenchApi";

function fixture() {
  const provider = newProvider(), evidence = memoryRequestEvidence();
  const owner = provider.scope + ":" + provider.resource_id;
  const { credential_ref: _reference, ...seed } = provider.value;
  const intent: CredentialIntent = { schema_version: 2, kind: "save", basis_reference: null,
    request: { identity: providerIdentity(provider), expected_sequence: 0, provider_seed: seed,
      credential_ref: `local:${crypto.randomUUID()}`, expected_generation: 0, idempotency_key: crypto.randomUUID() } };
  const legacyRead = vi.fn(async (_address: LocalDraftAddress) => null as LocalDraftRecord | null);
  const channel = createProviderCredentialEvidenceChannel(owner, evidence.ports, legacyRead);
  return { channel, evidence, legacyRead, intent, owner,
    reopen: () => createProviderCredentialEvidenceChannel(owner, evidence.ports, legacyRead) };
}
describe("provider private mutation public evidence channel", () => {
  it("stores only the exact public request and preserves verified evidence rather than a draft tombstone", async () => {
    const f = fixture();
    expect(await f.channel.read()).toBeNull();
    const saved = await f.channel.write(f.intent);
    expect(saved.request).toEqual(f.intent.request);
    expect(saved).toMatchObject({ operation: "provider.credential.save", owner_kind: "provider-credential",
      owner_id: f.owner, resolution: "pending" });
    expect(JSON.stringify(saved)).not.toContain("basis_reference");
    expect(await f.reopen().read()).toEqual(saved);
    await f.channel.write(null);
    expect(await f.channel.read()).toBeNull();
    expect([...f.evidence.records.values()][0]!.resolution).toBe("verified");
    expect(f.legacyRead).toHaveBeenCalledWith({ scope: "management", kind: "intent", key: `credential:${f.owner}`,
      workflow_definition_id: null, workflow_session_id: null });
  });
  it("archives the original record without copying it or invoking a business command", async () => {
    const f = fixture(), saved = await f.channel.write(f.intent);
    await f.channel.archive(f.intent);
    expect(await f.channel.read()).toEqual({ ...saved, resolution: "archived" });
    await f.channel.write(null);
    expect(f.evidence.records.size).toBe(1);
    expect([...f.evidence.records.values()][0]).toEqual({ ...saved, resolution: "archived" });
    expect(f.evidence.ports.write).toHaveBeenCalledOnce();
  });
  it("retains exact pending evidence after a response loss and rejects an altered resolution receipt", async () => {
    const f = fixture(), saved = await f.channel.write(f.intent);
    vi.mocked(f.evidence.ports.resolve).mockResolvedValueOnce({
      ...saved, resolution: "verified", request: { ...saved.request, expected_sequence: 9 },
    });
    await expect(f.channel.write(null)).rejects.toThrow("尚未结案");
    expect([...f.evidence.records.values()][0]!.resolution).toBe("pending");
  });
  it("reads a committed initial evidence write after response loss without dispatching a replacement", async () => {
    const f = fixture(), write = vi.mocked(f.evidence.ports.write).getMockImplementation()!;
    vi.mocked(f.evidence.ports.write).mockImplementationOnce(async request => {
      await write(request); throw new WorkbenchApiError("unknown", "Controlled evidence response loss");
    });
    await expect(f.channel.write(f.intent)).rejects.toThrow("response loss");
    expect((await f.channel.read())!.request).toEqual(f.intent.request);
    expect(f.evidence.ports.write).toHaveBeenCalledOnce();
  });
  it.each(["verified", "archived"] as const)("reads the original committed %s resolution after response loss", async resolution => {
    const f = fixture(), saved = await f.channel.write(f.intent);
    const resolve = vi.mocked(f.evidence.ports.resolve).getMockImplementation()!;
    vi.mocked(f.evidence.ports.resolve).mockImplementationOnce(async request => {
      await resolve(request); throw new WorkbenchApiError("unknown", "Controlled resolution response loss");
    });
    if (resolution === "verified") await expect(f.channel.write(null)).rejects.toThrow("response loss");
    else await expect(f.channel.archive(f.intent)).rejects.toThrow("response loss");
    expect(await f.channel.read()).toEqual({ ...saved, resolution });
    if (resolution === "archived") await f.channel.archive(f.intent);
    await f.channel.write(null);
    expect(f.evidence.ports.resolve).toHaveBeenCalledOnce();
    expect(await f.reopen().read()).toBeNull();
    expect(f.evidence.ports.write).toHaveBeenCalledOnce();
  });
  it("uses the same owner lock for Key save and delete evidence", async () => {
    const f = fixture();
    await f.channel.write(f.intent);
    const deletion: CredentialIntent = { schema_version: 2, kind: "delete", basis_reference: f.intent.request.credential_ref,
      request: { credential_ref: f.intent.request.credential_ref, expected_generation: 1, idempotency_key: crypto.randomUUID() } };
    await expect(f.channel.write(deletion)).rejects.toMatchObject({ code: "request_evidence_pending" });
    expect(f.evidence.records.size).toBe(1);
  });
  it.each(["unresolved", "unknown-format"])("blocks a present %s legacy intent without interpreting or replacing it", async mode => {
    const f = fixture(), address = { scope: "management" as const, kind: "intent" as const,
      key: `credential:${f.owner}`, workflow_definition_id: null, workflow_session_id: null };
    const record: LocalDraftRecord = { schema_version: 1, kind: "workflow.local-draft", address,
      format_version: mode === "unknown-format" ? 99 : 2, sequence: 1, updated_at: "2026-10-12T00:00:00Z",
      value: { unknown_original: "Preserve these exact bytes" }, deleted: false };
    const original = graphClone(record);
    f.legacyRead.mockResolvedValue(record);
    await expect(f.channel.read()).rejects.toThrow("旧原 Key 请求证据尚未接管");
    await expect(f.channel.write(f.intent)).rejects.toThrow("禁止新保存");
    expect(f.evidence.ports.write).not.toHaveBeenCalled(); expect(record).toEqual(original);
  });
  it("reports invalid or unavailable legacy evidence instead of assuming it is absent", async () => {
    const f = fixture();
    f.legacyRead.mockResolvedValueOnce({ deleted: true } as LocalDraftRecord);
    await expect(f.channel.read()).rejects.toThrow("格式或身份无法确认");
    f.legacyRead.mockRejectedValueOnce(new Error("Controlled old-evidence read failure"));
    await expect(f.channel.write(f.intent)).rejects.toThrow("old-evidence read failure");
    expect(f.evidence.ports.write).not.toHaveBeenCalled();
  });
  it("permits an existing formal pending receipt lookup but not a new mutation when old evidence also exists", async () => {
    const f = fixture(), saved = await f.channel.write(f.intent);
    f.legacyRead.mockImplementation(async address => ({ schema_version: 1, kind: "workflow.local-draft",
      address, format_version: 2, sequence: 1, updated_at: "2026-10-12T00:00:00Z",
      value: { unknown_original: true }, deleted: false }));
    expect(await f.channel.read()).toEqual(saved);
    await expect(f.channel.write(f.intent)).rejects.toThrow("禁止新保存");
    expect(f.evidence.ports.write).toHaveBeenCalledOnce();
  });
});
