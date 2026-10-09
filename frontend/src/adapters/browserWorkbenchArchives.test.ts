import { afterEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia } from "pinia";
import { browserArchiveValue, browserRecordDigest, createBrowserWorkbenchArchiver,
  listBrowserWorkbenchArchives } from "./browserWorkbenchArchives";
import * as drafts from "./localDraftsApi";
import { WORKBENCH_RECOVERY_STORAGE_KEY, WORKBENCH_STORAGE_KEY } from "./workbenchPersistence";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { WorkbenchApiError } from "./workbenchApi";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { useWorkflowGraphStore } from "../stores/workflowGraph";

afterEach(() => vi.restoreAllMocks());
function storage(raw = "{broken", recovery?: string) {
  const records = new Map([[WORKBENCH_STORAGE_KEY, raw]]);
  if (recovery !== undefined) records.set(WORKBENCH_RECOVERY_STORAGE_KEY, recovery);
  return { records, getItem: (key: string) => records.get(key) ?? null,
    setItem: vi.fn((key: string, value: string) => { records.set(key, value); }),
    removeItem: vi.fn((key: string) => { records.delete(key); }) };
}
describe("local archival of retired browser workbenches", () => {
  it("archives both namespaces byte-for-byte before removing either browser record", async () => {
    const old = '{"schemaVersion":99,"pending":{"opaque":"do not replay"}}';
    const target = storage("{broken", old), backend = memoryLocalDraftStorage();
    const archiver = createBrowserWorkbenchArchiver(backend.ports);
    expect(await archiver.archive(target)).toBe(2);
    expect([...backend.records.values()].map(browserArchiveValue).map(row => row.raw)).toEqual(["{broken", old]);
    expect(target.records.size).toBe(0); expect(target.setItem).not.toHaveBeenCalled();
    for (const record of backend.records.values()) {
      const value = browserArchiveValue(record);
      expect(value.raw_sha256).toBe(await browserRecordDigest(value.raw));
      expect(record.address.workflow_definition_id).toBeNull();
    }
    expect(await archiver.archive(target)).toBe(0); expect(backend.ports.save).toHaveBeenCalledTimes(2);
  });
  it("retains all original bytes when the disk write fails", async () => {
    const target = storage(), backend = memoryLocalDraftStorage();
    vi.mocked(backend.ports.save).mockRejectedValue(new Error("disk unavailable"));
    await expect(createBrowserWorkbenchArchiver(backend.ports).archive(target)).rejects.toThrow("disk unavailable");
    expect(target.getItem(WORKBENCH_STORAGE_KEY)).toBe("{broken"); expect(target.removeItem).not.toHaveBeenCalled();
  });
  it("settles an unknown archive write by its original receipt without resending it", async () => {
    const target = storage(), backend = memoryLocalDraftStorage(), archiver = createBrowserWorkbenchArchiver(backend.ports);
    const save = vi.mocked(backend.ports.save).getMockImplementation()!;
    vi.mocked(backend.ports.save).mockImplementationOnce(async request => {
      await save(request); throw new WorkbenchApiError("unknown", "response lost");
    });
    await expect(archiver.archive(target)).rejects.toThrow("response lost");
    expect(target.removeItem).not.toHaveBeenCalled();
    expect(await archiver.archive(target)).toBe(1);
    expect(backend.ports.save).toHaveBeenCalledOnce(); expect(backend.ports.receipt).toHaveBeenCalledOnce();
    expect(target.getItem(WORKBENCH_STORAGE_KEY)).toBeNull();
  });
  it("does not clear a newer browser record written while the original is being archived", async () => {
    const target = storage(), backend = memoryLocalDraftStorage(), save = vi.mocked(backend.ports.save).getMockImplementation()!;
    vi.mocked(backend.ports.save).mockImplementationOnce(async request => {
      const result = await save(request); target.records.set(WORKBENCH_STORAGE_KEY, "newer bytes"); return result;
    });
    await expect(createBrowserWorkbenchArchiver(backend.ports).archive(target)).rejects.toThrow("其他页面更新");
    expect(target.getItem(WORKBENCH_STORAGE_KEY)).toBe("newer bytes"); expect(target.removeItem).not.toHaveBeenCalled();
    expect(browserArchiveValue([...backend.records.values()][0]).raw).toBe("{broken");
  });
  it("keeps independent snapshots instead of overwriting the previous archive", async () => {
    const target = storage("first"), backend = memoryLocalDraftStorage(), archiver = createBrowserWorkbenchArchiver(backend.ports);
    await archiver.archive(target); target.records.set(WORKBENCH_STORAGE_KEY, "second"); await archiver.archive(target);
    expect([...backend.records.values()].map(row => browserArchiveValue(row).raw)).toEqual(["first", "second"]);
  });
  it("retries only browser cleanup after a confirmed archive has already been saved", async () => {
    const target = storage(), backend = memoryLocalDraftStorage(), archiver = createBrowserWorkbenchArchiver(backend.ports);
    target.removeItem.mockImplementationOnce(() => { throw new Error("storage blocked"); });
    await expect(archiver.archive(target)).rejects.toThrow("storage blocked");
    expect(target.getItem(WORKBENCH_STORAGE_KEY)).toBe("{broken");
    expect(await archiver.archive(target)).toBe(1); expect(backend.ports.save).toHaveBeenCalledOnce();
  });
  it("does not overwrite an unsupported archive version", async () => {
    const target = storage(), backend = memoryLocalDraftStorage(), archiver = createBrowserWorkbenchArchiver(backend.ports);
    await archiver.archive(target);
    const record = [...backend.records.values()][0]; record.format_version = 9;
    target.records.set(WORKBENCH_STORAGE_KEY, "{broken");
    await expect(createBrowserWorkbenchArchiver(backend.ports).archive(target)).rejects.toThrow("格式不兼容");
    expect(backend.ports.save).toHaveBeenCalledOnce(); expect(target.getItem(WORKBENCH_STORAGE_KEY)).toBe("{broken");
  });
  it("validates archive content and ownership when listing it for export", async () => {
    const backend = memoryLocalDraftStorage();
    await createBrowserWorkbenchArchiver(backend.ports).archive(storage());
    vi.spyOn(drafts, "listLocalDrafts").mockResolvedValue([...backend.records.values()]);
    expect(await listBrowserWorkbenchArchives()).toHaveLength(1);
    const record = [...backend.records.values()][0];
    (record.value as { raw: string }).raw = "tampered";
    await expect(listBrowserWorkbenchArchives()).rejects.toThrow("内容校验失败");
  });
  it("archives on initialization without adopting old workflows, then reopens with no legacy warning", async () => {
    const target = storage(), backend = memoryLocalDraftStorage();
    let pinia = createPinia(); setActivePinia(pinia);
    try {
      const first = useWorkbenchPersistenceStore(); await first.initialize(target, backend.ports);
      expect(first.status).toBe("saved"); expect(first.legacyArchived).toBe(true);
      expect(first.legacyArchiveError).toBeNull(); expect(first.canImportLegacy).toBe(false);
      const id = useWorkflowGraphStore().document!.workflow_definition_id;
      disposePinia(pinia); pinia = createPinia(); setActivePinia(pinia);
      const reopened = useWorkbenchPersistenceStore(); await reopened.initialize(target, backend.ports);
      expect(reopened.hasRawRecord).toBe(false); expect(reopened.legacyArchiveError).toBeNull();
      expect(useWorkflowGraphStore().document!.workflow_definition_id).toBe(id);
      expect([...backend.records.values()].filter(row => row.address.key.startsWith("workbench:browser-archive:"))).toHaveLength(1);
    } finally { disposePinia(pinia); }
  });
  it("reports archive failures separately without disabling the current backend workspace", async () => {
    const target = storage(), backend = memoryLocalDraftStorage(), save = vi.mocked(backend.ports.save).getMockImplementation()!;
    vi.mocked(backend.ports.save).mockImplementation(request => request.address.key.startsWith("workbench:browser-archive:")
      ? Promise.reject(new WorkbenchApiError("rejected", "archive too large", 413, "draft_capacity_exceeded")) : save(request));
    const pinia = createPinia(); setActivePinia(pinia);
    try {
      const store = useWorkbenchPersistenceStore(); await store.initialize(target, backend.ports);
      expect(store.status).toBe("saved"); expect(store.legacyArchiveError).toBe("archive too large");
      expect(store.error).toBeNull(); expect(target.getItem(WORKBENCH_STORAGE_KEY)).toBe("{broken");
      useWorkflowGraphStore().document!.name = "Current editing remains writable";
      expect(await store.save()).toBe(true); expect(store.legacyArchiveError).toBe("archive too large");
    } finally { disposePinia(pinia); }
  });
});
