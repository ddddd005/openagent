import { workbenchRequest, WorkbenchApiError } from "./workbenchApi";
import { graphClone, graphObject, graphUuid } from "../domain/workflowGraph";

function sameValue(left: unknown, right: unknown): boolean {
  if (Array.isArray(left) || Array.isArray(right))
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length
      && left.every((item, index) => sameValue(item, right[index]));
  if (graphObject(left) || graphObject(right))
    return graphObject(left) && graphObject(right) && Object.keys(left).length === Object.keys(right).length
      && Object.keys(left).every(key => Object.prototype.hasOwnProperty.call(right, key) && sameValue(left[key], right[key]));
  return left === right;
}
export function graphApplicationQuery(operation: string, parameters: Record<string, unknown> = {}): Promise<unknown> {
  return workbenchRequest("/api/graph/queries", { body: { operation, parameters: graphClone(parameters) }, readOnly: true });
}

// Preserve persisted outbox coordinates for current operations; retired writes
// stay rejected before any transport while exact old receipts remain readable.
function graphRequestOperation(path: string, body: Record<string, unknown>, receiptOnly = false) {
  if (Object.prototype.hasOwnProperty.call(body, "session_id"))
    throw new WorkbenchApiError("unknown", "原请求包含与路径重复的会话目标，保留原请求");
  if (path === "/api/graph/definitions")
    throw new WorkbenchApiError("unavailable",
      "旧整图保存入口已退役；原保存回执不可再核实，原记录保留",
      undefined, "graph_definition_save_retired");
  const roots: Record<string, string> = {
    "/api/graph/definitions/update": "definition.update",
    "/api/graph/sessions": "session.create",
    "/api/graph/resources/save": "resource.save",
    "/api/graph/content/metadata": "content.metadata.update",
  };
  if (roots[path]) return { operation: roots[path], parameters: graphClone(body) };
  const definition = /^\/api\/graph\/definitions\/([0-9a-fA-F-]+)\/(archive|delete)$/.exec(path);
  if (definition && graphUuid(definition[1]))
    return { operation: `definition.${definition[2]}`, parameters: { ...graphClone(body), identity: definition[1] } };
  const session = /^\/api\/graph\/sessions\/([0-9a-fA-F-]+)\/(runs|control|copy|rebind|archive|delete|data|events\/submit|candidates\/select|candidates\/fork)$/.exec(path);
  if (session && !receiptOnly && ["copy", "rebind", "candidates/select", "candidates/fork"].includes(session[2])) {
    const messages: Record<string, string> = {
      copy: "跨工作流会话数据复制入口已退役；新工作流不继承运行数据",
      rebind: "会话修订重绑入口已退役；请新建会话",
    };
    throw new WorkbenchApiError("rejected",
      messages[session[2]] ?? "候选历史恢复与分叉入口已退役；未提交新请求");
  }
  const operations: Record<string, string> = {
    runs: "run.start", control: "run.control", copy: "session.copy", rebind: "session.rebind",
    data: "session.data.update", "candidates/select": "candidate.select", "candidates/fork": "candidate.fork",
    "events/submit": "event.submit",
    archive: "session.archive", delete: "session.delete",
  };
  if (!session || !graphUuid(session[1]))
    throw new WorkbenchApiError("unknown", "原工作流请求没有对应应用操作，保留原请求");
  return { operation: operations[session[2]], parameters: { ...graphClone(body), session_id: session[1] } };
}

export function graphCommandOperation(path: string, body: Record<string, unknown>) {
  return graphRequestOperation(path, body);
}

export async function sendGraphCommand(path: string, body: Record<string, unknown>): Promise<unknown> {
  const request = graphCommandOperation(path, body);
  if (request.operation === "session.data.update")
    throw new WorkbenchApiError("rejected", "旧变量会话数据写入入口已退役；未提交新请求");
  const value = await workbenchRequest<unknown>("/api/graph/commands", { body: request });
  return validateGraphCommandResult(value, request);
}

