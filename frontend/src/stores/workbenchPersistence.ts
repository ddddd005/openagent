import { computed, onScopeDispose, ref, watch } from "vue";
import { defineStore } from "pinia";
import { readWorkbench, writeWorkbench, WORKBENCH_STORAGE_KEY,
  type SavedWorkbench, type WorkbenchConfiguration } from "../adapters/workbenchPersistence";
import type { DraftStorage } from "../adapters/browserStorage";
import { graphClone } from "../domain/workflowGraph";
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
  let storage: DraftStorage | null = null;
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
      storage = target ?? window.localStorage;
      const loaded = readWorkbench(storage);
      expectedRaw = loaded.raw;
      if (loaded.error) throw new Error(loaded.error);
      if (loaded.value) {
        workspace.restoreCatalog(loaded.value.catalog, loaded.value.activeWorkflowId, loaded.value.selectedWorkflowId);
        if (!graph.restoreSnapshot(loaded.value.graph))
          throw new Error("工作流文档或原请求无法安全恢复，原始保存未覆盖");
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
    if (event.key !== WORKBENCH_STORAGE_KEY || event.newValue === expectedRaw) return;
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
  return { status, error, savedAt, pendingCopy, initialize, save, saveWorkflow, discardDraft, reconcileCopy };
});
