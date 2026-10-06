"use strict";

const $ = (id) => document.getElementById(id);
const sessions = $("sessions");
const input = $("input");
const submitButton = $("submit");
const runControl = $("run-control");
const budgetControls = $("budget-controls");
const additionalRequests = $("additional-model-requests");
const additionalAttempts = $("additional-model-attempts");
const extendBudget = $("extend-budget");
const interruptedReroll = $("reroll-interrupted");
const closeExecution = $("close-execution");
const continueWorkflow = $("continue-workflow");
const retryCloseout = $("retry-closeout");
const startPending = $("start-pending");
const errorBox = $("error");
const promptChoices = [
  { stage: "A", config: $("prompt-config-a"), revision: $("prompt-revision-a") },
  { stage: "B", config: $("prompt-config-b"), revision: $("prompt-revision-b") }
];
const promptCatalog = new Map();
const userSessionRecords = new Map();
const userSessionRawRecords = new Map();
const promptRecordFailures = new Map();
const USER_SESSION_PREFIX = "workflow-user-ui:v1:";
const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SESSION_ID = /^[A-Za-z0-9_-]{1,128}$/;
let sessionItems = [];
let promptChoiceGeneration = 0;
let promptDraftSessionId = "";
let currentId = "";
let currentView = null;
let busy = false;
let controlBusy = false;
let versionBusy = false;
let pendingControl = null;
let pendingCloseout = null;
let pendingStart = null;
let pendingVersion = null;
let pendingRecovery = null;
let pendingBranch = null;
let pendingSessionSwitch = null;
let pendingCreation = null;
let activeSelection = null;
const candidatePreviews = new Map();
const pendingBudgets = new Map();
let budgetTarget = "";
let awaitedChainId = "";
let pollTimer = null;
let refreshRequestId = 0;
let exposureReadGeneration = 0;
let nodeOutputReadGeneration = 0;
const EXPOSURE_PREFIX = "workflow-user-exposure:v1:";

function validateExposureReference(value) {
  if (!exactFields(value, ["config_id", "revision"]) || !UUID4.test(value.config_id || "")
      || !Number.isSafeInteger(value.revision) || value.revision < 1)
    throw new Error("公开声明引用无效");
  return copyJson(value);
}

function exposureReference(id) {
  if (!id) return null;
  const raw = localStorage.getItem(EXPOSURE_PREFIX + id);
  if (!raw) return null;
  if (raw.length > 1024) throw new Error("公开声明引用容量无效");
  return validateExposureReference(JSON.parse(raw));
}

function clearExposureDisplay(message = "") {
  exposureReadGeneration++;
  const panel = $("public-exposures");
  if (!panel) return;
  $("public-exposure-list").replaceChildren();
  $("public-exposure-status").textContent = message;
  panel.hidden = !message;
}

function validateExposureRead(value, reference, view, sid) {
  if (!exactFields(value, ["schema_version", "kind", "config_id", "revision", "workflow_session_id",
    "session_revision", "head_commit_id", "availability", "reason_code", "registrations", "observations"])
      || value.schema_version !== 1 || value.kind !== "workflow_exposure_read"
      || value.config_id !== reference.config_id || value.revision !== reference.revision
      || value.workflow_session_id !== sid || value.session_revision !== view.revision
      || value.head_commit_id !== view.head_commit_id || !Array.isArray(value.registrations)
      || !Array.isArray(value.observations)) throw new Error("公开读取身份或版本不匹配");
  if (value.availability === "unavailable") {
    if (value.reason_code !== "declaration_stale" || value.registrations.length || value.observations.length)
      throw new Error("公开声明不可用响应无效");
    return value;
  }
  if (value.availability !== "available" || value.reason_code !== null
      || value.registrations.length > 128 || value.observations.length !== value.registrations.length)
    throw new Error("公开声明响应无效");
  const names = new Set(), identities = new Set();
  for (const declaration of value.registrations) {
    const binding = { A: "7be319b8-30bd-4674-b7bf-d1cf54a1a108", B: "7be319b8-30bd-4674-b7bf-d1cf54a1a109" };
    const allowed = declaration.kind === "state" && declaration.type === "public_agent_state"
      ? ["status", "run_id", "revision", "budget"]
      : declaration.kind === "result" && ["public_node_result", "delivered_workflow_result"].includes(declaration.type)
        ? declaration.stage === "A" ? ["result_text", "delivered_text"] : ["delivered_text"] : [];
    if (!exactFields(declaration, ["schemaVersion", "id", "workflowId", "stage", "nodeBindingId",
      "publicName", "kind", "type", "fields"]) || declaration.schemaVersion !== 1
        || !UUID4.test(declaration.id || "") || identities.has(declaration.id)
        || !(declaration.workflowId === "frontend:main-test" || UUID4.test(declaration.workflowId || ""))
        || declaration.nodeBindingId !== binding[declaration.stage]
        || typeof declaration.publicName !== "string" || !declaration.publicName.trim()
        || declaration.publicName.length > 128 || names.has(declaration.publicName)
        || !Array.isArray(declaration.fields) || !declaration.fields.length
        || new Set(declaration.fields).size !== declaration.fields.length
        || !declaration.fields.every((field) => allowed.includes(field))) throw new Error("公开字段声明无效");
    names.add(declaration.publicName);
    identities.add(declaration.id);
    const observations = value.observations.filter((row) => row.registrationId === declaration.id);
    const row = observations[0];
    if (observations.length !== 1 || !row || !exactFields(row, [
      "registrationId", "workflowId", "workflowSessionId", "nodeBindingId", "runId",
      "schemaVersion", "availability", "value", ...(Object.hasOwn(row, "reason") ? ["reason"] : [])
    ]) || row.workflowId !== declaration.workflowId || row.workflowSessionId !== sid
        || row.nodeBindingId !== declaration.nodeBindingId || row.schemaVersion !== 1
        || !(row.runId === null || UUID4.test(row.runId || ""))
        || !row.value || typeof row.value !== "object" || Array.isArray(row.value)
        || Object.keys(row.value).some((field) => !declaration.fields.includes(field)))
      throw new Error("公开观察归属或字段无效");
    if (row.availability === "unavailable") {
      if (Object.keys(row.value).length || !/^[A-Za-z0-9_]{1,80}$/.test(row.reason || ""))
        throw new Error("公开观察不可用响应无效");
    } else if (row.availability !== "available" || row.reason !== undefined || !UUID4.test(row.runId || "")
        || !Object.entries(row.value).every(([field, child]) => field === "budget"
          ? exactFields(child, ["max_model_requests", "max_model_attempts", "model_requests", "attempts"])
            && Object.values(child).every((count) => Number.isSafeInteger(count) && count >= 0)
          : field === "revision" ? Number.isSafeInteger(child) && child >= 0
            : field === "run_id" ? child === row.runId : typeof child === "string")) {
      throw new Error("公开观察值无效");
    }
  }
  return value;
}

async function refreshExposures(view) {
  clearExposureDisplay();
  const sid = currentId;
  let generation = exposureReadGeneration;
  try {
    const reference = exposureReference(sid);
    if (!reference || !$("public-exposures")) return;
    clearExposureDisplay("正在读取公开声明");
    generation = exposureReadGeneration;
    const activeGeneration = exposureReadGeneration;
    const response = await request(`/api/sessions/${encodeURIComponent(sid)}/exposures/${reference.config_id}/revisions/${reference.revision}`);
    if (sid !== currentId || view !== currentView || activeGeneration !== exposureReadGeneration) return;
    const value = validateExposureRead(response, reference, view, sid);
    $("public-exposure-status").textContent = value.availability === "unavailable"
      ? "公开声明已更新或撤销，旧引用不可用" : value.registrations.length ? `声明 r${reference.revision}` : "暂无公开条目";
    for (const declaration of value.registrations) {
      const row = value.observations.find((item) => item.registrationId === declaration.id);
      const section = document.createElement("article");
      const heading = document.createElement("h3");
      const content = document.createElement("pre");
      heading.textContent = declaration.publicName;
      content.textContent = row.availability === "available"
        ? JSON.stringify(row.value, null, 2) : `不可用 · ${row.reason}`;
      section.append(heading, content);
      $("public-exposure-list").append(section);
    }
  } catch (error) {
    if (sid === currentId && view === currentView && exposureReadGeneration === generation)
      clearExposureDisplay(error.message || "公开读取不可用");
  }
}

