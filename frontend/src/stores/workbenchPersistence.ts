import { computed, onScopeDispose, ref, watch } from "vue";
import { defineStore } from "pinia";
import { isSavedWorkbench,
  type SavedWorkbench, type WorkbenchConfiguration } from "../adapters/workbenchPersistence";
import { createLocalDraftChannel, localDraftPorts, workbenchDraftAddress, type LocalDraftPorts } from "../adapters/localDraftsApi";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import type { DraftStorage } from "../adapters/browserStorage";
import { graphClone } from "../domain/workflowGraph";
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
  const conflictCopyKey = ref<string | null>(null);
  const pendingCopy = computed(() => workspace.workflows.find(row => row.copyPending) ?? null);
  const hasUnsaved = computed(() => status.value !== "saved" || forms.hasUnsaved);
  let ports = localDraftPorts;
  let channel = createLocalDraftChannel(workbenchDraftAddress, ports);
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
  async function initialize(_target?: DraftStorage, injectedPorts: LocalDraftPorts = localDraftPorts): Promise<void> {
    if (initialization) return initialization;
    ports = injectedPorts;
    channel = createLocalDraftChannel(workbenchDraftAddress, ports);
    initialization = (async () => {
      status.value = "loading";
      try {
        const record = await channel.read();
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
        }
        ready.value = true;
        status.value = record && !record.deleted ? "saved" : "pending";
        graph.setPersistenceGuard(save);
        if (!record || record.deleted) await save();
      } catch (failure) {
        ready.value = true; writable = false; status.value = "blocked";
        error.value = failure instanceof Error ? failure.message : "本机工作台读取失败";
        graph.setPersistenceGuard(save);
      }
    })();
    return initialization;
  }
  async function saveWorkflow() { return await save() && await graph.saveWorkflow(); }
  async function discardDraft() { if (await save()) graph.discardDraft(); }
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
  return { status, error, savedAt, ready, pendingCopy, hasUnsaved, conflictCopyKey,
    initialize, save, flushAll, saveWorkflow, discardDraft, reconcileCopy };
});
