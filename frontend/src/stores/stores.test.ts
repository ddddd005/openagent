import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEditorStore } from "./editor";
import { createRunObserver } from "./runtime";
import { MockRunGateway } from "../adapters/mock";
import { runFixture } from "../fixtures/runs";
import type { RunGateway, RunProjection } from "../adapters/ports";

beforeEach(() => setActivePinia(createPinia()));
describe("editor state", () => {
  it("undoes and redoes config and layout changes", () => {
    const s = useEditorStore();
    s.updateConfig("local:macro", "expression", "{{test}}");
    s.move([{ id: "local:macro", position: { x: 42, y: 18 } }]);
    s.undo();
    expect(s.draft.layout["local:macro"]).toEqual({ x: 610, y: 90 });
    s.undo();
    expect(
      s.draft.nodes.find((n) => n.id === "local:macro")!.config.expression,
    ).toBe("{{user.name}}");
    s.redo();
    expect(
      s.draft.nodes.find((n) => n.id === "local:macro")!.config.expression,
    ).toBe("{{test}}");
  });
  it("creates fresh local identities for each added node", () => {
    const s = useEditorStore();
    s.addNode("macro");
    const first = s.selected[0];
    s.addNode("macro");
    expect(s.selected[0]).not.toBe(first);
    expect(s.selected[0]).toMatch(/^local:/);
  });
  it("prevents replacement of an occupied input", () => {
    const s = useEditorStore(),
      count = s.draft.edges.length;
    s.connect("local:context", "context", "local:macro", "collection");
    expect(s.draft.edges).toHaveLength(count);
  });
  it("does not delete a group hidden later in a multi-selection", () => {
    const s = useEditorStore();
    s.selected = ["local:macro", "local:regex"];
    s.group();
    const id = s.selected[0];
    s.selected = ["local:collect", id];
    s.removeSelection();
    expect(s.draft.nodes.some((n) => n.id === id)).toBe(true);
    expect(s.draft.groups[id]).toHaveLength(2);
  });
  it("edits parameters inside a group without rewriting interface identity", () => {
    const s = useEditorStore();
    s.selected = ["local:macro", "local:regex"];
    s.group();
    const id = s.selected[0],
      ports = JSON.stringify(s.draft.groups[id]);
    s.enterGroup(id);
    s.updateConfig("local:macro", "expression", "{{inside}}");
    s.leaveGroup();
    s.selected = [id];
    s.ungroup();
    expect(
      s.draft.nodes.find((n) => n.id === "local:macro")!.config.expression,
    ).toBe("{{inside}}");
    expect(ports).toContain("local:macro");
    expect(s.draft.edges).toHaveLength(5);
  });
  it("restores scope safely when undoing a group", () => {
    const s = useEditorStore();
    s.selected = ["local:macro", "local:regex"];
    s.group();
    s.enterGroup(s.selected[0]);
    s.undo();
    expect(s.scope).toBe("root");
    expect(s.draft.nodes).toHaveLength(6);
  });
});
describe("run observer and action boundary", () => {
  it("does not submit actions on observation or cancellation", async () => {
    const gateway = new MockRunGateway(),
      observer = createRunObserver(gateway);
    await observer.refresh("paused");
    await observer.refresh("final_ready");
    observer.stop();
    expect(gateway.submissions).toEqual([]);
  });
  it("rejects wrong targets and unavailable actions", async () => {
    const gateway = new MockRunGateway();
    await gateway.readSession("paused");
    expect(
      (
        await gateway.submitAction({
          sessionId: "wrong",
          chainId: "fixture:chain-003",
          runId: "fixture:run-a-021",
          action: "resume",
        })
      ).accepted,
    ).toBe(false);
    expect(
      (
        await gateway.submitAction({
          sessionId: "fixture:session-014",
          chainId: "fixture:chain-003",
          runId: "fixture:run-a-021",
          action: "retry-archive",
        })
      ).accepted,
    ).toBe(false);
    expect(gateway.submissions).toHaveLength(0);
  });
  it("ignores late responses from an earlier observation", async () => {
    const resolvers: ((v: RunProjection) => void)[] = [];
    const gateway: RunGateway = {
      mode: "mock",
      readSession: () => new Promise((resolve) => resolvers.push(resolve)),
      submitAction: async () => ({ accepted: false, reason: "unused" }),
    };
    const observer = createRunObserver(gateway);
    const old = observer.refresh("paused"),
      current = observer.refresh("failed");
    resolvers[1](runFixture("failed"));
    await current;
    resolvers[0](runFixture("paused"));
    await old;
    expect(observer.projection.value!.nodes[0].phase).toBe("失败");
  });
  it("does not invent B input snapshots or official assistant messages", () => {
    for (const scenario of [
      "paused",
      "running",
      "failed",
      "final_ready",
      "archived",
    ] as const) {
      const view = runFixture(scenario);
      expect(view.nodes[1].runId).toBeNull();
      expect(view.messages.some((m) => m.role === "assistant")).toBe(false);
    }
  });
});
