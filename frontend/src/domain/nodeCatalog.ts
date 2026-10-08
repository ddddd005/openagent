import type { GraphNode, GraphNodeType } from "./workflowGraph";
import type { CanvasMenuGroup } from "./canvasMenu";

export interface NodeProfileAxis {
  id: string;
  label: string;
  options: { value: string; label: string }[];
}
interface ProfilePolicy { values: Record<string, string> }
interface FamilyPolicy {
  label: string;
  category: string;
  preferred: string;
  axes?: NodeProfileAxis[];
  profiles?: Record<string, ProfilePolicy>;
}
export interface NodeProfile {
  key: string;
  label: string;
  values: Record<string, string>;
  known: boolean;
  definition: GraphNodeType;
}
export interface NodeFamily {
  id: string;
  label: string;
  category: string;
  categoryLabel: string;
  axes: NodeProfileAxis[];
  profiles: NodeProfile[];
  preferredKey: string | null;
}

export const nodeCategories = [
  { id: "basic", label: "基础工具" },
  { id: "conversion", label: "格式转换" },
  { id: "variables", label: "变量" },
  { id: "prompts", label: "提示词" },
  { id: "models", label: "模型" },
  { id: "agents", label: "Agent" },
  { id: "context", label: "上下文" },
  { id: "tavern", label: "酒馆" },
  { id: "presentation", label: "前端展示" },
  { id: "other", label: "其他" },
] as const;

const protocol: NodeProfileAxis = { id: "protocol", label: "模型协议", options: [
  { value: "chat", label: "DeepSeek" }, { value: "gemini", label: "Gemini" },
] };
const promptPolicy = (label: string): FamilyPolicy => ({
  label, category: "prompts", preferred: "2",
});
const simple = (label: string, category: string): FamilyPolicy => ({ label, category, preferred: "1" });

// These are display policies for exact installed declarations, not contract migrations.
const policies: Record<string, FamilyPolicy> = {
  "tools.current-input": simple("当前输入", "basic"),
  "tools.text": simple("文本", "basic"),
  "tools.regex": simple("正则处理", "basic"),
  "tools.output": simple("输出", "basic"),
  "tools.text-to-json": simple("文本转 JSON", "conversion"),
  "tools.json-to-text": simple("JSON 转文本", "conversion"),
  "tools.text-to-prompt": simple("文本转提示词", "conversion"),
  "tools.prompt-to-text": simple("提示词转文本", "conversion"),
  "tools.json-to-prompt": simple("JSON 转提示词", "conversion"),
  "tools.prompt-to-json": simple("提示词转 JSON", "conversion"),
  "tools.variable-register": simple("变量注册", "variables"),
  "tools.variable-assign": simple("变量赋值", "variables"),
  "tools.variable-to-content": simple("变量转内容", "variables"),
  "tools.variable-replace-selected": simple("替换指定变量", "variables"),
  "tools.variable-replace-all": simple("替换全部变量", "variables"),
  "prompts.item": promptPolicy("提示词条目"),
  "prompts.group": promptPolicy("提示词分组"),
  "prompts.source": promptPolicy("提示词材料"),
  "prompts.summary": promptPolicy("提示词汇总"),
  "prompts.assembly": promptPolicy("提示词装配"),
  "prompts.global-reference": promptPolicy("全局提示词引用"),
  "prompts.global-resolve": promptPolicy("全局提示词解析"),
  "prompts.tool": promptPolicy("工具提示词"),
  "prompts.tool-summary": promptPolicy("工具提示词汇总"),
  "models.source": { label: "模型来源", category: "models", preferred: "2", axes: [protocol],
    profiles: {
      "2": { values: { protocol: "chat" } },
      "4": { values: { protocol: "gemini" } },
    } },
  "models.chat": { label: "模型调用", category: "models", preferred: "3", axes: [protocol],
    profiles: { "3": { values: { protocol: "chat" } }, "4": { values: { protocol: "gemini" } } } },
  "agents.execute": { label: "Agent 执行", category: "agents", preferred: "4", axes: [protocol],
    profiles: {
      "4": { values: { protocol: "chat" } },
      "8": { values: { protocol: "gemini" } },
    } },
  "agents.compaction-policy": simple("上下文精简策略", "agents"),
  "context.output": { label: "读取 Agent 上下文", category: "context", preferred: "4" },
  "context.assembly": { label: "上下文装配", category: "context", preferred: "4" },
  "context.merge": { label: "写回 Agent 上下文", category: "context", preferred: "4" },
  "lorebook.item": simple("世界书条目", "tavern"),
  "lorebook.group": simple("世界书分组", "tavern"),
  "lorebook.global-reference": simple("全局世界书引用", "tavern"),
  "lorebook.global-activate": simple("世界书触发", "tavern"),
  "tavern.chat.output": simple("读取聊天记录", "tavern"),
  "tavern.chat.append": simple("追加聊天记录", "tavern"),
  "tavern.chat.presentation": simple("聊天展示", "tavern"),
  "frontend.state.output": simple("读取展示记录", "presentation"),
  "frontend.state.append": simple("追加展示记录", "presentation"),
  "frontend.presentation": simple("展示输出", "presentation"),
};

