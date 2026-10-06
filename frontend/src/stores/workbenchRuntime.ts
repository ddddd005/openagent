import { computed, onScopeDispose, ref } from "vue";
import { defineStore, storeToRefs } from "pinia";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import {
  AGENT_BINDINGS,
  WorkbenchApiError,
  workbenchRequest,
  type FailureKind,
  type PublicSession,
} from "../adapters/workbenchApi";
import {
  decodeSessionSelection,
  decodeWorkbenchSession,
  exactSessionObject,
  isExactPromptSelection,
  listWorkbenchSessions,
  readSessionSelection,
  readWorkbenchSession,
  safeSessionJson,
  sessionInteger,
  sessionUuid,
  validCompiledPromptRecord,
  type ExactPromptSelection,
  type ReplyCandidate,
  type SessionSummary,
  type WorkbenchSession,
} from "../adapters/workbenchSessions";
import { MAIN_WORKFLOW_ID, EMPTY_WORKFLOW_ID } from "../fixtures/workflows";
import { isWorkflowIdentity } from "../domain/workflowIdentity";
import { clonePreparation, compilePromptItems, type CompiledPromptItems } from "../domain/preparation";
import { usePreparationStore } from "./preparation";
import { useExposuresStore } from "./exposures";
import { useModelConfigurationStore } from "./modelConfiguration";
import { isExactModelSelection, type ExactModelSelection } from "../domain/modelConfiguration";
import { observedNode, type MainStage } from "../domain/observation";
import {
  decodeWorkbenchVariableWrite, variableNamePattern, variableReadBody,
  type InlinePromptConfiguration, type VariableValue, type WorkbenchVariableRead,
} from "../adapters/workbenchVariables";
import type { WorkbenchContextScope } from "../adapters/workbenchContext";

export interface PendingMutation {
  path: string;
  body: Record<string, unknown>;
  requestId: string;
  action: string;
  replayable: boolean;
}

interface WorkflowRuntime {
  sessionId: string | null;
  view: PublicSession | null;
  pending: PendingMutation | null;
  busy: string | null;
  requiresRefresh: boolean;
  promptSelection?: ExactPromptSelection | null;
  modelSelection?: ExactModelSelection | null;
}
export interface WorkbenchRuntimeSnapshot {
  schemaVersion: 1 | 2;
  kind: "workbench-runtime";
  selectedSessions: { workflowId: string; sessionId: string | null }[];
  sessions: {
    workflowId: string;
    sessionId: string | null;
    pending: PendingMutation | null;
    requiresRefresh: boolean;
    promptSelection: ExactPromptSelection | null;
    modelSelection?: ExactModelSelection | null;
  }[];
  selectionPending: PendingMutation | null;
}

type CompiledPlan = CompiledPromptItems;

export interface RuntimeErrorNotice {
  sequence: number;
  kind: FailureKind;
  reason: string;
  requestId: string;
}

export function primaryControl(view: PublicSession | null) {
  if (!view) return { action: "start", label: "启动", runId: null, revision: null };
  const bindings = Object.values(AGENT_BINDINGS) as string[];
  if (view.nodes.some((node) => bindings.includes(node.node_binding_id) && node.status === "pausing"))
    return { action: "waiting", label: "暂停中", runId: null, revision: null };
  const action = view.available_actions.includes("resume") ? "resume"
    : view.available_actions.includes("interrupt") ? "interrupt" : "";
  const node = view.nodes.find((row) => bindings.includes(row.node_binding_id)
    && (action === "resume" ? ["paused", "failed"] : ["prepared", "running"]).includes(row.status)
    && typeof row.run_id === "string" && Number.isSafeInteger(row.revision));
  if (action && node) return {
    action, label: action === "resume" ? "继续" : "暂停",
    runId: node.run_id, revision: node.revision,
  };
  return {
    action: view.can_submit ? "start" : "waiting",
    label: view.can_submit ? "启动" : "运行中",
    runId: null, revision: null,
  };
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value !== null && typeof value === "object") return `{${Object.entries(value)
    .sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => `${JSON.stringify(key)}:${stable(child)}`)
    .join(",")}}`;
  return JSON.stringify(value);
}

