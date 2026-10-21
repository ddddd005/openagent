import { WorkbenchApiError, workbenchRequest } from "./workbenchApi";
import { graphClone, graphJsonSignature, graphObject } from "../domain/workflowGraph";
import {
  isWorkflowConfigurationCopyRequest, isWorkflowConfigurationCopyResult,
  workflowConfigurationCopyAccepted, workflowConfigurationCopyOperation,
  type WorkflowConfigurationCopyRequest, type WorkflowConfigurationCopyResult,
} from "../domain/workflowConfigurationCopy";

/** Narrow transport for the formal configuration copy command.
 *
 * The request contains only the source identity and the two formal source
 * versions plus the fixed target identity/name. The result and the application
 * receipt's accepted projection are both validated; a copy result never enters
 * the generic session-result branches.
 */
function invalid(message: string, code = "graph_configuration_copy_invalid") {
  return new WorkbenchApiError("rejected", message, undefined, code);
}
function unknown(message: string, code = "graph_configuration_copy_receipt_invalid") {
  return new WorkbenchApiError("unknown", message, undefined, code);
}
function checkedRequest(request: WorkflowConfigurationCopyRequest): WorkflowConfigurationCopyRequest {
  if (!isWorkflowConfigurationCopyRequest(request)) throw invalid("配置复制请求字段无效，未提交");
  return graphClone(request);
}
function commandResult(value: unknown, request: WorkflowConfigurationCopyRequest):
  WorkflowConfigurationCopyResult {
  const receipt = graphObject(value) && graphObject(value.receipt) ? value.receipt : null;
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.application-command"
    || !receipt || Object.keys(receipt).length !== 9
    || receipt.schema_version !== 1 || receipt.kind !== "workflow.application-receipt"
    || receipt.operation !== workflowConfigurationCopyOperation
    || receipt.operation_scope !== "management"
    || receipt.idempotency_key !== request.idempotency_key
    || receipt.authority !== "service_receipt"
    || typeof receipt.request_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(receipt.request_sha256)
    || !graphObject(receipt.target) || Object.keys(receipt.target).length !== 0
    || !graphObject(receipt.accepted))
    throw unknown("配置复制命令回执身份无效，保留原请求");
  if (!isWorkflowConfigurationCopyResult(value.result, request))
    throw unknown("配置复制结果与来源/目标/版本不匹配，保留原请求");
  const result = graphClone(value.result);
  const accepted = workflowConfigurationCopyAccepted(result);
  if (graphJsonSignature(receipt.accepted) !== graphJsonSignature(accepted))
    throw unknown("配置复制回执接纳依据与结果不匹配，保留原请求");
  return result;
}

export async function submitConfigurationCopy(request: WorkflowConfigurationCopyRequest):
  Promise<WorkflowConfigurationCopyResult> {
  const captured = checkedRequest(request);
  const value = await workbenchRequest<unknown>("/api/graph/commands", {
    body: { operation: workflowConfigurationCopyOperation, parameters: graphClone(captured) },
  });
  return commandResult(value, captured);
}

export async function readConfigurationCopyReceipt(request: WorkflowConfigurationCopyRequest):
  Promise<WorkflowConfigurationCopyResult> {
  const captured = checkedRequest(request);
  const value = await workbenchRequest<unknown>("/api/graph/receipts/read", {
    body: { operation: workflowConfigurationCopyOperation, parameters: graphClone(captured) },
    readOnly: true,
  });
  if (!graphObject(value) || value.schema_version !== 1
    || value.kind !== "workflow.application-receipt-read")
    throw unknown("配置复制原请求回执读取契约无效，保留原请求");
  if (value.outcome === "unresolved") {
    if (Object.keys(value).length !== 4 || typeof value.reason_code !== "string"
      || !/^[A-Za-z0-9_]{1,128}$/.test(value.reason_code))
      throw unknown("配置复制原请求回执读取契约无效，保留原请求");
    throw unknown(`配置复制原请求仍待核实 [${value.reason_code}]`, value.reason_code);
  }
  if (value.outcome !== "matched" || value.reason_code !== "receipt_matched"
    || Object.keys(value).length !== 6 || !graphObject(value.receipt)
    || !Object.prototype.hasOwnProperty.call(value, "result"))
    throw unknown("配置复制原请求回执读取契约无效，保留原请求");
  return commandResult({
    schema_version: 1, kind: "workflow.application-command", receipt: value.receipt, result: value.result,
  }, captured);
}
