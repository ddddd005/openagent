import type { PreparationDraft, PreparationNodeKind } from "./preparation";

export interface MenuPoint {
  x: number;
  y: number;
}
export interface CanvasMenuItem {
  id: string;
  label: string;
  disabled?: boolean;
}
export interface CanvasMenuGroup {
  label?: string;
  items: CanvasMenuItem[];
}
export const preparationNodeCatalog: { kind: PreparationNodeKind; label: string }[] = [
  { kind: "assemble", label: "提示词装配" },
  { kind: "prompt-collect", label: "提示词汇总" },
  { kind: "prompt-item", label: "提示词条目" },
  { kind: "prompt-group", label: "提示词条目组" },
  { kind: "tool", label: "工具" },
  { kind: "tool-collect", label: "工具汇总" },
  { kind: "context", label: "上下文" },
  { kind: "root-input", label: "当前输入" },
  { kind: "text", label: "文本" },
  { kind: "text-to-prompt", label: "文本转提示词" },
  { kind: "prompt-to-text", label: "提示词转文本" },
  { kind: "regex", label: "正则" },
  { kind: "variable-register", label: "变量注册" },
  { kind: "variable-assign", label: "变量赋值" },
  { kind: "variable-replace", label: "变量替换" },
  { kind: "global-content", label: "全局内容引用" },
  { kind: "session-data-read", label: "会话数据读取" },
  { kind: "session-data-write", label: "会话数据写入" },
  { kind: "json-to-text", label: "JSON 转文本" },
];

export function preparationMenuItems(draft: PreparationDraft): CanvasMenuItem[] {
  return preparationNodeCatalog.map(({ kind, label }) => ({
    id: kind,
    label,
    disabled: draft.nodes.length >= 128
      || (!draft.nodes.some(n => n.kind === "assemble" && !!n.config.targetStage)
        && ["assemble", "root-input"].includes(kind)
        && draft.nodes.some((node) => node.kind === kind)),
  }));
}

export function isAddNodeShortcut(event: Pick<KeyboardEvent,
  "key" | "shiftKey" | "ctrlKey" | "metaKey" | "altKey" | "repeat" | "isComposing"
>) {
  return event.shiftKey && event.key.toLowerCase() === "a"
    && !event.ctrlKey && !event.metaKey && !event.altKey
    && !event.repeat && !event.isComposing;
}

export function isCanvasEditable(target: EventTarget | null) {
  return target instanceof Element
    && !!target.closest("input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='textbox']");
}

export function clampMenuPoint(point: MenuPoint, size: MenuPoint, viewport: MenuPoint): MenuPoint {
  const padding = 8;
  return {
    x: Math.max(padding, Math.min(point.x, viewport.x - size.x - padding)),
    y: Math.max(padding, Math.min(point.y, viewport.y - size.y - padding)),
  };
}
