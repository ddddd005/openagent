import { WorkbenchApiError } from "../adapters/workbenchApi";
import {
  readGraphArchive, readGraphCandidates, readGraphCatalogDetails, readGraphDefinition,
  readGraphRun, readGraphSession, readGraphSessions,
} from "../adapters/workflowGraphApi";
import {
  graphClone, graphSignature, type GraphCandidates, type GraphCommand, type GraphEntry, type GraphSession,
} from "../domain/workflowGraph";
import { createGraphCommand } from "./workflowCommands";
import { readGraphInformation, readGraphRegistrations } from "./workflowInformation";
import { assertEventPayload, readWorkflowEvent, readWorkflowEventBindings } from "./workflowEvents";

export interface WorkflowTarget { workflowId: string }
export type WorkflowControlAction = "pause" | "resume" | "close" | "extend_budget" | "retry_archive" | "retry_acceptance" | "retry_failed_node";
export interface WorkflowControlParameters { add_model_requests?: number; add_model_attempts?: number }
export interface WorkflowManagementPorts {
  entry(workflowId: string): GraphEntry | undefined;
  observedSession(sessionId: string): GraphSession | undefined;
  observe(session: GraphSession): GraphSession;
  dispatch(workflowId: string, request: GraphCommand): Promise<unknown>;
  commitSaved(workflowId: string): boolean;
  canSubmit(workflowId: string): boolean;
  commandState?(active: boolean): void;
}
export const workflowManagementQueries = {
  catalog: readGraphCatalogDetails,
  definition: readGraphDefinition,
  sessions: readGraphSessions,
  session: readGraphSession,
  candidates: readGraphCandidates,
  run: readGraphRun,
  archive: readGraphArchive,
  registrations: readGraphRegistrations,
  information: readGraphInformation,
  events: readWorkflowEventBindings,
  event: readWorkflowEvent,
};

