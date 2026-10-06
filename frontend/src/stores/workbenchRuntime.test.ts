import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkbenchRuntimeStore, createPublication, primaryControl, validRuntimeSnapshot } from "./workbenchRuntime";
import { usePreparationStore } from "./preparation";
import { compilePromptItems } from "../domain/preparation";
import { createPreparationDraft } from "../fixtures/preparation";
import { EMPTY_WORKFLOW_ID, MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { AGENT_BINDINGS, type PublicSession } from "../adapters/workbenchApi";
import { useWorkspaceStore } from "./workspace";

const SID = "00000000-0000-4000-8000-000000000001";
const RUN = "00000000-0000-4000-8000-000000000002";
const CHAIN = "00000000-0000-4000-8000-000000000003";
function view(status = "idle", actions: string[] = []): PublicSession {
  const idle = status === "idle";
  return {
    workflow_session_id: SID, revision: 1, mode: "offline", can_submit: idle,
    available_actions: actions,
    nodes: [
      { node_binding_id: AGENT_BINDINGS.A, label: "A", status, run_id: idle ? null : RUN, revision: idle ? null : 1 },
      { node_binding_id: AGENT_BINDINGS.B, label: "B", status: "idle", run_id: null, revision: null },
      { node_binding_id: "7be319b8-30bd-4674-b7bf-d1cf54a1a10a", label: "Output", status: "idle", run_id: null, revision: null },
    ],
    chains: idle ? [] : [{ chain_run_id: CHAIN, status }], messages: [],
  };
}
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
function transport(inputBehavior?: (body: Record<string, unknown>) => Response | Promise<Response>) {
  let accepted = false;
  return vi.fn(async (path: string, options?: RequestInit) => {
    if (path === "/api/health") return json({ mode: "offline" });
    if (path.startsWith("/api/prompt-configs/")) {
      if (options?.method === "GET") return json({ error: { code: "not_found" } }, 404);
      const body = JSON.parse(options?.body as string);
      return json({
        record: body.record,
        head: {
          kind: body.record.kind, id: body.record.item_id ?? body.record.group_id ?? body.record.config_id,
          definition_revision: 1, catalog_revision: 1, selectable: true,
        },
      }, 201);
    }
    if (path === "/api/sessions") return options?.method === "GET"
      ? json([{ workflow_session_id: SID, created_at: null, mode: "offline" }]) : json(view(), 201);
    if (path === `/api/sessions/${SID}`) return json(accepted ? view("succeeded") : view());
    if (path.endsWith("/inputs")) {
      accepted = true;
      const body = JSON.parse(options?.body as string);
      return inputBehavior ? await inputBehavior(body)
        : json({ workflow_session_id: SID, chain_run_id: CHAIN, visible_message_id: SID, input_id: RUN, status: "prepared" }, 202);
    }
    return json({ workflow_session_id: SID, run_id: RUN, chain_run_id: CHAIN, status: "paused" }, 202);
  });
}
beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
});
const SECOND_SID = "00000000-0000-4000-8000-000000000004";
const HEAD = "00000000-0000-4000-8000-000000000005";
const CANDIDATE = "00000000-0000-4000-8000-000000000006";
const ALTERNATIVE = "00000000-0000-4000-8000-000000000007";
const ALT_CHAIN = "00000000-0000-4000-8000-000000000008";
const USER_MESSAGE = "00000000-0000-4000-8000-000000000009";
const REPLY_MESSAGE = "00000000-0000-4000-8000-000000000010";
function historyView(sid = SID) {
  return {
    ...view("succeeded"), workflow_session_id: sid, can_submit: true,
    ref_revision: 2, head_commit_id: HEAD, can_reroll: true, can_select_candidates: true,
    messages: [
      { visible_message_id: USER_MESSAGE, role: "user", payload: { text: "request" }, sequence: 1, chain_run_id: CHAIN },
      {
        visible_message_id: REPLY_MESSAGE, role: "assistant", payload: { text: "selected" }, sequence: 2, chain_run_id: CHAIN,
        reply_candidates: [
          { candidate_id: CANDIDATE, chain_run_id: CHAIN, payload: { text: "selected" }, selected: true },
          { candidate_id: ALTERNATIVE, chain_run_id: ALT_CHAIN, payload: { text: "alternative" }, selected: false },
        ],
      },
    ],
  };
}
describe("workbench session history and durable request identities", () => {
  it("rereads successful chain closeout until the next input unlocks without another POST", async () => {
    const closing = { ...historyView(), can_submit: false, available_actions: ["retry_publish"] };
    const ready = historyView();
    const fetcher = vi.fn().mockResolvedValueOnce(json(closing)).mockResolvedValueOnce(json(ready));
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.restoreSession(SID);
    expect(runtime.control.action).toBe("waiting");
    expect(runtime.locked).toBe(true);
    await vi.advanceTimersByTimeAsync(1200);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(runtime.control.action).toBe("start");
    expect(runtime.locked).toBe(false);
    await vi.advanceTimersByTimeAsync(3600);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls.every(([, options]) => options.method === "GET")).toBe(true);
  });
  it.each(["paused", "failed", "pending-input"])("stops terminal polling at %s", async kind => {
    const projection = kind === "pending-input"
      ? { ...historyView(), can_submit: false, pending_input_id: RUN }
      : view(kind, kind === "paused" ? ["resume"] : []);
    const fetcher = vi.fn().mockResolvedValue(json(projection));
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.restoreSession(SID);
    await vi.advanceTimersByTimeAsync(3600);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][1].method).toBe("GET");
  });
  it("restores observation-only sessions and selects using authoritative selection revision", async () => {
    const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
      if (path === "/api/active-session" && options?.method === "GET") return json({ active_workflow_session_id: SID, revision: 4 });
      if (path === "/api/active-session") return json({ active_workflow_session_id: SECOND_SID, revision: 5 });
      return json(historyView(SECOND_SID));
    });
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.selectSession(SECOND_SID);
    const mutation = fetcher.mock.calls.find(([path, options]) => path === "/api/active-session" && options?.method === "POST")!;
    expect(JSON.parse(mutation[1]?.body as string)).toMatchObject({
      workflow_session_id: SECOND_SID, expected_selection_revision: 4,
    });
    expect(runtime.sessionId).toBe(SECOND_SID);
    expect(fetcher.mock.calls.some(([path]) => path.endsWith("/inputs"))).toBe(false);
  });
  it("creates a session without dispatch and then switches backend selection explicitly", async () => {
    const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
      if (path === "/api/health") return json({ mode: "offline" });
      if (path === "/api/sessions" && options?.method === "POST") return json({ ...view(), workflow_session_id: SECOND_SID }, 201);
      if (path === "/api/sessions") return json([{ workflow_session_id: SECOND_SID, created_at: null, mode: "offline" }]);
      if (path === "/api/active-session" && options?.method === "GET") return json({ active_workflow_session_id: SID, revision: 1 });
      if (path === "/api/active-session") return json({ active_workflow_session_id: SECOND_SID, revision: 2 });
      return json({ ...view(), workflow_session_id: SECOND_SID });
    });
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.createSession();
    expect(runtime.sessionId).toBe(SECOND_SID);
    expect(runtime.sessions).toHaveLength(1);
    expect(fetcher.mock.calls.some(([path]) => path.endsWith("/inputs"))).toBe(false);
    expect(validRuntimeSnapshot(runtime.exportRuntimeSnapshot())).toBe(true);
  });
  it("preserves another session's unknown request on switching and snapshot restoration without views", async () => {
    let reject!: (failure: Error) => void;
    const fetcher = vi.fn(async (path: string) => {
      if (path === "/api/health") return json({ mode: "offline" });
      if (path === `/api/sessions/${SECOND_SID}`) return json(historyView(SECOND_SID));
      if (path === `/api/sessions/${SID}`) return json(view("paused", ["resume"]));
      return await new Promise<Response>((_, failure) => { reject = failure; });
    });
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.runtimes[MAIN_WORKFLOW_ID].sessionId = SID;
    runtime.runtimes[MAIN_WORKFLOW_ID].view = view("paused", ["resume"]);
    const submitting = runtime.submitPrimary();
    await vi.waitFor(() => expect(reject).toBeDefined());
    await runtime.restoreSession(SECOND_SID);
    reject(new Error("lost response"));
    await submitting;
    expect(runtime.sessionId).toBe(SECOND_SID);
    expect(runtime.unknown).toBeNull();
    expect(runtime.pendingRequests).toHaveLength(1);
    const snapshot = runtime.exportRuntimeSnapshot();
    expect(JSON.stringify(snapshot)).not.toContain('"view"');
    expect(JSON.stringify(snapshot)).not.toContain('"text":"selected"');
    expect(validRuntimeSnapshot(snapshot)).toBe(true);
    setActivePinia(createPinia());
    const restored = useWorkbenchRuntimeStore();
    expect(restored.restoreRuntimeSnapshot(snapshot)).toBe(true);
    expect(restored.session).toBeNull();
    await restored.activateWorkflow(MAIN_WORKFLOW_ID);
    await restored.restoreSession(SID);
    expect(restored.unknown?.action).toBe("resume");
    expect(restored.unknown?.body).toEqual(snapshot.sessions.find(row => row.sessionId === SID)!.pending!.body);
    expect(restored.locked).toBe(true);
  });
  it("candidate preview is local, selection and reroll use the current authoritative CAS and never publish new prompts", async () => {
    const fetcher = vi.fn(async (path: string) => {
      if (path === "/api/health") return json({ mode: "offline" });
      if (path === `/api/sessions/${SID}`) return json(historyView());
      if (path.endsWith("/select")) return json({
        workflow_session_id: SID, candidate_id: ALTERNATIVE, head_commit_id: HEAD, session_revision: 2, ref_revision: 3, status: "succeeded",
      });
      return json({
        workflow_session_id: SID, chain_run_id: ALT_CHAIN, source_chain_run_id: CHAIN, input_id: RUN, run_id: RUN, status: "prepared",
      });
    });
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.restoreSession(SID);
    fetcher.mockClear();
    runtime.previewCandidate(REPLY_MESSAGE, ALTERNATIVE);
    expect(fetcher).not.toHaveBeenCalled();
    expect(runtime.session?.messages.at(-1)?.payload).toEqual({ text: "selected" });
    await runtime.selectCandidate(ALTERNATIVE);
    const selected = fetcher.mock.calls.find(([path]) => path.endsWith("/select"))!;
    expect(JSON.parse((selected as unknown as [string, RequestInit])[1].body as string)).toMatchObject({
      expected_session_revision: 1, expected_ref_revision: 2, expected_head_commit_id: HEAD,
    });
    await runtime.reroll(CHAIN);
    expect(fetcher.mock.calls.some(([path]) => path.endsWith("/reroll"))).toBe(true);
    expect(fetcher.mock.calls.some(([path]) => path.includes("/prompt-configs/"))).toBe(false);
  });
  it("rejects corrupted imported paths/definitions and prevents dispatch when durable identity save fails", async () => {
    const fetcher = transport();
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("request");
    runtime.setMutationPersistenceGuard(() => false);
    await runtime.submitPrimary();
    expect(fetcher.mock.calls.filter(([, opts]) => opts?.method === "POST")).toHaveLength(0);
    expect(runtime.unknown).toBeNull();
    expect(runtime.error?.reason).toContain("未提交");
    const snapshot = runtime.exportRuntimeSnapshot();
    snapshot.sessions[0].pending = {
      action: "start", path: "https://evil.example/run", body: { text: "x", idempotency_key: RUN }, requestId: RUN, replayable: true,
    };
    expect(runtime.restoreRuntimeSnapshot(snapshot)).toBe(false);
    expect(runtime.error?.reason).toContain("未覆盖");
  });
  it("exposes only successful exact input selection and supports an existing deepseek mode with a mocked transport", async () => {
    const fetcher = transport();
    const original = fetcher.getMockImplementation()!;
    fetcher.mockImplementation(async (path, options) => {
      const response = await original(path, options);
      const value = await response.json();
      if (path === "/api/health" || path === "/api/sessions" || path === `/api/sessions/${SID}`) value.mode = "deepseek";
      return json(value, response.status);
    });
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("request");
    expect(runtime.currentPromptSelection).toBeNull();
    await runtime.submitPrimary();
    expect(runtime.currentPromptSelection?.nodes.A.revision).toBe(1);
    expect(runtime.currentPromptSelection?.nodes.B.revision).toBe(1);
    expect(runtime.exportRuntimeSnapshot().sessions.find(row => row.sessionId === SID)?.promptSelection).toEqual(runtime.currentPromptSelection);
  });
  it("blocks oversized UTF8 prompt mutations before dispatch rather than saving an unrecoverable pending body", async () => {
    const fetcher = transport();
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    const preparation = usePreparationStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("request");
    const prompt = preparation.getDraft(MAIN_WORKFLOW_ID, "A").nodes.find(node => node.kind === "prompt-item")!;
    preparation.updateNodeConfig(MAIN_WORKFLOW_ID, "A", prompt.id, { text: "界".repeat(23_000) });
    await runtime.submitPrimary();
    expect(fetcher.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
    expect(runtime.error?.reason).toContain("64KB");
    expect(runtime.unknown).toBeNull();
    expect(validRuntimeSnapshot(runtime.exportRuntimeSnapshot())).toBe(true);
  });
  it("forks a completed candidate boundary and switches the child without sending an input", async () => {
    const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
      if (path === "/api/health") return json({ mode: "offline" });
      if (path === `/api/sessions/${SID}/branches`) return json({
        workflow_session_id: SECOND_SID, source_workflow_session_id: SID, visible_message_id: REPLY_MESSAGE,
        fork_anchor_id: HEAD, role: "assistant", pending_input_id: null, active_workflow_session_id: SID, status: "created",
      }, 201);
      if (path === "/api/active-session" && options?.method === "GET") return json({ active_workflow_session_id: SID, revision: 1 });
      if (path === "/api/active-session") return json({ active_workflow_session_id: SECOND_SID, revision: 2 });
      if (path === "/api/sessions") return json([{ workflow_session_id: SECOND_SID, created_at: null, mode: "offline" }]);
      return json(historyView(path === `/api/sessions/${SECOND_SID}` ? SECOND_SID : SID));
    });
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.restoreSession(SID);
    await runtime.forkAt(REPLY_MESSAGE, ALTERNATIVE);
    const mutation = fetcher.mock.calls.find(([path]) => path.endsWith("/branches"))!;
    expect(JSON.parse(mutation[1]?.body as string)).toMatchObject({
      visible_message_id: REPLY_MESSAGE, candidate_id: ALTERNATIVE, expected_source_revision: 1,
    });
    expect(runtime.sessionId).toBe(SECOND_SID);
    expect(fetcher.mock.calls.some(([path]) => path.endsWith("/inputs"))).toBe(false);
  });
  it("retains unknown session selection without network or persistence during reconciliation", async () => {
    let first = true;
    const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
      if (path === "/api/health") return json({ mode: "offline" });
      if (path === "/api/active-session" && options?.method === "GET") return json({ active_workflow_session_id: SID, revision: 1 });
      if (path === "/api/active-session") {
        if (first) { first = false; throw new Error("lost receipt"); }
        return json({ active_workflow_session_id: SECOND_SID, revision: 2 });
      }
      return json(historyView(path === `/api/sessions/${SECOND_SID}` ? SECOND_SID : SID));
    });
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.restoreSession(SID);
    await runtime.selectSession(SECOND_SID);
    expect(runtime.selectionPending?.action).toBe("select-session");
    const request = runtime.exportRuntimeSnapshot().selectionPending!;
    await runtime.refresh();
    expect(runtime.selectionPending).toEqual(request);
    expect(runtime.locked).toBe(true);
    const save = vi.fn(() => true);
    runtime.setMutationPersistenceGuard(save);
    fetcher.mockClear();
    await runtime.replayUnknown();
    expect(fetcher).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
    expect(runtime.sessionId).toBe(SID);
    expect(runtime.selectionPending).toEqual(request);
    expect(runtime.error).toMatchObject({ kind: "unknown", requestId: request.requestId });
    expect(runtime.error?.reason).toContain("unresolved");
  });
});
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
describe("fixed workbench runtime", () => {
  it("switching is read only and empty workflow never instantiates a definition or session", async () => {
    const fetcher = transport();
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(EMPTY_WORKFLOW_ID);
    await runtime.submitPrimary();
    expect(fetcher).not.toHaveBeenCalled();
    expect(Object.keys(usePreparationStore().drafts)).toEqual([]);
    expect(runtime.error?.kind).toBe("unavailable");
  });
  it("rebases definition revisions while preserving instance provenance and exact group references", () => {
    const draft = createPreparationDraft(MAIN_WORKFLOW_ID, "A");
    draft.revision = 18;
    const compiled = compilePromptItems(draft);
    const publication = createPublication(compiled);
    expect(publication.config.revision).toBe(1);
    expect(publication.config.config_id).not.toBe(compiled.config.config_id);
    expect(publication.groups[0].members[0].item_instance_id)
      .toBe(compiled.groups[0].members[0].item_instance_id);
    expect(publication.groups[0].members[0].item_id).toBe(publication.items[1].item_id);
    expect(publication.config.inputs[1].kind).toBe("group");
    expect(publication.config.inputs[1]).toMatchObject({
      group_id: publication.groups[0].group_id,
      group_instance_id: compiled.config.inputs[1].kind === "group"
        ? compiled.config.inputs[1].group_instance_id : "",
    });
  });
  it("submits edited root input with both exact published selections and groups saved before config", async () => {
    const fetcher = transport();
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("Edited current input");
    await runtime.submitPrimary();
    const calls = fetcher.mock.calls as unknown as [string, RequestInit][];
    const input = calls.find(([path]) => path.endsWith("/inputs"))!;
    const body = JSON.parse(input[1].body as string);
    expect(body.text).toBe("Edited current input");
    const configs = calls.filter(([path, opts]) => path === "/api/prompt-configs/config" && opts.method === "POST");
    expect(configs).toHaveLength(2);
    expect(body.prompt_selection.nodes.A).toMatchObject({
      config_id: JSON.parse(configs[0][1].body as string).record.config_id, revision: 1,
    });
    expect(calls.findIndex(([path]) => path === "/api/prompt-configs/group"))
      .toBeLessThan(calls.findIndex(([path]) => path === "/api/prompt-configs/config"));
    expect(runtime.sessionId).toBe(SID);
    expect(runtime.sessions[0].workflow_session_id).toBe(SID);
    expect(runtime.unknown).toBeNull();
  });
  it("guards duplicate clicks before the first await", async () => {
    let release!: (value: Response) => void;
    const fetcher = transport();
    fetcher.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("root");
    const first = runtime.submitPrimary();
    await runtime.submitPrimary();
    expect(fetcher).toHaveBeenCalledTimes(1);
    release(json({ mode: "offline" }));
    await first;
    expect(fetcher.mock.calls.filter(([path]) => path.endsWith("/inputs"))).toHaveLength(1);
  });
  it("starts the next call after a context read without uploading archived cache materials", async () => {
    const fetcher = transport();
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    const preparation = usePreparationStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("next current input");
    runtime.runtimes[MAIN_WORKFLOW_ID].sessionId = SID;
    runtime.runtimes[MAIN_WORKFLOW_ID].view = { ...view("succeeded"), can_submit: true };
    const context = preparation.getDraft(MAIN_WORKFLOW_ID, "A").nodes.find((node) => node.kind === "context")!;
    const reference = {
      workflowId: MAIN_WORKFLOW_ID, stage: "A" as const, workflowSessionId: SID,
      nodeBindingId: AGENT_BINDINGS.A, selectedChainId: CHAIN,
    };
    expect(preparation.setContextCache(MAIN_WORKFLOW_ID, "A", context.id, {
      reference, source: "backend-read", floors: [
        {
          id: "archived-root-floor", kind: "root", items: [{
            id: "archived-root-message", name: "previous root", role: "user",
            presentation: null, declarationIndex: 0,
            payload: { kind: "text", text: "archived input, never uploaded as history" },
            source: { kind: "context", nodeId: context.id, context: {
              reference, floorId: "archived-root-floor", floorKind: "root",
              messageId: "archived-root-message",
            } },
          }],
        },
        {
          id: "archived-delta-floor", kind: "delta", items: [{
            id: "archived-delta-message", name: "previous delta", role: "assistant",
            presentation: null, declarationIndex: 1,
            payload: { kind: "structured-final", value: { text: "archived result" }, displayText: "archived result" },
            source: { kind: "context", nodeId: context.id, context: {
              reference, floorId: "archived-delta-floor", floorKind: "delta",
              messageId: "archived-delta-message",
            } },
          }],
        },
      ],
    }, preparation.currentViewToken(MAIN_WORKFLOW_ID, "A"))).toBe(true);
    await runtime.submitPrimary();
    const submissions = fetcher.mock.calls.filter(([path]) => path.endsWith("/inputs"));
    expect(submissions).toHaveLength(1);
    const body = JSON.parse(submissions[0][1]?.body as string);
    expect(Object.keys(body).sort()).toEqual(["idempotency_key", "prompt_selection", "text"]);
    expect(body.text).toBe("next current input");
    expect(JSON.stringify(fetcher.mock.calls)).not.toContain("archived result");
    expect(runtime.unknown).toBeNull();
  });
  it("locks unknown submissions across refresh and preserves their exact identity without resending", async () => {
    let first = true;
    const fetcher = transport(() => {
      if (first) { first = false; throw new Error("network lost"); }
      return json({ workflow_session_id: SID, chain_run_id: CHAIN, visible_message_id: SID, input_id: RUN, status: "prepared" }, 202);
    });
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("root");
    await runtime.submitPrimary();
    const pending = runtime.exportRuntimeSnapshot().sessions.find(row => row.sessionId === SID)!.pending!;
    expect(pending.action).toBe("start");
    await runtime.refresh();
    expect(runtime.unknown?.requestId).toBe(pending.requestId);
    await runtime.submitPrimary();
    expect(fetcher.mock.calls.filter(([path]) => path.endsWith("/inputs"))).toHaveLength(1);
    runtime.setInputText("changed while unknown");
    const save = vi.fn(() => true);
    runtime.setMutationPersistenceGuard(save);
    fetcher.mockClear();
    await runtime.replayUnknown();
    expect(fetcher).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
    expect(runtime.unknown).toEqual(pending);
    expect(runtime.error).toMatchObject({ kind: "unknown", requestId: pending.requestId });
    expect(runtime.error?.reason).toContain("unresolved");
  });
  it("preserves unreplayable unknown session creation including malformed 2xx receipt", async () => {
    const fetcher = transport();
    const original = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((path, opts) => path === "/api/sessions" ? Promise.resolve(json({}))
      : original(path, opts));
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("root");
    await runtime.submitPrimary();
    expect(runtime.unknown).toMatchObject({ action: "create-session", replayable: false });
    expect(runtime.locked).toBe(true);
    await runtime.replayUnknown();
    expect(fetcher.mock.calls.filter(([path]) => path === "/api/sessions")).toHaveLength(1);
  });
  it("rejects unsupported B input in either supported service mode before creating a session", async () => {
    const fetcher = transport();
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("root");
    usePreparationStore().setRootText(MAIN_WORKFLOW_ID, "B", "manual B input");
    await runtime.submitPrimary();
    expect(runtime.error?.reason).toContain("Agent B");
    expect(fetcher.mock.calls.filter(([path]) => path === "/api/sessions")).toHaveLength(0);
    fetcher.mockResolvedValueOnce(json({ mode: "deepseek" }));
    await runtime.submitPrimary();
    expect(runtime.error?.reason).toContain("Agent B");
  });
  it("only controls the authoritative bound run and marks pausing, not falsely resumed", () => {
    const paused = view("paused", ["resume"]);
    expect(primaryControl(paused)).toMatchObject({ action: "resume", runId: RUN, revision: 1 });
    const running = view("running", ["interrupt"]);
    expect(primaryControl(running)).toMatchObject({ action: "interrupt", runId: RUN });
    running.nodes[0].status = "pausing";
    expect(primaryControl(running)).toMatchObject({ action: "waiting", label: "暂停中" });
    running.nodes[0].node_binding_id = "foreign";
    expect(primaryControl(running).action).toBe("waiting");
  });
  it("does not attribute a late old-session read to a newly opened workflow", async () => {
    let release!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { release = resolve; })));
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.runtimes[MAIN_WORKFLOW_ID].sessionId = SID;
    const pending = runtime.refresh();
    await runtime.activateWorkflow(EMPTY_WORKFLOW_ID);
    release(json(view("paused", ["resume"])));
    await pending;
    expect(runtime.session).toBeNull();
    expect(runtime.workflowId).toBe(EMPTY_WORKFLOW_ID);
    expect(runtime.error).toBeNull();
  });
  it("keeps request identity on server rejection without an unknown lock", async () => {
    const fetcher = transport(() => json({ error: { reason_code: "stale_revision" } }, 409));
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("root");
    await runtime.submitPrimary();
    expect(runtime.error?.kind).toBe("rejected");
    expect(runtime.error?.requestId).not.toBe("本地");
    expect(runtime.unknown).toBeNull();
  });
  it("keeps malformed catalog success locked instead of submitting a missing configuration", async () => {
    const fetcher = transport();
    const original = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((path, opts) =>
      path === "/api/prompt-configs/item" && opts?.method === "POST"
        ? Promise.resolve(json({ record: { kind: "wrong" }, head: {} }, 201))
        : original(path, opts));
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.setInputText("root");
    await runtime.submitPrimary();
    expect(runtime.unknown).toMatchObject({ action: "save-config", replayable: true });
    expect(runtime.locked).toBe(true);
    expect(fetcher.mock.calls.some(([path]) => path === "/api/sessions")).toBe(false);
  });
  it("single selection never retargets resume, which uses frozen run and CAS rather than live edits", async () => {
    const fetcher = vi.fn(async (path: string) => {
      if (path === "/api/health") return json({ mode: "offline" });
      if (path === `/api/sessions/${SID}`) return json(view("paused", ["resume"]));
      return json({ workflow_session_id: SID, run_id: RUN, chain_run_id: CHAIN, status: "running" }, 202);
    });
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.runtimes[MAIN_WORKFLOW_ID].sessionId = SID;
    runtime.runtimes[MAIN_WORKFLOW_ID].view = view("paused", ["resume"]);
    useWorkspaceStore().selectWorkflow(EMPTY_WORKFLOW_ID);
    usePreparationStore().setRootText(MAIN_WORKFLOW_ID, "B", "not applied to frozen resume");
    await runtime.submitPrimary();
    const call = fetcher.mock.calls.find(([path]) => path.endsWith("/resume"))!;
    expect(call[0]).toBe(`/api/sessions/${SID}/runs/${RUN}/resume`);
    const options = (call as unknown as [string, RequestInit])[1];
    expect(JSON.parse(options.body as string)).toMatchObject({
      expected_session_revision: 1, expected_run_revision: 1,
    });
    expect(fetcher.mock.calls.some(([path]) => path.includes("prompt-configs"))).toBe(false);
  });
  it("retains the old workflow unknown request without attributing its error to the new view", async () => {
    let reject!: (issue: Error) => void;
    const fetcher = vi.fn(async (path: string) => {
      if (path === "/api/health") return json({ mode: "offline" });
      if (path === `/api/sessions/${SID}`) return json(view("paused", ["resume"]));
      return await new Promise<Response>((_resolve, failure) => { reject = failure; });
    });
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.runtimes[MAIN_WORKFLOW_ID].sessionId = SID;
    runtime.runtimes[MAIN_WORKFLOW_ID].view = view("paused", ["resume"]);
    const operation = runtime.submitPrimary();
    await vi.waitFor(() => expect(reject).toBeDefined());
    await runtime.activateWorkflow(EMPTY_WORKFLOW_ID);
    reject(new Error("response lost"));
    await operation;
    expect(runtime.error).toBeNull();
    expect(runtime.session).toBeNull();
    expect(runtime.runtimes[MAIN_WORKFLOW_ID].pending?.action).toBe("resume");
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    expect(runtime.unknown?.action).toBe("resume");
    expect(runtime.locked).toBe(true);
  });
  it("locks accepted controls when authoritative reread fails until explicit refresh", async () => {
    let reads = 0;
    const fetcher = vi.fn(async (path: string) => {
      if (path === "/api/health") return json({ mode: "offline" });
      if (path === `/api/sessions/${SID}`) {
        if (reads++ === 0) throw new Error("GET network unavailable");
        return json(view("paused", ["resume"]));
      }
      return json({ workflow_session_id: SID, run_id: RUN, chain_run_id: CHAIN, status: "pausing" }, 202);
    });
    vi.stubGlobal("fetch", fetcher);
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.runtimes[MAIN_WORKFLOW_ID].sessionId = SID;
    runtime.runtimes[MAIN_WORKFLOW_ID].view = view("running", ["interrupt"]);
    await runtime.submitPrimary();
    expect(runtime.unknown).toBeNull();
    expect(runtime.refreshRequired).toBe(true);
    expect(runtime.locked).toBe(true);
    await runtime.refresh();
    expect(runtime.refreshRequired).toBe(false);
    expect(runtime.control.action).toBe("resume");
  });
  it.each(["accepted", "rejected", "lost"] as const)(
    "does not apply a late %s mutation after restoring the same pending identity",
    async outcome => {
      let release!: (value: Response) => void;
      let reject!: (failure: Error) => void;
      const fetcher = vi.fn(async (path: string) => {
        if (path === "/api/health") return json({ mode: "offline" });
        return await new Promise<Response>((resolve, failure) => { release = resolve; reject = failure; });
      });
      vi.stubGlobal("fetch", fetcher);
      const runtime = useWorkbenchRuntimeStore();
      await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
      runtime.runtimes[MAIN_WORKFLOW_ID].sessionId = SID;
      runtime.runtimes[MAIN_WORKFLOW_ID].view = view("paused", ["resume"]);
      const submitting = runtime.submitPrimary();
      await vi.waitFor(() => expect(release).toBeDefined());
      const restored = runtime.exportRuntimeSnapshot();
      expect(runtime.restoreRuntimeSnapshot(restored)).toBe(true);
      const original = runtime.exportRuntimeSnapshot();
      if (outcome === "lost") reject(new Error("lost receipt"));
      else release(outcome === "rejected" ? json({ error: { code: "stale_revision" } }, 409)
        : json({ workflow_session_id: SID, run_id: RUN, chain_run_id: CHAIN, status: "running" }, 202));
      await submitting;
      expect(runtime.exportRuntimeSnapshot()).toEqual(original);
      expect(runtime.session).toBeNull();
      expect(runtime.error).toBeNull();
      expect(fetcher).toHaveBeenCalledTimes(2);
    },
  );
  it.each(["body", "requestId", "path", "action", "replayable", "sessionId"] as const)(
    "does not settle an in-flight command after its %s coordinate changes",
    async coordinate => {
      let release!: (value: Response) => void;
      const fetcher = vi.fn(async (path: string, _options?: RequestInit) => path === "/api/health" ? json({ mode: "offline" })
        : await new Promise<Response>(resolve => { release = resolve; }));
      vi.stubGlobal("fetch", fetcher);
      const runtime = useWorkbenchRuntimeStore();
      await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
      const state = runtime.runtimes[MAIN_WORKFLOW_ID];
      state.sessionId = SID;
      state.view = view("paused", ["resume"]);
      const submitting = runtime.submitPrimary();
      await vi.waitFor(() => expect(release).toBeDefined());
      const command = state.pending!;
      if (coordinate === "body") command.body.expected_run_revision = 9;
      else if (coordinate === "requestId") command.requestId = SECOND_SID;
      else if (coordinate === "path") command.path += "-changed";
      else if (coordinate === "action") command.action = "interrupt";
      else if (coordinate === "replayable") command.replayable = false;
      else state.sessionId = SECOND_SID;
      const original = JSON.stringify(runtime.exportRuntimeSnapshot());
      const sent = fetcher.mock.calls[1][1] as RequestInit;
      expect(JSON.parse(sent.body as string).expected_run_revision).toBe(1);
      release(json({ workflow_session_id: SID, run_id: RUN, chain_run_id: CHAIN, status: "running" }, 202));
      await submitting;
      expect(JSON.stringify(runtime.exportRuntimeSnapshot())).toBe(original);
      expect(runtime.error?.reason).toContain("迟到");
      expect(fetcher).toHaveBeenCalledTimes(2);
    },
  );
  it.each(["accepted", "rejected"] as const)(
    "preserves the original session selection on a late %s response after restore",
    async outcome => {
      let release!: (value: Response) => void;
      const fetcher = vi.fn(async (_path: string, options?: RequestInit) => options?.method === "GET"
        ? json({ active_workflow_session_id: SID, revision: 1 })
        : await new Promise<Response>(resolve => { release = resolve; }));
      vi.stubGlobal("fetch", fetcher);
      const runtime = useWorkbenchRuntimeStore();
      await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
      const submitting = runtime.selectSession(SECOND_SID);
      await vi.waitFor(() => expect(release).toBeDefined());
      const snapshot = runtime.exportRuntimeSnapshot();
      expect(runtime.restoreRuntimeSnapshot(snapshot)).toBe(true);
      const original = runtime.exportRuntimeSnapshot();
      release(outcome === "accepted" ? json({ active_workflow_session_id: SECOND_SID, revision: 2 })
        : json({ error: { code: "stale_revision" } }, 409));
      await submitting;
      expect(runtime.exportRuntimeSnapshot()).toEqual(original);
      expect(runtime.sessionId).toBeNull();
      expect(runtime.error).toBeNull();
      expect(fetcher).toHaveBeenCalledTimes(2);
    },
  );
  it.each(["accepted", "rejected"] as const)(
    "retains the first control request when saving its %s result fails or throws",
    async outcome => {
      for (const throwing of [false, true]) {
        setActivePinia(createPinia());
        const fetcher = vi.fn(async (path: string) => path === "/api/health" ? json({ mode: "offline" })
          : outcome === "rejected" ? json({ error: { code: "stale_revision" } }, 409)
            : json({ workflow_session_id: SID, run_id: RUN, chain_run_id: CHAIN, status: "running" }, 202));
        vi.stubGlobal("fetch", fetcher);
        const runtime = useWorkbenchRuntimeStore();
        await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
        runtime.runtimes[MAIN_WORKFLOW_ID].sessionId = SID;
        runtime.runtimes[MAIN_WORKFLOW_ID].view = view("paused", ["resume"]);
        let original = "";
        const save = vi.fn(() => {
          if (!original) { original = JSON.stringify(runtime.exportRuntimeSnapshot()); return true; }
          if (throwing) throw new Error("quota");
          return false;
        });
        runtime.setMutationPersistenceGuard(save);
        await runtime.submitPrimary();
        expect(JSON.stringify(runtime.exportRuntimeSnapshot())).toBe(original);
        expect(runtime.unknown?.action).toBe("resume");
        expect(runtime.error).toMatchObject({ kind: "unknown", requestId: runtime.unknown!.requestId });
        expect(runtime.error?.reason).toContain("无法保存");
        expect(save).toHaveBeenCalledTimes(2);
        expect(fetcher).toHaveBeenCalledTimes(2);
      }
    },
  );
  it("restores an unbound create request when persisting the accepted session fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async (path: string) => path === "/api/health" ? json({ mode: "offline" }) : json(view(), 201)));
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    let original = "";
    runtime.setMutationPersistenceGuard(() => {
      if (!original) { original = JSON.stringify(runtime.exportRuntimeSnapshot()); return true; }
      return false;
    });
    await runtime.createSession();
    expect(JSON.stringify(runtime.exportRuntimeSnapshot())).toBe(original);
    expect(runtime.sessionId).toBeNull();
    expect(runtime.unknown?.action).toBe("create-session");
    expect(validRuntimeSnapshot(runtime.exportRuntimeSnapshot())).toBe(true);
    expect(runtime.error?.kind).toBe("unknown");
  });
  it.each(["accepted", "rejected"] as const)(
    "retains the first selection request when saving its %s result fails",
    async outcome => {
      const fetcher = vi.fn(async (_path: string, options?: RequestInit) => options?.method === "GET"
        ? json({ active_workflow_session_id: SID, revision: 1 })
        : outcome === "accepted" ? json({ active_workflow_session_id: SECOND_SID, revision: 2 })
          : json({ error: { code: "stale_revision" } }, 409));
      vi.stubGlobal("fetch", fetcher);
      const runtime = useWorkbenchRuntimeStore();
      await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
      let original = "";
      runtime.setMutationPersistenceGuard(() => {
        if (!original) { original = JSON.stringify(runtime.exportRuntimeSnapshot()); return true; }
        return false;
      });
      await runtime.selectSession(SECOND_SID);
      expect(JSON.stringify(runtime.exportRuntimeSnapshot())).toBe(original);
      expect(runtime.selectionPending?.action).toBe("select-session");
      expect(runtime.error?.kind).toBe("unknown");
      expect(fetcher).toHaveBeenCalledTimes(2);
    },
  );
  it("does not delete or rebind a workflow containing a restored pending request", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.runtimes[MAIN_WORKFLOW_ID].sessionId = SID;
    runtime.runtimes[MAIN_WORKFLOW_ID].pending = {
      path: `/api/sessions/${SID}/runs/${RUN}/resume`,
      body: { expected_session_revision: 1, expected_run_revision: 1, idempotency_key: RUN },
      requestId: RUN, action: "resume", replayable: true,
    };
    expect(runtime.restoreRuntimeSnapshot(runtime.exportRuntimeSnapshot())).toBe(true);
    const original = runtime.exportRuntimeSnapshot();
    expect(runtime.removeWorkflow(MAIN_WORKFLOW_ID)).toBe(false);
    expect(runtime.bindWorkflowSession(EMPTY_WORKFLOW_ID, MAIN_WORKFLOW_ID, SECOND_SID)).toBe(false);
    expect(runtime.exportRuntimeSnapshot()).toEqual(original);
    expect(runtime.error?.kind).toBe("unknown");
  });
  it("protects unselected pending sessions without blocking definitely unsent cleanup", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(historyView(SECOND_SID))));
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    runtime.runtimes[MAIN_WORKFLOW_ID].sessionId = SID;
    runtime.runtimes[MAIN_WORKFLOW_ID].pending = {
      path: `/api/sessions/${SID}/inputs`, body: { text: "original", idempotency_key: RUN },
      requestId: RUN, action: "start", replayable: true,
    };
    await runtime.restoreSession(SECOND_SID);
    const original = runtime.exportRuntimeSnapshot();
    expect(runtime.unknown).toBeNull();
    expect(runtime.removeWorkflow(MAIN_WORKFLOW_ID)).toBe(false);
    expect(runtime.bindWorkflowSession(EMPTY_WORKFLOW_ID, MAIN_WORKFLOW_ID, null)).toBe(false);
    expect(runtime.exportRuntimeSnapshot()).toEqual(original);
    expect(runtime.bindWorkflowSession(MAIN_WORKFLOW_ID, EMPTY_WORKFLOW_ID, null)).toBe(true);
    expect(runtime.removeWorkflow(EMPTY_WORKFLOW_ID)).toBe(true);
  });
  it.each(["mutation", "selection"] as const)(
    "keeps the first %s pending on idempotency conflict without clearing or resending",
    async kind => {
      const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
        if (path === "/api/health") return json({ mode: "offline" });
        if (options?.method === "GET") return json({ active_workflow_session_id: SID, revision: 1 });
        return json({ error: { reason_code: "idempotency_conflict" } }, 409);
      });
      vi.stubGlobal("fetch", fetcher);
      const runtime = useWorkbenchRuntimeStore();
      await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
      if (kind === "mutation") {
        runtime.runtimes[MAIN_WORKFLOW_ID].sessionId = SID;
        runtime.runtimes[MAIN_WORKFLOW_ID].view = view("paused", ["resume"]);
      }
      let original = "";
      const save = vi.fn(() => { original = JSON.stringify(runtime.exportRuntimeSnapshot()); return true; });
      runtime.setMutationPersistenceGuard(save);
      if (kind === "mutation") await runtime.submitPrimary();
      else await runtime.selectSession(SECOND_SID);
      expect(JSON.stringify(runtime.exportRuntimeSnapshot())).toBe(original);
      expect(runtime.unknown).not.toBeNull();
      expect(runtime.error).toMatchObject({ kind: "unknown", requestId: runtime.unknown!.requestId });
      expect(runtime.error?.reason).toContain("idempotency_conflict");
      expect(save).toHaveBeenCalledTimes(1);
      fetcher.mockClear();
      save.mockClear();
      await runtime.replayUnknown();
      expect(fetcher).not.toHaveBeenCalled();
      expect(save).not.toHaveBeenCalled();
      expect(JSON.stringify(runtime.exportRuntimeSnapshot())).toBe(original);
    },
  );
});
