import GraphFrontendDisplay from "../components/GraphFrontendDisplay.vue";
import GraphFrontendSources from "../components/GraphFrontendSources.vue";
import FrontendNodeFields from "../components/FrontendNodeFields.vue";
import GraphChatDisplay from "../components/GraphChatDisplay.vue";
import { chatSessionObjectExtension, frontendChatNodeFieldsExtension,
  frontendNodeFieldsExtension, workflowFrontendExtensions } from "./workflowFrontendManifest";
import type { WorkbenchFrontendPackageModule } from "../application/workbenchFrontendHost";

export const builtinFrontendPackage: WorkbenchFrontendPackageModule = {
  implementations: [
    { declaration: workflowFrontendExtensions[0], implementation: GraphFrontendDisplay },
    { declaration: workflowFrontendExtensions[1], implementation: GraphFrontendSources },
    { declaration: frontendNodeFieldsExtension, implementation: FrontendNodeFields,
      configurationFields: ["input_node_id"] },
    { declaration: frontendChatNodeFieldsExtension, implementation: FrontendNodeFields,
      configurationFields: ["input_node_id"] },
    { declaration: chatSessionObjectExtension, implementation: GraphChatDisplay },
  ],
};
