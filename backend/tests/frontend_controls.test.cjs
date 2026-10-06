const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(
  path.join(__dirname, "../src/phase1_agent/static/app.js"), "utf8"
);

const PROMPT_CONFIG_ID = "00000000-0000-4000-8000-000000000901";
const promptRecord = (revision = 2) => ({
  record: { config_id: PROMPT_CONFIG_ID, revision, name: "本轮规则" },
  head: { selectable: true }
});
const inputReceipt = (sessionId = "s1") => ({
  workflow_session_id: sessionId, status: "prepared",
  input_id: "00000000-0000-4000-8000-000000000911",
  visible_message_id: "00000000-0000-4000-8000-000000000912",
  chain_run_id: "00000000-0000-4000-8000-000000000913"
});

class Element {
  constructor() {
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.value = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
  }

  get options() { return this.children; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute(name, value) { this[name] = value; }
  append(...children) { this.children.push(...children); }
  add(child) { this.append(child); }
  replaceChildren(...children) { this.children = children; }
  dispatch(name) { return this.listeners[name]({ preventDefault() {} }); }
}

function sessionView({ status = "running", action = "interrupt", revision = 3, runRevision = 7 } = {}) {
  return {
    mode: "offline", revision, can_submit: false, messages: [], error: null,
    available_actions: action ? [action] : [],
    nodes: [
      { label: "A / Draft", run_id: "r1", revision: runRevision, status },
      { label: "B / Revision", run_id: null, revision: null, status: "idle" },
      { label: "Output", run_id: null, revision: null, status: "idle" }
    ],
    chains: [{ chain_run_id: "c1", status: status === "paused" ? "paused" : "running" }]
  };
}

async function setup(onControl, {
  initialView, onVersion, onBranch, onSessionSwitch, onCloseout, onPending, onRecovery, onBudget,
  onSubmit, promptCatalog = [], storage = new Map(), locationSearch = "",
  locationPathname = "/", locationHash = "",
  initialSessionId = "s1", extraSessions = [], exactConfigResponse, onExposure, onNodeOutputs
} = {}) {
  const ids = [
    "sessions", "input", "submit", "error", "mode", "stage-a", "stage-b",
    "stage-output", "messages", "empty", "progress", "run-status",
    "run-control", "reroll-interrupted", "retry-closeout", "start-pending",
    "close-execution", "continue-workflow", "budget-a", "budget-b",
    "budget-controls", "additional-model-requests", "additional-model-attempts", "extend-budget",
    "new-session", "composer", "prompt-config-a", "prompt-revision-a",
    "prompt-config-b", "prompt-revision-b", "prompt-state", "reset-prompt-state",
    "public-exposures", "public-exposure-list", "public-exposure-status",
    "public-node-outputs", "public-node-list", "public-node-status"
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element()]));
  const timers = new Map();
  const timerDelays = new Map();
  const posts = [];
  const versionPosts = [];
  const branchPosts = [];
  const sessionPosts = [];
  const closeoutPosts = [];
  const pendingPosts = [];
  const recoveryPosts = [];
  const budgetPosts = [];
  const inputPosts = [];
  const requests = [];
  const keys = [];
  const historyWrites = [];
  let nextTimer = 0;
  let nextKey = 0;
  let view = initialView || sessionView();
  let childView = null;
  let sessionRows = [{ workflow_session_id: initialSessionId },
    ...extraSessions.map((id) => ({ workflow_session_id: id }))];
  let activeSelection = { active_workflow_session_id: initialSessionId, revision: 1 };
  let promptRows = promptCatalog;
  const context = {
    document: {
      getElementById: (id) => elements[id],
      createElement: (tagName) => {
        const element = new Element();
        element.tagName = tagName;
        return element;
      }
    },
    Option: class extends Element {
      constructor(text, value) { super(); this.textContent = text; this.value = value; }
    },
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key)
    },
    location: { pathname: locationPathname, search: locationSearch, hash: locationHash },
    history: {
      state: null,
      replaceState: (state, title, url) => {
        historyWrites.push({ state, title, url });
        const parsed = new URL(url, "http://127.0.0.1");
        Object.assign(context.location, {
          pathname: parsed.pathname, search: parsed.search, hash: parsed.hash
        });
      }
    },
    URLSearchParams,
    crypto: { randomUUID: () => { const key = `key-${++nextKey}`; keys.push(key); return key; } },
    setTimeout: (fn, delay) => {
      timers.set(++nextTimer, fn);
      timerDelays.set(nextTimer, delay);
      return nextTimer;
    },
    clearTimeout: (id) => {
      timers.delete(id);
      timerDelays.delete(id);
    },
    fetch: async (url, options = {}) => {
      requests.push({ url, options });
      if (url === "/api/health") return response(200, { mode: "offline" });
      if (url === "/api/sessions") return response(200, sessionRows);
      if (url === "/api/prompt-configs/config") return response(200, promptRows);
      const exactPrompt = /^\/api\/prompt-configs\/config\/([^/]+)\/revisions\/([1-9][0-9]*)$/.exec(url);
      if (exactPrompt && !options.method) return exactConfigResponse
        ? exactConfigResponse(exactPrompt[1], Number(exactPrompt[2]))
        : response(200, {
          schema_version: 1, kind: "config", config_id: exactPrompt[1],
          revision: Number(exactPrompt[2]), name: "Exact fixture", inputs: []
        });
      const exactModel = /^\/api\/model-configurations\/model\/([^/]+)\/revisions\/([1-9][0-9]*)$/.exec(url);
      if (exactModel && !options.method) return response(200, {
        schema_version: 1, kind: "workflow_model_configuration",
        config_id: exactModel[1], revision: Number(exactModel[2]), nodes: [], edges: []
      });
      const exactExposure = /^\/api\/exposure-configurations\/([^/]+)\/revisions\/([1-9][0-9]*)$/.exec(url);
      if (exactExposure && !options.method) return response(200, {
        schema_version: 1, kind: "workflow_exposure_configuration",
        config_id: exactExposure[1], revision: Number(exactExposure[2]),
        workflow_id: "frontend:main-test", registrations: []
      });
      if (url.includes("/exposures/") && !options.method) return onExposure
        ? onExposure(url, view) : response(404, { error: { code: "not_found" } });
      if (url.endsWith("/outputs/public") && !options.method) return onNodeOutputs
        ? onNodeOutputs(url, view) : response(200, {
          schema_version: 1, kind: "session_public_outputs",
          workflow_session_id: url.split("/")[3], session_revision: view.revision,
          chain_run_id: null, nodes: []
        });
      if (url === "/api/active-session" && !options.method)
        return response(200, activeSelection);
      if (url === "/api/active-session" && options.method === "POST") {
        sessionPosts.push({ url, options, body: JSON.parse(options.body) });
        const result = onSessionSwitch
          ? await onSessionSwitch(sessionPosts.length, sessionPosts.at(-1), (selected) => {
            activeSelection = selected;
          })
          : response(200, {
            active_workflow_session_id: sessionPosts.at(-1).body.workflow_session_id,
            revision: activeSelection.revision + 1
          });
        if (result.ok) activeSelection = await result.json();
        return result;
      }
      if (url === `/api/sessions/${initialSessionId}` && !options.method) return response(200, view);
      if (url === "/api/sessions/s2" && !options.method) return response(200, childView || view);
      if (/^\/api\/sessions\/[^/]+$/.test(url) && !options.method) return response(200, view);
      if (url.endsWith("/inputs") && options.method === "POST") {
        inputPosts.push({ url, options, body: JSON.parse(options.body) });
        return onSubmit
          ? onSubmit(inputPosts.length, inputPosts.at(-1), (updated) => { view = updated; })
          : response(202, inputReceipt(url.split("/")[3]));
      }
      if (url.includes("/pending-inputs/") && url.endsWith("/continue")) {
        pendingPosts.push({ url, options, body: JSON.parse(options.body) });
        return onPending(pendingPosts.length, pendingPosts.at(-1), (updated) => { view = updated; });
      }
      if (url.endsWith("/close_execution") || url.endsWith("/continue_workflow")) {
        recoveryPosts.push({ url, options, body: JSON.parse(options.body) });
        return onRecovery(recoveryPosts.length, recoveryPosts.at(-1), (updated) => { view = updated; });
      }
      if (url.includes("/retry-archive") || url.includes("/retry-publish")) {
        closeoutPosts.push({ url, options, body: JSON.parse(options.body) });
        return onCloseout(closeoutPosts.length, closeoutPosts.at(-1), (updated) => { view = updated; });
      }
      if (url.endsWith("/extend_budget")) {
        budgetPosts.push({ url, options, body: JSON.parse(options.body) });
        return onBudget(budgetPosts.length, budgetPosts.at(-1), (updated) => { view = updated; });
      }
      if (url.startsWith("/api/sessions/s1/runs/")) {
        posts.push({ url, options, body: JSON.parse(options.body) });
        return onControl(posts.length, posts.at(-1), (updated) => { view = updated; });
      }
      if (url.includes("/reroll") || url.includes("/select")) {
        versionPosts.push({ url, options, body: JSON.parse(options.body) });
        return onVersion(versionPosts.length, versionPosts.at(-1), (updated) => { view = updated; });
      }
      if (url.includes("/branches/switch")) {
        branchPosts.push({ url, options, body: JSON.parse(options.body) });
        const result = await onBranch(branchPosts.length, branchPosts.at(-1), {
          setView(updated) { view = updated; },
          addChild(updated) {
            childView = updated;
            sessionRows = [...sessionRows, { workflow_session_id: "s2" }];
          }
        });
        if (result.ok) activeSelection = {
          active_workflow_session_id: "s2", revision: activeSelection.revision + 1
        };
        return result;
      }
      throw new Error(`Unexpected request ${url}`);
    }
  };
  vm.createContext(context);
  vm.runInContext(script, context);
  await new Promise((resolve) => setImmediate(resolve));
  return {
    elements, timers, timerDelays, posts, versionPosts, branchPosts, sessionPosts, closeoutPosts,
    pendingPosts, recoveryPosts, budgetPosts, inputPosts, context, requests, storage, keys, historyWrites,
    setView(updated) { view = updated; },
    addSession(id) { sessionRows = [...sessionRows, { workflow_session_id: id }]; },
    setActiveSelection(selection) { activeSelection = selection; },
    setPromptCatalog(rows) { promptRows = rows; },
    loadPromptConfigs: () => context.loadPromptConfigs(),
    loadSessions: () => context.loadSessions(),
    refresh: () => context.refreshSession(),
    click: () => elements["run-control"].dispatch("click")
  };
}

function descendants(element, predicate) {
  return [element, ...element.children.flatMap((child) => descendants(child, predicate))]
    .filter(predicate);
}

function byClass(ui, className) {
  return descendants(ui.elements.messages, (item) => item.className === className);
}

