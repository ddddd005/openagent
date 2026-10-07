import { graphObject } from "./workflowGraph";

export interface MaintenanceSummary { title: string; lines: string[] }

function capacityLine(value: unknown): string | null {
  if (!graphObject(value) || !["input_tokens", "output_reserve_tokens", "total_tokens", "context_window_tokens"]
    .every(key => Number.isSafeInteger(value[key]) && Number(value[key]) >= 0)) return null;
  const basis = value.token_count_kind === "utf8_bytes_estimate" ? "UTF-8 字节估算" : "提供的计数";
  return `输入 ${value.input_tokens} + 输出预留 ${value.output_reserve_tokens} = ${value.total_tokens} / ${value.context_window_tokens}（${basis}）`;
}

function usageLine(value: unknown): string {
  return graphObject(value) ? `供应商 usage：${JSON.stringify(value)}` : "供应商 usage：未提供";
}

export function contextMaintenanceSummary(item: unknown): MaintenanceSummary | null {
  if (graphObject(item) && graphObject(item.payload)
    && typeof item.payload.kind === "string" && graphObject(item.payload.payload)) {
    if (item.payload.schema_version === 1) item = item.payload;
    else if (item.payload.schema_version === undefined && graphObject(item.executor_ref)
      && typeof item.executor_ref.executor_id === "string" && typeof item.fact_id === "string"
      && graphObject(item.owner) && Number.isSafeInteger(item.generation) && Number(item.generation) > 0
      && typeof item.payload.created_at === "string") {
      // The versioned information page wraps an unversioned public fact body.
      item = { ...item.payload, schema_version: 1 };
    }
  }
  if (!graphObject(item) || item.schema_version !== 1 || !graphObject(item.payload)) return null;
  const payload = item.payload;
  if (item.kind === "context_compaction_started") {
    const capacity = capacityLine(payload.capacity);
    return { title: "上下文精简开始", lines: [
      ...(Array.isArray(payload.covered_message_ids) ? [`本次覆盖 ${payload.covered_message_ids.length} 条消息`] : []),
      ...(capacity ? [capacity] : []),
    ] };
  }
  if (item.kind === "context_compaction_finished") {
    return { title: payload.outcome === "responded" ? "上下文精简响应已收到" : "上下文精简未完成",
      lines: [`结果：${String(payload.outcome ?? "未知")}`, usageLine(graphObject(payload.result) ? payload.result.usage : null)] };
  }
  if (item.kind === "context_compaction_applied") {
    const before = graphObject(payload.capacity) ? capacityLine(payload.capacity.before) : null;
    const after = graphObject(payload.capacity) ? capacityLine(payload.capacity.after) : null;
    return { title: "工作上下文已精简", lines: [
      ...(before ? [`精简前：${before}`] : []), ...(after ? [`精简后：${after}`] : []),
      usageLine(graphObject(payload.result) ? payload.result.usage : null),
    ] };
  }
  if (item.kind === "model_attempt_finished") {
    return { title: "模型调用结果", lines: [`结果：${String(payload.outcome ?? "未知")}`, usageLine(payload.usage)] };
  }
  return null;
}

const diagnoses: Record<string, string> = {
  context_capacity_exceeded: "上下文已到安全水位，Agent 已停止。",
  context_compaction_no_eligible_text: "没有可精简的长期文本；当前输入、固定提示词和工具协议保持不变。",
  context_compaction_request_capacity_exceeded: "精简请求本身超过容量限制，未发起摘要调用。",
  context_compaction_cold_input_budget_exceeded: "精简请求超过冷输入 tokens 上限，未发起摘要调用。",
  context_compaction_summary_invalid: "摘要响应无效，未替换上下文。",
  context_compaction_insufficient: "精简后仍超出安全水位，Agent 已停止。",
  model_capacity_invalid: "模型容量配置无效，请核实窗口及输出预留。",
  model_capacity_unknown: "模型上下文容量尚未配置。",
  model_output_reserve_unknown: "模型输出上限或输出预留尚未配置。",
  context_stale_basis: "上下文写回依据已过期，未覆盖现有上下文。",
};

export function contextMaintenanceDiagnosis(value: unknown): string | null {
  if (!graphObject(value)) return null;
  const code = value.reason_code ?? value.code;
  return typeof code === "string" ? diagnoses[code] ?? null : null;
}
