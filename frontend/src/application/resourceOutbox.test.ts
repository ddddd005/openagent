import { describe, expect, it, vi } from "vitest";
import { createResourceOutbox, type ResourceOutboxPorts } from "./resourceOutbox";
import { WorkbenchApiError } from "../adapters/workbenchApi";

interface Request { idempotency_key: string; text: string }
const valid = (value: unknown): value is Request => value !== null && typeof value === "object"
  && typeof (value as Request).idempotency_key === "string" && typeof (value as Request).text === "string";
const request = (): Request => ({ idempotency_key: crypto.randomUUID(), text: "unfinished raw content" });
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (failure: unknown) => void;
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}
function fixture(overrides: Partial<ResourceOutboxPorts<Request>> = {}) {
  let durable: string | null = null;
  const ports: ResourceOutboxPorts<Request> = { save: vi.fn(async () => ({})), readReceipt: vi.fn(async () => ({})),
    readPending: () => durable, writePending: value => { durable = value; }, ...overrides };
  const refresh = vi.fn(async () => {});
  return { ports, refresh, outbox: createResourceOutbox(ports, valid, refresh, "test"), durable: () => durable };
}

describe("async resource mutation custody", () => {
  it("keeps new pending B when the late business confirmation for A arrives", async () => {
    let durable: string | null = null;
    const gate = deferred<void>(), entered = deferred<void>();
    const original = request(), next = request(), clear = vi.fn();
    const state = fixture({ readPending: () => durable, writePending: value => {
      if (value === null) clear();
      durable = value;
    }, save: vi.fn(async () => { entered.resolve(); await gate.promise; }) });
    const sending = state.outbox.send(original);
    await entered.promise;
    durable = JSON.stringify(next);
    gate.resolve();
    expect(await sending).toBe(false);
    expect(JSON.parse(durable!)).toEqual(next);
    expect(clear).not.toHaveBeenCalled();
    expect(state.ports.save).toHaveBeenCalledOnce();
    expect(state.ports.readReceipt).not.toHaveBeenCalled();
    expect(state.outbox.locked.value).toBe(true);
  });
  it("retains the original until the exact accepted form state has been persisted", async () => {
    const gate = deferred<boolean>(), called = deferred<void>();
    const state = fixture();
    const handler = vi.fn(async () => { called.resolve(); return gate.promise; });
    state.outbox.setCompletionHandler(handler);
    const original = request(), sending = state.outbox.send(original);
    await called.promise;
    expect(state.outbox.locked.value).toBe(true);
    expect(JSON.parse(state.durable()!)).toEqual(original);
    gate.resolve(false);
    expect(await sending).toBe(false);
    expect(state.outbox.pending.value).toEqual(original);
    expect(state.refresh).not.toHaveBeenCalled();
    handler.mockImplementation(async () => true);
    expect(await state.outbox.reconcile()).toBe(true);
    expect(state.ports.save).toHaveBeenCalledOnce();
    expect(state.ports.readReceipt).toHaveBeenCalledOnce();
    expect(state.durable()).toBeNull();
  });
  it("locks synchronously and waits for durable intent before dispatching", async () => {
    const gate = deferred<void>(), writes: (string | null)[] = [];
    const state = fixture({ writePending: async value => {
      writes.push(value);
      if (value !== null) await gate.promise;
    } });
    const original = request(), sending = state.outbox.send(original);
    original.text = "changed caller";
    expect(state.outbox.busy.value).toBe(true); expect(state.outbox.locked.value).toBe(true);
    expect(state.ports.save).not.toHaveBeenCalled();
    expect(await state.outbox.send(request())).toBe(false);
    expect(await state.outbox.reconcile()).toBe(false);
    gate.resolve();
    expect(await sending).toBe(true);
    expect(state.ports.save).toHaveBeenCalledWith(expect.objectContaining({ text: "unfinished raw content" }));
    expect(writes).toHaveLength(2); expect(writes[1]).toBeNull();
  });
  it("does not report completion or refresh until accepted-state clearing is durable", async () => {
    const gate = deferred<void>(), reachedClear = deferred<void>();
    let durable: string | null = null, completed = false;
    const state = fixture({ readPending: () => durable, writePending: async value => {
      if (value === null) { reachedClear.resolve(); await gate.promise; }
      durable = value;
    } });
    const sending = state.outbox.send(request()).then(value => { completed = true; return value; });
    await reachedClear.promise;
    expect(completed).toBe(false); expect(state.outbox.pending.value).not.toBeNull();
    expect(state.outbox.locked.value).toBe(true); expect(durable).not.toBeNull();
    expect(state.refresh).not.toHaveBeenCalled();
    gate.resolve();
    expect(await sending).toBe(true); expect(durable).toBeNull();
    expect(state.refresh).toHaveBeenCalledOnce();
  });
  it("retains an accepted original when its deferred clear fails and later reads only its receipt", async () => {
    const gate = deferred<void>(), reachedClear = deferred<void>();
    let durable: string | null = null, failClear = true;
    const state = fixture({ readPending: () => durable, writePending: async value => {
      if (value === null && failClear) { reachedClear.resolve(); await gate.promise; }
      durable = value;
    } });
    const sending = state.outbox.send(request());
    await reachedClear.promise;
    const original = durable;
    gate.reject(new Error("disk clear failed"));
    expect(await sending).toBe(false);
    expect(durable).toBe(original); expect(state.outbox.pending.value).toEqual(JSON.parse(original!));
    expect(state.outbox.locked.value).toBe(true);
    failClear = false;
    expect(await state.outbox.reconcile()).toBe(true);
    expect(state.ports.save).toHaveBeenCalledOnce();
    expect(state.ports.readReceipt).toHaveBeenCalledWith(JSON.parse(original!));
  });
  it("only reads the browser original receipt without importing, restoring forms, or replaying its mutation", async () => {
    const original = request(), raw = JSON.stringify(original);
    let durable: string | null = null;
    const state = fixture({ readPending: async () => durable, readLegacyPending: () => raw,
      readReceipt: vi.fn(async () => { throw new WorkbenchApiError("unknown", "no original receipt"); }) });
    const accept = vi.fn(async () => true);
    state.outbox.setCompletionHandler(accept);
    expect(await state.outbox.initialize()).toBe(true);
    expect(state.outbox.legacyRaw.value).toBe(raw); expect(state.outbox.locked.value).toBe(true);
    expect(await state.outbox.send(request())).toBe(false);
    expect(state.ports.save).not.toHaveBeenCalled();
    expect(await state.outbox.reconcileLegacyPending()).toBe(false);
    expect(state.ports.readReceipt).toHaveBeenCalledWith(original);
    expect(state.ports.save).not.toHaveBeenCalled(); expect(durable).toBeNull();
    expect(state.outbox.pending.value).toBeNull(); expect(state.outbox.legacyRaw.value).toBe(raw);
    expect(accept).not.toHaveBeenCalled(); expect(state.refresh).not.toHaveBeenCalled();
    vi.mocked(state.ports.readReceipt).mockResolvedValueOnce({});
    expect(await state.outbox.reconcileLegacyPending()).toBe(true);
    expect(state.outbox.legacyRaw.value).toBe(raw); expect(state.outbox.locked.value).toBe(true);
    expect(accept).not.toHaveBeenCalled(); expect(state.refresh).not.toHaveBeenCalled();
  });
  it.each(["saved", "rejected", "unknown"])("compares %s requests by JSON content after backend key sorting", async outcome => {
    let durable: string | null = null;
    const original = request();
    const state = fixture({
      readPending: () => durable,
      writePending: value => {
        durable = value === null ? null : JSON.stringify({ text: JSON.parse(value).text,
          idempotency_key: JSON.parse(value).idempotency_key });
      },
      save: vi.fn(async () => {
        if (outcome !== "saved") throw new WorkbenchApiError(outcome === "rejected" ? "rejected" : "unknown", outcome, 400);
      }),
    });
    expect(await state.outbox.send(original)).toBe(outcome === "saved");
    expect(state.outbox.pending.value === null).toBe(outcome !== "unknown");
    if (outcome === "saved") expect(state.refresh).toHaveBeenCalledOnce();
    else expect(state.outbox.error.value).toBe(outcome);
  });
  it("does not treat array reordering or a changed numeric type as the original request", async () => {
    let durable: string | null = null;
    const state = fixture({
      readPending: () => durable,
      writePending: value => { durable = value; },
      save: vi.fn(async () => {
        durable = JSON.stringify({ ...JSON.parse(durable!), text: 12 });
      }),
    });
    expect(await state.outbox.send(request())).toBe(false);
    expect(state.outbox.pending.value).not.toBeNull();
    expect(state.refresh).not.toHaveBeenCalled();
  });
  it("archives and acknowledges the exact unknown request before explicit abandonment without a replay", async () => {
    const archive = vi.fn(async (_raw: string) => {});
    const state = fixture({ archivePending: archive,
      save: vi.fn(async () => { throw new WorkbenchApiError("unknown", "missing receipt"); }) });
    await state.outbox.send(request());
    const raw = state.durable();
    expect(await state.outbox.abandon(false)).toBe(false);
    expect(archive).not.toHaveBeenCalled();
    expect(await state.outbox.abandon(true)).toBe(true);
    expect(archive).toHaveBeenCalledWith(raw);
    expect(state.durable()).toBeNull();
    expect(state.ports.save).toHaveBeenCalledOnce();
  });
  it("keeps unknown evidence when its archive fails", async () => {
    const state = fixture({ archivePending: async () => { throw new Error("archive unavailable"); },
      save: async () => { throw new WorkbenchApiError("unknown", "missing receipt"); } });
    await state.outbox.send(request());
    const raw = state.durable();
    expect(await state.outbox.abandon(true)).toBe(false);
    expect(state.durable()).toBe(raw);
    expect(state.outbox.pending.value).not.toBeNull();
  });
});
