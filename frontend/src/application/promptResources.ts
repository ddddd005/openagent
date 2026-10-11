import { hasInjectionContext, inject, type InjectionKey } from "vue";
import { listCurrentPromptResources, readCurrentPromptReceipt, saveCurrentPromptResource,
  type PromptSaveRequest } from "../adapters/promptResourcesApi";
import { isCurrentPromptResource, type CurrentPromptResource } from "../domain/workflowPromptResources";
import { graphObject, graphUuid } from "../domain/workflowGraph";
import { createResourcePendingStorage } from "../adapters/resourcePendingStorage";
import { createContentResourceManagement, type ContentResourceManagementPorts } from "./contentResourceManagement";

export interface PromptResourcesPorts extends ContentResourceManagementPorts<CurrentPromptResource> {}
function validRequest(value: unknown): value is PromptSaveRequest {
  return graphObject(value) && Object.keys(value).length === 3 && isCurrentPromptResource(value.record)
    && graphUuid(value.idempotency_key) && value.idempotency_key === value.idempotency_key.toLowerCase()
    && Number.isSafeInteger(value.expected_sequence) && Number(value.expected_sequence) >= 0
    && value.record.update_sequence === Number(value.expected_sequence) + 1;
}
export function createPromptResources(ports: PromptResourcesPorts) {
  return createContentResourceManagement(ports, {
    isResource: isCurrentPromptResource, isRequest: validRequest, label: "提示词资源",
    invalidSaveMessage: "提示词字段或当前序号无效",
  });
}
let installed: ReturnType<typeof createPromptResources> | undefined;
export const promptResourceControllerKey: InjectionKey<ReturnType<typeof createPromptResources>> =
  Symbol("workflow.prompts.current-resources");
export function usePromptResources() {
  const provided = hasInjectionContext() ? inject(promptResourceControllerKey, null) : null;
  if (provided) return provided;
  return installed ??= createPromptResources({
    list: listCurrentPromptResources, save: saveCurrentPromptResource, readReceipt: readCurrentPromptReceipt,
    ...createResourcePendingStorage("prompts:resource-pending:v1", "workflow.prompts.resource.pending.v1"),
  });
}
