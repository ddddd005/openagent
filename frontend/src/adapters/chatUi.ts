import { graphUuid } from "../domain/workflowGraph";

export const DEFAULT_CHAT_UI_URL = "http://127.0.0.1:8765/";
export function resolveChatUiUrl(configuredUrl?: string): string {
  try {
    const url = new URL(configuredUrl?.trim() || DEFAULT_CHAT_UI_URL);
    if (url.protocol === "http:" || url.protocol === "https:") return url.href;
  } catch { /* Fall back to the local chat page. */ }
  return DEFAULT_CHAT_UI_URL;
}
export const chatUiUrl = resolveChatUiUrl(import.meta.env.VITE_CHAT_UI_URL ?? import.meta.env.VITE_LEGACY_UI_URL);

export function graphChatInterfaceUrl(base: string, definitionId: string, sessionId: string | null): string | null {
  try {
    const url = new URL(base);
    if (!["http:", "https:"].includes(url.protocol) || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || url.username || url.password || !["/", "/static/index.html"].includes(url.pathname)
      || !graphUuid(definitionId) || sessionId !== null && !graphUuid(sessionId)) return null;
    url.search = "";
    url.searchParams.set("graph_workflow", definitionId);
    if (sessionId) url.searchParams.set("graph_session", sessionId);
    return url.href;
  } catch { return null; }
}