export const nodeTypeKey = (type: Pick<GraphNodeType, "component_id" | "component_version">) =>
  `${type.component_id}@${type.component_version}`;

function profileFor(definition: GraphNodeType, policy?: FamilyPolicy): NodeProfile {
  const version = definition.component_version;
  const recipe = policy?.profiles?.[version];
  const known = !!recipe || !!policy && !policy.profiles && version === policy.preferred;
  const values = recipe?.values ?? {};
  const labels = policy?.axes?.map(axis => axis.options.find(option => option.value === values[axis.id])?.label)
    .filter((label): label is string => !!label) ?? [];
  return { key: nodeTypeKey(definition), definition, values: { ...values }, known,
    label: known ? labels.join(" · ") || "标准" : `${definition.display_name} · 声明 ${version}` };
}

export function buildNodeFamilies(catalog: GraphNodeType[]): NodeFamily[] {
  const retired = new Set(["agents.delta", "context.read", "context.save", "context.window",
    "context.advance", "context.project-text", "context.plan", "context.summary-prompt",
    "context.summary", "context.replace"]);
  const families = new Map<string, GraphNodeType[]>();
  for (const definition of catalog) {
    if (!definition.executable || retired.has(definition.component_id)) continue;
    const rows = families.get(definition.component_id) ?? [];
    if (!rows.some(row => row.component_version === definition.component_version)) rows.push(definition);
    families.set(definition.component_id, rows);
  }
  const knownOrder = Object.keys(policies);
  return [...families].map(([id, definitions]): NodeFamily => {
    const policy = policies[id], category = policy?.category ?? "other";
    const current = policy ? definitions.filter(row => policy.profiles
      ? !!policy.profiles[row.component_version] : row.component_version === policy.preferred)
      : [...definitions].sort((a, b) => b.component_version.localeCompare(a.component_version, undefined, { numeric: true })).slice(0, 1);
    const profiles = current.map(definition => profileFor(definition, policy));
    const names = new Set(definitions.map(row => row.display_name));
    const preferredKey = policy ? `${id}@${policy.preferred}` : profiles[0]?.key ?? null;
    return { id, label: policy?.label ?? (names.size === 1 ? definitions[0]!.display_name : id),
      category, categoryLabel: nodeCategories.find(row => row.id === category)!.label,
      axes: policy?.axes ?? [], profiles,
      preferredKey: profiles.some(row => row.key === preferredKey) ? preferredKey : profiles[0]?.key ?? null };
  }).filter(family => family.profiles.length).sort((a, b) => {
    const category = nodeCategories.findIndex(row => row.id === a.category)
      - nodeCategories.findIndex(row => row.id === b.category);
    const rank = (id: string) => knownOrder.includes(id) ? knownOrder.indexOf(id) : knownOrder.length;
    return category || rank(a.id) - rank(b.id) || a.id.localeCompare(b.id);
  });
}

export function nodeMenuGroups(families: NodeFamily[], disabled = false): CanvasMenuGroup[] {
  return nodeCategories.flatMap(category => {
    const items = families.filter(row => row.category === category.id)
      .map(row => ({ id: row.id, label: row.label, disabled }));
    return items.length ? [{ id: category.id, label: category.label, items }] : [];
  });
}

export function profileForAxis(family: NodeFamily, selected: NodeProfile, axis: string, value: string,
  profiles = family.profiles): NodeProfile | undefined {
  if (!selected.known || !family.axes.some(row => row.id === axis)) return undefined;
  return profiles.find(profile => profile.known && profile.values[axis] === value
    && family.axes.every(row => row.id === axis || profile.values[row.id] === selected.values[row.id]));
}

export function nodeTypeLabel(type: GraphNodeType): string {
  return policies[type.component_id]?.label ?? type.display_name;
}
export function nodeProfileLabel(type: GraphNodeType): string {
  return profileFor(type, policies[type.component_id]).label;
}
export function nodeDisplayTitle(node: GraphNode, definition?: GraphNodeType, catalog: GraphNodeType[] = []): string {
  const defaultTitle = definition && (node.title === definition.display_name
    || catalog.some(row => row.component_id === definition.component_id && row.display_name === node.title));
  return definition && defaultTitle ? nodeTypeLabel(definition) : node.title;
}
