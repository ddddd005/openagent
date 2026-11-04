/** R02: close remains an explicit control. The dialog names the allowed actions and
 * states the consequences; it never implies that closing clears every run lock. */
export const workflowControlLabels: Record<string, string> = {
  pause: "暂停", resume: "继续", close: "关闭本次运行",
  retry_archive: "归档重试", retry_acceptance: "结果接纳重试", retry_failed_node: "重试失败节点",
};
export function availableControlLabels(actions: string[] | undefined): string[] {
  return (actions ?? []).map(action => `${workflowControlLabels[action] ?? action}（${action}）`);
}
export const closeRunConsequences = [
  "只关闭当前会话的当前活动链；不会删除会话，也不等于清空所有运行占用。",
  "服务端运行锁仍按实际占用判定；其他会话或链仍可能阻止配置保存。",
  "关闭后固定配置的修改仍可能被拒绝或要求派生新工作流；不会自动关闭或自动重试。",
] as const;
