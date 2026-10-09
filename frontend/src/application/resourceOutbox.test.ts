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
  it("imports a browser original only on explicit action and never replays its mutation", async () => {
    const original = request(), raw = JSON.stringify(original);
    let durable: string | null = null;
    const state = fixture({ readPending: async () => durable, readLegacyPending: () => raw,
      importLegacyPending: vi.fn(async value => { durable = value; }),
      readReceipt: vi.fn(async () => { throw new WorkbenchApiError("unknown", "no original receipt"); }) });
    expect(await state.outbox.initialize()).toBe(true);
    expect(state.outbox.legacyRaw.value).toBe(raw); expect(state.outbox.locked.value).toBe(true);
    expect(await state.outbox.send(request())).toBe(false);
    expect(state.ports.importLegacyPending).not.toHaveBeenCalled(); expect(state.ports.save).not.toHaveBeenCalled();
    expect(await state.outbox.importLegacyPending()).toBe(true);
    expect(durable).toBe(raw); expect(state.outbox.pending.value).toEqual(original);
    expect(state.ports.save).not.toHaveBeenCalled();
    expect(await state.outbox.reconcile()).toBe(false);
    expect(state.ports.readReceipt).toHaveBeenCalledWith(original);
    expect(state.ports.save).not.toHaveBeenCalled(); expect(durable).toBe(raw);
  });
});
