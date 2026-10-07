import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useWorkspaceStore } from "./workspace";

beforeEach(() => setActivePinia(createPinia()));
describe("current workspace catalog", () => {
  it("starts empty and keeps a stable placeholder before initialization", () => {
    const workspace = useWorkspaceStore();
    expect(workspace.workflows).toEqual([]); expect(workspace.activeWorkflow.nodeCount).toBe(0);
  });
  it("isolates selection from opening and invalidates stale view tokens", () => {
    const workspace = useWorkspaceStore(), first = crypto.randomUUID(), second = crypto.randomUUID();
    workspace.registerGraphWorkflow({ id: first, title: "First", description: "", nodeCount: 0 });
    workspace.registerGraphWorkflow({ id: second, title: "Second", description: "", nodeCount: 0 });
    workspace.openWorkflow(first); const token = workspace.currentViewToken();
    workspace.selectWorkflow(second); expect(workspace.activeWorkflowId).toBe(first);
    workspace.openWorkflow(second); expect(workspace.isCurrentView(token)).toBe(false);
  });
  it("keeps unconfirmed copies visible but does not open or remove the active workflow", () => {
    const workspace = useWorkspaceStore(), id = crypto.randomUUID(), copy = crypto.randomUUID();
    workspace.initializeFreshWorkspace({ id, title: "Source", description: "", nodeCount: 0 });
    workspace.registerGraphWorkflow({ id: copy, title: "Copy", description: "", nodeCount: 0, copyPending: true });
    workspace.openWorkflow(copy); expect(workspace.activeWorkflowId).toBe(id);
    expect(workspace.removeWorkflow(id)).toBe(false);
    workspace.setCopyPending(copy, false); workspace.openWorkflow(copy);
    workspace.markWorkflowSaved(copy, "Saved copy"); expect(workspace.activeWorkflow.state).toBe("saved");
  });
});
