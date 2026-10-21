import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSSRApp, defineComponent, h, inject } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createPinia, disposePinia, setActivePinia, type Pinia } from "pinia";
import { useWorkflowFrontendHost } from "./useWorkflowFrontendHost";
import { workbenchFrontendHostKey } from "../application/workbenchFrontendHost";
import { workflowFrontendSdkKey } from "../plugins/workflowFrontendSdk";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { memoryRequestEvidence } from "../testUtils/requestEvidence";
import { memoryLocalDraftStorage } from "../testUtils/localDraftStorage";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";

let pinia: Pinia;
beforeEach(() => {
  pinia = createPinia();
  setActivePinia(pinia);
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
});
afterEach(() => {
  disposePinia(pinia);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("workflow-owned frontend assembly", () => {
  it("provides one instance to both subtrees and starts directory, activation and configuration order", async () => {
    const graph = useWorkflowGraphStore(), workspace = useWorkspaceStore();
    const persistence = useWorkbenchPersistenceStore();
    const workflowId = crypto.randomUUID();
    workspace.activeWorkflowId = workflowId;
    await persistence.initialize(undefined, memoryLocalDraftStorage().ports);
    const directory = vi.spyOn(graph, "loadDefinitions").mockResolvedValue(true);
    const activate = vi.spyOn(graph, "activate").mockResolvedValue();
    const select = vi.spyOn(persistence, "selectLoadedWorkflow").mockReturnValue(true);
    const seen: Record<string, unknown> = {};
    const probe = (side: "sidebar" | "workspace") => defineComponent({
      setup() {
        seen[side] = inject(workbenchFrontendHostKey);
        seen[`${side}Sdk`] = inject(workflowFrontendSdkKey);
        return () => h("span");
      },
    });
    const shell = defineComponent({
      setup() {
        useWorkflowFrontendHost();
        return () => h("div", [h(probe("sidebar")), h(probe("workspace"))]);
      },
    });
    await renderToString(createSSRApp(shell).use(pinia));
    await vi.waitFor(() => expect(directory).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(select).toHaveBeenCalledTimes(1));
    expect(activate).toHaveBeenCalledTimes(1);
    expect(activate).toHaveBeenCalledWith(workflowId);
    expect(seen.sidebar).toBeDefined();
    expect(seen.sidebar).toBe(seen.workspace);
    expect(seen.sidebarSdk).toBeDefined();
    expect(seen.sidebarSdk).toBe(seen.workspaceSdk);
    const sdk = seen.sidebarSdk as { workflowId: { value: string }; configureNode: unknown };
    expect(sdk.workflowId.value).toBe(workflowId);
    expect(typeof sdk.configureNode).toBe("function");
  });
});
