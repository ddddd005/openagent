import { describe, expect, it } from "vitest";
import { createPreparationDraft } from "../fixtures/preparation";
import { createWorkspaceDrafts, EMPTY_WORKFLOW_ID, MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { createPreparationNode } from "../domain/preparation";
import {
  configurationOnly, isSavedWorkbench, readWorkbench, writeWorkbench, WORKBENCH_STORAGE_KEY,
  type SavedWorkbench,
} from "./workbenchPersistence";

function fixture(): SavedWorkbench {
  return {
    schemaVersion: 1, kind: "fixed-workbench", revision: 1, savedAt: "2026-10-01T12:00:00.000Z",
    activeWorkflowId: MAIN_WORKFLOW_ID, selectedWorkflowId: EMPTY_WORKFLOW_ID,
    layouts: Object.fromEntries(Object.entries(createWorkspaceDrafts()).map(([id, draft]) =>
      [id, Object.fromEntries(draft.nodes.map((node) => [node.id, node.position]))])),
    preparations: configurationOnly([createPreparationDraft(MAIN_WORKFLOW_ID, "A"), createPreparationDraft(MAIN_WORKFLOW_ID, "B")]),
    registrations: [], runtime: { schemaVersion: 1, kind: "workbench-runtime", selectedSessions: [], sessions: [], selectionPending: null },
  };
}
function memory(initial: string | null = null) {
  let raw = initial;
  return {
    getItem: (_key: string) => raw,
    setItem: (_key: string, value: string) => { raw = value; },
    value: () => raw,
  };
}
describe("fixed workbench persistence", () => {
  it("round trips independent A/B configuration, selected/open workflows and layouts", () => {
    const storage = memory();
    const value = fixture();
    const raw = writeWorkbench(storage, value, null);
    const restored = readWorkbench(storage);
    expect(restored.error).toBeNull();
    expect(restored.value).toEqual(value);
    expect(restored.raw).toBe(raw);
    expect(restored.value?.selectedWorkflowId).not.toBe(restored.value?.activeWorkflowId);
  });
  it("strips observations without changing the live draft or losing configuration identity", () => {
    const draft = createPreparationDraft(MAIN_WORKFLOW_ID, "A");
    const context = draft.nodes.find((node) => node.kind === "context")!;
    if (context.kind !== "context") throw new Error("wrong node");
    context.config = { source: "backend-read", floors: [], reference: {
      workflowId: MAIN_WORKFLOW_ID, stage: "A", workflowSessionId: "private-session", nodeBindingId: "private-binding", selectedChainId: "private-chain",
    } };
    const saved = configurationOnly([draft]);
    expect(JSON.stringify(saved)).not.toContain("private-session");
    expect(saved[0].configId).toBe(draft.configId);
    expect(context.config.reference?.workflowSessionId).toBe("private-session");
  });
  it("preserves invalid editable configurations for visible diagnosis, not silent repair", () => {
    const value = fixture();
    const prompt = value.preparations[0].nodes.find((node) => node.kind === "prompt-item")!;
    if (prompt.kind !== "prompt-item") throw new Error("wrong node");
    prompt.config.presentation.depth = -1;
    prompt.config.presentation.placement = "middle";
    expect(isSavedWorkbench(value)).toBe(true);
    expect(readWorkbench(memory(JSON.stringify(value))).value).toEqual(value);
  });
  it("rejects malformed identities, foreign workflows, duplicate scopes and observation injection", () => {
    for (const mutate of [
      (value: SavedWorkbench) => { value.activeWorkflowId = "foreign"; },
      (value: SavedWorkbench) => { value.preparations[0].configId = "bad-id"; },
      (value: SavedWorkbench) => { value.preparations.push(value.preparations[0]); },
      (value: SavedWorkbench) => { (value as unknown as Record<string, unknown>).messages = ["private-history"]; },
      (value: SavedWorkbench) => {
        const context = value.preparations[0].nodes.find((node) => node.kind === "context")!;
        if (context.kind === "context") context.config.source = "backend-read";
      },
    ]) {
      const value = fixture();
      mutate(value);
      const original = JSON.stringify(value);
      const storage = memory(original);
      expect(readWorkbench(storage).error).toBeTruthy();
      expect(storage.value()).toBe(original);
    }
  });
  it("does not overwrite another page's changes or inaccessible/oversized records", () => {
    const storage = memory("newer");
    expect(() => writeWorkbench(storage, fixture(), null)).toThrow("其他页面");
    expect(storage.value()).toBe("newer");
    expect(readWorkbench(memory("x".repeat(4_000_001))).error).toBeTruthy();
    expect(readWorkbench({ getItem: () => { throw new Error("denied"); }, setItem: () => {} }).error).toBeTruthy();
    expect(WORKBENCH_STORAGE_KEY).toBe("workflow-workbench:fixed-base:v1");
  });
  it("round trips v4 incremental nodes and rejects current-value injection or new nodes in older versions", () => {
    const value = fixture();
    value.schemaVersion = 4;
    value.models = { schemaVersion: 1, drafts: [], pending: null };
    value.exposures = { schemaVersion: 1, publications: [], pending: null };
    const declaration = createPreparationNode("variable-register");
    const regex = createPreparationNode("regex");
    if (declaration.kind !== "variable-register" || regex.kind !== "regex") throw new Error("wrong node");
    declaration.config = { name: "倒计时", valueType: "integer", hasInitialValue: true, initialValue: 10 };
    regex.config.flags = ["i", "m"];
    value.preparations[0].nodes.push(declaration, regex);
    const storage = memory();
    writeWorkbench(storage, value, null);
    expect(readWorkbench(storage).value).toEqual(value);
    const older = structuredClone(value);
    older.schemaVersion = 3;
    expect(isSavedWorkbench(older)).toBe(false);
    (declaration.config as unknown as Record<string, unknown>).currentValue = 9;
    expect(isSavedWorkbench(value)).toBe(false);
    expect(storage.value()).not.toContain("currentValue");
  });
});
