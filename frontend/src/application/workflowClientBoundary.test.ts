import { afterEach, describe, expect, it, vi } from "vitest";
import { graphApplicationQuery, readGraphReceipt, sendGraphCommand } from "../adapters/workflowApplicationApi";
import { WorkbenchApiError } from "../adapters/workbenchApi";
import {
  graphClone, newGraph, type GraphEntry, type GraphSession,
} from "../domain/workflowGraph";
import {
  createGraphCommand, dispatchGraphCommand, type GraphCommandPorts,
} from "./workflowCommands";

type StartedSession = GraphSession & { active_chain_run_id: string };

function fixture() {
  const document = newGraph("Plugin workflow");
  const session: StartedSession = {
    schema_version: 2, execution_model: "graph",
    workflow_definition_id: document.workflow_definition_id,
    workflow_session_id: crypto.randomUUID(),
    definition_revision: 1, revision: 2, data_revision: 0, head_revision: 1,
    status: "running", can_submit: false,
    nodes: [], chains: [], outputs: [], data: { revision: 0, values: {} },
    active_chain_run_id: crypto.randomUUID(),
  };
  const entry: GraphEntry = {
    document, saved_document: graphClone(document), saved_revision: 1,
    session_id: session.workflow_session_id, pending: null,
  };
  const command = createGraphCommand(`/api/graph/sessions/${session.workflow_session_id}/runs`, {
    expected_revision: 1, inputs: { event: { type: "plugin.input", payload: ["original"] } },
  }, "start");
  return { document, entry, session, command };
}

function envelope(session: StartedSession, key: unknown) {
  return {
    schema_version: 1, kind: "workflow.application-command",
    receipt: {
      schema_version: 1, kind: "workflow.application-receipt",
      operation: "run.start", operation_scope: "management",
      idempotency_key: key, request_sha256: "a".repeat(64),
      authority: "service_receipt", target: { session_id: session.workflow_session_id },
      accepted: {
        workflow_definition_id: session.workflow_definition_id,
        definition_revision: session.definition_revision,
        workflow_session_id: session.workflow_session_id,
        revision: session.revision, data_revision: session.data_revision,
        head_revision: session.head_revision, status: session.status,
        active_chain_run_id: session.active_chain_run_id,
      },
    },
    result: graphClone(session),
  };
}

function ports(overrides: Partial<GraphCommandPorts> = {}): GraphCommandPorts {
  return {
    persist: () => true,
    request: sendGraphCommand,
    readReceipt: readGraphReceipt,
    readDefinition: async () => { throw new Error("No definition lookup expected"); },
    current: () => true,
    accept: vi.fn(),
    ...overrides,
  };
}

const response = (value: unknown) => new Response(JSON.stringify(value));
afterEach(() => { vi.unstubAllGlobals(); });