export async function readGraphReceipt(path: string, body: Record<string, unknown>): Promise<unknown> {
  const request = graphRequestOperation(path, body, true);
  const value = await workbenchRequest<unknown>("/api/graph/receipts/read", { body: request, readOnly: true });
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.application-receipt-read")
    throw new WorkbenchApiError("unknown", "原工作流回执读取契约无效，保留原请求");
  if (value.outcome === "unresolved") {
    if (Object.keys(value).length !== 4 || typeof value.reason_code !== "string"
      || !/^[A-Za-z0-9_]{1,128}$/.test(value.reason_code))
      throw new WorkbenchApiError("unknown", "原工作流回执读取契约无效，保留原请求");
    throw new WorkbenchApiError("unknown", `原工作流请求仍待核实 [${value.reason_code}]`, undefined, value.reason_code);
  }
  if (value.outcome !== "matched" || value.reason_code !== "receipt_matched" || Object.keys(value).length !== 6
    || !Object.prototype.hasOwnProperty.call(value, "result") || !graphObject(value.receipt)
    || Object.keys(value.receipt).length !== 9)
    throw new WorkbenchApiError("unknown", "原工作流回执读取契约无效，保留原请求");
  return validateGraphCommandResult({
    schema_version: 1, kind: "workflow.application-command", receipt: value.receipt, result: value.result,
  }, request);
}

function validateGraphCommandResult(value: unknown, request: ReturnType<typeof graphCommandOperation>): unknown {
  const receipt = graphObject(value) && graphObject(value.receipt) ? value.receipt : null;
  const target = request.parameters.session_id ? { session_id: request.parameters.session_id } : {};
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.application-command"
    || !receipt || receipt.schema_version !== 1 || receipt.kind !== "workflow.application-receipt"
    || receipt.operation !== request.operation || receipt.operation_scope !== "management"
    || receipt.idempotency_key !== request.parameters.idempotency_key || receipt.authority !== "service_receipt"
    || typeof receipt.request_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(receipt.request_sha256)
    || !graphObject(receipt.target) || JSON.stringify(receipt.target) !== JSON.stringify(target)
    || !graphObject(receipt.accepted) || !Object.prototype.hasOwnProperty.call(value, "result"))
    throw new WorkbenchApiError("unknown", "应用命令回执与原请求不匹配，保留原请求");
  const result = value.result;
  const evidence = graphObject(result) && graphObject(result.session) ? result.session : result;
  const evidenceFields = ["workflow_definition_id", "definition_revision", "workflow_session_id",
    "revision", "data_revision", "head_revision", "head_commit_id", "status", "active_chain_run_id",
    "selected_chain_run_id", "reference", "update_sequence", "deleted", "package_lock"];
  const accepted = graphObject(evidence) ? Object.fromEntries(evidenceFields.filter(field => field in evidence)
    .map(field => [field, evidence[field]])) : {};
  if (graphObject(evidence) && evidence.kind === "workflow.configuration-result") {
    for (const key of ["outcome", "source_workflow_definition_id", "edit_sequence",
      "source_has_effective_session", "changed_categories", "node_id_map", "session_id"])
      accepted[key] = evidence[key];
  }
  if (graphObject(evidence) && Object.keys(evidence).length === 3
    && "reference" in evidence && "display_name" in evidence && "metadata_sequence" in evidence) {
    accepted.display_name = evidence.display_name;
    accepted.metadata_sequence = evidence.metadata_sequence;
  }
  if (graphObject(evidence) && evidence.workflow_definition_id !== undefined
    && evidence.workflow_session_id === undefined && evidence.revision !== undefined)
    accepted.definition_revision = evidence.revision;
  if (!graphObject(evidence) || Object.keys(accepted).length === 0
    || Object.keys(receipt.accepted).length !== Object.keys(accepted).length
    || !sameValue(receipt.accepted, accepted))
    throw new WorkbenchApiError("unknown", "应用回执接纳依据与操作结果不匹配，保留原请求");
  // The server's json-v1 digest uses Python number spelling; it is not a browser-produced idempotency digest.
  return result;
}
