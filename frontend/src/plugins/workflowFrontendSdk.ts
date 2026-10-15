import { inject, type ComputedRef, type InjectionKey } from "vue";
import type { GraphDocument, GraphNodeType, GraphSession } from "../domain/workflowGraph";

export interface WorkflowNodeConfigurationRequest {
  workflowId: string; nodeId: string; componentId: string; componentVersion: string;
  expectedConfig: Record<string, unknown>; patch: Record<string, unknown>;
  extensionId: string; lifecycle: string;
}
export interface WorkflowNodeConfigurationResult {
  workflowId: string;
  nodeId: string;
  outcome: "saved" | "derived" | "unchanged" | "cancelled";
}
export interface WorkflowFrontendSdk {
  workflowId: ComputedRef<string>;
  document: ComputedRef<GraphDocument | null>;
  catalog: ComputedRef<GraphNodeType[]>;
  packages: ComputedRef<{ package_id: string; version: string }[]>;
  session: ComputedRef<GraphSession | null>;
  locked: ComputedRef<boolean>;
  lifecycle: ComputedRef<string>;
  configureNode(request: WorkflowNodeConfigurationRequest): Promise<WorkflowNodeConfigurationResult | null>;
}
export const workflowFrontendSdkKey: InjectionKey<WorkflowFrontendSdk> = Symbol("workflow-frontend-sdk-v2");
export function useWorkflowFrontendSdk() {
  const sdk = inject(workflowFrontendSdkKey);
  if (!sdk) throw new Error("workflow.frontend requires host SDK protocol 2");
  return sdk;
}
