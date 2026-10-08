import { describe, expect, it } from "vitest";
import { clampMenuPoint, isAddNodeShortcut, placeNodeSubmenu } from "./canvasMenu";

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
  it("opens the second level right, flips left, or uses compact navigation without clipping", () => {
    const size = { x: 230, y: 450 }, viewport = { x: 1000, y: 700 };
    expect(placeNodeSubmenu({ x: 100, y: 100, width: 220 }, 130, size, viewport))
      .toEqual({ compact: false, position: { x: 324, y: 130 } });
    expect(placeNodeSubmenu({ x: 772, y: 100, width: 220 }, 600, size, viewport))
      .toEqual({ compact: false, position: { x: 538, y: 242 } });
    expect(placeNodeSubmenu({ x: 90, y: 100, width: 220 }, 700, size, { x: 390, y: 844 }))
      .toEqual({ compact: true, position: { x: 8, y: 386 } });
  });
});
