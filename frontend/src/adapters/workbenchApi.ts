export type FailureKind = "unavailable" | "rejected" | "unknown";

export class WorkbenchApiError extends Error {
  requestId?: string;
  nodeId?: string;
  diagnostics?: import("../domain/workflowGraph").GraphDiagnostic[];
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
  credential_unavailable: "后端凭据不可用",
  credential_changed: "原执行凭据已变化，不能替换为新凭据继续",
  model_credential_changed: "运行准备后的 Key 已变化，不能替换为新 Key 继续",
  model_credential_unavailable: "后端凭据缺失或不可读取",
  credential_protection_unavailable: "当前宿主不支持本机 Key 保护，请使用环境凭据",
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
    const error = (data as { error?: { reason_code?: string; code?: string; diagnostic?: { node_id?: unknown }; diagnostics?: unknown[] } })?.error;
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
    if (typeof error?.diagnostic?.node_id === "string"
      && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(error.diagnostic.node_id))
      failure.nodeId = error.diagnostic.node_id;
    const diagnostics = Array.isArray(error?.diagnostics) ? error.diagnostics : error?.diagnostic ? [error.diagnostic] : [];
    failure.diagnostics = diagnostics.filter(item => !!item && typeof item === "object")
      .map(item => {
        const diagnostic = item as Record<string, unknown>;
        return { reason_code: typeof diagnostic.reason_code === "string" ? diagnostic.reason_code : code,
          message: typeof diagnostic.message === "string" ? diagnostic.message : reason,
          ...(typeof diagnostic.node_id === "string" ? { node_id: diagnostic.node_id } : {}),
          ...(typeof diagnostic.port_id === "string" ? { port_id: diagnostic.port_id } : {}),
          ...(typeof diagnostic.edge_id === "string" ? { edge_id: diagnostic.edge_id } : {}),
          ...(Array.isArray(diagnostic.dependent_outputs) ? { dependent_outputs: diagnostic.dependent_outputs.filter((v): v is string => typeof v === "string") } : {}) };
      });
    throw failure;
  }
  return data as T;
}
