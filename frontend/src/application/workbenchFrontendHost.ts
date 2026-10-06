import { computed, inject, markRaw, shallowRef, type Component, type InjectionKey } from "vue";
import { createFrontendImplementationRegistry, frontendExtensionIdentity, resolveFrontendExtensions,
  type FrontendExtension, type FrontendImplementation } from "../domain/frontendExtensions";

export interface WorkbenchFrontendPackageModule {
  implementations: readonly (FrontendImplementation<Component> & { configurationFields?: readonly string[] })[];
}
export type WorkbenchFrontendHost = ReturnType<typeof createLocalWorkbenchFrontendHost>;
export const workbenchFrontendHostKey: InjectionKey<WorkbenchFrontendHost> = Symbol("workbench-frontend-host-v1");
export function useInstalledWorkbenchFrontendHost() {
  const host = inject(workbenchFrontendHostKey);
  if (!host) throw new Error("工作台前端扩展需要共享的宿主注册表");
  return host;
}

export function createLocalWorkbenchFrontendHost(declarations: () => FrontendExtension[],
  packages: () => { package_id: string; version: string }[], modules: readonly WorkbenchFrontendPackageModule[]) {
  const registry = createFrontendImplementationRegistry<Component>();
  const local = shallowRef<FrontendImplementation<Component>[]>([]);
  const configurationFields = new Map<string, readonly string[]>();
  function register(module: WorkbenchFrontendPackageModule) {
    for (const row of module.implementations) {
      if (row.declaration.binding.surface !== "workbench") throw new Error("工作台模块只能登记工作台实现");
      if (row.configurationFields !== undefined && (row.declaration.binding.slot !== "node-fields"
        || !Array.isArray(row.configurationFields)
        || row.configurationFields.some(field => typeof field !== "string" || !field.trim()
          || field.length > 128 || field !== field.trim())
        || new Set(row.configurationFields).size !== row.configurationFields.length))
        throw new Error("配置字段接管需要可信节点字段编辑器及唯一字段名");
    }
    registry.registerAll(module.implementations.map(row => ({ ...row, implementation: markRaw(row.implementation) })));
    for (const row of module.implementations)
      configurationFields.set(frontendExtensionIdentity(row.declaration), Object.freeze([...(row.configurationFields ?? [])]));
    local.value = registry.entries();
  }
  modules.forEach(register);
  const resolution = computed(() => resolveFrontendExtensions(
    declarations().filter(row => row.binding.surface === "workbench"), packages(), local.value.map(row => row.declaration)));
  return {
    register,
    issues: computed(() => resolution.value.issues),
    extensions: computed(() => resolution.value.available.map(declaration => ({
      declaration, component: local.value.find(row =>
        frontendExtensionIdentity(row.declaration) === frontendExtensionIdentity(declaration))!.implementation,
      configurationFields: configurationFields.get(frontendExtensionIdentity(declaration)) ?? [],
    }))),
  };
}
