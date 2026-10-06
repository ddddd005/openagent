import { computed, provide } from "vue";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";
import { readFrontendArtifact } from "../application/workflowFrontendDisplay";
import { workflowFrontendSdkKey } from "../plugins/workflowFrontendSdk";
import { createNodeConfigurationAccess } from "../application/workflowNodeConfiguration";

export function useWorkbenchFrontendHost() {
  const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
  const host = createWorkbenchFrontendHost(() => graph.frontendExtensions, () => graph.packageLock);
  const lifecycle = computed(() => JSON.stringify([workspace.activeWorkflowId, graph.session?.workflow_session_id,
    host.extensions.value.map(row => row.declaration)]));
  provide(workbenchFrontendHostKey, host);
  provide(workflowFrontendSdkKey, {
    workflowId: computed(() => workspace.activeWorkflowId),
    document: computed(() => graph.document), catalog: computed(() => graph.catalog),
    packages: computed(() => graph.packageLock), session: computed(() => graph.session),
    locked: computed(() => graph.locked),
    lifecycle,
    attachDisplay: request => !!graph.attachFrontendDisplay(request),
    readArtifact: readFrontendArtifact,
    configureNode: createNodeConfigurationAccess({
      workflowId: () => workspace.activeWorkflowId, lifecycle: () => lifecycle.value,
      trustedExtensions: () => host.extensions.value.map(row => row.declaration),
      patch: request => graph.patchNodeConfiguration(request),
    }),
  });
  return host;
}
