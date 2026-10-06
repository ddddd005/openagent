import { clonePreparation, type PreparationDraft, type PreparationNode } from "../domain/preparation";
import type { ExposureRegistration } from "../domain/exposure";
import { AGENT_BINDINGS } from "./workbenchApi";
import { createWorkspaceDrafts, workspaceWorkflows } from "../fixtures/workflows";
import type { DraftStorage } from "./browserStorage";
import { isModelConfigurationSnapshot, type ModelConfigurationSnapshot } from "../domain/modelConfiguration";
import { isExposureSnapshot, type ExposureSnapshot } from "../domain/exposureConfiguration";
import { isWorkflowIdentity } from "../domain/workflowIdentity";
import { isDataDefinition } from "../domain/workbenchResources";
import type { WorkspaceWorkflow } from "../domain/workspace";
import { decodeUnifiedWorkbench, encodeUnifiedWorkbench, isUnifiedWorkbench } from "./unifiedWorkbenchDocument";

export const WORKBENCH_STORAGE_KEY = "workflow-workbench:fixed-base:v1";
const MAX_BYTES = 4_000_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const workflows = new Set(workspaceWorkflows.map((workflow) => workflow.id));
type Layouts = Record<string, Record<string, { x: number; y: number }>>;
export interface WorkbenchConfiguration {
  activeWorkflowId: string;
  selectedWorkflowId: string;
  layouts: Layouts;
  preparations: PreparationDraft[];
  registrations: ExposureRegistration[];
  runtime: unknown;
  models?: ModelConfigurationSnapshot;
  exposures?: ExposureSnapshot;
  catalog?: WorkspaceWorkflow[];
  graph?: unknown;
}
export interface SavedWorkbench extends WorkbenchConfiguration {
  schemaVersion: 1 | 2 | 3 | 4 | 5 | 6;
  kind: "fixed-workbench";
  revision: number;
  savedAt: string;
}
export interface WorkbenchRead {
  value: SavedWorkbench | null;
  raw: string | null;
  error: string | null;
}
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const exact = (value: unknown, fields: string[]): value is Record<string, unknown> =>
  object(value) && Object.keys(value).length === fields.length && fields.every((field) => Object.hasOwn(value, field));
