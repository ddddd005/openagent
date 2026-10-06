import { onMounted, onUnmounted, ref, type Ref } from "vue";
import { isAddNodeShortcut, isCanvasEditable, type MenuPoint } from "../domain/canvasMenu";

export interface CanvasMenuState extends MenuPoint {
  view: "context" | "add";
}

export function useCanvasNodeMenu(canvas: Ref<HTMLElement | null>) {
  const menu = ref<CanvasMenuState | null>(null);
  const capture = { capture: true };
  let pointer: MenuPoint | null = null;
  let hovered = false;

  function close(restoreFocus = false) {
    menu.value = null;
    if (restoreFocus) canvas.value?.focus({ preventScroll: true });
  }

  function open(view: CanvasMenuState["view"], point?: MenuPoint) {
    const rect = canvas.value?.getBoundingClientRect();
    if (!rect || rect.width <= 0 || rect.height <= 0) return;
    const anchor = point ?? (hovered && pointer ? pointer : {
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
    });
    menu.value = { ...anchor, view };
    canvas.value?.focus({ preventScroll: true });
  }

  function contextMenu(event: MouseEvent) {
    if (isCanvasEditable(event.target)) return;
    event.preventDefault();
    event.stopPropagation();
    open("context", { x: event.clientX, y: event.clientY });
  }

  function pointerMove(event: PointerEvent) {
    hovered = true;
    pointer = { x: event.clientX, y: event.clientY };
  }

  function keyboard(event: KeyboardEvent) {
    if (event.defaultPrevented || isCanvasEditable(event.target)) return;
    if (menu.value && event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close(true);
      return;
    }
    const target = event.target;
    const inCanvas = target instanceof Node && canvas.value?.contains(target);
    const inMenu = target instanceof Element && !!target.closest("[data-canvas-node-menu]");
    if (!inCanvas && !inMenu && !(target === document.body && hovered)) return;
    if (!isAddNodeShortcut(event)) return;
    event.preventDefault();
    event.stopPropagation();
    open("add");
  }

  function outsideClick(event: PointerEvent) {
    if (event.target instanceof Element && event.target.closest("[data-canvas-node-menu]")) return;
    close();
  }
  const dismiss = () => close();
  const scroll = (event: Event) => {
    if (event.target instanceof Element && event.target.closest("[data-canvas-node-menu]")) return;
    close();
  };
  onMounted(() => {
    document.addEventListener("keydown", keyboard, capture);
    document.addEventListener("pointerdown", outsideClick, capture);
    window.addEventListener("resize", dismiss);
    window.addEventListener("blur", dismiss);
    document.addEventListener("scroll", scroll, capture);
  });
  onUnmounted(() => {
    document.removeEventListener("keydown", keyboard, capture);
    document.removeEventListener("pointerdown", outsideClick, capture);
    window.removeEventListener("resize", dismiss);
    window.removeEventListener("blur", dismiss);
    document.removeEventListener("scroll", scroll, capture);
  });

  return { menu, open, close, contextMenu, pointerMove, pointerLeave: () => { hovered = false; } };
}
