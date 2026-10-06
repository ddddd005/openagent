export type FixtureScenario =
  | "paused"
  | "running"
  | "failed"
  | "final_ready"
  | "archived";
export type RunAction =
  | "interrupt"
  | "resume"
  | "retry-archive"
  | "retry-publish";
export interface RunProjection {
  source: "ui-fixture";
  sessionId: string;
  chainId: string;
  revision: string;
  nodes: {
    bindingId: string;
    runId: string | null;
    title: string;
    phase: string;
    budget: string;
    actions: RunAction[];
  }[];
  messages: {
    id: string;
    role: "user" | "assistant";
    text: string;
    provenance: string;
  }[];
  timeline: { time: string; title: string; detail: string }[];
}
export interface ActionTarget {
  sessionId: string;
  chainId: string;
  runId: string;
  action: RunAction;
}
export interface RunGateway {
  mode: "mock";
  readSession(
    scenario: FixtureScenario,
    signal?: AbortSignal,
  ): Promise<RunProjection>;
  submitAction(
    target: ActionTarget,
  ): Promise<{ accepted: boolean; reason: string }>;
}
export interface RuntimeContext {
  runs: RunGateway;
  events: { supported: false };
  publishing: { supported: false };
}
