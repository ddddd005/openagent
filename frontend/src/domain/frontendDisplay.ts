import { graphObject, graphUuid, type GraphSessionObject } from "./workflowGraph";
import { frontendStateType, type FrontendRole } from "./frontendWiring";

export interface FrontendArtifactReference { scope: "artifact"; output_id: string }
export interface FrontendDisplayEntry { entry_id: string; role: FrontendRole; source_ref: FrontendArtifactReference }
export interface FrontendReadScope { sessionId: string; objectKey: string; revisionId: string; nodeId: string }
export interface FrontendArtifact {
  schema_version: 1;
  kind: "workflow.object-artifact";
  workflow_session_id: string;
  object_key: string;
  revision_id: string;
  reference: FrontendArtifactReference;
  value: { schema_version: 2; kind: "workflow.text"; text: string };
  component_id: string;
  component_version: string;
  producer: { workflow_session_id: string; chain_run_id: string; node_binding_id: string; node_run_id: string };
}
export function isFrontendArtifactReference(value: unknown): value is FrontendArtifactReference {
  return graphObject(value) && Object.keys(value).length === 2 && value.scope === "artifact" && graphUuid(value.output_id);
}
export function frontendDisplayEntries(object: GraphSessionObject): FrontendDisplayEntry[] | null {
  if (object.type_id !== frontendStateType || object.schema_version !== 1 || object.deleted
    || !graphObject(object.value) || Object.keys(object.value).length !== 2
    || !Array.isArray(object.value.entries) || object.value.entries.length > 4095
    || !(object.value.view_ref === null || isFrontendArtifactReference(object.value.view_ref))
    || object.value.view_ref === null && object.value.entries.length > 0
    || !object.value.entries.every(entry => graphObject(entry) && Object.keys(entry).length === 3
      && graphUuid(entry.entry_id) && ["user", "assistant", "system"].includes(String(entry.role))
      && isFrontendArtifactReference(entry.source_ref))
    || new Set(object.value.entries.map(entry => entry.entry_id)).size !== object.value.entries.length) return null;
  return object.value.entries as FrontendDisplayEntry[];
}
