import { clonePreparation } from "../domain/preparation";
import { isPublicSession, WorkbenchApiError, workbenchRequest, type PublicSession } from "./workbenchApi";
import { isExactModelSelection, type ExactModelSelection } from "../domain/modelConfiguration";
import { validObservation } from "../domain/observation";
import { isBackendPreparationProgram } from "../domain/preparationProgram";
import { isWorkflowIdentity } from "../domain/workflowIdentity";
import { exactSessionObject, safeSessionJson, sessionInteger, sessionObject, sessionUuid } from "../domain/sessionWire";
export { exactSessionObject, safeSessionJson, sessionInteger, sessionObject, sessionUuid } from "../domain/sessionWire";

export interface SessionSummary {
  workflow_session_id: string;
  created_at: string | null;
  mode: "offline" | "deepseek";
  workflow_id?: string;
}
export interface SessionSelection {
  active_workflow_session_id: string | null;
  revision: number;
}
export interface ReplyCandidate {
  candidate_id: string;
  chain_run_id: string;
  payload: unknown;
  selected: boolean;
}
export interface HistoryMessage {
  visible_message_id: string;
  role: "user" | "assistant";
  payload: unknown;
  sequence: number;
  chain_run_id: string | null;
  reply_candidates?: ReplyCandidate[];
}
export interface WorkbenchSession extends PublicSession {
  model_selection?: ExactModelSelection | null;
  messages: HistoryMessage[];
  can_reroll?: boolean;
  can_select_candidates?: boolean;
  can_close_execution?: boolean;
  can_continue_workflow?: boolean;
  recovery_chain_run_id?: string | null;
  pending_input_id?: string | null;
}
export interface ExactPromptSelection {
  schema_version: 1;
  kind: "workflow_prompt_selection";
  nodes: {
    A: { config_id: string; revision: number };
    B: { config_id: string; revision: number };
  };
}
function invalid(description: string): never {
  throw new WorkbenchApiError("unavailable", description);
}
export function isExactPromptSelection(value: unknown): value is ExactPromptSelection {
  return exactSessionObject(value, ["schema_version", "kind", "nodes"])
    && value.schema_version === 1 && value.kind === "workflow_prompt_selection"
    && exactSessionObject(value.nodes, ["A", "B"]) && ["A", "B"].every((stage) => {
      const ref = (value.nodes as Record<string, unknown>)[stage];
      return exactSessionObject(ref, ["config_id", "revision"])
        && sessionUuid(ref.config_id) && sessionInteger(ref.revision) && ref.revision > 0;
    });
}
export function decodeSessionSelection(value: unknown): SessionSelection {
  if (!exactSessionObject(value, ["active_workflow_session_id", "revision"])
    || !sessionInteger(value.revision)
    || (value.active_workflow_session_id === null ? value.revision !== 0 : !sessionUuid(value.active_workflow_session_id)))
    invalid("后端当前会话选择校验失败");
  return clonePreparation(value as unknown as SessionSelection);
}
export function decodeSessionList(value: unknown): SessionSummary[] {
  if (!Array.isArray(value) || value.length > 10_000) invalid("会话列表数量或结构无效");
  const seen = new Set<string>();
  for (const row of value) {
    if (!exactSessionObject(row, ["workflow_session_id", "created_at", "mode"], ["workflow_id"])
      || row.workflow_id !== undefined && !isWorkflowIdentity(row.workflow_id)
      || !sessionUuid(row.workflow_session_id) || seen.has(row.workflow_session_id)
      || !["offline", "deepseek"].includes(row.mode as string)
      || row.created_at !== null && (typeof row.created_at !== "string"
        || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(row.created_at)))
      invalid("会话列表身份、模式或日期无效");
    seen.add(row.workflow_session_id);
  }
  return clonePreparation(value);
}
export function decodeWorkbenchSession(value: unknown, sessionId: string): WorkbenchSession {
  if (!isPublicSession(value, sessionId) || !safeSessionJson(value)) invalid("会话公开投影校验失败");
  const extended = value as WorkbenchSession;
  if (extended.observation !== undefined && !validObservation(extended.observation, extended))
    invalid("节点观察的身份、版本或公开字段无效");
  if (extended.model_selection !== undefined && extended.model_selection !== null
    && !isExactModelSelection(extended.model_selection)) invalid("会话确切模型选择无效");
  for (const field of ["can_reroll", "can_select_candidates", "can_close_execution", "can_continue_workflow"] as const)
    if (extended[field] !== undefined && typeof extended[field] !== "boolean") invalid("会话候选动作标志无效");
  for (const field of ["recovery_chain_run_id", "pending_input_id"] as const)
    if (extended[field] !== undefined && extended[field] !== null && !sessionUuid(extended[field]))
      invalid("会话恢复目标身份无效");
  const messageIds = new Set<string>();
  const candidateIds = new Set<string>();
  for (const message of extended.messages) {
    if (messageIds.has(message.visible_message_id)) invalid("会话消息身份重复");
    messageIds.add(message.visible_message_id);
    if (message.reply_candidates === undefined) continue;
    if (message.role !== "assistant" || !Array.isArray(message.reply_candidates)
      || !message.reply_candidates.length || message.reply_candidates.length > 512) invalid("回复候选楼层结构无效");
    let selected = 0;
    for (const candidate of message.reply_candidates) {
      if (!exactSessionObject(candidate, ["candidate_id", "chain_run_id", "payload", "selected"])
        || !sessionUuid(candidate.candidate_id) || candidateIds.has(candidate.candidate_id)
        || !sessionUuid(candidate.chain_run_id) || typeof candidate.selected !== "boolean"
        || !safeSessionJson(candidate.payload)) invalid("回复候选身份或内容无效");
      candidateIds.add(candidate.candidate_id);
      if (candidate.selected) selected++;
    }
    if (selected !== 1) invalid("回复候选缺少唯一当前选中结果");
    const current = message.reply_candidates.find((candidate) => candidate.selected)!;
    const canonical = (input: unknown): unknown => Array.isArray(input) ? input.map(canonical)
      : sessionObject(input) ? Object.fromEntries(Object.keys(input).sort().map(key => [key, canonical(input[key])])) : input;
    if (current.chain_run_id !== message.chain_run_id || JSON.stringify(canonical(current.payload)) !== JSON.stringify(canonical(message.payload)))
      invalid("当前候选与正式消息投影不对应");
  }
  return clonePreparation(extended);
}
export function validCompiledPromptRecord(record: unknown, kind: string): boolean {
  if (!sessionObject(record) || !(record.schema_version === 1 || kind === "config" && record.schema_version === 2) || record.kind !== kind
    || !sessionUuid(record[`${kind}_id`]) || !sessionInteger(record.revision) || record.revision < 1
    || typeof record.name !== "string" || !record.name.trim()) return false;
  const revisionRef = (value: Record<string, unknown>) => sessionInteger(value.revision) && value.revision > 0;
  if (kind === "item") return exactSessionObject(record, [
    "schema_version", "kind", "item_id", "revision", "name", "text", "role", "enabled",
    "placement", "depth", "order", "interpolation", "source",
  ]) && typeof record.text === "string" && ["system", "user", "assistant"].includes(record.role as string)
    && typeof record.enabled === "boolean" && ["before", "middle", "after"].includes(record.placement as string)
    && (record.placement === "middle" ? sessionInteger(record.depth) : record.depth === null)
    && Number.isSafeInteger(record.order) && record.interpolation === "literal"
    && exactSessionObject(record.source, ["kind"]) && record.source.kind === "configuration";
  if (kind === "group") return exactSessionObject(record, ["schema_version", "kind", "group_id", "revision", "name", "members"])
    && Array.isArray(record.members) && record.members.length <= 512 && record.members.every((member) =>
      exactSessionObject(member, ["item_instance_id", "item_id", "revision", "overrides"])
      && sessionUuid(member.item_instance_id) && sessionUuid(member.item_id) && revisionRef(member)
      && exactSessionObject(member.overrides, []));
  if (kind !== "config" || !exactSessionObject(record, ["schema_version", "kind", "config_id", "revision", "name", "inputs",
    ...(record.schema_version === 2 ? ["preparation"] : [])])
    || record.schema_version === 2 && !isBackendPreparationProgram(record.preparation)
    || !Array.isArray(record.inputs) || record.inputs.length > 512) return false;
  const names = new Set<string>();
  const instances = new Set<string>();
  return record.inputs.every((input) => {
    if (!sessionObject(input) || typeof input.name !== "string" || !input.name || names.has(input.name)) return false;
    names.add(input.name);
    const instance = input.kind === "item" ? input.item_instance_id : input.group_instance_id;
    if (!sessionUuid(instance) || instances.has(instance) || !revisionRef(input)) return false;
    instances.add(instance);
    return input.kind === "item"
      ? exactSessionObject(input, ["name", "kind", "item_instance_id", "item_id", "revision", "overrides"])
        && sessionUuid(input.item_id) && exactSessionObject(input.overrides, [])
      : input.kind === "group" && exactSessionObject(input, ["name", "kind", "group_instance_id", "group_id", "revision", "enabled", "member_overrides"])
        && sessionUuid(input.group_id) && typeof input.enabled === "boolean"
        && Array.isArray(input.member_overrides) && input.member_overrides.length === 0;
  });
}
export function sessionHistoryText(payload: unknown): string {
  return sessionObject(payload) && typeof payload.text === "string" ? payload.text : JSON.stringify(payload, null, 2) ?? "";
}
export async function listWorkbenchSessions(signal?: AbortSignal) {
  return decodeSessionList(await workbenchRequest<unknown>("/api/sessions", { signal }));
}
export async function readSessionSelection(signal?: AbortSignal) {
  return decodeSessionSelection(await workbenchRequest<unknown>("/api/active-session", { signal }));
}
export async function readWorkbenchSession(sessionId: string, signal?: AbortSignal) {
  if (!sessionUuid(sessionId)) invalid("会话身份无效");
  return decodeWorkbenchSession(await workbenchRequest<unknown>(`/api/sessions/${sessionId}`, { signal }), sessionId);
}
