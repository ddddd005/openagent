import { graphSignature, type GraphDocument, type GraphNodeType } from "./workflowGraph";
import { nodeTypeKey } from "./nodeCatalog";

export interface NodeSelectionContext {
  workflowId: string;
  viewVersion: number;
  sessionId: string | null;
  document: GraphDocument | null;
  catalog: GraphNodeType[];
  packageLock: NonNullable<GraphDocument["package_lock"]>;
  locked: boolean;
  catalogLoading: boolean;
  catalogError: string | null;
}
export interface NodeSelectionRequest {
  workflowId: string;
  viewVersion: number;
  sessionId: string | null;
  documentSignature: string;
  catalogSignature: string;
  familyId: string;
  position?: { x: number; y: number };
  nodeId?: string;
  currentKey?: string;
}
const documentSignature = (document: GraphDocument) => `${document.revision}:${graphSignature(document)}`;
const catalogSignature = (context: NodeSelectionContext) => JSON.stringify([context.catalog, context.packageLock]);

export function captureNodeSelection(context: NodeSelectionContext, familyId: string,
  target: { position: { x: number; y: number } } | { nodeId: string }): NodeSelectionRequest | null {
  if (!context.document || context.locked || context.catalogLoading || context.catalogError) return null;
  const node = "nodeId" in target ? context.document.nodes.find(row => row.node_binding_id === target.nodeId) : undefined;
  if ("nodeId" in target && !node) return null;
  return {
    workflowId: context.workflowId, viewVersion: context.viewVersion, sessionId: context.sessionId,
    documentSignature: documentSignature(context.document), catalogSignature: catalogSignature(context), familyId,
    ...("position" in target ? { position: { ...target.position } }
      : { nodeId: target.nodeId, currentKey: nodeTypeKey(node!) }),
  };
}

export function nodeSelectionIssue(request: NodeSelectionRequest, context: NodeSelectionContext): string | null {
  if (context.workflowId !== request.workflowId || context.viewVersion !== request.viewVersion
    || context.sessionId !== request.sessionId) return "工作流或会话已切换，请重新选择节点。";
  if (!context.document || documentSignature(context.document) !== request.documentSignature)
    return "工作流已变化，请关闭后重新选择节点。";
  if (context.catalogLoading) return "节点目录正在刷新。";
  if (context.catalogError || catalogSignature(context) !== request.catalogSignature)
    return "节点目录已变化或不可用，请关闭后重新选择节点。";
  if (context.locked) return "工作流当前不可编辑。";
  return null;
}

export function nodeSelectionAllowed(request: NodeSelectionRequest, context: NodeSelectionContext,
  key: string, resetConfirmed = false): boolean {
  if (nodeSelectionIssue(request, context) || request.nodeId && (!resetConfirmed || request.currentKey === key)) return false;
  const definition = context.catalog.find(row => nodeTypeKey(row) === key);
  return !!definition?.executable && (!!request.nodeId || definition.component_id === request.familyId);
}
