import { describe, expect, it } from "vitest";
import { createPreparationDraft } from "../fixtures/preparation";
import { createPreparationNode } from "./preparation";
import { clampMenuPoint, isAddNodeShortcut, preparationMenuItems } from "./canvasMenu";

const shortcut = (patch: Partial<Parameters<typeof isAddNodeShortcut>[0]> = {}) => ({
  key: "A", shiftKey: true, ctrlKey: false, metaKey: false,
  altKey: false, repeat: false, isComposing: false, ...patch,
});

describe("canvas node menu", () => {
  it("recognizes only Shift+A outside composition and key repeats", () => {
    expect(isAddNodeShortcut(shortcut())).toBe(true);
    expect(isAddNodeShortcut(shortcut({ key: "a" }))).toBe(true);
    for (const patch of [
      { shiftKey: false }, { ctrlKey: true }, { metaKey: true }, { altKey: true },
      { repeat: true }, { isComposing: true }, { key: "b" },
    ]) expect(isAddNodeShortcut(shortcut(patch))).toBe(false);
  });

  it("keeps ordinary node types available and respects unique nodes", () => {
    const draft = createPreparationDraft("main", "A");
    const items = preparationMenuItems(draft);
    expect(items).toHaveLength(19);
    expect(items.filter((item) => item.disabled).map((item) => item.id))
      .toEqual(["assemble", "root-input"]);
    expect(items.find((item) => item.id === "prompt-item")?.disabled).toBe(false);
    for (const id of ["regex", "variable-register", "variable-assign", "variable-replace",
      "text", "text-to-prompt", "prompt-to-text"])
      expect(items.find((item) => item.id === id)?.disabled).toBe(false);
    draft.nodes = draft.nodes.filter((node) => node.kind !== "context");
    expect(preparationMenuItems(draft).find((item) => item.id === "context")?.disabled).toBe(false);
  });

  it("disables all additions at the existing node limit", () => {
    const draft = createPreparationDraft("main", "B");
    while (draft.nodes.length < 128) draft.nodes.push(createPreparationNode("prompt-item", { x: 0, y: 0 }));
    expect(preparationMenuItems(draft).every((item) => item.disabled)).toBe(true);
  });

  it("keeps menus within viewport edges without changing the requested point", () => {
    const requested = { x: 995, y: 695 };
    expect(clampMenuPoint(requested, { x: 220, y: 300 }, { x: 1000, y: 700 }))
      .toEqual({ x: 772, y: 392 });
    expect(requested).toEqual({ x: 995, y: 695 });
    expect(clampMenuPoint({ x: -10, y: -10 }, { x: 220, y: 300 }, { x: 1000, y: 700 }))
      .toEqual({ x: 8, y: 8 });
    expect(clampMenuPoint({ x: 100, y: 100 }, { x: 220, y: 300 }, { x: 1000, y: 700 }))
      .toEqual({ x: 100, y: 100 });
  });
});
