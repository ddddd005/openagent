import { graphApplicationQuery } from "../adapters/workflowApplicationApi";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphObject, graphUuid, isGraphEventBindings, isGraphJsonValue, type GraphEventBinding } from "../domain/workflowGraph";

export interface WorkflowEventTarget { sessionId: string; definitionId: string; definitionRevision: number }
export interface WorkflowEventBindings {
  schema_version: 1;
  kind: "workflow.event-bindings";
  workflow_definition_id: string;
  definition_revision: number;
  workflow_session_id: string;
  session_revision: number;
  can_submit: boolean;
  bindings: GraphEventBinding[];
}
export async function readWorkflowEventBindings(target: WorkflowEventTarget): Promise<WorkflowEventBindings> {
  const value = await graphApplicationQuery("event.bindings", { session_id: target.sessionId,
    workflow_definition_id: target.definitionId, definition_revision: target.definitionRevision });
  if (!graphObject(value) || Object.keys(value).length !== 8 || value.schema_version !== 1
    || value.kind !== "workflow.event-bindings" || value.workflow_definition_id !== target.definitionId
    || value.definition_revision !== target.definitionRevision || value.workflow_session_id !== target.sessionId
    || !Number.isSafeInteger(value.session_revision) || Number(value.session_revision) < 1
    || typeof value.can_submit !== "boolean" || !isGraphEventBindings(value.bindings))
    throw new WorkbenchApiError("unavailable", "事件目录身份、修订或契约无效");
  return value as unknown as WorkflowEventBindings;
}
export function assertEventPayload(payload: unknown): asserts payload is Record<string, unknown> {
  if (!graphObject(payload) || !isGraphJsonValue(payload) || Object.hasOwn(payload, "_workflow_frozen_resources"))
    throw new WorkbenchApiError("rejected", "事件载荷须为 JSON 对象，不能包含保留的资源字段");
}
export interface WorkflowEventRun {
  schema_version: 1;
  kind: "workflow.event-run";
  workflow_definition_id: string;
  definition_revision: number;
  workflow_session_id: string;
  session_revision: number;
  source: { workflow_definition_id: string; definition_revision: number; workflow_session_id: string };
  chain_run_id: string;
  execution_kind: "event";
  event: { event_id: string; schema_version: number; audience: "management" | "consumer"; idempotency_key: string };
  status: string;
  revision: number;
  targets: string[];
  ordered_nodes: string[];
  completed_nodes: string[];
  next_node_index: number;
  diagnostic: { reason_code: string; message: "Event execution requires attention" } | null;
}
export async function readWorkflowEvent(target: WorkflowEventTarget, chainId: string): Promise<WorkflowEventRun> {
  const value = await graphApplicationQuery("event.read", { session_id: target.sessionId, chain_id: chainId });
  const orderedNodes = graphObject(value) && Array.isArray(value.ordered_nodes) ? value.ordered_nodes : [];
  const exact = (row: unknown, keys: string[]): row is Record<string, unknown> =>
    graphObject(row) && Object.keys(row).length === keys.length && keys.every(key => Object.hasOwn(row, key));
  const positive = (row: unknown) => Number.isSafeInteger(row) && Number(row) > 0;
  const bounded = (row: unknown): row is string => typeof row === "string" && !!row && row.length <= 128 && row.trim() === row;
  const ids = (row: unknown): row is string[] => Array.isArray(row) && row.length <= 512
    && row.every(graphUuid) && new Set(row).size === row.length;
  if (!exact(value, ["schema_version", "kind", "workflow_definition_id", "definition_revision", "workflow_session_id",
    "session_revision", "source", "chain_run_id", "execution_kind", "event", "status", "revision", "targets",
    "ordered_nodes", "completed_nodes", "next_node_index", "diagnostic"]) || value.schema_version !== 1
    || value.kind !== "workflow.event-run" || value.workflow_definition_id !== target.definitionId
    || value.definition_revision !== target.definitionRevision || value.workflow_session_id !== target.sessionId
    || !positive(value.session_revision) || value.chain_run_id !== chainId || !graphUuid(chainId)
    || value.execution_kind !== "event" || typeof value.status !== "string" || !positive(value.revision)
    || !exact(value.source, ["workflow_definition_id", "definition_revision", "workflow_session_id"])
    || !graphUuid(value.source.workflow_definition_id) || !graphUuid(value.source.workflow_session_id) || !positive(value.source.definition_revision)
    || !exact(value.event, ["event_id", "schema_version", "audience", "idempotency_key"])
    || !bounded(value.event.event_id) || !positive(value.event.schema_version)
    || !["management", "consumer"].includes(String(value.event.audience)) || !bounded(value.event.idempotency_key)
    || !ids(value.targets) || !value.targets.length || !ids(value.ordered_nodes) || !ids(value.completed_nodes)
    || !Number.isSafeInteger(value.next_node_index) || Number(value.next_node_index) < 0 || Number(value.next_node_index) > value.ordered_nodes.length
    || !value.targets.every(id => orderedNodes.includes(id)) || !value.completed_nodes.every(id => orderedNodes.includes(id))
    || value.diagnostic !== null && (!exact(value.diagnostic, ["reason_code", "message"])
      || !bounded(value.diagnostic.reason_code) || value.diagnostic.message !== "Event execution requires attention"))
    throw new WorkbenchApiError("unavailable", "事件运行概况身份、来源或契约无效");
  return value as unknown as WorkflowEventRun;
}
