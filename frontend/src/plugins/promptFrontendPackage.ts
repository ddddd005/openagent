import CurrentPromptPanel from "../components/CurrentPromptPanel.vue";
import PromptReferenceFields from "../components/PromptReferenceFields.vue";
import type { WorkbenchFrontendPackageModule } from "../application/workbenchFrontendHost";
import { promptFrontendExtensions } from "./promptFrontendManifest";

export const promptFrontendPackage: WorkbenchFrontendPackageModule = {
  implementations: [
    { declaration: promptFrontendExtensions[0]!, implementation: CurrentPromptPanel },
    { declaration: promptFrontendExtensions[1]!, implementation: PromptReferenceFields,
      configurationFields: ["reference"] },
    { declaration: promptFrontendExtensions[2]!, implementation: PromptReferenceFields,
      configurationFields: ["reference"] },
  ],
};
