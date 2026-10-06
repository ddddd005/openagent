import { computed, onScopeDispose, ref, watch } from "vue";
import { defineStore } from "pinia";
import {
  readWorkbench, writeWorkbench, WORKBENCH_STORAGE_KEY,
  type SavedWorkbench, type WorkbenchConfiguration,
} from "../adapters/workbenchPersistence";
import type { DraftStorage } from "../adapters/browserStorage";
import { clonePreparation } from "../domain/preparation";
import { usePreparationStore } from "./preparation";
import { useWorkspaceStore } from "./workspace";
import { useExposuresStore } from "./exposures";
import { useWorkbenchRuntimeStore, validRuntimeSnapshot } from "./workbenchRuntime";
import { useModelConfigurationStore } from "./modelConfiguration";
import type { WorkflowEditGuard } from "../domain/workflowIdentity";
import type { PreparationDraft } from "../domain/preparation";
import type { ModelDraft } from "../domain/modelConfiguration";
import type { WorkspaceLayout } from "../domain/workspace";
import type { ExposureRegistration } from "../domain/exposure";
import { useWorkflowGraphStore } from "./workflowGraph";
import { encodeUnifiedWorkbench } from "../adapters/unifiedWorkbenchDocument";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import { WorkbenchApiError } from "../adapters/workbenchApi";

