import { vi } from "vitest";
import { isWorkflowDefinitionSummary, isWorkflowSessionSummary, sessionSummaryFromView,
  type WorkflowDefinitionSummary, type WorkflowSessionSummary } from "../domain/workflowDirectory";
import { isGraphConfigurationDocument } from "../domain/workflowConfiguration";
import { graphObject } from "../domain/workflowGraph";

function applicationCommand(operation: string, parameters: Record<string, unknown>, result: Record<string, unknown>) {
  const evidence = (result.session ?? result) as Record<string, unknown>;
  const fields = ["workflow_definition_id", "definition_revision", "workflow_session_id", "revision",
    "data_revision", "head_revision", "head_commit_id", "status", "active_chain_run_id",
    "selected_chain_run_id", "reference", "update_sequence", "deleted", "package_lock"];
  const accepted = Object.fromEntries(fields.filter(field => field in evidence).map(field => [field, evidence[field]]));
  if (evidence.workflow_definition_id !== undefined && evidence.workflow_session_id === undefined
    && evidence.revision !== undefined) accepted.definition_revision = evidence.revision;
  if (evidence.kind === "workflow.session-metadata") {
    accepted.name = evidence.name;
    accepted.metadata_sequence = evidence.metadata_sequence;
  }
  if (Object.keys(evidence).length === 3 && "reference" in evidence
    && "display_name" in evidence && "metadata_sequence" in evidence) {
    accepted.display_name = evidence.display_name;
    accepted.metadata_sequence = evidence.metadata_sequence;
  }
  return {
    schema_version: 1, kind: "workflow.application-command", result,
    receipt: { schema_version: 1, kind: "workflow.application-receipt", operation, operation_scope: "management",
      idempotency_key: parameters.idempotency_key, request_sha256: "a".repeat(64), authority: "service_receipt",
      target: parameters.session_id ? { session_id: parameters.session_id } : {}, accepted },
  };
}

// Only explicit frozen evidence fixtures may satisfy a receipt read; no handler mutation is invoked.
export function graphReceiptReadResponse(operation: string, parameters: Record<string, unknown>,
  result: object): Response {
  const value = applicationCommand(operation, parameters, result as Record<string, unknown>);
  return new Response(JSON.stringify({
    schema_version: 1, kind: "workflow.application-receipt-read", outcome: "matched",
    reason_code: "receipt_matched", receipt: value.receipt, result: value.result,
  }));
}

function definitionSummaryRow(row: unknown): WorkflowDefinitionSummary | null {
  if (isWorkflowDefinitionSummary(row)) return row;
  if (!graphObject(row)) return null;
  const wrapper = graphObject(row.document) ? row : null;
  const document = wrapper ? wrapper.document : row;
  if (!graphObject(document) || typeof document.workflow_definition_id !== "string"
    || typeof document.name !== "string" || !Number.isSafeInteger(document.revision)
    || !Array.isArray(document.nodes)) return null;
  const editSequence = typeof document.edit_sequence === "number" ? document.edit_sequence
    : isGraphConfigurationDocument(document) ? document.edit_sequence : null;
  return {
    workflow_definition_id: String(document.workflow_definition_id), display_name: document.name,
    fixed_definition_revision: Number(document.revision), edit_sequence: editSequence,
    archived: wrapper ? wrapper.archived === true : false, node_count: document.nodes.length,
    session_count: 0, has_current_configuration: editSequence !== null,
  };
}
function sessionSummaryRow(row: unknown): WorkflowSessionSummary | null {
  if (isWorkflowSessionSummary(row)) return row;
  if (!graphObject(row) || typeof row.workflow_session_id !== "string"
    || typeof row.workflow_definition_id !== "string"
    || !Number.isSafeInteger(row.definition_revision) || !Number.isSafeInteger(row.revision)
    || typeof row.status !== "string") return null;
  return sessionSummaryFromView(row as unknown as Parameters<typeof sessionSummaryFromView>[0]);
}
/** Translate legacy full-document/full-view fixtures into the summary contract. */
async function summaryResponse(response: Response, kind: "definition" | "session"): Promise<Response> {
  if (!response.ok) return response;
  let rows: unknown;
  try { rows = await response.clone().json(); } catch { return response; }
  const list = Array.isArray(rows) ? rows : null;
  if (list === null) return response;
  const mapped = list.map(row => kind === "definition" ? definitionSummaryRow(row) : sessionSummaryRow(row));
  if (mapped.some(row => row === null)) return response;
  return new Response(JSON.stringify(mapped), { status: response.status });
}

// Existing service fixtures describe the original authorities. This fake application
// server translates named requests to those fixtures and adds the real receipt shape.
export function stubGraphApplicationFetch(handler: (...arguments_: any[]) => any) {
  vi.stubGlobal("fetch", async (path: string, init?: RequestInit) => {
    if (path !== "/api/graph/queries" && path !== "/api/graph/commands") return handler(path, init);
    const { operation, parameters } = JSON.parse(String(init?.body));
    if (path === "/api/graph/commands"
      && ["session.copy", "session.rebind", "candidate.select", "candidate.fork"].includes(operation))
      throw new Error(`Retired ${operation} writes are not available in fixtures`);
    const sid = parameters.session_id;
    const routes: Record<string, string> = {
      "catalog.node-types": `/api/graph/node-types${parameters.protocol_version === 2 ? "/v2" : ""}`,
      "definition.read": `/api/graph/definitions/${parameters.identity}${parameters.revision ? `/revisions/${parameters.revision}` : ""}`,
      "definition.list": "/api/graph/definitions",
      "definition.summaries": "/api/graph/definitions",
      "definition.sessions": `/api/graph/definitions/${parameters.workflow_definition_id}/sessions`,
      "session.summaries": `/api/graph/definitions/${parameters.workflow_definition_id}/sessions`,
      "session.read": `/api/graph/sessions/${sid}`,
      "run.read": `/api/graph/sessions/${sid}/runs/${parameters.chain_id}`,
      "definition.archive": `/api/graph/definitions/${parameters.identity}/archive`,
      "definition.delete": `/api/graph/definitions/${parameters.identity}/delete`,
      "session.archive": `/api/graph/sessions/${sid}/archive`,
      "session.delete": `/api/graph/sessions/${sid}/delete`,
      "session.metadata.read": `/api/graph/sessions/${sid}/metadata`,
      "session.metadata.update": `/api/graph/sessions/${sid}/metadata`,
      "session.create": "/api/graph/sessions", "run.start": `/api/graph/sessions/${sid}/runs`,
      "run.control": `/api/graph/sessions/${sid}/control`,
      "session.data.update": `/api/graph/sessions/${sid}/data`,
    };
    if (!routes[operation]) throw new Error(`Fixture has no application operation: ${operation}`);
    const query = path === "/api/graph/queries", { session_id: _sid, ...body } = parameters;
    const response: Response = await handler(routes[operation], {
      ...init, method: query ? "GET" : "POST", body: query ? undefined : JSON.stringify(body),
    });
    if (query) {
      if (operation === "definition.summaries") return summaryResponse(response, "definition");
      if (operation === "session.summaries") return summaryResponse(response, "session");
      return response;
    }
    if (!response.ok) return response;
    let result: Record<string, unknown>;
    try { result = await response.clone().json(); } catch { return response; }
    return new Response(JSON.stringify(applicationCommand(operation, parameters, result)), { status: response.status });
  });
}
