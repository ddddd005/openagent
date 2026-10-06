import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useExposuresStore } from "./exposures";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { exposureSignature, isExposureRead, isExposureSnapshot } from "../domain/exposureConfiguration";
import { type PublicSession } from "../adapters/workbenchApi";
import { workbenchUserInterfaceUrl } from "../adapters/legacyUi";
import { clonePreparation } from "../domain/preparation";

const SID = "00000000-0000-4000-8000-000000000001";
const HEAD = "00000000-0000-4000-8000-000000000002";
const RUN = "00000000-0000-4000-8000-000000000003";
const json = (value: unknown) => new Response(JSON.stringify(value));
const view = { workflow_session_id: SID, revision: 3, head_commit_id: HEAD } as PublicSession;
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => vi.unstubAllGlobals());
describe("backend published declarations and scoped reads", () => {
  it("publishes an exact safe A result declaration and restores metadata without observations", async () => {
    const store = useExposuresStore();
    const snapshots: unknown[] = [];
    store.setPersistenceGuard(() => { snapshots.push(store.exportConfiguration()); return true; });
    const row = store.register(MAIN_WORKFLOW_ID, "A", "A.result", "result", ["result_text"]);
    vi.stubGlobal("fetch", vi.fn(async (_path, options) => {
      const command = JSON.parse(options.body);
      return json(JSON.parse(exposureSignature(command.record)));
    }));
    expect(await store.publish(MAIN_WORKFLOW_ID)).toBe(true);
    expect(store.referenceFor(MAIN_WORKFLOW_ID)?.revision).toBe(1);
    expect(snapshots.every(isExposureSnapshot)).toBe(true);
    expect(row.type).toBe("public_node_result");
    const snapshot = store.exportConfiguration();
    setActivePinia(createPinia());
    const restored = useExposuresStore();
    restored.restoreRegistrations([row]);
    expect(restored.restoreConfiguration(snapshot)).toBe(true);
    expect(restored.referenceFor(MAIN_WORKFLOW_ID)).toEqual(store.referenceFor(MAIN_WORKFLOW_ID));
    expect(restored.observations).toEqual({});
  });
  it("keeps the original request across unknown submission without redispatch", async () => {
    const store = useExposuresStore();
    store.setPersistenceGuard(() => true);
    store.register(MAIN_WORKFLOW_ID, "A", "state", "state", ["status"]);
    const fetcher = vi.fn().mockRejectedValueOnce(new Error("lost"))
      .mockImplementationOnce(async (_path, options) => json(JSON.parse(options.body).record));
    vi.stubGlobal("fetch", fetcher);
    expect(await store.publish(MAIN_WORKFLOW_ID)).toBe(false);
    const pending = clonePreparation(store.pending);
    expect(store.referenceFor(MAIN_WORKFLOW_ID)).toBeNull();
    await store.reconcile();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(store.pending).toEqual(pending);
    expect(store.error?.kind).toBe("unknown");
  });
  it("blocks dispatch when original request persistence is unavailable", async () => {
    const store = useExposuresStore();
    store.setPersistenceGuard(() => false);
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect(await store.publish(MAIN_WORKFLOW_ID)).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("rejects undeclared result fields and clears late observations after a session switch", async () => {
    const store = useExposuresStore();
    store.setPersistenceGuard(() => true);
    const row = store.register(MAIN_WORKFLOW_ID, "A", "state", "state", ["status"]);
    vi.stubGlobal("fetch", vi.fn(async (_path, options) => json(JSON.parse(options.body).record)));
    await store.publish(MAIN_WORKFLOW_ID);
    const ref = store.referenceFor(MAIN_WORKFLOW_ID)!;
    const response = {
      schema_version: 1, kind: "workflow_exposure_read", ...ref, workflow_session_id: SID,
      session_revision: 3, head_commit_id: HEAD, availability: "available", reason_code: null,
      registrations: [row], observations: [{ registrationId: row.id, workflowId: MAIN_WORKFLOW_ID,
        workflowSessionId: SID, nodeBindingId: row.nodeBindingId, runId: RUN, schemaVersion: 1,
        availability: "available", value: { status: "running" } }],
    };
    expect(isExposureRead(response, ref, view)).toBe(true);
    const unsafe = structuredClone(response);
    Object.assign(unsafe.observations[0].value, { private_data: "secret" });
    expect(isExposureRead(unsafe, ref, view)).toBe(false);
    let deliver!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { deliver = resolve; })));
    const reading = store.observePublished(MAIN_WORKFLOW_ID, view);
    store.clearObservations();
    deliver(json(response));
    await reading;
    expect(store.observations).toEqual({});
    store.remove(row.id);
    expect(store.referenceFor(MAIN_WORKFLOW_ID)).toBeNull();
  });
  it("passes only exact declaration identities to the local user interface", () => {
    const ref = { config_id: HEAD, revision: 2 };
    const url = new URL(workbenchUserInterfaceUrl("http://127.0.0.1:8765/", SID, null, null, ref)!);
    expect(url.searchParams.get("exposure_config")).toBe(HEAD);
    expect(url.searchParams.get("exposure_revision")).toBe("2");
    expect(workbenchUserInterfaceUrl("https://elsewhere.invalid/", SID, null, null, ref)).toBeNull();
  });
});

