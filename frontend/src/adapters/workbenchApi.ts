import type { WorkflowObservation } from "../domain/observation";

export const AGENT_BINDINGS = {
  A: "7be319b8-30bd-4674-b7bf-d1cf54a1a108",
  B: "7be319b8-30bd-4674-b7bf-d1cf54a1a109",
} as const;

export interface PublicNodeState {
  node_binding_id: string;
  label: string;
  status: string;
  run_id: string | null;
  revision: number | null;
  budget?: {
    max_model_requests: number;
    max_model_attempts: number;
    model_requests: number;
    attempts: number;
  };
}

export interface PublicSession {
  observation?: WorkflowObservation;
  workflow_session_id: string;
  revision: number;
  mode: string;
  can_submit: boolean;
  ref_revision?: number;
  head_commit_id?: string | null;
  available_actions: string[];
  nodes: PublicNodeState[];
  chains: { chain_run_id: string; status: string }[];
  messages: {
    visible_message_id: string;
    role: string;
    payload: unknown;
    sequence: number;
    chain_run_id: string | null;
  }[];
}

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
  model_dependency_missing: "A/B 模型依赖缺失",
  provider_missing: "确切供应商配置不存在",
  provider_unavailable: "供应商已停用",
  credential_reference_missing: "供应商凭据引用缺失或已撤销",
  credential_unavailable: "后端凭据不可用",
  credential_changed: "原执行凭据已变化，不能替换为新凭据继续",
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

export function isPublicSession(value: unknown, sessionId: string): value is PublicSession {
  const view = value as Partial<PublicSession> | null;
  const uuid = (input: unknown) => typeof input === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(input);
  const nonnegative = (input: unknown) => Number.isSafeInteger(input) && (input as number) >= 0;
  const statuses = [
    "idle", "pending", "prepared", "queued", "running", "pausing", "paused",
    "final_ready", "succeeded", "failed", "superseded", "closed", "recovery_unavailable",
  ];
  const bindings: string[] = [...Object.values(AGENT_BINDINGS), "7be319b8-30bd-4674-b7bf-d1cf54a1a10a"];
  return !!view && view.workflow_session_id === sessionId
    && uuid(view.workflow_session_id) && nonnegative(view.revision) && typeof view.can_submit === "boolean"
    && (view.ref_revision === undefined || nonnegative(view.ref_revision))
    && (view.head_commit_id === undefined || view.head_commit_id === null || uuid(view.head_commit_id))
    && ["offline", "deepseek"].includes(view.mode ?? "")
    && Array.isArray(view.available_actions) && view.available_actions.every((a) => typeof a === "string")
    && Array.isArray(view.nodes) && view.nodes.length === 3
    && new Set(view.nodes.map((node) => node?.node_binding_id)).size === 3
    && view.nodes.every((node) => node && bindings.includes(node.node_binding_id)
      && typeof node.label === "string" && statuses.includes(node.status)
      && (node.run_id === null ? node.revision === null : uuid(node.run_id) && nonnegative(node.revision))
      && (!node.budget || ["max_model_requests", "max_model_attempts", "model_requests", "attempts"]
        .every((key) => nonnegative(node.budget![key as keyof NonNullable<PublicNodeState["budget"]>]))
        && Object.keys(node.budget).length === 4))
    && Array.isArray(view.chains) && view.chains.every((chain) =>
      chain && uuid(chain.chain_run_id) && statuses.includes(chain.status))
    && Array.isArray(view.messages) && view.messages.every((message) =>
      message && uuid(message.visible_message_id) && ["user", "assistant"].includes(message.role)
      && nonnegative(message.sequence)
      && (message.chain_run_id === null || uuid(message.chain_run_id)));
}
