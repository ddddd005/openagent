import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone, graphObject, graphUuid, isGraphJsonValue } from "../domain/workflowGraph";
import { isFrontendExtension, type FrontendExtension } from "../domain/frontendExtensions";
import { isGraphConfigurationDocument, isGraphConfigurationResult, isGraphConfigurationUpdateRequest,
  type GraphConfigurationDocument, type GraphConfigurationResult,
  type GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";

// This SDK is not injected into installed host 1 packages until the joint package switch.
export interface WorkflowNodeConfigurationSubmission {
  workflowId: string;
  nodeId: string;
  componentId: string;
  componentVersion: string;
  extensionId: string;
  expectedEditSequence: number;
  expectedConfig: Record<string, unknown>;
  nextConfig: Record<string, unknown>;
  nextPolicy: { schema_version: 1; prompt_mode: "fixed" | "adjustable" };
}
export interface WorkflowConfigurationSubmissionPorts {
  document(): GraphConfigurationDocument | null;
  locked(workflowId: string): boolean;
  extensions(): readonly { declaration: FrontendExtension; configurationFields: readonly string[] }[];
  // The application submitter owns durable original-request evidence and unknown-request locks.
  submit(request: GraphConfigurationUpdateRequest): Promise<GraphConfigurationResult>;
  accept(result: GraphConfigurationResult, request: GraphConfigurationUpdateRequest): void;
  // A "confirmation_required" result wrote nothing; the application retains that
  // exact ask. Only explicit user consent resubmits it as a derivation request.
  confirmDerivation(workflowId: string): Promise<GraphConfigurationResult>;
  declineDerivation(workflowId: string): void;
}
function equal(left: unknown, right: unknown): boolean {
  if (Array.isArray(left) || Array.isArray(right))
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length
      && left.every((value, index) => equal(value, right[index]));
  if (graphObject(left) || graphObject(right))
    return graphObject(left) && graphObject(right) && Object.keys(left).length === Object.keys(right).length
      && Object.keys(left).every(key => Object.hasOwn(right, key) && equal(left[key], right[key]));
  return left === right;
}
export function createWorkflowConfigurationSdk(ports: WorkflowConfigurationSubmissionPorts) {
  const pending = new Set<string>();
  return {
    hostProtocolVersion: 2 as const,
    async submitNodeConfiguration(input: WorkflowNodeConfigurationSubmission): Promise<GraphConfigurationResult> {
      if (!graphObject(input) || !isGraphJsonValue(input))
        throw new WorkbenchApiError("rejected", "配置提交包含非 JSON 值", undefined, "graph_invalid_document");
      const request = graphClone(input), observed = ports.document();
      if (!isGraphConfigurationDocument(observed) || observed.workflow_definition_id !== request.workflowId
        || observed.edit_sequence !== request.expectedEditSequence || !graphUuid(request.nodeId)
        || ports.locked(request.workflowId) || pending.has(request.workflowId))
        throw new WorkbenchApiError("rejected", "工作流配置依据已变化或提交尚未结束", undefined, "stale_edit_sequence");
      const document = graphClone(observed), node = document.nodes.find(row => row.node_binding_id === request.nodeId);
      const extension = ports.extensions().find(row => isFrontendExtension(row.declaration)
        && row.declaration.schema_version === 3 && row.declaration.host_protocol_version === 2
        && row.declaration.extension_id === request.extensionId
        && row.declaration.binding.surface === "workbench"
        && ["node-fields", "node-actions"].includes(row.declaration.binding.slot)
        && row.declaration.component_id === request.componentId
        && row.declaration.component_version === request.componentVersion);
      if (!node || !extension || node.component_id !== request.componentId
        || node.component_version !== request.componentVersion || !equal(node.config, request.expectedConfig)
        || !graphObject(request.nextConfig))
        throw new WorkbenchApiError("rejected", "节点或可信配置扩展与提交依据不匹配", undefined, "graph_configuration_access_denied");
      if (extension.declaration.binding.slot === "node-actions") {
        if (!equal(node.config, request.nextConfig))
          throw new WorkbenchApiError("rejected", "节点动作不能修改业务配置", undefined, "graph_configuration_access_denied");
      } else {
        const changed = [...new Set([...Object.keys(node.config), ...Object.keys(request.nextConfig)])]
          .filter(key => !equal(node.config[key], request.nextConfig[key])
            || Object.hasOwn(node.config, key) !== Object.hasOwn(request.nextConfig, key));
        if (!equal(node.configuration_policy, request.nextPolicy)
          || changed.some(key => !extension.configurationFields.includes(key)))
          throw new WorkbenchApiError("rejected", "字段编辑器不能修改未接管字段或节点策略", undefined,
            "graph_configuration_access_denied");
      }
      node.config = graphClone(request.nextConfig);
      node.configuration_policy = graphClone(request.nextPolicy);
      const submission: GraphConfigurationUpdateRequest = {
        workflow_definition_id: request.workflowId, expected_edit_sequence: request.expectedEditSequence,
        document, idempotency_key: crypto.randomUUID(),
      };
      if (!isGraphConfigurationUpdateRequest(submission))
        throw new WorkbenchApiError("rejected", "配置文档格式无效", undefined, "graph_invalid_document");
      pending.add(request.workflowId);
      try {
        const result = await ports.submit(graphClone(submission));
        if (!isGraphConfigurationResult(result, submission))
          throw new WorkbenchApiError("unknown", "正式配置回执与原请求不匹配，保留原请求", undefined,
            "graph_configuration_receipt_invalid");
        ports.accept(graphClone(result), graphClone(submission));
        return graphClone(result);
      } finally {
        pending.delete(request.workflowId);
      }
    },
    // The caller asks the user, then reports consent or refusal here. A stale
    // question (no retained ask, or one already answered) is rejected instead
    // of silently deriving anything.
    async confirmDerivation(workflowId: string): Promise<GraphConfigurationResult> {
      if (!graphUuid(workflowId) || ports.locked(workflowId) || pending.has(workflowId))
        throw new WorkbenchApiError("rejected", "派生确认依据已变化", undefined, "stale_edit_sequence");
      pending.add(workflowId);
      try {
        return graphClone(await ports.confirmDerivation(workflowId));
      } finally {
        pending.delete(workflowId);
      }
    },
    declineDerivation(workflowId: string): void {
      if (!graphUuid(workflowId) || ports.locked(workflowId) || pending.has(workflowId))
        throw new WorkbenchApiError("rejected", "派生确认依据已变化", undefined, "stale_edit_sequence");
      ports.declineDerivation(workflowId);
    },
  };
}
