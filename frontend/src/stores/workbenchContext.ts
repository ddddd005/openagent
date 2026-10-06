import { ref, watch } from "vue";
import { defineStore } from "pinia";
import {
  isWorkbenchContextScope,
  mapWorkbenchContextRead,
  previewWorkbenchContext,
  readWorkbenchContext,
  type WorkbenchContextPreview,
  type WorkbenchContextRead,
  type WorkbenchContextScope,
} from "../adapters/workbenchContext";
import { AGENT_BINDINGS, WorkbenchApiError, type FailureKind } from "../adapters/workbenchApi";
import { clonePreparation, compilePromptItems, type PreparationDiagnostic } from "../domain/preparation";
import { usePreparationStore, type PreparationViewToken } from "./preparation";

export interface WorkbenchContextIssue {
  sequence: number;
  kind: FailureKind;
  reason: string;
}
export const useWorkbenchContextStore = defineStore("workbench-context", () => {
  const preparation = usePreparationStore();
  const scope = ref<WorkbenchContextScope | null>(null);
  const read = ref<WorkbenchContextRead | null>(null);
  const serverPreview = ref<WorkbenchContextPreview | null>(null);
  const loading = ref<"read" | "preview" | null>(null);
  const error = ref<WorkbenchContextIssue | null>(null);
  const diagnostics = ref<PreparationDiagnostic[]>([]);
  let generation = 0;
  let sequence = 0;
  let controller = new AbortController();

  const scopeKey = (value: WorkbenchContextScope | null) => JSON.stringify(value);
  const storageStage = (value: WorkbenchContextScope) => preparation.isUnified(value.workflowId) ? "A" : value.stage;
  function stop() {
    controller.abort();
    controller = new AbortController();
    generation += 1;
    loading.value = null;
  }
  function leave() {
    stop();
    if (scope.value) preparation.clearContextCache(scope.value.workflowId, storageStage(scope.value));
    scope.value = null;
    read.value = null;
    serverPreview.value = null;
    error.value = null;
    diagnostics.value = [];
  }
  function activate(value: WorkbenchContextScope | null) {
    if (scopeKey(value) === scopeKey(scope.value)) return;
    leave();
    if (value && isWorkbenchContextScope(value)) scope.value = clonePreparation(value);
  }
  function notify(failure: unknown) {
    const issue = failure instanceof WorkbenchApiError
      ? failure : new WorkbenchApiError("unavailable", "上下文只读操作失败");
    error.value = {
      sequence: ++sequence, kind: issue.kind === "unknown" ? "unavailable" : issue.kind, reason: issue.reason,
    };
    if (issue.nodeId) diagnostics.value = [{
      code: issue.code ?? "preparation_failed", message: issue.reason, severity: "error", nodeId: issue.nodeId,
    }];
  }
  function clearError(value: number) {
    if (error.value?.sequence === value) error.value = null;
  }
  function current(target: WorkbenchContextScope, token: PreparationViewToken, requestGeneration: number) {
    return generation === requestGeneration && scopeKey(scope.value) === scopeKey(target)
      && preparation.isCurrentView(token);
  }
  function begin(operation: "read" | "preview") {
    if (!scope.value) {
      notify(new WorkbenchApiError("unavailable", "尚无带明确版本依据的会话上下文"));
      return null;
    }
    stop();
    const target = clonePreparation(scope.value);
    const token = preparation.currentViewToken(target.workflowId, storageStage(target));
    loading.value = operation;
    error.value = null;
    if (operation === "preview") diagnostics.value = [];
    return { target, token, requestGeneration: generation, signal: controller.signal };
  }
  async function refresh() {
    const request = begin("read");
    if (!request) return;
    const { target, requestGeneration, signal } = request;
    read.value = null;
    preparation.clearContextCache(target.workflowId, storageStage(target));
    const readToken = preparation.currentViewToken(target.workflowId, storageStage(target));
    try {
      const result = await readWorkbenchContext(target, signal);
      if (!current(target, readToken, requestGeneration)) return;
      const nodes = preparation.getDraft(target.workflowId, storageStage(target)).nodes.filter(candidate =>
        candidate.kind === "context" && (candidate.config.sourceBindingId ?? AGENT_BINDINGS[target.stage]) === target.nodeBindingId);
      if (!nodes.length || !nodes.every(node => preparation.setContextCache(
        target.workflowId, storageStage(target), node.id, mapWorkbenchContextRead(result, target, node.id), readToken,
      ))) throw new WorkbenchApiError("unavailable", "上下文节点已变化，读取结果未应用");
      read.value = result;
    } catch (failure) {
      if (current(target, readToken, requestGeneration)) notify(failure);
    } finally {
      if (generation === requestGeneration && scopeKey(scope.value) === scopeKey(target)) loading.value = null;
    }
  }
  async function preview(stage?: "A" | "B") {
    const request = begin("preview");
    if (!request) return;
    const { token, requestGeneration, signal } = request;
    const target = stage ? { ...request.target, stage, nodeBindingId: AGENT_BINDINGS[stage] } : request.target;
    serverPreview.value = null;
    try {
      const compiled = compilePromptItems(preparation.getExecutionDraft(target.workflowId, target.stage));
      const diagnostic = compiled.diagnostics.find((entry) => entry.severity === "error");
      if (diagnostic) throw new WorkbenchApiError("unavailable", diagnostic.message);
      const text = target.stage === "A" ? preparation.getRootText(target.workflowId, "A") : null;
      if (target.stage === "A" && !text?.trim())
        throw new WorkbenchApiError("unavailable", "Agent A 当前输入不能为空");
      if (target.stage === "B" && preparation.getRootText(target.workflowId, "B").trim())
        throw new WorkbenchApiError("unavailable", "Agent B 当前输入由未来 A 的业务结果提供，不能预填");
      const result = await previewWorkbenchContext(target, {
        items: compiled.items, groups: compiled.groups, config: compiled.config,
      }, text, signal);
      if (!current(request.target, token, requestGeneration)) return;
      // The preview root is ephemeral and never becomes archived node context.
      serverPreview.value = result;
    } catch (failure) {
      if (current(request.target, token, requestGeneration)) notify(failure);
    } finally {
      if (generation === requestGeneration && scopeKey(scope.value) === scopeKey(request.target)) loading.value = null;
    }
  }
  watch(() => scope.value
    ? preparation.versions[JSON.stringify([scope.value.workflowId, storageStage(scope.value)])] : undefined, () => {
    serverPreview.value = null;
    if (loading.value === "preview") stop();
  }, { flush: "sync" });
  watch(() => scope.value
    ? preparation.versions[JSON.stringify([scope.value.workflowId, storageStage(scope.value)])] : undefined, () => {
    diagnostics.value = [];
  }, { flush: "sync" });
  return { scope, read, serverPreview, loading, error, diagnostics, activate, leave, refresh, preview, clearError };
});