export const useWorkbenchPersistenceStore = defineStore("workbench-persistence", () => {
  const preparation = usePreparationStore();
  const workspace = useWorkspaceStore();
  const exposures = useExposuresStore();
  const runtime = useWorkbenchRuntimeStore();
  const models = useModelConfigurationStore();
  const graph = useWorkflowGraphStore();
  const notices = useWorkbenchNoticesStore();
  const status = ref<"loading" | "saved" | "pending" | "blocked">("loading");
  const error = ref<string | null>(null);
  const savedAt = ref<string | null>(null);
  const pendingCopy = computed(() => workspace.workflows.find(workflow => workflow.copyPending) ?? null);
  interface LegacyDocument { preparations: PreparationDraft[]; layout: WorkspaceLayout; model: ModelDraft | null; registrations: ExposureRegistration[] }
  const legacyHistories = ref<Record<string, { past: LegacyDocument[]; future: LegacyDocument[] }>>({});
  let legacyTransactionDepth = 0;
  function legacyDocument(id: string): LegacyDocument {
    return { preparations: preparation.exportConfiguration().filter(row => row.workflowId === id),
      layout: clonePreparation(workspace.exportLayouts()[id]),
      model: clonePreparation(models.drafts.find(row => row.workflowId === id) ?? null),
      registrations: clonePreparation(exposures.registrations.filter(row => row.workflowId === id)) };
  }
  function recordLegacy(id: string) {
    const history = legacyHistories.value[id] ??= { past: [], future: [] };
    history.past.push(legacyDocument(id)); history.past = history.past.slice(-60); history.future = [];
  }
  function mutateLegacy(id: string, change: (target: string) => void) {
    if (!edit(id, target => mutateLegacy(target, change))) return;
    legacyTransactionDepth++;
    try { change(id); } finally { legacyTransactionDepth--; }
  }
  function restoreLegacy(id: string, doc: LegacyDocument) {
    for (const row of doc.preparations) preparation.restoreDocument(id, row.stage, row);
    workspace.restoreWorkflowLayout(id, doc.layout);
    models.restoreWorkflowDraft(id, doc.model);
    exposures.restoreWorkflowRegistrations(id, doc.registrations);
    save();
  }
  function undoLegacy(id: string, redo = false) {
    const history = legacyHistories.value[id];
    const doc = (redo ? history?.future : history?.past)?.pop();
    if (!doc) return;
    (redo ? history.past : history.future).push(legacyDocument(id)); restoreLegacy(id, doc);
  }
  let storage: DraftStorage | null = null;
  let expectedRaw: string | null = null;
  let revision = 0;
  let savedSchema: 1 | 2 | 3 | 4 | 5 | 6 = 6;
  const ready = ref(false);
  let timer: ReturnType<typeof setTimeout> | null = null;
  let signature = "";

  function capture(): WorkbenchConfiguration {
    return {
      activeWorkflowId: workspace.activeWorkflowId,
      selectedWorkflowId: workspace.selectedWorkflowId,
      layouts: workspace.exportLayouts(),
      preparations: preparation.exportConfiguration(),
      registrations: clonePreparation(exposures.registrations),
      runtime: runtime.exportRuntimeSnapshot(),
      models: models.exportConfiguration(),
      exposures: exposures.exportConfiguration(),
      catalog: clonePreparation(workspace.workflows),
      graph: graph.storeSnapshot(),
    };
  }
  async function migrateWorkflow() {
    const id = workspace.activeWorkflowId;
    if (status.value === "blocked" || graph.isGeneric(id) || runtime.busy || runtime.unknown || pendingCopy.value) return false;
    if (runtime.sessionId) await runtime.refresh();
    const view = runtime.runtimes[id]?.view;
    const sourceSessionId = runtime.runtimes[id]?.sessionId ?? null;
    if (sourceSessionId && (!view || view.workflow_session_id !== sourceSessionId)) {
      notices.notify("unavailable", "源会话状态尚未读取，迁移未提交", "迁移普通图"); return false;
    }
    const current = capture();
    const row = encodeUnifiedWorkbench({ ...current, graph: current.graph!, revision: revision + 1,
      savedAt: new Date().toISOString() }).documents[id];
    if (!row?.compatibility) return false;
    return graph.migrateLegacy(id, row.document, sourceSessionId, sourceSessionId ? view!.revision : null);
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
    if (nextSignature === signature && expectedRaw !== null && savedSchema === 6) {
      status.value = "saved";
      return true;
    }
    try {
      if (!validRuntimeSnapshot(configuration.runtime))
        throw new Error("会话或原请求记录无法安全恢复，原始保存未覆盖");
      const value: SavedWorkbench = {
        ...configuration, schemaVersion: 6, kind: "fixed-workbench",
        revision: revision + 1, savedAt: new Date().toISOString(),
      };
      expectedRaw = writeWorkbench(storage, value, expectedRaw);
      revision = value.revision;
      savedSchema = 6;
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
    let fresh = false;
    try {
      storage = target ?? window.localStorage;
      const loaded = readWorkbench(storage);
      expectedRaw = loaded.raw;
      if (loaded.error) throw new Error(loaded.error);
      if (loaded.value) {
        if (loaded.value.catalog) workspace.restoreCatalog(loaded.value.catalog);
        if (loaded.value.graph && !graph.restoreSnapshot(loaded.value.graph))
          throw new Error("统一工作流文档或原请求无法安全恢复，原始保存未覆盖");
        if (!runtime.restoreRuntimeSnapshot(loaded.value.runtime))
          throw new Error("保存的会话或待核实请求不兼容，原始记录未覆盖");
        preparation.restoreConfiguration(loaded.value.preparations);
        workspace.restoreLayouts(loaded.value.layouts, loaded.value.activeWorkflowId, loaded.value.selectedWorkflowId);
        exposures.restoreRegistrations(loaded.value.registrations);
        if (loaded.value.exposures && !exposures.restoreConfiguration(loaded.value.exposures))
          throw new Error("保存的注册声明无法安全恢复，原始记录未覆盖");
        if (loaded.value.models && !models.restoreConfiguration(loaded.value.models))
          throw new Error("保存的模型配置无法安全恢复，原始记录未覆盖");
        revision = loaded.value.revision;
        savedSchema = loaded.value.schemaVersion;
        savedAt.value = loaded.value.savedAt;
      } else if (loaded.raw === null) {
        graph.createWorkflow();
        workspace.initializeFreshWorkspace(workspace.activeWorkflow);
        fresh = true;
      } else {
        throw new Error("保存记录无法安全恢复，原始保存未覆盖");
      }
      for (const workflow of workspace.workflows)
        if (!graph.isGeneric(workflow.id) && workflow.nodeCount) preparation.enableUnified(workflow.id);
      const copying = workspace.workflows.find(workflow => workflow.copyPending);
      if (copying?.sourceId && !graph.isGeneric(copying.id)) editing = { source: copying.sourceId, target: copying.id };
      signature = JSON.stringify(capture());
      ready.value = true;
      status.value = expectedRaw ? "saved" : "pending";
    } catch (failure) {
      ready.value = true;
      status.value = "blocked";
      error.value = failure instanceof Error ? failure.message : "浏览器保存不可用";
    }
    runtime.setMutationPersistenceGuard(save);
    models.setPersistenceGuard(save);
    exposures.setPersistenceGuard(save);
    graph.setPersistenceGuard(save);
    runtime.setCopyHandler(finishCopy);
    preparation.setEditGuard(edit);
    workspace.setEditGuard(edit);
    models.setEditGuard(edit);
    exposures.setEditGuard(edit);
    if (fresh && status.value !== "blocked") save();
  }
  let editing: { source: string; target: string } | null = null;
  const edit: WorkflowEditGuard = (source, change) => {
    if (workspace.workflows.some(workflow => workflow.copyPending && workflow.sourceId === source && graph.isGeneric(workflow.id))) return false;
    if (workspace.workflows.find(w => w.id === source)?.state === "draft") {
      if (!legacyTransactionDepth) recordLegacy(source);
      return true;
    }
    if (status.value === "blocked") return false;
    if (editing) {
      if (editing.source === source) {
        change(editing.target);
        save();
      }
      return false;
    }
    const target = crypto.randomUUID();
    editing = { source, target };
    workspace.cloneWorkflow(source, target);
    preparation.cloneWorkflow(source, target);
    models.cloneWorkflow(source, target);
    exposures.cloneWorkflow(source, target);
    workspace.setCopyPending(target, true);
    change(target);
    if (!save()) {
      removeDraft(target, true);
      editing = null;
      return false;
    }
    void runtime.copyWorkflowSession(target, source).catch(failure => {
      notices.notify("unavailable", failure instanceof Error ? failure.message : "工作流复制失败", "编辑副本");
      const retained = runtime.exportRuntimeSnapshot().sessions.some(row =>
        row.pending?.action === `copy-workflow:${target}`);
      if (failure instanceof WorkbenchApiError && failure.kind !== "unknown" && !retained) {
        removeDraft(target, true);
        if (editing?.target === target) editing = null;
        save();
      }
    });
    return false;
  };
  function finishCopy(source: string, target: string, sessionId: string | null) {
    if (!workspace.workflows.some(w => w.id === target)) {
      workspace.cloneWorkflow(source, target);
      preparation.cloneWorkflow(source, target);
      models.cloneWorkflow(source, target);
      exposures.cloneWorkflow(source, target);
    }
    workspace.setCopyPending(target, false);
    runtime.bindWorkflowSession(source, target, sessionId);
    editing = null;
    workspace.openWorkflow(target);
    save();
  }
  function saveWorkflow() {
    if (graph.isGeneric(workspace.activeWorkflowId)) return graph.saveWorkflow();
    if (!save()) return false;
    workspace.saveDraft();
    preparation.clearHistory(workspace.activeWorkflowId);
    workspace.clearWorkflowHistory(workspace.activeWorkflowId);
    legacyHistories.value[workspace.activeWorkflowId] = { past: [], future: [] };
    return save();
  }
  async function reconcileCopy() {
    const copying = pendingCopy.value;
    if (!copying?.sourceId) return;
    if (graph.isGeneric(copying.id)) { await graph.reconcile(copying.id); return; }
    notices.notify("unknown", "旧副本请求无法只读核实，保留副本及原请求，不重新提交", "编辑副本");
  }
  function discardDraft() {
    if (graph.isGeneric(workspace.activeWorkflowId)) { graph.discardDraft(); return; }
    const source = workspace.activeWorkflow.sourceId;
    if (workspace.activeWorkflow.state === "draft" && source) {
      const id = workspace.activeWorkflowId;
      if (!canRemoveDraft(id)) return;
      workspace.openWorkflow(source);
      removeDraft(id);
      save();
    }
  }
  function canRemoveDraft(id: string) {
    const workflow = workspace.workflows.find(row => row.id === id);
    const runtimeSnapshot = runtime.exportRuntimeSnapshot();
    if (workflow?.copyPending || runtimeSnapshot.sessions.some(row =>
      (row.workflowId === id && row.pending) || row.pending?.action === `copy-workflow:${id}`)) {
      notices.notify("unknown", "原请求尚待核实，不能删除对应副本", "编辑副本");
      return false;
    }
    return true;
  }
  function removeDraft(id: string, confirmedUnsent = false) {
    if (!confirmedUnsent && !canRemoveDraft(id)) return;
    if (!workspace.removeWorkflow(id)) return;
    preparation.removeWorkflow(id);
    models.removeWorkflow(id);
    exposures.removeWorkflow(id);
    runtime.removeWorkflow(id);
    delete legacyHistories.value[id];
  }
  watch(() => ready.value ? JSON.stringify(capture()) : "", (value) => {
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
  return { status, error, savedAt, pendingCopy, initialize, save, saveWorkflow, discardDraft, reconcileCopy,
    legacyHistories, mutateLegacy, undoLegacy, migrateWorkflow };
});
