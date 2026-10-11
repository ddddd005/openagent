import { afterEach, describe, expect, it, vi } from "vitest";
import { createContentMetadata, type ContentMetadataPorts } from "./contentMetadata";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone } from "../domain/workflowGraph";
import { newPromptResource, promptIdentity } from "../domain/workflowPromptResources";
import type { ContentMetadata, ContentMetadataSaveRequest } from "../adapters/contentMetadataApi";
import { memoryRequestEvidence, requestEvidenceCommandResponse } from "../testUtils/requestEvidence";
import { graphReceiptReadResponse } from "../testUtils/graphApplicationServer";

afterEach(() => vi.unstubAllGlobals());

const reference = () => promptIdentity(newPromptResource());
const accepted = (request: ContentMetadataSaveRequest): ContentMetadata => ({
  reference: graphClone(request.reference), display_name: request.display_name,
  metadata_sequence: request.expected_metadata_sequence + 1,
});
function fixture(overrides: Partial<ContentMetadataPorts> = {}, target = reference()) {
  let raw: string | null = null;
  const save = vi.fn(async (request: ContentMetadataSaveRequest) => accepted(request));
  const readReceipt = vi.fn(async (request: ContentMetadataSaveRequest) => accepted(request));
  const ports: ContentMetadataPorts = { read: async identity => ({
    reference: graphClone(identity), display_name: "Before", metadata_sequence: 2,
  }), save, readReceipt, readPending: () => raw, writePending: value => { raw = value; }, ...overrides };
  const metadata = createContentMetadata(() => ports), entry = metadata.forReference(target);
  return { metadata, entry, ports, save, readReceipt, raw: () => raw, target };
}
describe("formal prompt metadata coordination", () => {
  it("persists the exact name original before dispatch with independent sequence", async () => {
    const order: string[] = [];
    const f = fixture({ writePending: value => { order.push(value ? "persist" : "clear"); },
      save: async request => { order.push("dispatch"); return accepted(request); } });
    await f.entry.load();
    expect(await f.entry.save("  Formal prompt  ")).toBe(true);
    expect(order).toEqual(["persist", "dispatch", "clear"]);
    expect(f.entry.record.value).toEqual({ reference: f.target, display_name: "Formal prompt", metadata_sequence: 3 });
    expect(await f.entry.save("Formal prompt")).toBe(true);
    expect(order).toHaveLength(3);
  });
  it("detaches reads and ignores an older read after exact name acceptance", async () => {
    let finish!: (value: ContentMetadata) => void;
    const f = fixture();
    await f.entry.load();
    const saved = f.entry.save("Accepted");
    f.ports.read = () => new Promise(resolve => { finish = resolve; });
    const reading = f.entry.refresh();
    expect(await saved).toBe(true);
    finish({ reference: f.target, display_name: "Old read", metadata_sequence: 2 });
    await reading;
    expect(f.entry.record.value?.display_name).toBe("Accepted");
    expect(f.entry.loading.value).toBe(false);
  });
  it("locks unknown names only for their full identity and reopens for receipt read without replay", async () => {
    const target = reference(), peer = { ...target, scope: "project:story" };
    const originals = new Map<string, string | null>(), sends: ContentMetadataSaveRequest[] = [], reads: ContentMetadataSaveRequest[] = [];
    const portsFor = (identity: typeof target): ContentMetadataPorts => ({
      read: async () => ({ reference: identity, display_name: "", metadata_sequence: 0 }),
      readPending: () => originals.get(identity.scope) ?? null,
      writePending: value => { originals.set(identity.scope, value); },
      save: async request => { sends.push(graphClone(request)); throw new WorkbenchApiError("unknown", "lost"); },
      readReceipt: async request => { reads.push(graphClone(request)); return accepted(request); },
    });
    const metadata = createContentMetadata(portsFor), first = metadata.forReference(target), other = metadata.forReference(peer);
    await Promise.all([first.load(), other.load()]);
    expect(await first.save("Original")).toBe(false);
    expect(first.locked.value).toBe(true); expect(other.locked.value).toBe(false);
    expect(await first.save("Second")).toBe(false);
    const reopened = createContentMetadata(portsFor).forReference(target);
    expect(await reopened.reconcile()).toBe(true);
    expect(sends).toHaveLength(1); expect(reads).toEqual(sends);
    expect(reopened.record.value?.display_name).toBe("Original");
    expect(originals.get(target.scope)).toBeNull();
    expect(other.record.value?.display_name).toBe("");
  });
  it("reports definitive CAS rejection and allows ordinary memory editing", async () => {
    const f = fixture({ save: async () => { throw new WorkbenchApiError("rejected", "name changed", 409, "stale_metadata_sequence"); } });
    await f.entry.load();
    expect(await f.entry.save("Edited text")).toBe(false);
    expect(f.entry.error.value).toBe("name changed");
    expect(f.entry.pending.value).toBeNull(); expect(f.entry.locked.value).toBe(false);
    expect(f.entry.record.value?.display_name).toBe("Before");
  });
  it("does not dispatch when original evidence cannot be written or read", async () => {
    for (const overrides of [
      { writePending: () => { throw new Error("evidence write failed"); } },
      { readPending: () => "{" },
    ]) {
      const f = fixture(overrides);
      await f.entry.load();
      expect(await f.entry.save("Edited")).toBe(false);
      expect(f.save).not.toHaveBeenCalled();
    }
  });
  it("retains original evidence after mismatched receipt and later receipt denial", async () => {
    const f = fixture({ save: async request => ({ ...accepted(request), display_name: "Different" }),
      readReceipt: async () => { throw new WorkbenchApiError("rejected", "No original receipt", 404); } });
    await f.entry.load();
    expect(await f.entry.save("Original")).toBe(false);
    const raw = f.raw();
    expect(await f.entry.reconcile()).toBe(false);
    expect(f.raw()).toBe(raw); expect(f.entry.locked.value).toBe(true);
    expect(f.entry.record.value?.display_name).toBe("Before");
  });
  it("reopens default independent evidence, reads original receipt and retains verified original text", async () => {
    vi.resetModules();
    const evidence = memoryRequestEvidence(), target = reference(), sent: ContentMetadataSaveRequest[] = [];
    const getItem = vi.fn(), setItem = vi.fn(), removeItem = vi.fn();
    vi.stubGlobal("localStorage", { getItem, setItem, removeItem });
    vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
      const { operation, parameters } = JSON.parse(String(init.body));
      if (operation === "request-evidence.read")
        return new Response(JSON.stringify(await evidence.ports.read(parameters.owner_kind, parameters.owner_id)));
      if (operation === "request-evidence.write")
        return requestEvidenceCommandResponse(operation, await evidence.ports.write(parameters));
      if (operation === "request-evidence.resolve")
        return requestEvidenceCommandResponse(operation, await evidence.ports.resolve(parameters));
      if (operation === "content.metadata.read")
        return new Response(JSON.stringify({ reference: target, display_name: "", metadata_sequence: 0 }));
      expect(operation).toBe("content.metadata.update");
      sent.push(graphClone(parameters));
      if (path === "/api/graph/commands") throw new Error("Controlled response loss");
      expect(path).toBe("/api/graph/receipts/read");
      return graphReceiptReadResponse(operation, parameters, accepted(parameters));
    }));
    const first = (await import("./contentMetadata")).useContentMetadata().forReference(target);
    await first.load(); await first.initialize();
    expect(await first.save("Persisted original")).toBe(false);
    const durable = [...evidence.records.values()][0]!;
    expect(durable.owner_id).toBe(`content-metadata:${JSON.stringify([target.scope, target.type_id, target.resource_id])}`);
    expect(durable.operation).toBe("content.metadata.update"); expect(durable.resolution).toBe("pending");
    expect(durable.request).toEqual(sent[0]);
    vi.resetModules();
    const reopened = (await import("./contentMetadata")).useContentMetadata().forReference(target);
    expect(await reopened.reconcile()).toBe(true);
    expect(sent).toEqual([sent[0], sent[0]]);
    expect([...evidence.records.values()][0]).toMatchObject({ resolution: "verified", request: sent[0] });
    expect(reopened.pending.value).toBeNull(); expect(reopened.record.value?.display_name).toBe("Persisted original");
    expect(getItem).not.toHaveBeenCalled(); expect(setItem).not.toHaveBeenCalled(); expect(removeItem).not.toHaveBeenCalled();
  });
});
