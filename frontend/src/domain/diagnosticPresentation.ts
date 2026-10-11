import type { GraphDiagnostic } from "./workflowGraph";

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
