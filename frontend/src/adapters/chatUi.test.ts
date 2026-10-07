import { describe, expect, it } from "vitest";
import { DEFAULT_CHAT_UI_URL, resolveChatUiUrl, graphChatInterfaceUrl } from "./chatUi";
const definition = "00000000-0000-4000-8000-000000000021";
const session = "00000000-0000-4000-8000-000000000022";
describe("current graph chat handoff", () => {
  it("builds only exact current graph coordinates and removes stale query parameters", () => {
    const url = new URL(graphChatInterfaceUrl(`${DEFAULT_CHAT_UI_URL}?session=old&prompt_a=old`, definition, session)!);
    expect([...url.searchParams]).toEqual([["graph_workflow", definition], ["graph_session", session]]);
    expect(new URL(graphChatInterfaceUrl(DEFAULT_CHAT_UI_URL, definition, null)!).search).toBe(`?graph_workflow=${definition}`);
  });
  it("requires loopback pages and exact UUIDs", () => {
    for (const base of ["https://example.com/", "file:///tmp/index.html", "http://localhost/other",
      "http://user:secret@localhost/"]) expect(graphChatInterfaceUrl(base, definition, session)).toBeNull();
    expect(graphChatInterfaceUrl(DEFAULT_CHAT_UI_URL, "invalid", session)).toBeNull();
    expect(graphChatInterfaceUrl(DEFAULT_CHAT_UI_URL, definition, "invalid")).toBeNull();
  });
  it("falls back when configuration cannot identify an HTTP page", () => {
    expect(resolveChatUiUrl("not a URL")).toBe(DEFAULT_CHAT_UI_URL);
    expect(resolveChatUiUrl("https://localhost:9000/")).toBe("https://localhost:9000/");
  });
});