describe("native exposure response custody", () => {
  it("retains the original exposure request on first idempotency conflict", async () => {
    const store = useExposuresStore();
    const guard = vi.fn(() => true);
    store.setPersistenceGuard(guard);
    store.register(MAIN_WORKFLOW_ID, "A", "state", "state", ["status"]);
    let original: unknown;
    const fetcher = vi.fn(async () => {
      original = store.exportConfiguration().pending;
      return new Response(JSON.stringify({ error: { reason_code: "idempotency_conflict" } }), { status: 409 });
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await store.publish(MAIN_WORKFLOW_ID)).toBe(false);
    expect(store.pending).toEqual(original);
    expect(store.error?.kind).toBe("unknown");
    expect(store.error?.code).toBe("idempotency_conflict");
    await store.reconcile();
    expect(store.pending).toEqual(original);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(guard).toHaveBeenCalledTimes(1);
  });

  it.each([false, true])("preserves restored request custody when initial persistence throws (restore=%s)", async restore => {
    const store = useExposuresStore();
    store.register(MAIN_WORKFLOW_ID, "A", "state", "state", ["status"]);
    let original = store.exportConfiguration();
    store.setPersistenceGuard(() => {
      original = store.exportConfiguration();
      if (restore) store.restoreConfiguration(original);
      throw new Error("quota");
    });
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect(await store.publish(MAIN_WORKFLOW_ID)).toBe(false);
    if (restore) {
      expect(store.exportConfiguration()).toEqual(original);
      expect(store.pending).not.toBeNull();
      expect(store.error).toBeNull();
    } else {
      expect(store.pending).toBeNull();
      expect(store.error?.kind).toBe("unavailable");
    }
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("retains first definite rejection semantics while successful local settlement clears pending", async () => {
    const store = useExposuresStore();
    store.setPersistenceGuard(() => true);
    store.register(MAIN_WORKFLOW_ID, "A", "state", "state", ["status"]);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { reason_code: "stale_revision" } }), { status: 409 })));
    expect(await store.publish(MAIN_WORKFLOW_ID)).toBe(false);
    expect(store.pending).toBeNull();
    expect(store.error?.code).toBe("stale_revision");
  });

  it.each([200, 409])("retains the original request if settlement cannot be persisted after HTTP %i", async status => {
    const store = useExposuresStore();
    const guard = vi.fn().mockReturnValueOnce(true).mockReturnValue(false);
    store.setPersistenceGuard(guard);
    store.register(MAIN_WORKFLOW_ID, "A", "state", "state", ["status"]);
    let original: unknown;
    const fetcher = vi.fn(async (_path: string, options: RequestInit) => {
      original = store.exportConfiguration().pending;
      return status === 200 ? json(JSON.parse(options.body as string).record)
        : new Response(JSON.stringify({ error: { reason_code: "stale_revision" } }), { status });
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await store.publish(MAIN_WORKFLOW_ID)).toBe(false);
    expect(store.pending).toEqual(original);
    expect(store.error?.kind).toBe("unknown");
    await store.reconcile();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(guard).toHaveBeenCalledTimes(2);
    expect(store.pending).toEqual(original);
  });

  it.each([200, 409, 404, 410])("isolates HTTP %i after restoring the identical native exposure request", async status => {
    const store = useExposuresStore();
    const guard = vi.fn(() => true);
    store.setPersistenceGuard(guard);
    store.register(MAIN_WORKFLOW_ID, "A", "state", "state", ["status"]);
    let deliver!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { deliver = resolve; })));
    const saving = store.publish(MAIN_WORKFLOW_ID);
    const snapshot = store.exportConfiguration();
    expect(store.restoreConfiguration(snapshot)).toBe(true);
    const current = clonePreparation(store.registrations);
    deliver(status === 200 ? json(snapshot.pending!.record)
      : new Response(JSON.stringify({ error: { reason_code: "stale_revision" } }), { status }));
    expect(await saving).toBe(false);
    expect(store.exportConfiguration()).toEqual(snapshot);
    expect(store.registrations).toEqual(current);
    expect(store.error).toBeNull();
    expect(guard).toHaveBeenCalledTimes(1);
  });

  it.each([200, 409])("compares the complete pending exposure body for HTTP %i", async status => {
    const store = useExposuresStore();
    store.setPersistenceGuard(() => true);
    store.register(MAIN_WORKFLOW_ID, "A", "state", "state", ["status"]);
    let deliver!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { deliver = resolve; })));
    const saving = store.publish(MAIN_WORKFLOW_ID);
    const original = store.exportConfiguration().pending!;
    const replacement = clonePreparation(original);
    replacement.record.registrations[0].publicName = "later state";
    store.pending = replacement;
    deliver(status === 200 ? json(original.record)
      : new Response(JSON.stringify({ error: { reason_code: "stale_revision" } }), { status }));
    expect(await saving).toBe(false);
    expect(store.pending).toEqual(replacement);
    expect(store.publications).toEqual([]);
    expect(store.error).toBeNull();
  });

  it("keeps a request restored by persistence during receipt settlement unresolved", async () => {
    const store = useExposuresStore();
    store.register(MAIN_WORKFLOW_ID, "A", "state", "state", ["status"]);
    let original = store.exportConfiguration();
    let writes = 0;
    store.setPersistenceGuard(() => {
      if (++writes === 1) original = store.exportConfiguration();
      else store.restoreConfiguration(original);
      return true;
    });
    const fetcher = vi.fn(async (_path: string, options: RequestInit) => json(JSON.parse(options.body as string).record));
    vi.stubGlobal("fetch", fetcher);
    expect(await store.publish(MAIN_WORKFLOW_ID)).toBe(false);
    expect(store.exportConfiguration()).toEqual(original);
    expect(store.error).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
