import { graphApplicationQuery } from "../adapters/workflowApplicationApi";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphClone, graphObject, graphUuid } from "../domain/workflowGraph";
import { isFrontendArtifactReference, type FrontendArtifact, type FrontendArtifactReference,
  type FrontendDisplayEntry, type FrontendReadScope } from "../domain/frontendDisplay";

export async function readFrontendArtifact(scope: FrontendReadScope, reference: FrontendArtifactReference): Promise<FrontendArtifact> {
  const fixed = graphClone(scope), ref = graphClone(reference);
  if (![fixed.sessionId, fixed.revisionId, fixed.nodeId].every(graphUuid) || !fixed.objectKey
    || !isFrontendArtifactReference(ref)) throw new WorkbenchApiError("unavailable", "展示来源或对象版本无效");
  const value = await graphApplicationQuery("object.artifact.read", { session_id: fixed.sessionId,
    object_key: fixed.objectKey, node_id: fixed.nodeId, revision_id: fixed.revisionId, reference: ref });
  const producer = graphObject(value) && graphObject(value.producer) ? value.producer : null;
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.object-artifact"
    || value.workflow_session_id !== fixed.sessionId || value.object_key !== fixed.objectKey
    || value.revision_id !== fixed.revisionId || !isFrontendArtifactReference(value.reference)
    || value.reference.output_id !== ref.output_id
    || typeof value.component_id !== "string" || typeof value.component_version !== "string"
    || !producer || Object.keys(producer).length !== 4
    || !["workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"].every(key => graphUuid(producer[key]))
    || !graphObject(value.value) || Object.keys(value.value).length !== 3
    || value.value.schema_version !== 2 || value.value.kind !== "workflow.text" || typeof value.value.text !== "string")
    throw new WorkbenchApiError("unavailable", "展示响应的引用、对象版本或 TEXT@2 内容不匹配");
  return value as unknown as FrontendArtifact;
}

export interface FrontendResolvedEntry {
  entry: FrontendDisplayEntry;
  artifact: FrontendArtifact | null;
  error: string | null;
}
export function createWorkflowFrontendDisplayAccess(currentBasis: () => string, read = readFrontendArtifact) {
  let generation = 0;
  return {
    invalidate() { generation++; },
    async display(scope: FrontendReadScope, entries: FrontendDisplayEntry[]): Promise<FrontendResolvedEntry[] | null> {
      const token = ++generation, basis = currentBasis(), fixed = graphClone(scope), sources = graphClone(entries);
      const current = () => token === generation && basis === currentBasis();
      const pending = [...new Map(sources.map(entry => [entry.source_ref.output_id, entry.source_ref])).values()];
      const artifacts = new Map<string, Pick<FrontendResolvedEntry, "artifact" | "error">>();
      let index = 0;
      await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => {
        while (current() && index < pending.length) {
          const reference = pending[index++]!;
          try { artifacts.set(reference.output_id, { artifact: await read(fixed, reference), error: null }); }
          catch (failure) { artifacts.set(reference.output_id, { artifact: null,
            error: failure instanceof Error ? failure.message : "展示来源读取失败" }); }
        }
      }));
      return current() ? sources.map(entry => ({ entry, ...artifacts.get(entry.source_ref.output_id)! })) : null;
    },
  };
}
