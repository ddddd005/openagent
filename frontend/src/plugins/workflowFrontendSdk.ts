import { inject, type ComputedRef, type InjectionKey } from "vue";
import type { GraphDocument, GraphNodeType, GraphSession } from "../domain/workflowGraph";
import type { FrontendWiringRequest } from "../domain/frontendWiring";
import type { FrontendArtifact, FrontendArtifactReference, FrontendReadScope } from "../domain/frontendDisplay";

export interface WorkflowNodeConfigurationRequest {
  workflowId: string; nodeId: string; componentId: string; componentVersion: string;
  expectedConfig: Record<string, unknown>; patch: Record<string, unknown>;
  extensionId: string; lifecycle: string;
}
export interface WorkflowNodeConfigurationResult { workflowId: string; nodeId: string }
export interface WorkflowFrontendSdk {
  workflowId: ComputedRef<string>;
  document: ComputedRef<GraphDocument | null>;
  catalog: ComputedRef<GraphNodeType[]>;
  packages: ComputedRef<{ package_id: string; version: string }[]>;
  session: ComputedRef<GraphSession | null>;
  locked: ComputedRef<boolean>;
  lifecycle: ComputedRef<string>;
  attachDisplay(request: FrontendWiringRequest): boolean;
  readArtifact(scope: FrontendReadScope, reference: FrontendArtifactReference): Promise<FrontendArtifact>;
  configureNode(request: WorkflowNodeConfigurationRequest): WorkflowNodeConfigurationResult | null;
}
export const workflowFrontendSdkKey: InjectionKey<WorkflowFrontendSdk> = Symbol("workflow-frontend-sdk-v1");
export function useWorkflowFrontendSdk() {
  const sdk = inject(workflowFrontendSdkKey);
  if (!sdk) throw new Error("workflow.frontend requires host SDK protocol 1");
  return sdk;
}
