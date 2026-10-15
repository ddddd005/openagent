import FrontendNodeFields from "../components/FrontendNodeFields.vue";
import GraphChatDisplay from "../components/GraphChatDisplay.vue";
import { chatSessionObjectExtension, frontendChatNodeFieldsExtension } from "./workflowFrontendManifest";
import type { WorkbenchFrontendPackageModule } from "../application/workbenchFrontendHost";

export const builtinFrontendPackage: WorkbenchFrontendPackageModule = {
  implementations: [
    { declaration: frontendChatNodeFieldsExtension, implementation: FrontendNodeFields,
      configurationFields: ["input_node_id"] },
    { declaration: chatSessionObjectExtension, implementation: GraphChatDisplay },
  ],
};
