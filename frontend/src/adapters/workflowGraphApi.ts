import { WorkbenchApiError } from "./workbenchApi";
import { graphApplicationQuery } from "./workflowApplicationApi";
import { isFrontendExtension } from "../domain/frontendExtensions";
import { graphObject, isGraphCatalog, isGraphDocument, isGraphSession, isGraphPackageLock, type GraphDocument,
  graphUuid, type GraphNodeType, type GraphDataType, type GraphSession, type GraphRunDetail, type GraphCandidates } from "../domain/workflowGraph";

export async function readGraphCatalogDetails() {
  const response = await graphApplicationQuery("catalog.node-types", { protocol_version: 2 });
  if (!graphObject(response) || response.schema_version !== 2 || !isGraphCatalog(response.node_types)
    || !isGraphPackageLock(response.package_lock) || !Array.isArray(response.data_types)
    || !response.data_types.every(type => graphObject(type) && typeof type.type_id === "string"
      && Number.isSafeInteger(type.schema_version) && Number(type.schema_version) > 0
      && ["session", "content", "global"].includes(String(type.scope)) && graphObject(type.schema)))
    throw new WorkbenchApiError("unavailable", "节点目录契约无效");
  if (response.frontend_extensions !== undefined && (!Array.isArray(response.frontend_extensions)
    || !response.frontend_extensions.every(isFrontendExtension)))
    throw new WorkbenchApiError("unavailable", "前端扩展目录契约无效");
  const loadedPackages = response.package_lock;
  const executionPackageLock = response.execution_package_lock ?? loadedPackages;
  if (!isGraphPackageLock(executionPackageLock) || !executionPackageLock.every(required =>
    loadedPackages.some(loaded => loaded.package_id === required.package_id && loaded.version === required.version)))
    throw new WorkbenchApiError("unavailable", "执行包目录须为已加载包的确切版本子集");
  return { node_types: response.node_types, package_lock: response.package_lock,
    execution_package_lock: executionPackageLock,
    frontend_extensions: response.frontend_extensions ?? [],
    data_types: response.data_types as GraphDataType[] };
}
export async function readGraphCatalog(): Promise<GraphNodeType[]> {
  return (await readGraphCatalogDetails()).node_types;
}
export async function readGraphDefinition(id: string, revision?: number): Promise<GraphDocument> {
  const value = await graphApplicationQuery("definition.read", { identity: id, ...(revision ? { revision } : {}) });
  if (!isGraphDocument(value) || value.workflow_definition_id !== id || revision && value.revision !== revision)
    throw new WorkbenchApiError("unavailable", "工作流定义身份或版本不匹配");
  return value;
}
export async function readGraphSessions(definitionId: string): Promise<GraphSession[]> {
  const value = await graphApplicationQuery("definition.sessions", { workflow_definition_id: definitionId });
  if (!Array.isArray(value) || !value.every(session => isGraphSession(session, definitionId)))
    throw new WorkbenchApiError("unavailable", "会话目录契约无效");
  return value;
}
export async function readGraphSession(id: string, definitionId: string): Promise<GraphSession> {
  const value = await graphApplicationQuery("session.read", { session_id: id });
  if (!isGraphSession(value, definitionId) || value.workflow_session_id !== id)
    throw new WorkbenchApiError("unavailable", "工作流会话身份或数据契约不匹配");
  return value;
}
export async function readGraphRun(sessionId: string, chainId: string): Promise<GraphRunDetail> {
  const value = await graphApplicationQuery("run.read", { session_id: sessionId, chain_id: chainId });
  if (!graphObject(value) || !graphObject(value.chain) || value.chain.chain_run_id !== chainId
    || typeof value.chain.status !== "string" || !Array.isArray(value.node_runs) || !Array.isArray(value.outputs)
    || !value.node_runs.every(run => graphObject(run) && run.chain_run_id === chainId && graphUuid(run.run_id)
      && graphUuid(run.node_binding_id) && graphUuid(run.workflow_session_id) && typeof run.status === "string"
      && graphObject(run.input_values) && graphObject(run.input_refs) && graphObject(run.output_refs)
      && Object.values(run.output_refs).every(graphUuid) && Array.isArray(run.reads) && Array.isArray(run.effects)
      && (run.input_storage === undefined || ["inline", "references"].includes(String(run.input_storage)))
      && (run.input_storage !== "references" || Object.keys(run.input_values).length === 0)
      && (run.control_refs === undefined || Array.isArray(run.control_refs) && run.control_refs.every(ref =>
        graphObject(ref) && graphUuid(ref.edge_id) && graphUuid(ref.run_id))))
    || !value.outputs.every(output => graphObject(output) && output.chain_run_id === chainId
      && graphUuid(output.output_id) && graphUuid(output.node_binding_id) && typeof output.port_id === "string"))
    throw new WorkbenchApiError("unavailable", "运行历史或节点记录身份不匹配");
  const result = value as unknown as GraphRunDetail;
  return { ...result, node_runs: result.node_runs.map(run => {
    const { agent: _privateAgent, ...record } = run as typeof run & { agent?: unknown };
    return record;
  }) };
}
export async function readGraphCandidates(sessionId: string, definitionId: string): Promise<GraphCandidates> {
  const value = await graphApplicationQuery("candidate.list", { session_id: sessionId });
  const positive = (n: unknown) => Number.isSafeInteger(n) && Number(n) > 0;
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.graph-candidates"
    || value.workflow_session_id !== sessionId || value.workflow_definition_id !== definitionId
    || !positive(value.definition_revision) || !positive(value.session_revision) || !positive(value.head_revision)
    || !Array.isArray(value.candidates) || !value.candidates.every(row => graphObject(row)
      && graphUuid(row.candidate_id) && graphUuid(row.chain_run_id) && graphUuid(row.source_session_id)
      && graphUuid(row.source_workflow_definition_id) && positive(row.source_definition_revision)
      && Number.isSafeInteger(row.data_revision) && Number(row.data_revision) >= 0
      && typeof row.selected === "boolean" && typeof row.can_select === "boolean"
      && (row.diagnostic === null || graphObject(row.diagnostic)))
    || new Set(value.candidates.map(row => row.candidate_id)).size !== value.candidates.length)
    throw new WorkbenchApiError("unavailable", "工作流候选归属或数据契约不匹配");
  return value as unknown as GraphCandidates;
}
