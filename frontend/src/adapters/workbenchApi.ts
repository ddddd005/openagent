import { diagnosticRecovery } from "../domain/diagnosticPresentation";
import { graphObject, graphUuid } from "../domain/workflowGraph";

export type FailureKind = "unavailable" | "rejected" | "unknown";

export class WorkbenchApiError extends Error {
  requestId?: string;
  nodeId?: string;
  diagnostics?: import("../domain/workflowGraph").GraphDiagnostic[];
  correlationId?: string;
  constructor(
    public kind: FailureKind,
    public reason: string,
    public status?: number,
    public code?: string,
  ) {
    super(reason);
  }
}

const publicReasons: Record<string, string> = {
  stale_revision: "状态修订已变化，请刷新",
  idempotency_conflict: "请求身份与既有请求不一致",
  invalid_request: "请求字段无效",
  ownership_mismatch: "请求归属不匹配",
  unsupported: "此操作尚未接入",
  invalid_state: "当前状态不允许此操作",
  budget_blocked: "运行预算不足",
  storage_error: "存储服务暂不可用",
  storage_contract_violation: "存储契约校验失败",
  not_found: "目标不存在",
  provider_missing: "确切供应商配置不存在",
  provider_unavailable: "供应商已停用",
  credential_reference_missing: "供应商凭据引用缺失或已撤销",
  credential_unavailable: "Key 不可用，请在模型供应商中填写并保存",
  credential_changed: "原执行凭据已变化，不能替换为新凭据继续",
  model_credential_changed: "运行准备后的 Key 已变化，不能替换为新 Key 继续",
  model_credential_unavailable: "Key 缺失或不可读取，请在模型供应商中填写并保存",
  model_credential_broker_unavailable: "私有 Key 服务未配置，请检查后端初始化",
  internal_error: "服务内部错误，请核实原请求或重新读取",
  model_not_dispatched: "模型请求在发送前被阻止，未自动重试",
  not_dispatched: "模型请求在发送前被阻止，未自动重试",
  model_profile_missing: "全局模型配置不存在，请在模型供应商中创建并重新选择",
  model_profile_unavailable: "引用的全局模型配置不存在或不可读取，请在模型供应商中检查并重新选择",
  model_profile_type_mismatch: "模型来源引用的不是全局模型配置，请重新选择模型配置",
  model_profile_disabled: "全局模型配置已停用，请在模型供应商中启用",
  model_profile_invalid: "全局模型配置无效，请在模型供应商中检查",
  model_source_invalid: "模型来源只能引用全局模型配置，请重新选择模型配置",
  model_source_protocol_mismatch: "模型配置、供应商与模型来源的协议不一致，请选择同一协议的配置",
  model_configuration_missing: "模型配置不存在，请在模型供应商中保存",
  model_capacity_unconfigured: "模型容量未配置，请在模型供应商中手动填写",
  model_capacity_unknown: "模型容量未配置，请在模型供应商中手动填写",
  model_capacity_invalid: "模型容量关系无效，请在模型供应商中检查窗口、最大输出、输出预留和摘要上限",
  model_output_reserve_unknown: "模型最大输出未配置，请在模型供应商中填写",
  model_provider_unavailable: "供应商配置不存在或不可读取，请在模型供应商中检查",
  model_provider_disabled: "供应商已停用，请在模型供应商中启用",
  model_provider_invalid: "供应商配置无效，请在模型供应商中检查名称、协议、服务地址和已保存 Key",
  model_provider_type_mismatch: "模型配置引用的不是供应商，请在模型供应商中重新选择",
  provider_binding_conflict: "Key 已保存，但供应商版本已变化，请核实后绑定已保存 Key",
  provider_binding_changed: "生效供应商引用已变化，请重新读取供应商配置",
  provider_credential_changed: "已保存 Key 的代次已变化，请重新读取并保存",
  credential_protection_unavailable: "当前宿主不支持本机 Key 安全保护，无法保存或使用 Key",
  credential_protection_failed: "本机 Key 无法安全保护或解密",
  credential_secret_invalid: "Key 为空、过长、首尾含空白或含控制字符",
  credential_reference_invalid: "本机 Key 引用无效",
  stale_credential_generation: "Key 已被其他页面修改，本次输入未保存",
  stale_provider_sequence: "供应商配置已变化，请刷新后重新获取列表",
  provider_discovery_unavailable: "上游模型列表请求未完成，仍可手填型号",
  provider_discovery_failed: "上游拒绝获取模型列表，仍可手填型号",
  provider_discovery_redirect_denied: "模型列表请求发生未许可的重定向",
  provider_discovery_invalid: "上游模型列表格式无法安全读取",
  provider_discovery_capacity: "上游模型列表超过页数、条目或响应大小限制",
  offline_model_unsupported: "离线服务只支持固定测试模型，不会转为在线调用",
  model_factory_unsupported: "模型适配器未支持确切供应商配置",
  model_parameters_unsupported: "模型参数超出当前适配器支持范围",
  model_selection_required: "此会话需携带确切模型选择，不能回退默认配置",
  missing_macro_variable: "变量尚未注册或赋值",
  variable_type_conflict: "当前会话变量与声明类型不一致",
  variable_projection_invalid: "会话变量响应格式无效",
  invalid_variable_data: "变量名称、类型或值无效",
  invalid_preparation_program: "提示词处理配置或连接无效",
  regex_timeout: "正则执行超时",
  regex_output_limit: "正则输出超出限制",
  invalid_regex_rule: "正则规则无效",
};

