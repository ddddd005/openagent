import { graphObject, graphSignature, type GraphDocument, type GraphEntry,
  type GraphEventBinding, type GraphNode, type GraphSession } from "./workflowGraph";

export const sessionVariableType = { type_id: "workflow.variable", schema_version: 1 } as const;
export function isVariableRegistrationNode(node: GraphNode) {
  return node.component_id === "tools.variable-register" && node.component_version === "2";
}
export function variableRegistrationIssues(config: Record<string, unknown>) {
  const issues: Record<string, string> = {};
  const name = config.name;
  if (typeof name !== "string" || [...name].length > 128 || !/^[\p{L}_][\p{L}\p{N}_]*$/u.test(name))
    issues.name = "变量名称须为 1-128 个字，只能包含字母、数字或下划线，且不能以数字开头";
  if (!["string", "integer", "number", "boolean"].includes(String(config.value_type))) {
    issues.value_type = "请选择变量类型";
    return issues;
  }
  if (config.has_initial !== true) return issues;
  const value = config.initial_value;
  if (config.value_type === "string" && typeof value !== "string")
    issues.initial_value = "初值必须是字符串";
  if (config.value_type === "integer" && !Number.isSafeInteger(value))
    issues.initial_value = "初值必须是安全范围内的整数";
  if (config.value_type === "number" && (typeof value !== "number" || !Number.isFinite(value)))
    issues.initial_value = "初值必须是有限数值";
  if (config.value_type === "boolean" && typeof value !== "boolean")
    issues.initial_value = "初值必须是布尔值";
  return issues;
}
export function variableRegistrationChange(previous: Record<string, unknown>, next: Record<string, unknown>) {
  if (next.has_initial !== true || previous.has_initial === true && previous.value_type === next.value_type
    || !variableRegistrationIssues(next).initial_value) return next;
  const defaults: Record<string, unknown> = { string: "", integer: 0, number: 0, boolean: false };
  return { ...next, initial_value: defaults[String(next.value_type)] };
}
export function variableRegistrationEvent(nodeId: string): GraphEventBinding {
  return { event_id: `tools.variable-register/${nodeId}`, schema_version: 1,
    display_name: "注册变量及初值", audience: "management", target_node_ids: [nodeId],
    payload_schema: { type: "object", properties: {}, additionalProperties: false } };
}
function isOwnRegistrationEvent(binding: GraphEventBinding, nodeId: string) {
  const expected = variableRegistrationEvent(nodeId);
  const schema = binding.payload_schema;
  return binding.event_id === expected.event_id && binding.schema_version === expected.schema_version
    && binding.audience === expected.audience && binding.target_node_ids.length === 1
    && binding.target_node_ids[0] === nodeId
    && Object.keys(schema).length === 3 && schema.type === "object" && schema.additionalProperties === false
    && graphObject(schema.properties) && Object.keys(schema.properties).length === 0;
}
export function syncVariableRegistrationEvent(document: GraphDocument, node: GraphNode) {
  if (document.event_bindings)
    document.event_bindings = document.event_bindings.filter(binding =>
      !isOwnRegistrationEvent(binding, node.node_binding_id));
  if (!isVariableRegistrationNode(node)) return;
  const expected = variableRegistrationEvent(node.node_binding_id);
  document.event_bindings ??= [];
  if (document.event_bindings.some(binding => binding.event_id === expected.event_id
    && binding.schema_version === expected.schema_version))
    throw new Error("变量注册事件身份已被其他声明占用");
  document.event_bindings.push(expected);
}
export function variableRegistrationTarget(entry: GraphEntry | undefined, session: GraphSession | null,
  nodeId: string) {
  const document = entry?.document, node = document?.nodes.find(row => row.node_binding_id === nodeId);
  if (!node || !isVariableRegistrationNode(node)) return null;
  const binding = document!.object_bindings?.find(row => row.object_key === node.config.object_key
    && row.registrar_node_id === nodeId && row.type_id === sessionVariableType.type_id
    && row.schema_version === sessionVariableType.schema_version
    && row.readers.includes(nodeId) && row.writers.includes(nodeId));
  const event = document!.event_bindings?.find(row => isOwnRegistrationEvent(row, nodeId));
  const matchingSession = session && entry?.session_id === session.workflow_session_id
    && session.workflow_definition_id === document!.workflow_definition_id
    && session.definition_revision === entry.saved_revision;
  const object = matchingSession && binding ? session.objects?.[binding.object_key] : null;
  const currentValue = object && !object.deleted && object.type_id === sessionVariableType.type_id
    && object.schema_version === sessionVariableType.schema_version && graphObject(object.value)
    ? object.value : null;
  const saved = !!entry?.saved_document && entry.saved_revision === document!.revision
    && graphSignature(entry.saved_document) === graphSignature(document!);
  const stable = !!matchingSession && session!.can_submit && !session!.archived && !session!.readonly
    && !["prepared", "running", "pausing", "paused"].includes(session!.status);
  const issue = Object.values(variableRegistrationIssues(node.config))[0];
  const canSubmit = !!binding && !!event && saved && stable && !entry?.pending && !issue;
  const state = !binding ? "登记项或读写权限无效"
    : issue ? issue
    : !matchingSession ? "尚无当前修订的会话"
    : !currentValue ? "当前变量状态尚未读取"
    : !currentValue.registered ? "未注册"
    : !currentValue.assigned ? "已注册，未赋值"
    : `已赋值：${JSON.stringify(currentValue.value)}`;
  return { binding, event, canSubmit, state, currentValue };
}