export function createPublication(compiled: CompiledPlan): CompiledPlan {
  const plan = clonePreparation(compiled);
  const items = new Map(plan.items.map((item) => [item.item_id, crypto.randomUUID()]));
  const groups = new Map(plan.groups.map((group) => [group.group_id, crypto.randomUUID()]));
  for (const item of plan.items) {
    item.item_id = items.get(item.item_id)!;
    item.revision = 1;
  }
  for (const group of plan.groups) {
    group.group_id = groups.get(group.group_id)!;
    group.revision = 1;
    for (const member of group.members) {
      member.item_id = items.get(member.item_id)!;
      member.revision = 1;
    }
  }
  plan.config.config_id = crypto.randomUUID();
  plan.config.revision = 1;
  for (const input of plan.config.inputs) {
    if (input.kind === "item") input.item_id = items.get(input.item_id)!;
    else input.group_id = groups.get(input.group_id)!;
    input.revision = 1;
  }
  return plan;
}
function validPending(value: unknown, sid: string | null, selection = false): value is PendingMutation | null {
  if (value === null) return true;
  if (!exactSessionObject(value, ["path", "body", "requestId", "action", "replayable"])
    || typeof value.path !== "string" || !sessionUuid(value.requestId) || typeof value.action !== "string"
    || typeof value.replayable !== "boolean" || !safeSessionJson(value.body) || !exactSessionObject(value.body, [], Object.keys(value.body as object))
    || new TextEncoder().encode(JSON.stringify(value.body)).byteLength > 64 * 1024) return false;
  const body = value.body;
  if (selection) return value.action === "select-session" && value.path === "/api/active-session"
    && value.replayable && exactSessionObject(body, ["workflow_session_id", "expected_selection_revision", "idempotency_key"])
    && sessionUuid(body.workflow_session_id) && sessionInteger(body.expected_selection_revision) && body.idempotency_key === value.requestId;
  if (value.action === "create-session") return sid === null && value.path === "/api/sessions"
    && !value.replayable && exactSessionObject(body, [], ["workflow_id"])
    && (body.workflow_id === undefined || isWorkflowIdentity(body.workflow_id));
  if (!value.replayable || body.idempotency_key !== value.requestId) return false;
  if (value.action === "save-config") {
    const record = body.record;
    return /^\/api\/prompt-configs\/(item|group|config)$/.test(value.path)
      && exactSessionObject(body, ["record", "expected_revision", "idempotency_key"])
      && sessionInteger(body.expected_revision) && validCompiledPromptRecord(record, value.path.split("/").at(-1)!);
  }
  if (!sid || !value.path.startsWith(`/api/sessions/${sid}/`)) return false;
  if (value.action.startsWith("copy-workflow:")) return sessionUuid(value.action.slice(14))
    && value.path === `/api/sessions/${sid}/copy`
    && exactSessionObject(body, ["expected_source_revision", "expected_data_revision", "idempotency_key"], ["target_workflow_id"])
    && (body.target_workflow_id === undefined || body.target_workflow_id === value.action.slice(14))
    && sessionInteger(body.expected_source_revision) && Number(body.expected_source_revision) > 0
    && sessionInteger(body.expected_data_revision);
  const revision = (key: string) => sessionInteger(body[key]) && (body[key] as number) > 0;
  if (value.action === "write-variables") {
    const config = body.prompt_config;
    return value.path === `/api/sessions/${sid}/variables/write`
      && exactSessionObject(body, ["expected_session_revision", "expected_ref_revision", "expected_head_commit_id",
        "prompt_config", "name", "value", "expected_variable_revision", "idempotency_key"])
      && revision("expected_session_revision") && revision("expected_ref_revision") && sessionUuid(body.expected_head_commit_id)
      && sessionInteger(body.expected_variable_revision) && typeof body.name === "string" && variableNamePattern.test(body.name)
      && ["string", "number", "boolean"].includes(typeof body.value)
      && (typeof body.value !== "number" || Number.isFinite(body.value))
      && exactSessionObject(config, ["items", "groups", "config"])
      && Array.isArray(config.items) && config.items.every((item) => validCompiledPromptRecord(item, "item"))
      && Array.isArray(config.groups) && config.groups.every((group) => validCompiledPromptRecord(group, "group"))
      && validCompiledPromptRecord(config.config, "config");
  }
  if (value.action === "start") return value.path === `/api/sessions/${sid}/inputs`
    && exactSessionObject(body, ["text", "idempotency_key"], ["prompt_selection", "model_selection"])
    && typeof body.text === "string" && !!body.text.trim()
    && (body.prompt_selection === undefined || isExactPromptSelection(body.prompt_selection))
    && (body.model_selection === undefined || isExactModelSelection(body.model_selection));
  if (value.action === "fork") return value.path === `/api/sessions/${sid}/branches`
    && exactSessionObject(body, ["visible_message_id", "expected_source_revision", "idempotency_key"], ["candidate_id"])
    && sessionUuid(body.visible_message_id) && revision("expected_source_revision")
    && (body.candidate_id === undefined || sessionUuid(body.candidate_id));
  if (["candidate-select", "reroll"].includes(value.action)) {
    const pattern = value.action === "reroll"
      ? /^\/api\/sessions\/[^/]+\/chains\/([0-9a-f-]+)\/reroll$/
      : /^\/api\/sessions\/[^/]+\/candidates\/([0-9a-f-]+)\/select$/;
    return !!value.path.match(pattern) && sessionUuid(value.path.match(pattern)![1])
      && exactSessionObject(body, ["idempotency_key", "expected_session_revision", "expected_ref_revision", "expected_head_commit_id"])
      && revision("expected_session_revision") && revision("expected_ref_revision") && sessionUuid(body.expected_head_commit_id);
  }
  const endpoint = ({ retry_archive: "retry-archive", retry_publish: "retry-publish" } as Record<string, string>)[value.action] ?? value.action;
  const match = value.path.match(/^\/api\/sessions\/[^/]+\/runs\/([0-9a-f-]+)\/([^/]+)$/);
  if (!match || !sessionUuid(match[1]) || match[2] !== endpoint
    || !["resume", "interrupt", "extend_budget", "retry_archive", "retry_publish"].includes(value.action)) return false;
  const keys = ["expected_session_revision", "idempotency_key"];
  if (["resume", "interrupt", "extend_budget"].includes(value.action)) keys.push("expected_run_revision");
  if (value.action === "extend_budget") keys.push("additional_model_requests", "additional_model_attempts");
  return exactSessionObject(body, keys) && revision("expected_session_revision")
    && (!keys.includes("expected_run_revision") || revision("expected_run_revision"))
    && (!keys.includes("additional_model_requests") || sessionInteger(body.additional_model_requests)
      && sessionInteger(body.additional_model_attempts));
}
export function validRuntimeSnapshot(value: unknown): value is WorkbenchRuntimeSnapshot {
  if (!exactSessionObject(value, ["schemaVersion", "kind", "selectedSessions", "sessions", "selectionPending"])
    || ![1, 2].includes(value.schemaVersion as number) || value.kind !== "workbench-runtime"
    || !Array.isArray(value.selectedSessions) || !Array.isArray(value.sessions)
    || value.selectedSessions.length > 128 || value.sessions.length > 10_000
    || !validPending(value.selectionPending, null, true)) return false;
  const keys = new Set<string>();
  for (const row of value.sessions) {
    if (!exactSessionObject(row, ["workflowId", "sessionId", "pending", "requiresRefresh", "promptSelection",
      ...(value.schemaVersion === 2 ? ["modelSelection"] : [])])
      || !isWorkflowIdentity(row.workflowId)
      || !(row.sessionId === null || sessionUuid(row.sessionId)) || typeof row.requiresRefresh !== "boolean"
      || !validPending(row.pending, row.sessionId as string | null)
      || !(row.promptSelection === null || isExactPromptSelection(row.promptSelection))
      || value.schemaVersion === 2 && !(row.modelSelection === null || isExactModelSelection(row.modelSelection))) return false;
    const key = JSON.stringify([row.workflowId, row.sessionId]);
    if (keys.has(key)) return false;
    keys.add(key);
  }
  const selected = new Set<string>();
  return value.selectedSessions.every((row) => {
    if (!exactSessionObject(row, ["workflowId", "sessionId"]) || typeof row.workflowId !== "string"
      || selected.has(row.workflowId) || !keys.has(JSON.stringify([row.workflowId, row.sessionId]))) return false;
    selected.add(row.workflowId);
    return true;
  });
}

