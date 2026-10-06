import { ref } from "vue";
import { defineStore } from "pinia";
import { runtimeContext } from "../adapters/mock";
import type {
  FixtureScenario,
  RunAction,
  RunGateway,
  RunProjection,
} from "../adapters/ports";

// Generation checks fence late reads; observing never submits an operation.
export function createRunObserver(gateway: RunGateway) {
  let generation = 0;
  let controller: AbortController | null = null;
  const projection = ref<RunProjection | null>(null);
  const loading = ref(false);
  const error = ref("");
  async function refresh(scenario: FixtureScenario) {
    const current = ++generation;
    controller?.abort();
    controller = new AbortController();
    loading.value = true;
    error.value = "";
    try {
      const view = await gateway.readSession(scenario, controller.signal);
      if (current === generation) projection.value = view;
    } catch (e) {
      if (current === generation && (e as Error).name !== "AbortError")
        error.value = "状态读取失败，未推断运行结果。";
    } finally {
      if (current === generation) loading.value = false;
    }
  }
  function stop() {
    generation++;
    controller?.abort();
    loading.value = false;
  }
  return { projection, loading, error, refresh, stop };
}

export const useRuntimeStore = defineStore("runtime", () => {
  const observer = createRunObserver(runtimeContext.runs);
  const scenario = ref<FixtureScenario>("paused");
  const actionNotice = ref("");
  const submitting = ref(false);
  async function observe(value: FixtureScenario = scenario.value) {
    scenario.value = value;
    actionNotice.value = "";
    await observer.refresh(value);
  }
  async function act(runId: string, action: RunAction) {
    const view = observer.projection.value;
    if (!view || submitting.value) return;
    const node = view.nodes.find((n) => n.runId === runId);
    if (!node?.actions.includes(action)) {
      actionNotice.value = "该目标没有此允许动作。";
      return;
    }
    submitting.value = true;
    try {
      const result = await runtimeContext.runs.submitAction({
        sessionId: view.sessionId,
        chainId: view.chainId,
        runId,
        action,
      });
      actionNotice.value = result.reason;
    } catch {
      actionNotice.value = "提交结果未知；未推断为执行成功。";
    } finally {
      submitting.value = false;
    }
  }
  return { ...observer, scenario, actionNotice, submitting, observe, act };
});
