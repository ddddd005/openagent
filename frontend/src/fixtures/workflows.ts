import type {
  WorkspaceDraft,
  WorkspaceWorkflow,
} from "../domain/workspace";

export const MAIN_WORKFLOW_ID = "frontend:main-test";
export const EMPTY_WORKFLOW_ID = "frontend:empty-test";

export const workspaceWorkflows: WorkspaceWorkflow[] = [
  {
    id: MAIN_WORKFLOW_ID,
    title: "默认测试工作流",
    description: "A / B / Output",
    nodeCount: 3,
  },
  {
    id: EMPTY_WORKFLOW_ID,
    title: "空工作流",
    description: "空白",
    nodeCount: 0,
  },
];

export function createWorkspaceDrafts(): Record<string, WorkspaceDraft> {
  return {
    [MAIN_WORKFLOW_ID]: {
      nodes: [
        {
          id: "frontend:main-test:agent-a",
          position: { x: 80, y: 180 },
          data: { stage: "A", title: "Agent A" },
        },
        {
          id: "frontend:main-test:agent-b",
          position: { x: 400, y: 180 },
          data: { stage: "B", title: "Agent B" },
        },
        {
          id: "frontend:main-test:output",
          position: { x: 720, y: 180 },
          data: { stage: "Output", title: "Output" },
        },
      ],
      edges: [
        {
          id: "frontend:main-test:a-to-b",
          source: "frontend:main-test:agent-a",
          target: "frontend:main-test:agent-b",
          sourceHandle: "out",
          targetHandle: "in",
        },
        {
          id: "frontend:main-test:b-to-output",
          source: "frontend:main-test:agent-b",
          target: "frontend:main-test:output",
          sourceHandle: "out",
          targetHandle: "in",
        },
      ],
    },
    [EMPTY_WORKFLOW_ID]: { nodes: [], edges: [] },
  };
}
