import type { CatalogNode, LocalDraft, Port, PortType } from "../domain/draft";

const port = (id: string, label: string, type: PortType): Port => ({
  id,
  label,
  type,
  version: "fixture-1",
});

// Only UI fixtures; these descriptors do not claim to describe the backend registry.
export const fixtureCatalog: CatalogNode[] = [
  {
    kind: "items",
    title: "提示词条目",
    category: "输入",
    description: "有序条目与来源引用",
    color: "#7091d6",
    inputs: [],
    outputs: [port("items", "条目", "PromptItem")],
    defaults: { role: "system", text: "你是一位严谨的研究助手。" },
  },
  {
    kind: "collect",
    title: "条目汇集",
    category: "处理",
    description: "保留条目身份与顺序",
    color: "#9a83d4",
    inputs: [port("items", "条目", "PromptItem")],
    outputs: [port("collection", "集合", "PromptCollection")],
    defaults: { order: "按输入顺序" },
  },
  {
    kind: "macro",
    title: "宏展开",
    category: "处理",
    description: "单节点、单遍宏处理",
    color: "#42a79a",
    inputs: [port("collection", "集合", "PromptCollection")],
    outputs: [port("collection", "集合", "PromptCollection")],
    defaults: { expression: "{{user.name}}", mode: "单遍" },
  },
  {
    kind: "regex",
    title: "正则处理",
    category: "处理",
    description: "显式选择处理类型",
    color: "#d2a365",
    inputs: [port("collection", "集合", "PromptCollection")],
    outputs: [port("collection", "集合", "PromptCollection")],
    defaults: { pattern: "\\s+$", replacement: "", mode: "PromptCollection" },
  },
  {
    kind: "context",
    title: "上下文读取",
    category: "输入",
    description: "公开上下文视图",
    color: "#7091d6",
    inputs: [],
    outputs: [port("context", "上下文", "ContextView")],
    defaults: { source: "已选历史", limit: "12" },
  },
  {
    kind: "assemble",
    title: "提示词装配",
    category: "输出",
    description: "集合与上下文的装配边界",
    color: "#42a79a",
    inputs: [
      port("collection", "集合", "PromptCollection"),
      port("context", "上下文", "ContextView"),
    ],
    outputs: [port("prompt", "装配结果", "PromptCollection")],
    defaults: { target: "A · 提示词准备" },
  },
  {
    kind: "to-text",
    title: "显式转文本",
    category: "转换",
    description: "集合到文本的显式转换",
    color: "#b783a7",
    inputs: [port("collection", "集合", "PromptCollection")],
    outputs: [port("text", "文本", "Text")],
    defaults: { separator: "\\n\\n" },
  },
];

export function createFixtureDraft(): LocalDraft {
  const entries = [
    ["local:items", "items", 70, 90],
    ["local:collect", "collect", 340, 90],
    ["local:macro", "macro", 610, 90],
    ["local:regex", "regex", 610, 365],
    ["local:context", "context", 70, 365],
    ["local:assemble", "assemble", 920, 245],
  ] as const;
  const nodes = entries.map(([id, kind]) => {
    const item = fixtureCatalog.find((n) => n.kind === kind)!;
    return {
      id,
      kind,
      title: item.title,
      scope: "root",
      config: { ...item.defaults },
      inputs: structuredClone(item.inputs),
      outputs: structuredClone(item.outputs),
    };
  });
  const pairs = [
    ["local:items", "items", "local:collect", "items"],
    ["local:collect", "collection", "local:macro", "collection"],
    ["local:macro", "collection", "local:regex", "collection"],
    ["local:regex", "collection", "local:assemble", "collection"],
    ["local:context", "context", "local:assemble", "context"],
  ];
  return {
    format: "workflow-ui-local/v1",
    id: "local:draft-prompt-a",
    title: "A · 提示词准备",
    nodes,
    edges: pairs.map(([source, sourcePort, target, targetPort], i) => ({
      id: `local:edge-${i}`,
      scope: "root",
      source,
      sourcePort,
      target,
      targetPort,
    })),
    groups: {},
    layout: Object.fromEntries(entries.map(([id, , x, y]) => [id, { x, y }])),
  };
}
