import { WorkbenchApiError, workbenchRequest } from "./workbenchApi";
import { readGraphReceipt, sendGraphCommand } from "./workflowApplicationApi";
import { graphClone, graphObject } from "../domain/workflowGraph";
import { isGraphConfigurationResult, isGraphConfigurationUpdateRequest,
  type GraphConfigurationResult, type GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";

const path = "/api/graph/definitions/update";
async function submit(request: GraphConfigurationUpdateRequest, readOnly: boolean): Promise<GraphConfigurationResult> {
  if (!isGraphConfigurationUpdateRequest(request))
    throw new WorkbenchApiError("rejected", "配置提交依据或文档无效", undefined, "graph_invalid_document");
  const captured = graphClone(request);
  const result = await (readOnly ? readGraphReceipt : sendGraphCommand)(path, { ...graphClone(captured) });
  if (!isGraphConfigurationResult(result, captured))
    throw new WorkbenchApiError("unknown", "配置回执与原提交依据不匹配，保留原请求", undefined,
      "graph_configuration_receipt_invalid");
  return graphClone(result);
}
export const updateGraphConfiguration = (request: GraphConfigurationUpdateRequest) => submit(request, false);
export const readGraphConfigurationReceipt = (request: GraphConfigurationUpdateRequest) => submit(request, true);

/** Ask the service to arbitrate an unresolved original request (S1-01b). */
export interface GraphRequestAbandonRequest {
  workflow_definition_id: string;
  target_operation: "definition.update";
  target_idempotency_key: string;
  target_request_digest: string;
  idempotency_key: string;
}
export interface GraphRequestArbitrationResult {
  schema_version: 1;
  kind: "workflow.request-arbitration";
  outcome: "committed" | "abandoned";
  operation: string;
  idempotency_key: string;
  request_digest: string;
  workflow_definition_id: string;
  evidence_resolution: "verified" | "archived";
  result: GraphConfigurationResult | null;
}
export function isGraphRequestArbitrationResult(value: unknown, original: GraphConfigurationUpdateRequest,
  targetKey: string): value is GraphRequestArbitrationResult {
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.request-arbitration"
    || !["committed", "abandoned"].includes(String(value.outcome))
    || value.workflow_definition_id !== original.workflow_definition_id
    || value.operation !== "definition.update" || typeof value.request_digest !== "string"
    || typeof value.idempotency_key !== "string" || value.idempotency_key !== targetKey) return false;
  // R7: a committed business result may arrive with evidence already archived earlier.
  if (value.outcome === "committed")
    return ["verified", "archived"].includes(String(value.evidence_resolution))
      && isGraphConfigurationResult(value.result, original);
  return value.evidence_resolution === "archived" && value.result === null;
}
export async function abandonGraphConfiguration(request: GraphRequestAbandonRequest,
  original: GraphConfigurationUpdateRequest): Promise<GraphRequestArbitrationResult> {
  const captured = graphClone(request);
  const envelope = await workbenchRequest<unknown>("/api/graph/commands", {
    body: { operation: "request.abandon", parameters: captured },
  });
  // The arbitration command has no application-origin receipt; accept either a direct
  // result object or the command envelope carrying it.
  const direct = isGraphRequestArbitrationResult(envelope, original, captured.target_idempotency_key)
    ? envelope : null;
  const result = direct ?? (graphObject(envelope) && graphObject(envelope.result) ? envelope.result : null);
  const receipt = graphObject(envelope) && graphObject(envelope.receipt) ? envelope.receipt : null;
  if (!graphObject(envelope) || (direct === null && (envelope.schema_version !== 1
      || envelope.kind !== "workflow.application-command"))
    || (receipt !== null && (receipt.schema_version !== 1 || receipt.kind !== "workflow.application-receipt"
      || receipt.operation !== "request.abandon" || receipt.idempotency_key !== captured.idempotency_key))
    || !isGraphRequestArbitrationResult(result, original, captured.target_idempotency_key))
    throw new WorkbenchApiError("unknown", "原请求仲裁回执无效，保持结果未决", undefined,
      "graph_request_arbitration_invalid");
  return graphClone(result);
}
