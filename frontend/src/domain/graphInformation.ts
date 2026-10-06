import { graphObject, graphUuid } from "./workflowGraph";

export interface GraphInformationOwner {
  workflow_session_id: string;
  chain_run_id: string;
  node_binding_id: string;
  node_run_id: string;
}
export interface GraphInformationReference { source_id: string; exact_version: string }
export interface GraphInformationDeclaration {
  source_ref: GraphInformationReference;
  component_id: string;
  component_version: string;
  channel_id: string;
  format_id: string;
  format_version: number;
  item_schema: Record<string, unknown>;
  discover_public: boolean;
  read_public: boolean;
  source_scope: "live" | "history" | "live_and_history";
  max_page_size: number;
  max_page_bytes: number;
}
export interface GraphRegistration {
  kind: string;
  registration_ref: Record<string, unknown>;
  package_id: string | null;
  package_version: string | null;
  declaration: Record<string, unknown>;
  discover_public: boolean;
  read_public: boolean;
  availability: string;
  owner?: GraphInformationOwner;
  generation?: number;
  [key: string]: unknown;
}
export interface GraphInformationBinding extends GraphRegistration {
  kind: "information_binding";
  registration_ref: GraphInformationReference & Record<string, unknown>;
  declaration: GraphInformationDeclaration & Record<string, unknown>;
  owner: GraphInformationOwner;
  generation: number;
}
export interface GraphRegistrationPage { items: GraphRegistration[]; next_cursor: string | null }
export interface GraphInformationPage {
  schema_version: 1;
  source_ref: GraphInformationReference;
  owner: GraphInformationOwner;
  generation: number;
  source_scope: "live" | "history";
  format_id: string;
  format_version: number;
  items: unknown[];
  next_cursor: string | null;
  status: "ok" | "gap" | "reset";
}
export interface GraphRegistrationParameters {
  session_id?: string;
  chain_id?: string;
  node_id?: string;
  kind?: string;
  package_id?: string;
  channel_id?: string;
  limit?: number;
  cursor?: string;
}
const positive = (value: unknown) => Number.isSafeInteger(value) && Number(value) > 0;
const name = (value: unknown) => typeof value === "string" && value.length > 0 && value.length <= 128 && value.trim() === value;
export const graphInformationCursor = (value: unknown): value is string | null =>
  value === null || typeof value === "string" && value.length > 0 && value.length <= 4096;
export function isGraphInformationOwner(value: unknown): value is GraphInformationOwner {
  return graphObject(value) && Object.keys(value).length === 4
    && ["workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"].every(field => graphUuid(value[field]));
}
export function sameGraphInformationOwner(a: GraphInformationOwner, b: GraphInformationOwner) {
  return a.workflow_session_id === b.workflow_session_id && a.chain_run_id === b.chain_run_id
    && a.node_binding_id === b.node_binding_id && a.node_run_id === b.node_run_id;
}
export function isGraphInformationReference(value: unknown): value is GraphInformationReference {
  return graphObject(value) && Object.keys(value).length === 2 && name(value.source_id) && name(value.exact_version);
}
export function sameGraphInformationReference(a: GraphInformationReference, b: GraphInformationReference) {
  return a.source_id === b.source_id && a.exact_version === b.exact_version;
}
export function isGraphInformationDeclaration(value: unknown): value is GraphInformationDeclaration {
  return graphObject(value) && isGraphInformationReference(value.source_ref)
    && ["component_id", "component_version", "channel_id", "format_id"].every(field => name(value[field]))
    && positive(value.format_version) && graphObject(value.item_schema)
    && typeof value.discover_public === "boolean" && typeof value.read_public === "boolean"
    && ["live", "history", "live_and_history"].includes(String(value.source_scope))
    && positive(value.max_page_size) && Number(value.max_page_size) <= 1000
    && positive(value.max_page_bytes) && Number(value.max_page_bytes) <= 4_000_000;
}
export function isGraphInformationBinding(value: unknown): value is GraphInformationBinding {
  return graphObject(value) && value.kind === "information_binding"
    && isGraphInformationReference(value.registration_ref) && isGraphInformationDeclaration(value.declaration)
    && sameGraphInformationReference(value.registration_ref, value.declaration.source_ref)
    && isGraphInformationOwner(value.owner) && positive(value.generation);
}
export function isGraphRegistration(value: unknown): value is GraphRegistration {
  return graphObject(value) && name(value.kind) && graphObject(value.registration_ref)
    && (value.package_id === null || name(value.package_id)) && (value.package_version === null || name(value.package_version))
    && graphObject(value.declaration) && typeof value.discover_public === "boolean"
    && typeof value.read_public === "boolean" && name(value.availability)
    && (value.kind !== "information_source" || isGraphInformationReference(value.registration_ref)
      && isGraphInformationDeclaration(value.declaration)
      && sameGraphInformationReference(value.registration_ref, value.declaration.source_ref))
    && (value.kind !== "information_binding" || isGraphInformationBinding(value));
}
export function graphInformationScopes(binding: GraphInformationBinding): ("live" | "history")[] {
  return binding.declaration.source_scope === "live_and_history" ? ["live", "history"] : [binding.declaration.source_scope];
}
