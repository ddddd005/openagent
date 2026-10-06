import type {
  RunProjection,
  FixtureScenario,
  RunAction,
} from "../adapters/ports";

export const scenarioLabels: Record<FixtureScenario, string> = {
  paused: "已暂停",
  running: "执行中",
  failed: "失败",
  final_ready: "待归档",
  archived: "待端口放行",
};
export const actionLabels: Record<RunAction, string> = {
  interrupt: "请求暂停",
  resume: "继续当前运行",
  "retry-archive": "重试归档",
  "retry-publish": "重试端口放行",
};

export function runFixture(scenario: FixtureScenario): RunProjection {
  const phase = scenarioLabels[scenario];
  const actions: RunAction[] =
    scenario === "paused"
      ? ["resume"]
      : scenario === "running"
        ? ["interrupt"]
        : scenario === "final_ready"
          ? ["retry-archive"]
          : scenario === "archived"
            ? ["retry-publish"]
            : [];
  return {
    source: "ui-fixture",
    sessionId: "fixture:session-014",
    chainId: "fixture:chain-003",
    revision: "fixture:frozen-08",
    nodes: [
      {
        bindingId: "fixture:binding-a",
        runId: "fixture:run-a-021",
        title: "A · 研究与规划",
        phase,
        budget: "4 / 12 步",
        actions,
      },
      {
        bindingId: "fixture:binding-b",
        runId: null,
        title: "B · 整理与表达",
        phase: "未准备",
        budget: "尚无实际输入快照",
        actions: [],
      },
      {
        bindingId: "fixture:binding-output",
        runId: null,
        title: "Output · 正式交付",
        phase: "未放行",
        budget: "无正式消息",
        actions: [],
      },
    ],
    messages: [
      {
        id: "fixture:input-014",
        role: "user",
        text: "请整理一份可执行的前端实施方案，区分当前能力与后续接入。",
        provenance: "界面测试输入 · 不会发送至模型",
      },
    ],
    timeline: [
      { time: "10:42:08", title: "输入已接纳", detail: "测试夹具 · 会话 014" },
      {
        time: "10:42:09",
        title: "A 的输入已冻结",
        detail: "确定修订 fixture:frozen-08",
      },
      {
        time: "10:42:12",
        title: "处理步骤 4",
        detail: "公开计数 4 / 12；不推断百分比",
      },
      {
        time: "10:42:15",
        title: phase,
        detail:
          scenario === "failed"
            ? "测试错误：读取失败；恢复授权未知"
            : scenario === "final_ready"
              ? "结果已接纳；尚无成功归档回执"
              : scenario === "archived"
                ? "已有归档回执；双端口尚未放行"
                : "B 的输入尚未准备",
      },
    ],
  };
}
