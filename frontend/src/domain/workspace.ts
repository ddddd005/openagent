export type MainFlowStage = "A" | "B" | "Output";

export interface WorkspacePosition {
  x: number;
  y: number;
}

export interface MainFlowNodeData {
  stage: MainFlowStage;
  title: string;
}

export interface WorkspaceNode {
  id: string;
  position: WorkspacePosition;
  data: MainFlowNodeData;
}

export interface WorkspaceEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle: string;
  targetHandle: string;
}

export interface WorkspaceWorkflow {
  id: string;
  title: string;
  description: string;
  nodeCount: number;
  state?: "saved" | "draft";
  sourceId?: string;
  copyPending?: boolean;
}

export interface WorkspaceDraft {
  nodes: WorkspaceNode[];
  edges: WorkspaceEdge[];
}

export type WorkspaceLayout = Record<string, WorkspacePosition>;

export interface WorkspaceHistory {
  past: WorkspaceLayout[];
  future: WorkspaceLayout[];
}

// This token only rejects events from a canvas that has already been closed.
export interface WorkspaceViewToken {
  workflowId: string;
  version: number;
}

export interface WorkspaceNodeMove {
  id: string;
  position: WorkspacePosition;
}
