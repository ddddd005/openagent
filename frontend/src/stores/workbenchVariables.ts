import { computed, ref, watch } from "vue";
import { defineStore } from "pinia";
import { AGENT_BINDINGS, WorkbenchApiError } from "../adapters/workbenchApi";
import {
  readWorkbenchVariables, validVariableValue,
  type SessionVariable, type VariableValue, type WorkbenchVariableRead,
} from "../adapters/workbenchVariables";
import type { WorkbenchContextScope } from "../adapters/workbenchContext";
import { compilePromptItems } from "../domain/preparation";
import { usePreparationStore } from "./preparation";
import { useWorkbenchRuntimeStore } from "./workbenchRuntime";

export const useWorkbenchVariablesStore = defineStore("workbench-variables", () => {
  const preparation = usePreparationStore();
  const runtime = useWorkbenchRuntimeStore();
  const target = ref<{ workflowId: string; stage: "A" | "B" } | null>(null);
  const read = ref<WorkbenchVariableRead | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  let generation = 0;
  let controller = new AbortController();
  const scope = computed<WorkbenchContextScope | null>(() => {
    const selected = target.value;
    const session = runtime.session;
    if (!selected || runtime.workflowId !== selected.workflowId || !session
      || session.ref_revision === undefined || !session.head_commit_id) return null;
    return {
      ...selected, sessionId: session.workflow_session_id, nodeBindingId: AGENT_BINDINGS[selected.stage],
      sessionRevision: session.revision, refRevision: session.ref_revision, headCommitId: session.head_commit_id,
    };
  });
  const signature = computed(() => JSON.stringify([
    scope.value, target.value && preparation.versions[JSON.stringify([target.value.workflowId, target.value.stage])],
  ]));
  function invalidate() {
    controller.abort();
    controller = new AbortController();
    generation++;
    read.value = null;
    loading.value = false;
    error.value = null;
  }
  watch(signature, invalidate, { flush: "sync" });
  function activate(workflowId: string, stage: "A" | "B") {
    if (target.value?.workflowId === workflowId && target.value.stage === stage) return;
    invalidate();
    target.value = { workflowId, stage };
  }
  function leave() {
    invalidate();
    target.value = null;
  }
  function compile() {
    if (!target.value) throw new WorkbenchApiError("unavailable", "没有当前变量配置");
    const plan = compilePromptItems(preparation.getDraft(target.value.workflowId, target.value.stage));
    const failure = plan.diagnostics.find((entry) => entry.severity === "error");
    if (failure) throw new WorkbenchApiError("unavailable", failure.message);
    return { items: plan.items, groups: plan.groups, config: plan.config };
  }
  async function refresh() {
    const current = scope.value;
    if (!current) { error.value = "未绑定会话"; return; }
    const request = ++generation;
    const expected = signature.value;
    loading.value = true;
    error.value = null;
    try {
      const result = await readWorkbenchVariables(current, compile(), controller.signal);
      if (request === generation && signature.value === expected) read.value = result;
    } catch (failure) {
      if (request === generation && signature.value === expected)
        error.value = failure instanceof WorkbenchApiError ? failure.reason : "会话变量读取失败";
    } finally {
      if (request === generation) loading.value = false;
    }
  }
  async function assign(row: SessionVariable, value: VariableValue) {
    const current = scope.value;
    if (!current || !read.value || runtime.busy || runtime.unknown || runtime.refreshRequired
      || !validVariableValue(value, row.type)) return false;
    try {
      const revision = read.value.revision;
      const success = await runtime.writeSessionVariable(current, compile(), row.name, value, revision);
      if (success && scope.value?.sessionId === current.sessionId) await refresh();
      return !!success;
    } catch (failure) {
      error.value = failure instanceof WorkbenchApiError ? failure.reason : "会话变量配置无效";
      return false;
    }
  }
  return { target, scope, signature, read, loading, error, activate, leave, refresh, assign };
});
