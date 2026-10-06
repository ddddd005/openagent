import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { clonePreparation, createPromptMember } from "../domain/preparation";
import { usePreparationStore } from "./preparation";

describe("preparation store", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("separates workflow and A/B editing and does not create drafts from dirty checks", () => {
    const store = usePreparationStore();
    expect(store.isDirty("empty", "A")).toBe(false);
    expect(Object.keys(store.drafts)).toEqual([]);
    const a = store.getDraft("main", "A");
    const b = store.getDraft("main", "B");
    const node = a.nodes.find((candidate) => candidate.kind === "prompt-item")!;
    expect(store.updateNodeConfig("main", "A", node.id, { text: "edited" })).toBe(true);
    expect(store.isDirty("main", "A")).toBe(true);
    expect(store.isDirty("main", "B")).toBe(false);
    expect(b.nodes).not.toEqual(store.getDraft("main", "A").nodes);
    expect(store.getDraft("other", "A").nodes.some((candidate) => candidate.id === node.id)).toBe(false);
  });

  it("undoes full config and layout while subsequent edits do not reuse a published revision", () => {
    const store = usePreparationStore();
    const draft = store.getDraft("main", "A");
    const node = draft.nodes.find((candidate) => candidate.kind === "prompt-item")!;
    store.updateNodeConfig("main", "A", node.id, { text: "first" });
    store.updateNodeConfig("main", "A", node.id, { text: "second" });
    const second = clonePreparation(store.getDraft("main", "A"));
    store.undo("main", "A");
    store.updateNodeConfig("main", "A", node.id, { text: "different" });
    const changed = store.getDraft("main", "A").nodes.find((candidate) => candidate.id === node.id)!;
    const old = second.nodes.find((candidate) => candidate.id === node.id)!;
    if (changed.kind !== "prompt-item" || old.kind !== "prompt-item") throw new Error("Wrong node");
    expect(changed.config.revision).toBeGreaterThan(old.config.revision);
    store.moveNodes("main", "A", [{ id: node.id, position: { x: 900, y: 200 } }]);
    store.undo("main", "A");
    expect(store.getDraft("main", "A").nodes.find((candidate) => candidate.id === node.id)!.position).toEqual(node.position);
    store.redo("main", "A");
    expect(store.getDraft("main", "A").nodes.find((candidate) => candidate.id === node.id)!.position).toEqual({ x: 900, y: 200 });
  });

  it("stores exact current input separately per stage and normalizes unused depth", () => {
    const store = usePreparationStore();
    store.setRootText("main", "A", "run input");
    expect(store.getRootText("main", "A")).toBe("run input");
    expect(store.getRootText("main", "B")).toBe("");
    const node = store.getDraft("main", "A").nodes.find((candidate) => candidate.kind === "prompt-item")!;
    store.updateNodeConfig("main", "A", node.id, {
      presentation: { role: "user", placement: "after", depth: 30, order: 1, enabled: true },
    });
    const changed = store.getDraft("main", "A").nodes.find((candidate) => candidate.id === node.id)!;
    if (changed.kind !== "prompt-item") throw new Error("Wrong node");
    expect(changed.config.presentation.depth).toBeNull();
  });

  it("preserves member exact identity and bumps changed content revision", () => {
    const store = usePreparationStore();
    const group = store.getDraft("main", "A").nodes.find((node) => node.kind === "prompt-group")!;
    if (group.kind !== "prompt-group") throw new Error("Wrong node");
    const members = clonePreparation(group.config.members);
    members[0].text = "changed";
    members.push(createPromptMember());
    store.updateNodeConfig("main", "A", group.id, { members });
    const changed = store.getDraft("main", "A").nodes.find((node) => node.id === group.id)!;
    if (changed.kind !== "prompt-group") throw new Error("Wrong node");
    expect(changed.config.members[0].id).toBe(group.config.members[0].id);
    expect(changed.config.members[0].revision).toBe(2);
    expect(changed.config.members).toHaveLength(2);
  });

  it("keeps invalid existing-port connections for diagnosis and undo restores topology", () => {
    const store = usePreparationStore();
    const draft = store.getDraft("main", "A");
    const root = draft.nodes.find((node) => node.kind === "root-input")!;
    const assemble = draft.nodes.find((node) => node.kind === "assemble")!;
    expect(store.connect("main", "A", {
      source: root.id, target: assemble.id,
      sourceHandle: "current-input", targetHandle: "prompt",
    })).toBe(true);
    expect(store.getPreview("main", "A").diagnostics.some((entry) => entry.code === "incompatible_port")).toBe(true);
    expect(store.getDraft("main", "A").edges).toHaveLength(10);
    store.undo("main", "A");
    expect(store.getDraft("main", "A").edges).toHaveLength(9);
  });

  it("rejects stale canvas movement and context cache replies after view generation changes", () => {
    const store = usePreparationStore();
    const draft = store.getDraft("main", "A");
    const root = draft.nodes.find((node) => node.kind === "root-input")!;
    const context = draft.nodes.find((node) => node.kind === "context")!;
    const token = store.currentViewToken("main", "A");
    store.invalidateView("main", "A");
    expect(store.moveNodes("main", "A", [{ id: root.id, position: { x: 0, y: 0 } }], token)).toBe(false);
    expect(store.setContextCache("main", "A", context.id, {
      source: "backend-read", floors: [],
      reference: { workflowId: "main", stage: "A", workflowSessionId: "session", nodeBindingId: "binding", selectedChainId: "chain" },
    }, token)).toBe(false);
    expect(store.setContextCache("main", "A", context.id, {
      source: "backend-read", floors: [],
      reference: { workflowId: "main", stage: "B", workflowSessionId: "session", nodeBindingId: "binding", selectedChainId: "chain" },
    }, store.currentViewToken("main", "A"))).toBe(false);
  });

  it("rejects direct editing of canonical context and native tool source", () => {
    const store = usePreparationStore();
    const draft = store.getDraft("main", "A");
    const context = draft.nodes.find((node) => node.kind === "context")!;
    const tool = draft.nodes.find((node) => node.kind === "tool")!;
    expect(store.updateNodeConfig("main", "A", context.id, { floors: [{ id: "fake" }] })).toBe(false);
    expect(store.updateNodeConfig("main", "A", tool.id, {
      toolRef: { name: "arbitrary_tool", version: "2" },
      parametersSchema: {},
    })).toBe(false);
    if (tool.kind !== "tool") throw new Error("Wrong node");
    expect(tool.config.toolRef).toEqual({ name: "inspect_text", version: "1" });
  });

  it("adds at the canvas-projected position with isolated scope and undo/redo", () => {
    const store = usePreparationStore();
    const a = store.getDraft("main", "A");
    const b = store.getDraft("main", "B");
    const aCount = a.nodes.length;
    const bCount = b.nodes.length;
    const position = { x: -230.5, y: 710.25 };
    const id = store.addNode("main", "B", "prompt-item", position);
    expect(id).not.toBeNull();
    expect(store.getDraft("main", "B").nodes.find((node) => node.id === id)?.position).toEqual(position);
    expect(store.getDraft("main", "A").nodes).toHaveLength(aCount);
    expect(store.getDraft("main", "B").nodes).toHaveLength(bCount + 1);
    expect(store.isDirty("main", "B")).toBe(true);
    store.undo("main", "B");
    expect(store.getDraft("main", "B").nodes.some((node) => node.id === id)).toBe(false);
    store.redo("main", "B");
    expect(store.getDraft("main", "B").nodes.find((node) => node.id === id)?.position).toEqual(position);
    position.x = 999;
    expect(store.getDraft("main", "B").nodes.find((node) => node.id === id)?.position.x).toBe(-230.5);
  });

  it("does not rewind observations when undoing configuration or mix them with dirty state", () => {
    const store = usePreparationStore();
    const draft = store.getDraft("main", "A");
    const context = draft.nodes.find((node) => node.kind === "context")!;
    const reference = {
      workflowId: "main", stage: "A" as const, workflowSessionId: "session",
      nodeBindingId: "7be319b8-30bd-4674-b7bf-d1cf54a1a108", selectedChainId: "chain",
    };
    store.setRootText("main", "A", "changed");
    expect(store.setContextCache("main", "A", context.id, {
      source: "backend-read", reference, floors: [],
    }, store.currentViewToken("main", "A"))).toBe(true);
    store.undo("main", "A");
    const restored = store.getDraft("main", "A").nodes.find((node) => node.id === context.id)!;
    if (restored.kind !== "context") throw new Error("Wrong node");
    expect(restored.config.reference).toEqual(reference);
    expect(store.isDirty("main", "A")).toBe(false);
    store.clearContextCache("main", "A");
    expect(restored.config.reference).toBeNull();
  });

  it("edits incremental nodes, retains edges on mode changes and restores explicit configuration only", () => {
    const store = usePreparationStore();
    const regexId = store.addNode("main", "A", "regex")!;
    const declarationId = store.addNode("main", "A", "variable-register")!;
    const assignmentId = store.addNode("main", "A", "variable-assign")!;
    const draft = store.getDraft("main", "A");
    const prompt = draft.nodes.find((node) => node.kind === "prompt-item")!;
    store.connect("main", "A", {
      source: prompt.id, target: regexId, sourceHandle: "prompt", targetHandle: "prompt",
    });
    const edge = clonePreparation(store.getDraft("main", "A").edges.at(-1));
    store.updateNodeConfig("main", "A", regexId, {
      mode: "text", pattern: "(?P<n>\\d+)", replacement: "\\g<n>", flags: ["i"], replaceMode: "first",
    });
    expect(store.getDraft("main", "A").edges.at(-1)).toEqual(edge);
    expect(store.getPreview("main", "A").diagnostics.some((entry) => entry.code === "incompatible_port")).toBe(true);
    store.updateNodeConfig("main", "A", declarationId, {
      name: "倒计时", valueType: "integer", hasInitialValue: true, initialValue: 10,
    });
    store.updateNodeConfig("main", "A", assignmentId, {
      name: "倒计时", valueType: "integer", operation: "subtract", value: 1,
    });
    const snapshot = store.exportConfiguration();
    expect(snapshot[0].nodes.find((node) => node.id === declarationId)?.config)
      .toEqual({ name: "倒计时", valueType: "integer", hasInitialValue: true, initialValue: 10 });
    expect(JSON.stringify(snapshot)).not.toContain("currentValue");
    store.restoreConfiguration(snapshot);
    expect(store.getDraft("main", "A").nodes.find((node) => node.id === assignmentId)?.config)
      .toEqual({ name: "倒计时", valueType: "integer", operation: "subtract", value: 1 });
    expect(store.getDraft("main", "B").nodes.some((node) => node.id === declarationId)).toBe(false);
  });
});