export async function workbenchRequest<T>(
  path: string,
  options: { body?: unknown; signal?: AbortSignal; readOnly?: boolean } = {},
): Promise<T> {
  const hasBody = options.body !== undefined;
  const mutation = hasBody && !options.readOnly;
  let response: Response;
  try {
    response = await fetch(path, {
      method: hasBody ? "POST" : "GET",
      cache: "no-store",
      signal: options.signal,
      ...(hasBody ? {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(options.body),
      } : {}),
    });
  } catch {
    throw new WorkbenchApiError(
      mutation ? "unknown" : "unavailable",
      mutation ? "未收到提交回执，保留原请求等待核实" : "本地服务连接不可用",
    );
  }
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new WorkbenchApiError(
      mutation ? "unknown" : "unavailable",
      "服务响应无法解析",
      response.status,
    );
  }
  if (!response.ok) {
    const error = graphObject(data) && graphObject(data.error) ? data.error : null;
    const candidate = error?.reason_code ?? error?.code;
    const code = typeof candidate === "string" && /^[A-Za-z0-9_]{1,64}$/.test(candidate)
      ? candidate : "unclassified";
    const reason = publicReasons[code] ?? (
      response.status === 409 ? "请求与当前会话状态冲突" :
        response.status === 403 ? "本地服务拒绝来源校验" :
          response.status >= 500 ? "服务暂不可用" : "服务端拒绝请求"
    );
    const failure = new WorkbenchApiError(
      mutation && response.status >= 500 ? "unknown" : mutation ? "rejected" : "unavailable",
      `${reason} [HTTP ${response.status} / ${code}]`,
      response.status,
      code,
    );
    const singular = graphObject(error?.diagnostic) ? error.diagnostic : null;
    if (graphUuid(singular?.node_id)) failure.nodeId = singular.node_id;
    if (graphUuid(error?.correlation_id)) failure.correlationId = error.correlation_id;
    const diagnostics = Array.isArray(error?.diagnostics) ? error.diagnostics : error?.diagnostic ? [error.diagnostic] : [];
    const token = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_]{1,64}$/.test(value);
    failure.diagnostics = diagnostics.filter(graphObject)
      .map(item => {
        const diagnostic = item as Record<string, unknown>;
        return { reason_code: token(diagnostic.reason_code) ? diagnostic.reason_code : token(diagnostic.code) ? diagnostic.code : code,
          message: typeof diagnostic.message === "string" ? diagnostic.message : reason,
          ...(typeof diagnostic.node_id === "string" ? { node_id: diagnostic.node_id } : {}),
          ...(typeof diagnostic.port_id === "string" ? { port_id: diagnostic.port_id } : {}),
          ...(typeof diagnostic.edge_id === "string" ? { edge_id: diagnostic.edge_id } : {}),
          ...(Array.isArray(diagnostic.dependent_outputs) ? { dependent_outputs: diagnostic.dependent_outputs.filter((v): v is string => typeof v === "string") } : {}),
          ...(token(diagnostic.cause_code) ? { cause_code: diagnostic.cause_code } : {}),
          ...(token(diagnostic.step) ? { step: diagnostic.step } : {}),
          ...(token(diagnostic.recovery) ? { recovery: diagnostic.recovery } : {}),
          ...(graphUuid(diagnostic.correlation_id) ? { correlation_id: diagnostic.correlation_id } : {}),
        };
      });
    const guidance = [...new Set(failure.diagnostics.map(diagnosticRecovery).filter(Boolean))].join("；");
    const correlation = failure.correlationId ?? failure.diagnostics.find(d => d.correlation_id)?.correlation_id;
    failure.reason += (guidance ? ` ${guidance}` : "") + (correlation ? ` 关联编号：${correlation}` : "");
    failure.message = failure.reason;
    throw failure;
  }
  return data as T;
}
