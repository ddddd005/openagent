import { graphApplicationQuery, readGraphReceipt, sendGraphCommand } from "./workflowApplicationApi";
import { WorkbenchApiError } from "./workbenchApi";
import { graphObject, graphUuid, isGraphJsonValue } from "../domain/workflowGraph";
import { isCurrentPromptResource, isPromptIdentity, promptIdentity, promptResourceType, samePromptIdentity,
  type CurrentPromptResource, type PromptIdentity } from "../domain/workflowPromptResources";

export interface PromptSaveRequest { record: CurrentPromptResource; expected_sequence: number; idempotency_key: string }
export async function listCurrentPromptResources(): Promise<CurrentPromptResource[]> {
  const value = await graphApplicationQuery("resource.list", { type_id: promptResourceType });
  if (!Array.isArray(value) || !value.every(isCurrentPromptResource))
    throw new WorkbenchApiError("unavailable", "\u63d0\u793a\u8bcd\u5f53\u524d\u8d44\u6e90\u5951\u7ea6\u65e0\u6548");
  return value;
}
export async function readCurrentPromptResource(identity: PromptIdentity): Promise<CurrentPromptResource | null> {
  if (!isPromptIdentity(identity))
    throw new WorkbenchApiError("rejected", "\u63d0\u793a\u8bcd\u5f15\u7528\u65e0\u6548");
  const value = await graphApplicationQuery("resource.read", { identity });
  if (value === null) return null;
  if (!isCurrentPromptResource(value) || !samePromptIdentity(promptIdentity(value), identity))
    throw new WorkbenchApiError("unavailable", "\u63d0\u793a\u8bcd\u5f53\u524d\u8d44\u6e90\u8eab\u4efd\u4e0d\u5339\u914d");
  return value;
}
function validatePromptSaveRequest(request: PromptSaveRequest) {
  if (!graphObject(request) || Object.keys(request).length !== 3
    || !isCurrentPromptResource(request.record) || !graphUuid(request.idempotency_key)
    || request.idempotency_key !== request.idempotency_key.toLowerCase()
    || !Number.isSafeInteger(request.expected_sequence) || request.expected_sequence < 0
    || request.record.update_sequence !== request.expected_sequence + 1 || !isGraphJsonValue(request))
    throw new WorkbenchApiError("rejected", "\u63d0\u793a\u8bcd\u5b57\u6bb5\u6216\u5f53\u524d\u5e8f\u53f7\u65e0\u6548");
}
function validatePromptSaveReceipt(value: unknown, request: PromptSaveRequest) {
  if (!graphObject(value) || Object.keys(value).length !== 3 || !isPromptIdentity(value.reference)
    || !samePromptIdentity(value.reference, promptIdentity(request.record))
    || value.update_sequence !== request.record.update_sequence || value.deleted !== false)
    throw new WorkbenchApiError("unknown", "\u63d0\u793a\u8bcd\u56de\u6267\u4e0e\u539f\u8bf7\u6c42\u4e0d\u5339\u914d\uff0c\u4fdd\u7559\u539f\u8bf7\u6c42");
  return value;
}
export async function saveCurrentPromptResource(request: PromptSaveRequest) {
  validatePromptSaveRequest(request);
  return validatePromptSaveReceipt(await sendGraphCommand("/api/graph/resources/save", { ...request }), request);
}
export async function readCurrentPromptReceipt(request: PromptSaveRequest) {
  validatePromptSaveRequest(request);
  return validatePromptSaveReceipt(await readGraphReceipt("/api/graph/resources/save", { ...request }), request);
}
