import { graphApplicationQuery, readGraphReceipt, sendGraphCommand } from "./workflowApplicationApi";
import { WorkbenchApiError } from "./workbenchApi";
import { graphObject, graphUuid } from "../domain/workflowGraph";
import { isCurrentModelProfile, isModelProfileIdentity, modelProfileIdentity, modelProfileType,
  sameModelProfileIdentity, type CurrentModelProfile } from "../domain/workflowModelResources";

export interface ModelProfileSaveRequest {
  record: CurrentModelProfile; expected_sequence: number; idempotency_key: string;
}
export async function listModelProfiles(): Promise<CurrentModelProfile[]> {
  const value = await graphApplicationQuery("resource.list", { type_id: modelProfileType });
  if (!Array.isArray(value) || !value.every(isCurrentModelProfile))
    throw new WorkbenchApiError("unavailable", "全局模型配置无法读取，请在模型供应商中检查");
  return value;
}
export function isModelProfileSaveRequest(value: unknown): value is ModelProfileSaveRequest {
  return graphObject(value) && Object.keys(value).length === 3 && isCurrentModelProfile(value.record)
    && graphUuid(value.idempotency_key) && value.idempotency_key === value.idempotency_key.toLowerCase()
    && Number.isSafeInteger(value.expected_sequence)
    && Number(value.expected_sequence) >= 0 && value.record.update_sequence === Number(value.expected_sequence) + 1;
}
function acceptance(value: unknown, request: ModelProfileSaveRequest) {
  if (!graphObject(value) || Object.keys(value).length !== 3 || !isModelProfileIdentity(value.reference)
    || !sameModelProfileIdentity(value.reference, modelProfileIdentity(request.record))
    || value.update_sequence !== request.record.update_sequence || value.deleted !== false)
    throw new WorkbenchApiError("unknown", "模型配置回执与原请求不匹配，保留原请求");
  return value;
}
export async function saveModelProfile(request: ModelProfileSaveRequest) {
  if (!isModelProfileSaveRequest(request)) throw new WorkbenchApiError("rejected", "模型配置字段无效");
  return acceptance(await sendGraphCommand("/api/graph/resources/save", { ...request }), request);
}
export async function readModelProfileReceipt(request: ModelProfileSaveRequest) {
  if (!isModelProfileSaveRequest(request)) throw new WorkbenchApiError("rejected", "原模型配置请求无效");
  return acceptance(await readGraphReceipt("/api/graph/resources/save", { ...request }), request);
}
