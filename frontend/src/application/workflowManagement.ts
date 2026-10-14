import { WorkbenchApiError } from "../adapters/workbenchApi";
import {
  readGraphCandidates, readGraphCatalogDetails, readGraphDefinition, readGraphDefinitions,
  readGraphRun, readGraphSession, readGraphSessions,
} from "../adapters/workflowGraphApi";
import {
  graphClone, graphSignature, type GraphCommand, type GraphEntry, type GraphSession,
} from "../domain/workflowGraph";
import { createGraphCommand } from "./workflowCommands";
import { readGraphInformation, readGraphRegistrations } from "./workflowInformation";
import { assertEventPayload, readWorkflowEvent, readWorkflowEventBindings } from "./workflowEvents";

export interface WorkflowTarget { workflowId: string }
export interface SavedWorkflowBasis { definitionId: string; definitionRevision: number }
export type WorkflowControlAction = "pause" | "resume" | "close" | "retry_archive" | "retry_acceptance" | "retry_failed_node";
export interface WorkflowManagementPorts {
  entry(workflowId: string): GraphEntry | undefined;
  observedSession(sessionId: string): GraphSession | undefined;
  observe(session: GraphSession): GraphSession;
  dispatch(workflowId: string, request: GraphCommand): Promise<unknown>;
  commitSaved?(workflowId: string): boolean | Promise<boolean>;
  canSubmit(workflowId: string): boolean;
  commandState?(active: boolean): void;
}
export const workflowManagementQueries = {
  catalog: readGraphCatalogDetails,
  definition: readGraphDefinition,
  definitions: readGraphDefinitions,
  sessions: readGraphSessions,
  session: readGraphSession,
  candidates: readGraphCandidates,
  run: readGraphRun,
  registrations: readGraphRegistrations,
  information: readGraphInformation,
  events: readWorkflowEventBindings,
  event: readWorkflowEvent,
};

