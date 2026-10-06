import { createPreparationDraft } from "../fixtures/preparation";
import { createWorkspaceDrafts, MAIN_WORKFLOW_ID, workspaceWorkflows } from "../fixtures/workflows";
import type { SavedWorkbench } from "../adapters/workbenchPersistence";
import { encodeUnifiedWorkbench } from "../adapters/unifiedWorkbenchDocument";
import type { WorkspaceWorkflow } from "../domain/workspace";

interface GraphFixture {
  graph: unknown; catalog: WorkspaceWorkflow[]; activeWorkflowId: string; selectedWorkflowId: string;
}
export function legacyWorkbenchRaw(graphFixture?: GraphFixture) {
  const value: SavedWorkbench = {
    schemaVersion: 5, kind: "fixed-workbench", revision: 1, savedAt: "2026-10-01T00:00:00.000Z",
    activeWorkflowId: MAIN_WORKFLOW_ID, selectedWorkflowId: MAIN_WORKFLOW_ID,
    layouts: Object.fromEntries(Object.entries(createWorkspaceDrafts()).map(([id, draft]) => [id,
      Object.fromEntries(draft.nodes.map(node => [node.id, { ...node.position }]))])),
    preparations: [createPreparationDraft(MAIN_WORKFLOW_ID, "A"), createPreparationDraft(MAIN_WORKFLOW_ID, "B")],
    registrations: [], catalog: workspaceWorkflows.map(workflow => ({ ...workflow, state: "saved" })),
    runtime: { schemaVersion: 2, kind: "workbench-runtime", selectedSessions: [], sessions: [], selectionPending: null },
    models: { schemaVersion: 1, drafts: [], pending: null },
    exposures: { schemaVersion: 1, publications: [], pending: null },
  };
  if (!graphFixture) return JSON.stringify(value);
  const ids = new Set(graphFixture.catalog.map(row => row.id));
  return JSON.stringify(encodeUnifiedWorkbench({ ...value, ...graphFixture,
    layouts: Object.fromEntries(graphFixture.catalog.map(row => [row.id, value.layouts[row.id] ?? {}])),
    preparations: value.preparations.filter(row => ids.has(row.workflowId)),
  }));
}

export function legacyWorkbenchStorage(graphFixture?: GraphFixture) {
  let raw: string | null = legacyWorkbenchRaw(graphFixture);
  return { getItem: () => raw, setItem: (_key: string, value: string) => { raw = value; } };
}
