import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

const source = readFileSync(new URL("../../../backend/src/phase1_agent/static/app.js", import.meta.url), "utf8");
const sid = "00000000-0000-4000-8000-000000000901";
const key = `workflow-user-ui:v1:${sid}`;
const requestId = "00000000-0000-4000-8000-000000000902";
type Listener = (event: { preventDefault: () => void }) => unknown;
class Element {
  value = "";
  textContent = "";
  hidden = false;
  disabled = false;
  dataset = {};
  children: Element[] = [];
  listeners = new Map<string, Listener>();
  get options() { return this.children; }
  add(element: Element) { this.children.push(element); }
  append(...elements: Element[]) { this.children.push(...elements); }
  replaceChildren(...elements: Element[]) { this.children = elements; }
  addEventListener(name: string, listener: Listener) { this.listeners.set(name, listener); }
}
function record(version = 2) {
  return {
    schema_version: version, kind: "workflow_user_session", workflow_session_id: sid,
    prompt_selection: null, ...(version === 2 ? { model_selection: null } : {}),
    pending_submission: { text: "original input", idempotency_key: "opaque-original-key" },
  };
}
function receipt() {
  return { workflow_session_id: sid, status: "prepared",
    input_id: requestId, visible_message_id: requestId, chain_run_id: requestId };
}
const runReceipt = { workflow_session_id: sid, chain_run_id: requestId, run_id: requestId, status: "running" };
const replacementReceipt = { ...runReceipt, source_chain_run_id: requestId, input_id: requestId, status: "prepared" };
const controls = [
  { name: "close", prepare: 'currentView.available_actions = ["close_execution"];',
    dispatch: 'recoverExecution("close_execution")', pending: "pendingRecovery",
    result: { workflow_session_id: sid, chain_run_id: requestId, closeout_id: requestId, status: "closed" } },
  { name: "continue", prepare: 'currentView.available_actions = ["continue_workflow"];',
    dispatch: 'recoverExecution("continue_workflow")', pending: "pendingRecovery", result: replacementReceipt },
  { name: "reroll", prepare: "", dispatch: `changeReply("reroll", "${requestId}", "${requestId}")`,
    pending: "pendingVersion", result: replacementReceipt },
  { name: "candidate", prepare: "", dispatch: `changeReply("select", "${requestId}", "${requestId}")`,
    pending: "pendingVersion", result: { workflow_session_id: sid, candidate_id: requestId,
      head_commit_id: requestId, ref_revision: 1, session_revision: 2, status: "succeeded" } },
  { name: "branch", prepare: "",
    dispatch: `forkFromCandidate("${requestId}", "${requestId}")`, pending: "pendingBranch",
    result: { workflow_session_id: requestId, source_workflow_session_id: sid, visible_message_id: requestId,
      fork_anchor_id: requestId, role: "assistant", pending_input_id: null,
      active_workflow_session_id: requestId, status: "created", selection_revision: 2 } },
  { name: "select-session", prepare: "", dispatch: `switchActiveSession("${requestId}")`,
    pending: "pendingSessionSwitch", result: { active_workflow_session_id: requestId, revision: 2 } },
  { name: "resume", prepare: `currentView.available_actions = ["resume"];
      currentView.nodes = [{ status: "paused", run_id: "${requestId}", revision: 1 }];`,
    dispatch: 'document.getElementById("run-control").listeners.get("click")({})',
    pending: "pendingControl", result: runReceipt },
  { name: "archive", prepare: `currentView.available_actions = ["retry_archive"];
      currentView.nodes = [{ status: "final_ready", run_id: "${requestId}", revision: 1 }];`,
    dispatch: 'document.getElementById("retry-closeout").listeners.get("click")({})',
    pending: "pendingCloseout", result: { ...runReceipt, status: "succeeded" } },
  { name: "pending-input", prepare: `currentView.available_actions = ["continue_pending_input"];
      currentView.pending_input_id = "${requestId}";`,
    dispatch: 'document.getElementById("start-pending").listeners.get("click")({})',
    pending: "pendingStart", result: receipt() },
  { name: "budget", prepare: `currentView.available_actions = ["extend_budget"];
      currentView.nodes = [{ status: "paused", run_id: "${requestId}", revision: 1,
        budget: { max_model_requests: 5, max_model_attempts: 20, model_requests: 5, attempts: 20 } }];
      additionalRequests.value = "1"; additionalAttempts.value = "4"; updateBudgetControls(currentView);`,
    dispatch: 'document.getElementById("extend-budget").listeners.get("click")({})',
    pending: "pendingBudgets.size > 0 ? Array.from(pendingBudgets.values())[0] : null",
    result: { ...runReceipt, status: "paused", revision: 2, budget: { max_model_requests: 6, max_model_attempts: 24 } } },
];
async function harness(initial: string | null = null) {
  const elements = new Map<string, Element>();
  function element(id: string) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id)!;
  }
  const saved = new Map<string, string>(initial === null ? [] : [[key, initial]]);
  const storage = {
    getItem: vi.fn((name: string) => saved.get(name) ?? null),
    setItem: vi.fn((name: string, value: string) => { saved.set(name, value); }),
    removeItem: vi.fn((name: string) => { saved.delete(name); }),
  };
  const fetcher = vi.fn(async (path: string, _init?: RequestInit): Promise<Response> =>
    new Response(JSON.stringify(path === "/api/health" ? { mode: "offline" }
      : path === "/api/active-session" ? { active_workflow_session_id: null, revision: 0 } : [])));
  const scope = {
    document: { getElementById: (id: string) => id.startsWith("public-") ? null : element(id),
      createElement: () => new Element() },
    Option: class extends Element {
      constructor(text: string, value: string) { super(); this.textContent = text; this.value = value; }
    },
    fetch: fetcher, localStorage: storage, crypto: { randomUUID: () => requestId },
    setTimeout: vi.fn(), clearTimeout: vi.fn(), URLSearchParams,
    location: { search: "" }, history: { replaceState: vi.fn() },
  };
  await runInNewContext(source, scope);
  const evaluate = (code: string) => runInNewContext(code, scope);
  evaluate(`currentId = "${sid}"; currentView = {
    can_submit: true, can_reroll: true, can_select_candidates: true,
    can_close_execution: true, can_continue_workflow: true,
    recovery_chain_run_id: "${requestId}", revision: 1, nodes: [], chains: [], messages: [],
    available_actions: [], head_commit_id: null, ref_revision: 0
  };`);
  fetcher.mockClear();
  storage.getItem.mockClear(); storage.setItem.mockClear(); storage.removeItem.mockClear();
  const submit = async () => {
    element("input").value = "first input";
    await element("composer").listeners.get("submit")!({ preventDefault() {} });
  };
  return { scope, element, evaluate, saved, storage, fetcher, submit };
}

