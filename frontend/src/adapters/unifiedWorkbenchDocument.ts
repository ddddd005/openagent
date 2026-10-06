import { graphClone, graphObject, graphUuid, isGraphDocument, type GraphDocument, type GraphNode } from "../domain/workflowGraph";
import { AGENT_BINDINGS } from "./workbenchApi";
import type { WorkbenchConfiguration } from "./workbenchPersistence";
import type { PreparationDraft, PreparationNode } from "../domain/preparation";
import type { ModelDraft } from "../domain/modelConfiguration";
import { createWorkspaceDrafts } from "../fixtures/workflows";
import { isWorkflowIdentity } from "../domain/workflowIdentity";

interface LegacyProjection {
  preparations: { workflowId: string; stage: "A" | "B"; revision: number; configId: string; nodes: string[]; edges: string[] }[];
  model?: { configId: string; backendRevision: number; nodes: string[]; edges: string[] };
}
interface DocumentRow { document: GraphDocument; compatibility?: LegacyProjection }
export interface UnifiedWorkbenchSave {
  schemaVersion: 6;
  kind: "unified-workbench";
  revision: number;
  savedAt: string;
  activeWorkflowId: string;
  selectedWorkflowId: string;
  catalog: NonNullable<WorkbenchConfiguration["catalog"]>;
  documents: Record<string, DocumentRow>;
  graph: unknown;
  registrations: WorkbenchConfiguration["registrations"];
  runtime: unknown;
  exposures: WorkbenchConfiguration["exposures"];
  modelPending: NonNullable<WorkbenchConfiguration["models"]>["pending"];
}
const MAIN_IDS: Record<string, string> = {
  "frontend:main-test:agent-a": AGENT_BINDINGS.A,
  "frontend:main-test:agent-b": AGENT_BINDINGS.B,
  "frontend:main-test:output": "7be319b8-30bd-4674-b7bf-d1cf54a1a10a",
};
const MAIN_EDGES: Record<string, string> = {
  "frontend:main-test:a-to-b": "7be319b8-30bd-4674-b7bf-d1cf54a1a10b",
  "frontend:main-test:b-to-output": "7be319b8-30bd-4674-b7bf-d1cf54a1a10c",
};
const OLD_IDS = Object.fromEntries(Object.entries(MAIN_IDS).map(([k,v]) => [v,k]));
const OLD_EDGES = Object.fromEntries(Object.entries(MAIN_EDGES).map(([k,v]) => [v,k]));
const rootId = (id: string) => graphUuid(id) ? id : id === "frontend:main-test"
  ? "7be319b8-30bd-4674-b7bf-d1cf54a1a101" : "7be319b8-30bd-4674-b7bf-d1cf54a1a102";
const nodeId = (id: string) => MAIN_IDS[id] ?? id;
const edgeId = (id: string) => MAIN_EDGES[id] ?? id;

