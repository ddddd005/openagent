import { computed, onScopeDispose, ref, watch } from "vue";
import { defineStore } from "pinia";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { readGraphReceipt } from "../adapters/workflowApplicationApi";
import { createGraphCommand as command, dispatchGraphCommand, type GraphReceipt } from "../application/workflowCommands";
import { createWorkflowManagementRequests } from "../application/workflowManagementRequests";
import { createWorkflowManagement, workflowManagementQueries } from "../application/workflowManagement";
import { createWorkflowConfigurationCopy, type WorkflowConfigurationCopyApplication } from "../application/workflowConfigurationCopy";
import { createWorkflowEditing } from "../application/workflowEditing";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";
import { newerGraphObservation } from "../domain/graphObservation";
import { graphConnectionIssue } from "../domain/graphConnections";
import { defaultGraphConfigurationPolicy, graphClone, graphNodeAllowsAdjustable, graphObject, graphUuid, isGraphDocument, newGraph,
  isGraphObjectBindings, isGraphEventBindings, isGraphJsonValue, graphSignature, upgradeGraph,
  type GraphCommand, type GraphDocument, type GraphEdge, type GraphEntry, type GraphNode,
  type GraphNodeType, type GraphSession, type GraphDataType,
  type GraphManagementResult } from "../domain/workflowGraph";