async function refreshNodeOutputs(view) {
  const panel = $("public-node-outputs");
  if (!panel) return;
  const generation = ++nodeOutputReadGeneration;
  const sid = currentId;
  const list = $("public-node-list");
  list.replaceChildren();
  panel.hidden = true;
  if (!sid) return;
  try {
    const value = await request(`/api/sessions/${encodeURIComponent(sid)}/outputs/public`);
    if (generation !== nodeOutputReadGeneration || sid !== currentId || view !== currentView) return;
    if (!exactFields(value, ["schema_version", "kind", "workflow_session_id", "session_revision", "chain_run_id", "nodes"])
        || value.schema_version !== 1 || value.kind !== "session_public_outputs"
        || value.workflow_session_id !== sid || value.session_revision !== view.revision
        || !(value.chain_run_id === null || UUID4.test(value.chain_run_id || ""))
        || !Array.isArray(value.nodes) || value.nodes.length > 256
        || !value.nodes.every(node => exactFields(node, [
          "schema_version", "workflow_session_id", "node_id", "run_id", "status", "outputs"
        ]) && node.schema_version === 1 && node.workflow_session_id === sid
          && typeof node.node_id === "string" && node.node_id.length <= 128
          && UUID4.test(node.run_id || "") && typeof node.status === "string"
          && node.outputs && typeof node.outputs === "object" && !Array.isArray(node.outputs)
          && Object.values(node.outputs).every(output => output && ["text", "prompt", "json"].includes(output.kind))))
      throw new Error("节点公开读取身份或结构无效");
    $("public-node-status").textContent = "";
    panel.hidden = !value.nodes.length;
    for (const node of value.nodes) {
      const section = document.createElement("article");
      const heading = document.createElement("h3");
      const content = document.createElement("pre");
      heading.textContent = `${node.node_id} · ${statusLabels[node.status] || node.status}`;
      content.textContent = JSON.stringify(node.outputs, null, 2);
      section.append(heading, content);
      list.append(section);
    }
  } catch (error) {
    if (generation !== nodeOutputReadGeneration || sid !== currentId || view !== currentView) return;
    panel.hidden = false;
    $("public-node-status").textContent = error.message || "节点公开读取不可用";
  }
}

const terminal = new Set(["succeeded", "failed", "superseded", "recovery_unavailable", "closed"]);
const statusLabels = {
  idle: "待开始", pending: "待开始", prepared: "准备中", queued: "排队中", running: "进行中",
  succeeded: "已完成", completed: "已完成", failed: "失败",
  final_ready: "正在存档", pausing: "正在暂停", paused: "已暂停",
  superseded: "已替代", recovery_unavailable: "恢复不可用", closed: "执行已结束"
};

function showError(message) {
  errorBox.textContent = message || "";
  errorBox.hidden = !message;
}

function copyJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function exactFields(value, fields) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === fields.length && fields.every((field) => Object.hasOwn(value, field));
}

function validateSavedSelection(value) {
  if (value === null) return null;
  if (!exactFields(value, ["schema_version", "kind", "nodes"]) || value.schema_version !== 1
      || value.kind !== "workflow_prompt_selection" || !value.nodes
      || typeof value.nodes !== "object" || Array.isArray(value.nodes)
      || Object.keys(value.nodes).some((stage) => !["A", "B"].includes(stage))) {
    throw new Error("提示词选择记录无效");
  }
  for (const reference of Object.values(value.nodes)) {
    if (!exactFields(reference, ["config_id", "revision"]) || typeof reference.config_id !== "string"
        || !UUID4.test(reference.config_id)
        || !Number.isSafeInteger(reference.revision) || reference.revision < 1) {
      throw new Error("提示词精确修订记录无效");
    }
  }
  return copyJson(value);
}

function emptyUserSessionRecord(id) {
  return {
    schema_version: 2, kind: "workflow_user_session", workflow_session_id: id,
    prompt_selection: null, model_selection: null, pending_submission: null
  };
}

function validateModelSelection(value) {
  if (value === null) return null;
  if (!exactFields(value, ["schema_version", "kind", "config_id", "revision"])
      || value.schema_version !== 1 || value.kind !== "workflow_model_selection"
      || typeof value.config_id !== "string" || !UUID4.test(value.config_id)
      || !Number.isSafeInteger(value.revision) || value.revision < 1) {
    throw new Error("模型精确修订记录无效");
  }
  return copyJson(value);
}

function validateUserSessionRecord(value, id) {
  if (!exactFields(value, [
    "schema_version", "kind", "workflow_session_id", "prompt_selection", "pending_submission",
    ...(value?.schema_version === 2 ? ["model_selection"] : [])
  ]) || ![1, 2].includes(value.schema_version) || value.kind !== "workflow_user_session"
      || value.workflow_session_id !== id || !SESSION_ID.test(id)) {
    throw new Error("本机会话记录版本或归属无效");
  }
  validateSavedSelection(value.prompt_selection);
  if (value.schema_version === 2) validateModelSelection(value.model_selection);
  const body = value.pending_submission;
  if (body !== null) {
    const fields = ["text", "idempotency_key",
      ...(Object.hasOwn(body, "prompt_selection") ? ["prompt_selection"] : []),
      ...(Object.hasOwn(body, "model_selection") ? ["model_selection"] : [])];
    if (!exactFields(body, fields) || typeof body.text !== "string" || !body.text.trim()
        || body.text.length > 12000 || typeof body.idempotency_key !== "string"
        || !body.idempotency_key.trim() || body.idempotency_key.length > 128) {
      throw new Error("待核实提交记录无效");
    }
    if (Object.hasOwn(body, "prompt_selection")) validateSavedSelection(body.prompt_selection);
    if (Object.hasOwn(body, "model_selection")) validateModelSelection(body.model_selection);
  }
  return copyJson({ ...value, schema_version: 2, model_selection: value.model_selection ?? null });
}

function userSessionRecord(id) {
  if (!id) return emptyUserSessionRecord("");
  if (userSessionRecords.has(id)) return userSessionRecords.get(id);
  let raw = null;
  let record = emptyUserSessionRecord(id);
  try {
    raw = localStorage.getItem(USER_SESSION_PREFIX + id);
    if (raw) {
      if (raw.length > 64 * 1024) throw new Error("本机会话记录过大");
      record = validateUserSessionRecord(JSON.parse(raw), id);
    }
  } catch (error) {
    promptRecordFailures.set(id, { raw, message: error.message || "本机会话记录无法读取" });
    showError("本机会话记录无效，提交已锁定。请检查或重置该记录。");
  }
  userSessionRawRecords.set(id, raw);
  userSessionRecords.set(id, record);
  return record;
}

function saveUserSessionRecord(id, record) {
  if (promptRecordFailures.has(id)) throw new Error("本机会话记录待处理，不能覆盖");
  const saved = validateUserSessionRecord(record, id);
  try {
    const raw = JSON.stringify(saved);
    if (localStorage.getItem(USER_SESSION_PREFIX + id) !== (userSessionRawRecords.get(id) ?? null))
      throw new Error("本机会话记录已由其他页面更新");
    localStorage.setItem(USER_SESSION_PREFIX + id, raw);
    userSessionRawRecords.set(id, raw);
  } catch (error) {
    promptRecordFailures.set(id, { raw: userSessionRawRecords.get(id) ?? null, message: "本机会话记录无法保存" });
    throw new Error("本机会话记录无法保存，未发送新请求");
  }
  userSessionRecords.set(id, saved);
  return saved;
}

function hasPendingSubmission(id = currentId) {
  return !!id && userSessionRecord(id).pending_submission !== null;
}

function rejectLegacyPending() {
  if (!(hasPendingSubmission() || Array.from(userSessionRecords.values()).some(row => row.pending_submission)
      || pendingControl || pendingCloseout || pendingStart || pendingVersion
      || pendingRecovery || pendingBranch || pendingSessionSwitch || pendingCreation || pendingBudgets.size)) return false;
  showError("旧请求结果仍待核实，已保留原请求，不能重新提交或发送其他写入。");
  return true;
}

function ownsSubmission(id, record, body) {
  return userSessionRecords.get(id) === record
    && JSON.stringify(record.pending_submission) === JSON.stringify(body)
    && localStorage.getItem(USER_SESSION_PREFIX + id) === userSessionRawRecords.get(id);
}

function requireLegacyReceipt(valid) {
  if (!valid) throw new Error("旧操作回执无法确认，原请求仍待核实。");
}

function validRunReceipt(value, id, runId, statuses, extra = []) {
  return exactFields(value, ["workflow_session_id", "chain_run_id", "run_id", "status", ...extra])
    && value.workflow_session_id === id && value.run_id === runId
    && UUID4.test(value.chain_run_id || "") && statuses.includes(value.status);
}

