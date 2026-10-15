import LorebookFields from "../components/LorebookFields.vue";
import type { WorkbenchFrontendPackageModule } from "../application/workbenchFrontendHost";
import { currentTavernFrontendExtensions, globalTavernFrontendExtensions, tavernFrontendExtensions } from "./tavernFrontendManifest";
import CurrentLorebookPanel from "./tavern/CurrentLorebookPanel.vue";
import GlobalLorebookFields from "./tavern/GlobalLorebookFields.vue";

export const tavernFrontendPackage: WorkbenchFrontendPackageModule = {
  implementations: [
    { declaration: tavernFrontendExtensions[0]!, implementation: LorebookFields,
      configurationFields: ["entry", "object_keys"] },
    { declaration: tavernFrontendExtensions[1]!, implementation: LorebookFields,
      configurationFields: ["entries", "object_keys"] },
    { declaration: globalTavernFrontendExtensions[0]!, implementation: LorebookFields,
      configurationFields: ["entry", "object_keys"] },
    { declaration: globalTavernFrontendExtensions[1]!, implementation: LorebookFields,
      configurationFields: ["entries", "object_keys"] },
    { declaration: globalTavernFrontendExtensions[2]!, implementation: CurrentLorebookPanel },
    { declaration: globalTavernFrontendExtensions[3]!, implementation: GlobalLorebookFields,
      configurationFields: ["reference"] },
    { declaration: globalTavernFrontendExtensions[4]!, implementation: GlobalLorebookFields,
      configurationFields: ["object_keys"] },
    { declaration: currentTavernFrontendExtensions[0]!, implementation: LorebookFields,
      configurationFields: ["entry", "object_keys"] },
    { declaration: currentTavernFrontendExtensions[1]!, implementation: LorebookFields,
      configurationFields: ["entries", "object_keys"] },
    { declaration: currentTavernFrontendExtensions[2]!, implementation: CurrentLorebookPanel },
    { declaration: currentTavernFrontendExtensions[3]!, implementation: GlobalLorebookFields,
      configurationFields: ["reference"] },
    { declaration: currentTavernFrontendExtensions[4]!, implementation: GlobalLorebookFields,
      configurationFields: ["object_keys"] },
  ],
};
