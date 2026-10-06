export interface GlobalContentMember {
  id: string; name: string; text: string; role: "system" | "user" | "assistant";
  placement: "before" | "middle" | "after"; depth: number | null; order: number; enabled: boolean;
}
export interface GlobalContent {
  schema_version: 1; kind: "global_prompt" | "role_card"; resource_id: string;
  revision: number; name: string; enabled: boolean; members: GlobalContentMember[];
}
export interface SessionDataDefinition {
  schema_version: 1; definition_id: string; revision: number; key: string;
  name: string; schema: Record<string, unknown>; writable: boolean; public: boolean; default?: unknown;
}
export const CORE_SESSION_NOTE: SessionDataDefinition = {
  schema_version: 1, definition_id: "7be319b8-30bd-4674-b7bf-d1cf54a1a121",
  revision: 1, key: "core:session_note", name: "会话文本",
  schema: { type: "string", maxLength: 65536 }, writable: true, public: true, default: "",
};
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const uuid = (v: unknown) => typeof v === "string"
  && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(v);
export function isDataDefinition(v: unknown): v is SessionDataDefinition {
  return object(v) && Object.keys(v).every(k => [
    "schema_version", "definition_id", "revision", "key", "name", "schema", "writable", "public", "default",
  ].includes(k)) && v.schema_version === 1 && uuid(v.definition_id)
    && Number.isSafeInteger(v.revision) && (v.revision as number) > 0 && typeof v.key === "string"
    && /^[A-Za-z][A-Za-z0-9_.-]{0,63}:[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(v.key)
    && typeof v.name === "string" && v.name.trim().length > 0 && v.name.length <= 128
    && object(v.schema) && typeof v.writable === "boolean" && typeof v.public === "boolean";
}
export function isGlobalContent(v: unknown): v is GlobalContent {
  return object(v) && Object.keys(v).length === 7 && v.schema_version === 1
    && ["global_prompt", "role_card"].includes(v.kind as string) && uuid(v.resource_id)
    && Number.isSafeInteger(v.revision) && (v.revision as number) > 0
    && typeof v.name === "string" && v.name.trim().length > 0 && v.name.length <= 128
    && typeof v.enabled === "boolean" && Array.isArray(v.members) && v.members.length > 0 && v.members.length <= 128
    && new Set(v.members.map(m => object(m) && m.id)).size === v.members.length
    && v.members.every(m => object(m) && Object.keys(m).length === 8 && uuid(m.id)
      && typeof m.name === "string" && m.name.length <= 128 && typeof m.text === "string"
      && ["system", "user", "assistant"].includes(m.role as string)
      && ["before", "middle", "after"].includes(m.placement as string)
      && (m.placement === "middle" ? Number.isSafeInteger(m.depth) && (m.depth as number) >= 0 : m.depth === null)
      && Number.isSafeInteger(m.order) && typeof m.enabled === "boolean");
}