function validReplacementReceipt(value, id, chainId) {
  return exactFields(value, ["workflow_session_id", "source_chain_run_id", "chain_run_id", "run_id", "input_id", "status"])
    && value.workflow_session_id === id && value.source_chain_run_id === chainId
    && ["chain_run_id", "run_id", "input_id"].every(field => UUID4.test(value[field] || ""))
    && ["prepared", "running", "succeeded", "failed", "paused"].includes(value.status);
}

function applyPromptSelection(selected) {
  for (const choice of promptChoices) {
    const reference = selected?.nodes[choice.stage];
    if (reference && !Array.from(choice.config.options).some((option) => option.value === reference.config_id)) {
      choice.config.add(new Option(`${reference.config_id} · r${reference.revision}`, reference.config_id));
    }
    choice.config.value = reference?.config_id || "";
    choice.revision.value = reference ? String(reference.revision) : "";
  }
}

function persistPromptChoice() {
  if (!currentId || hasPendingSubmission() || promptRecordFailures.has(currentId)) return;
  try {
    const record = userSessionRecord(currentId);
    saveUserSessionRecord(currentId, { ...record, prompt_selection: promptSelection() });
  } catch (error) {
    showError(error.message);
  }
}

function promptSelection() {
  const nodes = {};
  for (const choice of promptChoices) {
    if (!choice.config.value) continue;
    const revision = Number(choice.revision.value);
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(choice.config.value)
        || !choice.revision.value.trim() || !Number.isSafeInteger(revision) || revision < 1) {
      throw new Error(`${choice.stage} 提示词修订无效`);
    }
    nodes[choice.stage] = { config_id: choice.config.value, revision };
  }
  return Object.keys(nodes).length ? {
    schema_version: 1, kind: "workflow_prompt_selection", nodes
  } : null;
}

function updatePromptControls(view) {
  if (currentId !== promptDraftSessionId) {
    promptDraftSessionId = currentId;
    const record = userSessionRecord(currentId);
    applyPromptSelection(record.prompt_selection);
    input.value = record.pending_submission?.text || "";
  }
  const pending = hasPendingSubmission();
  const problem = promptRecordFailures.has(currentId);
  const locked = busy || controlBusy || versionBusy || !view?.can_submit || pending || problem;
  for (const choice of promptChoices) {
    choice.config.disabled = locked;
    choice.revision.disabled = locked || !choice.config.value;
  }
  input.disabled = busy || pending || problem;
  submitButton.disabled = busy || controlBusy || versionBusy || problem || (!pending && !view?.can_submit);
  submitButton.textContent = pending ? "核实提交" : "提交";
  $("prompt-state").textContent = problem ? "本机记录待处理" : pending ? "提交待核实"
    : promptSelectionLabel();
  $("reset-prompt-state").hidden = !problem;
  $("reset-prompt-state").disabled = busy || controlBusy || versionBusy;
}

function promptSelectionLabel() {
  const selected = userSessionRecord(currentId).prompt_selection;
  if (!selected) return "默认配置";
  return promptChoices.map(({ stage }) => selected.nodes[stage]
    ? `${stage} · r${selected.nodes[stage].revision}` : `${stage} · 默认`).join(" · ");
}

async function verifyExactSelection(selected) {
  validateSavedSelection(selected);
  await Promise.all(Object.values(selected?.nodes || {}).map(async (reference) => {
    const record = await request(
      `/api/prompt-configs/config/${reference.config_id}/revisions/${reference.revision}`
    );
    if (![1, 2].includes(record?.schema_version) || record.kind !== "config"
        || record.schema_version === 2 && (record.preparation?.schema_version !== 1
          || record.preparation.kind !== "prompt_preparation_program")
        || record.config_id !== reference.config_id || record.revision !== reference.revision) {
      throw new Error("提示词精确修订响应不匹配");
    }
  }));
}

async function loadPromptConfigs() {
  const rows = await request("/api/prompt-configs/config");
  if (!Array.isArray(rows)) throw new Error("提示词配置列表无效");
  promptCatalog.clear();
  for (const row of rows) {
    const record = row?.record;
    if (typeof record?.config_id === "string" && Number.isSafeInteger(record.revision)
        && record.revision > 0 && typeof record.name === "string") {
      promptCatalog.set(record.config_id, record);
    }
  }
  for (const choice of promptChoices) {
    const selected = choice.config.value;
    const previous = Array.from(choice.config.options).find((option) => option.value === selected);
    choice.config.replaceChildren(new Option("默认", ""));
    for (const record of promptCatalog.values()) {
      choice.config.add(new Option(`${record.name} · r${record.revision}`, record.config_id));
    }
    if (selected && !promptCatalog.has(selected)) {
      choice.config.add(new Option(previous?.textContent || selected, selected));
    }
    choice.config.value = selected || "";
  }
  updatePromptControls(currentView);
}

async function request(path, options) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || "请求失败");
    error.status = response.status;
    error.code = data.error?.reason_code ?? data.error?.code;
    throw error;
  }
  return data;
}

function schedulePoll(active) {
  clearTimeout(pollTimer);
  pollTimer = active ? setTimeout(() => refreshSession(), 1500) : null;
}

function stageStatus(nodes, index, pattern) {
  const node = nodes.find((item) => pattern.test(item.label || "")) || nodes[index];
  return node?.status || "pending";
}

function setStage(id, status) {
  const element = $(id);
  element.textContent = statusLabels[status] || status;
  element.dataset.status = status;
}

function messageText(payload) {
  if (typeof payload === "string") return payload;
  if (payload && typeof payload === "object") {
    if (typeof payload.content === "string") return payload.content;
    if (typeof payload.text === "string") return payload.text;
  }
  return JSON.stringify(payload ?? "");
}

function availableRunControl(view) {
  const actions = Array.isArray(view.available_actions) ? view.available_actions : [];
  const action = actions.includes("resume") ? "resume" : actions.includes("interrupt") ? "interrupt" : "";
  const statuses = action === "interrupt" ? ["prepared", "running"] : ["paused", "failed"];
  const node = Array.isArray(view.nodes)
    ? view.nodes.find((item) => statuses.includes(item.status)
      && typeof item.run_id === "string" && Number.isInteger(item.revision))
    : null;
  return action && node ? { action, runId: node.run_id, runRevision: node.revision } : null;
}

function validBudget(budget) {
  return budget && [
    "max_model_requests", "max_model_attempts", "model_requests", "attempts"
  ].every((field) => Number.isInteger(budget[field]) && budget[field] >= 0);
}

function setBudgetCounts(id, node) {
  const element = $(id);
  const budget = node?.budget;
  element.hidden = !validBudget(budget);
  element.textContent = element.hidden ? ""
    : `逻辑请求 ${budget.model_requests}/${budget.max_model_requests} · 传输尝试 ${budget.attempts}/${budget.max_model_attempts}`;
}

function availableBudgetControl(view) {
  const actions = Array.isArray(view?.available_actions) ? view.available_actions : [];
  if (!actions.includes("extend_budget")) return null;
  const node = Array.isArray(view.nodes) ? view.nodes.find((item) => ["paused", "failed"].includes(item.status)
    && typeof item.run_id === "string" && Number.isInteger(item.revision)
    && validBudget(item.budget)) : null;
  return node ? { runId: node.run_id, runRevision: node.revision, budget: node.budget } : null;
}

function budgetIncrements(control) {
  const requests = Number(additionalRequests.value);
  const attempts = Number(additionalAttempts.value);
  return control && additionalRequests.value.trim() && additionalAttempts.value.trim()
    && Number.isInteger(requests) && Number.isInteger(attempts)
    && requests >= 0 && attempts >= 0 && requests + attempts > 0
    && requests <= 64 - control.budget.max_model_requests
    && attempts <= 256 - control.budget.max_model_attempts
    ? { requests, attempts } : null;
}

function updateBudgetControls(view) {
  const control = availableBudgetControl(view);
  const target = control ? `${currentId}:${control.runId}` : "";
  const pending = pendingBudgets.get(target);
  budgetControls.hidden = !control;
  if (control && target !== budgetTarget && !pending) {
    additionalRequests.value = String(Math.min(1, 64 - control.budget.max_model_requests));
    additionalAttempts.value = String(Math.min(4, 256 - control.budget.max_model_attempts));
  }
  budgetTarget = target;
  if (pending) {
    additionalRequests.value = String(pending.additionalRequests);
    additionalAttempts.value = String(pending.additionalAttempts);
  }
  if (control) {
    additionalRequests.max = String(64 - control.budget.max_model_requests);
    additionalAttempts.max = String(256 - control.budget.max_model_attempts);
  }
  const locked = !control || busy || controlBusy || versionBusy;
  additionalRequests.disabled = locked || !!pending;
  additionalAttempts.disabled = locked || !!pending;
  extendBudget.disabled = locked || (!pending && !budgetIncrements(control));
}

