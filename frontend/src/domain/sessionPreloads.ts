import { graphObject, isGraphJsonValue, isGraphSessionPreloads,
  type GraphDataType, type GraphObjectBinding } from "./workflowGraph";

export const preloadTypeKey = (type: Pick<GraphDataType, "type_id" | "schema_version">) =>
  `${type.type_id}@${type.schema_version}`;
export const isPreloadVariable = (type: Pick<GraphDataType, "type_id" | "schema_version">) =>
  type.type_id === "workflow.variable" && type.schema_version === 1;

const callbackFlags = ["has_references", "has_reference_mapper", "has_migrator"] as const;
const optionalCallbackFlags = ["has_write_validator", "has_artifact_bindings", "has_public_references",
  "has_content_transformer"] as const;
function hasOnlyLocalSchemaReferences(schema: Record<string, unknown>): boolean {
  const pending: unknown[] = [schema];
  while (pending.length) {
    const current = pending.pop();
    if (!graphObject(current)) continue;
    for (const keyword of ["$ref", "$dynamicRef", "$recursiveRef"]) {
      if (Object.hasOwn(current, keyword)
        && (typeof current[keyword] !== "string" || !current[keyword].startsWith("#"))) return false;
    }
    for (const keyword of ["$defs", "definitions", "properties", "patternProperties", "dependentSchemas"])
      if (graphObject(current[keyword])) pending.push(...Object.values(current[keyword]));
    for (const keyword of ["additionalProperties", "unevaluatedProperties", "propertyNames", "contains",
      "items", "additionalItems", "unevaluatedItems", "not", "if", "then", "else"])
      if (Object.hasOwn(current, keyword)) pending.push(current[keyword]);
    for (const keyword of ["allOf", "anyOf", "oneOf", "prefixItems", "items"])
      if (Array.isArray(current[keyword])) pending.push(...current[keyword]);
    if (graphObject(current.dependencies)) pending.push(...Object.values(current.dependencies).filter(graphObject));
  }
  return true;
}
export function preloadTypeIssue(type: GraphDataType): string | null {
  if (!isPreloadVariable(type)
    && ["workflow.variable", "workflow.chat-context", "workflow.effective-context"].includes(type.type_id))
    return "此专用会话类型不支持普通预加载。";
  if (type.scope !== "session" || type.checkpoint_admission !== true)
    return "类型须为允许检查点恢复的会话类型。";
  if (!graphObject(type.schema)) return "类型缺少声明式 JSON schema。";
  if (!hasOnlyLocalSchemaReferences(type.schema)) return "预加载类型的 schema 引用须为本地片段。";
  if (callbackFlags.some(flag => type[flag] !== false)
    || optionalCallbackFlags.some(flag => type[flag] !== undefined && type[flag] !== false)
    || (!isPreloadVariable(type) && type.has_validator !== false))
    return "类型含专用行为，或目录尚未提供完整准入信息。";
  return null;
}
export function preloadValueIssue(type: GraphDataType, value: unknown): string | null {
  if (!isGraphJsonValue(value)) return "初值必须是有效 JSON。";
  if (!isPreloadVariable(type)) return null;
  if (!graphObject(value) || Object.keys(value).length !== 5 || value.registered !== true
    || typeof value.name !== "string" || [...value.name].length > 128
    || !/^[\p{L}_][\p{L}\p{N}_]*$/u.test(value.name)
    || !["string", "integer", "number", "boolean"].includes(String(value.value_type))
    || typeof value.assigned !== "boolean" || !Object.hasOwn(value, "value"))
    return "变量需要名称、标量类型和完整的已注册值。";
  if (!value.assigned) return value.value === null ? null : "未赋初值的变量值须为 null。";
  const valid = value.value_type === "integer" ? Number.isSafeInteger(value.value)
    : value.value_type === "number" ? typeof value.value === "number" && Number.isFinite(value.value)
    : typeof value.value === value.value_type;
  return valid ? null : "变量初值与所选类型不一致。";
}
export function sessionPreloadsIssue(bindings: GraphObjectBinding[],
  preloads: unknown, types: GraphDataType[]): string | null {
  if (!isGraphSessionPreloads(preloads, bindings))
    return "预加载键须唯一且引用现有绑定；不可同时声明默认值或注册节点。";
  for (const entry of preloads) {
    const binding = bindings.find(binding => binding.object_key === entry.object_key)!;
    const type = types.find(type => type.scope === "session"
      && type.type_id === binding.type_id && type.schema_version === binding.schema_version);
    if (!type) return `未加载准确类型 ${preloadTypeKey(binding)}。`;
    const issue = preloadTypeIssue(type) ?? preloadValueIssue(type, entry.value);
    if (issue) return `${entry.object_key}：${issue}`;
  }
  return null;
}
