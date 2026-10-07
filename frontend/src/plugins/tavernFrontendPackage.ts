import LorebookFields from "../components/LorebookFields.vue";
import type { WorkbenchFrontendPackageModule } from "../application/workbenchFrontendHost";
import { tavernFrontendExtensions } from "./tavernFrontendManifest";

export const tavernFrontendPackage: WorkbenchFrontendPackageModule = {
  implementations: [
    { declaration: tavernFrontendExtensions[0]!, implementation: LorebookFields,
      configurationFields: ["entry", "object_keys"] },
    { declaration: tavernFrontendExtensions[1]!, implementation: LorebookFields,
      configurationFields: ["entries", "object_keys"] },
  ],
};
