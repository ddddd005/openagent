import { computed, onScopeDispose, ref, watch } from "vue";
import { defineStore } from "pinia";
import { AGENT_BINDINGS, WorkbenchApiError, workbenchRequest } from "../adapters/workbenchApi";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { isWorkflowIdentity, type WorkflowEditGuard } from "../domain/workflowIdentity";
import {
  cloneModel, dependencyTarget, isChatProvider, isConfigurationMutation, isModelConfiguration,
  isModelConfigurationSnapshot, MAX_MODEL_NODES, MODEL_DIAGNOSTIC_MESSAGES, modelParameterDiagnostics,
  modelPublication, modelSignature, providerFieldDiagnostics, uuid,
  type ChatProvider, type ConfigurationMutation, type ModelConfiguration,
  type ModelConfigurationSnapshot, type ModelDraft,
} from "../domain/modelConfiguration";

export const useModelConfigurationStore = defineStore("model-configuration", () => {
  const providers = ref<ChatProvider[]>([]);
  const providersLoaded = ref(false);
  const loading = ref(false);
  const drafts = ref<ModelDraft[]>([]);
  const selectedNodeId = ref<string | null>(null);
  const pending = ref<ConfigurationMutation | null>(null);
  const busy = ref(false);
  const error = ref<WorkbenchApiError | null>(null);
  const diagnostics = ref<{ target: string; code: string }[]>([]);
  const providerForm = ref<ChatProvider | null>(null);
  const providerDiagnostics = computed(() => providerForm.value ? providerFieldDiagnostics(providerForm.value) : []);
  const savedConfigurations = ref<Record<string, ModelConfiguration>>({});
  let guard: (() => boolean) | null = null;
  let editGuard: WorkflowEditGuard | null = null;
  function setEditGuard(value: WorkflowEditGuard) { editGuard = value; }
  function cloneWorkflow(source: string, target: string) {
    const draft = getDraft(source);
    if (draft) drafts.value.push({ ...cloneModel(draft), workflowId: target, configId: crypto.randomUUID(), backendRevision: 0 });
  }
  function restoreWorkflowDraft(workflowId: string, draft: ModelDraft | null) {
    drafts.value = drafts.value.filter(row => row.workflowId !== workflowId);
    if (draft) drafts.value.push({ ...cloneModel(draft), workflowId });
    selectedNodeId.value = null;
    diagnostics.value = [];
  }
  function removeWorkflow(workflowId: string) {
    restoreWorkflowDraft(workflowId, null);
  }
  function updateNode(workflowId: string, nodeId: string, change: Record<string, unknown>) {
    if (editGuard && !editGuard(workflowId, id => updateNode(id, nodeId, change))) return;
    const node = getDraft(workflowId)?.nodes.find(n => n.id === nodeId);
    if (!node) return;
    if (Object.hasOwn(change, "provider_ref")) node.provider_ref = cloneModel(change.provider_ref) as typeof node.provider_ref;
    if (change.parameters) node.parameters = cloneModel(change.parameters) as typeof node.parameters;
    if (change.position) node.position = cloneModel(change.position) as typeof node.position;
  }
  let loadVersion = 0;
  let alive = true;
  const controller = new AbortController();
  const locked = computed(() => busy.value || !!pending.value);
  watch(() => JSON.stringify(drafts.value), () => { diagnostics.value = []; }, { flush: "sync" });
  onScopeDispose(() => { alive = false; controller.abort(); });
  function issue(failure: unknown) {
    error.value = failure instanceof WorkbenchApiError ? failure
      : new WorkbenchApiError("unavailable", "模型配置不可用");
  }
  function getDraft(workflowId: string): ModelDraft | null {
    if (!isWorkflowIdentity(workflowId) || workflowId === "frontend:empty-test") return null;
    let draft = drafts.value.find((row) => row.workflowId === workflowId);
    if (!draft) {
      draft = { workflowId, configId: crypto.randomUUID(), backendRevision: 0, nodes: [], edges: [] };
      drafts.value.push(draft);
    }
    return drafts.value.find((row) => row.workflowId === workflowId)!;
  }
  function isDraftSaved(workflowId: string) {
    const draft = drafts.value.find((row) => row.workflowId === workflowId);
    if (!draft?.backendRevision) return false;
    const saved = savedConfigurations.value[draft.configId];
    return !!saved && modelSignature(saved) === modelSignature({
      ...modelPublication(draft), revision: draft.backendRevision,
    });
  }
  function addNode(workflowId: string, position: { x: number; y: number }) {
    if (editGuard && !editGuard(workflowId, id => addNode(id, position))) return null;
    const draft = getDraft(workflowId);
    if (!draft || draft.nodes.length >= MAX_MODEL_NODES
      || !Number.isFinite(position.x) || !Number.isFinite(position.y)) return null;
    const id = crypto.randomUUID();
    draft.nodes.push({
      id, position: { ...position }, provider_ref: null,
      parameters: { model: "deepseek-flash", max_tokens: 2048 },
    });
    selectedNodeId.value = id;
    return id;
  }
  function removeNode(workflowId: string, id: string) {
    if (editGuard && !editGuard(workflowId, target => removeNode(target, id))) return;
    const draft = getDraft(workflowId);
    if (!draft) return;
    draft.nodes = draft.nodes.filter((node) => node.id !== id);
    draft.edges = draft.edges.filter((edge) => edge.source !== id);
    if (selectedNodeId.value === id) selectedNodeId.value = null;
  }
  function connect(workflowId: string, source: string, target: string, sourceHandle: string | null, targetHandle: string | null) {
    if (editGuard && !editGuard(workflowId, id => connect(id, source, target, sourceHandle, targetHandle))) return false;
    const draft = getDraft(workflowId);
    const stage = (["A", "B"] as const).find((stage) => dependencyTarget(stage) === target);
    if (!draft || !draft.nodes.some((node) => node.id === source)
      || !stage || sourceHandle !== "model-out" || targetHandle !== "model-in") {
      issue(new WorkbenchApiError("rejected", "模型连线只能连接 Agent 的模型输入端口"));
      return false;
    }
    if (draft.edges.some((edge) => edge.target_binding_id === AGENT_BINDINGS[stage])) {
      issue(new WorkbenchApiError("rejected", `Agent ${stage} 已连接模型，请先断开原连线`));
      return false;
    }
    draft.edges.push({ id: crypto.randomUUID(), source, target_binding_id: AGENT_BINDINGS[stage] });
    return true;
  }
  function disconnect(workflowId: string, id: string) {
    if (editGuard && !editGuard(workflowId, target => disconnect(target, id))) return;
    const draft = getDraft(workflowId);
    if (draft) draft.edges = draft.edges.filter((edge) => edge.id !== id);
  }
  async function loadProviders() {
    const generation = ++loadVersion;
    loading.value = true;
    try {
      const result = await workbenchRequest<unknown>("/api/model-configurations/provider", { signal: controller.signal });
      if (!alive || generation !== loadVersion) return;
      if (!Array.isArray(result) || result.length > 512 || !result.every(isChatProvider)
        || new Set(result.map((record) => record.provider_id)).size !== result.length)
        throw new WorkbenchApiError("unavailable", "供应商读取契约无效");
      providers.value = result;
      diagnostics.value = [];
      providersLoaded.value = true;
    } catch (failure) { if (alive && generation === loadVersion) issue(failure); }
    finally { if (alive && generation === loadVersion) loading.value = false; }
  }
  function editProvider(provider: ChatProvider | null) {
    providerForm.value = provider ? cloneModel(provider) : {
      schema_version: 1, kind: "chat_provider", provider_id: crypto.randomUUID(), revision: 0,
      name: "", protocol: "chat", base_url: "https://api.deepseek.com",
      credential_ref: "env:DEEPSEEK_API_KEY", enabled: true,
    };
  }
  function setPersistenceGuard(value: (() => boolean) | null) { guard = value; }
  async function dispatch(command: ConfigurationMutation) {
    if (!isConfigurationMutation(command)) throw new WorkbenchApiError("unavailable", "配置字段无效或超过 64KB，未提交");
    pending.value = cloneModel(command);
    if (!guard || !guard()) {
      pending.value = null;
      throw new WorkbenchApiError("unavailable", "原配置请求无法保存，未提交");
    }
    let result: unknown;
    try {
      result = await workbenchRequest<unknown>(command.path, { body: command.body, signal: controller.signal });
      if (!(command.path.endsWith("/provider") ? isChatProvider(result) : isModelConfiguration(result))
        || modelSignature(result as ChatProvider | ModelConfiguration) !== modelSignature(command.body.record))
        throw new WorkbenchApiError("unknown", "配置回执未对应原请求，保留请求等待核实");
    } catch (failure) {
      if (failure instanceof WorkbenchApiError && failure.kind === "rejected") {
        pending.value = null;
        guard();
      }
      throw failure;
    }
    if (command.path.endsWith("/provider")) {
      const provider = result as ChatProvider;
      providers.value = [...providers.value.filter((row) => row.provider_id !== provider.provider_id), provider];
      diagnostics.value = [];
      if (providerForm.value?.provider_id === provider.provider_id) providerForm.value = null;
    } else {
      const record = result as ModelConfiguration;
      const draft = drafts.value.find((row) => row.configId === record.config_id);
      if (draft) draft.backendRevision = record.revision;
      savedConfigurations.value[record.config_id] = cloneModel(record);
    }
    pending.value = null;
    if (!guard()) throw new WorkbenchApiError("unavailable", "后端已保存，但本机回执保存失败");
    return result;
  }
  async function saveProvider() {
    if (locked.value || !providerForm.value) return false;
    busy.value = true;
    error.value = null;
    try {
      const record = cloneModel(providerForm.value);
      const expected = record.revision;
      record.revision += 1;
      const fieldError = providerFieldDiagnostics(record)[0];
      if (fieldError) throw new WorkbenchApiError("unavailable", `${fieldError.message} [${fieldError.code}]`, undefined, fieldError.code);
      if (!isChatProvider(record)) throw new WorkbenchApiError("unavailable", "供应商名称、Chat 地址或凭据引用无效");
      await dispatch({
        path: "/api/model-configurations/provider",
        body: { record, expected_revision: expected, idempotency_key: crypto.randomUUID() },
      });
      return true;
    } catch (failure) { issue(failure); return false; }
    finally { busy.value = false; }
  }
  async function diagnose(record: ModelConfiguration) {
    try {
      const value = await workbenchRequest<{
        config_id: string; revision: number; execution_supported: boolean; diagnostics: { target: string; code: string }[];
      }>(`/api/model-configurations/model/${record.config_id}/revisions/${record.revision}/diagnostics`, { signal: controller.signal });
      if (value.config_id !== record.config_id || value.revision !== record.revision || typeof value.execution_supported !== "boolean"
        || !Array.isArray(value.diagnostics) || value.diagnostics.length > 34
        || !value.diagnostics.every((row) => row && Object.keys(row).length === 2 && uuid(row.target) && typeof row.code === "string"
          && [...record.nodes.map((node) => node.id), ...Object.values(AGENT_BINDINGS)].includes(row.target)
          && Object.hasOwn(MODEL_DIAGNOSTIC_MESSAGES, row.code))
        || new Set(value.diagnostics.map((row) => `${row.target}:${row.code}`)).size !== value.diagnostics.length)
        throw new WorkbenchApiError("unavailable", "模型诊断读取契约无效");
      const draft = drafts.value.find((row) => row.configId === record.config_id);
      if (alive && draft?.backendRevision === record.revision
        && modelSignature({ ...modelPublication(draft), revision: record.revision }) === modelSignature(record))
        diagnostics.value = value.diagnostics;
    } catch (failure) { if (alive) issue(failure); }
  }
  async function publishModels(workflowId: string): Promise<ModelConfiguration> {
    const draft = getDraft(workflowId);
    if (locked.value || !draft) throw new WorkbenchApiError("unavailable", "模型配置原请求尚待核实或目标不可发布");
    busy.value = true;
    error.value = null;
    try {
      const record = modelPublication(draft);
      const fieldError = record.nodes.flatMap((node) => modelParameterDiagnostics(node.parameters))[0];
      if (fieldError) throw new WorkbenchApiError("unavailable", `${fieldError.message} [${fieldError.code}]`, undefined, fieldError.code);
      await dispatch({
        path: "/api/model-configurations/model",
        body: { record, expected_revision: draft.backendRevision, idempotency_key: crypto.randomUUID() },
      });
      return record;
    } catch (failure) { issue(failure); throw failure; }
    finally { busy.value = false; }
  }
  async function saveModels(workflowId: string) {
    try {
      const record = await publishModels(workflowId);
      await diagnose(record);
      return true;
    } catch { return false; }
  }
  async function reconcile() {
    if (!pending.value || busy.value) return;
    busy.value = true;
    error.value = null;
    try {
      const result = await dispatch(cloneModel(pending.value));
      if (isModelConfiguration(result)) await diagnose(result);
    } catch (failure) { issue(failure); }
    finally { busy.value = false; }
  }
  function exportConfiguration(): ModelConfigurationSnapshot {
    return cloneModel({ schemaVersion: 1, drafts: drafts.value, pending: pending.value });
  }
  function restoreConfiguration(value: unknown) {
    if (!isModelConfigurationSnapshot(value)) return false;
    drafts.value = cloneModel(value.drafts);
    pending.value = cloneModel(value.pending);
    selectedNodeId.value = null;
    providers.value = [];
    providersLoaded.value = false;
    diagnostics.value = [];
    savedConfigurations.value = {};
    return true;
  }
  return {
    providers, providersLoaded, loading, drafts, selectedNodeId, pending, busy, locked, error,
    diagnostics, providerForm, providerDiagnostics, getDraft, isDraftSaved, addNode, removeNode, connect, disconnect,
    loadProviders, editProvider, saveProvider, saveModels, publishModels, reconcile, setPersistenceGuard,
    exportConfiguration, restoreConfiguration, setEditGuard, cloneWorkflow, updateNode,
    restoreWorkflowDraft, removeWorkflow,
  };
});
