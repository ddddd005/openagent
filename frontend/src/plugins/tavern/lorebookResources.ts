import { graphClone, graphObject, graphUuid, isGraphJsonValue } from "../../domain/workflowGraph";
import { isLorebookConfig, type LorebookEntry } from "../../domain/lorebook";

export const lorebookResourceType = "workflow.tavern.lorebook";
export const globalTavernPackage = { package_id: "workflow.tavern", version: "1.1.0" };
export interface LorebookIdentity {
  envelope_version: 1; scope: string; type_id: typeof lorebookResourceType; resource_id: string;
}
export interface CurrentLorebookResource extends LorebookIdentity {
  data_schema_version: 1; update_sequence: number;
  value: { name: string; enabled: boolean; entries: LorebookEntry[] };
}
const keysAre = (value: Record<string, unknown>, keys: string[]) =>
  Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const validText = (value: unknown, max: number): value is string => typeof value === "string"
  && [...value].length <= max
  && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);
function validIdentity(value: Record<string, unknown>) {
  return value.envelope_version === 1 && validText(value.scope, 128) && value.scope.length > 0
    && value.scope.trim() === value.scope && !["session", "artifact"].includes(value.scope)
    && value.type_id === lorebookResourceType && graphUuid(value.resource_id)
    && value.resource_id === value.resource_id.toLowerCase();
}
export function isLorebookIdentity(value: unknown): value is LorebookIdentity {
  return graphObject(value) && isGraphJsonValue(value) && keysAre(value, ["envelope_version", "scope", "type_id", "resource_id"])
    && validIdentity(value);
}
export function isCurrentLorebookResource(value: unknown): value is CurrentLorebookResource {
  try {
    return graphObject(value) && isGraphJsonValue(value) && keysAre(value, [
      "envelope_version", "scope", "type_id", "resource_id", "data_schema_version", "update_sequence", "value",
    ]) && validIdentity(value) && value.data_schema_version === 1
      && Number.isSafeInteger(value.update_sequence) && Number(value.update_sequence) > 0
      && graphObject(value.value) && keysAre(value.value, ["name", "enabled", "entries"])
      && validText(value.value.name, 128) && typeof value.value.enabled === "boolean"
      && isLorebookConfig({ entries: value.value.entries, object_keys: [] }, "lorebook.group")
      && new TextEncoder().encode(JSON.stringify(value)).byteLength <= 1_000_000;
  } catch { return false; }
}
export function isLorebookReferenceConfig(value: unknown): value is { reference: LorebookIdentity } {
  return graphObject(value) && keysAre(value, ["reference"]) && isLorebookIdentity(value.reference);
}
export function isLorebookActivationConfig(value: unknown): value is { object_keys: string[] } {
  return graphObject(value) && keysAre(value, ["object_keys"])
    && isLorebookConfig({ entries: [], object_keys: value.object_keys }, "lorebook.group");
}
export function lorebookIdentity(value: CurrentLorebookResource): LorebookIdentity {
  return { envelope_version: 1, scope: value.scope, type_id: lorebookResourceType, resource_id: value.resource_id };
}
export function lorebookIdentityKey(value: LorebookIdentity): string {
  return JSON.stringify([value.envelope_version, value.scope, value.type_id, value.resource_id]);
}
export function newLorebookResource(): CurrentLorebookResource {
  return { envelope_version: 1, scope: "workspace", type_id: lorebookResourceType, resource_id: crypto.randomUUID(),
    data_schema_version: 1, update_sequence: 1, value: { name: "", enabled: true, entries: [] } };
}
export function cloneLorebookResource(value: CurrentLorebookResource) { return graphClone(value); }
export function lorebookResourceLabel(value: CurrentLorebookResource) { return value.value.name.trim() || value.resource_id; }
