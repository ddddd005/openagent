import { computed, onScopeDispose, ref, watch } from "vue";
import { defineStore } from "pinia";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { readGraphReceipt } from "../adapters/workflowApplicationApi";
import { createGraphCommand as command, dispatchGraphCommand, type GraphReceipt } from "../application/workflowCommands";
import { createWorkflowManagementRequests } from "../application/workflowManagementRequests";
import { createWorkflowManagement, workflowManagementQueries } from "../application/workflowManagement";
import { createWorkflowEditing } from "../application/workflowEditing";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { editFrontendSource, wireFrontendDisplay, type FrontendWiringRequest } from "../domain/frontendWiring";
import { newerGraphObservation } from "../domain/graphObservation";
import { graphConnectionIssue } from "../domain/graphConnections";
import { graphClone, graphObject, graphUuid, isGraphDocument, newGraph,
  isGraphObjectBindings, isGraphEventBindings, isGraphJsonValue, graphSignature, upgradeGraph,
  type GraphCommand, type GraphDocument, type GraphEdge, type GraphEntry, type GraphNode,
  type GraphNodeType, type GraphSession, type GraphCandidates, type GraphDataType,
  type GraphManagementResult } from "../domain/workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import type { FrontendExtension } from "../domain/frontendExtensions";