function updateRunControl(view) {
  const control = view ? availableRunControl(view) : null;
  runControl.hidden = !control;
  runControl.disabled = !control || busy || controlBusy || versionBusy;
  const lastMessage = view?.messages?.at(-1);
  const canRestart = view?.can_reroll === true && lastMessage?.role === "user"
    && typeof recoveryChainId(view) === "string" && !hasActiveChain(view);
  interruptedReroll.hidden = !canRestart;
  interruptedReroll.disabled = !canRestart || busy || controlBusy || versionBusy;
  if (control) {
    runControl.dataset.action = control.action;
    runControl.textContent = control.action === "interrupt" ? "中断运行" : "继续运行";
    runControl.title = control.action === "resume" ? "继续当前运行" : "中断当前运行";
  }
}

function recoveryChainId(view) {
  return typeof view?.recovery_chain_run_id === "string"
    ? view.recovery_chain_run_id : view?.messages?.at(-1)?.chain_run_id;
}

function availableRecovery(view, action) {
  const actions = Array.isArray(view?.available_actions) ? view.available_actions : [];
  const flag = action === "close_execution" ? view?.can_close_execution : view?.can_continue_workflow;
  const chainId = view?.recovery_chain_run_id;
  return flag === true && actions.includes(action) && typeof chainId === "string"
    && (action === "close_execution" || !hasActiveChain(view)) ? chainId : null;
}

function updateRecoveryControls(view) {
  for (const [button, action] of [
    [closeExecution, "close_execution"], [continueWorkflow, "continue_workflow"]
  ]) {
    const chainId = availableRecovery(view, action);
    button.hidden = !chainId;
    button.disabled = !chainId || busy || controlBusy || versionBusy;
  }
}

function availableCloseout(view) {
  const actions = Array.isArray(view?.available_actions) ? view.available_actions : [];
  const nodes = Array.isArray(view?.nodes) ? view.nodes : [];
  const action = actions.includes("retry_archive") ? "retry_archive"
    : actions.includes("retry_publish") ? "retry_publish" : "";
  const node = action === "retry_archive"
    ? nodes.find((item) => item.status === "final_ready" && typeof item.run_id === "string")
    : action === "retry_publish" && nodes[1]?.status === "succeeded"
      && typeof nodes[1].run_id === "string" ? nodes[1] : null;
  return node ? { action, runId: node.run_id } : null;
}

function updateCloseoutControl(view) {
  const control = availableCloseout(view);
  retryCloseout.hidden = !control;
  retryCloseout.disabled = !control || busy || controlBusy || versionBusy;
  if (control) {
    retryCloseout.textContent = control.action === "retry_archive" ? "重试存档" : "重试发布";
    retryCloseout.title = control.action === "retry_archive"
      ? "继续收束已接受的运行结果" : "发布已存档的回复";
  }
}

function availablePendingStart(view) {
  const actions = Array.isArray(view?.available_actions) ? view.available_actions : [];
  return actions.includes("continue_pending_input") && typeof view.pending_input_id === "string"
    ? view.pending_input_id : null;
}

function updatePendingStart(view) {
  const inputId = availablePendingStart(view);
  startPending.hidden = !inputId;
  startPending.disabled = !inputId || busy || controlBusy || versionBusy;
}

interruptedReroll.addEventListener("click", () => {
  const lastMessage = currentView?.messages?.at(-1);
  if (!interruptedReroll.disabled && lastMessage?.role === "user") {
    return changeReply("reroll", recoveryChainId(currentView), lastMessage.visible_message_id);
  }
});

function hasActiveChain(view) {
  const chains = Array.isArray(view.chains) ? view.chains : [];
  const awaited = chains.find((chain) => chain.chain_run_id === awaitedChainId);
  return (!!awaitedChainId && (!awaited || (!terminal.has(awaited.status) && awaited.status !== "paused")))
    || chains.some((chain) => !terminal.has(chain.status) && chain.status !== "paused");
}

function canSelectCandidate(view) {
  return view.can_select_candidates === true && !hasActiveChain(view) && !hasPendingSubmission();
}

function canForkCandidate(view) {
  const chains = Array.isArray(view.chains) ? view.chains : [];
  const actions = Array.isArray(view.available_actions) ? view.available_actions : [];
  return chains.every((chain) => ["succeeded", "superseded", "closed"].includes(chain.status))
    && !view.error && !hasActiveChain(view)
    && !hasPendingSubmission()
    && !actions.includes("retry_archive") && !actions.includes("retry_publish");
}

async function recoverExecution(action) {
  if (!currentId || !currentView || busy || controlBusy || versionBusy) return;
  if (rejectLegacyPending()) return;
  const chainId = availableRecovery(currentView, action);
  if (!chainId) return;
  const id = currentId;
  const view = currentView;
  if (!pendingRecovery || pendingRecovery.sessionId !== id
      || pendingRecovery.action !== action || pendingRecovery.chainId !== chainId) {
    pendingRecovery = {
      sessionId: id, action, chainId, idempotencyKey: crypto.randomUUID(),
      expectedSessionRevision: view.revision,
      expectedRefRevision: view.ref_revision,
      expectedHeadCommitId: view.head_commit_id
    };
  }
  const command = pendingRecovery;
  versionBusy = true;
  sessions.disabled = true;
  $("new-session").disabled = true;
  render(view);
  showError("");
  let failure = "";
  try {
    const receipt = await request(
      `/api/sessions/${encodeURIComponent(id)}/chains/${encodeURIComponent(command.chainId)}/${action}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          idempotency_key: command.idempotencyKey,
          expected_session_revision: command.expectedSessionRevision,
          expected_ref_revision: command.expectedRefRevision,
          expected_head_commit_id: command.expectedHeadCommitId
        })
      }
    );
    if (pendingRecovery !== command) return;
    requireLegacyReceipt(action === "continue_workflow"
      ? validReplacementReceipt(receipt, id, command.chainId)
      : exactFields(receipt, ["workflow_session_id", "chain_run_id", "closeout_id", "status"])
        && receipt.workflow_session_id === id && receipt.chain_run_id === command.chainId
        && UUID4.test(receipt.closeout_id || "") && receipt.status === "closed");
    pendingRecovery = null;
    if (id === currentId) {
      awaitedChainId = action === "continue_workflow" ? receipt.chain_run_id || "" : "";
      await refreshSession();
    }
  } catch (error) {
    if (error.status === 409 && error.code !== "idempotency_conflict" && pendingRecovery === command) {
      pendingRecovery = null;
      if (id === currentId) {
        await refreshSession();
        failure = "会话状态已变化，已刷新。请重试。";
      }
    } else if (id === currentId) {
      failure = error.message || "操作失败，请重试";
    }
  } finally {
    versionBusy = false;
    sessions.disabled = busy;
    $("new-session").disabled = busy;
    if (currentView) render(currentView);
    if (failure) showError(failure);
  }
}

closeExecution.addEventListener("click", () => recoverExecution("close_execution"));
continueWorkflow.addEventListener("click", () => recoverExecution("continue_workflow"));

async function forkFromCandidate(visibleMessageId, candidateId) {
  if (!currentId || !currentView || busy || controlBusy || versionBusy
      || !canForkCandidate(currentView) || !activeSelection) return;
  if (rejectLegacyPending()) return;
  const id = currentId;
  const view = currentView;
  if (!pendingBranch || pendingBranch.sessionId !== id
      || pendingBranch.visibleMessageId !== visibleMessageId
      || pendingBranch.candidateId !== candidateId) {
    pendingBranch = {
      sessionId: id, visibleMessageId, candidateId,
      idempotencyKey: crypto.randomUUID(),
      expectedSourceRevision: view.revision,
      expectedSelectionRevision: activeSelection.revision
    };
  }
  const command = pendingBranch;
  versionBusy = true;
  sessions.disabled = true;
  $("new-session").disabled = true;
  render(view);
  showError("");
  try {
    const receipt = await request(`/api/sessions/${encodeURIComponent(id)}/branches/switch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        visible_message_id: command.visibleMessageId,
        idempotency_key: command.idempotencyKey,
        expected_source_revision: command.expectedSourceRevision,
        expected_selection_revision: command.expectedSelectionRevision,
        candidate_id: command.candidateId
      })
    });
    if (pendingBranch !== command) return;
    requireLegacyReceipt(exactFields(receipt, ["workflow_session_id", "source_workflow_session_id", "visible_message_id",
      "fork_anchor_id", "role", "pending_input_id", "active_workflow_session_id", "status", "selection_revision"])
      && UUID4.test(receipt.workflow_session_id || "") && receipt.workflow_session_id !== id
      && receipt.source_workflow_session_id === id
      && receipt.visible_message_id === command.visibleMessageId && UUID4.test(receipt.fork_anchor_id || "")
      && ["user", "assistant"].includes(receipt.role) && ["pending", "created"].includes(receipt.status)
      && (receipt.pending_input_id === null || UUID4.test(receipt.pending_input_id || ""))
      && receipt.active_workflow_session_id === receipt.workflow_session_id
      && receipt.selection_revision === command.expectedSelectionRevision + 1);
    if (id === currentId) {
      const childId = receipt.workflow_session_id;
      if (typeof childId !== "string" || !SESSION_ID.test(childId)) throw new Error("分支会话响应无效");
      const childExists = userSessionRecords.has(childId) || localStorage.getItem(USER_SESSION_PREFIX + childId) !== null;
      const childRecord = userSessionRecord(childId);
      if (!childExists) saveUserSessionRecord(childId, {
        ...childRecord, prompt_selection: userSessionRecord(id).prompt_selection,
        model_selection: userSessionRecord(id).model_selection
      });
      if (localStorage.getItem(EXPOSURE_PREFIX + childId) === null) {
        const reference = exposureReference(id);
        if (reference) localStorage.setItem(EXPOSURE_PREFIX + childId, JSON.stringify(reference));
      }
      await loadSessions();
      pendingBranch = null;
    }
  } catch (error) {
    if (error.status === 409 && error.code !== "idempotency_conflict" && pendingBranch === command) {
      pendingBranch = null;
      if (id === currentId) {
        await loadSessions();
        showError("会话状态已变化，已刷新。请重试。");
      }
    } else if (id === currentId) {
      showError(error.message || "创建分支失败，请重试");
    }
  } finally {
    versionBusy = false;
    sessions.disabled = busy;
    $("new-session").disabled = busy;
    if (currentView) render(currentView);
  }
}