function replyView({
  revision = 3, refRevision = 2, headCommitId = "head-1",
  candidates = [{ candidate_id: "candidate-1", chain_run_id: "c1", payload: "原回复", selected: true }],
  chainStatus = "succeeded", canReroll = true, canSelect = true, latestUser = false
} = {}) {
  const messages = [
    { visible_message_id: "user-1", role: "user", payload: "问题", sequence: 1, chain_run_id: "c1" },
    {
      visible_message_id: "reply-1", role: "assistant",
      payload: candidates.find((candidate) => candidate.selected)?.payload || "原回复",
      sequence: 2, chain_run_id: candidates.find((candidate) => candidate.selected)?.chain_run_id || "c1",
      reply_candidates: candidates
    }
  ];
  if (latestUser) messages.push({
    visible_message_id: "user-2", role: "user", payload: "下个问题",
    sequence: 3, chain_run_id: "c2"
  });
  return {
    mode: "offline", revision, ref_revision: refRevision, head_commit_id: headCommitId,
    can_submit: !latestUser, can_reroll: canReroll,
    can_select_candidates: canSelect, messages, error: null,
    available_actions: [], nodes: [],
    chains: [{ chain_run_id: "c1", status: chainStatus }]
  };
}

function response(status, data) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
}

const EXPOSURE_ID = "00000000-0000-4000-8000-000000009502";
const EXPOSURE_ROW = {
  schemaVersion: 1, id: "00000000-0000-4000-8000-000000009503",
  workflowId: "frontend:main-test", stage: "A", nodeBindingId: "7be319b8-30bd-4674-b7bf-d1cf54a1a108",
  publicName: "A.result", kind: "result", type: "public_node_result", fields: ["result_text"]
};
function exposureRead(sid, view, { revoked = false, privateField = false } = {}) {
  return {
    schema_version: 1, kind: "workflow_exposure_read", config_id: EXPOSURE_ID, revision: 1,
    workflow_session_id: sid, session_revision: view.revision, head_commit_id: view.head_commit_id,
    availability: revoked ? "unavailable" : "available", reason_code: revoked ? "declaration_stale" : null,
    registrations: revoked ? [] : [EXPOSURE_ROW],
    observations: revoked ? [] : [{
      registrationId: EXPOSURE_ROW.id, workflowId: EXPOSURE_ROW.workflowId, workflowSessionId: sid,
      nodeBindingId: EXPOSURE_ROW.nodeBindingId, runId: "00000000-0000-4000-8000-000000009504",
      schemaVersion: 1, availability: "available",
      value: { result_text: "safe A result", ...(privateField ? { private_data: "secret" } : {}) }
    }]
  };
}

test("user interface consumes exact public declarations and clears revoked values without POSTs", async () => {
  const sid = "00000000-0000-4000-8000-000000009505";
  let revoked = false;
  const ui = await setup(null, {
    initialSessionId: sid, initialView: replyView(), locationSearch: `?session=${sid}&exposure_config=${EXPOSURE_ID}&exposure_revision=1`,
    onExposure: (_url, view) => response(200, exposureRead(sid, view, { revoked }))
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.elements["public-exposures"].hidden, false);
  assert.ok(descendants(ui.elements["public-exposure-list"], (row) => row.textContent.includes("safe A result")).length);
  assert.equal(ui.inputPosts.length, 0);
  assert.equal(ui.sessionPosts.length, 0);
  assert.ok(Array.from(ui.timerDelays.values()).includes(1500));
  assert.equal(JSON.parse(ui.storage.get("workflow-user-exposure:v1:" + sid)).config_id, EXPOSURE_ID);
  revoked = true;
  await ui.refresh();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.elements["public-exposure-list"].children.length, 0);
  assert.match(ui.elements["public-exposure-status"].textContent, /撤销/);
});

test("user interface rejects undeclared public fields and late reads after session switch", async () => {
  const sid = "00000000-0000-4000-8000-000000009505";
  const storage = new Map([["workflow-user-exposure:v1:" + sid, JSON.stringify({ config_id: EXPOSURE_ID, revision: 1 })]]);
  const ui = await setup(null, {
    initialSessionId: sid, initialView: replyView(), storage,
    onExposure: (_url, view) => response(200, exposureRead(sid, view, { privateField: true }))
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.elements["public-exposure-list"].children.length, 0);
  assert.doesNotMatch(ui.elements["public-exposure-status"].textContent, /secret/);
  ui.addSession("s2");
  ui.setActiveSelection({ active_workflow_session_id: "s2", revision: 2 });
  await ui.loadSessions();
  assert.equal(ui.elements["public-exposure-list"].children.length, 0);
  assert.equal(ui.elements["public-exposures"].hidden, true);
});

test("cloned workflow public declarations retain their independent workflow identity", async () => {
  const workflowId = "00000000-0000-4000-8000-000000009506";
  const ui = await setup(null, { initialView: replyView() });
  const value = exposureRead("s1", replyView());
  value.registrations = [{ ...EXPOSURE_ROW, workflowId }];
  value.observations[0].workflowId = workflowId;
  assert.equal(ui.context.validateExposureRead(value, { config_id: EXPOSURE_ID, revision: 1 },
    replyView(), "s1").registrations[0].workflowId, workflowId);
});

test("node public outputs are discovered read-only and cleared rather than carrying previous results", async () => {
  let produced = true;
  const ui = await setup(null, {
    initialView: replyView(),
    onNodeOutputs: (url, view) => response(200, {
      schema_version: 1, kind: "session_public_outputs", workflow_session_id: url.split("/")[3],
      session_revision: view.revision, chain_run_id: "00000000-0000-4000-8000-000000009507",
      nodes: produced ? [{
        schema_version: 1, workflow_session_id: url.split("/")[3], node_id: "public-source",
        run_id: "00000000-0000-4000-8000-000000009508", status: "succeeded",
        outputs: { text: { kind: "text", text: "prepared value" } }
      }] : []
    })
  });
  assert.equal(ui.elements["public-node-outputs"].hidden, false);
  assert.ok(descendants(ui.elements["public-node-list"], row => row.textContent.includes("prepared value")).length);
  assert.equal(ui.inputPosts.length, 0);
  produced = false;
  await ui.refresh();
  assert.equal(ui.elements["public-node-list"].children.length, 0);
  assert.equal(ui.elements["public-node-outputs"].hidden, true);
});

test("late node public outputs cannot leak across a session switch", async () => {
  let deliver;
  const ui = await setup(null, {
    initialView: replyView(),
    onNodeOutputs: (url, view) => url.includes("/s1/")
      ? new Promise(resolve => {
        deliver = () => resolve(response(200, {
          schema_version: 1, kind: "session_public_outputs", workflow_session_id: "s1",
          session_revision: view.revision, chain_run_id: null,
          nodes: [{ schema_version: 1, workflow_session_id: "s1", node_id: "old",
            run_id: "00000000-0000-4000-8000-000000009508", status: "succeeded",
            outputs: { text: { kind: "text", text: "old value" } } }]
        }));
      })
      : response(200, { schema_version: 1, kind: "session_public_outputs",
        workflow_session_id: "s2", session_revision: view.revision, chain_run_id: null, nodes: [] })
  });
  ui.addSession("s2");
  ui.setActiveSelection({ active_workflow_session_id: "s2", revision: 2 });
  await ui.loadSessions();
  deliver();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.elements["public-node-list"].children.length, 0);
  assert.equal(ui.elements["public-node-outputs"].hidden, true);
});

test("candidate fork inherits only the public declaration reference and reads the child independently", async () => {
  const reference = { config_id: EXPOSURE_ID, revision: 1 };
  const storage = new Map([["workflow-user-exposure:v1:s1", JSON.stringify(reference)]]);
  const ui = await setup(null, {
    initialView: replyView(), storage,
    onExposure: (url, view) => response(200, exposureRead(url.split("/")[3], view)),
    onBranch: async (_attempt, _post, service) => {
      service.addChild(replyView());
      return response(201, { workflow_session_id: "s2" });
    }
  });
  await byClass(ui, "fork-candidate")[0].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.elements.sessions.value, "s2");
  assert.deepEqual(JSON.parse(storage.get("workflow-user-exposure:v1:s2")), reference);
  assert.doesNotMatch(storage.get("workflow-user-exposure:v1:s2"), /safe A result|observations/);
  assert.ok(ui.requests.some(({ url }) => url.startsWith("/api/sessions/s2/exposures/")));
  assert.equal(ui.inputPosts.length, 0);
});

test("a late public read cannot repopulate values after switching to an unregistered session", async () => {
  let deliver;
  const storage = new Map([["workflow-user-exposure:v1:s1", JSON.stringify({ config_id: EXPOSURE_ID, revision: 1 })]]);
  const ui = await setup(null, {
    initialView: replyView(), storage,
    onExposure: (_url, view) => new Promise(resolve => {
      deliver = () => resolve(response(200, exposureRead("s1", view)));
    })
  });
  assert.equal(typeof deliver, "function");
  ui.addSession("s2");
  ui.setActiveSelection({ active_workflow_session_id: "s2", revision: 2 });
  await ui.loadSessions();
  deliver();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.elements["public-exposure-list"].children.length, 0);
  assert.equal(ui.elements["public-exposures"].hidden, true);
});

test("default composer preserves the original submission body", async () => {
  const ui = await setup(null, { initialView: replyView() });
  ui.elements.input.value = "新输入";
  await ui.elements.composer.dispatch("submit");
  assert.deepEqual(ui.inputPosts[0].body, { text: "新输入", idempotency_key: "key-1" });
  assert.equal(ui.elements["prompt-revision-a"].disabled, true);
  assert.equal(ui.elements["prompt-revision-b"].disabled, true);
});

test("composer sends selected A/B exact revisions and leaves omitted stages unchanged", async () => {
  const ui = await setup(null, { initialView: replyView(), promptCatalog: [promptRecord()] });
  ui.elements["prompt-config-a"].value = PROMPT_CONFIG_ID;
  ui.elements["prompt-config-a"].dispatch("change");
  assert.equal(ui.elements["prompt-revision-a"].value, "2");
  assert.equal(ui.elements["prompt-revision-a"].disabled, false);
  ui.elements["prompt-revision-a"].value = "1";
  ui.elements.input.value = "新输入";
  await ui.elements.composer.dispatch("submit");
  assert.deepEqual(ui.inputPosts[0].body.prompt_selection, {
    schema_version: 1, kind: "workflow_prompt_selection",
    nodes: { A: { config_id: PROMPT_CONFIG_ID, revision: 1 } }
  });
});

test("lost-response submit retry preserves its exact choice and key after catalog updates", async () => {
  const ui = await setup(null, {
    initialView: replyView(), promptCatalog: [promptRecord(1)],
    onSubmit: async (attempt) => {
      if (attempt === 1) throw new TypeError("Connection lost");
      return response(202, inputReceipt());
    }
  });
  ui.elements["prompt-config-b"].value = PROMPT_CONFIG_ID;
  ui.elements["prompt-config-b"].dispatch("change");
  ui.elements.input.value = "新输入";
  await ui.elements.composer.dispatch("submit");
  ui.setPromptCatalog([promptRecord(2)]);
  await ui.loadPromptConfigs();
  assert.equal(ui.elements["prompt-revision-b"].value, "1");
  await ui.elements.composer.dispatch("submit");
  assert.deepEqual(ui.inputPosts[1].body, ui.inputPosts[0].body);
  assert.equal(ui.inputPosts[1].body.idempotency_key, "key-1");
});

