import { graphObject } from "./workflowGraph";

const protocolFields = new Set(["provider_metadata", "thoughtSignature", "thought_signature"]);

export function modelPresentation(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(modelPresentation);
  if (!graphObject(value)) return value;
  return Object.fromEntries(Object.entries(value).filter(([key]) =>
    !protocolFields.has(key) && key !== "thinking_summary").map(([key, item]) => [key, modelPresentation(item)]));
}

export function modelPresentationJson(value: unknown): string {
  return JSON.stringify(modelPresentation(value), null, 2) ?? "null";
}

export function thinkingSummaries(value: unknown): string[] {
  const summaries = new Set<string>();
  function visit(item: unknown) {
    if (Array.isArray(item)) { item.forEach(visit); return; }
    if (!graphObject(item)) return;
    if (typeof item.thinking_summary === "string" && item.thinking_summary.trim()) summaries.add(item.thinking_summary);
    for (const [key, child] of Object.entries(item)) if (!protocolFields.has(key) && key !== "thinking_summary") visit(child);
  }
  visit(value);
  return [...summaries];
}
