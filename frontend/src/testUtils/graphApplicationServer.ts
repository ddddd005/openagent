import { vi } from "vitest";

// Existing service fixtures describe the original authorities. This fake application
// server translates named requests to those fixtures and adds the real receipt shape.
export function stubGraphApplicationFetch(handler: (...arguments_: any[]) => any) {
  vi.stubGlobal("fetch", async (path: string, init?: RequestInit) => {
    if (path !== "/api/graph/queries" && path !== "/api/graph/commands") return handler(path, init);
    const { operation, parameters } = JSON.parse(String(init?.body));
    const sid = parameters.session_id;
    const routes: Record<string, string> = {
      "catalog.node-types": `/api/graph/node-types${parameters.protocol_version === 2 ? "/v2" : ""}`,
      "definition.read": `/api/graph/definitions/${parameters.identity}${parameters.revision ? `/revisions/${parameters.revision}` : ""}`,
      "definition.sessions": `/api/graph/definitions/${parameters.workflow_definition_id}/sessions`,
      "session.read": `/api/graph/sessions/${sid}`,
      "run.read": `/api/graph/sessions/${sid}/runs/${parameters.chain_id}`,
      "archive.read": `/api/graph/sessions/${sid}/archives/${parameters.archive_id}`,
      "candidate.list": `/api/graph/sessions/${sid}/candidates`,
      "definition.save": "/api/graph/definitions", "legacy.migrate": "/api/graph/migrations",
      "session.create": "/api/graph/sessions", "run.start": `/api/graph/sessions/${sid}/runs`,
      "run.control": `/api/graph/sessions/${sid}/control`, "session.copy": `/api/graph/sessions/${sid}/copy`,
      "session.rebind": `/api/graph/sessions/${sid}/rebind`, "session.data.update": `/api/graph/sessions/${sid}/data`,
      "candidate.select": `/api/graph/sessions/${sid}/candidates/select`,
      "candidate.fork": `/api/graph/sessions/${sid}/candidates/fork`,
    };
    if (!routes[operation]) throw new Error(`Fixture has no application operation: ${operation}`);
    const query = path === "/api/graph/queries", { session_id: _sid, ...body } = parameters;
    const response: Response = await handler(routes[operation], {
      ...init, method: query ? "GET" : "POST", body: query ? undefined : JSON.stringify(body),
    });
    if (query || !response.ok) return response;
    let result: Record<string, unknown>;
    try { result = await response.clone().json(); } catch { return response; }
    const evidence = (result.session ?? result) as Record<string, unknown>;
    const fields = ["workflow_definition_id", "definition_revision", "workflow_session_id", "revision",
      "data_revision", "head_revision", "head_commit_id", "status", "active_chain_run_id",
      "selected_chain_run_id", "reference", "update_sequence", "deleted", "package_lock"];
    const accepted = Object.fromEntries(fields.filter(field => field in evidence).map(field => [field, evidence[field]]));
    if (evidence.workflow_definition_id !== undefined && evidence.workflow_session_id === undefined
      && evidence.revision !== undefined) accepted.definition_revision = evidence.revision;
    return new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.application-command", result,
      receipt: { schema_version: 1, kind: "workflow.application-receipt", operation, operation_scope: "management",
        idempotency_key: parameters.idempotency_key, request_sha256: "a".repeat(64), authority: "service_receipt",
        target: sid ? { session_id: sid } : {}, accepted },
    }), { status: response.status });
  });
}
