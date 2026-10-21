export interface WorkspaceWorkflow {
  id: string;
  title: string;
  description: string;
  nodeCount: number;
  state?: "saved" | "draft";
  sourceId?: string;
  copyPending?: boolean;
  archived?: boolean;
  deleted?: boolean;
  /** Formal directory projections copied from the workflow summary query. */
  fixedDefinitionRevision?: number;
  /** null exactly when the formal record has no current configuration. */
  editSequence?: number | null;
  hasCurrentConfiguration?: boolean;
  sessionCount?: number;
  /** True when this row's formal fields came from a summary response. */
  fromSummary?: boolean;
}

// This token only rejects events from a canvas that has already been closed.
export interface WorkspaceViewToken {
  workflowId: string;
  version: number;
}
