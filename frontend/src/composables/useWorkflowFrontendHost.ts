import { computed, provide, watch } from "vue";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
import { createWorkbenchFrontendHost } from "../application/workflowFrontendPackage";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";
import { workflowFrontendSdkKey } from "../plugins/workflowFrontendSdk";
import { createNodeConfigurationAccess } from "../application/workflowNodeConfiguration";
import { createWorkflowConfigurationApplication } from "../application/workflowConfiguration";

/** Workflow-owned frontend assembly called once by the project's common ancestor.
 *
 * It creates the shared extension host and configuration application, provides
 * them to both the sidebar and the main region, then starts the workflow
 * directory read, the first activation and the configuration readiness path.
 * The project shell only calls this entry; it owns no second assembly.
 */
export function useWorkflowFrontendHost() {
  const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
  const persistence = useWorkbenchPersistenceStore();
  const host = createWorkbenchFrontendHost(() => graph.frontendExtensions, () => graph.packageLock);
  const configuration = createWorkflowConfigurationApplication({ extensions: () => host.extensions.value });
  graph.installWorkflowConfigurationApplication(configuration);
  const lifecycle = computed(() => JSON.stringify([workspace.activeWorkflowId, graph.session?.workflow_session_id,
    host.extensions.value.map(row => row.declaration)]));
  provide(workbenchFrontendHostKey, host);
  provide(workflowFrontendSdkKey, {
    workflowId: computed(() => workspace.activeWorkflowId),
    document: computed(() => graph.document), catalog: computed(() => graph.catalog),
    packages: computed(() => graph.packageLock), session: computed(() => graph.session),
    locked: computed(() => graph.editorLocked),
    lifecycle,
    configureNode: createNodeConfigurationAccess({
      workflowId: () => workspace.activeWorkflowId, lifecycle: () => lifecycle.value,
      trustedExtensions: () => host.extensions.value.map(row => row.declaration),
      patch: request => graph.patchNodeConfiguration(request),
    }),
  });
  // Assembly precedes the directory read, the first activation and any
  // configuration command: both watchers are registered after the provides.
  watch(() => persistence.ready, ready => {
    if (ready) void graph.loadDefinitions().then(() => persistence.selectLoadedWorkflow());
  }, { immediate: true });
  watch(() => [persistence.ready, workspace.activeWorkflowId] as const, ([ready, workflowId]) => {
    if (ready && workflowId) void graph.activate(workflowId);
  }, { immediate: true });
  return host;
}
