import { graphApplicationQuery, readGraphReceipt, sendGraphCommand } from "../../adapters/workflowApplicationApi";
import { WorkbenchApiError } from "../../adapters/workbenchApi";
import { graphObject, graphUuid, isGraphJsonValue } from "../../domain/workflowGraph";
import { isCurrentLorebookResource, isLorebookIdentity, lorebookIdentity, lorebookIdentityKey,
  lorebookResourceType, type CurrentLorebookResource, type LorebookIdentity } from "./lorebookResources";

export interface LorebookSaveRequest { record: CurrentLorebookResource; expected_sequence: number; idempotency_key: string }
export function isLorebookSaveRequest(value: unknown): value is LorebookSaveRequest {
  return graphObject(value) && isGraphJsonValue(value) && Object.keys(value).length === 3 && isCurrentLorebookResource(value.record)
    && graphUuid(value.idempotency_key) && value.idempotency_key === value.idempotency_key.toLowerCase()
    && Number.isSafeInteger(value.expected_sequence) && Number(value.expected_sequence) >= 0
    && value.record.update_sequence === Number(value.expected_sequence) + 1;
}
export async function listCurrentLorebooks(): Promise<CurrentLorebookResource[]> {
  const value = await graphApplicationQuery("resource.list", { type_id: lorebookResourceType });
  if (!Array.isArray(value) || !value.every(isCurrentLorebookResource))
    throw new WorkbenchApiError("unavailable", "Lorebook resource contract is invalid");
  return value;
}
export async function readCurrentLorebook(identity: LorebookIdentity): Promise<CurrentLorebookResource | null> {
  if (!isLorebookIdentity(identity)) throw new WorkbenchApiError("rejected", "Lorebook reference is invalid");
  const value = await graphApplicationQuery("resource.read", { identity });
  if (value === null) return null;
  if (!isCurrentLorebookResource(value) || lorebookIdentityKey(lorebookIdentity(value)) !== lorebookIdentityKey(identity))
    throw new WorkbenchApiError("unavailable", "Lorebook resource identity does not match");
  return value;
}
function validateReceipt(value: unknown, request: LorebookSaveRequest) {
  if (!graphObject(value) || Object.keys(value).length !== 3 || !isLorebookIdentity(value.reference)
    || lorebookIdentityKey(value.reference) !== lorebookIdentityKey(lorebookIdentity(request.record))
    || value.update_sequence !== request.record.update_sequence || value.deleted !== false)
    throw new WorkbenchApiError("unknown", "Lorebook receipt does not match; retain the original request");
  return value;
}
export async function saveCurrentLorebook(request: LorebookSaveRequest) {
  if (!isLorebookSaveRequest(request)) throw new WorkbenchApiError("rejected", "Lorebook save request is invalid");
  return validateReceipt(await sendGraphCommand("/api/graph/resources/save", { ...request }), request);
}
export async function readCurrentLorebookReceipt(request: LorebookSaveRequest) {
  if (!isLorebookSaveRequest(request)) throw new WorkbenchApiError("rejected", "Lorebook save request is invalid");
  return validateReceipt(await readGraphReceipt("/api/graph/resources/save", { ...request }), request);
}