export function createWorkflowManagement(ports: WorkflowManagementPorts) {
  const controlActions = new Set<WorkflowControlAction>([
    "pause", "resume", "close", "retry_archive", "retry_acceptance", "retry_failed_node",
  ]);
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
  async function createSession(target: WorkflowTarget) {
    const entry = entryFor(target);
    if (!entry.saved_document || entry.saved_revision < 1
      || graphSignature(entry.saved_document) !== graphSignature(entry.document))
      throw new WorkbenchApiError("unavailable", "正式工作流保存暂不可用；未创建会话");
    await ports.dispatch(target.workflowId, createGraphCommand("/api/graph/sessions", {
      workflow_definition_id: entry.document.workflow_definition_id, definition_revision: entry.saved_revision,
    }, "create"));
  }
  async function createSavedSession(target: WorkflowTarget, basis: SavedWorkflowBasis) {
    const entry = entryFor(target), saved = entry.saved_document;
    if (!saved || entry.session_id || entry.saved_revision !== basis.definitionRevision
      || saved.revision !== basis.definitionRevision || saved.workflow_definition_id !== basis.definitionId
      || entry.document.workflow_definition_id !== basis.definitionId)
      throw new WorkbenchApiError("unavailable", "已保存定义或所选会话已变化，未创建会话");
    await ports.dispatch(target.workflowId, createGraphCommand("/api/graph/sessions", {
      workflow_definition_id: basis.definitionId, definition_revision: basis.definitionRevision,
    }, "create"));
  }
  async function start(target: WorkflowTarget) {
    const original = entryFor(target);
    if (!original.saved_document || original.saved_revision < 1
      || graphSignature(original.saved_document) !== graphSignature(original.document))
      throw new WorkbenchApiError("unavailable", "当前定义未正式保存；运行暂不可用，未提交请求");
    const inputs = graphClone(original.external_inputs ?? {});
    if (original.session_id) {
      const sessionId = original.session_id, definitionId = original.document.workflow_definition_id;
      if ((ports.observedSession(sessionId)?.workflow_definition_id
        ?? original.session_document?.workflow_definition_id ?? definitionId) !== definitionId)
        throw new WorkbenchApiError("unavailable", "当前历史会话属于另一工作流，请新建当前工作流会话");
      const incoming = await workflowManagementQueries.session(sessionId, definitionId);
      if (entryFor(target) !== original || original.session_id !== sessionId)
        throw new WorkbenchApiError("unavailable", "所选会话已变化，未启动");
      ports.observe(incoming);
    }
    if (!entryFor(target).session_id) await createSession(target);
    const entry = entryFor(target), view = ports.observedSession(entry.session_id!);
    if (!view || !view.can_submit) throw new WorkbenchApiError("unavailable", "当前会话尚不能启动");
    if (view.workflow_definition_id !== entry.document.workflow_definition_id
      || view.definition_revision !== entry.saved_revision)
      throw new WorkbenchApiError("unavailable", "当前会话仍使用旧修订，请新建当前修订的会话");
    await ports.dispatch(target.workflowId, createGraphCommand(`/api/graph/sessions/${view.workflow_session_id}/runs`, {
      expected_revision: view.revision, inputs,
    }, "start"));
  }
  async function control(target: WorkflowTarget, action: WorkflowControlAction) {
    if (!controlActions.has(action))
      throw new WorkbenchApiError("rejected", "此工作流操作已退役；未提交新请求");
    const entry = entryFor(target), view = entry.session_id ? ports.observedSession(entry.session_id) : null;
    if (!view) throw new WorkbenchApiError("unavailable", "尚无可操作的工作流会话");
    await ports.dispatch(target.workflowId, createGraphCommand(`/api/graph/sessions/${view.workflow_session_id}/control`, {
      action, expected_revision: view.revision,
    }, action));
  }
  async function manageDefinition(target: WorkflowTarget, action: "archive" | "delete") {
    const entry = entryFor(target);
    if (entry.saved_revision < 1)
      throw new WorkbenchApiError("unavailable", "工作流尚未保存，不能提交归档或删除");
    await ports.dispatch(target.workflowId, createGraphCommand(
      `/api/graph/definitions/${entry.document.workflow_definition_id}/${action}`,
      { expected_revision: entry.saved_revision }, `definition-${action}`));
  }
  async function manageSession(target: WorkflowTarget, sessionId: string, action: "archive" | "delete") {
    const entry = entryFor(target), definitionId = entry.document.workflow_definition_id;
    const view = ports.observe(await workflowManagementQueries.session(sessionId, definitionId));
    entryFor(target);
    const request = createGraphCommand(`/api/graph/sessions/${sessionId}/${action}`,
      { expected_revision: view.revision }, `session-${action}`);
    request.source_session_id = sessionId;
    await ports.dispatch(target.workflowId, request);
  }
  async function submitEvent(target: WorkflowTarget, eventId: string, eventVersion: number, payload: Record<string, unknown>) {
    assertEventPayload(payload);
    const fixedPayload = graphClone(payload), entry = entryFor(target);
    const view = entry.session_id ? ports.observedSession(entry.session_id) : null;
    if (!view || !view.can_submit || ["prepared", "running", "pausing", "paused"].includes(view.status)
      || view.workflow_definition_id !== entry.document.workflow_definition_id
      || entry.saved_revision !== entry.document.revision || view.definition_revision !== entry.saved_revision
      || !entry.saved_document || graphSignature(entry.saved_document) !== graphSignature(entry.document))
      throw new WorkbenchApiError("unavailable", "局部事件需要正式保存的定义和稳定会话；定义提交暂不可用");
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
  function inspect(target: WorkflowTarget) {
    const entry = ports.entry(target.workflowId);
    return entry ? graphClone({ document: entry.document, savedRevision: entry.saved_revision,
      selectedSessionId: entry.session_id, pending: entry.pending }) : null;
  }
  async function retiredSave(_target: WorkflowTarget): Promise<void> {
    throw new WorkbenchApiError("unavailable", "旧整图发布入口已退役；正式保存暂不可用，未提交新请求");
  }
  return {
    commands: {
      save: (target: WorkflowTarget) => execute(target, fixed => retiredSave(fixed)),
      createSession: (target: WorkflowTarget) => execute(target, fixed => createSession(fixed)),
      createSavedSession: (target: WorkflowTarget, basis: SavedWorkflowBasis) =>
        execute(target, fixed => createSavedSession(fixed, { ...basis })),
      start: (target: WorkflowTarget) => execute(target, fixed => start(fixed)),
      submitEvent: (target: WorkflowTarget, eventId: string, eventVersion: number, payload: Record<string, unknown>) =>
        execute(target, fixed => submitEvent(fixed, eventId, eventVersion, payload)),
      control: (target: WorkflowTarget, action: WorkflowControlAction) =>
        execute(target, fixed => control(fixed, action)),
      manageDefinition: (target: WorkflowTarget, action: "archive" | "delete") =>
        execute(target, fixed => manageDefinition(fixed, action)),
      manageSession: (target: WorkflowTarget, sessionId: string, action: "archive" | "delete") =>
        execute(target, fixed => manageSession(fixed, sessionId, action)),
    },
    queries: { ...workflowManagementQueries, inspect },
  };
}
