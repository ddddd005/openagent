import {
  clonePreparation,
  PREPARATION_LIMITS,
  type CanonicalContextMessage,
  type ContextNodeConfig,
  type ContextReference,
  type PreparationStage,
  type PreparedMaterial,
} from "../domain/preparation";
import { AGENT_BINDINGS, WorkbenchApiError, workbenchRequest } from "./workbenchApi";
import { isBackendPreparationProgram } from "../domain/preparationProgram";

export interface WorkbenchContextScope {
  workflowId: string;
  stage: PreparationStage;
  sessionId: string;
  nodeBindingId: string;
  sessionRevision: number;
  refRevision: number;
  headCommitId: string | null;
}
export interface BackendContextScope {
  workflow_session_id: string;
  node_binding_id: string;
  session_revision: number;
  ref_revision: number;
  head_commit_id: string | null;
  state_snapshot_id: string | null;
  parent_turn_id: string | null;
  selected_chain_run_id: string | null;
}
export interface ProtectedBlockLocator {
  message_id: string;
  block_index: number;
}
export interface ContextTurn {
  workflow_session_id: string;
  node_binding_id: string;
  turn_id: string;
  parent_turn_id: string | null;
  run_id: string;
  input_id: string;
  snapshot_id: string;
  chain_run_id: string;
  root_message_id: string;
  message_ids: string[];
  final_message_id: string;
  result_ports: {
    final: { route_id: string; delivery_id: string };
    context_delta: { route_id: string; delivery_id: string };
  } | null;
}
export interface WorkbenchContextRead {
  schema_version: 1;
  kind: "workflow_context_read";
  scope: BackendContextScope;
  messages: CanonicalContextMessage[];
  logical_floors: string[][];
  protected_blocks: ProtectedBlockLocator[];
  turns: ContextTurn[];
}
export interface ContextPreparationPreview {
  schema_version: 1 | 2;
  kind: "context_preparation";
  node_input: Record<string, unknown>;
  canonical_messages: CanonicalContextMessage[];
  send_view: ContextView;
  display_view: ContextView;
  root_locator: ProtectedBlockLocator;
  logical_floors: string[][];
  protected_blocks: ProtectedBlockLocator[];
  prompt_message_ids: string[];
  s0: CanonicalContextMessage[];
  assembly: {
    schema_version: 1;
    kind: "prompt_assembly";
    messages: CanonicalContextMessage[];
    manifest: Record<string, unknown>;
  };
  evidence_digest: string;
  collection: { schema_version: 1; kind: "prompt_collection"; items: Record<string, unknown>[] };
  variables: Record<string, unknown> | null;
  config: Record<string, unknown>;
  processing: Record<string, unknown>;
  lorebook: unknown;
  context_regex: Record<string, unknown>;
  program?: Record<string, unknown>;
}
export interface ContextView {
  schema_version: 1;
  kind: "context_view";
  workflow_session_id: string;
  node_binding_id: string;
  parent_turn_id: string | null;
  projection_version: 1;
  purpose: "send" | "display";
  messages: CanonicalContextMessage[];
  overrides: { message_id: string; block_index: number; text: string }[];
  protected_blocks: ProtectedBlockLocator[];
}
export type WorkbenchContextPreview = {
  schema_version: 1;
  kind: "workflow_context_preview";
  scope: BackendContextScope;
} & ({
  status: "ready";
  reason_code: null;
  input: {
    kind: "ephemeral";
    input_id: string;
    source: { kind: "visible_message"; visible_message_id: string };
  };
  preparation: ContextPreparationPreview;
} | {
  status: "pending";
  reason_code: "upstream_input_pending";
  input: null;
  preparation: null;
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SYMBOL = /^[A-Za-z][A-Za-z0-9_.-]*$/;
const nullableUuid = (value: unknown) => value === null || uuid(value);
const uuid = (value: unknown): value is string => typeof value === "string" && UUID.test(value);
const integer = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const exact = (value: unknown, required: string[], optional: string[] = []): value is Record<string, unknown> =>
  object(value) && required.every((key) => Object.hasOwn(value, key))
  && Object.keys(value).every((key) => required.includes(key) || optional.includes(key));
function requireValue(condition: unknown, description: string): asserts condition {
  if (!condition) throw new WorkbenchApiError("unavailable", description, undefined, "context_projection_invalid");
}
function freeze<T>(value: T): T {
  if (value && typeof value === "object") {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
function same(left: unknown, right: unknown): boolean {
  const canonical = (value: unknown): unknown => Array.isArray(value)
    ? value.map(canonical) : object(value)
      ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])])) : value;
  return JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
}
function safeJson(value: unknown) {
  let entries = 0;
  let chars = 0;
  function visit(input: unknown, depth: number) {
    requireValue(depth < 64 && ++entries <= 100_000, "上下文结构容量超限");
    if (input === null || typeof input === "boolean") return;
    if (typeof input === "number") {
      requireValue(Number.isFinite(input), "上下文包含非 JSON 数值");
      return;
    }
    if (typeof input === "string") {
      chars += input.length;
      requireValue(chars <= 8_000_000, "上下文响应字符容量超限");
      return;
    }
    requireValue(Array.isArray(input) || object(input), "上下文不是规范 JSON 数据");
    for (const child of Object.values(input)) visit(child, depth + 1);
  }
  visit(value, 0);
}
export function isWorkbenchContextScope(value: unknown): value is WorkbenchContextScope {
  return exact(value, ["workflowId", "stage", "sessionId", "nodeBindingId", "sessionRevision", "refRevision", "headCommitId"])
    && typeof value.workflowId === "string" && !!value.workflowId
    && (value.stage === "A" || value.stage === "B")
    && uuid(value.sessionId) && value.nodeBindingId === AGENT_BINDINGS[value.stage]
    && integer(value.sessionRevision) && value.sessionRevision > 0
    && integer(value.refRevision) && value.refRevision > 0 && uuid(value.headCommitId);
}
function validateScope(value: unknown, expected: WorkbenchContextScope): asserts value is BackendContextScope {
  requireValue(exact(value, [
    "workflow_session_id", "node_binding_id", "session_revision", "ref_revision",
    "head_commit_id", "state_snapshot_id", "parent_turn_id", "selected_chain_run_id",
  ]) && value.workflow_session_id === expected.sessionId && value.node_binding_id === expected.nodeBindingId
    && value.session_revision === expected.sessionRevision && value.ref_revision === expected.refRevision
    && value.head_commit_id === expected.headCommitId
    && nullableUuid(value.state_snapshot_id) && nullableUuid(value.parent_turn_id)
    && nullableUuid(value.selected_chain_run_id), "上下文归属或读取版本不匹配");
}
function validateSource(message: Record<string, unknown>) {
  const source = message.source;
  const v = message.schema_version;
  const role = message.role;
  let valid = false;
  if (v === 3) {
    valid = role !== "tool" && exact(source, ["kind", "prompt_id", "revision", "item_instance_id", "group_instance_id"])
      && source.kind === "prompt" && uuid(source.prompt_id) && integer(source.revision) && source.revision > 0
      && uuid(source.item_instance_id) && nullableUuid(source.group_instance_id);
  } else if (v === 2 && role === "tool") {
    valid = exact(source, ["kind", "tool_call_id", "tool_execution_id", "reason_code"])
      && source.kind === "runtime_tool_observation" && uuid(source.tool_call_id)
      && ["never_started", "interrupted", "outcome_unknown"].includes(source.reason_code as string)
      && (source.reason_code === "never_started" ? source.tool_execution_id === null : uuid(source.tool_execution_id));
  } else if (v === 2 && role === "system") {
    valid = exact(source, ["kind", "closeout_id", "source_run_id"])
      && source.kind === "runtime_execution_observation" && uuid(source.closeout_id) && uuid(source.source_run_id);
  } else if (v === 1 && object(source)) {
    const fields: Record<string, string[]> = {
      human: ["visible_message_id"], upstream_node: ["output_id"], model: ["request_id"],
      tool: ["tool_execution_id"], protocol_feedback: ["request_id"], prompt: ["prompt_id", "revision"],
    };
    const allowed: Record<string, string[]> = {
      system: ["prompt"], user: ["human", "upstream_node", "prompt", "protocol_feedback"],
      assistant: ["model"], tool: ["tool"],
    };
    const kind = source.kind as string;
    const keys = fields[kind];
    valid = !!keys && allowed[role as string]?.includes(kind) === true
      && exact(source, ["kind", ...keys]) && keys.every((key) =>
        key === "revision" ? integer(source[key]) && (source[key] as number) > 0 : uuid(source[key]));
  }
  requireValue(valid, "上下文消息来源或 schema 不兼容");
}
function validateMessages(value: unknown): asserts value is CanonicalContextMessage[] {
  requireValue(Array.isArray(value) && value.length <= PREPARATION_LIMITS.maxMessages, "上下文消息数量超限");
  const ids = new Set<string>();
  const seenCalls = new Set<string>();
  let pending: string[] = [];
  let chars = 0;
  for (const message of value) {
    requireValue(exact(message, ["schema_version", "message_id", "role", "source", "blocks"], ["created_at"])
      && [1, 2, 3].includes(message.schema_version as number) && uuid(message.message_id)
      && !ids.has(message.message_id) && ["system", "user", "assistant", "tool"].includes(message.role as string)
      && (message.created_at === undefined || typeof message.created_at === "string"
        && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(message.created_at))
      && Array.isArray(message.blocks) && message.blocks.length > 0
      && message.blocks.length <= PREPARATION_LIMITS.maxMaterials, "上下文规范消息字段无效");
    ids.add(message.message_id);
    validateSource(message);
    if (message.role !== "tool") requireValue(!pending.length, "工具调用与结果之间插入了消息");
    for (const block of message.blocks) {
      requireValue(object(block), "上下文 block 字段无效");
      if (block.kind === "text") {
        requireValue(message.role !== "tool" && exact(block, ["kind", "text"]) && typeof block.text === "string",
          "上下文文本 block 字段无效");
      } else if (block.kind === "tool_call") {
        requireValue(message.role === "assistant" && message.schema_version === 1
          && exact(block, ["kind", "tool_call_id", "tool_name", "tool_definition_version", "raw_arguments", "parsed_arguments"])
          && uuid(block.tool_call_id) && !seenCalls.has(block.tool_call_id)
          && typeof block.tool_name === "string" && SYMBOL.test(block.tool_name)
          && typeof block.tool_definition_version === "string" && !!block.tool_definition_version
          && typeof block.raw_arguments === "string" && object(block.parsed_arguments), "工具调用 block 字段无效");
        seenCalls.add(block.tool_call_id);
        pending.push(block.tool_call_id);
      } else {
        requireValue(block.kind === "tool_result" && message.role === "tool"
          && exact(block, ["kind", "status", "is_error", "content", "tool_call_id", "tool_execution_id", "model_visible_text"])
          && uuid(block.tool_call_id) && block.tool_call_id === pending.shift()
          && ["success", "error"].includes(block.status as string)
          && block.is_error === (block.status === "error")
          && typeof block.model_visible_text === "string"
          && (message.schema_version === 2
            ? block.status === "error" && (message.source as Record<string, unknown>).tool_call_id === block.tool_call_id
              && block.tool_execution_id === (message.source as Record<string, unknown>).tool_execution_id
              && object(block.content) && block.content.reason_code === (message.source as Record<string, unknown>).reason_code
              && typeof block.content.error === "string" && !!block.content.error
              && typeof block.content.retry_guidance === "string" && !!block.content.retry_guidance
            : uuid(block.tool_execution_id)
              && block.tool_execution_id === (message.source as Record<string, unknown>).tool_execution_id),
        "工具结果 block 字段或协议顺序无效");
      }
      chars += JSON.stringify(block).length;
      requireValue(chars <= PREPARATION_LIMITS.maxTotalChars, "上下文内容容量超限");
    }
  }
  requireValue(!pending.length, "已归档上下文缺少工具结果");
}
function validateProtection(value: unknown, messages: CanonicalContextMessage[]): asserts value is ProtectedBlockLocator[] {
  requireValue(Array.isArray(value) && value.length <= PREPARATION_LIMITS.maxMaterials, "上下文保护定位数量无效");
  const seen = new Set<string>();
  for (const locator of value) {
    requireValue(exact(locator, ["message_id", "block_index"]) && uuid(locator.message_id)
      && integer(locator.block_index), "上下文保护定位字段无效");
    const key = `${locator.message_id}:${locator.block_index}`;
    const message = messages.find((entry) => entry.message_id === locator.message_id);
    requireValue(!seen.has(key) && message?.blocks[locator.block_index]?.kind === "text",
      "上下文保护定位不存在或重复");
    seen.add(key);
  }
}
function validateFloors(value: unknown, messages: CanonicalContextMessage[], currentRoot: boolean): asserts value is string[][] {
  requireValue(Array.isArray(value) && value.length <= PREPARATION_LIMITS.maxMessages
    && value.length % 2 === (currentRoot ? 1 : 0)
    && value.every((floor) => Array.isArray(floor) && floor.length > 0 && floor.every(uuid))
    && JSON.stringify(value.flat()) === JSON.stringify(messages.map((message) => message.message_id)),
  "上下文楼层未按顺序完整覆盖消息");
  let offset = 0;
  for (const [index, floor] of value.entries()) {
    const group = messages.slice(offset, offset + floor.length);
    offset += floor.length;
    requireValue(index % 2 === 0
      ? group.length === 1 && group[0].role === "user" && ["human", "upstream_node"].includes(group[0].source.kind as string)
      : group.every((message) => ["model", "tool", "protocol_feedback", "runtime_tool_observation"].includes(message.source.kind as string)),
    "上下文 root/delta 来源不属于对应楼层");
    validateMessages(group);
  }
}
function validateTurns(value: unknown, read: Omit<WorkbenchContextRead, "turns">): asserts value is ContextTurn[] {
  requireValue(Array.isArray(value) && value.length * 2 === read.logical_floors.length, "上下文归档轮次与楼层不匹配");
  let parent: string | null = null;
  const seen = new Set<string>();
  for (const [index, turn] of value.entries()) {
    requireValue(exact(turn, [
      "workflow_session_id", "node_binding_id", "turn_id", "parent_turn_id", "run_id",
      "input_id", "snapshot_id", "chain_run_id", "root_message_id", "message_ids", "final_message_id", "result_ports",
    ]) && uuid(turn.workflow_session_id) && turn.node_binding_id === read.scope.node_binding_id
      && ["turn_id", "run_id", "input_id", "snapshot_id", "chain_run_id", "root_message_id", "final_message_id"].every((key) => uuid(turn[key]))
      && !seen.has(turn.turn_id as string) && turn.parent_turn_id === parent
      && turn.root_message_id === read.logical_floors[index * 2][0]
      && JSON.stringify(turn.message_ids) === JSON.stringify(read.logical_floors[index * 2 + 1])
      && Array.isArray(turn.message_ids) && turn.message_ids.includes(turn.final_message_id), "上下文归档轮次来源或父链不匹配");
    const finalMessage = read.messages.find((message) => message.message_id === turn.final_message_id)!;
    requireValue(finalMessage.role === "assistant" && finalMessage.source.kind === "model"
      && finalMessage.blocks.every((block, blockIndex) => block.kind !== "text"
        || read.protected_blocks.some((locator) => locator.message_id === finalMessage.message_id && locator.block_index === blockIndex)),
    "上下文最终消息缺少明确保护定位");
    if (turn.result_ports !== null) {
      requireValue(exact(turn.result_ports, ["final", "context_delta"]), "上下文结果端口定位无效");
      for (const part of ["final", "context_delta"])
        requireValue(exact(turn.result_ports[part], ["route_id", "delivery_id"])
          && turn.result_ports[part].route_id === `fixed-node-${part === "final" ? "final" : "context"}:${read.scope.node_binding_id}`
          && turn.result_ports[part].delivery_id === `result-${part === "final" ? "final" : "context"}:${turn.run_id}`,
        "上下文结果端口来源无效");
    }
    parent = turn.turn_id as string;
    seen.add(parent);
  }
  requireValue(parent === read.scope.parent_turn_id, "上下文所选父链头不匹配");
}
export function decodeWorkbenchContextRead(value: unknown, expected: WorkbenchContextScope): WorkbenchContextRead {
  requireValue(isWorkbenchContextScope(expected), "上下文读取目标无效");
  safeJson(value);
  requireValue(exact(value, ["schema_version", "kind", "scope", "messages", "logical_floors", "protected_blocks", "turns"])
    && value.schema_version === 1 && value.kind === "workflow_context_read", "上下文读取响应版本无效");
  validateScope(value.scope, expected);
  validateMessages(value.messages);
  validateFloors(value.logical_floors, value.messages, false);
  validateProtection(value.protected_blocks, value.messages);
  const read = value as unknown as WorkbenchContextRead;
  validateTurns(value.turns, read);
  return freeze(clonePreparation(read));
}
export function mapWorkbenchContextRead(
  read: WorkbenchContextRead, target: WorkbenchContextScope, nodeId: string,
): ContextNodeConfig {
  const reference: ContextReference = {
    workflowId: target.workflowId, stage: target.stage, workflowSessionId: read.scope.workflow_session_id,
    nodeBindingId: read.scope.node_binding_id, selectedChainId: read.scope.selected_chain_run_id,
    basis: {
      sessionRevision: read.scope.session_revision, refRevision: read.scope.ref_revision,
      headCommitId: read.scope.head_commit_id, stateSnapshotId: read.scope.state_snapshot_id,
      parentTurnId: read.scope.parent_turn_id,
    },
  };
  const messages = new Map(read.messages.map((message) => [message.message_id, message]));
  const protectedIds = new Set(read.protected_blocks.map((locator) => locator.message_id));
  read.turns.forEach((turn) => protectedIds.add(turn.final_message_id));
  let declarationIndex = 0;
  const floors = read.logical_floors.map((ids, index) => {
    const turn = read.turns[Math.floor(index / 2)];
    const kind = index % 2 === 0 ? "root" as const : "delta" as const;
    const id = `${turn.turn_id}:${kind}`;
    const items: PreparedMaterial[] = [];
    for (const messageId of ids) {
      const canonical = freeze(clonePreparation(messages.get(messageId)!));
      const context = {
        reference: clonePreparation(reference), floorId: id, floorKind: kind, messageId,
        canonicalMessage: canonical, turnId: turn.turn_id, runId: turn.run_id,
        inputId: turn.input_id, snapshotId: turn.snapshot_id, chainId: turn.chain_run_id,
        workflowSessionId: turn.workflow_session_id,
      };
      const editable = !protectedIds.has(messageId) && canonical.blocks.every((block) => block.kind === "text")
        && (canonical.role === "user" && ["human", "upstream_node"].includes(canonical.source.kind as string)
          || canonical.role === "assistant" && canonical.source.kind === "model");
      if (editable) {
        for (const [blockIndex, block] of canonical.blocks.entries()) items.push({
          id: `${messageId}:${blockIndex}`, name: `${kind} / ${canonical.role}`,
          role: canonical.role, presentation: null, declarationIndex: declarationIndex++,
          payload: { kind: "text", text: block.text as string },
          source: { kind: "context", nodeId, context: { ...context, blockIndex } },
        });
      } else items.push({
        id: messageId, name: `${kind} / ${canonical.role}`, role: canonical.role,
        presentation: null, declarationIndex: declarationIndex++,
        payload: freeze({
          kind: "protected-message", message: {
            messageId, role: canonical.role, source: canonical.source,
            blocks: canonical.blocks, canonical,
          },
        }),
        source: { kind: "context", nodeId, context },
      });
    }
    return { id, kind, items };
  });
  requireValue(floors.reduce((sum, floor) => sum + floor.items.length, 0) <= PREPARATION_LIMITS.maxMaterials,
    "上下文材料数量超限");
  return { source: "backend-read", reference, floors };
}
function validateView(value: unknown, purpose: "send" | "display", preparation: ContextPreparationPreview, scope: BackendContextScope) {
  requireValue(exact(value, [
    "schema_version", "kind", "workflow_session_id", "node_binding_id", "parent_turn_id",
    "projection_version", "purpose", "messages", "overrides", "protected_blocks",
  ]) && value.schema_version === 1 && value.kind === "context_view" && value.purpose === purpose
    && value.projection_version === 1 && value.workflow_session_id === scope.workflow_session_id
    && value.node_binding_id === scope.node_binding_id && value.parent_turn_id === scope.parent_turn_id
    && JSON.stringify(value.messages) === JSON.stringify(preparation.canonical_messages)
    && JSON.stringify(value.protected_blocks) === JSON.stringify(preparation.protected_blocks)
    && Array.isArray(value.overrides), "服务端预览消息视图不匹配");
  if (preparation.schema_version === 1 || purpose === "display")
    requireValue(value.overrides.length === 0, "受限服务端预览不能修改消息视图");
  const byId = new Map(preparation.canonical_messages.map((message) => [message.message_id, message]));
  const protectedTargets = new Set(preparation.protected_blocks.map((target) => `${target.message_id}:${target.block_index}`));
  const overrides = new Map<string, string>();
  requireValue(value.overrides.length <= PREPARATION_LIMITS.maxMaterials, "上下文派生数量超限");
  for (const override of value.overrides) {
    requireValue(exact(override, ["message_id", "block_index", "text"])
      && uuid(override.message_id) && integer(override.block_index) && typeof override.text === "string",
    "上下文派生文本定位无效");
    const key = `${override.message_id}:${override.block_index}`;
    const message = byId.get(override.message_id);
    const editable = message && (message.role === "user" && ["human", "upstream_node"].includes(message.source.kind as string)
      || message.role === "assistant" && message.source.kind === "model");
    requireValue(!overrides.has(key) && !protectedTargets.has(key) && editable && message
      && message.blocks.every((block) => block.kind === "text") && message.blocks[override.block_index]?.kind === "text",
    "上下文派生修改了受保护内容");
    overrides.set(key, override.text);
  }
  return preparation.canonical_messages.map((message) => ({
    ...clonePreparation(message),
    blocks: message.blocks.map((block, index) => overrides.has(`${message.message_id}:${index}`)
      ? { ...clonePreparation(block), text: overrides.get(`${message.message_id}:${index}`)! } : clonePreparation(block)),
  }));
}
export function decodeWorkbenchContextPreview(
  value: unknown,
  expected: WorkbenchContextScope,
  request?: { text: string | null; promptConfig: { items: unknown[]; groups: unknown[]; config: unknown } },
): WorkbenchContextPreview {
  requireValue(isWorkbenchContextScope(expected), "上下文预览目标无效");
  safeJson(value);
  requireValue(exact(value, ["schema_version", "kind", "scope", "status", "reason_code", "input", "preparation"])
    && value.schema_version === 1 && value.kind === "workflow_context_preview", "上下文预览响应版本无效");
  validateScope(value.scope, expected);
  if (value.status === "pending") {
    requireValue(expected.stage === "B" && value.reason_code === "upstream_input_pending"
      && value.input === null && value.preparation === null, "待生成的上游输入不能伪造为装配结果");
    return freeze(clonePreparation(value as unknown as WorkbenchContextPreview));
  }
  requireValue(expected.stage === "A" && value.status === "ready" && value.reason_code === null
    && exact(value.input, ["kind", "input_id", "source"]) && value.input.kind === "ephemeral" && uuid(value.input.input_id)
    && exact(value.input.source, ["kind", "visible_message_id"]) && value.input.source.kind === "visible_message"
    && uuid(value.input.source.visible_message_id), "上下文预览输入不是临时用户输入");
  const input = value.input;
  const inputSource = input.source as { kind: "visible_message"; visible_message_id: string };
  const advanced = object(value.preparation) && value.preparation.schema_version === 2;
  requireValue(exact(value.preparation, [
    "schema_version", "kind", "node_input", "canonical_messages", "send_view", "display_view",
    "collection", "variables", "config", "root_locator", "logical_floors", "protected_blocks",
    "prompt_message_ids", "assembly", "processing", "lorebook", "context_regex", "s0", "evidence_digest",
    ...(advanced ? ["program"] : []),
  ]) && [1, 2].includes(value.preparation.schema_version as number) && value.preparation.kind === "context_preparation",
  "上下文装配证据版本无效");
  const preparation = value.preparation as unknown as ContextPreparationPreview;
  validateMessages(preparation.canonical_messages);
  validateMessages(preparation.s0);
  validateFloors(preparation.logical_floors, preparation.canonical_messages, true);
  validateProtection(preparation.protected_blocks, preparation.canonical_messages);
  requireValue(exact(preparation.root_locator, ["message_id", "block_index"]) && uuid(preparation.root_locator.message_id)
    && preparation.root_locator.block_index === 0 && exact(preparation.node_input, [
      "schema_version", "input_id", "port_id",
      "payload_schema_ref", "source", "payload",
    ], ["created_at"]) && preparation.node_input.schema_version === 1
    && preparation.node_input.input_id === input.input_id
    && preparation.node_input.port_id === "request"
    && exact(preparation.node_input.payload_schema_ref, ["schema_id", "version"])
    && preparation.node_input.payload_schema_ref.schema_id === "writing_text"
    && preparation.node_input.payload_schema_ref.version === 1
    && exact(preparation.node_input.payload, ["text"])
    && typeof preparation.node_input.payload.text === "string"
    && same(preparation.node_input.source, input.source),
  "临时输入或规范根来源不匹配");
  const root = preparation.canonical_messages.at(-1);
  requireValue(root?.message_id === preparation.root_locator.message_id && root?.role === "user"
    && root?.source.kind === "human" && root?.source.visible_message_id === inputSource.visible_message_id
    && root?.blocks.length === 1 && root?.blocks[0].kind === "text"
    && typeof root?.blocks[0].text === "string", "服务端规范根消息不匹配");
  let rootValue: unknown;
  try { rootValue = JSON.parse(root.blocks[0].text as string); } catch { rootValue = undefined; }
  requireValue(same(rootValue, preparation.node_input.payload), "服务端规范根未保留 schema 输入包装");
  if (request) requireValue(preparation.node_input.payload.text === request.text
    && same(preparation.config.prompt_config, request.promptConfig),
  "服务端预览与当前输入或提示词配置不对应");
  const derivedContext = validateView(preparation.send_view, "send", preparation, value.scope);
  validateView(preparation.display_view, "display", preparation, value.scope);
  requireValue(exact(preparation.assembly, ["schema_version", "kind", "messages", "manifest"])
    && preparation.assembly.schema_version === 1 && preparation.assembly.kind === "prompt_assembly"
    && JSON.stringify(preparation.assembly.messages) === JSON.stringify(preparation.s0)
    && object(preparation.assembly.manifest)
    && exact(preparation.collection, ["schema_version", "kind", "items"])
    && preparation.collection.schema_version === 1 && preparation.collection.kind === "prompt_collection"
    && Array.isArray(preparation.collection.items) && preparation.collection.items.every(object)
    && (preparation.variables === null || object(preparation.variables))
    && object(preparation.config) && object(preparation.processing) && object(preparation.context_regex)
    && typeof preparation.evidence_digest === "string" && /^json-v1:sha256:[0-9a-f]{64}$/.test(preparation.evidence_digest)
    && Array.isArray(preparation.prompt_message_ids) && preparation.prompt_message_ids.every(uuid),
  "服务端装配消息与证据不匹配");
  const prompts = new Set(preparation.prompt_message_ids);
  requireValue(prompts.size === preparation.prompt_message_ids.length
    && preparation.s0.filter((message) => prompts.has(message.message_id)).length === prompts.size
    && preparation.s0.every((message) => prompts.has(message.message_id)
      ? message.schema_version === 3 && message.source.kind === "prompt"
      : derivedContext.some((canonical) => same(canonical, message)))
    && JSON.stringify(preparation.s0.filter((message) => !prompts.has(message.message_id)))
      === JSON.stringify(derivedContext), "装配改变了未声明上下文或新增了未声明消息");
  if (advanced) requireValue(exact(preparation.program, [
    "schema_version", "kind", "program", "basis_state", "state", "stages", "collection", "view", "evidence_digest",
  ]) && preparation.program.schema_version === 1 && preparation.program.kind === "preparation_program_result"
    && preparation.config.schema_version === 3 && isBackendPreparationProgram(preparation.config.preparation)
    && object(preparation.config.preparation_state)
    && same(preparation.program.program, preparation.config.preparation)
    && same(preparation.program.basis_state, preparation.config.preparation_state)
    && same(preparation.program.collection, preparation.collection) && same(preparation.program.view, preparation.send_view)
    && Array.isArray(preparation.program.stages) && object(preparation.program.state)
    && typeof preparation.program.evidence_digest === "string"
    && /^json-v1:sha256:[0-9a-f]{64}$/.test(preparation.program.evidence_digest),
  "高级预览缺少匹配的处理配置或执行证据");
  return freeze(clonePreparation(value as unknown as WorkbenchContextPreview));
}
function casBody(scope: WorkbenchContextScope) {
  requireValue(isWorkbenchContextScope(scope), "上下文读取目标缺少明确版本依据");
  return {
    expected_session_revision: scope.sessionRevision, expected_ref_revision: scope.refRevision,
    expected_head_commit_id: scope.headCommitId,
  };
}
function contextPath(scope: WorkbenchContextScope, operation: string) {
  return `/api/sessions/${encodeURIComponent(scope.sessionId)}/nodes/${encodeURIComponent(scope.nodeBindingId)}/context/${operation}`;
}
export async function readWorkbenchContext(scope: WorkbenchContextScope, signal?: AbortSignal) {
  const value = await workbenchRequest<unknown>(contextPath(scope, "read"), {
    body: casBody(scope), readOnly: true, signal,
  });
  return decodeWorkbenchContextRead(value, scope);
}
export async function previewWorkbenchContext(
  scope: WorkbenchContextScope,
  promptConfig: { items: unknown[]; groups: unknown[]; config: unknown },
  text: string | null,
  signal?: AbortSignal,
) {
  requireValue(scope.stage === "A" ? typeof text === "string" : text === null, "当前输入不属于所选 Agent");
  const value = await workbenchRequest<unknown>(contextPath(scope, "preview"), {
    body: { ...casBody(scope), prompt_config: promptConfig, text }, readOnly: true, signal,
  });
  return decodeWorkbenchContextPreview(value, scope, { text, promptConfig });
}
