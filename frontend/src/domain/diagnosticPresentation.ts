import type { GraphDiagnostic, GraphOccupyingSession } from "./workflowGraph";

export const diagnosticPhaseLabels: Record<string, string> = {
  save: "保存校验", compile: "编译", prepare: "运行准备",
  run: "节点执行", settlement: "结算", storage: "存储",
};
export function diagnosticPhaseLabel(phase: string | undefined): string {
  return phase ? diagnosticPhaseLabels[phase] ?? phase : "阶段未标注";
}
/** Value summaries are deliberately bounded and type-first; never render a whole payload. */
export function diagnosticValueSummary(value: unknown, limit = 120): string {
  if (value === undefined) return "";
  let summary: string;
  if (value === null) summary = "null";
  else if (typeof value === "string") summary = `"${value.length > limit ? `${value.slice(0, limit)}…` : value}"`;
  else if (typeof value === "number" || typeof value === "boolean") summary = String(value);
  else if (Array.isArray(value)) summary = `array(${value.length})${JSON.stringify(value).slice(0, limit)}`;
  else if (typeof value === "object") summary = `object${JSON.stringify(value).slice(0, limit)}`;
  else summary = String(value);
  return summary.length > limit ? `${summary.slice(0, limit)}…` : summary;
}
export function diagnosticLocation(diagnostic: GraphDiagnostic): string {
  const parts: string[] = [];
  if (diagnostic.code ?? diagnostic.reason_code) parts.push(diagnostic.code ?? diagnostic.reason_code);
  if (diagnostic.path) parts.push(diagnostic.path);
  if (diagnostic.edge_id) parts.push(`连线 ${diagnostic.edge_id}`);
  if (diagnostic.node_id) parts.push(`节点 ${diagnostic.node_id}`);
  if (diagnostic.port_id) parts.push(`端口 ${diagnostic.port_id}`);
  if (diagnostic.object_key) parts.push(`对象键 ${diagnostic.object_key}`);
  if (diagnostic.dependent_outputs?.length) parts.push(`关联输出 ${diagnostic.dependent_outputs.join("、")}`);
  return parts.join(" · ");
}
export function diagnosticExpectation(diagnostic: GraphDiagnostic): string {
  const expected = diagnosticValueSummary(diagnostic.expected), actual = diagnosticValueSummary(diagnostic.actual);
  return [expected ? `期望 ${expected}` : "", actual ? `实际 ${actual}` : ""].filter(Boolean).join("；");
}
export function diagnosticFailureStage(diagnostic: GraphDiagnostic): string {
  return [diagnosticPhaseLabel(diagnostic.phase), diagnostic.failure_stage].filter(Boolean).join(" / ");
}
export function occupyingSessionLabels(rows: GraphOccupyingSession[] | undefined,
  labels: Record<string, string> = {}): string[] {
  return (rows ?? []).map(row => {
    const name = labels[row.workflow_session_id];
    return `${name ? `${name} ` : ""}会话 ${row.workflow_session_id}`
      + (row.active_chain_run_id ? ` · 活动链 ${row.active_chain_run_id}` : " · 无活动链");
  });
}

const recoveryLabels: Record<string, string> = {
  enable_provider: "在模型供应商中启用供应商，再开始新运行",
  configure_provider: "在模型供应商中检查服务地址和生效配置，再开始新运行",
  save_provider_key: "在模型供应商中保存有效 Key，再开始新运行",
  restore_private_credential_access: "恢复本机 Key 保护权限或重新保存 Key，再开始新运行",
  disable_transport_retries: "关闭供应商传输自动重试，再开始新运行",
  start_new_run: "配置订正后开始新运行，不重放原模型或工具请求",
  verify_original_request: "核实原请求回执，不要重新提交",
  retry_read: "重新读取；若仍失败，请凭关联编号检查服务日志",
  inspect_server_diagnostics: "检查服务日志中的失败步骤，再订正配置；不要重放原请求",
};
const causeLabels: Record<string, string> = {
  model_provider_disabled: "供应商已停用",
  model_provider_unavailable: "供应商配置不存在或不可用",
  model_provider_invalid: "供应商配置无效",
  model_credential_changed: "运行准备后 Key 已变化",
  model_credential_unavailable: "Key 缺失或不可读取",
  credential_reference_invalid: "本机 Key 引用无效",
  credential_protection_unavailable: "本机 Key 保护不可用",
  credential_protection_failed: "本机 Key 无法解密",
  model_retry_policy_invalid: "供应商自动重试策略不允许",
  model_transport_settings_invalid: "供应商通信参数无效",
  model_fact_acceptance_failed: "模型请求证据保存失败",
  model_fact_receipt_invalid: "模型请求证据回执无效",
  model_recovery_unavailable: "原运行不能继续",
  internal_error: "服务内部错误",
};
export function diagnosticRecovery(diagnostic: GraphDiagnostic): string {
  const cause = diagnostic.cause_code && causeLabels[diagnostic.cause_code];
  const action = diagnostic.recovery && recoveryLabels[diagnostic.recovery];
  return [cause, action].filter(Boolean).join("；");
}
