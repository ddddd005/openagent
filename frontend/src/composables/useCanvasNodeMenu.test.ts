import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createRenderer, defineComponent, ref, type App } from "vue";
import { useCanvasNodeMenu } from "./useCanvasNodeMenu";

// A small host exercises lifecycle and event ownership without adding a DOM dependency.
class TestElement extends EventTarget {
  parent: TestElement | null = null;
  editable = false;
  menu = false;
  visible = true;
  focus = vi.fn();
  contains(target: unknown): boolean {
    return target === this || target instanceof TestElement && this.contains(target.parent);
  }
  closest(selector: string): TestElement | null {
    const matches = selector.includes("data-canvas-node-menu") ? this.menu : this.editable;
    return matches ? this : this.parent?.closest(selector) ?? null;
  }
  getBoundingClientRect() {
    return { left: 200, top: 40, width: this.visible ? 800 : 0, height: this.visible ? 600 : 0 };
  }
}
type HostNode = Record<string, unknown>;
const renderer = createRenderer<HostNode, HostNode>({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert: () => {}, remove: () => {}, setText: () => {}, setElementText: () => {},
  patchProp: () => {}, parentNode: () => null, nextSibling: () => null,
});
let app: App<HostNode> | undefined;
let documentTarget: EventTarget & { body: TestElement };
let windowTarget: EventTarget;
let canvas: TestElement;
let menu: ReturnType<typeof useCanvasNodeMenu>;

function event(type: string, target: EventTarget, fields: Record<string, unknown> = {}) {
  const value = new Event(type, { cancelable: true });
  Object.defineProperty(value, "target", { value: target });
  Object.assign(value, fields);
  return value;
}
function key(target: TestElement, patch: Record<string, unknown> = {}) {
  const value = event("keydown", target, {
    key: "A", shiftKey: true, ctrlKey: false, metaKey: false,
    altKey: false, repeat: false, isComposing: false, ...patch,
  });
  documentTarget.dispatchEvent(value);
  return value;
}

describe("canvas menu event scope", () => {
  beforeEach(() => {
    documentTarget = Object.assign(new EventTarget(), { body: new TestElement() });
    windowTarget = new EventTarget();
    vi.stubGlobal("Element", TestElement);
    vi.stubGlobal("Node", TestElement);
    vi.stubGlobal("document", documentTarget);
    vi.stubGlobal("window", windowTarget);
    canvas = new TestElement();
    app = renderer.createApp(defineComponent({
      setup() {
        menu = useCanvasNodeMenu(ref(canvas as unknown as HTMLElement));
        return () => null;
      },
    }));
    app.mount({});
  });
  afterEach(() => {
    app?.unmount();
    app = undefined;
    vi.unstubAllGlobals();
  });

  it("replaces the canvas context menu, focuses the canvas, and preserves the anchor", () => {
    const value = event("contextmenu", canvas, { clientX: 950, clientY: 630 });
    menu.contextMenu(value as MouseEvent);
    expect(value.defaultPrevented).toBe(true);
    expect(canvas.focus).toHaveBeenCalledWith({ preventScroll: true });
    expect(menu.menu.value).toEqual({ x: 950, y: 630, view: "context" });
    menu.open("add", menu.menu.value!);
    expect(menu.menu.value).toEqual({ x: 950, y: 630, view: "add" });
  });

  it("uses the last canvas pointer for Shift+A and the center for keyboard-only focus", () => {
    expect(key(canvas).defaultPrevented).toBe(true);
    expect(menu.menu.value).toEqual({ x: 600, y: 340, view: "add" });
    menu.pointerMove({ clientX: 850, clientY: 410 } as PointerEvent);
    key(canvas);
    expect(menu.menu.value).toEqual({ x: 850, y: 410, view: "add" });
    menu.pointerLeave();
    key(canvas);
    expect(menu.menu.value).toEqual({ x: 600, y: 340, view: "add" });
  });

  it("ignores inputs, composition and unrelated page controls", () => {
    const input = new TestElement();
    input.parent = canvas;
    input.editable = true;
    const context = event("contextmenu", input, { clientX: 300, clientY: 100 });
    menu.contextMenu(context as MouseEvent);
    expect(context.defaultPrevented).toBe(false);
    expect(key(input).defaultPrevented).toBe(false);
    expect(key(new TestElement()).defaultPrevented).toBe(false);
    expect(key(canvas, { isComposing: true }).defaultPrevented).toBe(false);
    expect(key(canvas, { ctrlKey: true }).defaultPrevented).toBe(false);
    expect(menu.menu.value).toBeNull();
  });

  it("supports a hovered unfocused canvas but does not hijack sidebar input", () => {
    menu.pointerMove({ clientX: 410, clientY: 300 } as PointerEvent);
    expect(key(new TestElement()).defaultPrevented).toBe(false);
    expect(key(documentTarget.body).defaultPrevented).toBe(true);
    expect(menu.menu.value).toEqual({ x: 410, y: 300, view: "add" });
  });

  it("dismisses with Escape, outside clicks, resize and blur without stealing outside focus", () => {
    menu.open("context");
    key(canvas, { key: "Escape", shiftKey: false });
    expect(menu.menu.value).toBeNull();
    const popup = new TestElement();
    popup.menu = true;
    menu.open("add");
    documentTarget.dispatchEvent(event("pointerdown", popup));
    expect(menu.menu.value).not.toBeNull();
    canvas.focus.mockClear();
    documentTarget.dispatchEvent(event("pointerdown", new TestElement()));
    expect(menu.menu.value).toBeNull();
    expect(canvas.focus).not.toHaveBeenCalled();
    for (const type of ["resize", "blur"]) {
      menu.open("context");
      windowTarget.dispatchEvent(new Event(type));
      expect(menu.menu.value).toBeNull();
    }
  });

  it("dismisses scrolling outside the menu but permits scrolling the node list", () => {
    menu.open("add");
    const popup = new TestElement();
    popup.menu = true;
    documentTarget.dispatchEvent(event("scroll", popup));
    expect(menu.menu.value).not.toBeNull();
    documentTarget.dispatchEvent(event("scroll", canvas));
    expect(menu.menu.value).toBeNull();
  });

  it("does not reopen hidden canvases and removes global handlers when leaving the canvas", () => {
    canvas.visible = false;
    key(canvas);
    expect(menu.menu.value).toBeNull();
    canvas.visible = true;
    app!.unmount();
    app = undefined;
    expect(key(canvas).defaultPrevented).toBe(false);
    expect(menu.menu.value).toBeNull();
  });
});