describe("workflow client transport boundary", () => {
  it("reads the frozen outbox after an unknown submission without dispatching another command", async () => {
    const { entry, session, command } = fixture();
    const original = graphClone(command), sent: unknown[] = [], persisted: unknown[] = [];
    let attempts = 0;
    vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
      expect(path).toBe(attempts === 0 ? "/api/graph/commands" : "/api/graph/receipts/read");
      expect(init.method).toBe("POST");
      sent.push(JSON.parse(String(init.body)));
      if (++attempts === 1) throw new Error("Response lost after submission");
      const value = envelope(session, original.body.idempotency_key);
      return response({ schema_version: 1, kind: "workflow.application-receipt-read", outcome: "matched",
        reason_code: "receipt_matched", receipt: value.receipt, result: value.result });
    }));
    const boundary = ports({ persist: () => {
      persisted.push(graphClone(entry.pending)); return true;
    } });
    await expect(dispatchGraphCommand(entry, command, boundary)).rejects.toMatchObject({ kind: "unknown" });
    expect(entry.pending).toEqual(original);
    expect(persisted[0]).toEqual(original);
    command.body.inputs = { event: { payload: ["later caller mutation"] } };
    command.body.idempotency_key = crypto.randomUUID();
    entry.external_inputs = { event: { payload: ["later editor draft"] } };
    await dispatchGraphCommand(entry, entry.pending!, boundary, true);
    const expected = {
      operation: "run.start",
      parameters: { ...original.body, session_id: session.workflow_session_id },
    };
    expect(sent).toEqual([expected, expected]);
    expect(boundary.accept).toHaveBeenCalledTimes(1);
    expect(entry.pending).toBeNull();
  });

  it("does not submit until the complete original outbox is persisted", async () => {
    const { entry, command } = fixture();
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    await expect(dispatchGraphCommand(entry, command, ports({ persist: () => false })))
      .rejects.toMatchObject({ kind: "unavailable" });
    expect(fetcher).not.toHaveBeenCalled();
    expect(entry.pending).toBeNull();
  });

  it("does not expose an empty service result as an accepted command", async () => {
    const { session, command } = fixture();
    const original = envelope(session, command.body.idempotency_key);
    const invalid = {
      ...original, result: {}, receipt: { ...original.receipt, accepted: {} },
    };
    vi.stubGlobal("fetch", vi.fn(async () => response(invalid)));
    await expect(sendGraphCommand(command.path, command.body)).rejects.toMatchObject({ kind: "unknown" });
  });

  it.each([
    ["operation", (value: ReturnType<typeof envelope>) => { value.receipt.operation = "run.control"; }],
    ["key", (value: ReturnType<typeof envelope>) => { value.receipt.idempotency_key = "other-operation"; }],
    ["target", (value: ReturnType<typeof envelope>) => { value.receipt.target.session_id = crypto.randomUUID(); }],
    ["authority", (value: ReturnType<typeof envelope>) => { value.receipt.authority = "service_result"; }],
    ["accepted result", (value: ReturnType<typeof envelope>) => { value.receipt.accepted.revision++; }],
    ["digest format", (value: ReturnType<typeof envelope>) => { value.receipt.request_sha256 = "not-a-digest"; }],
  ])("retains the original request when the %s evidence disagrees", async (_label, change) => {
    const { entry, session, command } = fixture();
    const result = envelope(session, command.body.idempotency_key);
    change(result);
    vi.stubGlobal("fetch", vi.fn(async () => response(result)));
    const boundary = ports();
    await expect(dispatchGraphCommand(entry, command, boundary)).rejects.toMatchObject({ kind: "unknown" });
    expect(boundary.accept).not.toHaveBeenCalled();
    expect(entry.pending).toEqual(command);
  });

  it("does not accept a valid command receipt into a view whose session basis expired", async () => {
    const { entry, session, command } = fixture();
    vi.stubGlobal("fetch", vi.fn(async () => response(envelope(session, command.body.idempotency_key))));
    const boundary = ports({ current: () => false });
    await expect(dispatchGraphCommand(entry, command, boundary)).rejects.toMatchObject({ kind: "unknown" });
    expect(boundary.accept).not.toHaveBeenCalled();
    expect(entry.pending).toEqual(command);
  });

  it("retains the exact request after receipt acceptance if local confirmation cannot be saved", async () => {
    const { entry, session, command } = fixture();
    let saves = 0;
    vi.stubGlobal("fetch", vi.fn(async () => response(envelope(session, command.body.idempotency_key))));
    const boundary = ports({ persist: () => ++saves === 1 });
    await expect(dispatchGraphCommand(entry, command, boundary)).rejects.toMatchObject({ kind: "unknown" });
    expect(boundary.accept).toHaveBeenCalledTimes(1);
    expect(entry.pending).toEqual(command);
  });

  it.each([
    ["/api/graph/sessions/not-a-uuid/runs", { expected_revision: 1, idempotency_key: "original" }],
    ["/api/graph/sessions/other/runs", { expected_revision: 1, idempotency_key: "original" }],
    ["/api/graph/sessions", { session_id: crypto.randomUUID(), idempotency_key: "original" }],
    [`/api/graph/sessions/${crypto.randomUUID()}/runs`, {
      session_id: crypto.randomUUID(), expected_revision: 1, idempotency_key: "original",
    }],
  ])("leaves malformed frozen coordinates unresolved before network dispatch: %s", async (path, body) => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    const original = graphClone(body);
    await expect(sendGraphCommand(path, body)).rejects.toMatchObject({ kind: "unknown" });
    expect(body).toEqual(original);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("treats POST queries as reads and keeps exact owner, generation and opaque cursor coordinates", async () => {
    const parameters = {
      session_id: crypto.randomUUID(),
      reference: { source_id: "plugin.ordinary-source", exact_version: "7" },
      owner: {
        workflow_session_id: crypto.randomUUID(), chain_run_id: crypto.randomUUID(),
        node_binding_id: crypto.randomUUID(), node_run_id: crypto.randomUUID(),
      },
      generation: 4, source_scope: "history", limit: 8, cursor: "provider:opaque-position",
    };
    const original = graphClone(parameters), sent: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
      expect(path).toBe("/api/graph/queries");
      expect(init.method).toBe("POST");
      sent.push(JSON.parse(String(init.body)));
      throw new Error("Read disconnected");
    }));
    await expect(graphApplicationQuery("information.read", parameters))
      .rejects.toMatchObject({ kind: "unavailable" });
    expect(sent).toEqual([{ operation: "information.read", parameters: original }]);
    expect(parameters).toEqual(original);
  });

  it.each([404, 409, 410, 500])("keeps the original pending after a receipt reader fails with HTTP %s", async status => {
    const { entry, command } = fixture(); entry.pending = graphClone(command);
    const before = graphClone(entry);
    const boundary = ports({
      request: vi.fn(), persist: vi.fn(),
      readReceipt: vi.fn(async () => { throw new WorkbenchApiError("rejected", "read failed", status, "stale_revision"); }),
    });
    await expect(dispatchGraphCommand(entry, command, boundary, true)).rejects.toMatchObject({ status });
    expect(entry).toEqual(before); expect(boundary.request).not.toHaveBeenCalled();
    expect(boundary.persist).not.toHaveBeenCalled(); expect(boundary.accept).not.toHaveBeenCalled();
  });

  it.each(["false", "throw"])("retains the original receipt-read request on local confirmation failure: %s", async failure => {
    const { entry, command, session } = fixture(); entry.pending = graphClone(command);
    const boundary = ports({
      request: vi.fn(), readReceipt: vi.fn(async () => session),
      persist: () => { if (failure === "throw") throw new Error("disk"); return false; },
    });
    await expect(dispatchGraphCommand(entry, command, boundary, true)).rejects.toMatchObject({ kind: "unknown" });
    expect(entry.pending).toEqual(command); expect(boundary.accept).toHaveBeenCalledOnce();
    expect(boundary.request).not.toHaveBeenCalled();
  });

  it.each(["body", "key", "cleared", "rejected"])(
    "does not let a late receipt read overwrite a newer pending state: %s", async change => {
      const { entry, command, session } = fixture(); entry.pending = graphClone(command);
      let finish!: (value: unknown) => void, reject!: (failure: unknown) => void;
      const boundary = ports({ request: vi.fn(), persist: vi.fn(),
        readReceipt: () => new Promise((resolve, fail) => { finish = resolve; reject = fail; }) });
      const reading = dispatchGraphCommand(entry, command, boundary, true);
      const replacement = graphClone(command);
      if (change === "key" || change === "rejected") replacement.body.idempotency_key = crypto.randomUUID();
      else replacement.body.inputs = { original: "newer request with same key" };
      entry.pending = change === "cleared" ? null : replacement;
      entry.diagnostics = [{ reason_code: "newer", message: "retain newer state" }];
      const newer = graphClone(entry);
      if (change === "rejected") reject(new WorkbenchApiError("rejected", "old read rejection", 409));
      else finish(session);
      await expect(reading).rejects.toBeInstanceOf(WorkbenchApiError);
      expect(entry).toEqual(newer); expect(boundary.accept).not.toHaveBeenCalled();
      expect(boundary.persist).not.toHaveBeenCalled(); expect(boundary.request).not.toHaveBeenCalled();
    });

  it("does not restore the older outbox over a newer one when confirmation persistence fails", async () => {
    const { entry, command, session } = fixture(); entry.pending = graphClone(command);
    const newer = graphClone(command); newer.body.idempotency_key = crypto.randomUUID();
    const boundary = ports({ request: vi.fn(), readReceipt: async () => session,
      persist: () => { entry.pending = graphClone(newer); return false; } });
    await expect(dispatchGraphCommand(entry, command, boundary, true)).rejects.toMatchObject({ kind: "unknown" });
    expect(entry.pending).toEqual(newer); expect(boundary.request).not.toHaveBeenCalled();
  });

  it("does not manufacture a receipt-read request when neither pending nor a retained migration coordinate exists", async () => {
    const { entry, command } = fixture(), boundary = ports({ request: vi.fn(), readReceipt: vi.fn(), persist: vi.fn() });
    await expect(dispatchGraphCommand(entry, command, boundary, true)).rejects.toMatchObject({ kind: "unknown" });
    expect(entry.pending).toBeNull(); expect(boundary.readReceipt).not.toHaveBeenCalled();
    expect(boundary.request).not.toHaveBeenCalled(); expect(boundary.persist).not.toHaveBeenCalled();
  });

  it("clears an initial definitive rejection but keeps an initial identity conflict unresolved", async () => {
    for (const code of ["stale_revision", "idempotency_conflict"]) {
      const { entry, command } = fixture();
      const boundary = ports({ request: async () => { throw new WorkbenchApiError("rejected", code, 409, code); },
        readReceipt: vi.fn() });
      await expect(dispatchGraphCommand(entry, command, boundary)).rejects.toMatchObject({ code });
      expect(entry.pending).toEqual(code === "stale_revision" ? null : command);
      expect(boundary.readReceipt).not.toHaveBeenCalled();
    }
  });
  it("retains exact evidence only for a definitive copy rejection and removes it before an unknown retry", async () => {
    const { entry, command } = fixture();
    command.action = "copy";
    command.path = command.path.replace("/runs", "/copy");
    command.body.document = graphClone(entry.document);
    const original = graphClone(command);
    await expect(dispatchGraphCommand(entry, command, ports({
      request: async () => { throw new WorkbenchApiError("rejected", "stale copy", 409, "stale_revision"); },
    }))).rejects.toMatchObject({ code: "stale_revision" });
    expect(entry.pending).toBeNull(); expect(entry.rejected_copy).toEqual(original);
    command.body.idempotency_key = crypto.randomUUID();
    await expect(dispatchGraphCommand(entry, command, ports({
      request: async () => { throw new WorkbenchApiError("unknown", "retry response lost"); },
    }))).rejects.toMatchObject({ kind: "unknown" });
    expect(entry.pending).toEqual(command); expect(entry.rejected_copy).toBeUndefined();
  });

  it("does not lose definitive copy rejection evidence when a new outbox cannot be saved before submission", async () => {
    const { entry, command } = fixture();
    command.action = "copy"; command.path = command.path.replace("/runs", "/copy");
    command.body.document = graphClone(entry.document);
    const rejected = graphClone(command); entry.rejected_copy = rejected;
    command.body.idempotency_key = crypto.randomUUID();
    const boundary = ports({ persist: () => false, request: vi.fn() });
    await expect(dispatchGraphCommand(entry, command, boundary)).rejects.toMatchObject({ kind: "unavailable" });
    expect(entry.pending).toBeNull(); expect(entry.rejected_copy).toEqual(rejected);
    expect(boundary.request).not.toHaveBeenCalled();
  });
});