// Legacy stores are editable adapter projections. Only the unified documents are serialized.
export function encodeUnifiedWorkbench(value: WorkbenchConfiguration & { revision: number; savedAt: string; graph: unknown }): UnifiedWorkbenchSave {
  const documents: Record<string, DocumentRow> = {};
  const graphEntries = graphObject(value.graph) && graphObject(value.graph.entries) ? value.graph.entries : {};
  const defaults = createWorkspaceDrafts()["frontend:main-test"]!;
  for (const workflow of value.catalog ?? []) {
    const generic = graphEntries[workflow.id];
    if (graphObject(generic) && isGraphDocument(generic.document)) {
      documents[workflow.id] = { document: graphClone(generic.document) };
      continue;
    }
    const nodes = new Map<string, GraphNode>();
    const edges = new Map<string, GraphDocument["edges"][number]>();
    const projection: LegacyProjection = { preparations: [] };
    for (const prep of value.preparations.filter(d => d.workflowId === workflow.id)) {
      projection.preparations.push({ workflowId: workflow.id, stage: prep.stage, revision: prep.revision,
        configId: prep.configId, nodes: prep.nodes.map(n => n.id), edges: prep.edges.map(e => edgeId(e.id)) });
      for (const node of prep.nodes) if (!nodes.has(node.id)) nodes.set(node.id, {
        node_binding_id: node.id, component_id: `legacy.preparation.${node.kind}`, component_version: "1",
        title: node.title, position: graphClone(node.position), config: graphClone(node.config) as unknown as Record<string, unknown>,
        ...(node.publicOutputs ? { public_outputs: [...node.publicOutputs] } : {}),
      });
      for (const edge of prep.edges) if (!edges.has(edgeId(edge.id))) edges.set(edgeId(edge.id), {
        edge_id: edgeId(edge.id), source_node_id: nodeId(edge.source), source_port_id: edge.sourceHandle,
        target_node_id: nodeId(edge.target), target_port_id: edge.targetHandle, order: 0,
      });
    }
    if (workflow.nodeCount) {
      for (const node of defaults.nodes) nodes.set(nodeId(node.id), {
        node_binding_id: nodeId(node.id), component_id: node.data.stage === "Output" ? "legacy.output" : "legacy.agent",
        component_version: "1", title: node.data.title,
        position: graphClone(value.layouts[workflow.id]?.[node.id] ?? node.position), config: { stage: node.data.stage },
      });
      for (const edge of defaults.edges) edges.set(edgeId(edge.id), { edge_id: edgeId(edge.id),
        source_node_id: nodeId(edge.source), source_port_id: edge.sourceHandle,
        target_node_id: nodeId(edge.target), target_port_id: edge.targetHandle, order: 0 });
    }
    const model = value.models?.drafts.find(row => row.workflowId === workflow.id);
    if (model) {
      projection.model = { configId: model.configId, backendRevision: model.backendRevision,
        nodes: model.nodes.map(n => n.id), edges: model.edges.map(e => e.id) };
      for (const node of model.nodes) nodes.set(node.id, { node_binding_id: node.id, component_id: "legacy.model-provider",
        component_version: "1", title: "模型提供", position: graphClone(node.position),
        config: { provider_ref: graphClone(node.provider_ref), parameters: graphClone(node.parameters) } });
      for (const edge of model.edges) edges.set(edge.id, { edge_id: edge.id, source_node_id: edge.source,
        source_port_id: "model-out", target_node_id: edge.target_binding_id, target_port_id: "model-in", order: 0 });
    }
    for (const edge of edges.values()) {
      const inputOrder = nodes.get(edge.target_node_id)?.config.inputs;
      if (Array.isArray(inputOrder) && inputOrder.includes(edge.source_node_id)) edge.order = inputOrder.indexOf(edge.source_node_id);
    }
    documents[workflow.id] = { document: { schema_version: 1, workflow_definition_id: rootId(workflow.id),
      revision: Math.max(1, ...projection.preparations.map(d => d.revision)), name: workflow.title,
      nodes: [...nodes.values()], edges: [...edges.values()] }, compatibility: projection };
  }
  // Runtime references and pending commands belong to the client; documents remain the only graph authority.
  const graphMetadata = graphObject(value.graph) ? graphClone(value.graph) : { schema_version: 1, entries: {} };
  if (graphObject(graphMetadata.entries)) for (const entry of Object.values(graphMetadata.entries))
    if (graphObject(entry)) delete entry.document;
  return { schemaVersion: 6, kind: "unified-workbench", revision: value.revision, savedAt: value.savedAt,
    activeWorkflowId: value.activeWorkflowId, selectedWorkflowId: value.selectedWorkflowId,
    catalog: graphClone(value.catalog ?? []), documents, graph: graphMetadata,
    registrations: graphClone(value.registrations), runtime: graphClone(value.runtime),
    exposures: graphClone(value.exposures), modelPending: graphClone(value.models?.pending ?? null) };
}
export function isUnifiedWorkbench(value: unknown): value is UnifiedWorkbenchSave {
  if (!graphObject(value) || value.schemaVersion !== 6 || value.kind !== "unified-workbench"
    || !Number.isSafeInteger(value.revision) || Number(value.revision) < 1
    || typeof value.savedAt !== "string" || !Number.isFinite(Date.parse(value.savedAt))
    || !Array.isArray(value.catalog) || !value.catalog.length || value.catalog.length > 128 || !graphObject(value.documents)
    || !graphObject(value.graph) || !graphObject(value.graph.entries) || !graphObject(value.runtime)
    || !Array.isArray(value.registrations)) return false;
  if (value.catalog.length !== Object.keys(value.documents).length
    || new Set(value.catalog.map(w => graphObject(w) && w.id)).size !== value.catalog.length) return false;
  const documents = value.documents;
  return value.catalog.every(workflow => graphObject(workflow) && isWorkflowIdentity(workflow.id)
    && typeof workflow.title === "string" && typeof workflow.description === "string"
    && Number.isSafeInteger(workflow.nodeCount) && Number(workflow.nodeCount) >= 0
    && graphObject(documents[workflow.id]) && isGraphDocument((documents[workflow.id] as Record<string,unknown>).document))
    && value.catalog.some(w => w.id === value.activeWorkflowId) && value.catalog.some(w => w.id === value.selectedWorkflowId)
    && Object.entries(value.graph.entries).every(([id, meta]) => graphObject(meta)
      && graphObject(documents[id]) && !(documents[id] as Record<string,unknown>).compatibility
      && Number.isSafeInteger(meta.saved_revision) && (meta.session_id === null || graphUuid(meta.session_id)));
}
export function decodeUnifiedWorkbench(value: UnifiedWorkbenchSave): WorkbenchConfiguration & { graph: unknown } {
  const preparations: PreparationDraft[] = [];
  const layouts: WorkbenchConfiguration["layouts"] = {};
  const modelDrafts: ModelDraft[] = [];
  const graph = graphClone(value.graph) as { schema_version: 1; entries: Record<string, Record<string, unknown>> };
  for (const [workflowId, row] of Object.entries(value.documents)) {
    const { document, compatibility } = row;
    layouts[workflowId] = {};
    if (!compatibility) {
      if (!graph.entries[workflowId]) throw new Error("统一文档缺少客户端归属");
      graph.entries[workflowId].document = graphClone(document);
      continue;
    }
    for (const node of document.nodes) if (OLD_IDS[node.node_binding_id]) layouts[workflowId][OLD_IDS[node.node_binding_id]!] = { ...node.position };
    for (const descriptor of compatibility.preparations) {
      preparations.push({ workflowId, stage: descriptor.stage, revision: descriptor.revision, configId: descriptor.configId,
        nodes: descriptor.nodes.map(id => {
          const node = document.nodes.find(n => n.node_binding_id === id);
          if (!node || !node.component_id.startsWith("legacy.preparation.")) throw new Error("旧图节点投影不完整");
          return { id, kind: node.component_id.slice("legacy.preparation.".length), title: node.title, position: graphClone(node.position),
            config: graphClone(node.config), ...(node.public_outputs ? { publicOutputs: [...node.public_outputs] } : {}) } as PreparationNode;
        }),
        edges: descriptor.edges.map(id => {
          const edge = document.edges.find(e => e.edge_id === id); if (!edge) throw new Error("旧图边投影不完整");
          return { id: OLD_EDGES[edge.edge_id] ?? edge.edge_id, source: OLD_IDS[edge.source_node_id] ?? edge.source_node_id,
            target: OLD_IDS[edge.target_node_id] ?? edge.target_node_id, sourceHandle: edge.source_port_id, targetHandle: edge.target_port_id };
        }),
      });
    }
    if (compatibility.model) {
      const descriptor = compatibility.model;
      modelDrafts.push({ workflowId, configId: descriptor.configId, backendRevision: descriptor.backendRevision,
        nodes: descriptor.nodes.map(id => { const node = document.nodes.find(n => n.node_binding_id === id);
          if (!node) throw new Error("模型投影不完整");
          if (node.component_id !== "legacy.model-provider" || Object.keys(node.config).length !== 2
            || !Object.hasOwn(node.config, "provider_ref") || !Object.hasOwn(node.config, "parameters"))
            throw new Error("模型投影配置含有未声明字段，原始保存未覆盖");
          return { id, position: graphClone(node.position), provider_ref: graphClone(node.config.provider_ref),
            parameters: graphClone(node.config.parameters) } as ModelDraft["nodes"][number]; }),
        edges: descriptor.edges.map(id => { const edge = document.edges.find(e => e.edge_id === id);
          if (!edge) throw new Error("模型依赖投影不完整");
          return { id, source: edge.source_node_id, target_binding_id: edge.target_node_id }; }),
      });
    }
  }
  return { activeWorkflowId: value.activeWorkflowId, selectedWorkflowId: value.selectedWorkflowId,
    layouts, preparations, registrations: graphClone(value.registrations), runtime: graphClone(value.runtime),
    models: { schemaVersion: 1, drafts: modelDrafts, pending: graphClone(value.modelPending) },
    exposures: graphClone(value.exposures), catalog: graphClone(value.catalog), graph };
}
