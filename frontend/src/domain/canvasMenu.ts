export interface MenuPoint { x: number; y: number }
export interface CanvasMenuItem { id: string; label: string; disabled?: boolean }
export interface CanvasMenuGroup { label?: string; items: CanvasMenuItem[] }

export function isAddNodeShortcut(event: Pick<KeyboardEvent,
  "key" | "shiftKey" | "ctrlKey" | "metaKey" | "altKey" | "repeat" | "isComposing">) {
  return event.shiftKey && event.key.toLowerCase() === "a"
    && !event.ctrlKey && !event.metaKey && !event.altKey && !event.repeat && !event.isComposing;
}
export function isCanvasEditable(target: EventTarget | null) {
  return target instanceof Element
    && !!target.closest("input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='textbox']");
}
export function clampMenuPoint(point: MenuPoint, size: MenuPoint, viewport: MenuPoint): MenuPoint {
  const padding = 8;
  return { x: Math.max(padding, Math.min(point.x, viewport.x - size.x - padding)),
    y: Math.max(padding, Math.min(point.y, viewport.y - size.y - padding)) };
}
