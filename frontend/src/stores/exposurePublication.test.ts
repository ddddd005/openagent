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
  it("keeps the original request across unknown submission and replays without a new identity", async () => {
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
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual(pending);
    expect(store.pending).toBeNull();
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
    const url = new URL(workbenchUserInterfaceUrl("http://127.0.0.1:8765/", SID, null, null, ref));
    expect(url.searchParams.get("exposure_config")).toBe(HEAD);
    expect(url.searchParams.get("exposure_revision")).toBe("2");
    expect(new URL(workbenchUserInterfaceUrl("https://elsewhere.invalid/", SID, null, null, ref)).search).toBe("");
  });
});
