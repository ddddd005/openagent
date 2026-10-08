import LorebookFields from "../components/LorebookFields.vue";
import type { WorkbenchFrontendPackageModule } from "../application/workbenchFrontendHost";
import { chatTavernFrontendExtensions, globalTavernFrontendExtensions, tavernFrontendExtensions } from "./tavernFrontendManifest";
import CurrentLorebookPanel from "./tavern/CurrentLorebookPanel.vue";
import GlobalLorebookFields from "./tavern/GlobalLorebookFields.vue";
import TavernChatPanel from "./tavern/TavernChatPanel.vue";

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
    { declaration: chatTavernFrontendExtensions[0]!, implementation: LorebookFields,
      configurationFields: ["entry", "object_keys"] },
    { declaration: chatTavernFrontendExtensions[1]!, implementation: LorebookFields,
      configurationFields: ["entries", "object_keys"] },
    { declaration: chatTavernFrontendExtensions[2]!, implementation: CurrentLorebookPanel },
    { declaration: chatTavernFrontendExtensions[3]!, implementation: GlobalLorebookFields,
      configurationFields: ["reference"] },
    { declaration: chatTavernFrontendExtensions[4]!, implementation: GlobalLorebookFields,
      configurationFields: ["object_keys"] },
    { declaration: chatTavernFrontendExtensions[5]!, implementation: TavernChatPanel },
  ],
};
