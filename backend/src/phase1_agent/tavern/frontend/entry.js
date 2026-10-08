"use strict";

(async () => {
  const $ = id => document.getElementById(id);
  const labels = { idle: "待开始", prepared: "准备中", running: "进行中", pausing: "正在暂停", paused: "已暂停",
    budget_exhausted: "额度已耗尽", archive_failed: "归档待重试", succeeded: "已完成", failed: "失败",
    recovery_unavailable: "恢复不可用", closed: "已结束", superseded: "已替代" };
  const actions = {
    pause: ["pause", "暂停"], resume: ["play", "继续"], close: ["square", "结束运行"],
    extend_budget: ["plus", "增加额度"], retry_archive: ["archive-restore", "重试归档"],
    retry_acceptance: ["check-check", "结果接纳重试"], retry_failed_node: ["rotate-ccw", "重新调用失败节点"],
  };
  const query = new URLSearchParams(location.search);
  const workflowId = query.get("graph_workflow"), requestedSession = query.get("graph_session");
  let client = null, definition = null, localBusy = false, reading = false, timer = null;
  let directory = [], contextKey = null, historyKey = null, transcriptGeneration = 0, disposed = false;
  let transcriptMessages = [];
  const cache = new Map(), drafts = new Map();
  const renderer = new TavernChat.ChatRenderer({
    chatElement: $("chat"), messageTemplate: $("message_template"),
    onFork: entryId => forkEntry(entryId),
    onCopy: async (text, button) => {
      try {
        await navigator.clipboard.writeText(text);
        button.title = "已复制"; button.setAttribute("aria-label", "已复制");
        setTimeout(() => { button.title = "复制原文"; button.setAttribute("aria-label", "复制原文"); }, 1200);
      } catch { error("复制失败，请检查剪贴板权限"); }
    },
  });
  function error(message) { $("error").textContent = message || ""; $("error").hidden = !message; }
  function diagnostics(message) { $("adaptation").textContent = message || ""; $("adaptation").hidden = !message; }
  function draftKey(view) {
    return view ? `workflow.tavern:draft:v1:${workflowId}:${view.workflow_session_id}:${view.definition_revision}` : null;
  }
  function rememberDraft() {
    if (!contextKey) return;
    const value = $("send_textarea").value;
    drafts.set(contextKey, value);
    try { localStorage.setItem(contextKey, value); } catch { /* Drafts remain available for this page. */ }
  }
  function context() {
    const view = client?.consumer, key = draftKey(view);
    if (key === contextKey) return;
    rememberDraft(); contextKey = key; cache.clear(); historyKey = null;
    transcriptGeneration++; transcriptMessages = []; renderer.clear();
    const empty = document.createElement("p"); empty.className = "empty-chat"; empty.textContent = "暂无聊天记录";
    $("chat").append(empty);
    let draft = key ? drafts.get(key) : "";
    if (key && draft === undefined) {
      try { draft = localStorage.getItem(key) ?? ""; } catch { draft = ""; }
    }
    $("send_textarea").value = typeof draft === "string" && draft.length <= TavernAdapter.MAX_TEXT ? draft : "";
    const pending = client?.pending;
    if (pending?.action === "start" && pending.workflow_session_id === view?.workflow_session_id) {
      const values = Object.values(pending.body.inputs);
      if (values.length === 1 && typeof values[0] === "string") $("send_textarea").value = values[0];
    }
  }
  function renderDirectory() {
    const selector = $("sessions"); selector.replaceChildren();
    const empty = document.createElement("option"); empty.value = ""; empty.textContent = "选择会话"; selector.append(empty);
    for (const [index, row] of directory.entries()) {
      const option = document.createElement("option");
      option.value = row.workflow_session_id;
      option.textContent = `会话 ${index + 1} · ${row.workflow_session_id.slice(0, 8)} · ${labels[row.status] ?? row.status}`;
      option.title = row.workflow_session_id; selector.append(option);
    }
    selector.value = client?.sessionId ?? "";
  }
  function actionButton(action, locked) {
    const [icon, label] = actions[action];
    const button = document.createElement("button"); button.type = "button"; button.className = "icon-button";
    button.title = label; button.setAttribute("aria-label", label); button.disabled = locked;
    const glyph = document.createElement("i"); glyph.dataset.lucide = icon; button.append(glyph);
    button.onclick = () => void perform(action); return button;
  }
  function render() {
    context();
    const view = client?.consumer, locked = localBusy || reading || client?.busy || !!client?.pending;
    const adaptation = TavernAdapter.textInput(view), ports = TavernAdapter.displayPorts(view);
    const message = adaptation.diagnostic ?? (view && !ports.length
      ? "当前工作流没有公开的酒馆聊天展示端口。请在工作台检查节点、包版本和输出声明。"
      : ports.length > 1 ? "第一版聊天页需要唯一的公开酒馆展示端口。" : null);
    diagnostics(message);
    $("workflow-name").textContent = definition?.name ?? "OpenAgent";
    $("run-status").textContent = view
      ? `${labels[view.status] ?? view.status} · 定义 r${view.definition_revision} · ${view.workflow_session_id.slice(0, 8)}`
      : client?.pending ? "原请求待核实" : "选择或新建会话";
    $("sessions").disabled = locked || !directory.length;
    $("new-session").disabled = locked || !definition;
    $("refresh").disabled = localBusy || reading || client?.busy || !client?.sessionId;
    $("send_textarea").disabled = locked || !adaptation.input || ports.length !== 1 || !view?.can_submit;
    $("send_but").disabled = locked || !adaptation.input || ports.length !== 1 || !view?.can_submit;
    $("pending").hidden = !client?.pending;
    $("pending-text").textContent = client?.pending?.action === "fork"
      ? `分叉待核实 · 父会话 ${client.pending.workflow_session_id.slice(0, 8)} · 检查点 ${client.pending.candidate.candidate_id.slice(0, 8)} · ${client.pending.body.idempotency_key}`
      : client?.pending ? `待核实 · ${client.pending.body.idempotency_key}` : "";
    $("reconcile").disabled = localBusy || reading || client?.busy || !client?.pending;
    const container = $("run-actions"); container.replaceChildren();
    for (const action of view?.available_actions ?? []) container.append(actionButton(action, locked));
    const nodes = $("nodes"); nodes.replaceChildren();
    for (const node of view?.nodes ?? []) {
      const row = document.createElement("div"); row.className = "node-row";
      const name = document.createElement("span"), status = document.createElement("span");
      name.textContent = node.label; status.textContent = labels[node.status] ?? node.status; row.append(name, status);
      if (node.budget) {
        const budget = document.createElement("small");
        budget.textContent = `模型请求 ${node.budget.model_requests ?? 0}/${node.budget.max_model_requests} · 传输尝试 ${node.budget.attempts ?? 0}/${node.budget.max_model_attempts}`;
        row.append(budget);
      }
      if (node.diagnostic) {
        const detail = document.createElement("small");
        detail.textContent = `${node.diagnostic.reason_code ?? node.diagnostic.code ?? "运行诊断"} · ${node.diagnostic.message ?? ""}`;
        row.append(detail);
      }
      nodes.append(row);
    }
    $("run-detail").hidden = !view?.nodes.length;
    $("budget").hidden = !view?.available_actions.includes("extend_budget");
    $("requests").disabled = locked; $("attempts").disabled = locked;
    renderer.renderForks(currentTargets(), locked);
    TavernChat.icons(document);
  }
  function updateUrl() {
    if (client.sessionId) query.set("graph_session", client.sessionId);
    else query.delete("graph_session");
    history.replaceState(null, "", `${location.pathname}?${query}${location.hash}`);
  }
  function schedule() {
    clearTimeout(timer);
    if (!disposed && !localBusy && client?.consumer && ["prepared", "running", "pausing"].includes(client.consumer.status))
      timer = setTimeout(() => void refresh(), 800);
  }
  async function loadDirectory() {
    const session = client.sessionId, generation = client.generation;
    const value = await client.readSessions();
    if (disposed || session !== client.sessionId || generation !== client.generation) return;
    directory = value; renderDirectory();
  }
  function currentTargets() {
    return TavernAdapter.roundTargets(transcriptMessages, !client?.pending && client?.candidatesCurrent()
      ? client.forkCandidates : null);
  }
  async function loadCandidates() {
    if (!client?.consumer || client.pending || !client.application?.queries.some(row => row.name === "consumer.candidate.list")
      || !client.application.commands.some(row => row.name === "consumer.candidate.fork")) return;
    try { await client.readCandidates(); }
    catch (failure) { if (!disposed) error(failure.message); }
    render();
  }
  async function loadTranscript(force = false) {
    const view = client.consumer;
    if (!view) return;
    const key = JSON.stringify([view.workflow_session_id, view.definition_revision, view.session_revision,
      view.history, view.outputs]);
    if (!force && key === historyKey) return;
    const generation = ++transcriptGeneration;
    $("transcript-status").textContent = "读取聊天记录…";
    try {
      const messages = await TavernAdapter.transcript(client, cache);
      if (!messages || disposed || generation !== transcriptGeneration || view !== client.consumer) return;
      $("chat").querySelector(".empty-chat")?.remove();
      transcriptMessages = messages;
      renderer.render(messages);
      if (!messages.length) {
        const empty = document.createElement("p"); empty.className = "empty-chat"; empty.textContent = "暂无聊天记录";
        $("chat").append(empty);
      }
      historyKey = key; $("transcript-status").textContent = `${messages.length} 条展示记录`;
      renderer.renderForks(currentTargets(), localBusy || reading || client.busy || !!client.pending);
    } catch (failure) {
      if (!disposed && generation === transcriptGeneration && view === client.consumer) {
        $("transcript-status").textContent = "聊天记录读取未完成"; error(failure.message);
      }
    }
  }
  async function refresh() {
    if (localBusy || reading || client?.busy || !client?.sessionId) return;
    reading = true; render();
    try {
      await client.refresh(); error(""); render();
      await loadTranscript(); await loadCandidates();
    } catch (failure) { error(failure.message); }
    finally { reading = false; render(); schedule(); }
  }
  function budgetFields() {
    const requests = Number($("requests").value), attempts = Number($("attempts").value);
    if (!Number.isSafeInteger(requests) || requests < 0 || requests > 64
      || !Number.isSafeInteger(attempts) || attempts < 0 || attempts > 256 || requests + attempts === 0)
      throw new Error("额度增量无效");
    return { add_model_requests: requests, add_model_attempts: attempts };
  }
  async function forkEntry(entryId) {
    if (localBusy || reading || !client || client.busy || client.pending) return;
    const target = currentTargets().get(entryId);
    if (!target) { error("此记录没有当前可用的完成回合检查点"); return; }
    if (!confirm(TavernAdapter.forkConfirmation(target))) return;
    await perform("fork", false, target);
  }
  async function perform(action, reconcile = false, target = null) {
    if (localBusy || reading || !client || client.busy || !!client.pending !== reconcile) return;
    if (!reconcile && action === "start"
      && (!client.consumer?.can_submit || TavernAdapter.displayPorts(client.consumer).length !== 1)) {
      error("当前会话或酒馆展示声明不支持发送"); return;
    }
    if (!reconcile && action === "fork"
      && (!target || !client.candidatesCurrent() || !client.forkCandidates.can_fork
        || !client.forkCandidates.candidates.some(candidate => JSON.stringify(candidate) === JSON.stringify(target.candidate)))) {
      error("完成回合检查点依据已变化，请刷新"); return;
    }
    if (!reconcile && action === "close" && !confirm("结束当前运行？已接纳的结果保留。")) return;
    if (!reconcile && action === "retry_failed_node"
      && !confirm("重新调用失败节点？已接纳的结果保留，本次调用可能消耗模型额度。")) return;
    localBusy = true; clearTimeout(timer); error(""); rememberDraft(); render();
    let original = client.pending ? GraphChat.clone(client.pending) : null;
    try {
      const fields = reconcile ? {} : action === "start"
        ? { inputs: TavernAdapter.inputsFor(client.consumer, $("send_textarea").value) }
        : action === "create" ? { definition_revision: definition.definition_revision }
          : action === "extend_budget" ? budgetFields() : {};
      const submitted = original?.action === "start" ? Object.values(original.body.inputs)[0]
        : action === "start" ? $("send_textarea").value : null;
      if (!reconcile && action === "fork") await client.forkCandidate(target.candidate);
      else await client.command(reconcile ? original.action : action, fields);
      if ((original?.action ?? action) === "start" && typeof submitted === "string" && $("send_textarea").value === submitted) {
        $("send_textarea").value = ""; rememberDraft();
      }
      updateUrl(); await loadDirectory(); await client.refresh(); render();
      await loadTranscript(true); await loadCandidates();
    } catch (failure) { error(failure.message); }
    finally { localBusy = false; render(); schedule(); }
  }
  $("send_form").addEventListener("submit", event => { event.preventDefault(); void perform("start"); });
  $("send_textarea").addEventListener("input", rememberDraft);
  $("new-session").onclick = () => void perform("create");
  $("refresh").onclick = () => void refresh();
  $("reconcile").onclick = () => void perform(client?.pending?.action, true);
  $("sessions").onchange = async event => {
    const session = event.target.value;
    if (!session || localBusy || reading || client.busy || client.pending) return;
    localBusy = true; rememberDraft(); clearTimeout(timer); transcriptGeneration++;
    try {
      await client.selectSession(session); updateUrl(); error(""); render();
      await loadTranscript(true); await loadCandidates();
    } catch (failure) { error(failure.message); }
    finally { localBusy = false; render(); schedule(); }
  };
  window.addEventListener("pagehide", () => {
    disposed = true; clearTimeout(timer); rememberDraft(); renderer.clear(); transcriptGeneration++;
    if (client) { client.generation++; client.invalidateInformation(); }
  });
  try {
    TavernChat.icons(document);
    if (!GraphChat.uuid(workflowId) || requestedSession !== null && !GraphChat.uuid(requestedSession))
      throw new Error("酒馆入口需要有效的工作流和会话身份");
    client = TavernAdapter.createClient({ workflowId, sessionId: requestedSession, storage: localStorage,
      fetcher: fetch.bind(window), keyFactory: () => crypto.randomUUID() });
    await client.discover(); definition = await client.readDefinition(); await loadDirectory();
    await client.refresh(); updateUrl(); render(); await loadTranscript(); await loadCandidates(); schedule();
  } catch (failure) { error(failure.message); }
})();
