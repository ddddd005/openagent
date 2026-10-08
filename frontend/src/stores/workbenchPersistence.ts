import { computed, onScopeDispose, ref, watch } from "vue";
import { defineStore } from "pinia";
import { readWorkbench, writeWorkbench, WORKBENCH_STORAGE_KEY, WORKBENCH_RECOVERY_STORAGE_KEY,
  type SavedWorkbench, type WorkbenchConfiguration } from "../adapters/workbenchPersistence";
import type { DraftStorage } from "../adapters/browserStorage";
import { graphClone, newGraph } from "../domain/workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkflowGraphStore } from "./workflowGraph";
import { useWorkbenchNoticesStore } from "./workbenchNotices";

export const useWorkbenchPersistenceStore = defineStore("workbench-persistence", () => {
  const workspace = useWorkspaceStore();
  const graph = useWorkflowGraphStore();
  const notices = useWorkbenchNoticesStore();
  const status = ref<"loading" | "saved" | "pending" | "blocked">("loading");
  const error = ref<string | null>(null);
  const savedAt = ref<string | null>(null);
  const pendingCopy = computed(() => workspace.workflows.find(row => row.copyPending) ?? null);
  const ready = ref(false);
  const isolated = ref(false);
  const originalRaw = ref<string | null>(null);
  const hasRawRecord = computed(() => originalRaw.value !== null);
  const canRecover = computed(() => status.value === "blocked" && !isolated.value
    && hasRawRecord.value && !graph.locked
    && Object.values(graph.entries).every(row => row.saved_revision === 0 && row.session_id === null && row.pending === null));
  let sourceStorage: DraftStorage | null = null;
  let storage: DraftStorage | null = null;
  let storageKey = WORKBENCH_STORAGE_KEY;
  let expectedRaw: string | null = null;
  let revision = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let signature = "";

  function capture(): WorkbenchConfiguration {
    return { activeWorkflowId: workspace.activeWorkflowId, selectedWorkflowId: workspace.selectedWorkflowId,
      catalog: graphClone(workspace.workflows), graph: graph.storeSnapshot() };
  }
  function stopTimer() {
    if (timer) clearTimeout(timer);
    timer = null;
  }
  function scopedStorage(key: string): DraftStorage {
    return { getItem: () => sourceStorage!.getItem(key),
      setItem: (_key, raw) => sourceStorage!.setItem(key, raw) };
  }
  function rawRecord() { return originalRaw.value; }
  function recoverWorkspace() {
    if (!canRecover.value || !sourceStorage) return false;
    try {
      if (sourceStorage.getItem(WORKBENCH_STORAGE_KEY) !== expectedRaw)
        throw new Error("其他页面更新了原记录，请重新加载后再恢复");
      if (sourceStorage.getItem(WORKBENCH_RECOVERY_STORAGE_KEY) !== null)
        throw new Error("已有独立工作区记录，请重新加载后读取；原记录未覆盖");
      // Carry only new unsaved drafts, never a rejected session or its outbox.
      let configuration = capture();
      const fresh = !Object.keys(configuration.graph.entries).length;
      if (fresh) {
        const document = newGraph("新工作流", graph.executionPackageLock), id = document.workflow_definition_id;
        configuration = { activeWorkflowId: id, selectedWorkflowId: id,
          catalog: [{ id, title: document.name, description: "", nodeCount: 0, state: "draft" }],
          graph: { schema_version: 1, entries: { [id]: { document, saved_revision: 0, session_id: null, pending: null } } } };
      }
      const value: SavedWorkbench = { ...configuration, schemaVersion: 7, kind: "graph-workbench",
        revision: 1, savedAt: new Date().toISOString() };
      const recoveryStorage = scopedStorage(WORKBENCH_RECOVERY_STORAGE_KEY);
      const raw = writeWorkbench(recoveryStorage, value, null);
      storage = recoveryStorage;
      storageKey = WORKBENCH_RECOVERY_STORAGE_KEY;
      isolated.value = true;
      expectedRaw = raw;
      revision = value.revision;
      savedAt.value = value.savedAt;
      if (fresh) {
        workspace.restoreCatalog(value.catalog, value.activeWorkflowId, value.selectedWorkflowId);
        if (!graph.restoreSnapshot(value.graph)) throw new Error("独立工作区无法安全恢复，原记录未覆盖");
      }
      graph.selectedNodeIds = []; graph.selectedEdgeId = null;
      workspace.sidebarSection = "workflows"; workspace.interactionMode = "select";
      signature = JSON.stringify(capture());
      stopTimer();
      status.value = "saved";
      error.value = null;
      return true;
    } catch (failure) {
      error.value = failure instanceof Error ? failure.message : "独立工作区无法保存，原记录未覆盖";
      return false;
    }
  }
  function save() {
    stopTimer();
    if (!ready.value || !storage || status.value === "blocked") return false;
    const configuration = capture();
    const nextSignature = JSON.stringify(configuration);
    if (nextSignature === signature && expectedRaw !== null) {
      status.value = "saved";
      return true;
    }
    try {
      const value: SavedWorkbench = { ...configuration, schemaVersion: 7, kind: "graph-workbench",
        revision: revision + 1, savedAt: new Date().toISOString() };
      expectedRaw = writeWorkbench(storage, value, expectedRaw);
      revision = value.revision;
      savedAt.value = value.savedAt;
      signature = nextSignature;
      status.value = "saved";
      error.value = null;
      return true;
    } catch (failure) {
      status.value = "blocked";
      error.value = failure instanceof Error ? failure.message : "本地保存不可用，当前修改尚未保存";
      notices.notify("unavailable", error.value, "本地保存");
      return false;
    }
  }
  function initialize(target?: DraftStorage) {
    if (ready.value) return;
    let rewrite = false;
    try {
      sourceStorage = target ?? window.localStorage;
      storage = sourceStorage;
      let loaded = readWorkbench(storage);
      originalRaw.value = loaded.error ? loaded.raw : null;
      const recoveryRaw = sourceStorage.getItem(WORKBENCH_RECOVERY_STORAGE_KEY);
      if (recoveryRaw !== null) {
        originalRaw.value = loaded.raw;
        storageKey = WORKBENCH_RECOVERY_STORAGE_KEY;
        storage = scopedStorage(storageKey);
        isolated.value = true;
        loaded = readWorkbench(storage);
        if (loaded.error) originalRaw.value = loaded.raw;
      }
      expectedRaw = loaded.raw;
      if (loaded.error) throw new Error(loaded.error);
      if (loaded.value) {
        workspace.restoreCatalog(loaded.value.catalog, loaded.value.activeWorkflowId, loaded.value.selectedWorkflowId);
        if (!graph.restoreSnapshot(loaded.value.graph)) {
          originalRaw.value = loaded.raw;
          throw new Error("工作流文档或原请求无法安全恢复，原始保存未覆盖");
        }
        revision = loaded.value.revision;
        savedAt.value = loaded.value.savedAt;
      }
      if (!workspace.workflows.length) graph.createWorkflow();
      rewrite = loaded.rewrite || loaded.raw === null;
      signature = rewrite ? "" : JSON.stringify(capture());
      ready.value = true;
      status.value = "saved";
    } catch (failure) {
      ready.value = true;
      status.value = "blocked";
      error.value = failure instanceof Error ? failure.message : "浏览器保存不可用";
    }
    graph.setPersistenceGuard(save);
    if (rewrite && status.value !== "blocked") save();
  }
  function saveWorkflow() { return graph.saveWorkflow(); }
  function discardDraft() { graph.discardDraft(); }
  async function reconcileCopy() {
    if (pendingCopy.value) await graph.reconcile(pendingCopy.value.id);
  }
  watch(() => ready.value ? JSON.stringify(capture()) : "", value => {
    if (!ready.value || status.value === "blocked" || value === signature) return;
    status.value = "pending";
    stopTimer();
    timer = setTimeout(save, 200);
  }, { flush: "sync" });
  function changed(event: StorageEvent) {
    if (event.key !== storageKey || event.newValue === expectedRaw) return;
    stopTimer();
    status.value = "blocked";
    error.value = "其他页面更新了保存记录，当前修改尚未保存";
  }
  function flush() { save(); }
  if (typeof window !== "undefined") {
    window.addEventListener("storage", changed);
    window.addEventListener("pagehide", flush);
    window.addEventListener("beforeunload", flush);
    onScopeDispose(() => {
      stopTimer();
      window.removeEventListener("storage", changed);
      window.removeEventListener("pagehide", flush);
      window.removeEventListener("beforeunload", flush);
    });
  } else onScopeDispose(stopTimer);
  return { status, error, savedAt, pendingCopy, isolated, hasRawRecord, canRecover,
    initialize, save, saveWorkflow, discardDraft, reconcileCopy, rawRecord, recoverWorkspace };
});