test("changing an explicit revision cannot replace an unverified submission identity", async () => {
  const ui = await setup(null, {
    initialView: replyView(), promptCatalog: [promptRecord()],
    onSubmit: async () => { throw new TypeError("Connection lost"); }
  });
  ui.elements["prompt-config-a"].value = PROMPT_CONFIG_ID;
  ui.elements["prompt-config-a"].dispatch("change");
  ui.elements.input.value = "相同输入";
  await ui.elements.composer.dispatch("submit");
  ui.elements["prompt-revision-a"].value = "1";
  await ui.elements.composer.dispatch("submit");
  assert.equal(ui.inputPosts[0].body.idempotency_key, "key-1");
  assert.equal(ui.inputPosts[1].body.idempotency_key, "key-1");
  assert.equal(ui.inputPosts[0].body.prompt_selection.nodes.A.revision, 2);
  assert.equal(ui.inputPosts[1].body.prompt_selection.nodes.A.revision, 2);
  assert.equal(ui.elements["prompt-config-a"].disabled, true);
  assert.equal(ui.elements.input.disabled, true);
});

test("invalid successful input receipts retain the original request until an exact receipt is verified", async () => {
  for (const changed of [
    { workflow_session_id: "another-session" }, { status: "succeeded" },
    { input_id: null }, { visible_message_id: "invalid" }, { chain_run_id: "not-a-uuid" }
  ]) {
    const storage = new Map();
    const ui = await setup(null, {
      initialView: replyView(), storage,
      onSubmit: async (attempt) => response(202, {
        ...inputReceipt(), ...(attempt === 1 ? changed : {})
      })
    });
    ui.elements.input.value = "preserve original";
    await ui.elements.composer.dispatch("submit");
    const body = ui.inputPosts[0].body;
    assert.deepEqual(JSON.parse(storage.get(USER_SESSION_PREFIX + "s1")).pending_submission, body);
    assert.equal(ui.elements.input.disabled, true);
    assert.equal(ui.elements.submit.textContent, "核实提交");
    assert.match(ui.elements.error.textContent, /回执无效/);
    await ui.elements.composer.dispatch("submit");
    assert.deepEqual(ui.inputPosts[1].body, body);
    assert.equal(ui.keys.length, 1);
    assert.equal(JSON.parse(storage.get(USER_SESSION_PREFIX + "s1")).pending_submission, null);
  }
});

test("malformed or unsafe revisions cannot send a model-start request", async () => {
  const ui = await setup(null, { initialView: replyView(), promptCatalog: [promptRecord()] });
  ui.elements["prompt-config-a"].value = PROMPT_CONFIG_ID;
  ui.elements["prompt-config-a"].dispatch("change");
  ui.elements.input.value = "输入";
  for (const revision of ["", "0", "-1", "1.5", "9007199254740992"]) {
    ui.elements["prompt-revision-a"].value = revision;
    await ui.elements.composer.dispatch("submit");
  }
  assert.equal(ui.inputPosts.length, 0);
  assert.equal(ui.elements.error.textContent, "A 提示词修订无效");
});

test("active workflow locks prompt configuration and revision controls", async () => {
  const ui = await setup(null, { initialView: sessionView(), promptCatalog: [promptRecord()] });
  assert.equal(ui.elements["prompt-config-a"].disabled, true);
  assert.equal(ui.elements["prompt-config-b"].disabled, true);
  assert.equal(ui.elements["prompt-revision-a"].disabled, true);
  assert.equal(ui.elements["prompt-revision-b"].disabled, true);
});

test("succeeded chain keeps read-only polling until closeout unlocks the next input", async () => {
  const closing = {
    ...replyView(), can_submit: false, available_actions: ["retry_publish"],
    nodes: [
      { label: "A", run_id: "a-run", status: "succeeded" },
      { label: "B", run_id: "b-run", status: "succeeded" }
    ]
  };
  const ui = await setup(null, { initialView: closing });
  assert.equal(ui.elements.submit.disabled, true);
  assert.equal(ui.elements["retry-closeout"].hidden, false);
  assert.equal(ui.timers.size, 1);
  assert.equal([...ui.timerDelays.values()][0], 1500);
  ui.setView({ ...closing, can_submit: true, available_actions: [] });
  await [...ui.timers.values()][0]();
  assert.equal(ui.elements.submit.disabled, false);
  assert.equal(ui.elements["retry-closeout"].hidden, true);
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.requests.filter(({ options }) => options.method === "POST").length, 0);
});

test("terminal read polling stops for pending input, paused, failed, and recovery states", async () => {
  const cases = [
    { pending_input_id: "pending-input" },
    ...["paused", "failed", "recovery_unavailable"].map((status) => ({ nodes: [{ status }] })),
    ...["paused", "failed", "recovery_unavailable", "closed"].map((status) => ({
      chains: [{ chain_run_id: "c1", status }]
    }))
  ];
  for (const changed of cases) {
    const ui = await setup(null, { initialView: { ...replyView(), can_submit: false, ...changed } });
    assert.equal(ui.timers.size, 0);
    assert.equal(ui.requests.filter(({ options }) => options.method === "POST").length, 0);
  }
});

test("interrupt and same-run resume send only the three control fields", async () => {
  const ui = await setup(async (attempt, post, setView) => {
    setView(attempt === 1
      ? sessionView({ status: "pausing", action: "", revision: 4, runRevision: 8 })
      : sessionView({ status: "running", action: "interrupt", revision: 6, runRevision: 10 }));
    return response(202, {
      chain_run_id: "c1", run_id: "r1", status: attempt === 1 ? "pausing" : "running"
    });
  });
  assert.equal(ui.elements["run-control"].textContent, "中断运行");
  assert.equal(ui.elements["run-control"].hidden, false);
  await ui.click();
  assert.equal(ui.posts[0].url, "/api/sessions/s1/runs/r1/interrupt");
  assert.deepEqual(ui.posts[0].body, {
    idempotency_key: "key-1", expected_session_revision: 3, expected_run_revision: 7
  });
  assert.equal(ui.elements["run-status"].textContent, "正在暂停…");
  assert.equal(ui.elements["run-control"].hidden, true);
  assert.equal(ui.timers.size, 1);

  ui.setView(sessionView({ status: "paused", action: "resume", revision: 5, runRevision: 9 }));
  await ui.refresh();
  assert.equal(ui.elements["run-status"].textContent, "已暂停");
  assert.equal(ui.elements["run-control"].textContent, "继续运行");
  assert.equal(ui.timers.size, 0);
  await ui.click();
  assert.equal(ui.posts[1].url, "/api/sessions/s1/runs/r1/resume");
  assert.deepEqual(ui.posts[1].body, {
    idempotency_key: "key-2", expected_session_revision: 5, expected_run_revision: 9
  });
});

test("failed command retry reuses its idempotency key and revisions", async () => {
  const ui = await setup(async (attempt, post, setView) => {
    if (attempt === 1) throw new TypeError("Connection lost");
    setView(sessionView({ status: "paused", action: "resume", revision: 4, runRevision: 8 }));
    return response(202, { chain_run_id: "c1", run_id: "r1", status: "paused" });
  });
  await ui.click();
  assert.equal(ui.elements.error.textContent, "Connection lost");
  assert.equal(ui.elements["run-control"].disabled, false);
  await ui.click();
  assert.deepEqual(ui.posts[1].body, ui.posts[0].body);
  assert.equal(ui.posts[1].body.idempotency_key, "key-1");
});

test("409 refreshes revisions and starts a new command; no reroll control", async () => {
  const ui = await setup(async (attempt, post, setView) => {
    if (attempt === 1) {
      setView(sessionView({ revision: 4, runRevision: 8 }));
      return response(409, { error: { message: "Conflict" } });
    }
    setView(sessionView({ status: "pausing", action: "", revision: 5, runRevision: 9 }));
    return response(202, { chain_run_id: "c1", run_id: "r1", status: "pausing" });
  });
  await ui.click();
  assert.equal(ui.elements["run-control"].hidden, false);
  assert.match(ui.elements.error.textContent, /已刷新/);
  await ui.click();
  assert.deepEqual(ui.posts[1].body, {
    idempotency_key: "key-2", expected_session_revision: 4, expected_run_revision: 8
  });
  ui.setView(sessionView({ status: "paused", action: "reroll", revision: 6, runRevision: 10 }));
  await ui.refresh();
  assert.equal(ui.elements["run-control"].hidden, true);
  assert.equal(ui.timers.size, 0);
});

function closeoutView(action, revision = 3) {
  const view = sessionView({ status: "succeeded", action: "", revision });
  view.error = { code: "STORAGE_OR_COORDINATION_FAILED", message: "收束失败" };
  view.available_actions = [action];
  view.nodes[0].run_id = "a-run";
  view.nodes[1] = {
    label: "B / Revision", status: action === "retry_archive" ? "final_ready" : "succeeded",
    run_id: "b-run", revision: 8
  };
  view.chains = [{ chain_run_id: "c1", status: "running" }];
  return view;
}

test("closeout controls target the accepted B result with session CAS", async () => {
  for (const [action, suffix, label] of [
    ["retry_archive", "retry-archive", "重试存档"],
    ["retry_publish", "retry-publish", "重试发布"]
  ]) {
    const ui = await setup(null, {
      initialView: closeoutView(action),
      onCloseout: async (attempt, post, setView) => {
        setView({ ...replyView(), revision: 4 });
        return response(200, { run_id: "b-run", status: "succeeded" });
      }
    });
    const button = ui.elements["retry-closeout"];
    assert.equal(button.hidden, false);
    assert.equal(button.textContent, label);
    assert.equal(ui.elements.submit.disabled, true);
    await button.dispatch("click");
    assert.equal(ui.closeoutPosts[0].url, `/api/sessions/s1/runs/b-run/${suffix}`);
    assert.deepEqual(ui.closeoutPosts[0].body, {
      idempotency_key: "key-1", expected_session_revision: 3
    });
    assert.equal(button.hidden, true);
    assert.equal(ui.elements.error.hidden, true);
  }
});

test("closeout retries keep the command after network failure and renew it after 409", async () => {
  const ui = await setup(null, {
    initialView: closeoutView("retry_publish"),
    onCloseout: async (attempt, post, setView) => {
      if (attempt === 1) throw new TypeError("Connection lost");
      if (attempt === 2) {
        setView(closeoutView("retry_publish", 4));
        return response(409, { error: { message: "Conflict" } });
      }
      setView(replyView({ revision: 5 }));
      return response(200, { run_id: "b-run", status: "succeeded" });
    }
  });
  const button = ui.elements["retry-closeout"];
  await button.dispatch("click");
  assert.equal(ui.elements.error.textContent, "Connection lost");
  assert.equal(button.disabled, false);
  await button.dispatch("click");
  assert.deepEqual(ui.closeoutPosts[1].body, ui.closeoutPosts[0].body);
  assert.match(ui.elements.error.textContent, /已刷新/);
  await button.dispatch("click");
  assert.deepEqual(ui.closeoutPosts[2].body, {
    idempotency_key: "key-2", expected_session_revision: 4
  });
  assert.equal(button.hidden, true);
});

