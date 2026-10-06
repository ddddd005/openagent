import { describe, expect, it } from "vitest";
import { DEFAULT_LEGACY_UI_URL, graphChatInterfaceUrl, resolveLegacyUiUrl, workbenchUserInterfaceUrl } from "./legacyUi";
import type { ExactPromptSelection } from "./workbenchSessions";

describe("original user interface link", () => {
  it.each([undefined, "", "   "])("uses the local fallback for %s", (value) => {
    expect(resolveLegacyUiUrl(value)).toBe(DEFAULT_LEGACY_UI_URL);
  });

  it("accepts an absolute HTTP URL", () => {
    expect(resolveLegacyUiUrl("http://127.0.0.1:9000")).toBe(
      "http://127.0.0.1:9000/",
    );
  });

  it("preserves an HTTPS path, query and fragment", () => {
    expect(
      resolveLegacyUiUrl("  https://example.test/ui?session=abc#messages  "),
    ).toBe("https://example.test/ui?session=abc#messages");
  });

  it.each([
    "not a url",
    "/existing-ui",
    "//example.test/ui",
    "javascript:alert(1)",
    "data:text/html,hello",
    "file:///C:/ui/index.html",
    "ftp://example.test/ui",
  ])("rejects invalid or unsupported URLs: %s", (value) => {
    expect(resolveLegacyUiUrl(value)).toBe(DEFAULT_LEGACY_UI_URL);
  });
});

describe("ordinary workflow chat handoff", () => {
  const workflow = "00000000-0000-4000-8000-000000000021";
  const session = "00000000-0000-4000-8000-000000000022";
  it("carries graph scope and removes fixed A/B configuration", () => {
    const url = new URL(graphChatInterfaceUrl(`${DEFAULT_LEGACY_UI_URL}?session=${session}&prompt_a=${workflow}&model_revision=1`, workflow, session));
    expect([...url.searchParams]).toEqual([["graph_workflow", workflow], ["graph_session", session]]);
  });
  it("opens a saved workflow without creating a session", () => {
    const url = new URL(graphChatInterfaceUrl(DEFAULT_LEGACY_UI_URL, workflow, null));
    expect([...url.searchParams]).toEqual([["graph_workflow", workflow]]);
  });
  it.each(["http://other.test:8765/", "https://other.test/", "http://user:secret@localhost:9000/"])("keeps local scope away from %s", (base) => {
    expect(graphChatInterfaceUrl(base, workflow, session)).toBe(base);
  });
  it.each(["http://127.0.0.1:9000/", "https://localhost:8765/", "http://[::1]:9876/chat"])("opens the configured local service at %s", base => {
    const url = new URL(graphChatInterfaceUrl(`${base}?view=messages#history`, workflow, session));
    expect(url.searchParams.get("graph_workflow")).toBe(workflow);
    expect(url.searchParams.get("graph_session")).toBe(session);
    expect(url.searchParams.get("view")).toBe("messages");
    expect(url.hash).toBe("#history");
  });
  it("rejects invalid graph identities", () => {
    expect(graphChatInterfaceUrl(DEFAULT_LEGACY_UI_URL, "invalid", session)).toBe(DEFAULT_LEGACY_UI_URL);
    expect(graphChatInterfaceUrl(DEFAULT_LEGACY_UI_URL, workflow, "invalid")).toBe(DEFAULT_LEGACY_UI_URL);
  });
});

describe("workbench user interface handoff", () => {
  const sessionId = "362b924f-b942-4799-9863-30a73eb55992";
  const selection: ExactPromptSelection = {
    schema_version: 1,
    kind: "workflow_prompt_selection",
    nodes: {
      A: { config_id: "00000000-0000-4000-8000-000000000001", revision: 2 },
      B: { config_id: "00000000-0000-4000-8000-000000000002", revision: 3 },
    },
  };

  it.each(["127.0.0.1", "localhost"])("carries exact refs to the shared backend at %s", (host) => {
    const url = new URL(workbenchUserInterfaceUrl(`http://${host}:8765/?view=messages#history`, sessionId, selection));
    expect(url.searchParams.get("session")).toBe(sessionId);
    expect(url.searchParams.get("prompt_a")).toBe(selection.nodes.A.config_id);
    expect(url.searchParams.get("prompt_a_revision")).toBe("2");
    expect(url.searchParams.get("prompt_b")).toBe(selection.nodes.B.config_id);
    expect(url.searchParams.get("prompt_b_revision")).toBe("3");
    expect(url.searchParams.get("view")).toBe("messages");
    expect(url.hash).toBe("#history");
  });

  it.each([
    "http://127.0.0.1:9000/",
    "https://localhost:8765/",
    "http://example.test:8765/",
  ])("does not transmit local identities to unrelated endpoints: %s", (base) => {
    expect(workbenchUserInterfaceUrl(base, sessionId, selection)).toBe(base);
  });

  it("can open a known session without claiming a confirmed prompt selection", () => {
    const url = new URL(workbenchUserInterfaceUrl(DEFAULT_LEGACY_UI_URL, sessionId, null));
    expect(url.searchParams.get("session")).toBe(sessionId);
    expect(url.searchParams.has("prompt_a")).toBe(false);
    expect(url.searchParams.has("prompt_b")).toBe(false);
  });
  it("carries only exact model identity and revision, never provider metadata", () => {
    const url = new URL(workbenchUserInterfaceUrl(DEFAULT_LEGACY_UI_URL, sessionId, selection, {
      schema_version: 1, kind: "workflow_model_selection", config_id: selection.nodes.A.config_id, revision: 4,
    }));
    expect(url.searchParams.get("model_config")).toBe(selection.nodes.A.config_id);
    expect(url.searchParams.get("model_revision")).toBe("4");
    expect(url.href).not.toMatch(/credential|base_url|provider/);
  });

  it.each([null, "invalid", "../../other"])("rejects unvalidated session identity: %s", (value) => {
    expect(workbenchUserInterfaceUrl(DEFAULT_LEGACY_UI_URL, value, selection)).toBe(DEFAULT_LEGACY_UI_URL);
  });

  it("omits incomplete or invalid prompt revisions", () => {
    const invalid = { ...selection, nodes: { ...selection.nodes, B: { ...selection.nodes.B, revision: 0 } } };
    const url = new URL(workbenchUserInterfaceUrl(DEFAULT_LEGACY_UI_URL, sessionId, invalid));
    expect([...url.searchParams.keys()]).toEqual(["session"]);
  });
});