describe("legacy static pending custody", () => {
  it.each(controls)("validates the first $name receipt before clearing pending", async control => {
    const page = await harness();
    page.evaluate(`activeSelection = { active_workflow_session_id: "${sid}", revision: 1 }; ${control.prepare}`);
    page.fetcher.mockResolvedValueOnce(new Response(JSON.stringify(control.result)));
    await page.evaluate(control.dispatch);
    expect(page.fetcher.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true);
    expect(page.evaluate(control.pending)).toBeNull();
  });
  it.each(controls)("retains $name pending on malformed or foreign 2xx without resubmitting", async control => {
    for (const foreign of [false, true]) {
      const page = await harness();
      page.evaluate(`activeSelection = { active_workflow_session_id: "${sid}", revision: 1 }; ${control.prepare}`);
      const result = foreign ? { ...control.result, workflow_session_id: "other-session",
        active_workflow_session_id: "other-session" } : {};
      page.fetcher.mockResolvedValueOnce(new Response(JSON.stringify(result)));
      await page.evaluate(control.dispatch);
      const original = page.evaluate(`JSON.stringify(${control.pending})`);
      expect(original).not.toBe("null");
      const requests = page.fetcher.mock.calls.length;
      await page.evaluate(control.dispatch);
      expect(page.fetcher).toHaveBeenCalledTimes(requests);
      expect(page.evaluate(`JSON.stringify(${control.pending})`)).toBe(original);
    }
  });
  it.each([1, 2])("keeps schema %i opaque pending byte-for-byte without resending", async version => {
    const raw = JSON.stringify(record(version), null, 2);
    const page = await harness(raw);
    await page.submit();
    await page.element("new-session").listeners.get("click")!({ preventDefault() {} });
    expect(page.fetcher).not.toHaveBeenCalled();
    expect(page.storage.setItem).not.toHaveBeenCalled();
    expect(page.storage.removeItem).not.toHaveBeenCalled();
    expect(page.saved.get(key)).toBe(raw);
    expect(page.evaluate(`userSessionRecord("${sid}").pending_submission`)).toEqual(record().pending_submission);
  });
  it.each([
    "pendingControl", "pendingCloseout", "pendingStart", "pendingVersion",
    "pendingRecovery", "pendingBranch", "pendingSessionSwitch", "pendingCreation", "pendingBudgets",
  ])("blocks replacing an unknown %s and preserves it across local list refresh", async name => {
    const page = await harness();
    page.evaluate(name === "pendingBudgets" ? `pendingBudgets.set("original", { original: true });`
      : `${name} = { original: true };`);
    await page.submit();
    await page.element("new-session").listeners.get("click")!({ preventDefault() {} });
    await page.element("run-control").listeners.get("click")!({ preventDefault() {} });
    await page.element("retry-closeout").listeners.get("click")!({ preventDefault() {} });
    await page.element("start-pending").listeners.get("click")!({ preventDefault() {} });
    await page.evaluate(`recoverExecution("close_execution")`);
    await page.evaluate(`switchActiveSession("${requestId}")`);
    expect(page.fetcher).not.toHaveBeenCalled();
    expect(page.storage.setItem).not.toHaveBeenCalled();
    await page.evaluate("loadSessions()");
    expect(page.evaluate(name === "pendingBudgets" ? 'pendingBudgets.get("original")' : name))
      .toEqual({ original: true });
    expect(page.fetcher.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
  });
  it("sends a new submission once and keeps a lost receipt pending after reload", async () => {
    const page = await harness();
    page.fetcher.mockRejectedValueOnce(new Error("lost response"));
    await page.submit();
    expect(page.fetcher).toHaveBeenCalledTimes(1);
    const raw = page.saved.get(key)!;
    expect(JSON.parse(raw).pending_submission.idempotency_key).toBe(requestId);
    page.fetcher.mockClear(); page.storage.setItem.mockClear();
    await page.submit();
    expect(page.fetcher).not.toHaveBeenCalled();
    expect(page.storage.setItem).not.toHaveBeenCalled();
    const reopened = await harness(raw);
    await reopened.submit();
    expect(reopened.fetcher).not.toHaveBeenCalled();
    expect(reopened.saved.get(key)).toBe(raw);
  });
  it.each([
    [400, "invalid_request", false], [409, "idempotency_conflict", true], [503, "unavailable", true],
  ])("distinguishes a first %i/%s response from an already unknown request", async (status, code, retained) => {
    const page = await harness();
    page.fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ error: { code } }), { status }));
    await page.submit();
    const pending = JSON.parse(page.saved.get(key)!).pending_submission;
    expect(pending !== null).toBe(retained);
    if (retained) {
      page.fetcher.mockClear();
      await page.submit();
      expect(page.fetcher).not.toHaveBeenCalled();
    }
  });
  it("keeps the original durable pending when acknowledgement storage fails and refuses reset", async () => {
    const page = await harness();
    page.fetcher.mockImplementationOnce(async () => {
      page.storage.setItem.mockImplementationOnce(() => { throw new Error("quota"); });
      return new Response(JSON.stringify(receipt()));
    });
    await page.submit();
    const raw = page.saved.get(key)!;
    expect(JSON.parse(raw).pending_submission).not.toBeNull();
    expect(page.evaluate(`userSessionRecord("${sid}").pending_submission`)).not.toBeNull();
    expect(page.evaluate(`promptRecordFailures.get("${sid}").raw`)).toBe(raw);
    await page.element("reset-prompt-state").listeners.get("click")!({ preventDefault() {} });
    expect(page.storage.removeItem).not.toHaveBeenCalled();
    expect(page.saved.get(key)).toBe(raw);
  });
  it.each([true, false])("does not clear another page's record on late success=%s", async succeeded => {
    const page = await harness();
    const newer = JSON.stringify(record(1), null, 2);
    page.fetcher.mockImplementationOnce(async () => {
      page.saved.set(key, newer);
      return succeeded ? new Response(JSON.stringify(receipt()))
        : new Response('{"error":{"code":"retired"}}', { status: 410 });
    });
    await page.submit();
    expect(page.saved.get(key)).toBe(newer);
    expect(page.storage.setItem).toHaveBeenCalledTimes(1);
    expect(page.storage.removeItem).not.toHaveBeenCalled();
  });
  it("does not clear a restored same-key request on a late success", async () => {
    const page = await harness();
    page.fetcher.mockImplementationOnce(async () => {
      page.evaluate(`userSessionRecords.set("${sid}", copyJson(userSessionRecord("${sid}")));`);
      return new Response(JSON.stringify(receipt()));
    });
    await page.submit();
    expect(JSON.parse(page.saved.get(key)!).pending_submission).not.toBeNull();
    expect(page.evaluate(`userSessionRecord("${sid}").pending_submission`)).not.toBeNull();
    expect(page.storage.setItem).toHaveBeenCalledTimes(1);
  });
  it("does not recreate a session after losing its non-idempotent creation receipt", async () => {
    const page = await harness();
    page.fetcher.mockRejectedValueOnce(new Error("lost creation receipt"));
    await page.element("new-session").listeners.get("click")!({ preventDefault() {} });
    expect(page.fetcher).toHaveBeenCalledTimes(1);
    expect(page.evaluate("pendingCreation")).toEqual({ path: "/api/sessions", body: {} });
    page.fetcher.mockClear();
    await page.element("new-session").listeners.get("click")!({ preventDefault() {} });
    expect(page.fetcher).not.toHaveBeenCalled();
    expect(page.evaluate("pendingCreation")).not.toBeNull();
  });
  it("refuses to reset another page's newly pending record after a pre-dispatch storage conflict", async () => {
    const initial = JSON.stringify({ ...record(), pending_submission: null });
    const page = await harness(initial);
    page.evaluate(`userSessionRecord("${sid}")`);
    const newer = JSON.stringify(record(1), null, 2);
    page.saved.set(key, newer);
    await page.submit();
    expect(page.fetcher).not.toHaveBeenCalled();
    expect(page.evaluate(`promptRecordFailures.get("${sid}").raw`)).toBe(initial);
    await page.element("reset-prompt-state").listeners.get("click")!({ preventDefault() {} });
    expect(page.saved.get(key)).toBe(newer);
    expect(page.storage.setItem).not.toHaveBeenCalled();
    expect(page.storage.removeItem).not.toHaveBeenCalled();
  });
});
