import { computed, onScopeDispose, ref, watch } from "vue";
import { defineStore } from "pinia";
import { isSavedWorkbench, readWorkbench, WORKBENCH_RECOVERY_STORAGE_KEY,
  type SavedWorkbench, type WorkbenchConfiguration } from "../adapters/workbenchPersistence";
import { createLocalDraftChannel, localDraftPorts, workbenchDraftAddress, type LocalDraftPorts } from "../adapters/localDraftsApi";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import type { DraftStorage } from "../adapters/browserStorage";
import { graphClone, graphObject } from "../domain/workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { useFormDraftsStore } from "./formDrafts";

export const useWorkbenchPersistenceStore = defineStore("workbench-persistence", () => {
  const workspace = useWorkspaceStore();
  const graph = useWorkflowGraphStore();
  const notices = useWorkbenchNoticesStore();
  const forms = useFormDraftsStore();
  const status = ref<"loading" | "saved" | "pending" | "blocked">("loading");
  const error = ref<string | null>(null);
  const savedAt = ref<string | null>(null);
  const ready = ref(false);
  const isolated = ref(false);
  const originalRaw = ref<string | null>(null);
  const legacyValue = ref<SavedWorkbench | null>(null);
  const legacyImportError = ref<string | null>(null);
  const markerPending = ref<string | null>(null);
  const importing = ref(false);
  const conflictCopyKey = ref<string | null>(null);
  const pendingCopy = computed(() => workspace.workflows.find(row => row.copyPending) ?? null);
  const hasRawRecord = computed(() => originalRaw.value !== null);
  const canImportLegacy = computed(() => !!legacyValue.value && ready.value
    && (!graph.locked || markerPending.value !== null) && !legacyImportError.value && !importing.value);
  const canRecover = computed(() => false);
  const hasUnsaved = computed(() => status.value !== "saved" || forms.hasUnsaved || importing.value || markerPending.value !== null);
  let ports = localDraftPorts;
  let channel = createLocalDraftChannel(workbenchDraftAddress, ports);
  let importMarker = createLocalDraftChannel({ ...workbenchDraftAddress, kind: "intent", key: "workbench:legacy-import:v1" }, ports);
  let untouchedSignature: string | null = null;
  let sourceStorage: DraftStorage | null = null;
  let writable = false;
  let conflict = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let signature = "";
  let initialization: Promise<void> | null = null;
  let queue = Promise.resolve();
  function capture(): WorkbenchConfiguration {
    return { activeWorkflowId: workspace.activeWorkflowId, selectedWorkflowId: workspace.selectedWorkflowId,
      catalog: graphClone(workspace.workflows), graph: graph.storeSnapshot() };
  }
  function stopTimer() { if (timer) clearTimeout(timer); timer = null; }
  function rawRecord() { return originalRaw.value; }
  async function rawDigest() {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(originalRaw.value ?? ""));
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  }
  async function confirmLegacyImport(digest: string) {
    try {
      await importMarker.write({ raw_sha256: digest });
      legacyValue.value = null; isolated.value = true; markerPending.value = null;
      error.value = null;
      return true;
    } catch (failure) {
      error.value = `旧记录已写入本机，但导入凭据尚未确认${failure instanceof Error ? `：${failure.message}` : ""}`;
      return false;
    }
  }
  async function preserveConflict(value: SavedWorkbench) {
    const key = `workbench:conflict:${crypto.randomUUID()}`;
    const copy = createLocalDraftChannel({ ...workbenchDraftAddress, key }, ports);
    await copy.write(value);
    conflictCopyKey.value = key;
  }
  async function save(): Promise<boolean> {
    stopTimer();
    if (!ready.value || !writable || conflict) return false;
    const configuration = capture(), nextSignature = JSON.stringify(configuration);
    let success = false;
    const operation = queue.then(async () => {
      if (!writable || conflict) return;
      if (nextSignature === signature && channel.record() !== null) {
        status.value = JSON.stringify(capture()) === signature ? "saved" : "pending";
        success = true; return;
      }
      const value: SavedWorkbench = { ...configuration, schemaVersion: 7, kind: "graph-workbench",
        revision: (channel.record()?.sequence ?? 0) + 1, savedAt: new Date().toISOString() };
      if (!isSavedWorkbench(value)) {
        status.value = "blocked"; error.value = "工作台草稿格式无效，原记录未覆盖"; return;
      }
      try {
        const accepted = await channel.write(value);
        savedAt.value = accepted.updated_at;
        signature = nextSignature;
        status.value = JSON.stringify(capture()) === signature ? "saved" : "pending";
        error.value = null; success = true;
      } catch (failure) {
        status.value = "blocked";
        error.value = failure instanceof Error ? failure.message : "本机保存不可用，当前修改尚未保存";
        if (failure instanceof WorkbenchApiError && failure.code === "stale_draft_sequence") {
          conflict = true;
          try { await preserveConflict(value); error.value = "其他页面更新了本机草稿，当前编辑已保留为冲突副本；未覆盖任何一方"; }
          catch { error.value = "其他页面更新了本机草稿，冲突副本尚未写入本机；当前编辑仍在内存"; }
        }
        notices.notify("unavailable", error.value, "本机保存");
      }
    });
    queue = operation.catch(() => {});
    await operation;
    return success;
  }
  async function flushAll() {
    const storedForms = await forms.flushAll();
    const storedWorkbench = await save();
    return storedForms && storedWorkbench;
  }
  async function initialize(target?: DraftStorage, injectedPorts: LocalDraftPorts = localDraftPorts): Promise<void> {
    if (initialization) return initialization;
    ports = injectedPorts;
    channel = createLocalDraftChannel(workbenchDraftAddress, ports);
    importMarker = createLocalDraftChannel({ ...workbenchDraftAddress, kind: "intent", key: "workbench:legacy-import:v1" }, ports);
    initialization = (async () => {
      status.value = "loading";
      try {
        const record = await channel.read();
        try {
          sourceStorage = target ?? (typeof window === "undefined" ? null : window.localStorage);
          if (sourceStorage) {
            const recovery = sourceStorage.getItem(WORKBENCH_RECOVERY_STORAGE_KEY);
            const loaded = recovery === null ? readWorkbench(sourceStorage) : readWorkbench({
              getItem: () => recovery, setItem: () => { throw new Error("Legacy records are read-only"); },
            });
            originalRaw.value = loaded.raw;
            legacyValue.value = loaded.error ? null : loaded.value;
          }
        } catch { sourceStorage = null; }
        if (legacyValue.value) {
          try {
            const marker = await importMarker.read();
            if (marker && !marker.deleted) {
              if (marker.format_version !== 1 || !graphObject(marker.value) || Object.keys(marker.value).length !== 1
                || typeof marker.value.raw_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(marker.value.raw_sha256))
                throw new Error("浏览器旧记录导入凭据格式无效，原文仍保留");
              if (marker.value.raw_sha256 === await rawDigest()) {
                legacyValue.value = null; isolated.value = true;
              }
            }
          } catch (failure) {
            legacyImportError.value = failure instanceof Error ? failure.message : "浏览器旧记录导入凭据无法读取";
          }
        }
        if (record && !record.deleted) {
          if (record.format_version !== 1 || !isSavedWorkbench(record.value))
            throw new Error("本机工作台保存格式无效，原记录未覆盖");
          workspace.restoreCatalog(record.value.catalog, record.value.activeWorkflowId, record.value.selectedWorkflowId);
          if (!graph.restoreSnapshot(record.value.graph)) throw new Error("工作流文档或原请求无法安全恢复，原记录未覆盖");
          savedAt.value = record.updated_at;
          signature = JSON.stringify(capture());
        }
        writable = true;
        if (!workspace.workflows.length) {
          graph.createWorkflow();
          untouchedSignature = JSON.stringify(capture());
        }
        ready.value = true;
        status.value = record && !record.deleted ? "saved" : "pending";
        graph.setPersistenceGuard(flushAll);
        if (!record || record.deleted) await save();
      } catch (failure) {
        ready.value = true; writable = false; status.value = "blocked";
        error.value = failure instanceof Error ? failure.message : "本机工作台读取失败";
        graph.setPersistenceGuard(flushAll);
      }
    })();
    return initialization;
  }
  async function importLegacy(): Promise<boolean> {
    if (!canImportLegacy.value || !legacyValue.value || !sourceStorage) return false;
    importing.value = true;
    try { return await performLegacyImport(); }
    finally { importing.value = false; }
  }
  async function performLegacyImport(): Promise<boolean> {
    if (!legacyValue.value) return false;
    if (!await forms.flushAll()) return false;
    if (markerPending.value) return await save() && await confirmLegacyImport(markerPending.value);
    let digest: string;
    try { digest = await rawDigest(); }
    catch { error.value = "无法确认浏览器旧记录的原始字节身份，尚未导入"; return false; }
    const imported = graphClone(legacyValue.value);
    const current = capture();
    const undo = graphClone(graph.history);
    const empty = untouchedSignature !== null && JSON.stringify(current) === untouchedSignature;
    if (!empty && imported.catalog.some(row => current.catalog.some(existing => existing.id === row.id))) {
      error.value = "浏览器记录与本机已有工作流身份冲突，原记录未覆盖"; return false;
    }
    const merged = empty ? imported : { ...current, catalog: [...current.catalog, ...imported.catalog],
      graph: { schema_version: 1 as const, entries: { ...current.graph.entries, ...imported.graph.entries } } };
    workspace.restoreCatalog(merged.catalog, merged.activeWorkflowId, merged.selectedWorkflowId);
    if (!graph.restoreSnapshot(merged.graph)) {
      workspace.restoreCatalog(current.catalog, current.activeWorkflowId, current.selectedWorkflowId);
      graph.restoreSnapshot(current.graph);
      error.value = "浏览器原请求无法安全导入，原文未覆盖"; return false;
    }
    for (const [id, history] of Object.entries(undo)) if (graph.entries[id]) graph.history[id] = history;
    markerPending.value = digest;
    const success = await save();
    if (!success) return false;
    return confirmLegacyImport(digest);
  }
  async function recoverWorkspace() {
    return false;
  }
  async function saveWorkflow() { return await flushAll() && await graph.saveWorkflow(); }
  async function discardDraft() { if (await flushAll()) graph.discardDraft(); }
  async function reconcileCopy() { if (pendingCopy.value) await graph.reconcile(pendingCopy.value.id); }
  watch(() => ready.value ? JSON.stringify(capture()) : "", value => {
    if (!ready.value || !writable || conflict || value === signature) return;
    status.value = "pending";
    stopTimer();
    timer = setTimeout(() => { void save(); }, 200);
  }, { flush: "sync" });
  function beforeUnload(event: BeforeUnloadEvent) {
    if (!hasUnsaved.value) return;
    event.preventDefault(); event.returnValue = "";
  }
  if (typeof window !== "undefined") {
    window.addEventListener("beforeunload", beforeUnload);
    onScopeDispose(() => { stopTimer(); window.removeEventListener("beforeunload", beforeUnload); });
  } else onScopeDispose(stopTimer);
  return { status, error, savedAt, ready, pendingCopy, isolated, hasRawRecord, canRecover, canImportLegacy,
    hasUnsaved, conflictCopyKey, legacyImportError, importing, initialize, save, flushAll, saveWorkflow, discardDraft, reconcileCopy,
    rawRecord, recoverWorkspace, importLegacy };
});