export function createWorkflowManagement(ports: WorkflowManagementPorts) {
  let commandActive = false;
  async function execute<T>(target: WorkflowTarget, change: (fixed: WorkflowTarget) => Promise<T>) {
    if (commandActive) throw new WorkbenchApiError("unavailable", "工作流操作正在提交");
    const fixed = { workflowId: target.workflowId };
    entryFor(fixed);
    if (!ports.canSubmit(fixed.workflowId))
      throw new WorkbenchApiError("unavailable", "工作流副本尚未完成，不能提交新操作");
    commandActive = true;
    ports.commandState?.(true);
    try { return await change(fixed); }
    finally { commandActive = false; ports.commandState?.(false); }
  }
  function entryFor({ workflowId }: WorkflowTarget) {
    const entry = ports.entry(workflowId);
    if (!entry) throw new WorkbenchApiError("unavailable", "工作流不存在");
    if (entry.pending) throw new WorkbenchApiError("unknown", "原请求尚待核实，不能提交新操作");
    return entry;
  }
  async function publish(target: WorkflowTarget) {
    const entry = entryFor(target);
    if (entry.saved_document && graphSignature(entry.saved_document) === graphSignature(entry.document)) return;
    const document = graphClone(entry.document);
    document.revision = entry.saved_revision + 1;
    await ports.dispatch(target.workflowId, createGraphCommand("/api/graph/definitions", {
      document, expected_revision: entry.saved_revision,
    }, "save"));
  }
  async function bind(target: WorkflowTarget) {
    const entry = entryFor(target);
    if (!entry.session_id) return;
    const sessionId = entry.session_id, definitionId = entry.document.workflow_definition_id;
    const savedRevision = entry.saved_revision;
    const incoming = await workflowManagementQueries.session(sessionId, definitionId);
    if (ports.entry(target.workflowId) !== entry || entry.session_id !== sessionId
      || entry.document.workflow_definition_id !== definitionId || entry.saved_revision !== savedRevision)
      throw new WorkbenchApiError("unavailable", "定义或会话依据已变化，未提交重绑");
    const view = ports.observe(incoming);
    if (view.definition_revision === savedRevision) return;
    await ports.dispatch(target.workflowId, createGraphCommand(`/api/graph/sessions/${sessionId}/rebind`, {
      definition_revision: savedRevision, expected_revision: view.revision,
      expected_data_revision: view.data_revision, expected_head_revision: view.head_revision,
      mappings: entry.state_mappings ?? [],
    }, "rebind"));
  }
  async function publishAndBind(target: WorkflowTarget) {
    await publish(target);
    await bind(target);
  }
  async function save(target: WorkflowTarget) {
    await publishAndBind(target);
    if (!ports.commitSaved(target.workflowId))
      throw new WorkbenchApiError("unavailable", "工作流已保存，当前本机目录状态尚未保存");
  }
  async function createSession(target: WorkflowTarget) {
    await publish(target);
    const entry = entryFor(target);
    await ports.dispatch(target.workflowId, createGraphCommand("/api/graph/sessions", {
      workflow_definition_id: entry.document.workflow_definition_id, definition_revision: entry.saved_revision,
    }, "create"));
  }
  async function start(target: WorkflowTarget) {
    const inputs = graphClone(entryFor(target).external_inputs ?? {});
    await publishAndBind(target);
    if (!entryFor(target).session_id) await createSession(target);
    const entry = entryFor(target), view = ports.observedSession(entry.session_id!);
    if (!view || !view.can_submit) throw new WorkbenchApiError("unavailable", "当前会话尚不能启动");
    await ports.dispatch(target.workflowId, createGraphCommand(`/api/graph/sessions/${view.workflow_session_id}/runs`, {
      expected_revision: view.revision, inputs,
    }, "start"));
  }
  async function control(target: WorkflowTarget, action: WorkflowControlAction, parameters: WorkflowControlParameters = {}) {
    const entry = entryFor(target), view = entry.session_id ? ports.observedSession(entry.session_id) : null;
    if (!view) throw new WorkbenchApiError("unavailable", "尚无可操作的工作流会话");
    await ports.dispatch(target.workflowId, createGraphCommand(`/api/graph/sessions/${view.workflow_session_id}/control`, {
      ...parameters, action, expected_revision: view.revision,
    }, action));
  }
  async function submitEvent(target: WorkflowTarget, eventId: string, eventVersion: number, payload: Record<string, unknown>) {
    assertEventPayload(payload);
    const fixedPayload = graphClone(payload), entry = entryFor(target);
    const view = entry.session_id ? ports.observedSession(entry.session_id) : null;
    if (!view || !view.can_submit || ["prepared", "running", "pausing", "paused"].includes(view.status)
      || entry.saved_revision !== entry.document.revision || view.definition_revision !== entry.saved_revision
      || !entry.saved_document || graphSignature(entry.saved_document) !== graphSignature(entry.document))
      throw new WorkbenchApiError("unavailable", "局部事件需要已保存定义和已选择的稳定会话；请先保存并确认会话");
    const sessionId = view.workflow_session_id, revision = view.revision, definitionId = entry.document.workflow_definition_id;
    const bindings = await workflowManagementQueries.events({ sessionId, definitionId, definitionRevision: view.definition_revision });
    if (ports.entry(target.workflowId) !== entry || entry.session_id !== sessionId
      || entry.document.workflow_definition_id !== definitionId || entry.document.revision !== view.definition_revision
      || ports.observedSession(sessionId)?.revision !== revision || bindings.session_revision !== revision)
      throw new WorkbenchApiError("unavailable", "事件提交依据已变化，请刷新事件目录");
    if (!bindings.can_submit || !bindings.bindings.some(row => row.event_id === eventId && row.schema_version === eventVersion))
      throw new WorkbenchApiError("unavailable", "此版本事件未登记或当前不可提交");
    await ports.dispatch(target.workflowId, createGraphCommand(`/api/graph/sessions/${sessionId}/events/submit`, {
      workflow_definition_id: definitionId, definition_revision: view.definition_revision,
      event_id: eventId, event_schema_version: eventVersion, payload: fixedPayload, expected_revision: revision,
    }, "event"));
  }
  async function restoreCandidate(target: WorkflowTarget, snapshot: GraphCandidates, candidateId: string, fork = false) {
    const entry = entryFor(target), view = entry.session_id ? ports.observedSession(entry.session_id) : null;
    const candidate = snapshot.candidates.find(row => row.candidate_id === candidateId);
    if (!view?.can_submit || !candidate?.can_select)
      throw new WorkbenchApiError("unavailable", "当前候选不可恢复");
    if (snapshot.workflow_session_id !== view.workflow_session_id
      || snapshot.workflow_definition_id !== view.workflow_definition_id
      || snapshot.session_revision !== view.revision || snapshot.head_revision !== view.head_revision
      || snapshot.definition_revision !== view.definition_revision)
      throw new WorkbenchApiError("rejected", "候选依据已变化，请刷新后再操作 [stale_revision]");
    const action = fork ? "candidate-fork" : "candidate-select";
    const request = createGraphCommand(`/api/graph/sessions/${view.workflow_session_id}/candidates/${fork ? "fork" : "select"}`, {
      candidate_id: candidateId, expected_revision: view.revision, expected_data_revision: view.data_revision,
      expected_head_revision: view.head_revision,
    }, action);
    request.source_session_id = view.workflow_session_id;
    request.expected_chain_run_id = candidate.chain_run_id;
    request.expected_definition_id = candidate.source_workflow_definition_id;
    request.expected_definition_revision = candidate.source_definition_revision;
    await ports.dispatch(target.workflowId, request);
  }
  function inspect(target: WorkflowTarget) {
    const entry = ports.entry(target.workflowId);
    return entry ? graphClone({ document: entry.document, savedRevision: entry.saved_revision,
      selectedSessionId: entry.session_id, pending: entry.pending }) : null;
  }
  return {
    commands: {
      save: (target: WorkflowTarget) => execute(target, fixed => save(fixed)),
      createSession: (target: WorkflowTarget) => execute(target, fixed => createSession(fixed)),
      start: (target: WorkflowTarget) => execute(target, fixed => start(fixed)),
      submitEvent: (target: WorkflowTarget, eventId: string, eventVersion: number, payload: Record<string, unknown>) =>
        execute(target, fixed => submitEvent(fixed, eventId, eventVersion, payload)),
      control: (target: WorkflowTarget, action: WorkflowControlAction, parameters?: WorkflowControlParameters) =>
        execute(target, fixed => control(fixed, action, parameters)),
      restoreCandidate: (target: WorkflowTarget, snapshot: GraphCandidates, candidateId: string, fork = false) =>
        execute(target, fixed => restoreCandidate(fixed, snapshot, candidateId, fork)),
    },
    queries: { ...workflowManagementQueries, inspect },
  };
}
