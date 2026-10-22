import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { useWorkbenchNoticesStore } from "./workbenchNotices";

let pinia: Pinia;
beforeEach(() => { vi.useFakeTimers(); pinia = createPinia(); setActivePinia(pinia); });
afterEach(() => { disposePinia(pinia); vi.useRealTimers(); });
describe("project notice lifetimes", () => {
  it("times info, success and warning at five seconds while major issues require dismissal", () => {
    const store = useWorkbenchNoticesStore();
    for (const severity of ["info", "success", "warning", "major"] as const)
      store.publish({ id: severity, severity, title: severity, reason: "message", requestId: "" });
    vi.advanceTimersByTime(4999);
    expect(store.notices).toHaveLength(4);
    vi.advanceTimersByTime(1);
    expect(store.notices.map(row => row.id)).toEqual(["major"]);
    vi.advanceTimersByTime(60000);
    expect(store.notices.map(row => row.id)).toEqual(["major"]);
    store.dismiss("major");
    expect(store.notices).toEqual([]);
  });
  it("preserves simultaneous major issues against later normal messages and old timer callbacks", () => {
    const store = useWorkbenchNoticesStore();
    const old = store.publish({ id: "one", severity: "warning", title: "warning", reason: "old", requestId: "" });
    vi.advanceTimersByTime(4000);
    const major = store.publish({ id: "one", severity: "major", title: "problem", reason: "current", requestId: "" });
    store.publish({ id: "two", severity: "major", title: "problem", reason: "other", requestId: "" });
    store.publish({ severity: "info", title: "notice", reason: "new", requestId: "" });
    store.clearError(old);
    store.clearError(major);
    store.publish({ id: "one", severity: "success", title: "done", reason: "ordinary", requestId: "" });
    vi.advanceTimersByTime(10000);
    expect(store.notices.map(row => row.id)).toEqual(["one", "two"]);
    expect(store.notices[0]?.sequence).toBe(major);
    store.dismiss("one");
    expect(store.notices.map(row => row.id)).toEqual(["two"]);
  });
  it("does not restart a duplicate notice or execute the domain action on dismissal", () => {
    const store = useWorkbenchNoticesStore(), action = vi.fn();
    const input = { id: "issue", severity: "warning" as const, title: "check", reason: "same",
      requestId: "request", action: { label: "verify", run: action } };
    const first = store.publish(input);
    vi.advanceTimersByTime(4000);
    expect(store.publish(input)).toBe(first);
    vi.advanceTimersByTime(1000);
    expect(store.notices).toEqual([]);
    expect(action).not.toHaveBeenCalled();
    store.publish({ ...input, severity: "major" });
    store.dismiss("issue");
    expect(action).not.toHaveBeenCalled();
  });
});
