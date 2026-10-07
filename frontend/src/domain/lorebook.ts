import type { RolePlacement } from "./promptPresentation";
import { graphObject, graphUuid, isGraphJsonValue, type GraphDocument } from "./workflowGraph";

export const lorebookPackage = { package_id: "workflow.tavern", version: "1.0.0" };
export const lorebookLimits = {
  entries: 1024, keywords: 128, keywordLength: 4096, nameLength: 128,
  textLength: 1_000_000, scanDepth: 4096, configBytes: 4_000_000,
} as const;
export type LorebookKeywordRule = "primary_or_secondary" | "primary_and_secondary" | "primary_and_not_secondary";
export interface LorebookEntry {
  id: string;
  name: string;
  text: string;
  presentation: RolePlacement;
  metadata: Record<string, unknown>;
  mode: "keyword" | "constant";
  primary_keywords: string[];
  secondary_keywords: string[];
  keyword_rule: LorebookKeywordRule;
  case_sensitive: boolean;
  recursive: boolean;
  scan_depth: number;
  probability_enabled: boolean;
  probability: number;
}
export interface LorebookItemConfig { entry: LorebookEntry; object_keys: string[] }
export interface LorebookGroupConfig { entries: LorebookEntry[]; object_keys: string[] }
export type LorebookConfig = LorebookItemConfig | LorebookGroupConfig;
const keysAre = (value: Record<string, unknown>, keys: readonly string[]) =>
  Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const entryKeys = ["id", "name", "text", "presentation", "metadata", "mode", "primary_keywords",
  "secondary_keywords", "keyword_rule", "case_sensitive", "recursive", "scan_depth", "probability_enabled", "probability"];
const nonnegative = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0;
const wellFormedString = (value: string) =>
  !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);
const textWithin = (value: unknown, max: number): value is string => typeof value === "string"
  && [...value].length <= max && wellFormedString(value);
function wellFormedJsonStrings(value: unknown): boolean {
  if (typeof value === "string") return wellFormedString(value);
  if (Array.isArray(value)) return value.every(wellFormedJsonStrings);
  return !graphObject(value) || Object.entries(value).every(([key, child]) =>
    wellFormedString(key) && wellFormedJsonStrings(child));
}
function keywords(value: unknown): value is string[] {
  return Array.isArray(value) && value.length <= lorebookLimits.keywords
    && value.every(keyword => textWithin(keyword, lorebookLimits.keywordLength));
}
export function isLorebookEntry(value: unknown): value is LorebookEntry {
  if (!graphObject(value) || !keysAre(value, entryKeys) || !isGraphJsonValue(value)
    || !graphUuid(value.id) || value.id !== value.id.toLowerCase()
    || !textWithin(value.name, lorebookLimits.nameLength) || !textWithin(value.text, lorebookLimits.textLength)
    || !graphObject(value.metadata) || !wellFormedJsonStrings(value.metadata)
    || !["keyword", "constant"].includes(String(value.mode))
    || !keywords(value.primary_keywords) || !keywords(value.secondary_keywords)
    || !["primary_or_secondary", "primary_and_secondary", "primary_and_not_secondary"].includes(String(value.keyword_rule))
    || typeof value.case_sensitive !== "boolean" || typeof value.recursive !== "boolean"
    || !nonnegative(value.scan_depth) || value.scan_depth > lorebookLimits.scanDepth
    || typeof value.probability_enabled !== "boolean" || typeof value.probability !== "number"
    || !Number.isFinite(value.probability) || value.probability < 0 || value.probability > 100) return false;
  const presentation = value.presentation;
  return graphObject(presentation) && keysAre(presentation, ["role", "placement", "depth", "order", "enabled"])
    && ["system", "user", "assistant"].includes(String(presentation.role))
    && ["before", "middle", "after"].includes(String(presentation.placement))
    && typeof presentation.enabled === "boolean" && Number.isSafeInteger(presentation.order)
    && (presentation.placement === "middle" ? nonnegative(presentation.depth) : presentation.depth === null);
}
export function isLorebookConfig(value: unknown, componentId: string): value is LorebookConfig {
  if (!graphObject(value) || !isGraphJsonValue(value)
    || componentId !== "lorebook.item" && componentId !== "lorebook.group"
    || !keysAre(value, [componentId === "lorebook.item" ? "entry" : "entries", "object_keys"])
    || !Array.isArray(value.object_keys) || value.object_keys.length > 1024
    || !value.object_keys.every(key => textWithin(key, 128) && key.trim() === key && key.length > 0)
    || new Set(value.object_keys).size !== value.object_keys.length) return false;
  const entries = componentId === "lorebook.item" ? [value.entry] : value.entries;
  if (!Array.isArray(entries) || entries.length > lorebookLimits.entries || !entries.every(isLorebookEntry)
    || new Set(entries.map(entry => entry.id)).size !== entries.length) return false;
  return new TextEncoder().encode(JSON.stringify(value)).byteLength <= lorebookLimits.configBytes;
}
export function newLorebookEntry(order = 0): LorebookEntry {
  return { id: crypto.randomUUID(), name: "", text: "", metadata: {},
    presentation: { enabled: true, role: "system", placement: "before", depth: null, order },
    mode: "keyword", primary_keywords: [], secondary_keywords: [], keyword_rule: "primary_or_secondary",
    case_sensitive: false, recursive: false, scan_depth: 20, probability_enabled: false, probability: 100 };
}
export function moveLorebookEntry(entries: LorebookEntry[], index: number, offset: number): boolean {
  const next = index + offset;
  if (!Number.isSafeInteger(index) || !Number.isSafeInteger(next)
    || index < 0 || index >= entries.length || next < 0 || next >= entries.length || next === index) return false;
  [entries[index], entries[next]] = [entries[next]!, entries[index]!];
  const first = entries[index]!, second = entries[next]!;
  if (first.presentation.order === second.presentation.order)
    entries.forEach((entry, at) => { entry.presentation.order = at; });
  else [first.presentation.order, second.presentation.order] = [second.presentation.order, first.presentation.order];
  return true;
}
export function lorebookVariableBindings(document: GraphDocument, nodeId: string) {
  return (document.object_bindings ?? []).filter(binding => binding.type_id === "workflow.variable"
    && binding.schema_version === 1 && binding.readers.includes(nodeId));
}
export function lorebookBindingErrors(document: GraphDocument, nodeId: string, objectKeys: readonly string[]): string[] {
  const allowed = new Set(lorebookVariableBindings(document, nodeId).map(binding => binding.object_key));
  return objectKeys.filter(key => !allowed.has(key)).map(key => `变量对象未授权或类型不匹配：${key}`);
}