import { useWorkspaceStore } from "./workspace";
import { useWorkbenchNoticesStore } from "./workbenchNotices";
import type { FrontendExtension } from "../domain/frontendExtensions";
import { isGraphConfigurationDocument, type GraphConfigurationDocument, type GraphConfigurationResult,
  type GraphConfigurationUpdateDocument, type GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";
import { isWorkflowConfigurationCopyRequest, isWorkflowConfigurationCopyResult,
  workflowConfigurationCopyTargetName,
  type WorkflowConfigurationCopyRequest, type WorkflowConfigurationCopyResult } from "../domain/workflowConfigurationCopy";
import type { WorkflowNodeConfigurationRequest, WorkflowNodeConfigurationResult } from "../plugins/workflowFrontendSdk";
import { syncVariableRegistrationEvent, variableRegistrationTarget } from "../domain/workflowVariableRegistration";
import { sessionSummaryFromView, type WorkflowSessionSummary } from "../domain/workflowDirectory";

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
// Compare the edit body without the counters that a save is expected to move.
function sameConfigurationBody(left: GraphDocument, right: GraphDocument) {
  const body = (document: GraphDocument) =>
    graphSignature({ ...document, revision: 0, edit_sequence: 0 });
  return body(left) === body(right);
}

function syncProvidedSessionItems(document: GraphDocument, nodeId: string,
  provided: NonNullable<GraphNodeType["provided_session_item_types"]>, preserveExact: boolean,
  accesses: NonNullable<GraphNodeType["object_accesses"]> = []) {
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
  const node = document.nodes.find(row => row.node_binding_id === nodeId);
  if (!node) return;
  for (const access of accesses) {
    const own = document.object_bindings.find(binding => binding.registrar_node_id === nodeId
      && binding.type_id === access.type_id && binding.schema_version === access.schema_version);
    if (!own) continue;
    if (access.access !== "write" && !own.readers.includes(nodeId)) own.readers.push(nodeId);
    if (access.access !== "read" && !own.writers.includes(nodeId)) own.writers.push(nodeId);
    node.config[access.config_field] = access.multiple ? [own.object_key] : own.object_key;
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
  /** Formal session directory rows (summary projection); never full session views. */
  const sessionRows = ref<Record<string, WorkflowSessionSummary[]>>({});
  const views = ref<Record<string, GraphSession>>({});
  const history = ref<Record<string, { past: GraphDocument[]; future: GraphDocument[] }>>({});
  const busy = ref<string | null>(null);
  const applicationBusy = ref(false);
  const managementTargets = new Map<string, { entry: GraphEntry; sessionId: string | null;
    selection: number; view: ReturnType<typeof workspace.currentViewToken> }>();
  const managementRequests = createWorkflowManagementRequests({
    current: id => alive && !!entries.value[id],
    accept: acceptManagementReceipt,
    completed: (id, command) => finishManagedDeletion(id, command),
  });
  const configurationSubmitting = ref<Record<string, boolean>>({});
  const configurationApplicationVersion = ref(0);
  let configurationApplication: WorkflowConfigurationApplication | null = null;
  let copyApplication: WorkflowConfigurationCopyApplication | null = null;
  type CopyChannel = ReturnType<WorkflowConfigurationCopyApplication["forSource"]>;
  const copyChannels = new Map<string, CopyChannel>();
  const copyInitializedSources = new Set<string>();
  const copyOpenTokens = new Map<string, { targetId: string;
    token: ReturnType<typeof workspace.currentViewToken> }>();
  const copyVersion = ref(0);
  const bumpCopyVersion = () => { copyVersion.value++; };
  const receiptChecks = ref<Record<string, string>>({});
  const selectingSessionFor = ref<string | null>(null);
  const selectedNodeIds = ref<string[]>([]);
  const selectedEdgeId = ref<string | null>(null);
  const active = computed(() => entries.value[workspace.activeWorkflowId]);
  const document = computed(() => active.value?.document ?? null);
  /** A formal fixed revision with no current configuration: read-only, never editable. */
  const fixedOnly = computed(() => active.value?.fixed_only === true);
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
    || fixedOnly.value
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
  const definitionEntryLoads = new Map<string, Promise<boolean>>();
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
  function installWorkflowConfigurationApplication(application: WorkflowConfigurationApplication,
    options: { seed?: boolean } = {}) {
    configurationApplication = application;
    configurationApplicationVersion.value++;
    // Production installs before the directory read, so the current read below
    // establishes accepted. Tests that construct entries by hand must ask for
    // this explicit seed instead of relying on an implicit reverse observe.
    if (options.seed)
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
  function projectAcceptedConfiguration(workflowId: string, archived: boolean) {
    const accepted = configurationApplication?.accepted(workflowId);
    if (!accepted) return false;
    if (!entries.value[workflowId]) {
      entries.value[workflowId] = { document: accepted, saved_document: graphClone(accepted),
        saved_revision: accepted.revision, session_id: null, pending: null };
      history.value[workflowId] = { past: [], future: [] };
      workspace.registerGraphWorkflow({ id: workflowId, title: accepted.name, description: "",
        nodeCount: accepted.nodes.length, state: "saved", archived });
    }
    projectAcceptedState(workflowId, workflowId, accepted, workspace.currentViewToken(), "none", false);
    workspace.setWorkflowManagement(workflowId, archived);
    return true;
  }
  /** Refresh only the formal directory projection from the summary query.
   *
   * This never constructs an editor entry and never accepts a summary row as a
   * configuration document. Opening a workflow goes through
   * `ensureDefinitionEntry` and the single configuration acceptance path.
   */
  async function loadDefinitions() {
    if (definitionsLoading.value) return false;
    definitionsLoading.value = true;
    const generation = mutationGeneration;
    try {
      const rows = await management.queries.definitionSummaries(true);
      if (!alive || generation !== mutationGeneration) return false;
      // An opened workflow keeps its accepted basis. A directory row older than the
      // accepted current must not roll the visible projection back.
      workspace.applyDefinitionSummaries(rows.filter(row => {
        const accepted = configurationApplication?.accepted(row.workflow_definition_id);
        if (!accepted) return true;
        return row.edit_sequence !== null && row.edit_sequence >= accepted.edit_sequence;
      }));
      selectConfigurationBasis();
      return true;
    } catch (failure) { report(failure, "读取工作流目录"); return false; }
    finally { definitionsLoading.value = false; }
  }
  /** Read the formal current configuration only when a workflow is opened.
   *
   * The existing configuration acceptance arbitrates duplicates/stale/conflict;
   * a formal fixed revision without a current configuration stays a read-only
   * fixed display and is never accepted as an editable current configuration.
   */
  async function ensureDefinitionEntry(id: string): Promise<boolean> {
    if (!alive || !graphUuid(id)) return false;
    if (entries.value[id]) return true;
    const loading = definitionEntryLoads.get(id);
    if (loading) return loading;
    const load = (async () => {
      try {
        const document = await management.queries.definition(id);
        if (!alive) return false;
        if (entries.value[id]) return true;
        if (!isGraphDocument(document) || document.workflow_definition_id !== id)
          throw new WorkbenchApiError("unavailable", "工作流定义身份或版本不匹配");
        const workflow = workspace.workflows.find(row => row.id === id);
        const archived = workflow?.archived === true;
        if (isGraphConfigurationDocument(document)) {
          if (configurationApplication) {
            const acceptance = configurationApplication.acceptDocument(document);
            if (acceptance.status === "conflict" || acceptance.status === "invalid")
              throw new WorkbenchApiError("unavailable", acceptance.message ?? "工作流配置无法接纳");
            if (!projectAcceptedConfiguration(id, archived))
              throw new WorkbenchApiError("unknown", "配置结果尚未被正式配置应用接纳");
          } else {
            entries.value[id] = { document: graphClone(document), saved_document: graphClone(document),
              saved_revision: document.revision, latest_published_revision: document.revision,
              session_id: null, pending: null };
            history.value[id] = { past: [], future: [] };
          }
          if (!workspace.workflows.some(row => row.id === id))
            workspace.registerGraphWorkflow({ id, title: document.name, description: "",
              nodeCount: document.nodes.length, state: "saved", archived });
          workspace.markWorkflowSaved(id, document.name);
          workspace.setWorkflowManagement(id, archived);
          workspace.setWorkflowCurrent(id, document.revision, document.edit_sequence);
          return true;
        }
        // Formal fixed record without current configuration: keep the honest
        // fixed read-only display; do not invent edit_sequence or acceptance.
        const fixed = graphClone(document);
        entries.value[id] = { document: fixed, saved_document: graphClone(fixed),
          saved_revision: fixed.revision, latest_published_revision: fixed.revision,
          session_id: null, pending: null, fixed_only: true };
        history.value[id] = { past: [], future: [] };
        if (!workspace.workflows.some(row => row.id === id))
          workspace.registerGraphWorkflow({ id, title: fixed.name, description: "",
            nodeCount: fixed.nodes.length, state: "saved", archived });
        workspace.updateWorkflowProjection(id, fixed.name, fixed.nodes.length);
        workspace.markWorkflowSaved(id, fixed.name);
        workspace.setWorkflowManagement(id, archived);
        const fixedRow = workspace.workflows.find(row => row.id === id);
        if (fixedRow) {
          fixedRow.fixedDefinitionRevision = fixed.revision;
          fixedRow.editSequence = null;
          fixedRow.hasCurrentConfiguration = false;
        }
        notices.notify("unavailable",
          `工作流 ${fixed.name || id} 只有固定修订、没有正式 current 配置，仅可按只读方式查看`,
          "打开工作流");
        return true;
      } catch (failure) {
        if (alive) report(failure, "读取工作流配置");
        return false;
      } finally { definitionEntryLoads.delete(id); }
    })();
    definitionEntryLoads.set(id, load);
    return load;
  }
  function report(failure: unknown, action: string) {
    const error = failure instanceof WorkbenchApiError ? failure : new WorkbenchApiError("unavailable", String(failure));
    notices.notify(error.kind, error.reason, action);
  }
  function projectAcceptedState(sourceId: string, targetId: string, accepted: GraphConfigurationDocument,
    viewToken: ReturnType<typeof workspace.currentViewToken>, historyAction: ConfigurationHistoryAction,
    derived: boolean) {
    const source = entries.value[sourceId];
    if (!source) throw new WorkbenchApiError("unknown", "配置结果的原工作流已不存在");
    if (!derived) {
      const unchanged = source.document.edit_sequence === accepted.edit_sequence
        && graphSignature(source.document) === graphSignature(accepted);
      if (!unchanged) {
        const record = history.value[sourceId] ?? { past: [], future: [] };
        if (historyAction === "edit" && source.saved_revision > 0) {
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
        source.document = accepted;
      }
      source.saved_document = graphClone(accepted);
      source.saved_revision = accepted.revision;
      source.latest_published_revision = Math.max(source.latest_published_revision ?? 0, accepted.revision);
      workspace.updateWorkflowProjection(sourceId, accepted.name, accepted.nodes.length);
      workspace.markWorkflowSaved(sourceId, accepted.name);
      workspace.setWorkflowCurrent(sourceId, accepted.revision, accepted.edit_sequence);
      return { workflowId: targetId, document: accepted };
    }
    const existing = entries.value[targetId];
    entries.value[targetId] = { ...(existing ?? {}), document: accepted, saved_document: graphClone(accepted),
      saved_revision: accepted.revision, latest_published_revision: accepted.revision,
      session_id: null, pending: null };
    history.value[targetId] = history.value[targetId] ?? { past: [], future: [] };
    if (!workspace.workflows.some(row => row.id === targetId))
      workspace.registerGraphWorkflow({ id: targetId, title: accepted.name, description: "",
        nodeCount: accepted.nodes.length, state: "saved", sessionCount: 0 });
    workspace.updateWorkflowProjection(targetId, accepted.name, accepted.nodes.length);
    workspace.markWorkflowSaved(targetId, accepted.name);
    workspace.setWorkflowCurrent(targetId, accepted.revision, accepted.edit_sequence);
    if (workspace.activeWorkflowId === sourceId && workspace.isCurrentView(viewToken))
      workspace.openWorkflow(targetId);
    return { workflowId: targetId, document: accepted };
  }
  function projectConfigurationResult(sourceId: string, result: GraphConfigurationResult,
    viewToken: ReturnType<typeof workspace.currentViewToken>, historyAction: ConfigurationHistoryAction) {
    if (result.outcome === "confirmation_required") return null;
    const targetId = result.workflow_definition_id, derived = targetId !== sourceId;
    if (derived && result.outcome !== "derived")
      throw new WorkbenchApiError("unknown", "配置回执的工作流归属无效");
    const accepted = configurationApplication?.accepted(targetId);
    if (!accepted) throw new WorkbenchApiError("unknown", "配置结果尚未被正式配置应用接纳");
    return projectAcceptedState(sourceId, targetId, accepted, viewToken, historyAction, derived);
  }
  // A save may have been accepted before evidence closing failed. Project that
  // accepted state without reading the unaccepted lastResult backdoor.
  function projectChangedAcceptance(workflowId: string,
    viewToken: ReturnType<typeof workspace.currentViewToken>, historyAction: ConfigurationHistoryAction) {
    const accepted = configurationApplication?.accepted(workflowId), source = entries.value[workflowId];
    if (!accepted || !source) return false;
    if (source.document.edit_sequence === accepted.edit_sequence
      && graphSignature(source.document) === graphSignature(accepted)) return false;
    projectAcceptedState(workflowId, workflowId, accepted, viewToken, historyAction, false);
    return true;
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
    // Capture the body and counters from the same edit baseline. Waiting for
    // initialization must re-check this baseline, never fetch a newer version
    // and silently attach the old body to it.
    const baseline = graphClone(source.document);
    configurationSubmitting.value[workflowId] = true;
    const viewToken = workspace.currentViewToken();
    let request: GraphConfigurationUpdateRequest | null = null;
    try {
      await entry.initialize();
      const workflow = workspace.workflows.find(row => row.id === workflowId);
      const management = managementRequests.forWorkflow(workflowId);
      if (busy.value || applicationBusy.value || management.pending.value || management.busy.value
        || workflow?.archived || workflow?.deleted)
        throw new WorkbenchApiError("rejected", "工作流管理操作正在进行，未提交配置修改",
          undefined, "graph_management_pending");
      const current = application.accepted(workflowId);
      if (!current && source.saved_revision > 0)
        throw new WorkbenchApiError("unavailable", "正式配置读取暂不可用；未提交工作流修改");
      if (current && graphSignature(current) !== graphSignature(baseline))
        throw new WorkbenchApiError("rejected", "配置编辑依据已变化；请重新读取工作流",
          undefined, "stale_configuration_basis");
      const editSequence = current?.edit_sequence ?? 0;
      const document = configurationUpdateDocument(proposed, current ? editSequence : null);
      document.revision = current?.revision ?? 1;
      request = { workflow_definition_id: workflowId,
        expected_edit_sequence: editSequence, document, idempotency_key: crypto.randomUUID() };
      let result: GraphConfigurationResult;
      try {
        result = await entry.submit(request);
      } catch (failure) {
        // The application may have accepted the write before evidence closing
        // failed; reflect only that accepted state, not an unaccepted result.
        const accepted = application.accepted(workflowId);
        const fromThisRequest = !!request && !!accepted && sameConfigurationBody(accepted, request.document);
        projectChangedAcceptance(workflowId, viewToken, fromThisRequest ? historyAction : "none");
        throw failure;
      }
      const settled = await settleConfigurationQuestion(workflowId, result);
      if (!settled) return null;
      projectConfigurationResult(workflowId, settled, viewToken, historyAction);
      return settled;
    } catch (failure) {
      // A changed basis or management gate rejects this edit; only sync the
      // accepted projection, without advancing history for the rejected body.
      projectChangedAcceptance(workflowId, viewToken, "none");
      throw failure;
    } finally {
      delete configurationSubmitting.value[workflowId];
    }
  }
  async function patchNodeConfiguration(request: WorkflowNodeConfigurationRequest):
    Promise<WorkflowNodeConfigurationResult | null> {
    const application = configurationApplication;
    if (!application)
      throw new WorkbenchApiError("unavailable", "正式配置应用不可用，节点配置尚未提交");
    const source = entries.value[request.workflowId], document = application.accepted(request.workflowId);
    if (!alive || locked.value || request.workflowId !== workspace.activeWorkflowId || !source
      || !isGraphConfigurationDocument(document) || !graphObject(request.patch) || !isGraphJsonValue(request.patch))
      return null;
    if (graphSignature(source.document) !== graphSignature(document)) return null;
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
      const workflow = workspace.workflows.find(row => row.id === request.workflowId);
      const management = managementRequests.forWorkflow(request.workflowId);
      if (busy.value || applicationBusy.value || management.pending.value || management.busy.value
        || workflow?.archived || workflow?.deleted)
        throw new WorkbenchApiError("rejected", "工作流管理操作正在进行，未提交节点配置",
          undefined, "graph_management_pending");
      const current = application.accepted(request.workflowId);
      if (!isGraphConfigurationDocument(current) || graphSignature(current) !== graphSignature(document))
        throw new WorkbenchApiError("rejected", "配置编辑依据已变化；请重新读取工作流",
          undefined, "stale_configuration_basis");
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
      const accepted = projectConfigurationResult(request.workflowId, settled, viewToken, "edit");
      if (!accepted) return null;
      return { workflowId: accepted.workflowId, nodeId: request.nodeId, outcome: settled.outcome };
    } catch (failure) {
      projectChangedAcceptance(request.workflowId, viewToken, "edit");
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
      if (!node || !graphNodeAllowsAdjustable(node, typeFor(node))
        || document.schema_version !== 3 || !node.configuration_policy
        || !["fixed", "adjustable"].includes(mode)) return false;
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
      projectConfigurationResult(workflowId, settled, viewToken, "edit");
      return true;
    } catch (failure) {
      projectChangedAcceptance(workflowId, viewToken, "edit");
      report(failure, "核实工作流配置原请求");
      return false;
    } finally { delete configurationSubmitting.value[workflowId]; }
  }
  function observe(view: GraphSession) {
    const accepted = newerGraphObservation(views.value[view.workflow_session_id], view);
    if (alive) views.value[view.workflow_session_id] = accepted;
    return accepted;
  }
  /** Project a freshly accepted full session view into its directory row only. */
  function projectAcceptedSessionRow(workflowId: string, view: GraphSession) {
    const row = sessionSummaryFromView(view);
    const rows = sessionRows.value[workflowId] ?? [];
    const index = rows.findIndex(item => item.workflow_session_id === row.workflow_session_id);
    sessionRows.value[workflowId] = index >= 0
      ? rows.map(item => item.workflow_session_id === row.workflow_session_id ? row : item)
      : [...rows, row];
  }
  function markSessionRowArchived(workflowId: string, sessionId: string) {
    const rows = sessionRows.value[workflowId] ?? [];
    if (!rows.some(row => row.workflow_session_id === sessionId)) return;
    sessionRows.value[workflowId] = rows.map(row => row.workflow_session_id === sessionId
      ? { ...row, archived: true } : row);
  }
  async function acceptManagementReceipt(id: string, receipt: GraphReceipt, request: GraphCommand) {
    const entry = entries.value[id];
    if (!alive || !entry) return;
    if (receipt.kind === "session") {
      const accepted = observe(receipt.session);
      projectAcceptedSessionRow(id, accepted);
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
    // Accepting a definition management receipt invalidates directory reads
    // that were issued while the command was in flight.
    mutationGeneration++;
    if (receipt.result.deleted) {
      // The business is deleted, but the original request is not closed yet.
      // Keep the entry and its verification owner until the outbox resolves;
      // a failed resolution must remain visible instead of disappearing here.
      if (request.action === "definition-delete") workspace.setWorkflowManagement(id, true, true);
    } else if (request.action === "definition-archive") {
      workspace.setWorkflowManagement(id, true);
    } else {
      const sessionId = request.source_session_id, view = sessionId ? views.value[sessionId] : null;
      if (sessionId) markSessionRowArchived(id, sessionId);
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
  const guardedManagementActions = new Set([
    "definition-archive", "definition-delete", "session-archive", "session-delete",
  ]);
  // The public management commands and the outer manageWorkflow/manageSession
  // both reach this shared dispatch path. Check the target's own configuration
  // and management state here, before any new original-request evidence.
  async function guardManagementWrite(id: string, command: GraphCommand) {
    if (!guardedManagementActions.has(command.action)) return;
    const entry = entries.value[id], application = configurationApplication;
    if (!entry) throw new WorkbenchApiError("unavailable", "工作流不存在");
    if (command.action.startsWith("session-")) {
      const sessionId = command.source_session_id;
      const view = sessionId ? views.value[sessionId] : null;
      if (!sessionId || !view || view.workflow_definition_id !== entry.document.workflow_definition_id)
        throw new WorkbenchApiError("rejected", "目标会话不属于该工作流，未提交管理请求",
          undefined, "graph_management_target_invalid");
    }
    const channel = application?.forWorkflow(id);
    if (channel) {
      const initialized = await channel.initialize();
      if (!initialized)
        throw new WorkbenchApiError("unavailable", "工作流原请求证据未能初始化，未提交管理请求");
      if (!alive || entries.value[id] !== entry || configurationApplication !== application)
        throw new WorkbenchApiError("unavailable", "工作流目标已变化，未提交管理请求");
      if (configurationSubmitting.value[id] || channel.locked.value)
        throw new WorkbenchApiError("rejected", "工作流配置正在提交或待核实，未提交管理请求",
          undefined, "graph_configuration_pending");
    }
    const requests = managementRequests.forWorkflow(id);
    const ready = await requests.initialize();
    if (!ready)
      throw new WorkbenchApiError("unavailable", "工作流管理原请求证据未能初始化，未提交管理请求");
    if (!alive || entries.value[id] !== entry || configurationApplication !== application)
      throw new WorkbenchApiError("unavailable", "工作流目标已变化，未提交管理请求");
    if (channel && (configurationSubmitting.value[id] || channel.locked.value))
      throw new WorkbenchApiError("rejected", "工作流配置正在提交或待核实，未提交管理请求",
        undefined, "graph_configuration_pending");
    if (entry.pending || requests.pending.value || requests.busy.value)
      throw new WorkbenchApiError("unknown", "目标工作流仍有管理请求未结束，未提交新请求");
    const workflow = workspace.workflows.find(row => row.id === id);
    if (workflow?.deleted)
      throw new WorkbenchApiError("rejected", "目标工作流已删除，未提交管理请求");
  }
  async function dispatch(id: string, command: GraphCommand, reconcile = false): Promise<unknown> {
    if (!reconcile) await guardManagementWrite(id, command);
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
    try {
      return await dispatchGraphCommand(entry, command, {
        readReceipt: readGraphReceipt,
        readDefinition: workflowManagementQueries.definition,
        observedSession: sessionId => views.value[sessionId],
        current: () => alive && entries.value[id] === entry && mutationGeneration === generation
          && (command.action !== "event"
          || entry.session_id === eventSessionId && entry.document.workflow_definition_id === eventDefinitionId
            && entry.document.revision === eventDefinitionRevision),
      }, reconcile);
    } finally { mutationGeneration++; }
  }
  const management = createWorkflowManagement({
    entry: id => entries.value[id], observedSession: id => views.value[id], observe, dispatch,
    commandState: value => { applicationBusy.value = value; mutationGeneration++; },
    canSubmit(id) {
      const workflow = workspace.workflows.find(row => row.id === id);
      return alive && !!workflow && !workflow.copyPending
        && !workspace.workflows.some(row => row.copyPending && row.sourceId === id);
    },
  });
  const editing = createWorkflowEditing({
    entry: id => entries.value[id],
    canEdit: id => alive && !busy.value && !applicationBusy.value && !entries.value[id]?.pending
      && !entries.value[id]?.fixed_only
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
  });
  async function finishManagedDeletion(id: string, request: Pick<GraphCommand, "action" | "source_session_id">) {
    const entry = entries.value[id];
    if (!entry || entry.pending) return;
    if (request.action === "definition-delete") {
      mutationGeneration++;
      const ownedSessions = Object.values(views.value)
        .filter(view => view.workflow_definition_id === entry.document.workflow_definition_id);
      for (const view of ownedSessions) {
        delete views.value[view.workflow_session_id];
      }
      if (workspace.activeWorkflowId === id) {
        const fallback = workspace.workflows.find(row => row.id !== id && !row.copyPending && !row.deleted);
        if (fallback) workspace.openWorkflow(fallback.id);
        else { workspace.activeWorkflowId = ""; workspace.selectedWorkflowId = ""; workspace.viewVersion++; }
      }
      workspace.removeWorkflow(id);
      delete entries.value[id]; delete history.value[id]; delete sessionRows.value[id];
      configurationApplication?.forget(id);
    } else if (request.action === "session-delete") {
      const sessionId = request.source_session_id!;
      sessionRows.value[id] = (sessionRows.value[id] ?? [])
        .filter(row => row.workflow_session_id !== sessionId);
      delete views.value[sessionId];
      if (entry.session_id === sessionId) {
        entry.session_id = null; delete entry.session_document;
      }
    } else return;
  }
  async function manageWorkflow(id: string, action: "archive" | "delete") {
    if (busy.value || applicationBusy.value || !entries.value[id] || entries.value[id].pending) return false;
    const configuration = configurationApplication?.forWorkflow(id);
    if (configuration) {
      await configuration.initialize();
      if (configuration.locked.value || configurationSubmitting.value[id]) return false;
    }
    const requests = managementRequests.forWorkflow(id);
    if (requests.pending.value || requests.busy.value) return false;
    busy.value = `definition-${action}`;
    try {
      await management.commands.manageDefinition({ workflowId: id }, action);
      if (action === "archive") await refresh(id);
      return true;
    } catch (failure) { report(failure, action === "archive" ? "归档工作流" : "删除工作流"); return false; }
    finally { busy.value = null; }
  }
  async function manageSession(sessionId: string, action: "archive" | "delete", id = workspace.activeWorkflowId) {
    if (busy.value || applicationBusy.value || !entries.value[id] || entries.value[id].pending) return false;
    const configuration = configurationApplication?.forWorkflow(id);
    if (configuration) {
      await configuration.initialize();
      if (configuration.locked.value || configurationSubmitting.value[id]) return false;
    }
    const requests = managementRequests.forWorkflow(id);
    if (requests.pending.value || requests.busy.value) return false;
    busy.value = `session-${action}`;
    try {
      await management.commands.manageSession({ workflowId: id }, sessionId, action);
      if (action === "archive") await refresh(id);
      return true;
    } catch (failure) { report(failure, action === "archive" ? "归档会话" : "删除会话"); return false; }
    finally { busy.value = null; }
  }
  function copyChannel(sourceWorkflowDefinitionId: string): CopyChannel {
    const sourceId = sourceWorkflowDefinitionId.toLowerCase();
    copyApplication ??= createWorkflowConfigurationCopy({
      current: id => alive && entries.value[id] !== undefined,
      accept: acceptCopiedConfiguration,
    });
    let channel = copyChannels.get(sourceId);
    if (!channel) {
      channel = copyApplication.forSource(sourceId);
      copyChannels.set(sourceId, channel);
      bumpCopyVersion();
    }
    return channel;
  }
  async function restoreCopyChannel(sourceWorkflowDefinitionId: string) {
    const sourceId = sourceWorkflowDefinitionId.toLowerCase();
    if (copyInitializedSources.has(sourceId)) return;
    copyInitializedSources.add(sourceId);
    try {
      const channel = copyChannel(sourceId);
      if (!await channel.initialize() && channel.error.value)
        notices.notify("unavailable", channel.error.value, "读取配置复制原请求");
    } catch (failure) { report(failure, "读取配置复制原请求"); }
    finally { bumpCopyVersion(); }
  }
  /** Project a first-time copy result into the formal target directory/entry. */
  async function acceptCopiedConfiguration(request: WorkflowConfigurationCopyRequest,
    result: WorkflowConfigurationCopyResult, reconcile: boolean) {
    if (!isWorkflowConfigurationCopyResult(result, request))
      throw new WorkbenchApiError("unknown", "配置复制结果与请求不匹配，保留原请求");
    // A reconcile receipt proves the original copy happened; the target's current
    // availability is confirmed separately and never by registering the historical document.
    if (reconcile) return;
    const targetId = result.workflow_definition_id;
    let accepted: GraphConfigurationDocument | null = null;
    if (configurationApplication) {
      const acceptance = configurationApplication.acceptDocument(result.document);
      if (acceptance.status === "conflict" || acceptance.status === "invalid")
        throw new WorkbenchApiError("unknown", acceptance.message ?? "配置复制结果无法被正式配置应用接纳");
      accepted = configurationApplication.accepted(targetId);
    }
    accepted ??= isGraphConfigurationDocument(result.document) ? result.document : null;
    if (!accepted || accepted.workflow_definition_id !== targetId)
      throw new WorkbenchApiError("unknown", "配置复制结果缺少正式 current 配置");
    const source = entries.value[request.source_workflow_definition_id];
    if (!source) throw new WorkbenchApiError("unknown", "配置复制来源工作流已关闭");
    const token = copyOpenTokens.get(request.source_workflow_definition_id.toLowerCase())?.token
      ?? workspace.currentViewToken();
    projectAcceptedState(request.source_workflow_definition_id, targetId, accepted, token, "none", true);
    bumpCopyVersion();
  }
  /** After a matched receipt, confirm the target exists now without restoring history. */
  async function confirmCopiedTarget(result: WorkflowConfigurationCopyResult) {
    const targetId = result.workflow_definition_id;
    const sourceId = result.source_workflow_definition_id.toLowerCase();
    if (entries.value[targetId]
      || workspace.workflows.some(row => row.id === targetId && row.state === "saved" && !row.deleted))
      return;
    try {
      const document = await management.queries.definition(targetId);
      if (!isGraphConfigurationDocument(document) || document.workflow_definition_id !== targetId)
        throw new WorkbenchApiError("unavailable", "目标工作流当前配置不可用");
      let accepted: GraphConfigurationDocument | null = null;
      if (configurationApplication) {
        const acceptance = configurationApplication.acceptDocument(document);
        if (acceptance.status === "conflict" || acceptance.status === "invalid")
          throw new WorkbenchApiError("unavailable", acceptance.message ?? "目标工作流当前配置无法接纳");
        accepted = configurationApplication.accepted(targetId);
      }
      accepted ??= document;
      if (!entries.value[targetId]) {
        entries.value[targetId] = { document: graphClone(accepted), saved_document: graphClone(accepted),
          saved_revision: accepted.revision, latest_published_revision: accepted.revision,
          session_id: null, pending: null };
        history.value[targetId] = { past: [], future: [] };
      }
      if (!workspace.workflows.some(row => row.id === targetId))
        workspace.registerGraphWorkflow({ id: targetId, title: accepted.name, description: "",
          nodeCount: accepted.nodes.length, state: "saved", sessionCount: 0 });
      workspace.updateWorkflowProjection(targetId, accepted.name, accepted.nodes.length);
      workspace.markWorkflowSaved(targetId, accepted.name);
      workspace.setWorkflowCurrent(targetId, accepted.revision, accepted.edit_sequence);
      copyChannel(sourceId).note("");
    } catch {
      copyChannel(sourceId).note("原复制已确认，目标现不可用");
      notices.notify("unavailable", `原复制已确认，目标 ${targetId} 现不可用`, "核实原配置复制请求");
    }
  }
  /** Formal configuration copy from the saved current; selection and editor state do not matter. */
  async function copyWorkflow(id = workspace.activeWorkflowId): Promise<string | null> {
    try {
      const entry = entries.value[id];
      if (!entry) {
        notices.notify("rejected", "工作流尚未打开，无法读取正式来源配置", "复制工作流");
        return null;
      }
      const source = configurationApplication?.accepted(id)
        ?? (isGraphConfigurationDocument(entry.saved_document) ? entry.saved_document : null)
        ?? (isGraphConfigurationDocument(entry.document) ? entry.document : null);
      if (!source || source.workflow_definition_id !== id) {
        notices.notify("rejected", "来源工作流缺少正式 current 配置，不能复制", "复制工作流");
        return null;
      }
      const targetId = crypto.randomUUID();
      const request: WorkflowConfigurationCopyRequest = {
        source_workflow_definition_id: id,
        expected_source_definition_revision: source.revision,
        expected_source_edit_sequence: source.edit_sequence,
        target_workflow_definition_id: targetId,
        target_name: workflowConfigurationCopyTargetName(source.name),
        idempotency_key: crypto.randomUUID(),
      };
      if (!isWorkflowConfigurationCopyRequest(request)) {
        notices.notify("rejected", "复制请求来源版本或目标身份无效，未提交", "复制工作流");
        return null;
      }
      const channel = copyChannel(id);
      if (!await channel.initialize()) {
        notices.notify("unavailable", channel.error.value || "配置复制原请求无法读取，未提交", "复制工作流");
        return null;
      }
      if (channel.pending.value) {
        notices.notify("unknown", "该来源已有待核实的配置复制请求，不能提交新复制", "复制工作流");
        return null;
      }
      copyOpenTokens.set(id.toLowerCase(), { targetId, token: workspace.currentViewToken() });
      bumpCopyVersion();
      const success = await channel.submit(request);
      bumpCopyVersion();
      if (!success) {
        if (channel.error.value)
          notices.notify(channel.pending.value ? "unknown" : "rejected", channel.error.value, "复制工作流");
        // The target identity is fixed only when the original request was captured.
        return channel.pending.value ? targetId : null;
      }
      return targetId;
    } catch (failure) {
      report(failure, "复制工作流");
      return null;
    } finally {
      copyOpenTokens.delete(id.toLowerCase());
      bumpCopyVersion();
    }
  }
  /** Read-only verification of a pending copy request; never resends. */
  async function reconcileCopy(workflowId: string): Promise<boolean> {
    const sourceId = workflowId.toLowerCase();
    try {
      copyInitializedSources.add(sourceId);
      const channel = copyChannel(sourceId);
      if (!await channel.initialize()) return false;
      if (!channel.pending.value) { bumpCopyVersion(); return false; }
      const done = await channel.reconcile();
      bumpCopyVersion();
      if (!done) {
        if (channel.error.value)
          notices.notify("unknown", channel.error.value, "核实原配置复制请求");
        return false;
      }
      const result = channel.lastResult.value;
      if (!result) return false;
      await confirmCopiedTarget(result);
      return true;
    } catch (failure) {
      report(failure, "核实原配置复制请求");
      return false;
    } finally { bumpCopyVersion(); }
  }
  /** Pending/error copy rows for the workflow region banner; creates the owner channel lazily. */
  function copyPendingRequests() {
    copyVersion.value;
    const activeId = workspace.activeWorkflowId;
    if (graphUuid(activeId) && entries.value[activeId] && !copyInitializedSources.has(activeId.toLowerCase()))
      void restoreCopyChannel(activeId);
    return [...copyChannels.entries()].flatMap(([sourceId, channel]) => {
      if (!channel.pending.value && !channel.busy.value && !channel.error.value) return [];
      return [{ workflowId: sourceId,
        title: workspace.workflows.find(row => row.id === sourceId)?.title ?? sourceId,
        error: channel.error.value, busy: channel.busy.value, pending: !!channel.pending.value }];
    });
  }
  /** Optional injection used by tests or a workflow-owned assembly; the store otherwise
   * lazily creates exactly one default application instance. */
  function installWorkflowConfigurationCopyApplication(application: WorkflowConfigurationCopyApplication) {
    copyApplication = application;
    copyChannels.clear();
    copyInitializedSources.clear();
    bumpCopyVersion();
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
    document.control_edges ??= [];
    return document;
  }
  function addNode(component: string, position: { x: number; y: number }) {
    const type = catalog.value.find(row => `${row.component_id}@${row.component_version}` === component);
    if (!type) return null;
    const node: GraphNode = { node_binding_id: crypto.randomUUID(), component_id: type.component_id,
      component_version: type.component_version, title: type.display_name, position: { ...position },
      config: graphClone(type.default_config), configuration_policy: defaultGraphConfigurationPolicy(type) };
    const providedTypes = type.provided_session_item_types ?? [];
    for (const access of type.object_accesses ?? []) {
      node.config[access.config_field] = access.multiple ? [] : "";
    }
    const id = editable(workspace.activeWorkflowId, doc => {
      const ports = [...type.inputs, ...type.outputs, ...Object.values(type.port_modes ?? {}).flatMap(mode => [...mode.inputs, ...mode.outputs])];
      if (ports.some(port => (port.data_schema_version ?? 1) !== 1) || type.input_storage === "references"
        || type.capabilities?.some(capability => capability.startsWith("objects:")) || providedTypes.length)
        prepareGraphEdit(doc);
      doc.nodes.push(node);
      syncProvidedSessionItems(doc, node.node_binding_id, providedTypes, false, type.object_accesses);
      syncVariableRegistrationEvent(doc, node);
      return true;
    });
    if (id) selectedNodeIds.value = [node.node_binding_id];
    return id ? node.node_binding_id : null;
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
      const copiedItemKeys = new Map<string, { object_key: string; type_id: string; schema_version: number;
        readers: string[]; writers: string[] }>();
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
            readers: sourceBinding.readers, writers: sourceBinding.writers,
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
              && (access.access !== "write" && item.readers.includes(source.node_binding_id)
                || access.access !== "read" && item.writers.includes(source.node_binding_id))
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
      doc.nodes.push(...nodes); doc.edges.push(...edges);
      for (const node of nodes) syncVariableRegistrationEvent(doc, node);
      return !!nodes.length;
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
      node.configuration_policy ??= defaultGraphConfigurationPolicy(type);
      if (node.configuration_policy.prompt_mode === "adjustable" && !graphNodeAllowsAdjustable(node, type))
        node.configuration_policy = { schema_version: 1, prompt_mode: "fixed" };
      if (type.input_storage === "references" || type.capabilities?.some(capability => capability.startsWith("objects:"))
        || [...type.inputs, ...type.outputs, ...Object.values(type.port_modes ?? {}).flatMap(mode =>
          [...mode.inputs, ...mode.outputs])].some(port => (port.data_schema_version ?? 1) !== 1)
        || (type.provided_session_item_types?.length ?? 0) > 0 || hadRegistration)
        prepareGraphEdit(doc);
      for (const access of type.object_accesses ?? []) {
        node.config[access.config_field] = access.multiple ? [] : "";
      }
      syncProvidedSessionItems(doc, id, type.provided_session_item_types ?? [], true, type.object_accesses);
      syncVariableRegistrationEvent(doc, node);
      // Changed types preserve old edges for explicit diagnosis, never guess port mappings.
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
      // Directory refresh reads only session summaries and never builds full views.
      const rows = await management.queries.sessionSummaries(definitionId, true);
      if (!current()) return;
      sessionRows.value[id] = rows;
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
    if (!alive) return;
    const ready = await ensureDefinitionEntry(id);
    if (!alive || generation !== viewGeneration || workspace.activeWorkflowId !== id) return;
    if (!ready || !entries.value[id]) return;
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
  function registrationTarget(nodeId: string) {
    return variableRegistrationTarget(active.value, session.value, nodeId);
  }
  async function submitVariableRegistration(nodeId: string) {
    const target = registrationTarget(nodeId);
    if (!target?.canSubmit || !target.event || locked.value) return false;
    return submitEvent(target.event.event_id, target.event.schema_version, {});
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
    if (!entry) return;
    // Restoring a saved draft returns to the accepted current, not to an
    // arbitrary Store value that happens to sit in saved_document.
    const accepted = configurationApplication?.accepted(id) ?? null;
    if (accepted || entry.saved_document) {
      const restored = graphClone(accepted ?? entry.saved_document!);
      entry.document = restored;
      entry.saved_document = graphClone(restored);
      entry.saved_revision = restored.revision;
      entry.state_mappings = [];
      history.value[id] = { past: [], future: [] };
      workspace.updateWorkflowProjection(id, restored.name, restored.nodes.length);
      workspace.markWorkflowSaved(id, restored.name);
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
  return { entries, catalog, frontendExtensions, packageLock, executionPackageLock, dataTypes, catalogLoading, catalogError, definitionsLoading, sessionRows, views, history, busy, receiptChecks, active, document,
    fixedOnly, session, pending, locked, controlLocked, editorLocked, primaryAction, statusLabel, selectedNodeIds, selectedEdgeId, canUndo, canRedo, setEventBindings, submitEvent, registrationTarget, submitVariableRegistration,
    isGeneric, createWorkflow, typeFor, loadCatalog, setPersistenceGuard, storeSnapshot, restoreSnapshot,
    saveWorkflow, loadDefinitions, ensureDefinitionEntry, manageWorkflow, manageSession, copyWorkflow, renameWorkflow, refreshPublishedRevision, addNode, patchNode,
    patchNodeConfiguration, patchNodeConfigurationPolicy, moveNodes, connect, removeSelection, reorderEdge, undo, duplicateSelection,
    replaceNode, setControlDependencies, setObjectBindings,
    activate, refresh, createSession, selectSession, submitPrimary, reconcile, canRetryRejectedCopy, retryRejectedCopy, discardDraft, discardRejectedCopy, closeRun, setInputs, resetPrivateState, submitAgentAction,
    management, editing, installWorkflowConfigurationApplication, configurationPendingRequests, reconcileConfiguration,
    managementPendingRequests, copyPendingRequests, reconcileCopy, installWorkflowConfigurationCopyApplication };
});
