import { describe, expect, it, vi } from "vitest";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone } from "../domain/workflowGraph";
import { newPromptResource } from "../domain/workflowPromptResources";
import { newLorebookResource } from "../plugins/tavern/lorebookResources";
import { createLorebookResources } from "../plugins/tavern/lorebookResourceController";
import { createPromptResources } from "./promptResources";
import type { ContentResourceManagementPorts, createContentResourceManagement } from "./contentResourceManagement";

function managementCases<T extends { update_sequence: number }>(
  label: string, newRecord: () => T, otherRecord: () => unknown,
  create: (ports: ContentResourceManagementPorts<T>) => ReturnType<typeof createContentResourceManagement<T>>,
) {
  describe(`${label} shared content management`, () => {
    function fixture(list: ContentResourceManagementPorts<T>["list"]) {
      let raw: string | null = null;
      const save = vi.fn(async () => ({})), readReceipt = vi.fn(async () => ({}));
      const resources = create({ list, save, readReceipt, readPending: () => raw, writePending: value => { raw = value; } });
      return { resources, save, readReceipt, raw: () => raw };
    }
    it("owns a detached list and refuses another content type without losing the current records", async () => {
      const record = newRecord(), original = graphClone(record);
      const list = vi.fn().mockResolvedValueOnce([record]).mockResolvedValueOnce([otherRecord()]);
      const f = fixture(list);
      await f.resources.refresh();
      record.update_sequence = 20;
      expect(f.resources.records.value).toEqual([original]);
      await f.resources.refresh();
      expect(f.resources.records.value).toEqual([original]);
      expect(f.resources.error.value).toContain("契约无效");
      expect(f.resources.loading.value).toBe(false);
      expect(f.save).not.toHaveBeenCalled();
    });
    it("does not let an older malformed read replace the latest list or its diagnostic", async () => {
      let finish!: (value: unknown) => void;
      const record = newRecord();
      const list = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
        .mockResolvedValueOnce([record]);
      const f = fixture(list), first = f.resources.refresh();
      await f.resources.refresh();
      f.resources.error.value = "current operation diagnostic";
      finish([otherRecord()]);
      await first;
      expect(f.resources.records.value).toEqual([record]);
      expect(f.resources.error.value).toBe("current operation diagnostic");
      expect(f.resources.loading.value).toBe(false);
    });
    it("rejects an overflowing sequence before persistence or dispatch", async () => {
      const f = fixture(async () => []);
      expect(await f.resources.save(newRecord(), Number.MAX_SAFE_INTEGER)).toBe(false);
      expect(f.raw()).toBeNull();
      expect(f.save).not.toHaveBeenCalled();
      expect(f.resources.error.value).toContain("序号无效");
    });
    it("preserves original request evidence and locking through a failed list refresh", async () => {
      const f = fixture(async () => { throw new Error("content list read failed"); });
      f.save.mockRejectedValue(new WorkbenchApiError("unknown", "content write result unknown"));
      expect(await f.resources.save(newRecord(), 0)).toBe(false);
      const original = f.raw();
      await f.resources.refresh();
      expect(f.resources.error.value).toBe("content list read failed");
      expect(f.raw()).toBe(original);
      expect(f.resources.locked.value).toBe(true);
      expect(await f.resources.save(newRecord(), 0)).toBe(false);
      expect(f.save).toHaveBeenCalledOnce();
      expect(f.readReceipt).not.toHaveBeenCalled();
    });
  });
}
managementCases("prompt", newPromptResource, newLorebookResource, createPromptResources);
managementCases("Lorebook", newLorebookResource, newPromptResource, createLorebookResources);
