import type { FrontendExtension } from "../domain/frontendExtensions";
import { createLocalWorkbenchFrontendHost, type WorkbenchFrontendPackageModule } from "./workbenchFrontendHost";
import { builtinFrontendPackage } from "../plugins/workflowFrontendPackage";
import { modelFrontendPackage } from "../plugins/modelFrontendPackage";
import { promptFrontendPackage } from "../plugins/promptFrontendPackage";
export type { WorkbenchFrontendPackageModule } from "./workbenchFrontendHost";
export function createWorkbenchFrontendHost(
  declarations: () => FrontendExtension[], packages: () => { package_id: string; version: string }[],
  modules: readonly WorkbenchFrontendPackageModule[] = [builtinFrontendPackage, modelFrontendPackage, promptFrontendPackage],
) {
  return createLocalWorkbenchFrontendHost(declarations, packages, modules);
}
