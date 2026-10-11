import { WorkbenchApiError } from "./workbenchApi";
import { graphApplicationQuery, readGraphReceipt, sendGraphCommand } from "./workflowApplicationApi";
import { graphObject, graphUuid, isGraphJsonValue } from "../domain/workflowGraph";
import { isPromptIdentity, samePromptIdentity, type PromptIdentity } from "../domain/workflowPromptResources";

export interface ContentMetadata {
  reference: PromptIdentity; display_name: string; metadata_sequence: number;
}
export interface ContentMetadataSaveRequest {
  reference: PromptIdentity; display_name: string; expected_metadata_sequence: number; idempotency_key: string;
}
export function validContentDisplayName(value: unknown): value is string {
  return typeof value === "string" && [...value].length <= 128
    && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);
}
export function isContentMetadata(value: unknown): value is ContentMetadata {
  return graphObject(value) && isGraphJsonValue(value) && Object.keys(value).length === 3
    && isPromptIdentity(value.reference) && validContentDisplayName(value.display_name)
    && value.display_name.trim() === value.display_name
    && Number.isSafeInteger(value.metadata_sequence) && Number(value.metadata_sequence) >= 0;
}
export function isContentMetadataSaveRequest(value: unknown): value is ContentMetadataSaveRequest {
  return graphObject(value) && isGraphJsonValue(value) && Object.keys(value).length === 4
    && isPromptIdentity(value.reference) && validContentDisplayName(value.display_name)
    && value.display_name.trim() === value.display_name
    && Number.isSafeInteger(value.expected_metadata_sequence) && Number(value.expected_metadata_sequence) >= 0
    && Number(value.expected_metadata_sequence) < Number.MAX_SAFE_INTEGER
    && graphUuid(value.idempotency_key) && value.idempotency_key === value.idempotency_key.toLowerCase();
}
export async function readContentMetadata(reference: PromptIdentity): Promise<ContentMetadata> {
  if (!isPromptIdentity(reference)) throw new WorkbenchApiError("rejected", "内容名称引用无效");
  const value = await graphApplicationQuery("content.metadata.read", { reference });
  if (!isContentMetadata(value) || !samePromptIdentity(value.reference, reference))
    throw new WorkbenchApiError("unavailable", "内容名称读取身份或字段无效");
  return value;
}
export function validateContentMetadataReceipt(value: unknown, request: ContentMetadataSaveRequest): ContentMetadata {
  if (!isContentMetadata(value) || !samePromptIdentity(value.reference, request.reference)
    || value.display_name !== request.display_name || value.metadata_sequence !== request.expected_metadata_sequence + 1)
    throw new WorkbenchApiError("unknown", "内容名称回执与原请求不匹配，保留原请求");
  return value;
}
export async function saveContentMetadata(request: ContentMetadataSaveRequest): Promise<ContentMetadata> {
  if (!isContentMetadataSaveRequest(request)) throw new WorkbenchApiError("rejected", "内容名称请求无效");
  return validateContentMetadataReceipt(await sendGraphCommand("/api/graph/content/metadata", { ...request }), request);
}
export async function readContentMetadataReceipt(request: ContentMetadataSaveRequest): Promise<ContentMetadata> {
  if (!isContentMetadataSaveRequest(request)) throw new WorkbenchApiError("rejected", "内容名称原请求无效");
  return validateContentMetadataReceipt(await readGraphReceipt("/api/graph/content/metadata", { ...request }), request);
}
