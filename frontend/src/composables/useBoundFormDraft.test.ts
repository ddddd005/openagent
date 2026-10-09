import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { createRenderer, defineComponent, nextTick, ref, type App, type Ref } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { localDraftPorts } from "../adapters/localDraftsApi";
import { useFormDraftsStore } from "../stores/formDrafts";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { newProvider } from "../domain/workflowModelResources";
import { useBoundFormDraft } from "./useBoundFormDraft";
import { useResourceFormDraft } from "./useResourceFormDraft";

const renderer = createRenderer<object, object>({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
  parentNode: () => null, nextSibling: () => null,
});
const apps: App<object>[] = [], piniaInstances: Pinia[] = [];
let pinia: Pinia, disk: ReturnType<typeof memoryLocalDraftStorage>;
function freshPinia() {
  pinia = createPinia(); piniaInstances.push(pinia); setActivePinia(pinia);
}
async function settled() { for (let count = 0; count < 8; count++) { await Promise.resolve(); await nextTick(); } }
function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
function mount<T>(setup: () => T) {
  let binding!: T;
  const app = renderer.createApp(defineComponent({ setup() { binding = setup(); return () => null; } })).use(pinia);
  apps.push(app); app.mount({});
  return { app, binding };
}
function bind(id: Ref<string>, basis: Ref<unknown>, raw: Ref<string>) {
  return mount(() => useBoundFormDraft({
    enabled: true, id: () => id.value, basis: () => basis.value, capture: () => raw.value,
    reset: () => { raw.value = ""; },
    restore: value => {
      if (typeof value !== "string") throw new Error("Invalid raw form");
      raw.value = value;
    },
  }));
}
beforeEach(() => {
  freshPinia(); disk = memoryLocalDraftStorage();
  vi.spyOn(localDraftPorts, "read").mockImplementation(disk.ports.read);
  vi.spyOn(localDraftPorts, "save").mockImplementation(disk.ports.save);
  vi.spyOn(localDraftPorts, "receipt").mockImplementation(disk.ports.receipt);
});
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount());
  piniaInstances.splice(0).forEach(disposePinia);
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe("bound local forms", () => {
  it("resets local fields on context changes even when persistence is disabled", async () => {
    const id = ref("node:first"), raw = ref("stale");
    mount(() => useBoundFormDraft({
      enabled: false, id: () => id.value, basis: () => ({}),
      capture: () => raw.value, restore: value => { raw.value = value; },
      reset: () => { raw.value = id.value; },
    }));
    expect(raw.value).toBe("node:first");
    id.value = "node:second"; await settled();
    expect(raw.value).toBe("node:second");
    expect(localDraftPorts.read).not.toHaveBeenCalled();
    expect(localDraftPorts.save).not.toHaveBeenCalled();
  });
  it.each(['{"incomplete":', "-", "1e", "Long prompt ".repeat(1000)])(
    "restores exact unapplied text after a confirmed write and fresh store", async value => {
      const id = ref("node:form"), basis = ref<unknown>({ config: 1 }), raw = ref("");
      const first = bind(id, basis, raw); await settled();
      raw.value = value; expect(await first.binding.flush()).toBe(true);
      first.app.unmount(); freshPinia();
      const reopened = ref("default"), second = bind(id, basis, reopened); await settled();
      expect(reopened.value).toBe(value); expect(second.binding.conflict.value).toBe(false);
    },
  );
  it("retains each node's staged input during selection changes", async () => {
    const id = ref("node:first"), basis = ref<unknown>({}), raw = ref("");
    const editor = bind(id, basis, raw); await settled();
    raw.value = "first invalid {"; expect(await editor.binding.flush()).toBe(true);
    id.value = "node:second"; await settled(); expect(raw.value).toBe("");
    raw.value = "second invalid ["; expect(await editor.binding.flush()).toBe(true);
    id.value = "node:first"; await settled(); expect(raw.value).toBe("first invalid {");
    id.value = "node:second"; await settled(); expect(raw.value).toBe("second invalid [");
  });
  it("never applies an old form to a changed configuration without explicit restoration", async () => {
    const id = ref("node:config"), basis = ref<unknown>({ config: 1 }), raw = ref("");
    const editor = bind(id, basis, raw); await settled();
    raw.value = "old retained text"; await editor.binding.flush();
    basis.value = { config: 2 }; await settled();
    expect(raw.value).toBe(""); expect(editor.binding.conflict.value).toBe(true);
    expect(await editor.binding.flush()).toBe(false);
    editor.binding.restoreConflict();
    expect(raw.value).toBe("old retained text"); expect(await editor.binding.flush()).toBe(true);
    const value = [...disk.records.values()][0]!.value;
    expect(value).toEqual({ basis: { config: 2 }, raw: "old retained text" });
  });
  it("discards a late read from the previous node", async () => {
    const delayed = deferred<null>();
    vi.mocked(localDraftPorts.read).mockImplementationOnce(() => delayed.promise);
    const id = ref("node:slow"), basis = ref<unknown>({}), raw = ref("");
    const editor = bind(id, basis, raw);
    id.value = "node:current"; await settled(); raw.value = "current unsaved text";
    delayed.resolve(null); await settled();
    expect(raw.value).toBe("current unsaved text"); expect(await editor.binding.flush()).toBe(true);
  });
  it("does not clear another node's conflict after a delayed discard", async () => {
    const store = useFormDraftsStore();
    await store.load("node:other", { config: 1 });
    store.stage("node:other", { config: 1 }, "other retained text");
    expect(await store.flush("node:other")).toBe(true);
    const id = ref("node:first"), basis = ref<unknown>({ config: 1 }), raw = ref("");
    const editor = bind(id, basis, raw); await settled();
    raw.value = "first retained text"; expect(await editor.binding.flush()).toBe(true);
    basis.value = { config: 2 }; await settled();
    expect(editor.binding.conflict.value).toBe(true);
    const delayed = deferred<void>(), save = disk.ports.save;
    vi.mocked(localDraftPorts.save).mockImplementationOnce(async request => {
      await delayed.promise; return save(request);
    });
    const discarding = editor.binding.discardConflict(); await settled();
    id.value = "node:other"; await settled();
    expect(editor.binding.conflict.value).toBe(true);
    delayed.resolve(undefined); await discarding; await settled();
    expect(editor.binding.conflict.value).toBe(true);
    expect(await editor.binding.flush()).toBe(false);
    editor.binding.restoreConflict();
    expect(raw.value).toBe("other retained text");
  });
  it("rejects a submit flush whose context changed while writing", async () => {
    const delayed = deferred<void>();
    const save = disk.ports.save;
    vi.mocked(localDraftPorts.save).mockImplementationOnce(async request => { await delayed.promise; return save(request); });
    const id = ref("node:before"), basis = ref<unknown>({}), raw = ref("");
    const editor = bind(id, basis, raw); await settled();
    raw.value = "before"; const saving = editor.binding.flush(); await settled();
    id.value = "node:after"; await settled(); delayed.resolve(undefined);
    expect(await saving).toBe(false);
  });
  it("keeps in-memory raw content and reports a disk failure until a verified retry", async () => {
    const id = ref("node:disk-failure"), basis = ref<unknown>({}), raw = ref("");
    const editor = bind(id, basis, raw); await settled();
    raw.value = "{unsubmitted";
    vi.mocked(localDraftPorts.save).mockImplementationOnce(async request => {
      await disk.ports.save(request); throw new Error("Disk unavailable");
    });
    expect(await editor.binding.flush()).toBe(false);
    expect(raw.value).toBe("{unsubmitted"); expect(editor.binding.message.value).toContain("Disk unavailable");
    expect(await editor.binding.flush()).toBe(true);
    expect(useFormDraftsStore().hasUnsaved).toBe(false);
  });
  it("advances the basis only for an editor's explicit accepted local patch", async () => {
    const id = ref("node:self-edit"), basis = ref<unknown>({ config: 1 }), raw = ref("");
    const editor = bind(id, basis, raw); await settled();
    raw.value = "raw unchanged";
    expect(editor.binding.adoptBasis({ config: 2 })).toBe(true);
    basis.value = { config: 2 }; await settled();
    expect(editor.binding.conflict.value).toBe(false); expect(raw.value).toBe("raw unchanged");
    expect(await editor.binding.flush()).toBe(true);
  });
  it("preserves inactive resource forms and restores them independently", async () => {
    vi.stubGlobal("window", {});
    const first = newProvider(), second = newProvider();
    const editor = mount(() => useResourceFormDraft("provider", newProvider, row => row.value.name));
    await settled();
    editor.binding.edit(first); editor.binding.form.value!.value.name = "Unsubmitted first";
    editor.binding.edit(second); editor.binding.form.value!.value.name = "Unsubmitted second";
    expect(editor.binding.choices.value).toHaveLength(1);
    expect(await editor.binding.localDraft.flush()).toBe(true);
    editor.app.unmount(); freshPinia();
    const reopened = mount(() => useResourceFormDraft("provider", newProvider, row => row.value.name));
    await settled();
    expect(reopened.binding.form.value!.value.name).toBe("Unsubmitted second");
    reopened.binding.resume(reopened.binding.choices.value[0]!.id);
    expect(reopened.binding.form.value!.value.name).toBe("Unsubmitted first");
  });
});
