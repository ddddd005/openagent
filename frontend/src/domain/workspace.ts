export interface WorkspaceWorkflow {
  id: string;
  title: string;
  description: string;
  nodeCount: number;
  state?: "saved" | "draft";
  sourceId?: string;
  copyPending?: boolean;
}

// This token only rejects events from a canvas that has already been closed.
export interface WorkspaceViewToken {
  workflowId: string;
  version: number;
}