async function changeReply(kind, targetId, floorId) {
  if (!currentId || !currentView || busy || controlBusy || versionBusy
      || hasActiveChain(currentView) || hasPendingSubmission()) return;
  if (rejectLegacyPending()) return;
  const id = currentId;
  const view = currentView;
  if (kind === "reroll" && !view.can_reroll) return;
  if (kind === "select" && !canSelectCandidate(view)) return;
  if (!pendingVersion || pendingVersion.sessionId !== id
      || pendingVersion.kind !== kind || pendingVersion.targetId !== targetId) {
    pendingVersion = {
      sessionId: id, kind, targetId,
      idempotencyKey: crypto.randomUUID(),
      expectedSessionRevision: view.revision,
      expectedRefRevision: view.ref_revision,
      expectedHeadCommitId: view.head_commit_id
    };
  }
  const command = pendingVersion;
  const path = kind === "reroll"
    ? `/api/sessions/${encodeURIComponent(id)}/chains/${encodeURIComponent(targetId)}/reroll`
    : `/api/sessions/${encodeURIComponent(id)}/candidates/${encodeURIComponent(targetId)}/select`;
  versionBusy = true;
  sessions.disabled = true;
  $("new-session").disabled = true;
  render(view);
  showError("");
  try {
    const receipt = await request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        idempotency_key: command.idempotencyKey,
        expected_session_revision: command.expectedSessionRevision,
        expected_ref_revision: command.expectedRefRevision,
        expected_head_commit_id: command.expectedHeadCommitId
      })
    });
    if (pendingVersion !== command) return;
    requireLegacyReceipt(kind === "reroll" ? validReplacementReceipt(receipt, id, command.targetId)
      : exactFields(receipt, ["workflow_session_id", "candidate_id", "head_commit_id", "ref_revision", "session_revision", "status"])
        && receipt.workflow_session_id === id && receipt.candidate_id === command.targetId
        && UUID4.test(receipt.head_commit_id || "") && receipt.status === "succeeded"
        && receipt.ref_revision === command.expectedRefRevision + 1
        && receipt.session_revision === command.expectedSessionRevision + 1);
    pendingVersion = null;
    candidatePreviews.delete(`${id}:${floorId}`);
    if (id === currentId) {
      if (kind === "reroll") awaitedChainId = receipt.chain_run_id || "";
      await refreshSession();
    }
  } catch (error) {
    if (error.status === 409 && error.code !== "idempotency_conflict" && pendingVersion === command) {
      pendingVersion = null;
      if (id === currentId) {
        await refreshSession();
        showError("会话状态已变化，已刷新。请重试。");
      }
    } else if (id === currentId) {
      showError(error.message || "操作失败，请重试");
    }
  } finally {
    versionBusy = false;
    sessions.disabled = busy;
    $("new-session").disabled = busy;
    if (currentView) render(currentView);
  }
}

function addReplyControls(item, message, view, isLatest, floorId) {
  const candidates = Array.isArray(message.reply_candidates) ? message.reply_candidates : [];
  const controls = document.createElement("div");
  controls.className = "reply-controls";
  const locked = busy || controlBusy || versionBusy || hasActiveChain(view);
  if (candidates.length) {
    const floorKey = `${currentId}:${floorId}`;
    const selected = candidates.find((candidate) => candidate.selected);
    const previewId = candidatePreviews.get(floorKey);
    const previewed = candidates.find((candidate) => candidate.candidate_id === previewId)
      || selected || candidates[0];
    const label = document.createElement("label");
    const chooser = document.createElement("select");
    chooser.id = `candidate-${floorId}`;
    label.htmlFor = chooser.id;
    label.textContent = "候选";
    chooser.setAttribute("aria-label", "浏览回复候选");
    candidates.forEach((candidate, index) => {
      const option = document.createElement("option");
      option.value = candidate.candidate_id;
      option.textContent = `候选 ${index + 1}${candidate.selected ? "（当前选中）" : ""}`;
      chooser.append(option);
    });
    chooser.value = previewed.candidate_id;
    chooser.disabled = locked;
    const preview = document.createElement("div");
    preview.className = "candidate-preview";
    const previewHeading = document.createElement("strong");
    previewHeading.textContent = "预览（未选中）";
    const previewBody = document.createElement("div");
    preview.append(previewHeading, previewBody);
    const selectButton = document.createElement("button");
    selectButton.type = "button";
    selectButton.className = "select-candidate";
    selectButton.textContent = "选中此版本";
    selectButton.title = "将预览的候选设为当前回复";
    const forkButton = document.createElement("button");
    forkButton.type = "button";
    forkButton.className = "fork-candidate";
    forkButton.textContent = "从此分支";
    forkButton.title = "从所选候选建立并切换分支";
    forkButton.disabled = locked || !activeSelection || !canForkCandidate(view);
    const updatePreview = (remember = false) => {
      const candidate = candidates.find((row) => row.candidate_id === chooser.value);
      if (!candidate) return;
      if (remember) candidatePreviews.set(floorKey, candidate.candidate_id);
      preview.hidden = !!candidate.selected;
      previewBody.textContent = messageText(candidate.payload);
      selectButton.hidden = !isLatest || !!candidate.selected;
      selectButton.disabled = locked || !canSelectCandidate(view);
    };
    chooser.addEventListener("change", () => updatePreview(true));
    selectButton.addEventListener("click", () => {
      if (!selectButton.disabled && !selectButton.hidden)
        return changeReply("select", chooser.value, floorId);
    });
    forkButton.addEventListener("click", () => {
      if (!forkButton.disabled) return forkFromCandidate(message.visible_message_id, chooser.value);
    });
    updatePreview();
    controls.append(label, chooser, selectButton, forkButton);
    item.append(controls, preview);
  }
  if (isLatest && typeof message.chain_run_id === "string") {
    const rerollButton = document.createElement("button");
    rerollButton.type = "button";
    rerollButton.className = "reroll";
    rerollButton.textContent = "重新生成";
    rerollButton.title = "重新运行整条工作流，保留已有候选";
    rerollButton.disabled = locked || !view.can_reroll;
    rerollButton.addEventListener("click", () => {
      if (!rerollButton.disabled) return changeReply("reroll", message.chain_run_id, floorId);
    });
    controls.append(rerollButton);
  }
  if (!candidates.length && controls.children.length) item.append(controls);
}

