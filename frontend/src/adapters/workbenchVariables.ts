import { clonePreparation } from "../domain/preparation";
import { WorkbenchApiError, workbenchRequest } from "./workbenchApi";
import { isWorkbenchContextScope, type WorkbenchContextScope } from "./workbenchContext";

export type VariableValue = string | number | boolean;
export type VariableType = "string" | "integer" | "number" | "boolean";
export interface SessionVariable {
  name: string;
  type: VariableType;
  assigned: boolean;
  value?: VariableValue;
  source: "default" | "assignment" | "unassigned";
}
export interface WorkbenchVariableRead {
  schema_version: 1;
  kind: "workflow_variable_read";
  workflow_session_id: string;
  revision: number;
  values: SessionVariable[];
}
export interface VariableWriteIdentity {
  idempotency_key: string;
  name: string;
  expected_variable_revision: number;
  value: VariableValue;
}
export interface InlinePromptConfiguration {
  items: unknown[];
  groups: unknown[];
  config: unknown;
}
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const exact = (value: unknown, fields: string[]) =>
  object(value) && Object.keys(value).length === fields.length && fields.every((key) => Object.hasOwn(value, key));
export const variableNamePattern = /^[\p{L}_][\p{L}\p{N}_]*$/u;
export function validVariableValue(value: unknown, type: VariableType): value is VariableValue {
  return type === "string" ? typeof value === "string" && value.length <= 1_000_000
    : type === "boolean" ? typeof value === "boolean"
      : typeof value === "number" && Number.isFinite(value) && (type !== "integer" || Number.isSafeInteger(value));
}
export function decodeWorkbenchVariables(value: unknown, sessionId: string): WorkbenchVariableRead {
  const invalid = () => { throw new WorkbenchApiError("unavailable", "会话变量响应格式或归属无效", undefined, "variable_projection_invalid"); };
  if (!exact(value, ["schema_version", "kind", "workflow_session_id", "revision", "values"])
    || !object(value) || value.schema_version !== 1 || value.kind !== "workflow_variable_read"
    || value.workflow_session_id !== sessionId || !Number.isSafeInteger(value.revision) || (value.revision as number) < 0
    || !Array.isArray(value.values) || value.values.length > 512) return invalid();
  const names = new Set<string>();
  for (const row of value.values) {
    if (!object(row) || typeof row.name !== "string" || row.name.length > 128 || !variableNamePattern.test(row.name)
      || names.has(row.name) || typeof row.assigned !== "boolean"
      || !["string", "integer", "number", "boolean"].includes(row.type as string)
      || !exact(row, ["name", "type", "assigned", "source", ...(row.assigned ? ["value"] : [])])
      || !(row.assigned ? ["default", "assignment"].includes(row.source as string) : row.source === "unassigned")
      || row.assigned && !validVariableValue(row.value, row.type as VariableType)) return invalid();
    names.add(row.name);
  }
  return clonePreparation(value) as unknown as WorkbenchVariableRead;
}
export function decodeWorkbenchVariableWrite(
  value: unknown, sessionId: string, request: VariableWriteIdentity,
): WorkbenchVariableRead {
  const fields = ["schema_version", "kind", "workflow_session_id", "revision", "values", "write_receipt"];
  if (!exact(value, fields) || !object(value)
    || !exact(value.write_receipt, ["idempotency_key", "name", "expected_variable_revision"])
    || !object(value.write_receipt)
    || value.write_receipt.idempotency_key !== request.idempotency_key
    || value.write_receipt.name !== request.name
    || value.write_receipt.expected_variable_revision !== request.expected_variable_revision)
    throw new WorkbenchApiError("unknown", "变量写入回执未对应原请求");
  const { write_receipt: _receipt, ...projection } = value;
  const result = decodeWorkbenchVariables(projection, sessionId);
  const written = result.values.find((row) => row.name === request.name);
  if (result.revision <= request.expected_variable_revision
    || !written?.assigned || written.source !== "assignment" || written.value !== request.value)
    throw new WorkbenchApiError("unknown", "变量写入回执未确认提交的值");
  return result;
}
export function variableReadBody(scope: WorkbenchContextScope, promptConfig: InlinePromptConfiguration) {
  if (!isWorkbenchContextScope(scope))
    throw new WorkbenchApiError("unavailable", "会话变量缺少明确的版本依据");
  return {
    expected_session_revision: scope.sessionRevision,
    expected_ref_revision: scope.refRevision,
    expected_head_commit_id: scope.headCommitId,
    prompt_config: clonePreparation(promptConfig),
  };
}
export async function readWorkbenchVariables(
  scope: WorkbenchContextScope, promptConfig: InlinePromptConfiguration, signal?: AbortSignal,
) {
  return decodeWorkbenchVariables(await workbenchRequest<unknown>(
    `/api/sessions/${encodeURIComponent(scope.sessionId)}/variables/read`,
    { body: variableReadBody(scope, promptConfig), readOnly: true, signal },
  ), scope.sessionId);
}
