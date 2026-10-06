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
    const url = new URL(graphChatInterfaceUrl(`${DEFAULT_LEGACY_UI_URL}?session=${session}&prompt_a=${workflow}&model_revision=1`, workflow, session)!);
    expect([...url.searchParams]).toEqual([["graph_workflow", workflow], ["graph_session", session]]);
  });
  it("opens a saved workflow without creating a session", () => {
    const url = new URL(graphChatInterfaceUrl(DEFAULT_LEGACY_UI_URL, workflow, null)!);
    expect([...url.searchParams]).toEqual([["graph_workflow", workflow]]);
  });
  it.each(["http://other.test:8765/", "https://other.test/", "http://user:secret@localhost:9000/"])("disables unsupported endpoint %s instead of returning an anonymous link", (base) => {
    expect(graphChatInterfaceUrl(base, workflow, session)).toBeNull();
  });
  it.each(["http://127.0.0.1:9000/", "https://localhost:8765/", "http://[::1]:9876/static/index.html"])("opens the supported local page at %s", base => {
    const url = new URL(graphChatInterfaceUrl(`${base}?view=messages#history`, workflow, session)!);
    expect(url.searchParams.get("graph_workflow")).toBe(workflow);
    expect(url.searchParams.get("graph_session")).toBe(session);
    expect([...url.searchParams]).toEqual([["graph_workflow", workflow], ["graph_session", session]]);
    expect(url.hash).toBe("#history");
  });
  it("rejects invalid graph identities", () => {
    expect(graphChatInterfaceUrl(DEFAULT_LEGACY_UI_URL, "invalid", session)).toBeNull();
    expect(graphChatInterfaceUrl(DEFAULT_LEGACY_UI_URL, workflow, "invalid")).toBeNull();
  });
  it.each(["\n", "\r", "\r\n", " ", "\t"])("rejects extra trailing characters on graph identities: %j", suffix => {
    expect(graphChatInterfaceUrl(DEFAULT_LEGACY_UI_URL, workflow + suffix, session)).toBeNull();
    expect(graphChatInterfaceUrl(DEFAULT_LEGACY_UI_URL, workflow, session + suffix)).toBeNull();
  });
  it("cannot inherit duplicate, stale or mixed identities from its configured URL", () => {
    const base = `${DEFAULT_LEGACY_UI_URL}?graph_workflow=old&graph_workflow=other&graph_session=old&session=${session}`
      + `&prompt_a=${workflow}&prompt_a_revision=3&model_config=${workflow}&model_revision=2`
      + `&exposure_config=${workflow}&exposure_revision=4&view=messages&unknown=1#history`;
    const url = new URL(graphChatInterfaceUrl(base, workflow, null)!);
    expect([...url.searchParams]).toEqual([["graph_workflow", workflow]]);
    expect(url.hash).toBe("#history");
  });
  it.each(["invalid", "/relative", "javascript:alert(1)", "ftp://127.0.0.1:8765/",
    "http://localhost:8765/chat", "http://localhost:8765/static/app.js", "http://localhost:8765/static/index.html/"])(
    "rejects an invalid or unsupported page %s", base => {
      expect(graphChatInterfaceUrl(base, workflow, session)).toBeNull();
      expect(workbenchUserInterfaceUrl(base, session, null)).toBeNull();
    },
  );
  it("requires the supplied definition identity even when the base already contains a valid one", () => {
    expect(graphChatInterfaceUrl(`${DEFAULT_LEGACY_UI_URL}?graph_workflow=${workflow}`, "", null)).toBeNull();
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
    const url = new URL(workbenchUserInterfaceUrl(`http://${host}:8765/?view=messages#history`, sessionId, selection)!);
    expect(url.searchParams.get("session")).toBe(sessionId);
    expect(url.searchParams.get("prompt_a")).toBe(selection.nodes.A.config_id);
    expect(url.searchParams.get("prompt_a_revision")).toBe("2");
    expect(url.searchParams.get("prompt_b")).toBe(selection.nodes.B.config_id);
    expect(url.searchParams.get("prompt_b_revision")).toBe("3");
    expect(url.searchParams.has("view")).toBe(false);
    expect(url.hash).toBe("#history");
  });

  it.each([
    "http://127.0.0.1:9000/",
    "https://localhost:8765/",
    "http://example.test:8765/",
  ])("does not transmit local identities to unrelated endpoints: %s", (base) => {
    expect(workbenchUserInterfaceUrl(base, sessionId, selection)).toBeNull();
  });

  it("can open a known session without claiming a confirmed prompt selection", () => {
    const url = new URL(workbenchUserInterfaceUrl(DEFAULT_LEGACY_UI_URL, sessionId, null)!);
    expect(url.searchParams.get("session")).toBe(sessionId);
    expect(url.searchParams.has("prompt_a")).toBe(false);
    expect(url.searchParams.has("prompt_b")).toBe(false);
  });
  it("carries only exact model identity and revision, never provider metadata", () => {
    const url = new URL(workbenchUserInterfaceUrl(DEFAULT_LEGACY_UI_URL, sessionId, selection, {
      schema_version: 1, kind: "workflow_model_selection", config_id: selection.nodes.A.config_id, revision: 4,
    })!);
    expect(url.searchParams.get("model_config")).toBe(selection.nodes.A.config_id);
    expect(url.searchParams.get("model_revision")).toBe("4");
    expect(url.href).not.toMatch(/credential|base_url|provider/);
  });

  it.each([null, "invalid", "../../other"])("rejects unvalidated session identity: %s", (value) => {
    expect(workbenchUserInterfaceUrl(DEFAULT_LEGACY_UI_URL, value, selection)).toBeNull();
  });
  it.each(["\n", "\r", "\r\n", " ", "\t"])("rejects extra trailing characters on legacy session identity: %j", suffix => {
    expect(workbenchUserInterfaceUrl(DEFAULT_LEGACY_UI_URL, sessionId + suffix, selection)).toBeNull();
  });

  it("omits incomplete or invalid prompt revisions", () => {
    const invalid = { ...selection, nodes: { ...selection.nodes, B: { ...selection.nodes.B, revision: 0 } } };
    const url = new URL(workbenchUserInterfaceUrl(DEFAULT_LEGACY_UI_URL, sessionId, invalid)!);
    expect([...url.searchParams.keys()]).toEqual(["session"]);
  });
  it("removes graph identities and stale exact refs before generating an explicit legacy-session link", () => {
    const base = `${DEFAULT_LEGACY_UI_URL}static/index.html?graph_workflow=${selection.nodes.A.config_id}`
      + `&graph_session=${sessionId}&session=old&session=other&prompt_a=old&prompt_a_revision=10`
      + "&prompt_b=old&prompt_b_revision=11&model_config=old&model_revision=12&exposure_config=old&exposure_revision=13#history";
    const url = new URL(workbenchUserInterfaceUrl(base, sessionId, null)!);
    expect(url.pathname).toBe("/static/index.html");
    expect([...url.searchParams]).toEqual([["session", sessionId]]);
    expect(url.hash).toBe("#history");
  });
  it("does not adopt an existing query identity when the caller has no session binding", () => {
    expect(workbenchUserInterfaceUrl(`${DEFAULT_LEGACY_UI_URL}?session=${sessionId}`, null, selection)).toBeNull();
  });
  it.each(["http://[::1]:8765/", "http://user:secret@127.0.0.1:8765/", "https://127.0.0.1:8765/"])(
    "does not expand the legacy shared-backend endpoint policy for %s", base => {
      expect(workbenchUserInterfaceUrl(base, sessionId, selection)).toBeNull();
    },
  );
  it("cannot retain invalid optional configuration refs from its configured URL", () => {
    const base = `${DEFAULT_LEGACY_UI_URL}?model_config=${selection.nodes.A.config_id}&model_revision=99`
      + `&exposure_config=${selection.nodes.B.config_id}&exposure_revision=98`;
    const url = new URL(workbenchUserInterfaceUrl(base, sessionId, null, {
      schema_version: 1, kind: "workflow_model_selection", config_id: selection.nodes.A.config_id, revision: 0,
    }, { config_id: selection.nodes.B.config_id, revision: 0 })!);
    expect([...url.searchParams]).toEqual([["session", sessionId]]);
  });
  it.each(["A", "B"] as const)("omits the complete prompt selection when %s has a trailing-newline identity", stage => {
    const invalid = { ...selection, nodes: { ...selection.nodes,
      [stage]: { ...selection.nodes[stage], config_id: selection.nodes[stage].config_id + "\n" },
    } };
    const url = new URL(workbenchUserInterfaceUrl(DEFAULT_LEGACY_UI_URL, sessionId, invalid)!);
    expect([...url.searchParams]).toEqual([["session", sessionId]]);
  });
  it("omits model and exposure references with trailing-newline identities", () => {
    const url = new URL(workbenchUserInterfaceUrl(DEFAULT_LEGACY_UI_URL, sessionId, null, {
      schema_version: 1, kind: "workflow_model_selection", config_id: selection.nodes.A.config_id + "\n", revision: 4,
    }, { config_id: selection.nodes.B.config_id + "\n", revision: 3 })!);
    expect([...url.searchParams]).toEqual([["session", sessionId]]);
  });
});