function pendingInputView(revision = 3, inputId = "input-1") {
  return {
    ...sessionView({ action: "", revision }),
    pending_input_id: inputId,
    messages: [{ visible_message_id: "user-1", role: "user", payload: "分叉输入", sequence: 1 }],
    nodes: [], chains: [], available_actions: ["continue_pending_input"]
  };
}

test("pending fork input starts a new chain, never a same-run resume", async () => {
  const ui = await setup(null, {
    initialView: pendingInputView(),
    onPending: async (attempt, post, setView) => {
      setView({
        ...pendingInputView(4, null),
        available_actions: ["interrupt"],
        chains: [{ chain_run_id: "new-chain", status: "prepared" }]
      });
      return response(202, { chain_run_id: "new-chain", status: "prepared" });
    }
  });
  const button = ui.elements["start-pending"];
  assert.equal(button.hidden, false);
  assert.equal(ui.elements["run-control"].hidden, true);
  assert.equal(ui.elements.submit.disabled, true);
  await button.dispatch("click");
  assert.equal(ui.pendingPosts[0].url, "/api/sessions/s1/pending-inputs/input-1/continue");
  assert.deepEqual(ui.pendingPosts[0].body, {
    idempotency_key: "key-1", expected_session_revision: 3
  });
  assert.equal(ui.posts.length, 0);
  assert.equal(button.hidden, true);
  assert.equal(ui.elements.progress.textContent, "工作流进行中…");
  assert.equal(ui.timers.size, 1);
});

test("pending start requires both action and input identity", async () => {
  const ui = await setup(null, { initialView: { ...pendingInputView(), available_actions: [] } });
  assert.equal(ui.elements["start-pending"].hidden, true);
  ui.setView(pendingInputView(4, null));
  await ui.refresh();
  assert.equal(ui.elements["start-pending"].hidden, true);
  await ui.elements["start-pending"].dispatch("click");
  assert.equal(ui.pendingPosts.length, 0);
});

test("pending start retains idempotency on network retry and refreshes after 409", async () => {
  const ui = await setup(null, {
    initialView: pendingInputView(),
    onPending: async (attempt, post, setView) => {
      if (attempt === 1) throw new TypeError("Connection lost");
      if (attempt === 2) {
        setView(pendingInputView(4, "input-2"));
        return response(409, { error: { message: "Conflict" } });
      }
      setView({
        ...pendingInputView(5, null), available_actions: [],
        chains: [{ chain_run_id: "new-chain", status: "prepared" }]
      });
      return response(202, { chain_run_id: "new-chain", status: "prepared" });
    }
  });
  const button = ui.elements["start-pending"];
  await button.dispatch("click");
  assert.equal(ui.elements.error.textContent, "Connection lost");
  await button.dispatch("click");
  assert.deepEqual(ui.pendingPosts[1].body, ui.pendingPosts[0].body);
  assert.match(ui.elements.error.textContent, /已刷新/);
  await button.dispatch("click");
  assert.equal(ui.pendingPosts[2].url, "/api/sessions/s1/pending-inputs/input-2/continue");
  assert.deepEqual(ui.pendingPosts[2].body, {
    idempotency_key: "key-2", expected_session_revision: 4
  });
  assert.equal(button.hidden, true);
});

test("reroll sends four CAS fields, retains old candidate, and explicit selection changes Head", async () => {
  const oldCandidate = {
    candidate_id: "candidate-1", chain_run_id: "c1", payload: "原回复", selected: true
  };
  const newCandidate = {
    candidate_id: "candidate-2", chain_run_id: "c2", payload: "新回复", selected: true
  };
  const ui = await setup(null, {
    initialView: replyView(),
    onVersion: async (attempt, post, setView) => {
      if (attempt === 1) {
        setView({
          ...replyView({ canReroll: false }),
          chains: [{ chain_run_id: "c1", status: "succeeded" },
            { chain_run_id: "c2", status: "running" }]
        });
        return response(202, { chain_run_id: "c2", status: "prepared" });
      }
      setView(replyView({
        revision: 5, refRevision: 4, headCommitId: "head-1",
        candidates: [{ ...oldCandidate, selected: true }, { ...newCandidate, selected: false }]
      }));
      return response(200, { candidate_id: "candidate-1", head_commit_id: "head-1" });
    }
  });
  assert.equal(byClass(ui, "reroll")[0].disabled, false);
  await byClass(ui, "reroll")[0].dispatch("click");
  assert.equal(ui.versionPosts[0].url, "/api/sessions/s1/chains/c1/reroll");
  assert.deepEqual(ui.versionPosts[0].body, {
    idempotency_key: "key-1",
    expected_session_revision: 3,
    expected_ref_revision: 2,
    expected_head_commit_id: "head-1"
  });
  assert.equal(byClass(ui, "reroll")[0].disabled, true);
  assert.equal(byClass(ui, "candidate-preview")[0].hidden, true);

  ui.setView({
    ...replyView({
    revision: 4, refRevision: 3, headCommitId: "head-2",
    candidates: [{ ...oldCandidate, selected: false }, newCandidate]
    }),
    chains: [
      { chain_run_id: "c1", status: "succeeded" },
      { chain_run_id: "c2", status: "succeeded" }
    ]
  });
  await ui.refresh();
  const chooser = byClass(ui, "reply-controls")[0].children.find((child) => child.tagName === "select");
  assert.equal(chooser.options.length, 2);
  assert.equal(chooser.value, "candidate-2");
  chooser.value = "candidate-1";
  chooser.dispatch("change");
  assert.equal(byClass(ui, "candidate-preview")[0].hidden, false);
  assert.equal(byClass(ui, "candidate-preview")[0].children[1].textContent, "原回复");
  assert.equal(ui.versionPosts.length, 1);
  await byClass(ui, "select-candidate")[0].dispatch("click");
  assert.equal(ui.versionPosts[1].url, "/api/sessions/s1/candidates/candidate-1/select");
  assert.deepEqual(ui.versionPosts[1].body, {
    idempotency_key: "key-2",
    expected_session_revision: 4,
    expected_ref_revision: 3,
    expected_head_commit_id: "head-2"
  });
  assert.equal(byClass(ui, "reply-controls")[0].children.find((child) => child.tagName === "select").options.length, 2);
  assert.equal(byClass(ui, "assistant")[0].children[1].textContent, "原回复");
});

test("version command retry preserves its key; 409 refreshes CAS before the next attempt", async () => {
  const ui = await setup(null, {
    initialView: replyView(),
    onVersion: async (attempt, post, setView) => {
      if (attempt === 1) throw new TypeError("Connection lost");
      if (attempt === 2) {
        setView(replyView({ revision: 4, refRevision: 3, headCommitId: "head-2" }));
        return response(409, { error: { message: "Conflict" } });
      }
      setView(replyView({ canReroll: false, chainStatus: "running" }));
      return response(202, { chain_run_id: "c2", status: "prepared" });
    }
  });
  await byClass(ui, "reroll")[0].dispatch("click");
  assert.equal(ui.elements.error.textContent, "Connection lost");
  await byClass(ui, "reroll")[0].dispatch("click");
  assert.deepEqual(ui.versionPosts[1].body, ui.versionPosts[0].body);
  assert.match(ui.elements.error.textContent, /已刷新/);
  await byClass(ui, "reroll")[0].dispatch("click");
  assert.deepEqual(ui.versionPosts[2].body, {
    idempotency_key: "key-2",
    expected_session_revision: 4,
    expected_ref_revision: 3,
    expected_head_commit_id: "head-2"
  });
});

test("explicit candidate selection retries with its original key and keeps preview visible", async () => {
  const candidates = [
    { candidate_id: "candidate-1", chain_run_id: "c1", payload: "原回复", selected: true },
    { candidate_id: "candidate-2", chain_run_id: "c2", payload: "备选回复", selected: false }
  ];
  const ui = await setup(null, {
    initialView: replyView({ candidates }),
    onVersion: async (attempt, post, setView) => {
      if (attempt === 1) throw new TypeError("Connection lost");
      setView(replyView({
        revision: 4, refRevision: 3, headCommitId: "head-2",
        candidates: candidates.map((candidate) => ({
          ...candidate, selected: candidate.candidate_id === "candidate-2"
        }))
      }));
      return response(200, { candidate_id: "candidate-2", head_commit_id: "head-2" });
    }
  });
  const chooser = byClass(ui, "reply-controls")[0].children.find((child) => child.tagName === "select");
  chooser.value = "candidate-2";
  chooser.dispatch("change");
  await byClass(ui, "select-candidate")[0].dispatch("click");
  assert.equal(ui.elements.error.textContent, "Connection lost");
  assert.equal(byClass(ui, "candidate-preview")[0].hidden, false);
  await byClass(ui, "select-candidate")[0].dispatch("click");
  assert.equal(ui.versionPosts[0].url, "/api/sessions/s1/candidates/candidate-2/select");
  assert.deepEqual(ui.versionPosts[1].body, ui.versionPosts[0].body);
  assert.equal(ui.versionPosts[1].body.idempotency_key, "key-1");
  assert.equal(byClass(ui, "candidate-preview")[0].hidden, true);
  assert.equal(byClass(ui, "assistant")[0].children[1].textContent, "备选回复");
});

test("historical candidate can be previewed but cannot be selected or rerolled", async () => {
  const ui = await setup(null, {
    initialView: replyView({
      latestUser: true,
      candidates: [
        { candidate_id: "candidate-1", chain_run_id: "c1", payload: "原回复", selected: true },
        { candidate_id: "candidate-2", chain_run_id: "c2", payload: "备选回复", selected: false }
      ]
    })
  });
  const chooser = byClass(ui, "reply-controls")[0].children.find((child) => child.tagName === "select");
  chooser.value = "candidate-2";
  chooser.dispatch("change");
  assert.equal(byClass(ui, "candidate-preview")[0].children[1].textContent, "备选回复");
  assert.equal(byClass(ui, "select-candidate")[0].hidden, true);
  assert.equal(byClass(ui, "reroll").length, 0);
  assert.equal(ui.versionPosts.length, 0);
});

test("server selection gate keeps failed, paused, and unpublished sessions read-only", async () => {
  const candidates = [
    { candidate_id: "candidate-1", chain_run_id: "c1", payload: "原回复", selected: true },
    { candidate_id: "candidate-2", chain_run_id: "c2", payload: "备选回复", selected: false }
  ];
  const base = replyView({ candidates, canSelect: false });
  const cases = [
    { ...base, chains: [{ chain_run_id: "c1", status: "succeeded" },
      { chain_run_id: "c2", status: "failed" }] },
    { ...base, chains: [{ chain_run_id: "c1", status: "succeeded" },
      { chain_run_id: "c2", status: "paused" }] },
    { ...base, available_actions: ["retry_archive"] },
    { ...base, available_actions: ["retry_publish"] },
    { ...base, error: { code: "RUN_FAILED", message: "失败" } },
    { ...base, chains: [] }
  ];
  for (const view of cases) {
    const ui = await setup(null, { initialView: view });
    const chooser = byClass(ui, "reply-controls")[0].children.find((child) => child.tagName === "select");
    chooser.value = "candidate-2";
    chooser.dispatch("change");
    assert.equal(byClass(ui, "candidate-preview")[0].children[1].textContent, "备选回复");
    const selectButton = byClass(ui, "select-candidate")[0];
    assert.equal(selectButton.disabled, true);
    await selectButton.dispatch("click");
    assert.equal(ui.versionPosts.length, 0);
  }
});

