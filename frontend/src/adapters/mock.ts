import { runFixture } from "../fixtures/runs";
import type {
  ActionTarget,
  FixtureScenario,
  RunGateway,
  RuntimeContext,
} from "./ports";

export class MockRunGateway implements RunGateway {
  readonly mode = "mock" as const;
  private scenario: FixtureScenario = "paused";
  readonly submissions: ActionTarget[] = [];

  async readSession(scenario: FixtureScenario, signal?: AbortSignal) {
    if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    this.scenario = scenario;
    return runFixture(scenario);
  }
  async submitAction(target: ActionTarget) {
    const view = runFixture(this.scenario);
    const node = view.nodes.find((n) => n.runId === target.runId);
    if (
      target.sessionId !== view.sessionId ||
      target.chainId !== view.chainId ||
      !node?.actions.includes(target.action)
    ) {
      return {
        accepted: false,
        reason: "测试夹具拒绝：目标或允许动作已变化。",
      };
    }
    this.submissions.push({ ...target });
    return {
      accepted: true,
      reason: "模拟提交已记录；不代表真实执行或后端授权。",
    };
  }
}

export const runtimeContext: RuntimeContext = {
  runs: new MockRunGateway(),
  events: { supported: false },
  publishing: { supported: false },
};
