import type { FrontendExtension } from "../domain/frontendExtensions";
import type { WorkflowNodeConfigurationRequest, WorkflowNodeConfigurationResult } from "../plugins/workflowFrontendSdk";

export interface NodeConfigurationPorts {
  workflowId(): string;
  lifecycle(): string;
  trustedExtensions(): readonly FrontendExtension[];
  patch(request: WorkflowNodeConfigurationRequest): WorkflowNodeConfigurationResult | null;
}
export function createNodeConfigurationAccess(ports: NodeConfigurationPorts) {
  return (request: WorkflowNodeConfigurationRequest): WorkflowNodeConfigurationResult | null => {
    if (request.lifecycle !== ports.lifecycle() || request.workflowId !== ports.workflowId()
      || !ports.trustedExtensions().some(row => row.extension_id === request.extensionId
        && row.binding.surface === "workbench" && row.binding.slot === "node-fields"
        && row.component_id === request.componentId && row.component_version === request.componentVersion)) return null;
    return ports.patch(request);
  };
}
