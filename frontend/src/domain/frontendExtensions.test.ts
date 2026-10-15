import { describe, expect, it } from "vitest";
import { graphClone } from "./workflowGraph";
import { createFrontendImplementationRegistry, isFrontendExtension, resolveFrontendExtensions, type FrontendExtension } from "./frontendExtensions";
import { workflowFrontendExtensions } from "../plugins/workflowFrontendManifest";

const packages = [{ package_id: "workflow.frontend", version: "1.0.0" }];
const panel: FrontendExtension = { schema_version: 2, host_protocol_version: 1,
  extension_id: "local.panel", kind: "workbench-panel", entrypoint: "local.panel",
  component_id: null, component_version: null, package_id: "workflow.frontend", package_version: "1.0.0",
  binding: { surface: "workbench", slot: "panel", target: {} } };
const renderer = workflowFrontendExtensions[0], fields = workflowFrontendExtensions[1];
describe("trusted frontend host protocol", () => {
  it("accepts all four exact full-package declarations", () => {
    expect(workflowFrontendExtensions.every(isFrontendExtension)).toBe(true);
    expect(resolveFrontendExtensions(workflowFrontendExtensions, packages, workflowFrontendExtensions).available).toEqual(workflowFrontendExtensions);
  });
  it.each(["schema_version", "host_protocol_version", "binding", "component_id"] as const)(
    "rejects incompatible %s without interpreting legacy UI", key => {
      const row = graphClone(panel) as unknown as Record<string, unknown>;
      row[key] = key === "binding" ? { surface: "workbench", slot: "panel", target: { type_id: "guessed" } } : 99;
      expect(isFrontendExtension(row)).toBe(false);
    });
  it("does not load undeclared implementations, disabled packages or guessed versions", () => {
    expect(resolveFrontendExtensions([], packages, workflowFrontendExtensions).available).toEqual([]);
    expect(resolveFrontendExtensions(workflowFrontendExtensions, [], workflowFrontendExtensions).available).toEqual([]);
    const row = graphClone(workflowFrontendExtensions[0]); row.package_version = "latest";
    expect(resolveFrontendExtensions([row], [{ package_id: row.package_id, version: "latest" }], workflowFrontendExtensions).issues[0]).toContain("可信本地");
    row.package_version = "1.0.0"; row.entrypoint = "https://untrusted.example/plugin.js";
    expect(isFrontendExtension(row)).toBe(false);
    expect(resolveFrontendExtensions([row], packages, workflowFrontendExtensions).available).toEqual([]);
  });
  it("rejects every conflicting declaration rather than choosing first or last", () => {
    const first = graphClone(renderer), second = { ...first, extension_id: "other.renderer" };
    expect(resolveFrontendExtensions([first, second], packages, workflowFrontendExtensions).available).toEqual([]);
    expect(resolveFrontendExtensions([first, first], packages, workflowFrontendExtensions).available).toEqual([]);
  });
  it("allows independent panel declarations to coexist and reports unavailable local implementations", () => {
    const first = graphClone(panel), second = { ...first, extension_id: "other.panel" };
    const result = resolveFrontendExtensions([first, second], packages, [panel]);
    expect(result.available).toEqual([first]);
    expect(result.issues).toHaveLength(1); expect(result.issues[0]).toContain("可信本地");
    expect(result.issues[0]).not.toContain("冲突");
  });
  it.each(["extension_id", "package_id", "package_version"] as const)("bounds %s exactly", key => {
    for (const value of ["", " leading", "trailing ", "x".repeat(129)]) {
      expect(isFrontendExtension({ ...workflowFrontendExtensions[0], [key]: value })).toBe(false);
    }
    expect(isFrontendExtension({ ...workflowFrontendExtensions[0], [key]: "x".repeat(128) })).toBe(true);
  });
  it("accepts bounded local symbols and rejects URLs, paths, scripts and oversized entrypoints", () => {
    for (const entrypoint of ["", "local.symbol\n", "https://host/plugin.js", "/plugin.js", "import('plugin')", "x".repeat(513)]) {
      expect(isFrontendExtension({ ...workflowFrontendExtensions[0], entrypoint })).toBe(false);
    }
    expect(isFrontendExtension({ ...workflowFrontendExtensions[0], entrypoint: "plugin.local:panel-v1" })).toBe(true);
    expect(isFrontendExtension({ ...workflowFrontendExtensions[0], entrypoint: "x".repeat(512) })).toBe(true);
  });
  it.each(["component_id", "component_version"] as const)("rejects empty or unbounded node identity %s", key => {
    for (const value of ["", " node", "node ", "x".repeat(129)]) {
      expect(isFrontendExtension({ ...fields, [key]: value,
        binding: { ...fields.binding, target: { ...fields.binding.target, [key]: value } } })).toBe(false);
    }
  });
  it("requires a bounded nonempty object type target", () => {
    for (const type_id of ["", " object", "x".repeat(129)]) {
      expect(isFrontendExtension({ ...renderer,
        binding: { ...renderer.binding, target: { ...renderer.binding.target, type_id } } })).toBe(false);
    }
  });
  it("registers only explicit local declarations, freezes them and rejects partial duplicate registration", () => {
    const registry = createFrontendImplementationRegistry<string>();
    const declaration = graphClone(panel);
    registry.register(declaration, "panel");
    declaration.extension_id = "mutated";
    expect(registry.entries()[0].declaration.extension_id).toBe("local.panel");
    expect(() => registry.registerAll([
      { declaration: renderer, implementation: "renderer" },
      { declaration: panel, implementation: "duplicate" },
    ])).toThrow("重复登记");
    expect(registry.entries()).toHaveLength(1);
    expect(resolveFrontendExtensions(workflowFrontendExtensions, packages, []).available).toEqual([]);
  });
  it("requires schema 3 and independent frontend host 2 for registered node actions", () => {
    const action = {
      schema_version: 3, host_protocol_version: 2, extension_id: "workflow.prompts.group-policy",
      kind: "node-action", entrypoint: "workflow.prompts.workbench.group-policy",
      component_id: "prompts.group", component_version: "3", package_id: "workflow.prompts", package_version: "2.0.0",
      binding: { surface: "workbench", slot: "node-actions", target: { component_id: "prompts.group", component_version: "3" } },
    };
    expect(isFrontendExtension(action)).toBe(true);
    expect(isFrontendExtension({ ...action, schema_version: 2, host_protocol_version: 1 })).toBe(false);
    expect(isFrontendExtension({ ...action, host_protocol_version: 1 })).toBe(false);
    expect(isFrontendExtension({ ...action, binding: { ...action.binding, target: { ...action.binding.target, component_version: "2" } } })).toBe(false);
    if (!isFrontendExtension(action)) throw new Error("invalid fixture");
    const registry = createFrontendImplementationRegistry<string>();
    registry.register(action, "policy");
    expect(resolveFrontendExtensions([action], [{ package_id: action.package_id, version: "2.0.0" }],
      registry.entries().map(row => row.declaration)).available).toEqual([action]);
    expect(resolveFrontendExtensions([action], [{ package_id: action.package_id, version: "1.0.0" }],
      registry.entries().map(row => row.declaration)).available).toEqual([]);
  });
});
