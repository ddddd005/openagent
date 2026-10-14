import { effectScope, nextTick, ref } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";
import { localDraftPorts } from "../adapters/localDraftsApi";
import { useVolatileFormState } from "./useVolatileFormState";

afterEach(() => { vi.restoreAllMocks(); });

describe("volatile editor state", () => {
  it("keeps edits only in memory and resets when the owning context changes", async () => {
    const read = vi.spyOn(localDraftPorts, "read"), save = vi.spyOn(localDraftPorts, "save");
    const workflowId = ref("workflow-a"), basis = ref(1), raw = ref("");
    const scope = effectScope();
    const state = scope.run(() => useVolatileFormState({
      id: () => workflowId.value,
      basis: () => ({ revision: basis.value }),
      reset: () => { raw.value = ""; },
    }))!;

    const originalContext = state.context.value, originalGeneration = state.generation.value;
    raw.value = "unfinished numeric text";
    await nextTick();
    expect(state.context.value).toBe(originalContext);
    expect(raw.value).toBe("unfinished numeric text");

    workflowId.value = "workflow-b";
    await nextTick();
    expect(state.context.value).not.toBe(originalContext);
    expect(state.generation.value).toBeGreaterThan(originalGeneration);
    expect(raw.value).toBe("");
    expect(read).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
    scope.stop();
  });
});
