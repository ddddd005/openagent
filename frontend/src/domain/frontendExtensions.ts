import { graphClone, graphObject } from "./workflowGraph";

export interface FrontendExtension {
  schema_version: 2;
  host_protocol_version: 1;
  extension_id: string;
  kind: "workbench-panel" | "renderer" | "field-editor" | "consumer";
  entrypoint: string;
  component_id: string | null;
  component_version: string | null;
  package_id: string;
  package_version: string;
  binding: { surface: "workbench" | "consumer"; slot: "panel" | "session-object" | "node-fields" | "public-output"; target: Record<string, unknown> };
}

function exactKeys(value: Record<string, unknown>, keys: string[]) {
  return Object.keys(value).length === keys.length && keys.every(key => key in value);
}
const boundedName = (value: unknown): value is string => typeof value === "string"
  && value.length > 0 && value.length <= 128 && value.trim() === value;
export function isFrontendExtension(value: unknown): value is FrontendExtension {
  if (!graphObject(value) || !exactKeys(value, ["schema_version", "host_protocol_version", "extension_id", "kind",
    "entrypoint", "component_id", "component_version", "package_id", "package_version", "binding"])
    || value.schema_version !== 2 || value.host_protocol_version !== 1
    || !["extension_id", "package_id", "package_version"].every(key => boundedName(value[key]))
    || typeof value.entrypoint !== "string" || value.entrypoint.length > 512 || value.entrypoint.trim() !== value.entrypoint
    || !/^[A-Za-z_][A-Za-z0-9_.:-]*$/.test(value.entrypoint)
    || !graphObject(value.binding) || !exactKeys(value.binding, ["surface", "slot", "target"])
    || !graphObject(value.binding.target)) return false;
  const { surface, slot, target } = value.binding;
  if (slot === "panel") return surface === "workbench" && value.kind === "workbench-panel"
    && value.component_id === null && value.component_version === null && exactKeys(target, []);
  if (slot === "node-fields") return surface === "workbench" && value.kind === "field-editor"
    && exactKeys(target, ["component_id", "component_version"]) && boundedName(target.component_id)
    && boundedName(target.component_version) && target.component_id === value.component_id
    && target.component_version === value.component_version;
  return (slot === "session-object" && surface === "workbench" && value.kind === "renderer" && target.scope === "session"
    || slot === "public-output" && surface === "consumer" && value.kind === "consumer" && target.scope === "content")
    && value.component_id === null && value.component_version === null
    && exactKeys(target, ["scope", "type_id", "schema_version"]) && boundedName(target.type_id)
    && Number.isSafeInteger(target.schema_version) && Number(target.schema_version) > 0;
}
function signature(value: unknown): string {
  if (Array.isArray(value)) return JSON.stringify(value.map(signature));
  if (graphObject(value)) return JSON.stringify(Object.keys(value).sort().map(key => [key, signature(value[key])]));
  return JSON.stringify(value);
}
export function frontendExtensionIdentity(declaration: FrontendExtension) { return signature(declaration); }
export interface FrontendImplementation<T> { declaration: FrontendExtension; implementation: T }
export function createFrontendImplementationRegistry<T>() {
  const implementations: FrontendImplementation<T>[] = [];
  function registerAll(rows: readonly FrontendImplementation<T>[]) {
    const ids = new Set(implementations.map(row => row.declaration.extension_id));
    for (const { declaration } of rows) {
      if (!isFrontendExtension(declaration)) throw new Error("本地前端实现声明契约无效");
      if (ids.has(declaration.extension_id))
        throw new Error(`本地前端扩展重复登记：${declaration.extension_id}`);
      ids.add(declaration.extension_id);
    }
    implementations.push(...rows.map(row => ({ declaration: graphClone(row.declaration), implementation: row.implementation })));
  }
  return {
    register: (declaration: FrontendExtension, implementation: T) => registerAll([{ declaration, implementation }]),
    registerAll,
    entries: () => implementations.map(row => ({ declaration: graphClone(row.declaration), implementation: row.implementation })),
  };
}
export function resolveFrontendExtensions(declarations: FrontendExtension[], packages: { package_id: string; version: string }[],
  localImplementations: readonly FrontendExtension[]) {
  const available: FrontendExtension[] = [], issues: string[] = [];
  const ids = new Map<string, number>(), slots = new Map<string, number>();
  for (const row of declarations) {
    ids.set(row.extension_id, (ids.get(row.extension_id) ?? 0) + 1);
    if (row.binding.slot !== "panel") {
      const key = signature(row.binding);
      slots.set(key, (slots.get(key) ?? 0) + 1);
    }
  }
  for (const row of declarations) {
    if (ids.get(row.extension_id)! > 1 || row.binding.slot !== "panel" && slots.get(signature(row.binding))! > 1) {
      issues.push(`前端扩展冲突：${row.extension_id}`); continue;
    }
    if (!packages.some(pkg => pkg.package_id === row.package_id && pkg.version === row.package_version)) {
      issues.push(`前端包未启用：${row.package_id}@${row.package_version}`); continue;
    }
    if (!localImplementations.some(local => signature(local) === signature(row))) {
      issues.push(`缺少可信本地前端实现：${row.extension_id}`); continue;
    }
    available.push(row);
  }
  return { available, issues: [...new Set(issues)] };
}