const uuid = (value: unknown) => typeof value === "string" && UUID.test(value);
const text = (value: unknown, max = 1000) => typeof value === "string" && value.length <= max;
const revision = (value: unknown) => Number.isSafeInteger(value) && (value as number) > 0;
const number = (value: unknown) => typeof value === "number" && Number.isFinite(value);
const position = (value: unknown) => exact(value, ["x", "y"]) && number(value.x) && number(value.y);
function presentation(value: unknown) {
  return exact(value, ["role", "placement", "depth", "order", "enabled"])
    && ["system", "user", "assistant"].includes(value.role as string)
    && ["before", "middle", "after"].includes(value.placement as string)
    && (value.depth === null || number(value.depth)) && number(value.order) && typeof value.enabled === "boolean";
}
function node(value: unknown): value is PreparationNode {
  if (!object(value) || !["id", "kind", "title", "position", "config"].every(k => Object.hasOwn(value, k))
    || !Object.keys(value).every(k => ["id", "kind", "title", "position", "config", "publicOutputs"].includes(k))
    || Object.hasOwn(value, "publicOutputs") && (!Array.isArray(value.publicOutputs)
      || value.publicOutputs.length > 4 || !value.publicOutputs.every(id => text(id, 64)))
    || !uuid(value.id) || !text(value.title) || !position(value.position)) return false;
  const config = value.config;
  switch (value.kind) {
    case "prompt-item":
      return exact(config, ["itemId", "revision", "text", "presentation"])
        && uuid(config.itemId) && revision(config.revision) && text(config.text, 1_000_000) && presentation(config.presentation);
    case "prompt-group":
      return exact(config, ["groupId", "revision", "enabled", "members"])
        && uuid(config.groupId) && revision(config.revision) && typeof config.enabled === "boolean"
        && Array.isArray(config.members) && config.members.length <= 512
        && new Set(config.members.map((member) => object(member) && member.id)).size === config.members.length
        && config.members.every((member) => exact(member, ["id", "itemId", "revision", "name", "text", "presentation"])
          && uuid(member.id) && uuid(member.itemId) && revision(member.revision)
          && text(member.name) && text(member.text, 1_000_000) && presentation(member.presentation));
    case "tool":
      return exact(config, ["toolRef", "revision", "descriptionItemId", "schemaItemId",
        "descriptionInstanceId", "schemaInstanceId", "description", "schema"])
        && exact(config.toolRef, ["name", "version"]) && text(config.toolRef.name) && text(config.toolRef.version)
        && revision(config.revision) && ["descriptionItemId", "schemaItemId", "descriptionInstanceId", "schemaInstanceId"]
          .every((field) => uuid(config[field])) && presentation(config.description) && presentation(config.schema);
    case "prompt-collect":
    case "tool-collect":
      return exact(config, ["inputs"]) && Array.isArray(config.inputs) && config.inputs.length <= 128 && config.inputs.every(uuid);
    case "context":
      return object(config) && Object.keys(config).every(k => ["source", "reference", "floors", "sourceBindingId"].includes(k))
        && (!Object.hasOwn(config, "sourceBindingId") || uuid(config.sourceBindingId))
        && config.source === "local-preview"
        && config.reference === null && Array.isArray(config.floors) && config.floors.length === 0;
    case "assemble":
      return exact(config, []) || exact(config, ["targetStage"]) && ["A", "B"].includes(config.targetStage as string);
    case "global-content":
      return exact(config, ["resourceId"]) && (config.resourceId === null || uuid(config.resourceId));
    case "session-data-read":
    case "session-data-write":
      return exact(config, value.kind === "session-data-read" ? ["definition"] : ["definition", "value"])
        && isDataDefinition(config.definition);
    case "json-to-text":
      return exact(config, []);
    case "root-input":
    case "text":
      return exact(config, ["text"]) && text(config.text, 1_000_000);
    case "text-to-prompt":
      return exact(config, ["presentation"]) && presentation(config.presentation);
    case "prompt-to-text":
      return exact(config, ["separator"]) && text(config.separator, 1_000_000);
    case "regex":
      return exact(config, ["mode", "pattern", "replacement", "flags", "replaceMode"])
        && ["text", "prompt"].includes(config.mode as string)
        && text(config.pattern, 1_000_000) && text(config.replacement, 1_000_000)
        && Array.isArray(config.flags) && config.flags.length <= 5
        && new Set(config.flags).size === config.flags.length
        && config.flags.every((flag) => ["i", "m", "s", "x", "a"].includes(flag))
        && ["first", "all"].includes(config.replaceMode as string);
    case "variable-register":
      return exact(config, ["name", "valueType", "hasInitialValue", "initialValue"])
        && text(config.name, 128) && ["string", "integer", "number", "boolean"].includes(config.valueType as string)
        && typeof config.hasInitialValue === "boolean" && variableValue(config.initialValue, true);
    case "variable-assign":
      return exact(config, ["name", "valueType", "operation", "value"])
        && text(config.name, 128) && ["string", "integer", "number", "boolean"].includes(config.valueType as string)
        && ["set", "add", "subtract"].includes(config.operation as string) && variableValue(config.value);
    case "variable-replace":
      return exact(config, ["mode"]) && ["text", "prompt"].includes(config.mode as string);
    default:
      return false;
  }
}
function variableValue(value: unknown, nullable = false) {
  return nullable && value === null || typeof value === "boolean" || number(value) || text(value, 1_000_000);
}
function preparation(value: unknown, incremental = false): value is PreparationDraft {
  if (!exact(value, ["workflowId", "stage", "revision", "configId", "nodes", "edges"])
    || !isWorkflowIdentity(value.workflowId) || !["A", "B"].includes(value.stage as string)
    || !revision(value.revision) || !uuid(value.configId)
    || !Array.isArray(value.nodes) || value.nodes.length > 128 || !value.nodes.every(node)
    || !incremental && value.nodes.some((entry) => ["text", "text-to-prompt", "prompt-to-text",
      "regex", "variable-register", "variable-assign", "variable-replace"].includes(entry.kind))
    || new Set(value.nodes.map((entry) => entry.id)).size !== value.nodes.length
    || !Array.isArray(value.edges) || value.edges.length > 1024) return false;
  const ids = new Set(value.nodes.map((entry) => entry.id));
  return new Set(value.edges.map((edge) => object(edge) && edge.id)).size === value.edges.length
    && value.edges.every((edge) => exact(edge, ["id", "source", "target", "sourceHandle", "targetHandle"])
      && uuid(edge.id) && ids.has(edge.source as string) && (ids.has(edge.target as string)
        || ["frontend:main-test:agent-a", "frontend:main-test:agent-b"].includes(edge.target as string)
        && edge.sourceHandle === "assembled" && edge.targetHandle === "prompt-in")
      && text(edge.sourceHandle, 128) && text(edge.targetHandle, 128));
}
function registration(value: unknown): value is ExposureRegistration {
  if (!exact(value, ["schemaVersion", "id", "workflowId", "stage", "nodeBindingId", "publicName", "kind", "type", "fields"])
    || value.schemaVersion !== 1 || !uuid(value.id) || !isWorkflowIdentity(value.workflowId)
    || !["A", "B"].includes(value.stage as string) || value.nodeBindingId !== AGENT_BINDINGS[value.stage as "A" | "B"]
    || !text(value.publicName, 128) || !(value.publicName as string).trim()
    || !Array.isArray(value.fields) || !value.fields.length || new Set(value.fields).size !== value.fields.length) return false;
  const fields = value.kind === "state" && value.type === "public_agent_state"
    ? ["status", "run_id", "revision", "budget"]
    : value.kind === "result" && ["delivered_workflow_result", "public_node_result"].includes(value.type as string)
      ? value.stage === "A" ? ["delivered_text", "result_text"] : ["delivered_text"] : [];
  return value.fields.every((field) => fields.includes(field as string));
}
function layouts(value: unknown, catalog = workspaceWorkflows): value is Layouts {
  if (!object(value) || Object.keys(value).length !== catalog.length) return false;
  const defaults = createWorkspaceDrafts();
  return catalog.every(({ id, nodeCount }) => object(value[id])
    && Object.keys(value[id]).length === (nodeCount ? 3 : 0)
    && (nodeCount ? defaults["frontend:main-test"]!.nodes : []).every((entry) => position((value[id] as Record<string, unknown>)[entry.id])));
}
export function configurationOnly(drafts: PreparationDraft[]): PreparationDraft[] {
  return clonePreparation(drafts).map((draft) => ({
    ...draft,
    nodes: draft.nodes.map((entry) => entry.kind === "context"
      ? { ...entry, config: { ...(entry.config.sourceBindingId ? { sourceBindingId: entry.config.sourceBindingId } : {}),
        source: "local-preview", reference: null, floors: [] } } : entry),
  }));
}
export function isSavedWorkbench(value: unknown): value is SavedWorkbench {
  if (!object(value)) return false;
  const modelVersion = [2, 3, 4, 5].includes(value.schemaVersion as number);
  const exposureVersion = [3, 4, 5].includes(value.schemaVersion as number);
  const catalog = value.schemaVersion === 5 ? value.catalog : workspaceWorkflows;
  if (!Array.isArray(catalog) || catalog.length > 128 || !catalog.length
    || new Set(catalog.map(w => object(w) && w.id)).size !== catalog.length
    || !catalog.every(w => object(w) && isWorkflowIdentity(w.id) && text(w.title)
      && text(w.description) && [0, 3].includes(w.nodeCount as number)
      && (w.state === undefined || ["saved", "draft"].includes(w.state as string))
      && (w.sourceId === undefined || isWorkflowIdentity(w.sourceId))
      && (w.copyPending === undefined || typeof w.copyPending === "boolean")
      && (!w.copyPending || w.state === "draft" && isWorkflowIdentity(w.sourceId)
        && w.id !== w.sourceId && catalog.some(source => object(source) && source.id === w.sourceId)))) return false;
  return exact(value, ["schemaVersion", "kind", "revision", "savedAt", "activeWorkflowId",
    "selectedWorkflowId", "layouts", "preparations", "registrations", "runtime", ...(modelVersion ? ["models"] : []),
    ...(exposureVersion ? ["exposures"] : []), ...(value.schemaVersion === 5 ? ["catalog"] : [])])
    && [1, 2, 3, 4, 5].includes(value.schemaVersion as number) && value.kind === "fixed-workbench" && revision(value.revision)
    && typeof value.savedAt === "string" && Number.isFinite(Date.parse(value.savedAt))
    && catalog.some(w => w.id === value.activeWorkflowId) && catalog.some(w => w.id === value.selectedWorkflowId)
    && layouts(value.layouts, catalog as WorkspaceWorkflow[]) && Array.isArray(value.preparations) && value.preparations.length <= catalog.length * 2
    && value.preparations.every((draft) => preparation(draft, [4, 5].includes(value.schemaVersion as number)))
    && new Set(value.preparations.map((draft) => JSON.stringify([draft.workflowId, draft.stage]))).size === value.preparations.length
    && Array.isArray(value.registrations) && value.registrations.length <= 512 && value.registrations.every(registration)
    && new Set(value.registrations.map((entry) => entry.id)).size === value.registrations.length
    && new Set(value.registrations.map((entry) => JSON.stringify([entry.workflowId, entry.publicName]))).size === value.registrations.length
    && object(value.runtime) && (!modelVersion || isModelConfigurationSnapshot(value.models))
    && (!exposureVersion || isExposureSnapshot(value.exposures));
}
export function readWorkbench(storage: DraftStorage): WorkbenchRead {
  let raw: string | null = null;
  try {
    raw = storage.getItem(WORKBENCH_STORAGE_KEY);
    if (raw === null) return { value: null, raw, error: null };
    if (raw.length > MAX_BYTES) throw new Error("capacity");
    const value: unknown = JSON.parse(raw);
    if (isUnifiedWorkbench(value)) {
      const projected = decodeUnifiedWorkbench(value);
      const legacyCatalog = value.catalog.filter(row => value.documents[row.id].compatibility);
      if (legacyCatalog.length) {
        const legacyIds = new Set(legacyCatalog.map(row => row.id));
        const { graph: _graph, ...legacyProjection } = projected;
        if (!isSavedWorkbench({ ...legacyProjection, schemaVersion: 5, kind: "fixed-workbench",
          revision: value.revision, savedAt: value.savedAt, catalog: legacyCatalog,
          activeWorkflowId: legacyCatalog[0].id, selectedWorkflowId: legacyCatalog[0].id,
          layouts: Object.fromEntries(Object.entries(projected.layouts).filter(([id]) => legacyIds.has(id))),
          preparations: projected.preparations.filter(row => legacyIds.has(row.workflowId)),
          registrations: projected.registrations.filter(row => legacyIds.has(row.workflowId)),
          models: { ...projected.models, drafts: projected.models?.drafts.filter(row => legacyIds.has(row.workflowId)) ?? [] },
        })) throw new Error("legacy-schema");
      }
      return { value: { ...projected, schemaVersion: 6, kind: "fixed-workbench",
        revision: value.revision, savedAt: value.savedAt }, raw, error: null };
    }
    if (!isSavedWorkbench(value)) throw new Error("schema");
    return { value, raw, error: null };
  } catch {
    return { value: null, raw, error: "保存记录无法读取或版本不兼容，原始记录未覆盖" };
  }
}
export function writeWorkbench(storage: DraftStorage, value: SavedWorkbench, expectedRaw: string | null): string {
  const unified = value.schemaVersion === 6 && value.graph !== undefined
    ? encodeUnifiedWorkbench({ ...value, graph: value.graph }) : null;
  if (unified ? !isUnifiedWorkbench(unified) : !isSavedWorkbench(value)) throw new Error("工作台保存内容格式无效");
  const raw = JSON.stringify(unified ?? value);
  if (raw.length > MAX_BYTES) throw new Error("工作台保存内容超出容量");
  if (storage.getItem(WORKBENCH_STORAGE_KEY) !== expectedRaw)
    throw new Error("其他页面更新了保存记录，当前修改尚未保存");
  storage.setItem(WORKBENCH_STORAGE_KEY, raw);
  return raw;
}
