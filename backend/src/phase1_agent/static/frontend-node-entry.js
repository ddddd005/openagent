"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const labels = { idle: "待开始", prepared: "已接纳，准备执行", running: "运行中", pausing: "正在请求暂停",
    paused: "已暂停", succeeded: "已完成", failed: "运行失败", closed: "已结束", archive_failed: "结果归档失败",
    recovery_unavailable: "当前执行不可继续", budget_exhausted: "本轮额度已耗尽" };
  const inputLabels = { ready: "已读取正式输入", pending: "等待保存", saving: "正在保存输入",
    saved: "输入已正式保存", unchanged: "输入未变", unknown: "输入保存结果待核实",
    rejected: "输入尚未保存", confirmation_required: "等待固定修改确认", derived: "已派生工作流" };
  const errors = {
    frontend_node_response_unknown: "通信结果未知，请核实原请求。",
    frontend_node_events_disconnected: "实时连接已断开，可手动刷新并重新连接。",
    frontend_node_invalid_binding: "入口没有准确的工作流、修订、节点和会话身份。",
    frontend_node_entry_mismatch: "后端入口与本页绑定不一致，未继续连接。",
    frontend_node_input_blocked: "输入尚未正式保存，未启动本轮。",
    frontend_node_request_unknown: "原请求仍待核实，未重新发送。",
    frontend_node_input_basis_changed: "正式输入已被其他页面修改，当前编辑未自动改基。",
    frontend_node_result_mismatch: "返回结果与原请求不一致，原请求保留待核实。",
    configuration_run_locked: "该工作流有未结束的运行，运行期间不能修改配置；请先结束并关闭该运行。",
    frontend_node_start_unavailable: "当前会话不可启动新一轮。",
    frontend_node_pause_unavailable: "当前会话不可请求暂停。",
    frontend_node_resume_unavailable: "当前会话不可继续本轮。",
    frontend_node_close_unavailable: "当前会话不可结束本轮。",
    frontend_node_fork_unavailable: "当前不可分叉，请先保存输入并核实未完成请求。",
    frontend_node_checkpoints_unavailable: "尚无可读取检查点的会话。",
    frontend_node_checkpoints_mismatch: "检查点结果与当前绑定不一致。",
    frontend_node_reroll_unavailable: "当前楼层暂不可重 roll，请先保存输入并核实未完成请求。",
    frontend_node_select_candidate_unavailable: "当前候选不可切换；有下文的楼层已固定。",
    frontend_node_fork_floor_unavailable: "当前楼层暂不可分叉。",
  };
  const operationLabels = { patch_input: "输入保存", start: "启动", pause: "暂停",
    resume: "继续", close: "结束本轮", fork_checkpoint: "检查点分叉",
    reroll: "重 roll", select_candidate: "候选选择", fork_floor: "楼层分叉" };
  const roleLabels = { user: "用户", assistant: "助手", system: "系统" };
  let host = null, latest = null, renderedSnapshot = "", renderedCheckpoints = "", closing = false;
  const icons = element => globalThis.lucide?.createIcons({ root: element, nameAttr: "data-lucide",
    attrs: { width: "18", height: "18", "aria-hidden": "true" } });
  const message = value => errors[value] ?? value;
  const short = value => value.slice(0, 8);
  function identity(owner) {
    return `${owner.workflow_definition_id} · r${owner.definition_revision} · ${owner.node_binding_id} · ${owner.session_id ?? "无会话"}`;
  }
  function messageArticle(entryId, role, text, orderText) {
    const article = document.createElement("article"), image = document.createElement("img");
    const body = document.createElement("div"), heading = document.createElement("div");
    const name = document.createElement("h2"), order = document.createElement("small"), content = document.createElement("pre");
    article.className = "message"; article.dataset.entryId = entryId;
    image.src = role === "user" ? "/static/assets/user-avatar.svg" : "/static/assets/assistant-avatar.svg";
    image.alt = ""; heading.className = "message-head"; name.textContent = roleLabels[role];
    order.textContent = orderText; content.textContent = text;
    heading.append(name, order); body.append(heading, content); article.append(image, body);
    return article;
  }
  function chatTool(icon, label, disabled, action) {
    const button = document.createElement("button"), symbol = document.createElement("i");
    button.type = "button"; button.title = label; button.setAttribute("aria-label", label);
    button.disabled = disabled; symbol.dataset.lucide = icon; button.append(symbol);
    button.addEventListener("click", action);
    return button;
  }
  function renderMessages(snapshot, client) {
    const signature = JSON.stringify(snapshot ? [snapshot.owner, snapshot.basis, client?.run, client?.dirty,
      client?.sending, client?.pending, client?.busy, client?.confirmation, client?.next_owner] : null);
    if (signature === renderedSnapshot) return;
    renderedSnapshot = signature;
    const list = $("messages"); list.replaceChildren();
    if (snapshot?.chat.rounds.length) {
      const blocked = !client?.initialized || client.dirty || client.sending || client.confirmation || client.next_owner
        || client.run?.object_revision !== snapshot.basis.revision
        || Object.values(client.pending).some(Boolean) || Object.values(client.busy).some(Boolean);
      for (const [index, round] of snapshot.chat.rounds.entries()) {
        const section = document.createElement("section"), heading = document.createElement("div");
        const title = document.createElement("strong"), select = document.createElement("select");
        section.className = "chat-round"; section.dataset.roundId = round.round_id;
        heading.className = "chat-round-heading"; title.textContent = `第 ${index + 1} 轮`;
        const last = index === snapshot.chat.rounds.length - 1;
        select.setAttribute("aria-label", `第 ${index + 1} 轮候选`);
        if (!round.candidates.length) {
          const option = document.createElement("option");
          option.value = ""; option.textContent = "尚无候选"; select.append(option);
        }
        for (const [candidateIndex, candidate] of round.candidates.entries()) {
          const option = document.createElement("option");
          option.value = candidate.candidate_id; option.disabled = candidate.result_checkpoint_id === null;
          option.textContent = `候选 ${candidateIndex + 1}${candidate.result_checkpoint_id === null ? " · 待成功收尾" : ""}`;
          select.append(option);
        }
        select.value = round.selected_candidate_id ?? "";
        select.disabled = blocked || !last || !client.run?.can_start || !round.candidates.length;
        select.addEventListener("change", () => void perform("selectCandidate", {
          round_id: round.round_id, candidate_id: select.value,
        }));
        const controls = document.createElement("div"); controls.className = "chat-round-controls";
        controls.append(select,
          chatTool("rotate-ccw", `重 roll 第 ${index + 1} 轮`,
            blocked || !last || !client.run?.can_start, () => void perform("reroll", round.round_id)),
          chatTool("git-fork", `从第 ${index + 1} 轮分叉`,
            blocked || !client.run?.can_fork || !round.candidates.some(candidate => candidate.result_checkpoint_id !== null),
            () => void perform("forkFloor", round.round_id)));
        heading.append(title, controls); section.append(heading);
        if (round.submitted_input !== null)
          section.append(messageArticle(round.round_id + ":input", "user", round.submitted_input, "#" + (index + 1)));
        const selected = round.candidates.find(candidate => candidate.candidate_id === round.selected_candidate_id);
        for (const message of selected?.messages ?? [])
          section.append(messageArticle(message.message_id, message.role, message.text, "#" + (index + 1)));
        list.append(section);
      }
      icons(list); return;
    }
    const empty = document.createElement("div"), symbol = document.createElement("i"), text = document.createElement("p");
    empty.className = "empty"; symbol.dataset.lucide = "messages-square"; text.textContent = "暂无消息";
    empty.append(symbol, text); list.append(empty); icons(empty);
  }
  function renderPending(client) {
    const container = $("pending"); container.replaceChildren();
    for (const [operation, original] of Object.entries(client?.pending ?? {})) {
      if (!original) continue;
      const row = document.createElement("div"), label = document.createElement("span"), button = document.createElement("button");
      row.className = "pending-row"; label.textContent = `${operationLabels[operation]}${client.busy[operation] ? "处理中" : "待核实"} · ${original.parameters.idempotency_key}`;
      button.type = "button"; button.disabled = client.busy[operation];
      const symbol = document.createElement("i"); symbol.dataset.lucide = "search-check";
      const text = document.createElement("span"); text.textContent = "核实原请求"; button.append(symbol, text);
      button.addEventListener("click", () => void perform("reconcile", operation));
      row.append(label, button); container.append(row);
    }
    container.hidden = !container.children.length; icons(container);
  }
  function renderCheckpoints(client) {
    const select = $("checkpoint-select"), points = client?.checkpoints?.items ?? [];
    const signature = JSON.stringify([client?.owner, points]);
    if (signature !== renderedCheckpoints) {
      renderedCheckpoints = signature;
      const previous = select.value;
      select.replaceChildren();
      for (const [index, point] of points.entries()) {
        const option = document.createElement("option");
        option.value = point.checkpoint_id; option.textContent = `#${index + 1} · ${short(point.checkpoint_id)}`;
        select.append(option);
      }
      if (!points.length) {
        const option = document.createElement("option");
        option.value = ""; option.textContent = "暂无检查点"; select.append(option);
      } else select.value = points.some(point => point.checkpoint_id === previous)
        ? previous : points.at(-1).checkpoint_id;
    }
    select.disabled = !points.length;
    const selected = points.find(point => point.checkpoint_id === select.value);
    $("checkpoint-detail").textContent = selected
      ? `${selected.checkpoint_id} · ${selected.parent_checkpoint_id ? "前点 " + selected.parent_checkpoint_id : "首个检查点"}`
      : "";
    $("checkpoint-fork").disabled = !selected || !client?.run?.can_fork || !!client?.next_owner
      || !!client?.dirty || !!client?.sending || Object.values(client?.pending ?? {}).some(Boolean)
      || Object.values(client?.busy ?? {}).some(Boolean);
  }
  function render(view) {
    if (closing) return;
    latest = view;
    const client = view.client, owner = view.owner;
    $("connection-status").dataset.state = view.connection;
    $("connection-status").textContent = { connecting: "连接中", connected: "实时连接", disconnected: "连接断开" }[view.connection];
    if (owner) {
      $("workflow-identity").textContent = `工作流 ${short(owner.workflow_definition_id)} · r${owner.definition_revision}`;
      $("node-identity").textContent = `节点 ${short(owner.node_binding_id)}`;
      $("session-identity").textContent = owner.session_id ? `会话 ${short(owner.session_id)}` : "尚未创建会话";
      for (const [id, value] of [["workflow-identity", owner.workflow_definition_id], ["node-identity", owner.node_binding_id],
        ["session-identity", owner.session_id ?? "尚未创建会话"]]) $(id).title = value;
    }
    const error = view.error || Object.values(client?.errors ?? {}).find(Boolean) || "";
    $("error").hidden = !error; $("error").textContent = message(error);
    $("notice").hidden = !view.notice; $("notice").textContent = view.notice;
    const ready = client?.initialized, blockedInput = !ready || client.input === null || client.sending || client.next_owner !== null;
    $("message").disabled = blockedInput;
    if (client && $("message").value !== client.text) $("message").value = client.text;
    $("input-status").textContent = !ready ? "正在读取正式输入" : client.input === null ? "未绑定输入节点"
      : inputLabels[client.status.patch_input] ?? client.status.patch_input;
    $("input-policy").textContent = client?.input ? `${client.input.policy === "fixed" ? "固定输入" : "可调输入"} · 编辑序号 ${client.input.edit_sequence}` : "";
    $("snapshot-status").textContent = client?.snapshot
      ? `展示修订 ${client.snapshot.basis.revision} · ${client.snapshot.chat.rounds.length} 轮` : "";
    const blockedSave = blockedInput || !!client?.pending.patch_input || !!client?.confirmation || !!client?.busy.patch_input;
    $("save").disabled = blockedSave || !client?.dirty;
    $("send").disabled = !ready || client.sending || client.next_owner !== null || !!client.pending.start
      || !!client.pending.patch_input || !!client.confirmation || client.status.patch_input === "rejected"
      || owner.session_id !== null && !client.run?.can_start;
    $("pause").disabled = !ready || !client.run?.can_pause || !!client.pending.pause || !!client.busy.pause || !!client.next_owner;
    for (const [id, operation] of [["resume", "resume"], ["close-run", "close"]])
      $(id).disabled = !ready || !client.run?.["can_" + operation]
        || !!client.pending[operation] || !!client.busy[operation] || !!client.next_owner;
    $("checkpoints-open").disabled = !ready || owner.session_id === null;
    $("checkpoints-refresh").disabled = !ready || owner.session_id === null;
    $("run-status").textContent = client?.run ? labels[client.run.status] ?? client.run.status
      : ready ? "尚未开始" : "正在读取状态";
    $("confirmation").hidden = !client?.confirmation;
    $("confirmation-text").textContent = client?.confirmation?.parameters.text ?? "";
    $("confirm").disabled = !!client?.busy.patch_input;
    $("decline").disabled = !!client?.busy.patch_input;
    $("binding-next").hidden = !client?.next_owner;
    if (client?.next_owner) {
      $("next-identity").textContent = identity(client.next_owner);
      const result = client.last_result?.result;
      $("next-title").textContent = result?.failure_phase ? "会话已创建，启动未完成"
        : client.last_result?.operation === "patch_input" ? "配置已保存，请接续准确的新绑定"
          : ["fork_checkpoint", "fork_floor"].includes(client.last_result?.operation)
            ? "分叉已创建，请接续新会话" : "有新的准确绑定";
    }
    renderPending(client); renderMessages(client?.snapshot ?? null, client); renderCheckpoints(client);
  }
  async function perform(action, operation) {
    if (!host || closing) return;
    try { await host.action(action, operation); }
    catch { /* The host and client retain the original result and publish the diagnostic. */ }
  }
  $("composer").addEventListener("submit", event => { event.preventDefault(); void perform("send"); });
  $("message").addEventListener("input", event => { try { host?.update(event.target.value); } catch {} });
  $("save").addEventListener("click", () => void perform("flush"));
  $("pause").addEventListener("click", () => void perform("pause"));
  $("resume").addEventListener("click", () => void perform("resume"));
  $("close-run").addEventListener("click", () => void perform("close"));
  $("checkpoints-open").addEventListener("click", () => {
    $("checkpoints-panel").hidden = !$("checkpoints-panel").hidden;
    if (!$("checkpoints-panel").hidden) void perform("listCheckpoints");
  });
  $("checkpoints-refresh").addEventListener("click", () => void perform("listCheckpoints"));
  $("checkpoint-select").addEventListener("change", () => {
    renderCheckpoints(latest?.client);
    if ($("checkpoint-select").value) void perform("readCheckpoint", $("checkpoint-select").value);
  });
  $("checkpoint-fork").addEventListener("click", () => void perform("forkCheckpoint", $("checkpoint-select").value));
  $("confirm").addEventListener("click", () => void perform("confirm"));
  $("decline").addEventListener("click", () => void perform("decline"));
  $("refresh").addEventListener("click", () => { if (host) void host.refresh().catch(() => {}); });
  $("continue-binding").addEventListener("click", () => { if (host) void host.continueBinding().catch(() => {}); });
  window.addEventListener("pagehide", () => { closing = true; host?.dispose(); });
  window.addEventListener("beforeunload", event => {
    if (!latest?.client?.dirty && !Object.values(latest?.client?.pending ?? {}).some(Boolean)) return;
    event.preventDefault(); event.returnValue = "";
  });
  icons(document);
  try {
    if (location.search) throw new Error("frontend_node_invalid_binding");
    const owner = FrontendNodePage.parseOwnerPath(location.pathname);
    host = new FrontendNodePage.Host({
      origin: location.origin, fetcher: (...args) => fetch(...args),
      eventSourceFactory: url => new EventSource(url),
      keyFactory: () => crypto.randomUUID(), onChange: render,
      onBinding: (binding, url) => history.replaceState(null, "", url),
    });
    void host.bind(owner).catch(() => {});
  } catch (failure) {
    $("error").hidden = false; $("error").textContent = message(failure.message);
    $("connection-status").textContent = "入口不可用"; $("refresh").disabled = true;
  }
})();
