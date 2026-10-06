import { graphClone, graphObject, graphUuid, isGraphJsonValue } from "./workflowGraph";

export const promptResourceType = "workflow.prompt-resource";
export interface PromptIdentity {
  envelope_version: 1; scope: string; type_id: typeof promptResourceType; resource_id: string;
}
export interface PromptPresentation {
  role: "system" | "user" | "assistant";
  placement: "before" | "middle" | "after";
  depth: number | null;
  order: number;
  enabled: boolean;
}
export interface PromptMember {
  id: string; text: string; presentation: PromptPresentation; metadata: Record<string, unknown>;
}
export interface CurrentPromptResource extends PromptIdentity {
  data_schema_version: 1; update_sequence: number;
  value: { enabled: boolean; members: PromptMember[] };
}
const keysAre = (value: Record<string, unknown>, keys: string[]) =>
  Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const canonicalUuid = (value: unknown): value is string => graphUuid(value) && value === value.toLowerCase();
const wellFormedString = (value: string) =>
  !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);
function wellFormedJsonStrings(value: unknown): boolean {
  if (typeof value === "string") return wellFormedString(value);
  if (Array.isArray(value)) return value.every(wellFormedJsonStrings);
  return !graphObject(value) || Object.entries(value).every(([key, child]) =>
    wellFormedString(key) && wellFormedJsonStrings(child));
}
function promptJson(value: unknown): boolean {
  try { return isGraphJsonValue(value) && wellFormedJsonStrings(value); }
  catch { return false; }
}
function validIdentity(value: Record<string, unknown>): boolean {
  return value.envelope_version === 1 && typeof value.scope === "string"
    && value.scope === value.scope.trim() && value.scope.length > 0 && [...value.scope].length <= 128
    && !["session", "artifact"].includes(value.scope)
    && value.type_id === promptResourceType && canonicalUuid(value.resource_id);
}
export function isPromptIdentity(value: unknown): value is PromptIdentity {
  return promptJson(value) && graphObject(value)
    && keysAre(value, ["envelope_version", "scope", "type_id", "resource_id"]) && validIdentity(value);
}
function isPresentation(value: unknown): value is PromptPresentation {
  return graphObject(value) && keysAre(value, ["role", "placement", "depth", "order", "enabled"])
    && typeof value.role === "string" && ["system", "user", "assistant"].includes(value.role)
    && typeof value.placement === "string" && ["before", "middle", "after"].includes(value.placement)
    && Number.isSafeInteger(value.order) && typeof value.enabled === "boolean"
    && (value.placement === "middle"
      ? Number.isSafeInteger(value.depth) && Number(value.depth) >= 0 : value.depth === null);
}
function isMember(value: unknown): value is PromptMember {
  return graphObject(value) && keysAre(value, ["id", "text", "presentation", "metadata"])
    && canonicalUuid(value.id) && typeof value.text === "string" && isPresentation(value.presentation)
    && graphObject(value.metadata);
}
export function isCurrentPromptResource(value: unknown): value is CurrentPromptResource {
  if (!promptJson(value) || !graphObject(value)
    || !keysAre(value, ["envelope_version", "scope", "type_id", "resource_id",
      "data_schema_version", "update_sequence", "value"]) || !validIdentity(value)
    || value.data_schema_version !== 1 || !Number.isSafeInteger(value.update_sequence)
    || Number(value.update_sequence) <= 0 || !graphObject(value.value)
    || !keysAre(value.value, ["enabled", "members"]) || typeof value.value.enabled !== "boolean"
    || !Array.isArray(value.value.members) || value.value.members.length > 1024
    || !value.value.members.every(isMember)) return false;
  return new Set(value.value.members.map(member => member.id)).size === value.value.members.length;
}
export function promptIdentity(value: CurrentPromptResource): PromptIdentity {
  return { envelope_version: 1, scope: value.scope, type_id: promptResourceType, resource_id: value.resource_id };
}
export function samePromptIdentity(left: PromptIdentity, right: PromptIdentity) {
  return left.envelope_version === right.envelope_version && left.scope === right.scope
    && left.type_id === right.type_id && left.resource_id === right.resource_id;
}
export function newPromptResource(): CurrentPromptResource {
  return { envelope_version: 1, scope: "workspace", type_id: promptResourceType, resource_id: crypto.randomUUID(),
    data_schema_version: 1, update_sequence: 1, value: { enabled: true, members: [] } };
}
export function newPromptMember(text = ""): PromptMember {
  return { id: crypto.randomUUID(), text,
    presentation: { role: "system", placement: "before", depth: null, order: 0, enabled: true }, metadata: {} };
}
export function clonePromptResource(value: CurrentPromptResource) { return graphClone(value); }
export function promptResourceLabel(value: CurrentPromptResource): string {
  const text = value.value.members.find(member => member.text.trim())?.text.trim();
  return text ? text.split(/\r\n?|\n/, 1)[0]! : value.resource_id;
}