function render(view) {
  currentView = view;
  void refreshExposures(view);
  void refreshNodeOutputs(view);
  updatePromptControls(view);
  $("mode").textContent = view.mode === "deepseek" ? "DeepSeek" : "离线测试（非真实模型）";
  const nodes = Array.isArray(view.nodes) ? view.nodes : [];
  setStage("stage-a", stageStatus(nodes, 0, /^(A|草稿)/i));
  setStage("stage-b", stageStatus(nodes, 1, /^(B|修订)/i));
  setStage("stage-output", stageStatus(nodes, 2, /输出/));
  setBudgetCounts("budget-a", nodes.find((node) => /^(A|草稿)/i.test(node.label || "")) || nodes[0]);
  setBudgetCounts("budget-b", nodes.find((node) => /^(B|修订)/i.test(node.label || "")) || nodes[1]);

  const list = $("messages");
  list.replaceChildren();
  const messages = Array.isArray(view.messages) ? [...view.messages] : [];
  messages.sort((a, b) => a.sequence - b.sequence);
  for (const [index, message] of messages.entries()) {
    const item = document.createElement("li");
    item.className = message.role === "human" || message.role === "user" ? "human" : "assistant";
    const role = document.createElement("strong");
    role.textContent = item.className === "human" ? "你" : "工作流";
    const body = document.createElement("div");
    body.textContent = messageText(message.payload);
    item.append(role, body);
    if (item.className === "assistant") {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      const json = document.createElement("pre");
      summary.textContent = "JSON";
      json.textContent = JSON.stringify(message.payload, null, 2);
      details.append(summary, json);
      item.append(details);
      addReplyControls(item, message, view, index === messages.length - 1,
        messages[index - 1]?.visible_message_id || message.visible_message_id);
    }
    list.append(item);
  }
  $("empty").hidden = messages.length > 0;
  updateRunControl(view);
  updateBudgetControls(view);
  updateCloseoutControl(view);
  updatePendingStart(view);
  updateRecoveryControls(view);
  if (view.error) showError(view.error.message || view.error.code || "执行失败");
  const chains = Array.isArray(view.chains) ? view.chains : [];
  const awaited = chains.find((chain) => chain.chain_run_id === awaitedChainId);
  const inFlight = (status) => !terminal.has(status) && status !== "paused";
  const awaiting = !!awaitedChainId && (!awaited || inFlight(awaited.status));
  if (awaited && !inFlight(awaited.status)) awaitedChainId = "";
  const active = !view.error && (awaiting || chains.some((chain) => inFlight(chain.status)));
  const awaitingCloseout = chains.at(-1)?.status === "succeeded" && !view.can_submit
    && !view.pending_input_id && !nodes.some((node) =>
      ["paused", "failed", "recovery_unavailable"].includes(node.status));
  const pausing = nodes.some((node) => node.status === "pausing");
  const paused = chains.some((chain) => chain.status === "paused");
  $("run-status").textContent = pausing ? "正在暂停…" : paused ? "已暂停" : "";
  $("progress").textContent = active ? "工作流进行中…" : "";
  let registered = false;
  try { registered = !!exposureReference(currentId); } catch { /* Invalid references remain unavailable. */ }
  schedulePoll(active || awaitingCloseout || registered);
}

async function refreshSession() {
  const id = currentId;
  if (!id) return;
  const requestId = ++refreshRequestId;
  try {
    const view = await request(`/api/sessions/${encodeURIComponent(id)}`);
    if (id === currentId && requestId === refreshRequestId) {
      if (Object.hasOwn(view, "model_selection")) {
        const selected = validateModelSelection(view.model_selection);
        const record = userSessionRecord(id);
        if (!record.pending_submission && !promptRecordFailures.has(id)
            && JSON.stringify(record.model_selection) !== JSON.stringify(selected))
          saveUserSessionRecord(id, { ...record, model_selection: selected });
      }
      render(view);
    }
  } catch (error) {
    if (id === currentId && requestId === refreshRequestId) {
      clearExposureDisplay("公开读取暂不可用");
      showError(error.message || "获取会话失败");
      schedulePoll(true);
    }
  }
}

async function loadSessions() {
  clearExposureDisplay();
  const [list, selection] = await Promise.all([
    request("/api/sessions"), request("/api/active-session")
  ]);
  activeSelection = selection;
  sessions.replaceChildren();
  const items = Array.isArray(list) ? list : [];
  sessionItems = items;
  for (const session of items) {
    const id = session.workflow_session_id;
    if (typeof id !== "string") continue;
    const option = document.createElement("option");
    option.value = id;
    option.textContent = session.label || id;
    sessions.append(option);
  }
  if (!sessions.options.length) {
    sessions.add(new Option("暂无会话", ""));
  }
  const selectedId = selection.active_workflow_session_id || "";
  currentId = items.some((item) => item.workflow_session_id === selectedId)
    ? selectedId : sessions.options[0].value;
  sessions.value = currentId;
  currentView = null;
  submitButton.disabled = true;
  updateRunControl(null);
  updateBudgetControls(null);
  updateCloseoutControl(null);
  updatePendingStart(null);
  updateRecoveryControls(null);
  awaitedChainId = "";
  schedulePoll(false);
  if (currentId) await refreshSession();
}

async function switchActiveSession(targetId) {
  if (!activeSelection || !targetId || versionBusy || busy || controlBusy) return false;
  if (activeSelection.active_workflow_session_id === targetId) {
    await loadSessions();
    return currentId === targetId;
  }
  userSessionRecord(targetId);
  if (rejectLegacyPending()) return false;
  if (!pendingSessionSwitch || pendingSessionSwitch.targetId !== targetId) {
    pendingSessionSwitch = {
      targetId,
      idempotencyKey: crypto.randomUUID(),
      expectedSelectionRevision: activeSelection.revision
    };
  }
  const command = pendingSessionSwitch;
  versionBusy = true;
  sessions.disabled = true;
  $("new-session").disabled = true;
  showError("");
  try {
    const receipt = await request("/api/active-session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workflow_session_id: command.targetId,
        expected_selection_revision: command.expectedSelectionRevision,
        idempotency_key: command.idempotencyKey
      })
    });
    if (pendingSessionSwitch !== command) return false;
    requireLegacyReceipt(exactFields(receipt, ["active_workflow_session_id", "revision"])
      && receipt.active_workflow_session_id === command.targetId
      && receipt.revision === command.expectedSelectionRevision + 1);
    pendingSessionSwitch = null;
    await loadSessions();
    return currentId === targetId;
  } catch (error) {
    if (error.status === 409 && error.code !== "idempotency_conflict" && pendingSessionSwitch === command) {
      pendingSessionSwitch = null;
      await loadSessions();
      showError("活动会话已变化，已刷新。请重试。");
    } else {
      sessions.value = currentId;
      showError(error.message || "切换会话失败，请重试");
    }
  } finally {
    versionBusy = false;
    sessions.disabled = busy;
    $("new-session").disabled = busy;
    if (currentView) render(currentView);
  }
  return false;
}

sessions.addEventListener("change", () => switchActiveSession(sessions.value));

$("new-session").addEventListener("click", async () => {
  if (busy || controlBusy || versionBusy || rejectLegacyPending()) return;
  const button = $("new-session");
  button.disabled = true;
  showError("");
  const command = { path: "/api/sessions", body: {} };
  pendingCreation = command;
  try {
    const created = await request("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}"
    });
    if (!created || !UUID4.test(created.workflow_session_id || ""))
      throw new Error("新建会话回执无效，原请求仍待核实");
    if (pendingCreation !== command) return;
    pendingCreation = null;
    await loadSessions();
    await switchActiveSession(created.workflow_session_id);
  } catch (error) {
    if (Number.isInteger(error.status) && error.status >= 400 && error.status < 500
        && error.code !== "idempotency_conflict" && pendingCreation === command) pendingCreation = null;
    showError(error.message || "新建会话失败");
  } finally {
    button.disabled = false;
  }
});