export const useWorkbenchRuntimeStore = defineStore("workbench-runtime", () => {
  const preparation = usePreparationStore();
  const exposures = useExposuresStore();
  const models = useModelConfigurationStore();
  const workflowId = ref("");
  const runtimes = ref<Record<string, WorkflowRuntime>>({});
  const sessionRuntimes = ref<Record<string, WorkflowRuntime>>({});
  const sessions = ref<SessionSummary[]>([]);
  const sessionsLoading = ref(false);
  const selectionPending = ref<PendingMutation | null>(null);
  const selectionBusy = ref(false);
  const candidatePreview = ref<{ messageId: string; candidate: ReplyCandidate } | null>(null);
  const notices = useWorkbenchNoticesStore();
  const { error } = storeToRefs(notices);
  const { notify, clearError } = notices;
  let scopeGeneration = 0;
  let refreshGeneration = 0;
  let controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | null = null;
  let sessionsGeneration = 0;
  let persistenceGuard: (() => boolean) | null = null;
  let copyHandler: ((source: string, target: string, sessionId: string | null) => void) | null = null;
  function setCopyHandler(handler: typeof copyHandler) { copyHandler = handler; }
  onScopeDispose(() => {
    scopeGeneration++;
    refreshGeneration++;
    sessionsGeneration++;
    stopPoll();
    controller.abort();
    exposures.clearObservations();
  });
  const stateKey = (id: string, sid: string | null) => JSON.stringify([id, sid]);

  function runtimeFor(id: string) {
    if (!runtimes.value[id]) runtimes.value[id] = {
      sessionId: null, view: null, pending: null, busy: null, requiresRefresh: false,
      promptSelection: null,
    };
    return runtimes.value[id];
  }

  const current = computed(() => runtimeFor(workflowId.value));
  const session = computed(() => current.value.view as WorkbenchSession | null);
  const sessionId = computed(() => current.value.sessionId);
  const busy = computed(() => current.value.busy ?? (selectionBusy.value ? "select-session" : null));
  const unknown = computed(() => current.value.pending ?? selectionPending.value);
  const currentPromptSelection = computed(() => current.value.promptSelection ?? null);
  const currentModelSelection = computed(() => current.value.modelSelection ?? null);
  const pendingRequests = computed(() => {
    const states = { ...sessionRuntimes.value };
    for (const [id, runtime] of Object.entries(runtimes.value)) states[stateKey(id, runtime.sessionId)] = runtime;
    return Object.entries(states).filter(([, row]) => !!row.pending).map(([key, row]) => ({
      workflowId: JSON.parse(key)[0] as string, sessionId: row.sessionId,
      requestId: row.pending!.requestId, action: row.pending!.action,
    }));
  });
  const refreshRequired = computed(() => current.value.requiresRefresh);
  const executable = computed(() => isWorkflowIdentity(workflowId.value) && workflowId.value !== EMPTY_WORKFLOW_ID);
  const control = computed(() => primaryControl(session.value));
  const inputText = computed(() => executable.value
    ? preparation.getRootText(workflowId.value, "A") : "");
  const locked = computed(() => !executable.value || !!busy.value
    || !!unknown.value || current.value.requiresRefresh || control.value.action === "waiting"
    || control.value.action === "start" && models.locked);
  const secondaryActions = computed(() => session.value?.available_actions.filter(
    (action) => ["extend_budget", "retry_archive", "retry_publish"].includes(action),
  ) ?? []);
  const statusLabel = computed(() => unknown.value ? "状态待核实"
    : busy.value === "interrupt" ? "暂停中"
      : busy.value === "start" ? "启动中"
        : busy.value ? "提交中" : current.value.requiresRefresh ? "状态待刷新" : control.value.label);

  function setInputText(value: string) {
    if (executable.value) preparation.setRootText(workflowId.value, "A", value);
  }

  function nodeStatus(stage: "A" | "B" | "Output") {
    const observed = nodeObservation(stage);
    if (observed) return observed.status;
    const binding = stage === "Output"
      ? "7be319b8-30bd-4674-b7bf-d1cf54a1a10a" : AGENT_BINDINGS[stage];
    return session.value?.nodes.find((node) => node.node_binding_id === binding)?.status
      ?? "未绑定会话";
  }
  function nodeObservation(stage: MainStage) {
    return observedNode(session.value, stage);
  }

  function fence(id: string, generation: number) {
    return workflowId.value === id && scopeGeneration === generation;
  }

  function stopPoll() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  function schedulePoll() {
    stopPoll();
    const view = session.value;
    if (!view) return;
    const terminal = view.chains.every((chain) =>
      ["succeeded", "failed", "superseded", "closed", "paused", "recovery_unavailable"].includes(chain.status));
    // Delivery and worker cleanup can finish after the chain reports success.
    const awaitingCloseout = view.chains.at(-1)?.status === "succeeded" && !view.can_submit
      && !view.pending_input_id && !view.nodes.some((node) =>
        ["paused", "failed", "recovery_unavailable"].includes(node.status));
    if (terminal && !awaitingCloseout) return;
    timer = setTimeout(() => void refresh(), 1200);
  }

  async function activateWorkflow(id: string) {
    if (workflowId.value === id) return;
    controller.abort();
    controller = new AbortController();
    scopeGeneration += 1;
    refreshGeneration += 1;
    stopPoll();
    exposures.clearObservations();
    for (const stage of ["A", "B"] as const) preparation.clearContextCache(workflowId.value, stage);
    candidatePreview.value = null;
    error.value = null;
    workflowId.value = id;
    const runtime = runtimeFor(id);
    // Stored configuration survives; observations are always reread on entry.
    runtime.view = null;
    runtime.requiresRefresh = !!runtime.sessionId;
    if (id !== EMPTY_WORKFLOW_ID && runtime.sessionId) await refresh();
  }

  async function refresh() {
    const id = workflowId.value;
    const generation = scopeGeneration;
    const requestGeneration = ++refreshGeneration;
    const target = runtimeFor(id);
    if (!target.sessionId || id === EMPTY_WORKFLOW_ID) return;
    try {
      const view = await readWorkbenchSession(target.sessionId, controller.signal);
      if (!fence(id, generation) || requestGeneration !== refreshGeneration) return;
      target.view = view;
      if (view.model_selection !== undefined)
        target.modelSelection = clonePreparation(view.model_selection);
      target.requiresRefresh = false;
      exposures.observe(id, view);
      void exposures.observePublished(id, view);
      candidatePreview.value = null;
      schedulePoll();
    } catch (failure) {
      if (!fence(id, generation) || requestGeneration !== refreshGeneration) return;
      target.view = null;
      target.requiresRefresh = true;
      exposures.clearObservations();
      const issue = failure instanceof WorkbenchApiError
        ? failure : new WorkbenchApiError("unavailable", "读取会话状态失败");
      notify(issue.kind, issue.reason);
    }
  }

  function assertScope(id: string, generation: number) {
    if (!fence(id, generation)) throw new WorkbenchApiError("unavailable", "当前打开工作流已变化");
  }
  function setMutationPersistenceGuard(guard: (() => boolean) | null) {
    persistenceGuard = guard;
  }
  function persistBeforeDispatch() {
    if (persistenceGuard && !persistenceGuard())
      throw new WorkbenchApiError("unavailable", "原请求无法保存到本地，操作未提交");
  }
  function persistSettlement() {
    try {
      if (persistenceGuard && !persistenceGuard()) throw new Error("save");
    } catch {
      throw new WorkbenchApiError("unknown", "原请求结果无法保存到本地，保留原请求等待核实");
    }
  }
  function validateHistoryReceipt(receipt: Record<string, unknown>, command: PendingMutation, sid: string | null) {
    const fail = () => { throw new WorkbenchApiError("unknown", "历史操作回执无效，保留原请求等待核实"); };
    if (command.action === "candidate-select" && (
      receipt.workflow_session_id !== sid || receipt.candidate_id !== command.path.split("/").at(-2)
      || !sessionUuid(receipt.head_commit_id) || receipt.status !== "succeeded"
      || receipt.session_revision !== Number(command.body.expected_session_revision) + 1
      || receipt.ref_revision !== Number(command.body.expected_ref_revision) + 1)) fail();
    if (command.action === "reroll" && (
      receipt.workflow_session_id !== sid || !sessionUuid(receipt.chain_run_id)
      || !sessionUuid(receipt.run_id) || !sessionUuid(receipt.input_id)
      || receipt.source_chain_run_id !== command.path.split("/").at(-2)
      || !["prepared", "running", "succeeded", "failed", "paused"].includes(receipt.status as string))) fail();
    if (command.action === "fork" && (
      !sessionUuid(receipt.workflow_session_id) || receipt.source_workflow_session_id !== sid
      || receipt.visible_message_id !== command.body.visible_message_id || !sessionUuid(receipt.fork_anchor_id)
      || !["user", "assistant"].includes(receipt.role as string)
      || !["pending", "created"].includes(receipt.status as string)
      || !(receipt.pending_input_id === null || sessionUuid(receipt.pending_input_id))
      || !sessionUuid(receipt.active_workflow_session_id))) fail();
  }

  async function mutate(
    id: string,
    generation: number,
    command: PendingMutation,
  ): Promise<Record<string, unknown>> {
    assertScope(id, generation);
    const target = runtimeFor(id);
    if (target.pending) throw new WorkbenchApiError("unknown", "原请求尚待核实，禁止重发或覆盖");
    if (!validPending(command, target.sessionId))
      throw new WorkbenchApiError("unavailable", "请求字段不兼容或超过本地接口 64KB 容量，操作未提交");
    command = clonePreparation(command);
    const sid = target.sessionId;
    const identity = stable(command);
    const stateFence = () => fence(id, generation) && runtimes.value[id] === target;
    const ownsPending = () => stateFence() && target.sessionId === sid
      && target.pending !== null && stable(target.pending) === identity;
    target.pending = clonePreparation(command);
    try { persistBeforeDispatch(); } catch (failure) {
      if (ownsPending()) target.pending = null;
      throw failure;
    }
    if (!ownsPending()) throw new WorkbenchApiError("unknown", "原请求或恢复代际已变化，操作未提交");
    try {
      const receipt = await workbenchRequest<Record<string, unknown>>(command.path, {
        body: command.body, signal: controller.signal,
      });
      if (!ownsPending()) throw new WorkbenchApiError("unknown", "迟到回执未应用，原请求仍待核实");
      const validUuid = sessionUuid;
      const record = command.body.record as Record<string, unknown> | undefined;
      const head = receipt?.head as Record<string, unknown> | undefined;
      const recordId = record && (record.item_id ?? record.group_id ?? record.config_id);
      const controlAction = ["interrupt", "resume", "extend_budget", "retry_archive", "retry_publish"].includes(command.action);
      if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)
        || (command.action === "create-session" && (!validUuid(receipt.workflow_session_id)
          || !validCreatedSession(receipt, receipt.workflow_session_id as string)))
        || (command.action === "start" && (!validUuid(receipt.chain_run_id)
          || !validUuid(receipt.visible_message_id) || !validUuid(receipt.input_id)
          || receipt.status !== "prepared" || receipt.workflow_session_id !== target.sessionId))
        || (command.action === "save-config" && (!record || !head || stable(receipt.record) !== stable(record)
          || head.id !== recordId || head.kind !== record.kind
          || head.definition_revision !== record.revision
          || head.catalog_revision !== Number(command.body.expected_revision) + 1 || head.selectable !== true))
        || (controlAction && (!validUuid(receipt.run_id) || !validUuid(receipt.chain_run_id)
          || !command.path.includes(`/runs/${receipt.run_id}/`)
          || !["running", "pausing", "paused", "succeeded", "failed"].includes(receipt.status as string)
          || receipt.workflow_session_id !== target.sessionId)))
        throw new WorkbenchApiError("unknown", "提交回执字段不完整，保留原请求等待核实");
      validateHistoryReceipt(receipt, command, target.sessionId);
      if (command.action.startsWith("copy-workflow:") && (
        !sessionUuid(receipt.workflow_session_id) || receipt.workflow_session_id === target.sessionId
        || ("source_workflow_session_id" in receipt
          ? receipt.source_workflow_session_id !== target.sessionId || receipt.role !== "assistant"
            || receipt.status !== "created" || !sessionUuid(receipt.fork_anchor_id)
          : !validCreatedSession(receipt, receipt.workflow_session_id as string))
      )) throw new WorkbenchApiError("unknown", "复制会话回执不完整，保留原请求等待核实");
      if (command.action === "write-variables") {
        try {
          decodeWorkbenchVariableWrite(receipt, target.sessionId!, {
            idempotency_key: command.requestId, name: command.body.name as string,
            expected_variable_revision: command.body.expected_variable_revision as number,
            value: command.body.value as VariableValue,
          });
        }
        catch { throw new WorkbenchApiError("unknown", "变量写入回执无法确认，保留原请求等待核实"); }
      }
      const previous = {
        sessionId: target.sessionId, promptSelection: target.promptSelection,
        modelSelection: target.modelSelection, requiresRefresh: target.requiresRefresh,
      };
      const previousStates = sessionRuntimes.value;
      const settledStates = { ...previousStates };
      if (command.action === "create-session") {
        delete settledStates[stateKey(id, null)];
        target.sessionId = receipt.workflow_session_id as string;
      }
      if (command.action === "start" && isExactPromptSelection(command.body.prompt_selection))
        target.promptSelection = clonePreparation(command.body.prompt_selection);
      if (command.action === "start")
        target.modelSelection = isExactModelSelection(command.body.model_selection)
          ? clonePreparation(command.body.model_selection) : null;
      settledStates[stateKey(id, target.sessionId)] = target;
      sessionRuntimes.value = settledStates;
      const savedStates = sessionRuntimes.value;
      const settledSid = target.sessionId;
      if (command.action !== "save-config") target.requiresRefresh = true;
      target.pending = null;
      try { persistSettlement(); } catch (failure) {
        if (stateFence() && target.sessionId === settledSid && target.pending === null) {
          Object.assign(target, previous, { pending: clonePreparation(command) });
          if (sessionRuntimes.value === savedStates) sessionRuntimes.value = previousStates;
        }
        throw failure;
      }
      if (!stateFence() || target.sessionId !== settledSid || target.pending !== null)
        throw new WorkbenchApiError("unknown", "保存期间原请求或恢复代际已变化，迟到结果未继续应用");
      if (command.action.startsWith("copy-workflow:"))
        copyHandler?.(id, command.action.slice(14), receipt.workflow_session_id as string);
      return receipt;
    } catch (failure) {
      const issue = failure instanceof WorkbenchApiError
        ? failure : new WorkbenchApiError("unknown", "提交结果待核实");
      issue.requestId = command.requestId;
      if (issue.code === "idempotency_conflict") issue.kind = "unknown";
      if (issue.kind !== "unknown" && ownsPending()) {
        target.pending = null;
        try { persistSettlement(); } catch (storageFailure) {
          if (stateFence() && target.sessionId === sid && target.pending === null)
            target.pending = clonePreparation(command);
          const storageIssue = storageFailure as WorkbenchApiError;
          storageIssue.requestId = command.requestId;
          if (fence(id, generation)) notify(storageIssue.kind, storageIssue.reason, command.requestId);
          throw storageIssue;
        }
      }
      if (fence(id, generation)) notify(issue.kind, issue.reason, command.requestId);
      throw issue;
    }
  }
  function validCreatedSession(receipt: unknown, sid: string) {
    try { decodeWorkbenchSession(receipt, sid); return true; } catch { return false; }
  }

  async function loadSessions(silent = false) {
    const request = ++sessionsGeneration;
    const id = workflowId.value;
    const generation = scopeGeneration;
    sessionsLoading.value = true;
    try {
      const rows = await listWorkbenchSessions();
      if (request === sessionsGeneration) {
        const owned = new Set(Object.entries(sessionRuntimes.value)
          .filter(([key]) => JSON.parse(key)[0] === id).map(([, state]) => state.sessionId));
        owned.add(runtimeFor(id).sessionId);
        const others = new Set(Object.entries(sessionRuntimes.value)
          .filter(([key]) => JSON.parse(key)[0] !== id).map(([, state]) => state.sessionId));
        sessions.value = rows.filter(row => row.workflow_id ? row.workflow_id === id
          : id === MAIN_WORKFLOW_ID ? !others.has(row.workflow_session_id) : owned.has(row.workflow_session_id));
      }
    } catch (failure) {
      if (!silent && request === sessionsGeneration && fence(id, generation)) {
        const issue = failure instanceof WorkbenchApiError ? failure : new WorkbenchApiError("unavailable", "会话列表读取失败");
        notify(issue.kind, issue.reason);
      }
    } finally {
      if (request === sessionsGeneration) sessionsLoading.value = false;
    }
  }
  async function observeSession(sid: string | null, read = true) {
    const id = workflowId.value;
    const previous = runtimeFor(id);
    sessionRuntimes.value[stateKey(id, previous.sessionId)] = previous;
    controller.abort();
    controller = new AbortController();
    scopeGeneration += 1;
    refreshGeneration += 1;
    stopPoll();
    exposures.clearObservations();
    for (const stage of ["A", "B"] as const) preparation.clearContextCache(id, stage);
    candidatePreview.value = null;
    error.value = null;
    previous.view = null;
    const next = sessionRuntimes.value[stateKey(id, sid)] ?? {
      sessionId: sid, view: null, pending: null, busy: null, requiresRefresh: !!sid, promptSelection: null,
    };
    next.view = null;
    next.requiresRefresh = !!sid;
    runtimes.value[id] = next;
    sessionRuntimes.value[stateKey(id, sid)] = next;
    if (read && sid) await refresh();
  }
  async function restoreSession(sid: string) {
    if (!executable.value || !sessionUuid(sid)) { notify("unavailable", "当前工作流或会话身份不可恢复"); return; }
    await observeSession(sid);
  }
  async function restoreUnboundSession() {
    if (executable.value) await observeSession(null, false);
  }
  async function selectSession(sid: string) {
    if (!executable.value || !sessionUuid(sid) || selectionBusy.value || selectionPending.value) {
      notify("unavailable", "会话选择尚不可提交");
      return;
    }
    const id = workflowId.value;
    const generation = scopeGeneration;
    selectionBusy.value = true;
    try {
      const active = await readSessionSelection(controller.signal);
      assertScope(id, generation);
      if (active.active_workflow_session_id !== sid) {
        const operation = command("/api/active-session", {
          workflow_session_id: sid, expected_selection_revision: active.revision,
        }, "select-session");
        await mutateSessionSelection(operation, id, generation);
      }
      assertScope(id, generation);
      await observeSession(sid);
    } catch (failure) {
      if (fence(id, generation)) {
        const issue = failure instanceof WorkbenchApiError ? failure : new WorkbenchApiError("unavailable", "切换会话失败");
        notify(issue.kind, issue.reason, issue.requestId);
      }
    } finally { selectionBusy.value = false; }
  }
  async function mutateSessionSelection(operation: PendingMutation, id: string, generation: number) {
    assertScope(id, generation);
    if (selectionPending.value) throw new WorkbenchApiError("unknown", "原会话选择尚待核实，禁止重发或覆盖");
    if (!validPending(operation, null, true))
      throw new WorkbenchApiError("unavailable", "会话选择请求无法恢复，操作未提交");
    operation = clonePreparation(operation);
    const identity = stable(operation);
    const ownsPending = () => fence(id, generation) && selectionPending.value !== null
      && stable(selectionPending.value) === identity;
    selectionPending.value = clonePreparation(operation);
    try { persistBeforeDispatch(); } catch (failure) {
      if (ownsPending()) selectionPending.value = null;
      throw failure;
    }
    if (!ownsPending()) throw new WorkbenchApiError("unknown", "原会话选择或恢复代际已变化，操作未提交");
    try {
      const result = decodeSessionSelection(await workbenchRequest<unknown>("/api/active-session", {
        body: operation.body, signal: controller.signal,
      }));
      if (!ownsPending()) throw new WorkbenchApiError("unknown", "迟到会话选择回执未应用，原请求仍待核实");
      if (result.active_workflow_session_id !== operation.body.workflow_session_id
        || result.revision !== Number(operation.body.expected_selection_revision) + 1)
        throw new WorkbenchApiError("unknown", "会话选择回执未对应原请求");
      selectionPending.value = null;
      try { persistSettlement(); } catch (failure) {
        if (fence(id, generation) && selectionPending.value === null)
          selectionPending.value = clonePreparation(operation);
        throw failure;
      }
      if (!fence(id, generation) || selectionPending.value !== null)
        throw new WorkbenchApiError("unknown", "保存期间会话选择或恢复代际已变化，迟到结果未继续应用");
    } catch (failure) {
      const issue = failure instanceof WorkbenchApiError ? failure : new WorkbenchApiError("unknown", "会话选择结果待核实");
      issue.requestId = operation.requestId;
      if (issue.code === "idempotency_conflict") issue.kind = "unknown";
      if (issue.kind !== "unknown" && issue.kind !== "unavailable" && ownsPending()) {
        selectionPending.value = null;
        try { persistSettlement(); } catch (storageFailure) {
          if (fence(id, generation) && selectionPending.value === null)
            selectionPending.value = clonePreparation(operation);
          const storageIssue = storageFailure as WorkbenchApiError;
          storageIssue.requestId = operation.requestId;
          throw storageIssue;
        }
      }
      if (issue.kind === "unavailable") {
        issue.kind = "unknown";
        issue.reason = "会话选择回执无法校验，保留原请求等待核实";
      }
      throw issue;
    }
  }
  async function createSession() {
    if (!executable.value || selectionBusy.value || selectionPending.value || busy.value) {
      notify("unavailable", "当前状态不能新建会话");
      return;
    }
    const unbound = sessionRuntimes.value[stateKey(workflowId.value, null)];
    if (unbound?.pending || current.value.sessionId === null && current.value.pending) {
      notify("unavailable", "未绑定会话的原请求尚待核实，不能再次新建");
      return;
    }
    await observeSession(null, false);
    const id = workflowId.value;
    const generation = scopeGeneration;
    const target = current.value;
    target.busy = "create-session";
    try {
      await ensureServiceMode(id, generation);
      const receipt = await mutate(id, generation, {
        path: "/api/sessions", body: id === MAIN_WORKFLOW_ID ? {} : { workflow_id: id },
        requestId: crypto.randomUUID(), action: "create-session", replayable: false,
      });
      assertScope(id, generation);
      target.busy = null;
      await selectSession(receipt.workflow_session_id as string);
      await loadSessions();
    } catch (failure) {
      if (fence(id, generation)) {
        const issue = failure instanceof WorkbenchApiError ? failure : new WorkbenchApiError("unavailable", "新建会话失败");
        notify(issue.kind, issue.reason, issue.requestId);
      }
    } finally { target.busy = null; }
  }
  function previewCandidate(messageId: string, candidateId: string | null) {
    if (candidateId === null) { candidatePreview.value = null; return; }
    const candidate = session.value?.messages.find((message) => message.visible_message_id === messageId)
      ?.reply_candidates?.find((row) => row.candidate_id === candidateId);
    if (candidate) candidatePreview.value = { messageId, candidate: clonePreparation(candidate) };
  }
  function historyCas(view: WorkbenchSession) {
    if (!sessionInteger(view.revision) || view.revision < 1
      || !sessionInteger(view.ref_revision) || view.ref_revision < 1 || !sessionUuid(view.head_commit_id))
      throw new WorkbenchApiError("unavailable", "历史操作缺少权威会话与 Head 版本");
    return {
      expected_session_revision: view.revision, expected_ref_revision: view.ref_revision,
      expected_head_commit_id: view.head_commit_id,
    };
  }
  async function historyAction(action: string, targetId: string, candidateId?: string) {
    if (!executable.value || busy.value || unknown.value || refreshRequired.value || !session.value || !sessionId.value) {
      notify("unavailable", "当前会话不能提交历史操作");
      return;
    }
    const id = workflowId.value;
    const generation = scopeGeneration;
    const target = current.value;
    const view = session.value;
    const sid = sessionId.value;
    target.busy = action;
    try {
      let path: string;
      let body: Record<string, unknown>;
      if (action === "candidate-select") {
        if (!view.can_select_candidates || !view.messages.at(-1)?.reply_candidates?.some((row) => row.candidate_id === targetId && !row.selected))
          throw new WorkbenchApiError("unavailable", "只能选用当前末尾回复的非当前候选");
        path = `/api/sessions/${sid}/candidates/${targetId}/select`;
        body = historyCas(view);
      } else if (action === "reroll") {
        if (!view.can_reroll || !sessionUuid(targetId)) throw new WorkbenchApiError("unavailable", "当前轮次不可重抽样");
        path = `/api/sessions/${sid}/chains/${targetId}/reroll`;
        body = historyCas(view);
      } else {
        const index = view.messages.findIndex((row) => row.visible_message_id === targetId);
        const message = view.messages[index];
        if (!message || candidateId && !message.reply_candidates?.some((row) => row.candidate_id === candidateId))
          throw new WorkbenchApiError("unavailable", "分叉锚点或候选不属于当前历史");
        if (view.chains.some((chain) => !["succeeded", "superseded", "closed"].includes(chain.status))
          || message.role === "user" && view.messages[index + 1]?.role !== "assistant")
          throw new WorkbenchApiError("unavailable", "当前消息没有可分叉的已完成边界");
        path = `/api/sessions/${sid}/branches`;
        body = {
          visible_message_id: targetId, expected_source_revision: view.revision,
          ...(candidateId ? { candidate_id: candidateId } : {}),
        };
      }
      await ensureServiceMode(id, generation);
      const receipt = await mutate(id, generation, command(path, body, action));
      if (fence(id, generation)) {
        if (action === "fork") {
          const child = receipt.workflow_session_id as string;
          sessionRuntimes.value[stateKey(id, child)] = {
            sessionId: child, pending: null, view: null, busy: null, requiresRefresh: true,
            promptSelection: clonePreparation(target.promptSelection ?? null),
            modelSelection: clonePreparation(target.modelSelection ?? null),
          };
          target.busy = null;
          await selectSession(child);
          await loadSessions();
        } else await refresh();
      }
    } catch (failure) {
      if (fence(id, generation) && !target.pending) {
        const issue = failure instanceof WorkbenchApiError ? failure : new WorkbenchApiError("unavailable", "历史操作失败");
        notify(issue.kind, issue.reason, issue.requestId);
        await refresh();
      }
    } finally { target.busy = null; }
  }
  const selectCandidate = (candidateId: string) => historyAction("candidate-select", candidateId);
  const reroll = (chainId: string) => historyAction("reroll", chainId);
  const forkAt = (messageId: string, candidateId?: string) => historyAction("fork", messageId, candidateId);

  function exportRuntimeSnapshot(): WorkbenchRuntimeSnapshot {
    const states = { ...sessionRuntimes.value };
    for (const [id, runtime] of Object.entries(runtimes.value)) if (id) states[stateKey(id, runtime.sessionId)] = runtime;
    return clonePreparation({
      schemaVersion: 2, kind: "workbench-runtime",
      selectedSessions: Object.entries(runtimes.value).filter(([id]) => !!id).map(([workflowId, row]) => ({
        workflowId, sessionId: row.sessionId,
      })),
      sessions: Object.entries(states).filter(([key]) => !!JSON.parse(key)[0]).map(([key, row]) => ({
        workflowId: JSON.parse(key)[0], sessionId: row.sessionId, pending: row.pending,
        requiresRefresh: row.requiresRefresh, promptSelection: row.promptSelection ?? null,
        modelSelection: row.modelSelection ?? null,
      })),
      selectionPending: selectionPending.value,
    });
  }
  function restoreRuntimeSnapshot(value: unknown): boolean {
    if (!validRuntimeSnapshot(value)) {
      notify("unavailable", "本地会话恢复记录损坏或版本不兼容，未覆盖当前状态");
      return false;
    }
    controller.abort();
    controller = new AbortController();
    scopeGeneration += 1;
    refreshGeneration += 1;
    stopPoll();
    exposures.clearObservations();
    candidatePreview.value = null;
    const states: Record<string, WorkflowRuntime> = {};
    for (const row of value.sessions) states[stateKey(row.workflowId, row.sessionId)] = {
      sessionId: row.sessionId, pending: clonePreparation(row.pending), requiresRefresh: !!row.sessionId || row.requiresRefresh,
      promptSelection: clonePreparation(row.promptSelection), view: null, busy: null,
      modelSelection: clonePreparation(row.modelSelection ?? null),
    };
    const selected: Record<string, WorkflowRuntime> = {};
    for (const row of value.selectedSessions) selected[row.workflowId] = states[stateKey(row.workflowId, row.sessionId)];
    sessionRuntimes.value = states;
    runtimes.value = selected;
    selectionPending.value = clonePreparation(value.selectionPending);
    selectionBusy.value = false;
    return true;
  }

  function command(path: string, body: Record<string, unknown>, action: string): PendingMutation {
    const requestId = crypto.randomUUID();
    return { path, body: { ...body, idempotency_key: requestId }, requestId, action, replayable: true };
  }

  async function ensureServiceMode(id: string, generation: number) {
    const health = await workbenchRequest<{ mode: string }>("/api/health", { signal: controller.signal });
    assertScope(id, generation);
    if (!["offline", "deepseek"].includes(health.mode)) throw new WorkbenchApiError(
      "unavailable", "后端模式尚不受当前工作台支持",
    );
  }

  async function savePlan(id: string, generation: number, plan: CompiledPlan) {
    const records = [
      ...plan.items.map((record) => ({ kind: "item", identity: record.item_id, record })),
      ...plan.groups.map((record) => ({ kind: "group", identity: record.group_id, record })),
      { kind: "config", identity: plan.config.config_id, record: plan.config },
    ];
    for (const entry of records) {
      assertScope(id, generation);
      const revisionPath = `/api/prompt-configs/${entry.kind}/${entry.identity}/revisions/${entry.record.revision}`;
      try {
        const existing = await workbenchRequest<Record<string, unknown>>(revisionPath, { signal: controller.signal });
        if (stable(existing) !== stable(entry.record)) throw new WorkbenchApiError(
          "rejected", "提示词身份与已保存修订的内容不一致",
        );
        continue;
      } catch (failure) {
        if (!(failure instanceof WorkbenchApiError) || failure.status !== 404) throw failure;
      }
      let expected = 0;
      try {
        const head = await workbenchRequest<{ catalog_revision: number; definition_revision: number }>(
          `/api/prompt-configs/${entry.kind}/${entry.identity}`, { signal: controller.signal },
        );
        expected = head.catalog_revision;
        if (entry.record.revision !== head.definition_revision + 1)
          throw new WorkbenchApiError("rejected", "提示词修订不连续，不能静默跳过未保存编辑");
      } catch (failure) {
        if (!(failure instanceof WorkbenchApiError) || failure.status !== 404) throw failure;
        if (entry.record.revision !== 1) throw new WorkbenchApiError(
          "rejected", "首次导入的提示词修订必须为 1",
        );
      }
      await mutate(id, generation, command(`/api/prompt-configs/${entry.kind}`, {
        record: entry.record, expected_revision: expected,
      }, "save-config"));
    }
  }

  function compile(stage: "A" | "B"): CompiledPlan {
    const result = compilePromptItems(preparation.getExecutionDraft(workflowId.value, stage));
    const diagnostic = result.diagnostics.find((row) => row.severity === "error");
    if (diagnostic) throw new WorkbenchApiError("unavailable", diagnostic.message ?? `${stage} 装配配置无效`);
    if (result.tools.length !== 1 || result.tools[0]?.name !== "inspect_text" || result.tools[0]?.version !== "1") throw new WorkbenchApiError(
      "unavailable", `${stage} 的工具集合不受固定后端支持`,
    );
    if (stage === "B" && preparation.getRootText(workflowId.value, "B").trim())
      throw new WorkbenchApiError("unavailable", "Agent B 当前输入由 A 的业务结果提供，不能提交本地自定义输入");
    return createPublication(result);
  }

  async function submitPrimary() {
    if (locked.value) {
      notify("unavailable", unknown.value ? "原请求尚待核实，禁止提交新操作"
        : executable.value ? "当前运行状态不可操作" : "空工作流没有正式可执行目标");
      return;
    }
    const id = workflowId.value;
    const generation = scopeGeneration;
    const target = runtimeFor(id);
    const action = control.value.action;
    let createdSession = false;
    target.busy = action;
    try {
      await ensureServiceMode(id, generation);
      if (action === "start") {
        const text = preparation.getRootText(id, "A");
        if (!text.trim()) throw new WorkbenchApiError("unavailable", "Agent A 当前输入不能为空");
        const a = compile("A");
        const b = compile("B");
        let modelSelection = clonePreparation(target.modelSelection ?? null);
        const draft = models.drafts.find((draft) => draft.workflowId === id);
        if (draft && (draft.nodes.length || draft.backendRevision > 0)) {
          if (draft.nodes.length && (!draft.nodes.every((node) => node.provider_ref)
            || !Object.values(AGENT_BINDINGS).every((binding) =>
              draft.edges.some((edge) => edge.target_binding_id === binding))))
            throw new WorkbenchApiError("unavailable", "模型供应商或 A/B 模型依赖缺失，未启动");
          const record = await models.publishModels(id);
          assertScope(id, generation);
          modelSelection = {
            schema_version: 1, kind: "workflow_model_selection",
            config_id: record.config_id, revision: record.revision,
          };
        }
        await savePlan(id, generation, a);
        await savePlan(id, generation, b);
        if (!target.sessionId) {
          const requestId = crypto.randomUUID();
          const created = await mutate(id, generation, {
            path: "/api/sessions", body: id === MAIN_WORKFLOW_ID ? {} : { workflow_id: id },
            requestId, action: "create-session", replayable: false,
          });
          if (typeof created.workflow_session_id !== "string") {
            throw new WorkbenchApiError("unknown", "新建会话回执不完整，不能识别所属会话");
          }
          target.sessionId = created.workflow_session_id;
          createdSession = true;
        }
        await refresh();
        assertScope(id, generation);
        if (!target.view || !["offline", "deepseek"].includes(target.view.mode) || !target.view.can_submit)
          throw new WorkbenchApiError("unavailable", "当前会话不允许首次启动");
        await mutate(id, generation, command(
          `/api/sessions/${encodeURIComponent(target.sessionId)}/inputs`,
          {
            text,
            ...(modelSelection ? { model_selection: modelSelection } : {}),
            prompt_selection: {
              schema_version: 1, kind: "workflow_prompt_selection",
              nodes: {
                A: { config_id: a.config.config_id, revision: a.config.revision },
                B: { config_id: b.config.config_id, revision: b.config.revision },
              },
            },
          }, "start",
        ));
      } else {
        const available = primaryControl(target.view);
        if (available.action !== action || !available.runId || !target.sessionId)
          throw new WorkbenchApiError("unavailable", "运行目标已变化，请刷新");
        await mutate(id, generation, command(
          `/api/sessions/${target.sessionId}/runs/${available.runId}/${action}`,
          { expected_session_revision: target.view!.revision, expected_run_revision: available.revision }, action,
        ));
      }
      if (fence(id, generation)) await refresh();
    } catch (failure) {
      if (fence(id, generation) && !target.pending) {
        const issue = failure instanceof WorkbenchApiError
          ? failure : new WorkbenchApiError("unavailable", "当前配置尚不能由固定后端执行");
        notify(issue.kind, issue.reason, issue.requestId);
        await refresh();
      }
    } finally {
      target.busy = null;
      if (createdSession && fence(id, generation)) await loadSessions(true);
    }
  }
  async function copyWorkflowSession(targetId: string, sourceId = workflowId.value) {
    const id = sourceId;
    const generation = scopeGeneration;
    const target = runtimeFor(id);
    if (target.busy || target.pending || target.requiresRefresh || !sessionUuid(targetId))
      throw new WorkbenchApiError("unavailable", "当前会话或原请求未稳定，不能复制工作流");
    if (!target.sessionId) {
      copyHandler?.(id, targetId, null);
      return;
    }
    assertScope(id, generation);
    await refresh();
    assertScope(id, generation);
    if (!target.view?.can_submit || !target.view.revision)
      throw new WorkbenchApiError("unavailable", "活动运行不可复制，请先完成当前运行");
    target.busy = "copy-workflow";
    try {
      const data = await workbenchRequest<{ revision: number }>(`/api/sessions/${target.sessionId}/data/revision`, { body: {} });
      assertScope(id, generation);
      if (!sessionInteger(data.revision)) throw new WorkbenchApiError("unavailable", "会话数据版本读取无效");
      await mutate(id, generation, command(`/api/sessions/${target.sessionId}/copy`, {
        expected_source_revision: target.view.revision, expected_data_revision: data.revision,
        target_workflow_id: targetId,
      }, `copy-workflow:${targetId}`));
    } finally { target.busy = null; }
  }
  function hasWorkflowPending(id: string) {
    return !!runtimes.value[id]?.pending || Object.entries(sessionRuntimes.value)
      .some(([key, state]) => JSON.parse(key)[0] === id && !!state.pending);
  }
  function bindWorkflowSession(sourceId: string, targetId: string, sid: string | null) {
    if (hasWorkflowPending(targetId)) {
      notify("unknown", "目标工作流仍有待核实请求，原会话归属未覆盖");
      return false;
    }
    const source = runtimeFor(sourceId);
    const state: WorkflowRuntime = {
      sessionId: sid, view: null, busy: null, pending: null, requiresRefresh: !!sid,
      promptSelection: clonePreparation(source.promptSelection ?? null), modelSelection: clonePreparation(source.modelSelection ?? null),
    };
    runtimes.value[targetId] = state;
    sessionRuntimes.value[stateKey(targetId, sid)] = state;
    return true;
  }
  function removeWorkflow(id: string) {
    if (hasWorkflowPending(id)) {
      notify("unknown", "工作流仍有待核实请求，原记录未删除");
      return false;
    }
    delete runtimes.value[id];
    for (const key of Object.keys(sessionRuntimes.value))
      if (JSON.parse(key)[0] === id) delete sessionRuntimes.value[key];
    if (workflowId.value === id) {
      scopeGeneration++;
      refreshGeneration++;
      stopPoll();
    }
    return true;
  }

  async function submitSecondary(action: string, requests = 1, attempts = 4) {
    if (locked.value || !secondaryActions.value.includes(action)) {
      // Waiting primary controls do not prohibit an explicitly allowed closeout/budget action.
      if (!executable.value || busy.value || unknown.value || refreshRequired.value || !secondaryActions.value.includes(action)) {
        notify("unavailable", "当前状态不允许此操作");
        return;
      }
    }
    const id = workflowId.value;
    const generation = scopeGeneration;
    const target = runtimeFor(id);
    const view = target.view;
    if (!view || !target.sessionId) return;
    const node = view.nodes.find((row) => {
      if (!Object.values(AGENT_BINDINGS).includes(row.node_binding_id as typeof AGENT_BINDINGS.A)) return false;
      return action === "retry_archive" ? row.status === "final_ready"
        : action === "retry_publish" ? row.node_binding_id === AGENT_BINDINGS.B && row.status === "succeeded"
          : ["paused", "failed"].includes(row.status) && row.budget && Number.isSafeInteger(row.revision);
    });
    if (!node?.run_id) { notify("unavailable", "缺少可操作的运行目标"); return; }
    const body: Record<string, unknown> = { expected_session_revision: view.revision };
    if (action === "extend_budget") {
      if (!Number.isSafeInteger(requests) || !Number.isSafeInteger(attempts)
        || requests < 0 || attempts < 0 || requests + attempts === 0
        || requests + node.budget!.max_model_requests > 64
        || attempts + node.budget!.max_model_attempts > 256) {
        notify("unavailable", "预算增额超出允许范围");
        return;
      }
      Object.assign(body, {
        expected_run_revision: node.revision,
        additional_model_requests: requests, additional_model_attempts: attempts,
      });
    }
    target.busy = action;
    try {
      await ensureServiceMode(id, generation);
      const endpoint = action === "retry_archive" ? "retry-archive"
        : action === "retry_publish" ? "retry-publish" : action;
      await mutate(id, generation, command(
        `/api/sessions/${target.sessionId}/runs/${node.run_id}/${endpoint}`, body, action,
      ));
      if (fence(id, generation)) await refresh();
    } catch (failure) {
      if (fence(id, generation) && !target.pending) {
        const issue = failure instanceof WorkbenchApiError ? failure : new WorkbenchApiError("unavailable", "提交失败");
        notify(issue.kind, issue.reason, issue.requestId);
        await refresh();
      }
    } finally { target.busy = null; }
  }

  async function replayUnknown() {
    const target = runtimeFor(workflowId.value);
    const pending = target.pending ?? selectionPending.value;
    if (!pending || target.busy) return;
    notify("unknown", "旧请求缺少可证明的应用回执归属，无法核实（unresolved）；未重发，原请求已保留", pending.requestId);
  }

  async function writeSessionVariable(
    scope: WorkbenchContextScope, promptConfig: InlinePromptConfiguration,
    name: string, value: VariableValue, expectedRevision: number,
  ): Promise<WorkbenchVariableRead | null> {
    if (!executable.value || busy.value || unknown.value || refreshRequired.value
      || workflowId.value !== scope.workflowId || sessionId.value !== scope.sessionId
      || session.value?.revision !== scope.sessionRevision || session.value?.ref_revision !== scope.refRevision
      || session.value?.head_commit_id !== scope.headCommitId) return null;
    const id = workflowId.value;
    const generation = scopeGeneration;
    const target = current.value;
    target.busy = "write-variables";
    try {
      const operation = command(
        `/api/sessions/${scope.sessionId}/variables/write`,
        { ...variableReadBody(scope, promptConfig), name, value, expected_variable_revision: expectedRevision },
        "write-variables",
      );
      const receipt = await mutate(id, generation, operation);
      const result = decodeWorkbenchVariableWrite(receipt, scope.sessionId, {
        idempotency_key: operation.requestId, name, expected_variable_revision: expectedRevision, value,
      });
      if (fence(id, generation)) await refresh();
      return fence(id, generation) ? result : null;
    } catch {
      if (fence(id, generation) && !target.pending) await refresh();
      return null;
    } finally { target.busy = null; }
  }

  return {
    workflowId, runtimes, sessionRuntimes, session, sessionId, busy, unknown, refreshRequired, executable, control, inputText,
    sessions, sessionsLoading, selectionPending, candidatePreview, currentPromptSelection, currentModelSelection, pendingRequests,
    locked, secondaryActions, statusLabel, error, notify, clearError, setInputText, nodeStatus, nodeObservation,
    activateWorkflow, refresh, submitPrimary, submitSecondary, replayUnknown,
    loadSessions, createSession, selectSession, restoreSession, restoreUnboundSession, previewCandidate, selectCandidate, reroll, forkAt,
    exportRuntimeSnapshot, restoreRuntimeSnapshot, setMutationPersistenceGuard,
    writeSessionVariable,
    copyWorkflowSession, bindWorkflowSession, setCopyHandler, removeWorkflow,
  };
});
