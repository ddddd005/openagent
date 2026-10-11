import ModelSourceFields from "../components/ModelSourceFields.vue";
import CurrentProviderPanel from "../components/CurrentProviderPanel.vue";
import type { WorkbenchFrontendPackageModule } from "../application/workbenchFrontendHost";
import { modelFrontendExtensions } from "./modelFrontendManifest";
export const modelFrontendPackage: WorkbenchFrontendPackageModule = {
  implementations: [
    { declaration: modelFrontendExtensions[0]!, implementation: CurrentProviderPanel },
    { declaration: modelFrontendExtensions[1]!, implementation: ModelSourceFields,
      configurationFields: ["reference"] },
    { declaration: modelFrontendExtensions[2]!, implementation: ModelSourceFields,
      configurationFields: ["reference"] },
  ],
};