test("fresh assistant fork child with no local chains can select a frozen candidate", async () => {
  const candidates = [
    { candidate_id: "candidate-1", chain_run_id: "c1", payload: "原回复", selected: true },
    { candidate_id: "candidate-2", chain_run_id: "c2", payload: "备选回复", selected: false }
  ];
  const ui = await setup(null, {
    initialView: { ...replyView({ candidates, canReroll: false }), chains: [] },
    onVersion: async (attempt, post, setView) => {
      setView({
        ...replyView({
          revision: 4, refRevision: 3, headCommitId: "head-2",
          candidates: candidates.map((candidate) => ({
            ...candidate, selected: candidate.candidate_id === "candidate-2"
          }))
        }),
        chains: []
      });
      return response(200, { candidate_id: "candidate-2", head_commit_id: "head-2" });
    }
  });
  const chooser = byClass(ui, "reply-controls")[0].children.find((child) => child.tagName === "select");
  chooser.value = "candidate-2";
  chooser.dispatch("change");
  const button = byClass(ui, "select-candidate")[0];
  assert.equal(button.disabled, false);
  await button.dispatch("click");
  assert.equal(ui.versionPosts[0].url, "/api/sessions/s1/candidates/candidate-2/select");
  assert.equal(byClass(ui, "assistant")[0].children[1].textContent, "备选回复");
});

function historicalReplyView() {
  const view = replyView({
    candidates: [
      { candidate_id: "candidate-1", chain_run_id: "c1", payload: "原回复", selected: true },
      { candidate_id: "candidate-2", chain_run_id: "c2", payload: "备选回复", selected: false }
    ]
  });
  view.messages.push(
    { visible_message_id: "user-2", role: "user", payload: "后续问题", sequence: 3, chain_run_id: "c3" },
    {
      visible_message_id: "reply-2", role: "assistant", payload: "后续回复",
      sequence: 4, chain_run_id: "c3",
      reply_candidates: [
        { candidate_id: "candidate-3", chain_run_id: "c3", payload: "后续回复", selected: true }
      ]
    }
  );
  view.chains.push({ chain_run_id: "c3", status: "succeeded" });
  return view;
}

test("historical selected or previewed candidate forks and switches to the child", async () => {
  for (const candidateId of ["candidate-1", "candidate-2"]) {
    const ui = await setup(null, {
      initialView: historicalReplyView(),
      onBranch: async (attempt, post, controls) => {
        controls.addChild({
          ...replyView({ canReroll: false }),
          workflow_session_id: "s2",
          chains: []
        });
        return response(201, {
          workflow_session_id: "s2", active_workflow_session_id: "s2", status: "created"
        });
      }
    });
    const chooser = byClass(ui, "reply-controls")[0].children.find((child) => child.tagName === "select");
    if (candidateId === "candidate-2") {
      chooser.value = candidateId;
      chooser.dispatch("change");
      assert.equal(byClass(ui, "candidate-preview")[0].children[1].textContent, "备选回复");
    }
    assert.equal(byClass(ui, "select-candidate")[0].hidden, true);
    await byClass(ui, "fork-candidate")[0].dispatch("click");
    assert.equal(ui.branchPosts.length, 1);
    assert.equal(ui.branchPosts[0].url, "/api/sessions/s1/branches/switch");
    assert.deepEqual(ui.branchPosts[0].body, {
      visible_message_id: "reply-1",
      idempotency_key: "key-1",
      expected_source_revision: 3,
      expected_selection_revision: 1,
      candidate_id: candidateId
    });
    assert.equal(ui.elements.sessions.value, "s2");
    assert.equal(ui.elements.messages.children.length, 2);
  }
});

test("branch network retry keeps its key, then 409 refreshes source revision", async () => {
  const ui = await setup(null, {
    initialView: historicalReplyView(),
    onBranch: async (attempt, post, controls) => {
      if (attempt === 1) throw new TypeError("Connection lost");
      if (attempt === 2) {
        controls.setView({ ...historicalReplyView(), revision: 4 });
        return response(409, { error: { message: "Conflict" } });
      }
      controls.addChild({ ...replyView(), workflow_session_id: "s2", chains: [] });
      return response(201, { workflow_session_id: "s2", status: "created" });
    }
  });
  await byClass(ui, "fork-candidate")[0].dispatch("click");
  assert.equal(ui.elements.error.textContent, "Connection lost");
  await byClass(ui, "fork-candidate")[0].dispatch("click");
  assert.deepEqual(ui.branchPosts[1].body, ui.branchPosts[0].body);
  assert.match(ui.elements.error.textContent, /已刷新/);
  await byClass(ui, "fork-candidate")[0].dispatch("click");
  assert.deepEqual(ui.branchPosts[2].body, {
    visible_message_id: "reply-1",
    idempotency_key: "key-2",
    expected_source_revision: 4,
    expected_selection_revision: 1,
    candidate_id: "candidate-1"
  });
  assert.equal(ui.elements.sessions.value, "s2");
});

test("manual session switch persists a separate revision and survives reload", async () => {
  const ui = await setup(null);
  ui.addSession("s2");
  await ui.loadSessions();
  ui.elements.sessions.value = "s2";
  await ui.elements.sessions.dispatch("change");
  assert.deepEqual(ui.sessionPosts[0].body, {
    workflow_session_id: "s2",
    expected_selection_revision: 1,
    idempotency_key: "key-1"
  });
  assert.equal(ui.elements.sessions.value, "s2");
  await ui.loadSessions();
  assert.equal(ui.elements.sessions.value, "s2");
});

test("session switch reuses key after network failure and refreshes after 409", async () => {
  const ui = await setup(null, {
    onSessionSwitch: async (attempt, post, setSelection) => {
      if (attempt === 1) throw new TypeError("Connection lost");
      if (attempt === 2) {
        setSelection({ active_workflow_session_id: "s1", revision: 2 });
        return response(409, { error: { message: "Conflict" } });
      }
      return response(200, { active_workflow_session_id: "s2", revision: 3 });
    }
  });
  ui.addSession("s2");
  await ui.loadSessions();
  ui.elements.sessions.value = "s2";
  await ui.elements.sessions.dispatch("change");
  assert.equal(ui.elements.sessions.value, "s1");
  ui.elements.sessions.value = "s2";
  await ui.elements.sessions.dispatch("change");
  assert.deepEqual(ui.sessionPosts[1].body, ui.sessionPosts[0].body);
  assert.match(ui.elements.error.textContent, /已刷新/);
  ui.elements.sessions.value = "s2";
  await ui.elements.sessions.dispatch("change");
  assert.deepEqual(ui.sessionPosts[2].body, {
    workflow_session_id: "s2",
    expected_selection_revision: 2,
    idempotency_key: "key-2"
  });
  assert.equal(ui.elements.sessions.value, "s2");
});

test("candidate fork remains disabled while any source chain is unfinished", async () => {
  for (const status of ["failed", "paused", "running"]) {
    const view = historicalReplyView();
    view.chains[1].status = status;
    const ui = await setup(null, { initialView: view });
    const button = byClass(ui, "fork-candidate")[0];
    assert.equal(button.disabled, true);
    await button.dispatch("click");
    assert.equal(ui.branchPosts.length, 0);
  }
});

test("completed replacement remains forkable when its older chain is superseded", async () => {
  const ui = await setup(null, {
    initialView: {
      ...replyView(),
      chains: [
        { chain_run_id: "c0", status: "superseded" },
        { chain_run_id: "c1", status: "succeeded" }
      ]
    }
  });
  assert.equal(byClass(ui, "fork-candidate")[0].disabled, false);
});

test("paused latest input offers a distinct whole-chain reroll only when the service allows it", async () => {
  const paused = {
    ...sessionView({ status: "paused", action: "resume" }),
    can_reroll: true,
    ref_revision: 2,
    head_commit_id: "head-1",
    messages: [{
      visible_message_id: "user-1", role: "user", payload: "待完成输入",
      sequence: 1, chain_run_id: "c1"
    }]
  };
  const ui = await setup(null, {
    initialView: paused,
    onVersion: async (attempt, post, setView) => {
      setView({
        ...paused, can_reroll: false, available_actions: ["interrupt"],
        chains: [
          { chain_run_id: "c1", status: "superseded" },
          { chain_run_id: "c2", status: "running" }
        ]
      });
      return response(202, { chain_run_id: "c2", status: "prepared" });
    }
  });
  assert.equal(ui.elements["run-control"].textContent, "继续运行");
  assert.equal(ui.elements["reroll-interrupted"].hidden, false);
  await ui.elements["reroll-interrupted"].dispatch("click");
  assert.equal(ui.versionPosts[0].url, "/api/sessions/s1/chains/c1/reroll");
  assert.deepEqual(ui.versionPosts[0].body, {
    idempotency_key: "key-1",
    expected_session_revision: 3,
    expected_ref_revision: 2,
    expected_head_commit_id: "head-1"
  });
  assert.equal(ui.elements["reroll-interrupted"].hidden, true);

  ui.setView({ ...paused, can_reroll: false });
  await ui.refresh();
  assert.equal(ui.elements["reroll-interrupted"].hidden, true);
});

function recoveryView({
  status = "recovery_unavailable", revision = 3, refRevision = 2,
  headCommitId = "head-1", chainId = "failed-chain", canContinue = true
} = {}) {
  const closed = status === "closed";
  return {
    ...replyView({ revision, refRevision, headCommitId, canReroll: closed }),
    can_submit: closed, can_select_candidates: closed,
    recovery_chain_run_id: chainId,
    can_close_execution: !closed,
    can_continue_workflow: closed && canContinue,
    available_actions: closed ? [...(canContinue ? ["continue_workflow"] : []), "reroll"]
      : ["close_execution"],
    error: closed ? null : { code: "RECOVERY_UNAVAILABLE", message: "执行现场无法恢复" },
    chains: [{ chain_run_id: "c1", status: "succeeded" }, { chain_run_id: chainId, status }],
    nodes: [],
    messages: [{
      visible_message_id: "user-2", role: "user", payload: "待完成输入",
      sequence: 3, chain_run_id: "original-input-chain"
    }]
  };
}