runControl.addEventListener("click", async () => {
  if (!currentId || !currentView || busy || controlBusy || versionBusy) return;
  if (rejectLegacyPending()) return;
  const control = availableRunControl(currentView);
  if (!control) return;
  const id = currentId;
  if (!pendingControl || pendingControl.sessionId !== id
      || pendingControl.action !== control.action || pendingControl.runId !== control.runId) {
    pendingControl = {
      sessionId: id, action: control.action, runId: control.runId,
      idempotencyKey: crypto.randomUUID(),
      expectedSessionRevision: currentView.revision,
      expectedRunRevision: control.runRevision
    };
  }
  const command = pendingControl;
  controlBusy = true;
  render(currentView);
  showError("");
  let failure = "";
  try {
    const receipt = await request(
      `/api/sessions/${encodeURIComponent(id)}/runs/${encodeURIComponent(command.runId)}/${command.action}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          idempotency_key: command.idempotencyKey,
          expected_session_revision: command.expectedSessionRevision,
          expected_run_revision: command.expectedRunRevision
        })
      }
    );
    if (pendingControl !== command) return;
    requireLegacyReceipt(validRunReceipt(receipt, id, command.runId,
      command.action === "resume" ? ["running"] : ["pausing", "paused"]));
    pendingControl = null;
    if (id === currentId) {
      awaitedChainId = receipt.status === "paused" ? "" : receipt.chain_run_id || "";
      await refreshSession();
    }
  } catch (error) {
    if (error.status === 409 && error.code !== "idempotency_conflict" && pendingControl === command) {
      pendingControl = null;
      if (id === currentId) {
        await refreshSession();
        failure = "会话状态已变化，已刷新。请重试。";
      }
    } else if (id === currentId) {
      failure = error.message || "操作失败，请重试";
    }
  } finally {
    controlBusy = false;
    if (currentView) render(currentView);
    if (failure) showError(failure);
  }
});

additionalRequests.addEventListener("input", () => updateBudgetControls(currentView));
additionalAttempts.addEventListener("input", () => updateBudgetControls(currentView));

extendBudget.addEventListener("click", async () => {
  if (!currentId || !currentView || busy || controlBusy || versionBusy || extendBudget.disabled) return;
  if (rejectLegacyPending()) return;
  const control = availableBudgetControl(currentView);
  if (!control) return;
  const id = currentId;
  const target = `${id}:${control.runId}`;
  if (!pendingBudgets.has(target)) {
    const increments = budgetIncrements(control);
    if (!increments) return;
    pendingBudgets.set(target, {
      runId: control.runId, idempotencyKey: crypto.randomUUID(),
      expectedSessionRevision: currentView.revision,
      expectedRunRevision: control.runRevision,
      additionalRequests: increments.requests, additionalAttempts: increments.attempts
    });
  }
  const command = pendingBudgets.get(target);
  controlBusy = true;
  sessions.disabled = true;
  $("new-session").disabled = true;
  render(currentView);
  showError("");
  let failure = "";
  try {
    const receipt = await request(
      `/api/sessions/${encodeURIComponent(id)}/runs/${encodeURIComponent(command.runId)}/extend_budget`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          idempotency_key: command.idempotencyKey,
          expected_session_revision: command.expectedSessionRevision,
          expected_run_revision: command.expectedRunRevision,
          additional_model_requests: command.additionalRequests,
          additional_model_attempts: command.additionalAttempts
        })
      }
    );
    if (pendingBudgets.get(target) !== command) return;
    requireLegacyReceipt(validRunReceipt(receipt, id, command.runId, ["paused", "failed"], ["revision", "budget"])
      && receipt.revision === command.expectedRunRevision + 1
      && exactFields(receipt.budget, ["max_model_requests", "max_model_attempts"])
      && receipt.budget.max_model_requests === control.budget.max_model_requests + command.additionalRequests
      && receipt.budget.max_model_attempts === control.budget.max_model_attempts + command.additionalAttempts);
    pendingBudgets.delete(target);
    if (id === currentId) await refreshSession();
  } catch (error) {
    if (error.status === 409 && error.code !== "idempotency_conflict" && pendingBudgets.get(target) === command) {
      pendingBudgets.delete(target);
      if (id === currentId) {
        await refreshSession();
        failure = "会话状态已变化，已刷新。请重试。";
      }
    } else if (id === currentId) {
      failure = error.message || "增加额度失败，请重试";
    }
  } finally {
    controlBusy = false;
    sessions.disabled = busy || versionBusy;
    $("new-session").disabled = busy || versionBusy;
    if (currentView) render(currentView);
    if (failure) showError(failure);
  }
});

retryCloseout.addEventListener("click", async () => {
  if (!currentId || !currentView || busy || controlBusy || versionBusy) return;
  if (rejectLegacyPending()) return;
  const control = availableCloseout(currentView);
  if (!control) return;
  const id = currentId;
  if (!pendingCloseout || pendingCloseout.sessionId !== id
      || pendingCloseout.action !== control.action || pendingCloseout.runId !== control.runId) {
    pendingCloseout = {
      sessionId: id, action: control.action, runId: control.runId,
      idempotencyKey: crypto.randomUUID(),
      expectedSessionRevision: currentView.revision
    };
  }
  const command = pendingCloseout;
  controlBusy = true;
  render(currentView);
  showError("");
  let failure = "";
  try {
    const receipt = await request(
      `/api/sessions/${encodeURIComponent(id)}/runs/${encodeURIComponent(command.runId)}/`
      + (command.action === "retry_archive" ? "retry-archive" : "retry-publish"),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          idempotency_key: command.idempotencyKey,
          expected_session_revision: command.expectedSessionRevision
        })
      }
    );
    if (pendingCloseout !== command) return;
    requireLegacyReceipt(validRunReceipt(receipt, id, command.runId,
      command.action === "retry_archive" ? ["succeeded", "accepted"] : ["succeeded"],
      receipt?.status === "accepted" ? ["completion"] : [])
      && (receipt.status !== "accepted" || receipt.completion === "unconfirmed"));
    pendingCloseout = null;
    if (id === currentId) await refreshSession();
  } catch (error) {
    if (error.status === 409 && error.code !== "idempotency_conflict" && pendingCloseout === command) {
      pendingCloseout = null;
      if (id === currentId) {
        await refreshSession();
        failure = "会话状态已变化，已刷新。请重试。";
      }
    } else if (id === currentId) {
      failure = error.message || "收束失败，请重试";
    }
  } finally {
    controlBusy = false;
    if (currentView) render(currentView);
    if (failure) showError(failure);
  }
});

startPending.addEventListener("click", async () => {
  if (!currentId || !currentView || busy || controlBusy || versionBusy) return;
  if (rejectLegacyPending()) return;
  const inputId = availablePendingStart(currentView);
  if (!inputId) return;
  const id = currentId;
  if (!pendingStart || pendingStart.sessionId !== id || pendingStart.inputId !== inputId) {
    pendingStart = {
      sessionId: id, inputId, idempotencyKey: crypto.randomUUID(),
      expectedSessionRevision: currentView.revision
    };
  }
  const command = pendingStart;
  controlBusy = true;
  render(currentView);
  showError("");
  let failure = "";
  try {
    const receipt = await request(
      `/api/sessions/${encodeURIComponent(id)}/pending-inputs/${encodeURIComponent(inputId)}/continue`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          idempotency_key: command.idempotencyKey,
          expected_session_revision: command.expectedSessionRevision
        })
      }
    );
    if (pendingStart !== command) return;
    requireLegacyReceipt(exactFields(receipt, ["workflow_session_id", "chain_run_id", "visible_message_id", "input_id", "status"])
      && receipt.workflow_session_id === id && receipt.input_id === command.inputId && receipt.status === "prepared"
      && UUID4.test(receipt.chain_run_id || "") && UUID4.test(receipt.visible_message_id || ""));
    pendingStart = null;
    if (id === currentId) {
      awaitedChainId = receipt.chain_run_id || "";
      await refreshSession();
    }
  } catch (error) {
    if (error.status === 409 && error.code !== "idempotency_conflict" && pendingStart === command) {
      pendingStart = null;
      if (id === currentId) {
        await refreshSession();
        failure = "会话状态已变化，已刷新。请重试。";
      }
    } else if (id === currentId) {
      failure = error.message || "启动分支输入失败，请重试";
    }
  } finally {
    controlBusy = false;
    if (currentView) render(currentView);
    if (failure) showError(failure);
  }
});

input.addEventListener("input", () => {
  const pending = userSessionRecord(currentId).pending_submission;
  if (pending) input.value = pending.text;
});

for (const choice of promptChoices) {
  choice.config.addEventListener("change", () => {
    if (hasPendingSubmission()) {
      applyPromptSelection(userSessionRecord(currentId).prompt_selection);
      return;
    }
    promptChoiceGeneration += 1;
    const record = promptCatalog.get(choice.config.value);
    choice.revision.value = record ? String(record.revision) : "";
    persistPromptChoice();
    updatePromptControls(currentView);
  });
  const changedRevision = () => {
    if (hasPendingSubmission()) {
      applyPromptSelection(userSessionRecord(currentId).prompt_selection);
      return;
    }
    promptChoiceGeneration += 1;
    persistPromptChoice();
    updatePromptControls(currentView);
  };
  choice.revision.addEventListener("input", changedRevision);
  choice.revision.addEventListener("change", changedRevision);
}

$("reset-prompt-state").addEventListener("click", () => {
  if (!currentId || busy || controlBusy || versionBusy || !promptRecordFailures.has(currentId)) return;
  if (rejectLegacyPending()) return;
  const id = currentId;
  const failure = promptRecordFailures.get(id);
  try {
    if (localStorage.getItem(USER_SESSION_PREFIX + id) !== failure.raw)
      throw new Error("本机会话记录已由其他页面更新，不能重置。");
    if (failure.raw) localStorage.setItem(`${USER_SESSION_PREFIX}quarantine:${id}:${Date.now()}`, failure.raw);
    localStorage.removeItem(USER_SESSION_PREFIX + id);
    promptRecordFailures.delete(id);
    userSessionRecords.delete(id);
    userSessionRawRecords.delete(id);
    promptDraftSessionId = "";
    saveUserSessionRecord(id, emptyUserSessionRecord(id));
    showError("原记录已隔离，本机会话配置已重置。");
    updatePromptControls(currentView);
  } catch (error) {
    showError(error.message || "本机会话记录无法重置");
  }
});

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentId || !currentView || busy || controlBusy || versionBusy || promptRecordFailures.has(currentId)) return;
  if (rejectLegacyPending()) return;
  const id = currentId;
  let record = userSessionRecord(id);
  if (!record.pending_submission && (!currentView.can_submit || !input.value.trim())) return;
  let body;
  try {
    const selected = promptSelection();
    body = {
      text: input.value, idempotency_key: crypto.randomUUID(),
      ...(selected ? { prompt_selection: selected } : {}),
      ...(record.model_selection ? { model_selection: record.model_selection } : {})
    };
    record = saveUserSessionRecord(id, {
      ...record, prompt_selection: selected, pending_submission: body
    });
  } catch (error) {
    showError(error.message);
    updatePromptControls(currentView);
    return;
  }
  busy = true;
  sessions.disabled = true;
  $("new-session").disabled = true;
  submitButton.disabled = true;
  updatePromptControls(currentView);
  showError("");
  try {
    const receipt = await request(`/api/sessions/${encodeURIComponent(id)}/inputs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!ownsSubmission(id, record, body)) {
      if (id === currentId) showError("本机原请求已变化，晚到回执不覆盖当前记录。");
      return;
    }
    if (receipt?.workflow_session_id !== id || receipt.status !== "prepared"
        || ["input_id", "visible_message_id", "chain_run_id"].some(
          (field) => typeof receipt[field] !== "string" || !UUID4.test(receipt[field])
        )) {
      throw new Error("提交回执无效，原请求仍待核实");
    }
    saveUserSessionRecord(id, { ...userSessionRecord(id), pending_submission: null });
    if (id === currentId && input.value === body.text) input.value = "";
    if (id === currentId) {
      awaitedChainId = receipt.chain_run_id || "";
      $("progress").textContent = "工作流进行中…";
      schedulePoll(true);
      await refreshSession();
    }
  } catch (error) {
    if (Number.isInteger(error.status) && error.status >= 400 && error.status < 500
        && error.code !== "idempotency_conflict") {
      try {
        if (ownsSubmission(id, record, body))
          saveUserSessionRecord(id, { ...userSessionRecord(id), pending_submission: null });
      } catch (storageError) {
        showError(storageError.message);
      }
    }
    showError(error.message || "提交失败，请重试");
  } finally {
    busy = false;
    sessions.disabled = false;
    $("new-session").disabled = false;
    updatePromptControls(currentView);
    if (currentView) render(currentView);
  }
});

