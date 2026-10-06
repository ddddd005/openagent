import { AGENT_BINDINGS, type PublicNodeState, type PublicSession } from "../adapters/workbenchApi";
import { exactSessionObject, sessionInteger, sessionUuid } from "./sessionWire";

export const OUTPUT_BINDING = "7be319b8-30bd-4674-b7bf-d1cf54a1a10a";
export type MainStage = "A" | "B" | "Output";
export interface ObservedNode extends PublicNodeState {
  workflow_session_id: string;
  chain_run_id: string | null;
  source_workflow_session_id: string | null;
  diagnostic: { code: string; category: string } | null;
  result: { availability: "available"; text: string; output_id: string; source_run_id: string }
    | { availability: "unavailable"; reason_code: string };
}
export interface WorkflowObservation {
  schema_version: 1;
  kind: "workflow_observation";
  workflow_session_id: string;
  session_revision: number;
  head_commit_id: string;
  chain_run_id: string | null;
  chain_status: string;
  diagnostic: { code: string } | null;
  nodes: ObservedNode[];
}
export const stageBinding = (stage: MainStage) => stage === "Output" ? OUTPUT_BINDING : AGENT_BINDINGS[stage];
const code = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_]{1,80}$/.test(value);
const nullableUuid = (value: unknown) => value === null || sessionUuid(value);
const statuses = ["idle", "pending", "prepared", "queued", "running", "pausing", "paused",
  "final_ready", "succeeded", "failed", "superseded", "closed", "recovery_unavailable"];

export function validObservation(value: unknown, view: PublicSession): value is WorkflowObservation {
  if (!exactSessionObject(value, ["schema_version", "kind", "workflow_session_id", "session_revision",
    "head_commit_id", "chain_run_id", "chain_status", "diagnostic", "nodes"])
    || value.schema_version !== 1 || value.kind !== "workflow_observation"
    || value.workflow_session_id !== view.workflow_session_id || value.session_revision !== view.revision
    || value.head_commit_id !== view.head_commit_id || !sessionUuid(value.head_commit_id)
    || !nullableUuid(value.chain_run_id) || !statuses.includes(value.chain_status as string)
    || !(value.diagnostic === null || exactSessionObject(value.diagnostic, ["code"]) && code(value.diagnostic.code))
    || !Array.isArray(value.nodes) || value.nodes.length !== 3) return false;
  const bindings = [AGENT_BINDINGS.A, AGENT_BINDINGS.B, OUTPUT_BINDING];
  return new Set(value.nodes.map((row) => row?.node_binding_id)).size === 3 && value.nodes.every((node) => {
    if (!exactSessionObject(node, ["node_binding_id", "label", "workflow_session_id", "chain_run_id",
      "source_workflow_session_id", "run_id", "revision", "status", "diagnostic", "result"], ["budget"])
      || !bindings.includes(node.node_binding_id as typeof bindings[number])
      || node.workflow_session_id !== view.workflow_session_id || node.chain_run_id !== value.chain_run_id
      || typeof node.label !== "string" || !statuses.includes(node.status as string)
      || !nullableUuid(node.source_workflow_session_id)
      || (node.run_id === null ? node.revision !== null || node.source_workflow_session_id !== null
        : !sessionUuid(node.run_id) || !sessionInteger(node.revision) || !sessionUuid(node.source_workflow_session_id))
      || !(node.diagnostic === null || exactSessionObject(node.diagnostic, ["code", "category"])
        && code(node.diagnostic.code) && code(node.diagnostic.category))) return false;
    if (node.budget !== undefined && (!exactSessionObject(node.budget,
      ["max_model_requests", "max_model_attempts", "model_requests", "attempts"])
      || !Object.values(node.budget).every(sessionInteger))) return false;
    const result = node.result;
    return exactSessionObject(result, ["availability", "reason_code"])
      && result.availability === "unavailable" && code(result.reason_code)
      || exactSessionObject(result, ["availability", "text", "output_id", "source_run_id"])
      && result.availability === "available" && node.status === "succeeded"
      && typeof result.text === "string" && sessionUuid(result.output_id) && sessionUuid(result.source_run_id);
  });
}

export function observedNode(view: PublicSession | null, stage: MainStage): ObservedNode | null {
  return view?.observation?.nodes.find((node) => node.node_binding_id === stageBinding(stage)) ?? null;
}