test("lost execution is closed explicitly before a new workflow can start", async () => {
  const ui = await setup(null, {
    initialView: recoveryView(),
    onRecovery: async (attempt, post, setView) => {
      if (attempt === 1) {
        setView(recoveryView({ status: "closed", revision: 4 }));
        return response(200, { chain_run_id: "failed-chain", status: "closed" });
      }
      setView({
        ...recoveryView({ status: "closed", revision: 5 }),
        can_continue_workflow: false, can_reroll: false, can_submit: false,
        available_actions: ["interrupt"],
        chains: [
          { chain_run_id: "failed-chain", status: "closed" },
          { chain_run_id: "new-chain", status: "running" }
        ]
      });
      return response(202, { chain_run_id: "new-chain", status: "prepared" });
    }
  });
  assert.equal(ui.recoveryPosts.length, 0);
  assert.equal(ui.elements["close-execution"].hidden, false);
  assert.equal(ui.elements["continue-workflow"].hidden, true);
  assert.equal(ui.elements["run-control"].hidden, true);
  assert.equal(ui.elements["start-pending"].hidden, true);
  assert.equal(ui.timers.size, 0);
  await ui.elements["close-execution"].dispatch("click");
  assert.equal(ui.recoveryPosts[0].url, "/api/sessions/s1/chains/failed-chain/close_execution");
  assert.deepEqual(ui.recoveryPosts[0].body, {
    idempotency_key: "key-1", expected_session_revision: 3,
    expected_ref_revision: 2, expected_head_commit_id: "head-1"
  });
  assert.equal(ui.elements["close-execution"].hidden, true);
  assert.equal(ui.elements["continue-workflow"].hidden, false);
  assert.equal(ui.elements["reroll-interrupted"].hidden, false);
  assert.equal(ui.timers.size, 0);
  await ui.elements["continue-workflow"].dispatch("click");
  assert.equal(ui.recoveryPosts[1].url, "/api/sessions/s1/chains/failed-chain/continue_workflow");
  assert.deepEqual(ui.recoveryPosts[1].body, {
    idempotency_key: "key-2", expected_session_revision: 4,
    expected_ref_revision: 2, expected_head_commit_id: "head-1"
  });
  assert.equal(ui.posts.length, 0);
  assert.equal(ui.pendingPosts.length, 0);
  assert.equal(ui.elements["continue-workflow"].hidden, true);
  assert.equal(ui.timers.size, 1);
});

test("execution recovery retry keeps key on network failure and refreshes all CAS fields on 409", async () => {
  for (const action of ["close_execution", "continue_workflow"]) {
    const status = action === "close_execution" ? "recovery_unavailable" : "closed";
    const ui = await setup(null, {
      initialView: recoveryView({ status }),
      onRecovery: async (attempt, post, setView) => {
        if (attempt === 1) throw new TypeError("Connection lost");
        if (attempt === 2) {
          setView(recoveryView({
            status, revision: 4, refRevision: 3, headCommitId: "head-2",
            chainId: "new-target"
          }));
          return response(409, { error: { message: "Conflict" } });
        }
        setView({ ...recoveryView({ status: "closed", revision: 5 }),
          can_continue_workflow: false, can_close_execution: false, available_actions: [] });
        return response(action === "close_execution" ? 200 : 202, { status: "closed" });
      }
    });
    const button = ui.elements[action === "close_execution" ? "close-execution" : "continue-workflow"];
    await button.dispatch("click");
    assert.equal(ui.elements.error.textContent, "Connection lost");
    await button.dispatch("click");
    assert.deepEqual(ui.recoveryPosts[1].body, ui.recoveryPosts[0].body);
    assert.match(ui.elements.error.textContent, /已刷新/);
    await button.dispatch("click");
    assert.equal(ui.recoveryPosts[2].url, `/api/sessions/s1/chains/new-target/${action}`);
    assert.deepEqual(ui.recoveryPosts[2].body, {
      idempotency_key: "key-2", expected_session_revision: 4,
      expected_ref_revision: 3, expected_head_commit_id: "head-2"
    });
  }
});

test("program failure forbids continuation while closed latest input can reroll its actual source chain", async () => {
  const view = recoveryView({ status: "closed", canContinue: false });
  const ui = await setup(null, {
    initialView: view,
    onVersion: async (attempt, post, setView) => {
      setView({ ...view, can_reroll: false });
      return response(202, { chain_run_id: "new-chain", status: "prepared" });
    }
  });
  assert.equal(ui.elements["continue-workflow"].hidden, true);
  await ui.elements["continue-workflow"].dispatch("click");
  assert.equal(ui.recoveryPosts.length, 0);
  assert.equal(ui.elements["reroll-interrupted"].hidden, false);
  await ui.elements["reroll-interrupted"].dispatch("click");
  assert.equal(ui.versionPosts[0].url, "/api/sessions/s1/chains/failed-chain/reroll");
});

test("closed execution does not block branching stable history", async () => {
  const ui = await setup(null, {
    initialView: {
      ...replyView(),
      chains: [{ chain_run_id: "c1", status: "succeeded" },
        { chain_run_id: "failed-chain", status: "closed" }]
    }
  });
  assert.equal(byClass(ui, "fork-candidate")[0].disabled, false);
  assert.equal(ui.elements["close-execution"].hidden, true);
  assert.equal(ui.elements["continue-workflow"].hidden, true);
});

test("stopped worker can be closed when the persisted chain is still marked running", async () => {
  const ui = await setup(null, {
    initialView: recoveryView({ status: "running" }),
    onRecovery: async (attempt, post, setView) => {
      setView(recoveryView({ status: "closed", revision: 4 }));
      return response(200, { chain_run_id: "failed-chain", status: "closed" });
    }
  });
  assert.equal(ui.elements["close-execution"].hidden, false);
  assert.equal(ui.elements["close-execution"].disabled, false);
  assert.equal(ui.elements["continue-workflow"].hidden, true);
  assert.equal(ui.timers.size, 0);
  await ui.elements["close-execution"].dispatch("click");
  assert.equal(ui.recoveryPosts[0].url, "/api/sessions/s1/chains/failed-chain/close_execution");
  assert.equal(ui.elements["continue-workflow"].hidden, false);
});

test("paused B keeps same-run resume and whole-chain restart distinct", async () => {
  const paused = {
    ...sessionView({ status: "succeeded", action: "resume" }),
    can_reroll: true, ref_revision: 2, head_commit_id: "head-1",
    chains: [{ chain_run_id: "c1", status: "paused" }],
    nodes: [
      { label: "A / Draft", run_id: "a-run", revision: 9, status: "succeeded" },
      { label: "B / Revision", run_id: "b-run", revision: 8, status: "paused" }
    ],
    messages: [{
      visible_message_id: "user-1", role: "user", payload: "待完成输入",
      sequence: 1, chain_run_id: "c1"
    }]
  };
  const ui = await setup(null, {
    initialView: paused,
    onVersion: async () => response(202, { chain_run_id: "new-chain", status: "prepared" })
  });
  assert.equal(ui.elements["run-control"].textContent, "继续运行");
  assert.equal(ui.elements["reroll-interrupted"].hidden, false);
  assert.equal(ui.elements["continue-workflow"].hidden, true);
  await ui.elements["reroll-interrupted"].dispatch("click");
  assert.equal(ui.versionPosts[0].url, "/api/sessions/s1/chains/c1/reroll");
  assert.equal(ui.posts.length, 0);
});

function budgetView({
  revision = 3, runRevision = 7, action = "extend_budget",
  requests = 8, attempts = 10, maxRequests = 8, maxAttempts = 32
} = {}) {
  const view = sessionView({ status: "failed", action, revision, runRevision });
  view.chains[0].status = "failed";
  view.error = { code: "model_request_limit_exceeded", message: "逻辑请求额度已用完" };
  view.nodes[0].budget = {
    model_requests: requests, attempts,
    max_model_requests: maxRequests, max_model_attempts: maxAttempts
  };
  return view;
}

test("eligible failed node resumes the same run rather than starting recovery", async () => {
  const ui = await setup(async (attempt, post, setView) => {
    setView(sessionView({ revision: 4, runRevision: 8 }));
    return response(202, { chain_run_id: "c1", run_id: "r1", status: "running" });
  }, { initialView: budgetView({ action: "resume", requests: 4 }) });
  assert.equal(ui.elements["run-control"].hidden, false);
  assert.equal(ui.elements["run-control"].textContent, "继续运行");
  assert.equal(ui.elements["budget-controls"].hidden, true);
  assert.equal(ui.elements["budget-a"].textContent, "逻辑请求 4/8 · 传输尝试 10/32");
  await ui.click();
  assert.equal(ui.posts[0].url, "/api/sessions/s1/runs/r1/resume");
  assert.deepEqual(ui.posts[0].body, {
    idempotency_key: "key-1", expected_session_revision: 3, expected_run_revision: 7
  });
  assert.equal(ui.recoveryPosts.length, 0);
  assert.equal(ui.budgetPosts.length, 0);
});

test("explicit budget extension sends both increments and never automatically resumes", async () => {
  const ui = await setup(null, {
    initialView: budgetView(),
    onBudget: async (attempt, post, setView) => {
      setView(budgetView({ revision: 4, runRevision: 8, maxRequests: 9, maxAttempts: 36, action: "resume" }));
      return response(200, { run_id: "r1", status: "failed" });
    }
  });
  assert.equal(ui.elements["budget-controls"].hidden, false);
  assert.equal(ui.elements["extend-budget"].disabled, false);
  assert.equal(ui.elements["run-control"].hidden, true);
  await ui.elements["extend-budget"].dispatch("click");
  assert.equal(ui.budgetPosts[0].url, "/api/sessions/s1/runs/r1/extend_budget");
  assert.deepEqual(ui.budgetPosts[0].body, {
    idempotency_key: "key-1", expected_session_revision: 3, expected_run_revision: 7,
    additional_model_requests: 1, additional_model_attempts: 4
  });
  assert.equal(ui.posts.length, 0);
  assert.equal(ui.elements["budget-controls"].hidden, true);
  assert.equal(ui.elements["run-control"].hidden, false);
  assert.equal(ui.elements["budget-a"].textContent, "逻辑请求 8/9 · 传输尝试 10/36");
  assert.equal(ui.timers.size, 0);
});

test("budget controls require server permission, run identity, and strict bounded increments", async () => {
  const ui = await setup(null, { initialView: budgetView({ action: "" }) });
  assert.equal(ui.elements["budget-controls"].hidden, true);
  await ui.elements["extend-budget"].dispatch("click");
  assert.equal(ui.budgetPosts.length, 0);
  const malformed = budgetView();
  malformed.nodes[0].run_id = null;
  ui.setView(malformed);
  await ui.refresh();
  assert.equal(ui.elements["budget-controls"].hidden, true);
  ui.setView(budgetView({ maxRequests: 64, maxAttempts: 256 }));
  await ui.refresh();
  assert.equal(ui.elements["additional-model-requests"].max, "0");
  assert.equal(ui.elements["additional-model-attempts"].max, "0");
  assert.equal(ui.elements["extend-budget"].disabled, true);
  ui.setView(budgetView());
  await ui.refresh();
  for (const [requests, attempts] of [["0", "0"], ["-1", "4"], ["1.5", "4"], ["57", "0"], ["0", "225"], ["", "4"]]) {
    ui.elements["additional-model-requests"].value = requests;
    ui.elements["additional-model-attempts"].value = attempts;
    ui.elements["additional-model-attempts"].dispatch("input");
    assert.equal(ui.elements["extend-budget"].disabled, true, `${requests}, ${attempts}`);
  }
  ui.elements["additional-model-requests"].value = "0";
  ui.elements["additional-model-attempts"].value = "4";
  ui.elements["additional-model-attempts"].dispatch("input");
  assert.equal(ui.elements["extend-budget"].disabled, false);
});

