import { workbenchRequest, WorkbenchApiError } from "./workbenchApi";
import { graphClone, graphObject, graphUuid } from "../domain/workflowGraph";

export interface SessionMetadata {
  schema_version: 1; kind: "workflow.session-metadata";
  workflow_definition_id: string; workflow_session_id: string;
  name: string; metadata_sequence: number;
}
export interface SessionMetadataSaveRequest {
  session_id: string; name: string; expected_metadata_sequence: number; idempotency_key: string;
}
export function validSessionName(value: unknown): value is string {
  return typeof value === "string" && [...value].length <= 128
    && !/[\u0000-\u001f\u007f]/.test(value)
    && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);
}
export function isSessionMetadata(value: unknown): value is SessionMetadata {
  return graphObject(value) && Object.keys(value).length === 6
    && value.schema_version === 1 && value.kind === "workflow.session-metadata"
    && graphUuid(value.workflow_definition_id) && graphUuid(value.workflow_session_id)
    && validSessionName(value.name) && value.name === value.name.trim()
    && Number.isSafeInteger(value.metadata_sequence) && Number(value.metadata_sequence) >= 0;
}
export function isSessionMetadataSaveRequest(value: unknown): value is SessionMetadataSaveRequest {
  return graphObject(value) && Object.keys(value).length === 4
    && graphUuid(value.session_id) && validSessionName(value.name)
    && Number.isSafeInteger(value.expected_metadata_sequence) && Number(value.expected_metadata_sequence) >= 0
    && Number(value.expected_metadata_sequence) < Number.MAX_SAFE_INTEGER
    && graphUuid(value.idempotency_key) && value.idempotency_key === value.idempotency_key.toLowerCase();
}
export function validateSessionMetadataResult(value: unknown, request: SessionMetadataSaveRequest): SessionMetadata {
  if (!isSessionMetadata(value) || value.workflow_session_id !== request.session_id
    || value.name !== request.name.trim() || value.metadata_sequence !== request.expected_metadata_sequence + 1)
    throw new WorkbenchApiError("unknown", "会话名称回执与原请求不匹配，保留原请求");
  return value;
}
function commandResult(value: unknown, request: SessionMetadataSaveRequest) {
  const receipt = graphObject(value) && graphObject(value.receipt) ? value.receipt : null;
  if (!graphObject(value) || Object.keys(value).length !== 4
    || value.schema_version !== 1 || value.kind !== "workflow.application-command"
    || !receipt || Object.keys(receipt).length !== 9 || receipt.schema_version !== 1
    || receipt.kind !== "workflow.application-receipt" || receipt.operation !== "session.metadata.update"
    || receipt.operation_scope !== "management" || receipt.authority !== "service_receipt"
    || receipt.idempotency_key !== request.idempotency_key
    || typeof receipt.request_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(receipt.request_sha256)
    || !graphObject(receipt.target) || Object.keys(receipt.target).length !== 1
    || receipt.target.session_id !== request.session_id || !graphObject(receipt.accepted))
    throw new WorkbenchApiError("unknown", "会话名称命令回执身份无效，保留原请求");
  const result = validateSessionMetadataResult(value.result, request);
  const accepted = receipt.accepted;
  if (Object.keys(accepted).length !== 4 || accepted.workflow_definition_id !== result.workflow_definition_id
    || accepted.workflow_session_id !== result.workflow_session_id || accepted.name !== result.name
    || accepted.metadata_sequence !== result.metadata_sequence)
    throw new WorkbenchApiError("unknown", "会话名称接纳依据与结果不匹配，保留原请求");
  return result;
}
export async function readSessionMetadata(sessionId: string): Promise<SessionMetadata> {
  if (!graphUuid(sessionId)) throw new WorkbenchApiError("rejected", "会话名称目标无效");
  const value = await workbenchRequest<unknown>("/api/graph/queries", {
    body: { operation: "session.metadata.read", parameters: { session_id: sessionId } }, readOnly: true });
  if (!isSessionMetadata(value) || value.workflow_session_id !== sessionId)
    throw new WorkbenchApiError("unavailable", "会话名称读取身份或字段无效");
  return value;
}
export async function saveSessionMetadata(request: SessionMetadataSaveRequest): Promise<SessionMetadata> {
  if (!isSessionMetadataSaveRequest(request)) throw new WorkbenchApiError("rejected", "会话名称请求无效");
  return commandResult(await workbenchRequest("/api/graph/commands", {
    body: { operation: "session.metadata.update", parameters: graphClone(request) } }), request);
}
export async function readSessionMetadataReceipt(request: SessionMetadataSaveRequest): Promise<SessionMetadata> {
  if (!isSessionMetadataSaveRequest(request)) throw new WorkbenchApiError("rejected", "原会话名称请求无效");
  const value = await workbenchRequest<unknown>("/api/graph/receipts/read", {
    body: { operation: "session.metadata.update", parameters: graphClone(request) }, readOnly: true });
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.application-receipt-read")
    throw new WorkbenchApiError("unknown", "会话名称原请求回执格式无效，保留原请求");
  if (value.outcome === "unresolved" && Object.keys(value).length === 4
    && typeof value.reason_code === "string" && /^[A-Za-z0-9_]{1,128}$/.test(value.reason_code))
    throw new WorkbenchApiError("unknown", `原会话名称请求仍待核实 [${value.reason_code}]`, undefined, value.reason_code);
  if (value.outcome !== "matched" || value.reason_code !== "receipt_matched" || Object.keys(value).length !== 6)
    throw new WorkbenchApiError("unknown", "会话名称原请求回执格式无效，保留原请求");
  return commandResult({ schema_version: 1, kind: "workflow.application-command",
    receipt: value.receipt, result: value.result }, request);
}
