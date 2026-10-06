import GraphFrontendWiring from "../components/GraphFrontendWiring.vue";
import GraphFrontendDisplay from "../components/GraphFrontendDisplay.vue";
import GraphFrontendSources from "../components/GraphFrontendSources.vue";
import { workflowFrontendExtensions } from "./workflowFrontendManifest";
import type { WorkbenchFrontendPackageModule } from "../application/workbenchFrontendHost";

export const builtinFrontendPackage: WorkbenchFrontendPackageModule = {
  implementations: [
    { declaration: workflowFrontendExtensions[0], implementation: GraphFrontendWiring },
    { declaration: workflowFrontendExtensions[1], implementation: GraphFrontendDisplay },
    { declaration: workflowFrontendExtensions[2], implementation: GraphFrontendSources },
  ],
};