test("uncertain budget extension retains one command and locks increments until receipt or 409", async () => {
  const ui = await setup(null, {
    initialView: budgetView(),
    onBudget: async (attempt, post, setView) => {
      if (attempt === 1) throw new TypeError("Connection lost");
      if (attempt === 2) {
        setView(budgetView({ revision: 4, runRevision: 8 }));
        return response(409, { error: { message: "Conflict" } });
      }
      setView(budgetView({ revision: 5, runRevision: 9, maxRequests: 10, action: "resume" }));
      return response(200, { run_id: "r1", status: "failed" });
    }
  });
  const button = ui.elements["extend-budget"];
  await button.dispatch("click");
  assert.equal(ui.elements.error.textContent, "Connection lost");
  assert.equal(button.disabled, false);
  assert.equal(ui.elements["additional-model-requests"].disabled, true);
  assert.equal(ui.elements["additional-model-attempts"].disabled, true);
  await button.dispatch("click");
  assert.deepEqual(ui.budgetPosts[1].body, ui.budgetPosts[0].body);
  assert.match(ui.elements.error.textContent, /已刷新/);
  assert.equal(ui.elements["additional-model-requests"].disabled, false);
  ui.elements["additional-model-requests"].value = "2";
  ui.elements["additional-model-requests"].dispatch("input");
  await button.dispatch("click");
  assert.deepEqual(ui.budgetPosts[2].body, {
    idempotency_key: "key-2", expected_session_revision: 4, expected_run_revision: 8,
    additional_model_requests: 2, additional_model_attempts: 4
  });
  assert.equal(ui.posts.length, 0);
});

test("budget controls and resume stay disabled while the extension request is in flight", async () => {
  let finish;
  const view = budgetView();
  view.available_actions.push("resume");
  const ui = await setup(null, {
    initialView: view,
    onBudget: async () => new Promise((resolve) => { finish = resolve; })
  });
  const pending = ui.elements["extend-budget"].dispatch("click");
  assert.equal(ui.elements["extend-budget"].disabled, true);
  assert.equal(ui.elements["additional-model-requests"].disabled, true);
  assert.equal(ui.elements["additional-model-attempts"].disabled, true);
  assert.equal(ui.elements["run-control"].disabled, true);
  assert.equal(ui.elements.submit.disabled, true);
  await ui.click();
  assert.equal(ui.posts.length, 0);
  finish(response(200, { run_id: "r1", status: "failed" }));
  await pending;
  assert.equal(ui.elements["run-control"].disabled, false);
});

const USER_SESSION_PREFIX = "workflow-user-ui:v1:";
const HANDOFF_SOURCE = "00000000-0000-4000-8000-000000009901";
const HANDOFF_TARGET = "00000000-0000-4000-8000-000000009902";
const HANDOFF_B_CONFIG = "00000000-0000-4000-8000-000000000902";

function savedUserRecord(id, selection = null, pending = null) {
  return {
    schema_version: 1, kind: "workflow_user_session", workflow_session_id: id,
    prompt_selection: selection, pending_submission: pending
  };
}

function savedChoice(stage = "A", revision = 1) {
  return {
    schema_version: 1, kind: "workflow_prompt_selection",
    nodes: { [stage]: { config_id: PROMPT_CONFIG_ID, revision } }
  };
}

test("fixed exact choices survive consecutive inputs and reload without following catalog heads", async () => {
  const storage = new Map();
  const ui = await setup(null, { initialView: replyView(), promptCatalog: [promptRecord(2)], storage });
  ui.elements["prompt-config-a"].value = PROMPT_CONFIG_ID;
  ui.elements["prompt-config-a"].dispatch("change");
  ui.elements["prompt-revision-a"].value = "1";
  ui.elements["prompt-revision-a"].dispatch("input");
  for (const text of ["first input", "second input"]) {
    ui.elements.input.value = text;
    await ui.elements.composer.dispatch("submit");
    assert.deepEqual(ui.inputPosts.at(-1).body.prompt_selection, savedChoice());
  }
  const stored = JSON.parse(storage.get(USER_SESSION_PREFIX + "s1"));
  assert.deepEqual(stored, { ...savedUserRecord("s1", savedChoice()), schema_version: 2, model_selection: null });
  assert.equal(JSON.stringify(stored).includes("first input"), false);
  const restored = await setup(null, { initialView: replyView(), promptCatalog: [promptRecord(3)], storage });
  assert.equal(restored.elements["prompt-config-a"].value, PROMPT_CONFIG_ID);
  assert.equal(restored.elements["prompt-revision-a"].value, "1");
  assert.equal(restored.inputPosts.length, 0);
  restored.elements.input.value = "third input";
  await restored.elements.composer.dispatch("submit");
  assert.deepEqual(restored.inputPosts[0].body.prompt_selection, savedChoice());
});

test("session-specific choices are isolated and restored when switching back", async () => {
  const ui = await setup(null, { initialView: replyView(), promptCatalog: [promptRecord(2)] });
  ui.elements["prompt-config-a"].value = PROMPT_CONFIG_ID;
  ui.elements["prompt-config-a"].dispatch("change");
  ui.elements["prompt-revision-a"].value = "1";
  ui.elements["prompt-revision-a"].dispatch("change");
  ui.addSession("s2");
  await ui.loadSessions();
  ui.elements.sessions.value = "s2";
  await ui.elements.sessions.dispatch("change");
  assert.equal(ui.elements["prompt-config-a"].value, "");
  ui.elements["prompt-config-b"].value = PROMPT_CONFIG_ID;
  ui.elements["prompt-config-b"].dispatch("change");
  ui.elements.sessions.value = "s1";
  await ui.elements.sessions.dispatch("change");
  assert.equal(ui.elements["prompt-revision-a"].value, "1");
  assert.equal(ui.elements["prompt-config-b"].value, "");
  assert.deepEqual(JSON.parse(ui.storage.get(USER_SESSION_PREFIX + "s2")).prompt_selection, savedChoice("B", 2));
});

test("refresh retains an unverified exact request and never creates a new identity", async () => {
  const storage = new Map();
  const original = await setup(null, {
    initialView: replyView(), promptCatalog: [promptRecord(2)], storage,
    onSubmit: async () => { throw new TypeError("Connection lost"); }
  });
  original.elements["prompt-config-a"].value = PROMPT_CONFIG_ID;
  original.elements["prompt-config-a"].dispatch("change");
  original.elements.input.value = "unverified original input";
  await original.elements.composer.dispatch("submit");
  const body = original.inputPosts[0].body;
  const restored = await setup(null, { initialView: replyView(), promptCatalog: [promptRecord(3)], storage });
  assert.equal(restored.elements.input.value, body.text);
  assert.equal(restored.elements.input.disabled, true);
  assert.equal(restored.elements["prompt-revision-a"].disabled, true);
  assert.equal(restored.elements.submit.textContent, "核实提交");
  assert.equal(restored.inputPosts.length, 0);
  restored.elements.input.value = "replacement must not be sent";
  restored.elements.input.dispatch("input");
  assert.equal(restored.elements.input.value, body.text);
  restored.elements["prompt-revision-a"].value = "1";
  restored.elements["prompt-revision-a"].dispatch("change");
  await restored.elements.composer.dispatch("submit");
  assert.deepEqual(restored.inputPosts[0].body, body);
  assert.equal(restored.keys.length, 0);
  assert.equal(JSON.parse(storage.get(USER_SESSION_PREFIX + "s1")).pending_submission, null);
});

test("unverified submission survives session switching without leaking its input into another session", async () => {
  const ui = await setup(null, {
    initialView: replyView(),
    onSubmit: async () => { throw new TypeError("Connection lost"); }
  });
  ui.elements.input.value = "source pending input";
  await ui.elements.composer.dispatch("submit");
  ui.addSession("s2");
  await ui.loadSessions();
  ui.elements.sessions.value = "s2";
  await ui.elements.sessions.dispatch("change");
  assert.equal(ui.elements.input.value, "");
  assert.equal(ui.elements.input.disabled, false);
  ui.elements.sessions.value = "s1";
  await ui.elements.sessions.dispatch("change");
  assert.equal(ui.elements.input.value, "source pending input");
  assert.equal(ui.elements.submit.textContent, "核实提交");
  assert.equal(ui.inputPosts.length, 1);
});

test("a definitive rejection unlocks manual corrections while storage failure prevents dispatch", async () => {
  const ui = await setup(null, {
    initialView: replyView(),
    onSubmit: async (attempt) => attempt === 1
      ? response(400, { error: { message: "Rejected" } })
      : response(202, inputReceipt())
  });
  ui.elements.input.value = "rejected";
  await ui.elements.composer.dispatch("submit");
  assert.equal(ui.elements.input.disabled, false);
  assert.equal(ui.elements.submit.textContent, "提交");
  ui.elements.input.value = "corrected";
  await ui.elements.composer.dispatch("submit");
  assert.equal(ui.inputPosts[1].body.idempotency_key, "key-2");
  const unavailable = await setup(null, {
    initialView: replyView(),
    storage: { get: () => null, set: () => { throw new Error("Quota exceeded"); }, delete: () => {} }
  });
  unavailable.elements.input.value = "must not dispatch";
  await unavailable.elements.composer.dispatch("submit");
  assert.equal(unavailable.inputPosts.length, 0);
  assert.equal(unavailable.elements.submit.disabled, true);
  assert.match(unavailable.elements.error.textContent, /无法保存/);
});

test("corrupt or unsupported local records stay preserved until explicit quarantine reset", async () => {
  for (const raw of ["{corrupt", JSON.stringify({ ...savedUserRecord("s1"), schema_version: 2 })]) {
    const storage = new Map([[USER_SESSION_PREFIX + "s1", raw]]);
    const ui = await setup(null, { initialView: replyView(), storage });
    assert.equal(ui.elements.submit.disabled, true);
    assert.equal(storage.get(USER_SESSION_PREFIX + "s1"), raw);
    assert.equal(ui.elements["reset-prompt-state"].hidden, false);
    await ui.elements["reset-prompt-state"].dispatch("click");
    assert.equal(ui.elements.submit.disabled, false);
    assert.deepEqual(JSON.parse(storage.get(USER_SESSION_PREFIX + "s1")), {
      ...savedUserRecord("s1"), schema_version: 2, model_selection: null
    });
    assert.ok([...storage.entries()].some(([key, value]) => key.includes("quarantine:s1:") && value === raw));
    assert.equal(ui.inputPosts.length, 0);
  }
});

