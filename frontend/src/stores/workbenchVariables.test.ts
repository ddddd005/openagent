import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import { contextScope } from "../adapters/workbenchContext.fixtures";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { useWorkbenchRuntimeStore, validRuntimeSnapshot } from "./workbenchRuntime";
import { usePreparationStore } from "./preparation";
import { useWorkbenchVariablesStore } from "./workbenchVariables";

const scope = contextScope();
const session = () => ({
  workflow_session_id: scope.sessionId, revision: scope.sessionRevision, mode: "offline",
  can_submit: true, ref_revision: scope.refRevision, head_commit_id: scope.headCommitId,
  available_actions: [], chains: [], messages: [],
  nodes: [
    ...Object.values(AGENT_BINDINGS).map((id) => ({ node_binding_id: id, label: id === AGENT_BINDINGS.A ? "A" : "B", status: "idle", run_id: null, revision: null })),
    { node_binding_id: "7be319b8-30bd-4674-b7bf-d1cf54a1a10a", label: "Output", status: "idle", run_id: null, revision: null },
  ],
});
const projection = (value = 10, revision = 0) => ({
  schema_version: 1, kind: "workflow_variable_read", workflow_session_id: scope.sessionId, revision,
  values: [{ name: "倒计时", type: "integer", assigned: true, source: revision ? "assignment" : "default", value }],
});
const json = (value: unknown) => new Response(JSON.stringify(value));
const writeProjection = (value: number, revision: number, options: RequestInit) => {
  const body = JSON.parse(options.body as string);
  return {
    ...projection(value, revision),
    write_receipt: {
      idempotency_key: body.idempotency_key, name: body.name,
      expected_variable_revision: body.expected_variable_revision,
    },
  };
};

beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
});
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
async function setup() {
  const runtime = useWorkbenchRuntimeStore();
  await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
  await runtime.restoreSession(scope.sessionId);
  const preparation = usePreparationStore();
  const id = preparation.addNode(MAIN_WORKFLOW_ID, "A", "variable-register")!;
  preparation.updateNodeConfig(MAIN_WORKFLOW_ID, "A", id, {
    name: "倒计时", valueType: "integer", hasInitialValue: true, initialValue: 10,
  });
  const variables = useWorkbenchVariablesStore();
  variables.activate(MAIN_WORKFLOW_ID, "A");
  return { runtime, preparation, variables };
}
describe("current session variable editing", () => {
  it("reads without writing and updates through a durable typed mutation, without editing the default", async () => {
    let value = 10;
    let revision = 0;
    const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
      if (path.endsWith("/variables/read")) return json(projection(value, revision));
      if (path.endsWith("/variables/write")) {
        const body = JSON.parse(options!.body as string);
        value = body.value;
        revision++;
        return json(writeProjection(value, revision, options!));
      }
      return path === "/api/health" ? json({ mode: "offline" }) : json(session());
    });
    vi.stubGlobal("fetch", fetcher);
    const { variables, preparation } = await setup();
    const exported = preparation.exportConfiguration();
    await variables.refresh();
    expect(variables.read?.values[0].value).toBe(10);
    expect(fetcher.mock.calls.filter(([path]) => path.endsWith("/variables/write"))).toHaveLength(0);
    await variables.assign(variables.read!.values[0], 9);
    expect(variables.read?.values[0].value).toBe(9);
    expect(preparation.exportConfiguration()).toEqual(exported);
    const body = JSON.parse(fetcher.mock.calls.find(([path]) => path.endsWith("/variables/write"))![1]!.body as string);
    expect(body).toMatchObject({ name: "倒计时", value: 9, expected_variable_revision: 0 });
    expect(body.idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
  });
  it("preserves an unknown variable write without resending its original key and configuration", async () => {
    let writes = 0;
    const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
      if (path.endsWith("/variables/read")) return json(projection());
      if (path.endsWith("/variables/write")) {
        if (++writes === 1) throw new Error("lost receipt");
        return json(writeProjection(9, 1, options!));
      }
      return path === "/api/health" ? json({ mode: "offline" }) : json(session());
    });
    vi.stubGlobal("fetch", fetcher);
    const { runtime, variables, preparation } = await setup();
    await variables.refresh();
    await variables.assign(variables.read!.values[0], 9);
    expect(runtime.unknown).toBeTruthy();
    const snapshot = runtime.exportRuntimeSnapshot();
    expect(validRuntimeSnapshot(snapshot)).toBe(true);
    preparation.setRootText(MAIN_WORKFLOW_ID, "A", "changed after unknown write");
    const beforeReconcile = fetcher.mock.calls.length;
    await runtime.replayUnknown();
    const calls = fetcher.mock.calls.filter(([path]) => path.endsWith("/variables/write"));
    expect(calls).toHaveLength(1);
    expect(fetcher).toHaveBeenCalledTimes(beforeReconcile);
    expect(runtime.exportRuntimeSnapshot()).toEqual(snapshot);
    expect(runtime.unknown?.body).toEqual(JSON.parse(calls[0]![1]!.body as string));
    expect(runtime.error?.kind).toBe("unknown");
  });
  it.each([
    { value: 10, revision: 1 },
    { value: 9, revision: 0 },
  ])("keeps a mismatched write receipt pending ($value, revision $revision)", async ({ value, revision }) => {
    vi.stubGlobal("fetch", vi.fn(async (path: string, options?: RequestInit) => {
      if (path.endsWith("/variables/read")) return json(projection());
      if (path.endsWith("/variables/write")) return json(writeProjection(value, revision, options!));
      return path === "/api/health" ? json({ mode: "offline" }) : json(session());
    }));
    const { runtime, variables } = await setup();
    await variables.refresh();
    expect(await variables.assign(variables.read!.values[0], 9)).toBe(false);
    expect(runtime.unknown).toBeTruthy();
    expect(validRuntimeSnapshot(runtime.exportRuntimeSnapshot())).toBe(true);
    expect(variables.read?.values[0].value).toBe(10);
  });
  it("accepts a nonconsecutive revision only with its exact request receipt", async () => {
    let written = false;
    vi.stubGlobal("fetch", vi.fn(async (path: string, options?: RequestInit) => {
      if (path.endsWith("/variables/read")) return json(written ? projection(9, 3) : projection());
      if (path.endsWith("/variables/write")) {
        written = true;
        return json(writeProjection(9, 3, options!));
      }
      return path === "/api/health" ? json({ mode: "offline" }) : json(session());
    }));
    const { runtime, variables } = await setup();
    await variables.refresh();
    expect(await variables.assign(variables.read!.values[0], 9)).toBe(true);
    expect(runtime.unknown).toBeNull();
    expect(variables.read?.revision).toBe(3);
  });
  it("drops a delayed variable response after changing the viewed stage", async () => {
    let resolve!: (response: Response) => void;
    const waiting = new Promise<Response>((complete) => { resolve = complete; });
    vi.stubGlobal("fetch", vi.fn(async (path: string) => {
      if (path.endsWith("/variables/read")) return waiting;
      return path === "/api/health" ? json({ mode: "offline" }) : json(session());
    }));
    const { variables } = await setup();
    const reading = variables.refresh();
    variables.activate(MAIN_WORKFLOW_ID, "B");
    resolve(json(projection()));
    await reading;
    expect(variables.read).toBeNull();
    expect(variables.error).toBeNull();
    expect(variables.loading).toBe(false);
  });
  it("invalidates observations after configuration or scope changes", async () => {
    vi.stubGlobal("fetch", vi.fn(async (path: string) => json(path.endsWith("/variables/read") ? projection() : session())));
    const { variables, preparation } = await setup();
    await variables.refresh();
    expect(variables.read).not.toBeNull();
    preparation.setRootText(MAIN_WORKFLOW_ID, "A", "different input");
    expect(variables.read).toBeNull();
    variables.leave();
    expect(variables.scope).toBeNull();
  });
});
