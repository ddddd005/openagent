import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

function load() {
  const scope: Record<string, any> = {};
  for (const name of ["graph-chat-core.js", "frontend-package-host.js", "frontend-package.js"])
    runInNewContext(readFileSync(new URL(`../../../backend/src/phase1_agent/static/${name}`, import.meta.url), "utf8"), scope);
  return scope;
}
const { WorkflowFrontendHost, WorkflowFrontendPackage } = load();
const declaration = JSON.parse(JSON.stringify(WorkflowFrontendPackage.declaration));
const output = { node_binding_id: "node", port_id: "display", data_type: "FRONTEND_DISPLAY", data_schema_version: 1,
  availability: "produced", payload: { schema_version: 1, kind: "workflow.frontend-display", entries: [
    { entry_id: "00000000-0000-4000-8000-000000000001", role: "assistant",
      source_ref: { scope: "artifact", output_id: "00000000-0000-4000-8000-000000000002" } }] } };
function selection(extensions = [declaration], packages = [{ package_id: "workflow.frontend", version: "1.0.0" }]) {
  return { package_lock: packages, frontend_extensions: extensions };
}
class Element {
  children: Element[] = [];
  ownerDocument = { createElement: (_name: string) => new Element() };
  textContent = ""; disabled = false; type = "";
  onclick: (() => Promise<void>) | null = null;
  append(...children: Element[]) { this.children.push(...children); }
  replaceChildren(...children: Element[]) { this.children = children; }
}
function client() {
  return { consumer: { outputs: [output] }, publicHistory: [],
    informationContext: () => ({ session: "session", definition: 1, generation: 0 }),
    readFrontendExtensions: vi.fn().mockResolvedValue(selection()), readOutputArtifact: vi.fn() };
}
function deferred() {
  let resolve: (value: any) => void = () => {}, reject: (reason: any) => void = () => {};
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
describe("trusted consumer frontend packages", () => {
  it("mounts a registered independent plugin using only its exact declaration and narrow read/event SDK", async () => {
    const local = { ...declaration, extension_id: "sample.view", package_id: "sample", entrypoint: "sample.renderer",
      binding: { ...declaration.binding, target: { scope: "content", type_id: "SAMPLE", schema_version: 4 } } };
    const customOutput = { ...output, data_type: "SAMPLE", data_schema_version: 4 }, api = client();
    api.consumer.outputs = [customOutput];
    api.readFrontendExtensions.mockResolvedValue(selection([local], [{ package_id: "sample", version: "1.0.0" }]));
    const host = new WorkflowFrontendHost.ConsumerFrontendHost(api), dispose = vi.fn(), mount = vi.fn(context => {
      expect(Object.keys(context.sdk).sort()).toEqual(["current", "readArtifact", "readEvent", "readEventBindings", "submitEvent"]);
      expect(Object.isFrozen(context.output)).toBe(true);
      return dispose;
    });
    host.register(local, mount);
    const handle = host.attach(customOutput, new Element());
    expect(await handle.ready).toBe("mounted"); expect(mount).toHaveBeenCalledOnce();
    handle.dispose(); handle.dispose(); expect(dispose).toHaveBeenCalledOnce();
    expect(api.readOutputArtifact).not.toHaveBeenCalled();
  });
  it("delegates public event ports through the client and stops obsolete mounts from submitting", async () => {
    const api = { ...client(), readEventBindings: vi.fn().mockResolvedValue({ bindings: [] }),
      submitEvent: vi.fn().mockResolvedValue({ accepted: true }), readEvent: vi.fn().mockResolvedValue({ status: "succeeded" }) };
    const host = new WorkflowFrontendHost.ConsumerFrontendHost(api);
    let sdk: any;
    host.register(declaration, (context: any) => { sdk = context.sdk; return () => {}; });
    const handle = host.attach(output, new Element()); expect(await handle.ready).toBe("mounted");
    expect(await sdk.readEventBindings()).toEqual({ bindings: [] });
    expect(await sdk.submitEvent({ event_id: "edit" }, { edit: "value" })).toEqual({ accepted: true });
    expect(await sdk.readEvent("chain")).toEqual({ status: "succeeded" });
    expect(api.submitEvent).toHaveBeenCalledWith({ event_id: "edit" }, { edit: "value" });
    await expect(sdk.submitEvent({ event_id: "edit" }, { edit: NaN })).rejects.toThrow("JSON");
    expect(api.submitEvent).toHaveBeenCalledOnce();
    handle.dispose();
    expect(await sdk.readEventBindings()).toBeNull();
    await expect(sdk.submitEvent({ event_id: "edit" }, {})).rejects.toThrow("过期");
    expect(api.submitEvent).toHaveBeenCalledOnce();
  });
  it.each(["unregistered", "implementation-unavailable"])("keeps generic content for %s UI", async expected => {
    const api = client(), host = new WorkflowFrontendHost.ConsumerFrontendHost(api), container = new Element();
    container.textContent = "original immutable value";
    if (expected === "unregistered") api.readFrontendExtensions.mockResolvedValue(selection([], []));
    expect(await host.attach(output, container).ready).toBe(expected);
    expect(container.textContent).toBe("original immutable value"); expect(api.readOutputArtifact).not.toHaveBeenCalled();
  });
  it.each(["v1", "protocol", "duplicate", "lock", "target", "remote", "scope"])(
    "rejects an invalid %s selection before mounting", async kind => {
      const local = JSON.parse(JSON.stringify(declaration)), packet = selection([local]);
      if (kind === "v1") local.schema_version = 1;
      if (kind === "protocol") local.host_protocol_version = 2;
      if (kind === "duplicate") packet.frontend_extensions.push({ ...local, extension_id: "second" });
      if (kind === "lock") packet.package_lock[0]!.version = "wrong";
      if (kind === "target") local.binding.target.type_id = "PRIVATE";
      if (kind === "remote") local.entrypoint = "https://example.invalid/plugin.js";
      if (kind === "scope") local.binding.surface = "workbench";
      const api = client(); api.readFrontendExtensions.mockResolvedValue(packet);
      const host = new WorkflowFrontendHost.ConsumerFrontendHost(api); WorkflowFrontendPackage.install(host);
      await expect(host.attach(output, new Element()).ready).rejects.toThrow("界面");
      expect(api.readOutputArtifact).not.toHaveBeenCalled();
    });
  it("unmounts old package renderers when a newer selection disables the UI", async () => {
    const api = client(), host = new WorkflowFrontendHost.ConsumerFrontendHost(api), dispose = vi.fn();
    host.register(declaration, () => dispose);
    const first = host.attach(output, new Element()); expect(await first.ready).toBe("mounted");
    api.readFrontendExtensions.mockResolvedValueOnce(selection([], []));
    expect(await host.attach(output, new Element()).ready).toBe("unregistered");
    expect(dispose).toHaveBeenCalledOnce();
  });
  it("prevents a late old package selection from re-enabling a disabled renderer", async () => {
    const api = client(), host = new WorkflowFrontendHost.ConsumerFrontendHost(api), old = deferred(), newer = deferred(), mount = vi.fn(() => vi.fn());
    host.register(declaration, mount);
    api.readFrontendExtensions.mockImplementationOnce(() => old.promise).mockImplementationOnce(() => newer.promise);
    const first = host.attach(output, new Element()), second = host.attach(output, new Element());
    newer.resolve(selection([], [])); expect(await second.ready).toBe("unregistered");
    old.resolve(selection()); expect(await first.ready).toBe("obsolete"); expect(mount).not.toHaveBeenCalled();
  });
  it.each(["success", "failure"])("drops a declaration %s after disposal", async kind => {
    const api = client(), pending = deferred(), host = new WorkflowFrontendHost.ConsumerFrontendHost(api), mount = vi.fn(() => vi.fn());
    api.readFrontendExtensions.mockReturnValue(pending.promise); host.register(declaration, mount);
    const handle = host.attach(output, new Element()); handle.dispose();
    if (kind === "success") pending.resolve(selection()); else pending.reject(new Error("stale"));
    expect(await handle.ready).toBe("obsolete"); expect(mount).not.toHaveBeenCalled();
  });
  it.each(["success", "failure"])("prevents a disposed built-in renderer's late artifact %s from updating DOM", async kind => {
    const api = client(), pending = deferred(), host = new WorkflowFrontendHost.ConsumerFrontendHost(api), container = new Element();
    api.readOutputArtifact.mockReturnValue(pending.promise); WorkflowFrontendPackage.install(host);
    const handle = host.attach(output, container); expect(await handle.ready).toBe("mounted");
    const row = container.children[1]!, button = row.children[2]!, body = row.children[3]!;
    const reading = button.onclick!(); expect(body.textContent).toBe("正在读取正文");
    handle.dispose(); expect(container.children).toEqual([]); expect(button.onclick).toBeNull();
    if (kind === "success") pending.resolve({ data_type: "TEXT", data_schema_version: 2,
      value: { schema_version: 2, kind: "workflow.text", text: "late" }, producer: {} });
    else pending.reject(new Error("late"));
    await reading; expect(body.textContent).toBe("正在读取正文");
  });
  it("renders only explicitly supported TEXT@2 bodies and owns no execution control", async () => {
    const api = client(), host = new WorkflowFrontendHost.ConsumerFrontendHost(api), container = new Element();
    api.readOutputArtifact.mockResolvedValue({ data_type: "TEXT", data_schema_version: 2,
      value: { schema_version: 2, kind: "workflow.text", text: "result" }, producer: {
        workflow_session_id: "s", chain_run_id: "c", node_run_id: "r" } });
    WorkflowFrontendPackage.install(host);
    expect(await host.attach(output, container).ready).toBe("mounted");
    const row = container.children[1]!; await row.children[2]!.onclick!();
    expect(row.children[3]!.textContent).toBe("result");
    expect(api.readOutputArtifact).toHaveBeenCalledWith(output, output.payload.entries[0]!.source_ref);
    host.dispose();
  });
  it("keeps loading the platform shell when optional package scripts are missing", async () => {
    const calls: string[] = [], box = { textContent: "", hidden: true };
    const scope = { URLSearchParams, location: { search: "?graph_workflow=00000000-0000-4000-8000-000000000021" },
      document: { getElementById: () => box, createElement: () => ({}),
        head: { append(script: any) { calls.push(script.src); queueMicrotask(() => {
          if (script.src.includes("frontend-package")) script.onerror(); else script.onload();
        }); } } } };
    await runInNewContext(readFileSync(new URL("../../../backend/src/phase1_agent/static/chat-entry.js", import.meta.url), "utf8"), scope);
    expect(calls.at(-1)).toBe("/static/graph-chat.js"); expect(box.hidden).toBe(true);
  });
});