function handoffFromLocation() {
  const search = typeof location === "object" && typeof location.search === "string" ? location.search : "";
  if (!search) return null;
  const params = new URLSearchParams(search);
  const fields = new Set(["session", "prompt_a", "prompt_a_revision", "prompt_b", "prompt_b_revision",
    "model_config", "model_revision", "exposure_config", "exposure_revision"]);
  if (Array.from(params.keys()).some((field) => !fields.has(field) || params.getAll(field).length !== 1)
      || !UUID4.test(params.get("session") || "")) {
    throw new Error("工作台链接参数无效，未应用链接配置");
  }
  const nodes = {};
  for (const stage of ["A", "B"]) {
    const name = "prompt_" + stage.toLowerCase();
    const hasConfig = params.has(name);
    const hasRevision = params.has(name + "_revision");
    if (hasConfig !== hasRevision) throw new Error("工作台链接缺少精确提示词修订");
    if (!hasConfig) continue;
    const identity = params.get(name);
    const revisionText = params.get(name + "_revision");
    const revision = Number(revisionText);
    if (!UUID4.test(identity || "") || !/^[1-9][0-9]*$/.test(revisionText || "")
        || !Number.isSafeInteger(revision) || revision < 1) {
      throw new Error("工作台链接提示词修订无效");
    }
    nodes[stage] = { config_id: identity, revision };
  }
  let modelSelection = null;
  let exposureSelection = null;
  if (params.has("exposure_config") !== params.has("exposure_revision"))
    throw new Error("工作台链接缺少公开声明修订");
  if (params.has("exposure_config")) {
    if (!/^[1-9][0-9]*$/.test(params.get("exposure_revision") || ""))
      throw new Error("工作台链接公开声明修订无效");
    exposureSelection = validateExposureReference({
      config_id: params.get("exposure_config"), revision: Number(params.get("exposure_revision"))
    });
  }
  if (params.has("model_config") !== params.has("model_revision"))
    throw new Error("工作台链接缺少精确模型修订");
  if (params.has("model_config")) {
    if (!/^[1-9][0-9]*$/.test(params.get("model_revision") || ""))
      throw new Error("工作台链接模型修订无效");
    modelSelection = validateModelSelection({
      schema_version: 1, kind: "workflow_model_selection",
      config_id: params.get("model_config"), revision: Number(params.get("model_revision"))
    });
  }
  return {
    sessionId: params.get("session"),
    modelSelection,
    exposureSelection,
    selection: Object.keys(nodes).length
      ? { schema_version: 1, kind: "workflow_prompt_selection", nodes } : null
  };
}

async function initializeUserInterface() {
  try {
    await loadPromptConfigs();
  } catch (error) {
    showError(error.message || "提示词配置列表无法读取");
  }
  await loadSessions();
  const handoff = handoffFromLocation();
  if (!handoff) return;
  if (!sessionItems.some((session) => session.workflow_session_id === handoff.sessionId)) {
    throw new Error("工作台链接会话不存在，未新建或执行会话");
  }
  const selected = await switchActiveSession(handoff.sessionId);
  if (!selected || currentId !== handoff.sessionId) return;
  if (handoff.exposureSelection) {
    const ref = handoff.exposureSelection;
    const record = await request(`/api/exposure-configurations/${ref.config_id}/revisions/${ref.revision}`);
    if (record?.schema_version !== 1 || record.kind !== "workflow_exposure_configuration"
        || record.config_id !== ref.config_id || record.revision !== ref.revision
        || record.workflow_id !== "frontend:main-test" || currentId !== handoff.sessionId)
      throw new Error("公开声明精确修订响应不匹配");
    localStorage.setItem(EXPOSURE_PREFIX + currentId, JSON.stringify(ref));
    if (currentView) render(currentView);
  }
  if (!handoff.selection && !handoff.modelSelection) return;
  if (hasPendingSubmission()) throw new Error("该会话有待核实提交，链接配置未覆盖原请求");
  const generation = promptChoiceGeneration;
  await verifyExactSelection(handoff.selection);
  if (handoff.modelSelection) {
    const selected = handoff.modelSelection;
    const record = await request(
      `/api/model-configurations/model/${selected.config_id}/revisions/${selected.revision}`
    );
    if (record?.schema_version !== 1 || record.kind !== "workflow_model_configuration"
        || record.config_id !== selected.config_id || record.revision !== selected.revision)
      throw new Error("模型精确修订响应不匹配");
  }
  if (currentId !== handoff.sessionId || generation !== promptChoiceGeneration) {
    throw new Error("会话或手动配置已变化，未应用链接配置");
  }
  saveUserSessionRecord(currentId, {
    ...userSessionRecord(currentId),
    ...(handoff.selection ? { prompt_selection: handoff.selection } : {}),
    ...(handoff.modelSelection ? { model_selection: handoff.modelSelection } : {})
  });
  if (handoff.selection) applyPromptSelection(handoff.selection);
  updatePromptControls(currentView);
  history.replaceState(history.state, "", location.pathname
    + "?session=" + encodeURIComponent(handoff.sessionId) + location.hash);
}

request("/api/health")
  .then((health) => { $("mode").textContent = health.mode === "deepseek" ? "DeepSeek" : "离线测试（非真实模型）"; })
  .catch((error) => showError(error.message || "服务不可用"));
initializeUserInterface().catch((error) => showError(error.message || "加载会话失败"));
