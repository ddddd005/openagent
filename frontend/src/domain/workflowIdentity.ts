export function isWorkflowIdentity(value: unknown): value is string {
  return typeof value === "string" && (
    ["frontend:main-test", "frontend:empty-test"].includes(value)
    || /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value)
  );
}
export type WorkflowEditGuard = (workflowId: string, change: (editableId: string) => void) => boolean;