test("candidate fork inherits future exact choices without dispatching or changing parent choices", async () => {
  const ui = await setup(null, {
    initialView: replyView(), promptCatalog: [promptRecord(2)],
    onBranch: async (_attempt, _post, service) => {
      service.addChild(replyView());
      return response(201, { workflow_session_id: "s2" });
    }
  });
  ui.elements["prompt-config-a"].value = PROMPT_CONFIG_ID;
  ui.elements["prompt-config-a"].dispatch("change");
  ui.elements["prompt-revision-a"].value = "1";
  ui.elements["prompt-revision-a"].dispatch("change");
  await byClass(ui, "fork-candidate")[0].dispatch("click");
  assert.equal(ui.elements.sessions.value, "s2");
  assert.equal(ui.elements["prompt-revision-a"].value, "1");
  assert.deepEqual(JSON.parse(ui.storage.get(USER_SESSION_PREFIX + "s1")).prompt_selection, savedChoice());
  assert.deepEqual(JSON.parse(ui.storage.get(USER_SESSION_PREFIX + "s2")).prompt_selection, savedChoice());
  assert.equal(ui.inputPosts.length, 0);
});

test("workbench session-only handoff observes its existing choices using selection CAS and never runs", async () => {
  const storage = new Map([[USER_SESSION_PREFIX + HANDOFF_TARGET, JSON.stringify(
    savedUserRecord(HANDOFF_TARGET, savedChoice())
  )]]);
  const ui = await setup(null, {
    initialView: replyView(), initialSessionId: HANDOFF_SOURCE, extraSessions: [HANDOFF_TARGET],
    storage, locationSearch: `?session=${HANDOFF_TARGET}`
  });
  assert.equal(ui.elements.sessions.value, HANDOFF_TARGET);
  assert.deepEqual(ui.sessionPosts[0].body, {
    workflow_session_id: HANDOFF_TARGET, expected_selection_revision: 1, idempotency_key: "key-1"
  });
  assert.equal(ui.elements["prompt-revision-a"].value, "1");
  assert.equal(ui.inputPosts.length, 0);
  assert.equal(ui.pendingPosts.length, 0);
});

test("workbench exact handoff verifies immutable A/B revisions before saving and never follows latest", async () => {
  const ui = await setup(null, {
    initialView: replyView(), initialSessionId: HANDOFF_SOURCE, extraSessions: [HANDOFF_TARGET],
    promptCatalog: [promptRecord(9)],
    locationSearch: `?session=${HANDOFF_TARGET}&prompt_a=${PROMPT_CONFIG_ID}&prompt_a_revision=1`
      + `&prompt_b=${HANDOFF_B_CONFIG}&prompt_b_revision=4`
  });
  assert.equal(ui.elements.sessions.value, HANDOFF_TARGET);
  assert.equal(ui.elements["prompt-revision-a"].value, "1");
  assert.equal(ui.elements["prompt-revision-b"].value, "4");
  const reads = ui.requests.filter(({ url }) => url.includes("/revisions/"));
  assert.deepEqual(reads.map(({ url }) => url), [
    `/api/prompt-configs/config/${PROMPT_CONFIG_ID}/revisions/1`,
    `/api/prompt-configs/config/${HANDOFF_B_CONFIG}/revisions/4`
  ]);
  assert.ok(reads.every(({ options }) => !options.method));
  assert.equal(ui.context.location.search, `?session=${HANDOFF_TARGET}`);
  assert.equal(ui.historyWrites.length, 1);
  assert.equal(ui.inputPosts.length, 0);
});

test("workbench v2 preparation handoff preserves exact refs without executing its nodes in the user UI", async () => {
  const ui = await setup(null, {
    initialView: replyView(), initialSessionId: HANDOFF_SOURCE, extraSessions: [HANDOFF_TARGET],
    locationSearch: `?session=${HANDOFF_TARGET}&prompt_a=${PROMPT_CONFIG_ID}&prompt_a_revision=1`,
    exactConfigResponse: async (configId, revision) => response(200, {
      schema_version: 2, kind: "config", config_id: configId, revision, name: "Incremental fixture",
      inputs: [], preparation: {
        schema_version: 1, kind: "prompt_preparation_program", nodes: [],
        outputs: { prompt: null, context: null }
      }
    })
  });
  assert.equal(ui.elements.sessions.value, HANDOFF_TARGET);
  assert.equal(ui.elements["prompt-revision-a"].value, "1");
  assert.equal(ui.inputPosts.length, 0);
  assert.equal(ui.context.location.search, `?session=${HANDOFF_TARGET}`);
  assert.equal(ui.requests.filter(({ url }) => url.includes("/variables/write")).length, 0);
});

test("consumed workbench prompt refs cannot replace a later manual choice on refresh", async () => {
  const storage = new Map();
  const ui = await setup(null, {
    initialView: replyView(), initialSessionId: HANDOFF_TARGET, storage,
    promptCatalog: [promptRecord(9)], locationPathname: "/index.html", locationHash: "#messages",
    locationSearch: `?session=${HANDOFF_TARGET}&prompt_a=${PROMPT_CONFIG_ID}&prompt_a_revision=1`
  });
  assert.equal(ui.historyWrites[0].url, `/index.html?session=${HANDOFF_TARGET}#messages`);
  ui.elements["prompt-revision-a"].value = "3";
  ui.elements["prompt-revision-a"].dispatch("change");
  const restored = await setup(null, {
    initialView: replyView(), initialSessionId: HANDOFF_TARGET, storage,
    promptCatalog: [promptRecord(9)], locationSearch: ui.context.location.search
  });
  assert.equal(restored.elements["prompt-revision-a"].value, "3");
  assert.equal(restored.requests.filter(({ url }) => url.includes("/revisions/")).length, 0);
  assert.equal(restored.inputPosts.length, 0);
});

test("invalid or partial handoffs leave normal manual observation available without executing", async () => {
  for (const locationSearch of [
    "?session=invalid",
    `?session=${HANDOFF_TARGET}&prompt_a=${PROMPT_CONFIG_ID}`,
    `?session=${HANDOFF_TARGET}&prompt_a=${PROMPT_CONFIG_ID}&prompt_a_revision=latest`,
    `?session=${HANDOFF_TARGET}&config=https://untrusted.invalid/config`,
    `?session=${HANDOFF_TARGET}&session=${HANDOFF_SOURCE}`
  ]) {
    const ui = await setup(null, {
      initialView: replyView(), initialSessionId: HANDOFF_SOURCE, extraSessions: [HANDOFF_TARGET], locationSearch
    });
    assert.equal(ui.elements.sessions.value, HANDOFF_SOURCE);
    assert.equal(ui.sessionPosts.length, 0);
    assert.equal(ui.inputPosts.length, 0);
    assert.equal(ui.elements.submit.disabled, false);
    assert.match(ui.elements.error.textContent, /链接/);
  }
});

test("mismatched exact configuration response and pending request prevent handoff configuration overwrite", async () => {
  const badResponse = await setup(null, {
    initialView: replyView(), initialSessionId: HANDOFF_SOURCE, extraSessions: [HANDOFF_TARGET],
    locationSearch: `?session=${HANDOFF_TARGET}&prompt_a=${PROMPT_CONFIG_ID}&prompt_a_revision=1`,
    exactConfigResponse: async () => response(200, {
      schema_version: 1, kind: "config", config_id: PROMPT_CONFIG_ID, revision: 99
    })
  });
  assert.equal(badResponse.elements["prompt-config-a"].value, "");
  assert.match(badResponse.elements.error.textContent, /不匹配/);
  assert.equal(badResponse.historyWrites.length, 0);
  assert.equal(badResponse.inputPosts.length, 0);
  const body = { text: "pending", idempotency_key: "original-key", prompt_selection: savedChoice("B", 2) };
  const storage = new Map([[USER_SESSION_PREFIX + HANDOFF_TARGET, JSON.stringify(
    savedUserRecord(HANDOFF_TARGET, body.prompt_selection, body)
  )]]);
  const pending = await setup(null, {
    initialView: replyView(), initialSessionId: HANDOFF_SOURCE, extraSessions: [HANDOFF_TARGET], storage,
    locationSearch: `?session=${HANDOFF_TARGET}&prompt_a=${PROMPT_CONFIG_ID}&prompt_a_revision=1`
  });
  assert.match(pending.elements.error.textContent, /待核实/);
  assert.equal(pending.elements["prompt-config-a"].value, "");
  assert.equal(pending.elements["prompt-revision-b"].value, "2");
  assert.deepEqual(JSON.parse(storage.get(USER_SESSION_PREFIX + HANDOFF_TARGET)).pending_submission, body);
  assert.equal(pending.historyWrites.length, 0);
  assert.equal(pending.inputPosts.length, 0);
});

const modelChoice = (revision = 1) => ({
  schema_version: 1, kind: "workflow_model_selection", config_id: HANDOFF_B_CONFIG, revision
});

test("observed model selection enters the exact submit and uncertain replay survives reload", async () => {
  const storage = new Map();
  const selectedView = { ...replyView(), model_selection: modelChoice(2) };
  const ui = await setup(null, {
    initialView: selectedView, storage, onSubmit: async () => { throw new Error("lost reply"); }
  });
  ui.elements.input.value = "frozen model";
  await ui.elements.composer.dispatch("submit");
  const original = ui.inputPosts[0].body;
  assert.deepEqual(original.model_selection, modelChoice(2));
  const restored = await setup(null, {
    initialView: { ...replyView(), model_selection: modelChoice(9) }, storage
  });
  assert.equal(restored.inputPosts.length, 0);
  await restored.elements.composer.dispatch("submit");
  assert.deepEqual(restored.inputPosts[0].body, original);
});

test("exact model handoff verifies the immutable revision without dispatch or secret URL fields", async () => {
  const ui = await setup(null, {
    initialView: replyView(), initialSessionId: HANDOFF_SOURCE, extraSessions: [HANDOFF_TARGET],
    locationSearch: `?session=${HANDOFF_TARGET}&model_config=${HANDOFF_B_CONFIG}&model_revision=4`
  });
  assert.equal(ui.inputPosts.length, 0);
  assert.ok(ui.requests.some(({ url }) => url === `/api/model-configurations/model/${HANDOFF_B_CONFIG}/revisions/4`));
  assert.deepEqual(JSON.parse(ui.storage.get(USER_SESSION_PREFIX + HANDOFF_TARGET)).model_selection, modelChoice(4));
  assert.equal(ui.historyWrites.at(-1).url, `/?session=${HANDOFF_TARGET}`);
});

test("partial and invalid model handoffs do not overwrite a saved choice", async () => {
  for (const query of [
    `&model_config=${HANDOFF_B_CONFIG}`,
    `&model_config=${HANDOFF_B_CONFIG}&model_revision=0`,
    `&model_config=${HANDOFF_B_CONFIG}&model_revision=9007199254740992`
  ]) {
    const ui = await setup(null, {
      initialView: replyView(), initialSessionId: HANDOFF_SOURCE,
      locationSearch: `?session=${HANDOFF_SOURCE}${query}`
    });
    assert.equal(ui.inputPosts.length, 0);
    assert.match(ui.elements.error.textContent, /模型/);
  }
});
