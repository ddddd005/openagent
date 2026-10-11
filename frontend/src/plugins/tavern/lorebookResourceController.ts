import { hasInjectionContext, inject, type InjectionKey } from "vue";
import { isCurrentLorebookResource, type CurrentLorebookResource } from "./lorebookResources";
import { isLorebookSaveRequest, listCurrentLorebooks, readCurrentLorebookReceipt, saveCurrentLorebook } from "./lorebookResourcesApi";
import { createResourcePendingStorage } from "../../adapters/resourcePendingStorage";
import { createContentResourceManagement, type ContentResourceManagementPorts } from "../../application/contentResourceManagement";

export interface LorebookResourcesPorts extends ContentResourceManagementPorts<CurrentLorebookResource> {}
export function createLorebookResources(ports: LorebookResourcesPorts) {
  return createContentResourceManagement(ports, {
    isResource: isCurrentLorebookResource, isRequest: isLorebookSaveRequest, label: "Lorebook 资源",
    invalidSaveMessage: "Lorebook 字段或当前序号无效",
  });
}
export const lorebookResourceControllerKey: InjectionKey<ReturnType<typeof createLorebookResources>> =
  Symbol("workflow.tavern.lorebook-resources");
let installed: ReturnType<typeof createLorebookResources> | undefined;
export function useLorebookResources() {
  const provided = hasInjectionContext() ? inject(lorebookResourceControllerKey, null) : null;
  if (provided) return provided;
  return installed ??= createLorebookResources({
    list: listCurrentLorebooks, save: saveCurrentLorebook, readReceipt: readCurrentLorebookReceipt,
    ...createResourcePendingStorage("lorebooks:resource-pending:v1", "workflow.tavern.lorebook.pending.v1"),
  });
}