import { isGraphConfigurationDocument, type GraphConfigurationResult,
  type GraphConfigurationUpdateDocument, type GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import type { WorkflowNodeConfigurationRequest, WorkflowNodeConfigurationResult } from "../plugins/workflowFrontendSdk";
import { createCompactingSerialAgentDemo, createSerialAgentDemo, type SerialAgentModelConfig } from "../domain/serialAgentDemo";

type WorkflowConfigurationApplication = ReturnType<typeof createWorkflowConfigurationApplication>;
type ConfigurationHistoryAction = "edit" | "undo" | "redo" | "none";
type SettledConfigurationResult = Omit<GraphConfigurationResult, "outcome"> & {
  outcome: Exclude<GraphConfigurationResult["outcome"], "confirmation_required">;
};

function configurationUpdateDocument(document: GraphDocument, editSequence: number | null): GraphConfigurationUpdateDocument {
  const next = graphClone(document) as GraphConfigurationUpdateDocument;
  next.schema_version = 3;
  next.object_bindings ??= [];
  if (editSequence === null) delete next.edit_sequence;
  else next.edit_sequence = editSequence;
  for (const node of next.nodes)
    node.configuration_policy ??= { schema_version: 1, prompt_mode: "fixed" };
  return next;
}

function syncProvidedSessionItems(document: GraphDocument, nodeId: string,
  provided: NonNullable<GraphNodeType["provided_session_item_types"]>, preserveExact: boolean) {
  const bindings = document.object_bindings ?? [];
  const prior = bindings.filter(binding => binding.registrar_node_id === nodeId);
  document.object_bindings = bindings.filter(binding => binding.registrar_node_id !== nodeId);
  const retained = new Set<string>();
  for (const item of provided) {
    const existing = preserveExact ? prior.find(binding => binding.type_id === item.type_id
      && binding.schema_version === item.schema_version && !retained.has(binding.object_key)) : undefined;
    if (existing) {
      document.object_bindings.push(existing);
      retained.add(existing.object_key);
      continue;
    }
    let objectKey: string;
    do { objectKey = `item/${crypto.randomUUID()}`; }
    while (document.object_bindings.some(binding => binding.object_key === objectKey));
    document.object_bindings.push({ object_key: objectKey, type_id: item.type_id,
      schema_version: item.schema_version, scope: "shared", owner_node_id: null,
      readers: [], writers: [], registrar_node_id: nodeId });
  }
}

export const useWorkflowGraphStore = defineStore("workflow-graph", () => {
  const workspace = useWorkspaceStore();
  const notices = useWorkbenchNoticesStore();
  const entries = ref<Record<string, GraphEntry>>({});
  const catalog = ref<GraphNodeType[]>([]);
  const frontendExtensions = ref<FrontendExtension[]>([]);
  const packageLock = ref<NonNullable<GraphDocument["package_lock"]>>([]);
  const observedExecutionPackageLock = ref<NonNullable<GraphDocument["package_lock"]> | null>(null);
  const executionPackageLock = computed({
    get: () => observedExecutionPackageLock.value ?? packageLock.value,
    set: (value: NonNullable<GraphDocument["package_lock"]>) => { observedExecutionPackageLock.value = value; },
  });
  const dataTypes = ref<GraphDataType[]>([]);
  const catalogLoading = ref(false);
  const catalogError = ref<string | null>(null);
  const definitionsLoading = ref(false);
  const sessions = ref<Record<string, GraphSession[]>>({});
  const views = ref<Record<string, GraphSession>>({});
  const candidates = ref<Record<string, GraphCandidates>>({});
  const history = ref<Record<string, { past: GraphDocument[]; future: GraphDocument[] }>>({});
  const busy = ref<string | null>(null);
  const applicationBusy = ref(false);
  const managementTargets = new Map<string, { entry: GraphEntry; sessionId: string | null;
    selection: number; view: ReturnType<typeof workspace.currentViewToken> }>();
  const managementRequests = createWorkflowManagementRequests({
    current: id => alive && !!entries.value[id],
    accept: acceptManagementReceipt,
  });
  const configurationSubmitting = ref<Record<string, boolean>>({});
  const configurationApplicationVersion = ref(0);
  let configurationApplication: WorkflowConfigurationApplication | null = null;
  const receiptChecks = ref<Record<string, string>>({});
  const selectingSessionFor = ref<string | null>(null);
  const selectedNodeIds = ref<string[]>([]);
  const selectedEdgeId = ref<string | null>(null);
  const active = computed(() => entries.value[workspace.activeWorkflowId]);
  const document = computed(() => active.value?.document ?? null);
  const session = computed(() => active.value?.session_id ? views.value[active.value.session_id] ?? null : null);
  const activeManagement = computed(() => active.value
    ? managementRequests.forWorkflow(active.value.document.workflow_definition_id) : null);
  const pending = computed(() => active.value?.pending ?? activeManagement.value?.pendingCommand.value ?? null);
  const activeConfigurationLocked = computed(() => {
    configurationApplicationVersion.value;
    const id = workspace.activeWorkflowId;
    return !!id && (!!configurationSubmitting.value[id]
      || !!configurationApplication?.forWorkflow(id).locked.value);
  });
  const mutationLocked = computed(() => !!busy.value || applicationBusy.value || activeConfigurationLocked.value
    || !!activeManagement.value?.locked.value
    || !!pending.value || !!workspace.activeWorkflow.copyPending
    || workspace.workflows.some(row => row.copyPending && row.sourceId === workspace.activeWorkflowId));
  const controlLocked = computed(() => mutationLocked.value || selectingSessionFor.value === workspace.activeWorkflowId
    || !!workspace.activeWorkflow.archived || !!workspace.activeWorkflow.deleted
    || !!session.value?.archived);
  const locked = computed(() => controlLocked.value || !!session.value?.readonly);
  const primaryAction = computed(() => ["running", "prepared", "pausing"].includes(session.value?.status ?? "") ? "pause"
    : session.value?.available_actions?.includes("resume") || session.value?.status === "paused" ? "resume" : "start");
  const statusLabel = computed(() => busy.value ? "提交中" : primaryAction.value === "pause" ? "暂停"
    : primaryAction.value === "resume" ? "继续" : "启动");
  const editorStable = computed(() => !active.value?.session_id || !!session.value?.can_submit
    && !["prepared", "running", "pausing", "paused"].includes(session.value.status));
  const editorLocked = computed(() => locked.value || !editorStable.value);
  const canUndo = computed(() => !!history.value[workspace.activeWorkflowId]?.past.length
    && !locked.value && editorStable.value);
  const canRedo = computed(() => !!history.value[workspace.activeWorkflowId]?.future.length
    && !locked.value && editorStable.value);
  let alive = true;
  let viewGeneration = 0;
  let sessionSelectionGeneration = 0;
  let mutationGeneration = 0;
  const refreshGenerations = new Map<string, number>();
  const candidateGenerations = new Map<string, number>();
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  onScopeDispose(() => { alive = false; frontendExtensions.value = []; viewGeneration++; sessionSelectionGeneration++; if (pollTimer) clearTimeout(pollTimer); });
  const isGeneric = (id: string) => !!entries.value[id];
  function selectConfigurationBasis() {
    const id = workspace.activeWorkflowId, entry = entries.value[id];
    if (!configurationApplication || !graphUuid(id)) return;
    configurationApplication.select(id, entry?.session_id ?? null);
    void configurationApplication.forWorkflow(id).initialize()
      .catch(failure => report(failure, "读取工作流原请求"));
  }
  watch(() => [workspace.activeWorkflowId, entries.value[workspace.activeWorkflowId]?.session_id] as const,
    selectConfigurationBasis);
  function installWorkflowConfigurationApplication(application: WorkflowConfigurationApplication) {
    configurationApplication = application;
    configurationApplicationVersion.value++;
    for (const entry of Object.values(entries.value))
      if (isGraphConfigurationDocument(entry.document) && Number.isSafeInteger(entry.document.edit_sequence))
        application.observe(entry.document);
    selectConfigurationBasis();
  }
  function setPersistenceGuard(_guard: () => boolean | Promise<boolean>) {
    // Legacy test surface only; graph editing is intentionally memory-only.
  }
  function storeSnapshot(): { schema_version: 1; entries: Record<string, GraphEntry> } {
    return graphClone({ schema_version: 1, entries: entries.value });
  }
  function restoreSnapshot(_value: unknown) {
    return false;
  }
  function registerWorkflowDraft(value: GraphDocument, description = "") {
    const doc = graphClone(value), id = doc.workflow_definition_id;
    entries.value[id] = { document: doc, saved_revision: 0, session_id: null, pending: null };
    history.value[id] = { past: [], future: [] };
    workspace.registerGraphWorkflow({ id, title: doc.name, description, nodeCount: doc.nodes.length, state: "draft" });
    workspace.openWorkflow(id);
    selectedNodeIds.value = []; selectedEdgeId.value = null;
    if (configurationApplication)
      void submitConfigurationDocument(id, doc, "none")
        .catch(failure => report(failure, "创建工作流配置"));
    return id;
  }
  function createWorkflow() {
    return registerWorkflowDraft(newGraph("新工作流", executionPackageLock.value));
  }
  function createWorkflowDraft(value: GraphDocument, description = "") {
    if (locked.value || !isGraphDocument(value) || entries.value[value.workflow_definition_id]
      || workspace.workflows.some(row => row.id === value.workflow_definition_id)) return null;
    return registerWorkflowDraft(value, description);
  }
  function createSerialAgentExample(modelConfig?: SerialAgentModelConfig, compacting = false) {
    if (catalogLoading.value || catalogError.value) return null;
    try {
      const builder = compacting ? createCompactingSerialAgentDemo : createSerialAgentDemo;
      const doc = builder(catalog.value, executionPackageLock.value, modelConfig);
      return registerWorkflowDraft(doc, compacting ? "A → B 串行，独立上下文与精简策略" : "A → B 串行");
    } catch (failure) {
      report(failure, "创建串行 Agent 示例");
      return null;
    }
  }
  function createCompactingSerialAgentExample(modelConfig?: SerialAgentModelConfig) {
    return createSerialAgentExample(modelConfig, true);
  }
  async function saveWorkflow(_id = workspace.activeWorkflowId) {
    notices.notify("unavailable", "旧整图发布入口已退役；正式保存暂不可用，未提交新请求", "保存工作流");
    return false;
  }
  function typeFor(node: GraphNode) {
    return catalog.value.find(type => type.component_id === node.component_id
      && type.component_version === node.component_version);
  }
  async function loadCatalog() {
    if (catalogLoading.value) return;
    catalogLoading.value = true;
    catalogError.value = null;
    frontendExtensions.value = [];
    try {
      const result = await workflowManagementQueries.catalog();
      if (alive) { catalog.value = result.node_types; packageLock.value = result.package_lock; dataTypes.value = result.data_types;
        executionPackageLock.value = result.execution_package_lock ?? result.package_lock;
        frontendExtensions.value = result.frontend_extensions ?? []; }
    }
    catch (failure) { if (alive) catalogError.value = failure instanceof Error ? failure.message : "节点目录不可用"; }
    finally { catalogLoading.value = false; }
  }
  async function loadDefinitions() {
    if (definitionsLoading.value) return false;
    definitionsLoading.value = true;
    const generation = mutationGeneration;
    try {
      const rows = await management.queries.definitions(true);
      if (!alive || generation !== mutationGeneration) return false;
      for (const row of rows) {
        const doc = graphClone(row.document), id = doc.workflow_definition_id;
        const entry = entries.value[id], workflow = workspace.workflows.find(item => item.id === id);
        if (!entry) {
          entries.value[id] = { document: doc, saved_document: graphClone(doc), saved_revision: doc.revision,
            session_id: null, pending: null };
          history.value[id] = { past: [], future: [] };
          workspace.registerGraphWorkflow({ id, title: doc.name, description: "", nodeCount: doc.nodes.length,
            state: "saved", archived: row.archived });
        } else if (!entry.pending && workflow?.state !== "draft" && doc.revision >= entry.saved_revision) {
          entry.document = doc; entry.saved_document = graphClone(doc); entry.saved_revision = doc.revision;
          workspace.updateWorkflowProjection(id, doc.name, doc.nodes.length);
        }
        entries.value[id].latest_published_revision = doc.revision;
        if (configurationApplication && isGraphConfigurationDocument(doc))
          configurationApplication.observe(doc);
        workspace.setWorkflowManagement(id, row.archived);
      }
      selectConfigurationBasis();
      return true;
    } catch (failure) { report(failure, "读取工作流目录"); return false; }
    finally { definitionsLoading.value = false; }
  }
  function report(failure: unknown, action: string) {
    const error = failure instanceof WorkbenchApiError ? failure : new WorkbenchApiError("unavailable", String(failure));
    notices.notify(error.kind, error.reason, action);
  }
  function acceptConfigurationResult(sourceId: string, result: GraphConfigurationResult,
    viewToken: ReturnType<typeof workspace.currentViewToken>, historyAction: ConfigurationHistoryAction) {
    if (result.outcome === "confirmation_required") return null;
    const targetId = result.workflow_definition_id, document = graphClone(result.document);
    const source = entries.value[sourceId];
    if (!source) throw new WorkbenchApiError("unknown", "配置结果的原工作流已不存在");
    if (targetId === sourceId) {
      const record = history.value[sourceId] ?? { past: [], future: [] };
      if (historyAction === "edit" && source.saved_revision > 0
        && graphSignature(source.document) !== graphSignature(document)) {
        record.past = [...record.past, graphClone(source.document)].slice(-60);
        record.future = [];
      } else if (historyAction === "undo") {
        const previous = record.past.at(-1);
        if (previous) {
          record.past = record.past.slice(0, -1);
          record.future = [...record.future, graphClone(source.document)].slice(-60);
        }
      } else if (historyAction === "redo") {
        const next = record.future.at(-1);
        if (next) {
          record.future = record.future.slice(0, -1);
          record.past = [...record.past, graphClone(source.document)].slice(-60);
        }
      }
      history.value[sourceId] = record;
      source.document = document;
      source.saved_document = graphClone(document);
      source.saved_revision = document.revision;
      source.latest_published_revision = Math.max(source.latest_published_revision ?? 0, document.revision);
      workspace.updateWorkflowProjection(sourceId, document.name, document.nodes.length);
      workspace.markWorkflowSaved(sourceId, document.name);
      return { workflowId: targetId, document };
    }
    if (result.outcome !== "derived")
      throw new WorkbenchApiError("unknown", "配置回执的工作流归属无效");
    const existing = entries.value[targetId];
    entries.value[targetId] = { ...(existing ?? {}), document, saved_document: graphClone(document),
      saved_revision: document.revision, latest_published_revision: document.revision,
      session_id: null, pending: null };
    history.value[targetId] = history.value[targetId] ?? { past: [], future: [] };
    if (!workspace.workflows.some(row => row.id === targetId))
      workspace.registerGraphWorkflow({ id: targetId, title: document.name, description: "",
        nodeCount: document.nodes.length, state: "saved" });
    workspace.updateWorkflowProjection(targetId, document.name, document.nodes.length);
    workspace.markWorkflowSaved(targetId, document.name);
    if (workspace.activeWorkflowId === sourceId && workspace.isCurrentView(viewToken))
      workspace.openWorkflow(targetId);
    return { workflowId: targetId, document };
  }
  async function settleConfigurationQuestion(workflowId: string,
    result: GraphConfigurationResult): Promise<SettledConfigurationResult | null> {
    if (result.outcome !== "confirmation_required") return result as SettledConfigurationResult;
    const request = configurationApplication?.forWorkflow(workflowId);
    if (!request) throw new WorkbenchApiError("unknown", "派生确认请求已不可用");
    if (!window.confirm("此固定配置修改需要创建新的工作流；原工作流及会话保持不变。继续吗？")) {
      request.reject();
      return null;
    }
    return await request.confirm() as SettledConfigurationResult;
  }
  async function submitConfigurationDocument(workflowId: string, proposed: GraphDocument,
    historyAction: ConfigurationHistoryAction): Promise<GraphConfigurationResult | null> {
    const application = configurationApplication, source = entries.value[workflowId];
    if (!application || !source)
      throw new WorkbenchApiError("unavailable", "正式配置应用不可用，未提交工作流修改");
    if (configurationSubmitting.value[workflowId])
      throw new WorkbenchApiError("rejected", "工作流配置正在提交", undefined, "graph_configuration_pending");
    const entry = application.forWorkflow(workflowId);
    if (source.pending || source.document.workflow_definition_id !== workflowId
      || proposed.workflow_definition_id !== workflowId)
      throw new WorkbenchApiError("rejected", "工作流配置编辑依据无效");
    configurationSubmitting.value[workflowId] = true;
    const viewToken = workspace.currentViewToken();
    let request: GraphConfigurationUpdateRequest | null = null;
    try {
      await entry.initialize();
      const current = application.document(workflowId);
      if (source.saved_revision > 0 && !current)
        throw new WorkbenchApiError("unavailable", "正式配置读取暂不可用；未提交工作流修改");
      if (current && current.revision !== source.document.revision)
        throw new WorkbenchApiError("rejected", "正式配置修订已变化；请重新读取工作流", undefined, "stale_revision");
      const editSequence = current?.edit_sequence ?? 0;
      const document = configurationUpdateDocument(proposed, current ? editSequence : null);
      document.revision = current?.revision ?? 1;
      request = { workflow_definition_id: workflowId, expected_edit_sequence: editSequence,
        document, idempotency_key: crypto.randomUUID() };
      let result: GraphConfigurationResult;
      try {
        result = await entry.submit(request);
      } catch (failure) {
        const known = entry.lastResult.value;
        if (known && known.outcome !== "confirmation_required")
          acceptConfigurationResult(workflowId, graphClone(known) as GraphConfigurationResult, viewToken, historyAction);
        throw failure;
      }
      const settled = await settleConfigurationQuestion(workflowId, result);
      if (!settled) return null;
      acceptConfigurationResult(workflowId, settled, viewToken, historyAction);
      return settled;
    } finally {
      delete configurationSubmitting.value[workflowId];
    }
  }
  async function patchNodeConfiguration(request: WorkflowNodeConfigurationRequest):
    Promise<WorkflowNodeConfigurationResult | null> {
    const application = configurationApplication;
    if (!application)
      throw new WorkbenchApiError("unavailable", "正式配置应用不可用，节点配置尚未提交");
    const source = entries.value[request.workflowId], document = application.document(request.workflowId);
    if (!alive || locked.value || request.workflowId !== workspace.activeWorkflowId || !source
      || !isGraphConfigurationDocument(document) || !graphObject(request.patch) || !isGraphJsonValue(request.patch))
      return null;
    const node = document.nodes.find(row => row.node_binding_id === request.nodeId);
    if (!node || !node.configuration_policy)
      return null;
    const definition = typeFor(node), properties = definition?.config_schema.properties;
    if (!definition?.executable || !graphObject(properties) || !Object.keys(request.patch).length
      || Object.keys(request.patch).some(field => !Object.hasOwn(properties, field))) return null;
    if (graphSignature({ ...document, nodes: [{ ...node, config: node.config }] })
      !== graphSignature({ ...document, nodes: [{ ...node, config: request.expectedConfig }] })) return null;
    if (configurationSubmitting.value[request.workflowId]) return null;
    configurationSubmitting.value[request.workflowId] = true;
    const viewToken = workspace.currentViewToken(), entry = application.forWorkflow(request.workflowId);
    try {
      await entry.initialize();
      const submission = {
        workflowId: request.workflowId, nodeId: request.nodeId, componentId: request.componentId,
        componentVersion: request.componentVersion, extensionId: request.extensionId,
        expectedEditSequence: document.edit_sequence,
        expectedConfig: graphClone(request.expectedConfig),
        nextConfig: { ...graphClone(node.config), ...graphClone(request.patch) },
        nextPolicy: graphClone(node.configuration_policy),
      };
      const result = await application.sdk.submitNodeConfiguration(submission);
      const settled = await settleConfigurationQuestion(request.workflowId, result);
      if (!settled)
        return { workflowId: request.workflowId, nodeId: request.nodeId, outcome: "cancelled" };
      const accepted = acceptConfigurationResult(request.workflowId, settled, viewToken, "edit");
      if (!accepted) return null;
      return { workflowId: accepted.workflowId, nodeId: request.nodeId, outcome: settled.outcome };
    } catch (failure) {
      const known = entry.lastResult.value;
      if (known && known.outcome !== "confirmation_required")
        acceptConfigurationResult(request.workflowId, graphClone(known) as GraphConfigurationResult, viewToken, "edit");
      report(failure, "保存节点配置");
      return null;
    } finally {
      delete configurationSubmitting.value[request.workflowId];
    }
  }
  function patchNodeConfigurationPolicy(nodeId: string, mode: "fixed" | "adjustable") {
    const id = workspace.activeWorkflowId;
    return editable(id, document => {
      const node = document.nodes.find(row => row.node_binding_id === nodeId);
      if (!node || node.component_id !== "prompts.item" || node.component_version !== "3"
        || document.schema_version !== 3 || !node.configuration_policy) return false;
      node.configuration_policy = { ...node.configuration_policy, prompt_mode: mode };
      return true;
    });
  }
  function configurationPendingRequests() {
    return Object.keys(entries.value).flatMap(workflowId => {
      if (!configurationApplication) return [];
      const request = configurationApplication.forWorkflow(workflowId);
      return request.pending.value ? [{ workflowId, error: request.error.value, busy: request.busy.value }] : [];
    });
  }
  async function reconcileConfiguration(workflowId: string) {
    if (!configurationApplication) return false;
    const request = configurationApplication.forWorkflow(workflowId);
    if (request.busy.value || !request.pending.value) return false;
    configurationSubmitting.value[workflowId] = true;
    const viewToken = workspace.currentViewToken();
    try {
      const result = await request.reconcile();
      const settled = await settleConfigurationQuestion(workflowId, result);
      if (!settled) return true;
      acceptConfigurationResult(workflowId, settled, viewToken, "edit");
      return true;
    } catch (failure) {
      report(failure, "核实工作流配置原请求");
      return false;
    } finally { delete configurationSubmitting.value[workflowId]; }
  }
  function observe(view: GraphSession) {
    const accepted = newerGraphObservation(views.value[view.workflow_session_id], view);
    if (alive) {
      views.value[view.workflow_session_id] = accepted;
      for (const [workflowId, rows] of Object.entries(sessions.value)) {
        if (rows.some(row => row.workflow_session_id === accepted.workflow_session_id && row !== accepted))
          sessions.value[workflowId] = rows.map(row => row.workflow_session_id === accepted.workflow_session_id ? accepted : row);
      }
    }
    return accepted;
  }
  async function acceptManagementReceipt(id: string, receipt: GraphReceipt, request: GraphCommand) {
    const entry = entries.value[id];
    if (!alive || !entry) return;
    if (receipt.kind === "session") {
      const accepted = observe(receipt.session);
      const rows = sessions.value[id] ?? [];
      if (!rows.some(row => row.workflow_session_id === accepted.workflow_session_id))
        sessions.value[id] = [...rows, accepted];
      const target = managementTargets.get(String(request.body.idempotency_key));
      if (request.action === "create" && target?.entry === entry
        && target.sessionId === entry.session_id && target.selection === sessionSelectionGeneration
        && workspace.isCurrentView(target.view)) {
        entry.session_id = accepted.workflow_session_id;
        delete entry.session_document;
      }
      return;
    }
    if (receipt.kind !== "management")
      throw new WorkbenchApiError("unknown", "管理回执不是当前入口的结果");
    if (receipt.result.deleted) {
      await finishManagedDeletion(id, request);
    } else if (request.action === "definition-archive") {
      workspace.setWorkflowManagement(id, true);
    } else {
      const sessionId = request.source_session_id, view = sessionId ? views.value[sessionId] : null;
      if (view) observe({ ...view, archived: true, readonly: true, can_submit: false, available_actions: [] });
    }
  }
  function managementPendingRequests() {
    return Object.keys(entries.value).flatMap(workflowId => {
      const request = managementRequests.forWorkflow(workflowId);
      return request.pending.value || request.error.value ? [{ workflowId, error: request.error.value,
        busy: request.busy.value, pending: !!request.pending.value }] : [];
    });
  }
  async function dispatch(id: string, command: GraphCommand, reconcile = false): Promise<unknown> {
    const generation = ++mutationGeneration;
    const entry = entries.value[id];
    const eventSessionId = entry.session_id, eventDefinitionId = entry.document.workflow_definition_id,
      eventDefinitionRevision = entry.document.revision;
    if (!reconcile) {
      const key = String(command.body.idempotency_key);
      managementTargets.set(key, { entry, sessionId: entry.session_id,
        selection: sessionSelectionGeneration, view: workspace.currentViewToken() });
      try { return await managementRequests.forWorkflow(id).dispatch(command); }
      finally { managementTargets.delete(key); }
    }
    return dispatchGraphCommand(entry, command, {
      readReceipt: readGraphReceipt,
      readDefinition: workflowManagementQueries.definition,
      observedSession: sessionId => views.value[sessionId],
      current: () => alive && entries.value[id] === entry && mutationGeneration === generation
        && (command.action !== "event"
        || entry.session_id === eventSessionId && entry.document.workflow_definition_id === eventDefinitionId
          && entry.document.revision === eventDefinitionRevision),
    }, reconcile);
  }
  const management = createWorkflowManagement({
    entry: id => entries.value[id], observedSession: id => views.value[id], observe, dispatch,
    commandState: value => { applicationBusy.value = value; if (value) mutationGeneration++; },
    canSubmit(id) {
      const workflow = workspace.workflows.find(row => row.id === id);
      return alive && !!workflow && !workflow.copyPending
        && !workspace.workflows.some(row => row.copyPending && row.sourceId === id);
    },
  });
  const editing = createWorkflowEditing({
    entry: id => entries.value[id],
    isDraft: id => workspace.workflows.find(row => row.id === id)?.state === "draft",
    canEdit: id => alive && !busy.value && !applicationBusy.value && !entries.value[id]?.pending
      && !managementRequests.forWorkflow(id).locked.value
      && !configurationSubmitting.value[id] && !configurationApplication?.forWorkflow(id).locked.value
      && !workspace.workflows.find(row => row.id === id)?.archived
      && !workspace.workflows.find(row => row.id === id)?.deleted
      && selectingSessionFor.value !== id && !workspace.workflows.find(row => row.id === id)?.copyPending
      && !workspace.workflows.some(row => row.copyPending && row.sourceId === id),
    observedSession: id => views.value[id],
    acceptDraft(id, next) {
      if (configurationApplication) {
        void submitConfigurationDocument(id, next, "edit")
          .catch(failure => report(failure, "保存工作流配置"));
        return;
      }
      const entry = entries.value[id];
      const past = [...(history.value[id]?.past ?? []), graphClone(entry.document)].slice(-60);
      history.value[id] = { past, future: [] };
      entry.document = next;
      workspace.updateWorkflowProjection(id, next.name, next.nodes.length);
      workspace.markWorkflowDraft(id);
    },
    stageCopy(sourceId, targetId, next, baseline, copying) {
      entries.value[targetId] = { document: next, saved_revision: 0, session_id: null, pending: null };
      history.value[targetId] = { past: [baseline], future: [] };
      workspace.registerGraphWorkflow({ id: targetId, title: next.name, description: "", state: "draft",
        sourceId, nodeCount: next.nodes.length, copyPending: copying });
      if (configurationApplication)
        void submitConfigurationDocument(targetId, next, "none")
          .catch(failure => report(failure, "创建工作流配置副本"));
    },
    open: workspace.openWorkflow, dispatch,
    copying: value => { busy.value = value ? "copy" : null; },
    report: failure => report(failure, "工作流会话复制"),
  });
  async function finishManagedDeletion(id: string, request: Pick<GraphCommand, "action" | "source_session_id">) {
    const entry = entries.value[id];
    if (!entry || entry.pending) return;
    if (request.action === "definition-delete") {
      const ownedSessions = Object.values(views.value)
        .filter(view => view.workflow_definition_id === entry.document.workflow_definition_id);
      for (const view of ownedSessions) {
        delete views.value[view.workflow_session_id]; delete candidates.value[view.workflow_session_id];
      }
      if (workspace.activeWorkflowId === id) {
        const fallback = workspace.workflows.find(row => row.id !== id && !row.copyPending && !row.deleted);
        if (fallback) workspace.openWorkflow(fallback.id);
        else { workspace.activeWorkflowId = ""; workspace.selectedWorkflowId = ""; workspace.viewVersion++; }
      }
      workspace.removeWorkflow(id);
      delete entries.value[id]; delete history.value[id]; delete sessions.value[id];
    } else if (request.action === "session-delete") {
      const sessionId = request.source_session_id!;
      sessions.value[id] = (sessions.value[id] ?? []).filter(view => view.workflow_session_id !== sessionId);
      delete views.value[sessionId]; delete candidates.value[sessionId];
      if (entry.session_id === sessionId) {
        entry.session_id = null; delete entry.session_document;
      }
    } else return;
  }
  async function manageWorkflow(id: string, action: "archive" | "delete") {
    if (busy.value || applicationBusy.value || !entries.value[id] || entries.value[id].pending) return false;
    busy.value = `definition-${action}`;
    try {
      await management.commands.manageDefinition({ workflowId: id }, action);
      if (action === "delete") await finishManagedDeletion(id, { action: "definition-delete" });
      else await refresh(id);
      return true;
    } catch (failure) { report(failure, action === "archive" ? "归档工作流" : "删除工作流"); return false; }
    finally { busy.value = null; }
  }
  async function manageSession(sessionId: string, action: "archive" | "delete", id = workspace.activeWorkflowId) {
    if (busy.value || applicationBusy.value || !entries.value[id] || entries.value[id].pending) return false;
    busy.value = `session-${action}`;
    try {
      await management.commands.manageSession({ workflowId: id }, sessionId, action);
      if (action === "delete") await finishManagedDeletion(id, {
        action: "session-delete", source_session_id: sessionId,
      });
      else await refresh(id);
      return true;
    } catch (failure) { report(failure, action === "archive" ? "归档会话" : "删除会话"); return false; }
    finally { busy.value = null; }
  }
  function copyWorkflow(id = workspace.activeWorkflowId) {
    try { return editing.copy({ workflowId: id }); }
    catch (failure) { report(failure, "复制工作流"); return null; }
  }
  function renameWorkflow(name: string) {
    const title = name.trim();
    if (!title || title.length > 128 || /[\u0000-\u001f\u007f]/.test(title)) return null;
    return editable(workspace.activeWorkflowId, doc => { doc.name = title; return true; });
  }
  async function refreshPublishedRevision(id = workspace.activeWorkflowId) {
    const entry = entries.value[id]; if (!entry?.saved_revision || locked.value) return false;
    const definitionId = entry.document.workflow_definition_id;
    try {
      const latest = await management.queries.definition(definitionId);
      if (!alive || entries.value[id] !== entry || entry.document.workflow_definition_id !== definitionId) return false;
      entry.latest_published_revision = Math.max(entry.latest_published_revision ?? entry.saved_revision, latest.revision);
      return true;
    } catch (failure) { report(failure, "读取最新已发布修订"); return false; }
  }
  function editable(id: string, change: (document: GraphDocument) => boolean) {
    try { return editing.edit({ workflowId: id }, change); }
    catch (failure) { report(failure, "工作流编辑"); return null; }
  }
  function prepareGraphEdit(document: GraphDocument) {
    if (document.schema_version !== 3) return upgradeGraph(document, executionPackageLock.value);
    document.object_bindings ??= [];
    document.package_lock ??= graphClone(executionPackageLock.value);
    const knownPackages = new Set(document.package_lock.map(item => item.package_id));
    document.package_lock.push(...graphClone(
      executionPackageLock.value.filter(item => !knownPackages.has(item.package_id))));
    document.execution_roots ??= [];
    document.control_edges ??= [];
    return document;
  }
  function addNode(component: string, position: { x: number; y: number }) {
    const type = catalog.value.find(row => `${row.component_id}@${row.component_version}` === component);
    if (!type) return null;
    const node: GraphNode = { node_binding_id: crypto.randomUUID(), component_id: type.component_id,
      component_version: type.component_version, title: type.display_name, position: { ...position }, config: graphClone(type.default_config) };
    const providedTypes = type.provided_session_item_types ?? [];
    for (const access of type.object_accesses ?? []) {
      if (access.type_id === "workflow.variable") continue;
      node.config[access.config_field] = access.multiple ? [] : "";
    }
    const id = editable(workspace.activeWorkflowId, doc => {
      const ports = [...type.inputs, ...type.outputs, ...Object.values(type.port_modes ?? {}).flatMap(mode => [...mode.inputs, ...mode.outputs])];
      if (ports.some(port => (port.data_schema_version ?? 1) !== 1) || type.input_storage === "references"
        || type.capabilities?.some(capability => capability.startsWith("objects:")) || providedTypes.length)
        prepareGraphEdit(doc);
      doc.nodes.push(node);
      syncProvidedSessionItems(doc, node.node_binding_id, providedTypes, false);
      return true;
    });
    if (id) selectedNodeIds.value = [node.node_binding_id];
    return id ? node.node_binding_id : null;
  }
  function attachFrontendDisplay(request: FrontendWiringRequest) {
    let added: string[] = [];
    const id = editable(workspace.activeWorkflowId, doc => {
      added = wireFrontendDisplay(doc, catalog.value, executionPackageLock.value, request);
      return true;
    });
    if (id) { selectedNodeIds.value = added; selectedEdgeId.value = null; }
    return id;
  }
  function patchFrontendSource(nodeId: string, edgeId: string | null, source: { nodeId: string; portId: string } | null) {
    return editable(workspace.activeWorkflowId, doc => editFrontendSource(doc, catalog.value, nodeId, edgeId, source));
  }
  function patchNode(id: string, patch: Partial<Pick<GraphNode, "title" | "config" | "public_outputs">>) {
    return editable(workspace.activeWorkflowId, doc => {
      const node = doc.nodes.find(row => row.node_binding_id === id);
      if (!node) return false;
      Object.assign(node, graphClone(patch));
      return true;
    });
  }
  function moveNodes(moves: { id: string; position: { x: number; y: number } }[]) {
    return editable(workspace.activeWorkflowId, doc => {
      for (const move of moves) {
        const node = doc.nodes.find(row => row.node_binding_id === move.id);
        if (node) node.position = { ...move.position };
      }
      return true;
    });
  }
  function connect(edge: Omit<GraphEdge, "edge_id" | "order">) {
    return editable(workspace.activeWorkflowId, doc => {
      const issue = graphConnectionIssue(doc, catalog.value, edge);
      if (issue) { notices.notify("rejected", issue, "添加连线"); return false; }
      const incoming = doc.edges.filter(e => e.target_node_id === edge.target_node_id && e.target_port_id === edge.target_port_id);
      doc.edges.push({ ...edge, edge_id: crypto.randomUUID(), order: incoming.length ? Math.max(...incoming.map(e => e.order)) + 1 : 0 });
      return true;
    });
  }
  function removeSelection() {
    const ids = new Set(selectedNodeIds.value);
    const changed = editable(workspace.activeWorkflowId, doc => {
      doc.nodes = doc.nodes.filter(n => !ids.has(n.node_binding_id));
      doc.edges = doc.edges.filter(e => !ids.has(e.source_node_id) && !ids.has(e.target_node_id) && e.edge_id !== selectedEdgeId.value);
      if (doc.schema_version >= 2) {
        doc.execution_roots = (doc.execution_roots ?? []).filter(id => !ids.has(id));
        doc.control_edges = (doc.control_edges ?? []).filter(edge => !ids.has(edge.source_node_id)
          && !ids.has(edge.target_node_id) && edge.edge_id !== selectedEdgeId.value);
        if (doc.event_bindings) doc.event_bindings = doc.event_bindings.map(binding => ({ ...binding,
          target_node_ids: binding.target_node_ids.filter(id => !ids.has(id)) })).filter(binding => binding.target_node_ids.length);
        doc.object_bindings = (doc.object_bindings ?? []).filter(binding =>
          (binding.scope !== "private" || !ids.has(binding.owner_node_id!))
          && !(binding.registrar_node_id && ids.has(binding.registrar_node_id))).map(binding => ({
          ...binding, readers: binding.readers.filter(id => !ids.has(id)),
          writers: binding.writers.filter(id => !ids.has(id)),
        }));
      }
      return true;
    });
    if (changed) { selectedNodeIds.value = []; selectedEdgeId.value = null; }
  }
  function reorderEdge(id: string, offset: number) {
    return editable(workspace.activeWorkflowId, doc => {
      const edge = doc.edges.find(e => e.edge_id === id);
      if (!edge) return false;
      const siblings = doc.edges.filter(e => e.target_node_id === edge.target_node_id
        && e.target_port_id === edge.target_port_id).sort((a, b) => a.order - b.order);
      const at = siblings.indexOf(edge); const destination = at + offset;
      if (destination < 0 || destination >= siblings.length) return false;
      [siblings[at], siblings[destination]] = [siblings[destination]!, siblings[at]!];
      siblings.forEach((sibling, order) => { sibling.order = order; });
      return true;
    });
  }
  function undo(redo = false) {
    if (redo ? !canRedo.value : !canUndo.value) return;
    const id = workspace.activeWorkflowId;
    const h = history.value[id];
    const snapshot = (redo ? h?.future : h?.past)?.pop();
    if (!snapshot) return;
    if (configurationApplication) {
      if (redo) h?.future.push(snapshot);
      else h?.past.push(snapshot);
      void submitConfigurationDocument(id, snapshot, redo ? "redo" : "undo")
        .catch(failure => report(failure, redo ? "重做工作流修改" : "撤销工作流修改"));
      return;
    }
    (redo ? h.past : h.future).push(graphClone(entries.value[id].document));
    entries.value[id].document = snapshot;
    workspace.updateWorkflowProjection(id, snapshot.name, snapshot.nodes.length);
    if (entries.value[id].saved_document
      && graphSignature(snapshot) === graphSignature(entries.value[id].saved_document!)) workspace.markWorkflowSaved(id);
    else workspace.markWorkflowDraft(id);
  }
  function duplicateSelection() {
    const selected = new Set(selectedNodeIds.value); const clonedIds: string[] = [];
    const changed = editable(workspace.activeWorkflowId, doc => {
      const mapping = new Map<string, string>();
      const sourceForCopy = new Map<string, GraphNode>();
      const nodes = doc.nodes.filter(n => selected.has(n.node_binding_id)).map(n => {
        const copy = graphClone(n); copy.node_binding_id = crypto.randomUUID();
        mapping.set(n.node_binding_id, copy.node_binding_id); clonedIds.push(copy.node_binding_id);
        sourceForCopy.set(copy.node_binding_id, n);
        copy.position.x += 35; copy.position.y += 35; return copy;
      });
      const copiedProviderBindings: NonNullable<GraphDocument["object_bindings"]> = [];
      const copiedItemKeys = new Map<string, { object_key: string; type_id: string; schema_version: number }>();
      for (const [copyId, source] of sourceForCopy) {
        for (const provided of typeFor(source)?.provided_session_item_types ?? []) {
          const sourceBinding = (doc.object_bindings ?? []).find(binding =>
            binding.registrar_node_id === source.node_binding_id && binding.type_id === provided.type_id
            && binding.schema_version === provided.schema_version);
          let objectKey: string;
          do { objectKey = `item/${crypto.randomUUID()}`; }
          while ((doc.object_bindings ?? []).some(binding => binding.object_key === objectKey)
            || copiedProviderBindings.some(binding => binding.object_key === objectKey));
          const owner = sourceBinding?.owner_node_id;
          const copyOwner = owner ? mapping.get(owner) ?? owner : null;
          const readers = sourceBinding?.readers.filter(id => mapping.has(id)).map(id => mapping.get(id)!) ?? [];
          const writers = sourceBinding?.writers.filter(id => mapping.has(id)).map(id => mapping.get(id)!) ?? [];
          copiedProviderBindings.push({
            ...(sourceBinding ? graphClone(sourceBinding) : {
              type_id: provided.type_id, schema_version: provided.schema_version, scope: "shared" as const,
              owner_node_id: null, readers: [], writers: [],
            }),
            object_key: objectKey, registrar_node_id: copyId,
            owner_node_id: copyOwner, readers, writers,
          });
          if (sourceBinding) copiedItemKeys.set(sourceBinding.object_key, {
            object_key: objectKey, type_id: provided.type_id, schema_version: provided.schema_version,
          });
        }
      }
      for (const node of nodes) {
        const source = sourceForCopy.get(node.node_binding_id)!;
        for (const access of typeFor(source)?.object_accesses ?? []) {
          const rewrite = (key: unknown) => {
            if (typeof key !== "string") return key;
            const item = copiedItemKeys.get(key);
            return item?.type_id === access.type_id && item.schema_version === access.schema_version
              ? item.object_key : key;
          };
          const value = source.config[access.config_field];
          node.config[access.config_field] = access.multiple && Array.isArray(value)
            ? value.map(rewrite) : rewrite(value);
        }
        for (const reference of typeFor(node)?.config_node_references ?? []) {
          const value = node.config[reference.config_field];
          if (reference.multiple && Array.isArray(value))
            node.config[reference.config_field] = value.map(id =>
              typeof id === "string" ? mapping.get(id) ?? id : id);
          else if (!reference.multiple && typeof value === "string")
            node.config[reference.config_field] = mapping.get(value) ?? value;
        }
      }
      const edges = doc.edges.filter(e => mapping.has(e.source_node_id) && mapping.has(e.target_node_id))
        .map(e => ({ ...e, edge_id: crypto.randomUUID(), source_node_id: mapping.get(e.source_node_id)!, target_node_id: mapping.get(e.target_node_id)! }));
      if (doc.schema_version >= 2) {
        doc.execution_roots = [...(doc.execution_roots ?? []), ...(doc.execution_roots ?? [])
          .filter(id => mapping.has(id)).map(id => mapping.get(id)!)];
        doc.control_edges = [...(doc.control_edges ?? []), ...(doc.control_edges ?? [])
          .filter(edge => mapping.has(edge.source_node_id) && mapping.has(edge.target_node_id))
          .map(edge => ({ ...edge, edge_id: crypto.randomUUID(), source_node_id: mapping.get(edge.source_node_id)!,
            target_node_id: mapping.get(edge.target_node_id)! }))];
        const privateBindings: NonNullable<GraphDocument["object_bindings"]> = [];
        doc.object_bindings = (doc.object_bindings ?? []).map(binding => {
          if (binding.registrar_node_id && mapping.has(binding.registrar_node_id)) return binding;
          if (binding.scope === "private") {
            const owner = mapping.get(binding.owner_node_id!);
            if (!owner) return binding;
            const key = `private/${crypto.randomUUID()}`;
            privateBindings.push({ ...graphClone(binding), object_key: key, owner_node_id: owner,
              readers: binding.readers.length ? [owner] : [], writers: binding.writers.length ? [owner] : [] });
            const copy = nodes.find(node => node.node_binding_id === owner)!;
            if (copy.config.object_key === binding.object_key) copy.config.object_key = key;
            if (Array.isArray(copy.config.object_keys)) copy.config.object_keys = copy.config.object_keys
              .map(existing => existing === binding.object_key ? key : existing);
            return binding;
          }
          return { ...binding, readers: [...binding.readers, ...binding.readers.filter(id => mapping.has(id)).map(id => mapping.get(id)!)],
            writers: [...binding.writers, ...binding.writers.filter(id => mapping.has(id)).map(id => mapping.get(id)!)] };
        });
        doc.object_bindings.push(...privateBindings, ...copiedProviderBindings);
      }
      doc.nodes.push(...nodes); doc.edges.push(...edges); return !!nodes.length;
    });
    if (changed) selectedNodeIds.value = clonedIds;
  }
  function replaceNode(id: string, component: string) {
    const type = catalog.value.find(t => `${t.component_id}@${t.component_version}` === component);
    if (!type) return null;
    return editable(workspace.activeWorkflowId, doc => {
      const node = doc.nodes.find(n => n.node_binding_id === id); if (!node) return false;
      const hadRegistration = (doc.object_bindings ?? []).some(binding => binding.registrar_node_id === id);
      node.component_id = type.component_id; node.component_version = type.component_version;
      node.config = graphClone(type.default_config); node.public_outputs = [];
      if (type.input_storage === "references" || type.capabilities?.some(capability => capability.startsWith("objects:"))
        || [...type.inputs, ...type.outputs, ...Object.values(type.port_modes ?? {}).flatMap(mode =>
          [...mode.inputs, ...mode.outputs])].some(port => (port.data_schema_version ?? 1) !== 1)
        || (type.provided_session_item_types?.length ?? 0) > 0 || hadRegistration)
        prepareGraphEdit(doc);
      for (const access of type.object_accesses ?? []) {
        if (access.type_id !== "workflow.variable")
          node.config[access.config_field] = access.multiple ? [] : "";
      }
      syncProvidedSessionItems(doc, id, type.provided_session_item_types ?? [], true);
      // Changed types preserve old edges for explicit diagnosis, never guess port mappings.
      return true;
    });
  }
  function setExecutionRoot(nodeId: string, enabled: boolean) {
    return editable(workspace.activeWorkflowId, doc => {
      if (!doc.nodes.some(node => node.node_binding_id === nodeId)) return false;
      prepareGraphEdit(doc);
      doc.execution_roots = [...doc.execution_roots!.filter(id => id !== nodeId), ...(enabled ? [nodeId] : [])];
      return true;
    });
  }
  function setControlDependencies(nodeId: string, sources: string[]) {
    return editable(workspace.activeWorkflowId, doc => {
      const ids = new Set(doc.nodes.map(node => node.node_binding_id));
      if (!ids.has(nodeId) || !sources.every(id => ids.has(id) && id !== nodeId)
        || new Set(sources).size !== sources.length) return false;
      prepareGraphEdit(doc);
      const existing = doc.control_edges!.filter(edge => edge.target_node_id === nodeId);
      doc.control_edges = [...doc.control_edges!.filter(edge => edge.target_node_id !== nodeId),
        ...sources.map(source => existing.find(edge => edge.source_node_id === source) ?? {
          edge_id: crypto.randomUUID(), source_node_id: source, target_node_id: nodeId,
        })];
      return true;
    });
  }
  function setObjectBindings(value: unknown) {
    if (!document.value || !isGraphObjectBindings(value, document.value.nodes)) {
      notices.notify("rejected", "对象绑定须包含唯一键、确切类型版本和有效的节点权限；私有对象仅允许所有者访问", "会话对象绑定");
      return null;
    }
    return editable(workspace.activeWorkflowId, doc => {
      prepareGraphEdit(doc);
      doc.object_bindings = graphClone(value); return true;
    });
  }
  async function refresh(id = workspace.activeWorkflowId) {
    const entry = entries.value[id]; if (!entry) return;
    const generation = (refreshGenerations.get(id) ?? 0) + 1;
    refreshGenerations.set(id, generation);
    const definitionId = entry.document.workflow_definition_id, sessionId = entry.session_id;
    const mutationVersion = mutationGeneration, viewToken = workspace.currentViewToken();
    const current = () => alive && refreshGenerations.get(id) === generation && entries.value[id] === entry
      && entry.document.workflow_definition_id === definitionId && entry.session_id === sessionId
      && mutationGeneration === mutationVersion && workspace.isCurrentView(viewToken);
    try {
      const rows = await management.queries.sessions(definitionId);
      if (!current()) return;
      sessions.value[id] = rows.map(observe);
      if (sessionId) {
        const incoming = await management.queries.session(sessionId,
          views.value[sessionId]?.workflow_definition_id ?? entry.session_document?.workflow_definition_id ?? definitionId);
        if (current()) observe(incoming);
      }
    } catch (failure) { if (current()) report(failure, "读取工作流会话"); }
  }
  function schedulePoll(id: string, generation: number) {
    if (!alive || generation !== viewGeneration || workspace.activeWorkflowId !== id) return;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    const entry = entries.value[id];
    const view = entry?.session_id ? views.value[entry.session_id] : null;
    if (view?.archived || !["running", "prepared", "pausing"].includes(view?.status ?? "")) return;
    pollTimer = setTimeout(async () => {
      if (!alive || generation !== viewGeneration || workspace.activeWorkflowId !== id) return;
      const sessionId = entries.value[id]?.session_id, mutationVersion = mutationGeneration;
      await refresh(id);
      const settled = sessionId ? views.value[sessionId] : null;
      if (alive && generation === viewGeneration && workspace.activeWorkflowId === id
        && entries.value[id]?.session_id === sessionId && mutationGeneration === mutationVersion
        && settled && !["running", "prepared", "pausing"].includes(settled.status)) {
        // Node termination can become visible before the run's final permissions.
        await refresh(id);
      }
      schedulePoll(id, generation);
    }, 350);
  }
  async function activate(id: string) {
    const generation = ++viewGeneration;
    sessionSelectionGeneration++; selectingSessionFor.value = null;
    selectedNodeIds.value = []; selectedEdgeId.value = null;
    if (pollTimer) clearTimeout(pollTimer);
    if (!entries.value[id]) return;
    if (!catalog.value.length) await loadCatalog();
    await refresh(id);
    schedulePoll(id, generation);
  }
  async function createSession(id = workspace.activeWorkflowId) {
    if (locked.value) return false;
    busy.value = "create";
    try { await management.commands.createSession({ workflowId: id }); return true; }
    catch (failure) { report(failure, "创建工作流会话"); return false; }
    finally { busy.value = null; }
  }
  async function createSavedSession(workflowId: string, expectedDefinitionId: string, expectedRevision: number) {
    if (locked.value || !entries.value[workflowId]) return false;
    busy.value = "create";
    try {
      await management.commands.createSavedSession({ workflowId }, {
        definitionId: expectedDefinitionId, definitionRevision: expectedRevision,
      });
      return true;
    } catch (failure) { report(failure, "创建已保存修订会话"); return false; }
    finally { busy.value = null; }
  }
  async function selectSession(id: string) {
    if (mutationLocked.value || !active.value) return;
    const workflowId = workspace.activeWorkflowId;
    const entry = active.value;
    const generation = ++sessionSelectionGeneration;
    const viewVersion = viewGeneration;
    const mutationVersion = mutationGeneration;
    selectingSessionFor.value = workflowId;
    const current = () => alive && generation === sessionSelectionGeneration && viewVersion === viewGeneration
      && mutationVersion === mutationGeneration
      && workspace.activeWorkflowId === workflowId && entries.value[workflowId] === entry;
    try {
      const row = await management.queries.session(id, entry.document.workflow_definition_id);
      if (!current() || mutationLocked.value) return;
      entry.session_id = row.workflow_session_id;
      if (entry.session_document?.workflow_definition_id !== row.workflow_definition_id
        || entry.session_document?.revision !== row.definition_revision) delete entry.session_document;
      observe(row);
      schedulePoll(workflowId, viewVersion);
    } catch (failure) { if (current()) report(failure, "切换会话"); }
    finally { if (generation === sessionSelectionGeneration) selectingSessionFor.value = null; }
  }
  async function submitPrimary() {
    if (!active.value) return;
    const id = workspace.activeWorkflowId;
    const action = primaryAction.value;
    if (action === "start") {
      if (locked.value || !!session.value && (!session.value.can_submit || session.value.readonly)) return;
    } else if (controlLocked.value || !session.value?.available_actions?.includes(action)) return;
    busy.value = action;
    try {
      if (action === "start") await management.commands.start({ workflowId: id });
      else await management.commands.control({ workflowId: id }, action);
      schedulePoll(id, viewGeneration);
    } catch (failure) {
      if (action === "pause" && failure instanceof WorkbenchApiError
        && failure.kind === "rejected" && failure.code === "stale_revision") {
        const message = "会话状态已变化，暂停未提交；请刷新运行后重试 [stale_revision]";
        const entry = entries.value[id];
        if (entry?.diagnostics) entry.diagnostics = entry.diagnostics.map(diagnostic =>
          diagnostic.reason_code === "stale_revision" ? { ...diagnostic, message } : diagnostic);
        notices.notify(failure.kind, message, "暂停工作流");
      } else report(failure, "运行工作流");
    }
    finally { busy.value = null; }
  }
  function setEventBindings(value: unknown) {
    return editable(workspace.activeWorkflowId, doc => {
      if (!isGraphEventBindings(value, doc.nodes)) throw new Error("检查事件身份、版本、节点 UUID、受众和本地对象 schema");
      prepareGraphEdit(doc); doc.event_bindings = graphClone(value);
      return true;
    });
  }
  async function submitEvent(eventId: string, eventVersion: number, payload: Record<string, unknown>) {
    if (locked.value || !active.value) return false;
    const id = workspace.activeWorkflowId; busy.value = "event";
    try { await management.commands.submitEvent({ workflowId: id }, eventId, eventVersion, payload);
      schedulePoll(id, viewGeneration); return true; }
    catch (failure) { report(failure, "提交局部事件"); return false; }
    finally { busy.value = null; }
  }
  async function reconcile(id = workspace.activeWorkflowId) {
    const entry = entries.value[id]; if (!entry || busy.value) return;
    const managed = managementRequests.forWorkflow(id);
    await managed.initialize();
    if (managed.pending.value) {
      busy.value = "reconcile";
      try {
        if (!await managed.reconcile())
          throw new WorkbenchApiError("unknown", managed.error.value || "原管理请求仍待核实");
      } catch (failure) { report(failure, "核实原工作流管理请求"); }
      finally { busy.value = null; }
      return;
    }
    const workflow = workspace.workflows.find(row => row.id === id);
    if (!entry.pending && workflow?.copyPending) {
      notices.notify("unknown", entry.rejected_copy ? "复制已明确拒绝；核实不会重新提交，请使用重试已拒绝复制"
        : "副本缺少原请求坐标，无法安全核实或重发；副本仍保留", "核实会话复制");
      return;
    }
    if (!entry.pending) return;
    busy.value = "reconcile";
    try {
      const request = graphClone(entry.pending);
      const receipt = await dispatch(id, request, true) as
        | { kind: "definition"; document: GraphDocument }
        | { kind: "session"; session: GraphSession }
        | { kind: "management"; result: GraphManagementResult };
      receiptChecks.value[id] = receipt.kind === "definition"
        ? `原定义回执已匹配，修订 ${receipt.document.revision}；pending 证据保留`
        : receipt.kind === "session"
          ? `原会话回执已匹配，${receipt.session.workflow_session_id}；pending 证据保留`
          : `原管理回执已匹配；pending 证据保留`;
    }
    catch (failure) { report(failure, "核实原工作流请求"); }
    finally { busy.value = null; }
  }
  function canRetryRejectedCopy(id: string) {
    const entry = entries.value[id], workflow = workspace.workflows.find(row => row.id === id);
    const source = workflow?.sourceId ? entries.value[workflow.sourceId] : null;
    const rejected = entry?.rejected_copy;
    return !!entry && !entry.pending && !!workflow?.copyPending && !!source?.session_id
      && rejected?.action === "copy" && rejected.path === `/api/graph/sessions/${source.session_id}/copy`
      && isGraphDocument(rejected.body.document)
      && graphSignature(rejected.body.document) === graphSignature(entry.document);
  }
  async function retryRejectedCopy(id: string) {
    if (busy.value || applicationBusy.value || !canRetryRejectedCopy(id)) return;
    const entry = entries.value[id], rejected = graphClone(entry.rejected_copy!);
    const sourceId = workspace.workflows.find(row => row.id === id)!.sourceId!;
    const source = entries.value[sourceId], sessionId = source.session_id!;
    busy.value = "copy";
    try {
      const view = await management.queries.session(sessionId, source.document.workflow_definition_id);
      if (!alive || entries.value[id] !== entry || entries.value[sourceId] !== source
        || source.session_id !== sessionId || !canRetryRejectedCopy(id)
        || JSON.stringify(entry.rejected_copy) !== JSON.stringify(rejected))
        throw new WorkbenchApiError("unknown", "已拒绝复制的依据已变化，未提交新请求");
      await dispatch(id, command(`/api/graph/sessions/${sessionId}/copy`, {
        document: graphClone(entry.document), expected_session_revision: view.revision,
        expected_data_revision: view.data_revision, expected_definition_revision: view.definition_revision,
        expected_head_revision: view.head_revision, mappings: entry.state_mappings ?? [],
      }, "copy"));
    } catch (failure) { report(failure, "重试已拒绝会话复制"); }
    finally { busy.value = null; }
  }
  function discardDraft() {
    const workflow = workspace.activeWorkflow;
    if (locked.value || !editorStable.value || workflow.state !== "draft") return;
    const id = workflow.id;
    const entry = entries.value[id];
    if (entry?.saved_document) {
      entry.document = graphClone(entry.saved_document);
      entry.state_mappings = [];
      history.value[id] = { past: [], future: [] };
      workspace.updateWorkflowProjection(id, entry.document.name, entry.document.nodes.length);
      workspace.markWorkflowSaved(id, entry.document.name);
      return;
    }
    const fallback = workspace.workflows.find(row => row.id !== id && !row.copyPending)?.id;
    if (fallback) workspace.openWorkflow(fallback);
    else {
      workspace.activeWorkflowId = ""; workspace.selectedWorkflowId = ""; workspace.viewVersion++;
    }
    workspace.removeWorkflow(id);
    delete entries.value[id]; delete history.value[id];
  }
  function discardRejectedCopy(id: string) {
    const entry = entries.value[id]; const workflow = workspace.workflows.find(row => row.id === id);
    if (busy.value || entry?.pending || !workflow?.copyPending) return;
    workspace.removeWorkflow(id);
    delete entries.value[id]; delete history.value[id];
  }
  async function writeData(variables: Record<string, unknown>, shared?: Record<string, unknown>) {
    const id = workspace.activeWorkflowId, view = session.value;
    if (!view || locked.value || !view.can_submit || view.readonly || view.archived) return false;
    busy.value = "write-data";
    try { await dispatch(id, command(`/api/graph/sessions/${view.workflow_session_id}/data`, {
      expected_revision: view.revision, expected_data_revision: view.data_revision, variables, ...(shared ? { shared } : {}) }, "data")); return true; }
    catch (failure) { report(failure, "写入会话数据"); return false; }
    finally { busy.value = null; }
  }
  async function closeRun() {
    const view = session.value;
    if (!view || controlLocked.value || !view.available_actions?.includes("close")) return;
    busy.value = "close";
    try { await management.commands.control({ workflowId: workspace.activeWorkflowId }, "close"); }
    catch (failure) { report(failure, "关闭运行"); }
    finally { busy.value = null; }
  }
  const retryAgentActions = ["retry_archive", "retry_acceptance", "retry_failed_node"] as const;
  async function submitAgentAction(action: typeof retryAgentActions[number]) {
    if (!(retryAgentActions as readonly string[]).includes(action)) return false;
    const view = session.value;
    if (!view || controlLocked.value || !view.available_actions?.includes(action)) return false;
    const id = workspace.activeWorkflowId; busy.value = action;
    try {
      await management.commands.control({ workflowId: id }, action);
      schedulePoll(id, viewGeneration); return true;
    } catch (failure) { report(failure, action === "retry_acceptance" ? "结果接纳重试"
      : action === "retry_failed_node" ? "重试失败节点" : "归档重试"); return false; }
    finally { busy.value = null; }
  }
  async function loadCandidates(workflowId = workspace.activeWorkflowId) {
    const entry = entries.value[workflowId];
    const view = entry?.session_id ? views.value[entry.session_id] : null;
    if (!view) return null;
    const generation = (candidateGenerations.get(workflowId) ?? 0) + 1;
    candidateGenerations.set(workflowId, generation);
    const mutationVersion = mutationGeneration, viewToken = workspace.currentViewToken();
    const currentRead = () => alive && candidateGenerations.get(workflowId) === generation
      && entries.value[workflowId] === entry && entry.session_id === view.workflow_session_id
      && views.value[view.workflow_session_id]?.workflow_definition_id === view.workflow_definition_id
      && mutationGeneration === mutationVersion && workspace.isCurrentView(viewToken);
    try {
      const value = await management.queries.candidates(view.workflow_session_id, view.workflow_definition_id);
      const current = views.value[view.workflow_session_id];
      if (!currentRead()
        || current && (value.session_revision < current.revision || value.head_revision < current.head_revision)) return null;
      const previous = candidates.value[view.workflow_session_id];
      if (previous && previous.session_revision > value.session_revision) return previous;
      candidates.value[view.workflow_session_id] = value; return value;
    } catch (failure) { if (currentRead()) report(failure, "读取工作流候选"); return null; }
  }
  function setInputs(value: Record<string, unknown>) {
    if (!active.value || editorLocked.value) return;
    active.value.external_inputs = graphClone(value);
  }
  function resetPrivateState(id: string, nodeId: string) {
    const entry = entries.value[id]; if (!entry || entry.pending || busy.value) return;
    if (!entry.document.nodes.some(node => node.node_binding_id === nodeId)) return;
    entry.state_mappings = [...(entry.state_mappings ?? []).filter(mapping => mapping.target_node_id !== nodeId),
      { source_node_id: nodeId, target_node_id: nodeId, action: "reset" }];
  }
  return { entries, catalog, frontendExtensions, packageLock, executionPackageLock, dataTypes, catalogLoading, catalogError, definitionsLoading, sessions, views, candidates, history, busy, receiptChecks, active, document,
    session, pending, locked, controlLocked, editorLocked, primaryAction, statusLabel, selectedNodeIds, selectedEdgeId, canUndo, canRedo, setEventBindings, submitEvent,
    isGeneric, createWorkflow, createWorkflowDraft, createSerialAgentExample, createCompactingSerialAgentExample, typeFor, loadCatalog, setPersistenceGuard, storeSnapshot, restoreSnapshot,
    saveWorkflow, loadDefinitions, manageWorkflow, manageSession, copyWorkflow, renameWorkflow, refreshPublishedRevision, addNode, patchNode,
    patchNodeConfiguration, patchNodeConfigurationPolicy, moveNodes, connect, removeSelection, reorderEdge, undo, duplicateSelection,
    replaceNode, setExecutionRoot, setControlDependencies, setObjectBindings, attachFrontendDisplay, patchFrontendSource,
    activate, refresh, createSession, createSavedSession, selectSession, submitPrimary, reconcile, canRetryRejectedCopy, retryRejectedCopy, discardDraft, discardRejectedCopy, writeData, closeRun, setInputs, resetPrivateState, submitAgentAction, loadCandidates,
    management, editing, installWorkflowConfigurationApplication, configurationPendingRequests, reconcileConfiguration,
    managementPendingRequests };
});
