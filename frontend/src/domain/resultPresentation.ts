import { graphObject } from "./workflowGraph";

export interface ResultText { label: string; text: string }
export function resultTexts(value: unknown): ResultText[] {
  if (Array.isArray(value)) return value.flatMap(resultTexts);
  if (!graphObject(value)) return [];
  if (value.kind === "workflow.text" && value.schema_version === 2 && typeof value.text === "string")
    return [{ label: "正文", text: value.text }];
  if (value.kind === "workflow.model-result" && (value.schema_version === 1 || value.schema_version === 2) && typeof value.content === "string")
    return [{ label: "回答", text: value.content }];
  if ((value.kind === "workflow.prompt" && value.schema_version === 2
    || value.kind === "workflow.prompt-materials" && value.schema_version === 1) && Array.isArray(value.items))
    return value.items.filter(item => graphObject(item) && typeof item.text === "string")
      .map(item => ({ label: typeof item.role === "string" ? item.role : "提示词", text: item.text as string }));
  return [];
}
export const resultStatusLabel = (value: string) => ({
  idle: "待运行", prepared: "准备中", running: "运行中", pausing: "暂停中", paused: "已暂停",
  succeeded: "已完成", failed: "失败", closed: "已关闭", budget_exhausted: "预算已用尽",
  archive_failed: "归档失败", recovery_unavailable: "无法恢复",
}[value] ?? value);
