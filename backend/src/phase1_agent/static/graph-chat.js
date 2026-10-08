"use strict";

(async () => {
  const { uuid, supportedContent, displayText, httpFailure, parseExternalInput, GraphChatClient } = GraphChat;
  const query = new URLSearchParams(location.search);
  const workflowId = query.get("graph_workflow"), requestedSession = query.get("graph_session");
  const main = document.querySelector("main");
  main.innerHTML = `<header><h1>聊天前端</h1><span id="graph-name" class="mode"></span></header>
    <div class="session-bar"><label for="graph-sessions">会话</label><select id="graph-sessions"></select><button id="graph-new-session" type="button" disabled>新建会话</button></div>
    <div class="run-toolbar"><span id="graph-status" role="status"></span><div class="graph-actions"><div id="graph-actions" class="graph-actions"></div><button id="graph-refresh" type="button" disabled>刷新</button></div></div>
    <div id="graph-budget" class="budget-controls" hidden><label for="graph-requests">逻辑请求 +</label><input id="graph-requests" type="number" min="0" max="64" step="1" value="1"><label for="graph-attempts">传输尝试 +</label><input id="graph-attempts" type="number" min="0" max="256" step="1" value="4"></div>
    <div id="graph-error" class="error" role="alert" hidden></div>
    <section class="graph-node-progress" aria-label="节点状态"><div id="graph-node-list"></div></section>
    <section class="public-exposures" aria-label="公开结果"><h2>公开结果</h2><div id="graph-outputs"></div></section>
    <section class="public-exposures graph-history" aria-label="公开历史"><h2>公开历史</h2><select id="graph-history-port" aria-label="公开历史端口"></select><div id="graph-history-list"></div></section>
    <details class="public-exposures"><summary>消费能力与操作</summary><div id="graph-capabilities"></div></details>
    <section class="public-exposures" aria-label="公开登记与信息"><h2>公开登记与信息</h2>
      <div class="session-bar"><button id="graph-registration-refresh" type="button" disabled>刷新目录</button><button id="graph-registration-next" type="button" disabled>下一页</button><span id="graph-registration-status" role="status"></span></div>
      <div id="graph-registrations"></div>
      <div class="session-bar"><select id="graph-information-source" aria-label="公开信息调用"></select><select id="graph-information-scope" aria-label="信息来源作用域"></select><button id="graph-information-read" type="button" disabled>读取</button><button id="graph-information-next" type="button" disabled>下一页</button></div>
      <div id="graph-information-status" role="status"></div><div id="graph-information-items"></div>
    </section>
    <form id="graph-composer"><div id="graph-inputs"></div><div class="submit-row"><span id="graph-pending" class="mode" role="status"></span><button id="graph-submit" type="submit" disabled>启动</button></div></form>
    <section class="public-exposures" aria-label="局部事件"><h2>局部事件</h2>
      <div class="session-bar"><select id="graph-events" aria-label="事件版本"></select><button id="graph-event-refresh" type="button" disabled>刷新事件</button></div>
      <form id="graph-event-composer"><label for="graph-event-payload">事件载荷 JSON</label><textarea id="graph-event-payload" rows="6" maxlength="4000000" spellcheck="false">{}</textarea>
        <div class="submit-row"><span id="graph-event-status" role="status"></span><button id="graph-event-submit" type="submit" disabled>提交局部事件</button></div>
      </form><details><summary>事件载荷 schema</summary><pre id="graph-event-schema"></pre></details>
    </section>`;
  const $ = id => document.getElementById(id);
  function appendThinking(target, value) {
    const summaries = GraphChat.thinkingSummaries(value);
    if (!summaries.length) return;
    const details = document.createElement("details"), label = document.createElement("summary");
    details.setAttribute("aria-label", "思考摘要"); label.textContent = "思考摘要"; details.append(label);
    for (const text of summaries) {
      const paragraph = document.createElement("pre"); paragraph.textContent = text; details.append(paragraph);
    }
    target.append(details);
  }
  const labels = { idle: "待开始", prepared: "准备中", running: "进行中", pausing: "正在暂停", paused: "已暂停",
    budget_exhausted: "额度已耗尽", archive_failed: "归档待重试", succeeded: "已完成", failed: "失败",
    recovery_unavailable: "恢复不可用", closed: "已结束", superseded: "已替代" };
  const actionLabels = { pause: "暂停", resume: "继续", close: "结束运行", extend_budget: "增加额度", retry_archive: "重试归档", retry_acceptance: "结果接纳重试", retry_failed_node: "重新调用失败节点" };
  let definition = null, client = null, directory = [], timer = null, historyGeneration = 0, inputSession = null, historyViewKey = null;
  let formInputs = [], inputRevision = null, localBusy = false;
  let eventPacket = null, eventsBusy = false;
  let registrationCursor = null, informationCursor = null, registrationBusy = false, informationBusy = false;
  let informationViewGeneration = 0;
  let frontendHost = null;
  const outputMounts = [], historyMounts = [];
  function disposeMounts(mounts) { for (const mount of mounts.splice(0)) mount.dispose(); }
  function clearHistory() { disposeMounts(historyMounts); $("graph-history-list").replaceChildren(); }
  const inputDrafts = new Map();
  function error(message) { $("graph-error").textContent = message || ""; $("graph-error").hidden = !message; }
  async function request(path, options = {}) {
    const mutation = options.method === "POST" && path === "/api/graph/consumer/commands";
    let response;
    try { response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...options.headers }, cache: "no-store" }); }
    catch { throw new Error(mutation ? "请求结果未知，请核实原请求" : "公开读取失败，请刷新"); }
    let value;
    try { value = await response.json(); } catch { throw new Error(mutation ? "响应内容未知，请核实原请求" : "公开读取响应无效，请刷新"); }
    if (!response.ok) throw httpFailure(value, response.status);
    return value;
  }
  function rememberInputs() {
    if (inputSession) inputDrafts.set(inputSession, Object.fromEntries(formInputs.map(row => [row.name, row.element.value])));
  }
  function renderInputs(view) {
    const session = view?.workflow_session_id ?? null;
    if (session === inputSession && view?.definition_revision === inputRevision) {
      for (const row of formInputs) row.element.disabled = localBusy || client.busy || !!client.pending;
      return;
    }
    rememberInputs(); inputSession = session; inputRevision = view?.definition_revision ?? null;
    formInputs = []; const container = $("graph-inputs"); container.replaceChildren();
    const pendingInputs = client.pending?.action === "start" && client.pending.workflow_session_id === session
      ? client.pending.body.inputs : null;
    const draft = pendingInputs ?? inputDrafts.get(session) ?? {};
    const declarations = (view?.inputs ?? []).map(input => ({ ...input,
      ...(pendingInputs && Object.hasOwn(pendingInputs, input.name)
        ? { data_type: typeof pendingInputs[input.name] === "string" ? "TEXT" : "PROMPT" } : {}) }));
    for (const name of Object.keys(pendingInputs ?? {})) if (!declarations.some(input => input.name === name)) declarations.push({
      name, data_type: typeof pendingInputs[name] === "string" ? "TEXT" : "PROMPT",
    });
    for (const [index, declaration] of declarations.entries()) {
      const label = document.createElement("label"), element = document.createElement("textarea");
      label.htmlFor = `graph-input-${index}`; label.textContent = `${declaration.name} · ${declaration.data_type}`;
      element.id = label.htmlFor; element.rows = declaration.data_type === "PROMPT" ? 5 : 3;
      element.maxLength = declaration.data_type === "PROMPT" ? 4000000 : 1000000;
      const value = Object.hasOwn(draft, declaration.name) ? draft[declaration.name]
        : declaration.data_type === "PROMPT" ? { schema_version: 1, kind: "workflow.prompt", stage: "materials", items: [], assembly: null } : "";
      element.value = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      element.disabled = localBusy || client.busy || !!client.pending;
      formInputs.push({ name: declaration.name, data_type: declaration.data_type, element }); container.append(label, element);
    }
  }
  function renderEvents() {
    const packet = client?.eventBindings ?? null, selector = $("graph-events");
    if (packet !== eventPacket) {
      const selected = selector.value; selector.replaceChildren();
      const empty = document.createElement("option"); empty.value = ""; empty.textContent = "选择事件版本"; selector.append(empty);
      for (const binding of packet?.bindings ?? []) {
        const option = document.createElement("option"); option.value = JSON.stringify([binding.event_id, binding.schema_version]);
        option.textContent = `${binding.display_name} · ${binding.event_id}@${binding.schema_version}`; selector.append(option);
      }
      if ([...selector.options].some(option => option.value === selected)) selector.value = selected;
      else $("graph-event-payload").value = "{}";
      eventPacket = packet;
    }
    const binding = packet?.bindings.find(row => JSON.stringify([row.event_id, row.schema_version]) === selector.value);
    $("graph-event-schema").textContent = binding ? JSON.stringify(binding.payload_schema, null, 2) : "";
    const locked = localBusy || client?.busy || !!client?.pending || eventsBusy;
    selector.disabled = locked || !packet?.bindings.length;
    $("graph-event-payload").disabled = locked;
    $("graph-event-refresh").disabled = locked || !client?.consumer;
    $("graph-event-submit").disabled = locked || !binding || !packet?.can_submit;
  }
  async function loadEvents() {
    if (!client?.consumer || eventsBusy) { renderEvents(); return; }
    if (client.application && !client.application.queries.some(row => row.name === "consumer.event.bindings")) {
      $("graph-event-status").textContent = "未登记局部事件入口"; renderEvents(); return;
    }
    eventsBusy = true; renderEvents();
    try { const packet = await client.readEventBindings();
      if (packet) $("graph-event-status").textContent = packet.can_submit ? `${packet.bindings.length} 个公开事件` : "当前会话不可提交事件";
    } catch (failure) { $("graph-event-status").textContent = failure.message; }
    finally { eventsBusy = false; renderEvents(); }
  }
  async function performEvent() {
    if (localBusy || client?.busy || client?.pending) return;
    const binding = client?.eventBindings?.bindings.find(row => JSON.stringify([row.event_id, row.schema_version]) === $("graph-events").value);
    if (!binding) return;
    localBusy = true; clearTimeout(timer); error(""); render();
    try {
      const payload = JSON.parse($("graph-event-payload").value);
      await client.submitEvent(binding, payload); await client.refresh(); await loadEvents();
    } catch (failure) { error(failure.message); }
    finally { localBusy = false; render(); schedule(); }
  }
  function outputArticle(output, historical = false) {
    const article = document.createElement("article"), heading = document.createElement("h3"), content = document.createElement("pre");
    heading.textContent = `${output.label} / ${output.port_id}`;
    const version = output.data_schema_version ?? output.payload?.schema_version;
    content.textContent = output.availability === "produced" ? supportedContent(output.data_type, version)
      ? displayText(output.payload) : `不支持的公开格式 · ${output.data_type}@${version ?? "未知版本"}`
      : output.availability === "unproduced" ? "尚未产出" : `不可用 · ${output.reason_code}`;
    article.append(heading); appendThinking(article, output.payload); article.append(content);
    if (output.availability === "produced" && frontendHost) {
      const slot = document.createElement("div"), status = document.createElement("small");
      article.append(slot, status);
      const handle = frontendHost.attach(output, slot);
      (historical ? historyMounts : outputMounts).push(handle);
      handle.ready.then(state => {
        if (!article.isConnected) return;
        if (state === "mounted") content.hidden = true;
        else if (state === "implementation-unavailable") status.textContent = "界面扩展实现不可用，显示原始公开内容。";
      }).catch(failure => { if (article.isConnected) status.textContent = `界面扩展读取失败 · ${failure.message}`; });
    }
    if (output.source) {
      const source = document.createElement("small"); source.className = "muted";
      source.textContent = `${historical ? "历史 · " : ""}运行 ${output.source.chain_run_id} · 节点 ${output.source.node_binding_id} · 会话 ${output.source.workflow_session_id} · 定义 r${output.source.definition_revision}`;
      article.append(source);
    }
    return article;
  }
  function render() {
    const view = client?.consumer, locked = localBusy || client?.busy || !!client?.pending;
    const historyKey = JSON.stringify([view?.workflow_session_id, view?.definition_revision, view?.session_revision,
      view?.outputs.map(output => [output.node_binding_id, output.port_id, output.data_type,
        output.data_schema_version, output.run_id, output.availability])]);
    if (historyKey !== historyViewKey) {
      historyViewKey = historyKey; historyGeneration++; clearHistory();
    }
    $("graph-sessions").disabled = locked || !directory.length;
    $("graph-new-session").disabled = locked || !definition;
    $("graph-refresh").disabled = localBusy || client?.busy || !client?.sessionId;
    $("graph-status").textContent = view ? `${labels[view.status] ?? view.status} · 定义 r${view.definition_revision} · 会话 ${view.workflow_session_id}` : "选择或新建会话";
    $("graph-submit").disabled = localBusy || client?.busy || !client?.pending && !view?.can_submit;
    $("graph-submit").textContent = client?.pending ? "核实原请求" : "启动";
    $("graph-pending").textContent = client?.pending ? `待核实 · ${client.pending.body.idempotency_key}` : "";
    renderInputs(view);
    const nodes = $("graph-node-list"); nodes.replaceChildren();
    for (const node of view?.nodes ?? []) {
      const row = document.createElement("div"); row.className = "graph-progress-node"; row.dataset.status = node.status;
      const name = document.createElement("strong"), state = document.createElement("span");
      name.textContent = node.label; state.textContent = labels[node.status] ?? node.status; row.append(name, state);
      if (node.budget) {
        const budget = document.createElement("small");
        budget.textContent = `模型请求 ${node.budget.model_requests ?? 0}/${node.budget.max_model_requests} · 尝试 ${node.budget.attempts ?? 0}/${node.budget.max_model_attempts}`;
        row.append(budget);
      }
      if (node.diagnostic) { const detail = document.createElement("small"); detail.textContent = `${node.diagnostic.reason_code ?? node.diagnostic.code} · ${node.diagnostic.message}`; row.append(detail); }
      if (node.diagnostic?.failure_retry) {
        const detail = document.createElement("small"), permission = node.diagnostic.failure_retry;
        const reasons = {
          failed_retry_unavailable: "运行现场已不可用，可结束运行。",
          failed_retry_basis_changed: "对象或执行依据已变化，可结束运行后重新开始。",
          failed_retry_unsafe_outcome: "调用结果未知或不支持安全重试，可查询结果或结束运行。",
          failed_retry_evidence_incomplete: "缺少完整失败依据或已有工具效果，可查询结果或结束运行。",
          failed_retry_unsupported: "此失败不支持原运行继续，可结束运行后重新开始。"
        };
        detail.textContent = permission.allowed
          ? "可重新调用此节点，先前已接纳结果保留。"
          : reasons[permission.reason_code] ?? "此失败暂不能继续，可查询结果或结束运行。";
        row.append(detail);
      }
      nodes.append(row);
    }
    disposeMounts(outputMounts);
    const outputs = $("graph-outputs"); outputs.replaceChildren();
    for (const output of view?.outputs ?? []) outputs.append(outputArticle(output));
    if (!view?.outputs.length) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "暂无声明的公开端口"; outputs.append(empty); }
    const actions = $("graph-actions"); actions.replaceChildren();
    if (["failed", "recovery_unavailable", "closed"].includes(view?.status)) {
      const note = document.createElement("small");
      note.textContent = "已接纳的结果和对象写入保留；重新开始可能再次执行先前节点。";
      actions.append(note);
    }
    for (const action of view?.available_actions ?? []) {
      const button = document.createElement("button"); button.type = "button"; button.textContent = actionLabels[action];
      button.disabled = locked; button.onclick = () => void perform(action); actions.append(button);
    }
    $("graph-budget").hidden = !view?.available_actions.includes("extend_budget");
    for (const id of ["graph-requests", "graph-attempts"]) $(id).disabled = locked;
    const selector = $("graph-history-port"), selected = selector.value;
    selector.replaceChildren();
    const option = document.createElement("option"); option.value = ""; option.textContent = "选择公开端口"; selector.append(option);
    for (const output of view?.outputs ?? []) { const choice = document.createElement("option"); choice.value = `${output.node_binding_id}:${output.port_id}`; choice.textContent = `${output.label} / ${output.port_id}`; selector.append(choice); }
    if ([...selector.options].some(option => option.value === selected)) selector.value = selected;
    selector.disabled = !view?.outputs.length;
    if (view?.diagnostics.length) error(view.diagnostics.map(item => `${item.reason_code ?? item.code} · ${item.message}`).join("\n"));
    renderInformationControls();
    renderEvents();
  }
  function schedule() {
    clearTimeout(timer);
    if (client?.consumer && ["prepared", "running", "pausing"].includes(client.consumer.status))
      timer = setTimeout(() => void refresh(), 800);
  }
  async function refresh() {
    try { await client.refresh(); await loadEvents(); error(""); render(); schedule(); }
    catch (failure) { error(failure.message); render(); schedule(); }
  }
  async function loadDirectory() {
    const value = await client.readSessions();
    directory = value; const selector = $("graph-sessions"); selector.replaceChildren();
    const empty = document.createElement("option"); empty.value = ""; empty.textContent = "选择会话"; selector.append(empty);
    for (const row of directory) { const option = document.createElement("option"); option.value = row.workflow_session_id; option.textContent = `${row.workflow_session_id} · ${labels[row.status] ?? row.status}`; selector.append(option); }
    selector.value = client.sessionId ?? "";
  }
  async function perform(action) {
    if (localBusy || client?.busy) return;
    localBusy = true; error(""); rememberInputs(); render();
    try {
      const fields = action === "start" && !client.pending ? { inputs: Object.fromEntries(formInputs.map(row => [row.name,
        parseExternalInput(row.data_type, row.element.value)])) }
        : action === "create" ? { definition_revision: definition.definition_revision }
          : action === "extend_budget" ? { add_model_requests: Number($("graph-requests").value), add_model_attempts: Number($("graph-attempts").value) } : {};
      await client.command(action, fields);
      historyGeneration++; clearHistory();
      query.set("graph_session", client.sessionId); history.replaceState(null, "", `${location.pathname}?${query}${location.hash}`);
      await loadDirectory(); await client.refresh();
      await loadRegistrations();
      await loadEvents();
    } catch (failure) { error(failure.message); }
    finally { localBusy = false; render(); schedule(); }
  }
  async function readHistory() {
    const view = client?.consumer, selected = $("graph-history-port").value;
    const generation = ++historyGeneration, list = $("graph-history-list"); clearHistory();
    const port = view?.outputs.find(output => `${output.node_binding_id}:${output.port_id}` === selected);
    if (!port) return;
    try {
      const value = await client.readHistory(port);
      if (!value || generation !== historyGeneration || !client.consumer || view.workflow_session_id !== client.consumer.workflow_session_id
        || view.definition_revision !== client.consumer.definition_revision || view.session_revision !== client.consumer.session_revision) return;
      for (const output of value.outputs) list.append(outputArticle(output, true));
      if (!value.outputs.length) list.textContent = "暂无公开历史";
    } catch (failure) { if (generation === historyGeneration && view === client.consumer) list.textContent = failure.message; }
  }
  function renderCapabilities(application) {
    const container = $("graph-capabilities"); container.replaceChildren();
    for (const operation of [...application.commands, ...application.queries]) {
      const row = document.createElement("p");
      row.textContent = `${operation.kind === "command" ? "操作" : "查询"} · ${operation.name}`;
      container.append(row);
    }
  }
  function resetInformation() {
    informationViewGeneration++; informationCursor = null;
    $("graph-information-items").replaceChildren(); $("graph-information-status").textContent = "";
  }
  function selectedInformation() {
    return $("graph-information-source").value !== "" && client?.registrationsCurrent()
      ? client.registrations[Number($("graph-information-source").value)] : null;
  }
  function renderInformationScopes() {
    resetInformation();
    const binding = selectedInformation(), selector = $("graph-information-scope");
    selector.replaceChildren();
    if (binding?.kind === "information_binding" && binding.read_public) {
      for (const scope of ["live", "history"]) if ([scope, "live_and_history"].includes(binding.declaration.source_scope)) {
        const option = document.createElement("option"); option.value = scope; option.textContent = scope === "live" ? "活动来源" : "历史来源";
        selector.append(option);
      }
      if (binding.availability === "history") selector.value = "history";
    }
    renderInformationControls();
  }
  function renderInformationControls() {
    const current = client?.registrationsCurrent(), binding = selectedInformation();
    if (!current && $("graph-information-source").options.length) {
      resetInformation(); registrationCursor = null;
      $("graph-information-source").replaceChildren(); $("graph-information-scope").replaceChildren();
      $("graph-registrations").replaceChildren(); $("graph-registration-status").textContent = "目录已变化，请刷新";
    }
    $("graph-registration-refresh").disabled = !client || registrationBusy;
    $("graph-registration-next").disabled = registrationBusy || !current || !registrationCursor;
    $("graph-information-source").disabled = !current || informationBusy;
    $("graph-information-scope").disabled = !binding || !binding.read_public || informationBusy;
    const readable = binding?.kind === "information_binding" && binding.read_public && !!$("graph-information-scope").value;
    $("graph-information-read").disabled = !readable || informationBusy;
    $("graph-information-next").disabled = !readable || informationBusy || !informationCursor;
  }
  async function loadRegistrations(next = false) {
    if (registrationBusy) return;
    const session = client.sessionId;
    registrationBusy = true; resetInformation(); renderInformationControls();
    try {
      const value = await client.listRegistrations({ limit: 100, cursor: next ? registrationCursor : null });
      if (!value) return;
      registrationCursor = value.next_cursor;
      const container = $("graph-registrations"), selector = $("graph-information-source");
      container.replaceChildren(); selector.replaceChildren();
      const empty = document.createElement("option"); empty.value = ""; empty.textContent = "选择公开信息调用"; selector.append(empty);
      for (const [index, item] of value.items.entries()) {
        const detail = document.createElement("details"), summary = document.createElement("summary"), content = document.createElement("pre");
        const identity = JSON.stringify(item.registration_ref);
        summary.textContent = `${item.kind} · ${identity} · ${item.availability}`;
        content.textContent = JSON.stringify(item.declaration, null, 2); detail.append(summary, content); container.append(detail);
        if (item.kind === "information_binding") {
          const option = document.createElement("option"); option.value = String(index);
          option.textContent = `${item.declaration.channel_id} · ${item.owner.node_run_id} · 代次 ${item.generation}${item.read_public ? "" : " · 未授予读取"}`;
          selector.append(option);
        }
      }
      $("graph-registration-status").textContent = `${value.items.length} 项${value.next_cursor ? " · 还有下一页" : ""}`;
      renderInformationScopes();
    } catch (failure) {
      if (session === client.sessionId) $("graph-registration-status").textContent = failure.message;
    } finally { registrationBusy = false; renderInformationControls(); }
  }
  async function readInformation(next = false) {
    const binding = selectedInformation(), sourceScope = $("graph-information-scope").value;
    if (!binding || informationBusy) return;
    const generation = ++informationViewGeneration, context = client.registrationContext;
    informationBusy = true; $("graph-information-items").replaceChildren(); renderInformationControls();
    try {
      const value = await client.readInformation(binding, { source_scope: sourceScope, cursor: next ? informationCursor : null });
      if (!value || generation !== informationViewGeneration) return;
      informationCursor = value.next_cursor;
      $("graph-information-status").textContent = `${value.format_id}@${value.format_version} · ${value.status === "gap" ? "存在缺口" : value.status === "reset" ? "需要重新读取" : "当前页"} · ${value.items.length} 项`;
      for (const item of value.items) {
        appendThinking($("graph-information-items"), item);
        const content = document.createElement("pre"); content.textContent = JSON.stringify(GraphChat.modelPresentation(item), null, 2);
        $("graph-information-items").append(content);
      }
    } catch (failure) {
      if (generation === informationViewGeneration && context === client.registrationContext) {
        informationCursor = null; $("graph-information-status").textContent = failure.message;
      }
    } finally { informationBusy = false; renderInformationControls(); }
  }
  $("graph-composer").addEventListener("submit", event => { event.preventDefault(); void perform("start"); });
  $("graph-event-composer").addEventListener("submit", event => { event.preventDefault(); void performEvent(); });
  $("graph-events").onchange = renderEvents;
  $("graph-event-refresh").onclick = () => void loadEvents();
  $("graph-new-session").onclick = () => void perform("create");
  $("graph-refresh").onclick = () => void refresh();
  $("graph-history-port").onchange = () => void readHistory();
  $("graph-registration-refresh").onclick = () => void loadRegistrations();
  $("graph-registration-next").onclick = () => void loadRegistrations(true);
  $("graph-information-source").onchange = renderInformationScopes;
  $("graph-information-scope").onchange = () => { resetInformation(); renderInformationControls(); };
  $("graph-information-read").onclick = () => void readInformation();
  $("graph-information-next").onclick = () => void readInformation(true);
  $("graph-sessions").onchange = async event => {
    if (!event.target.value || localBusy || client.busy || client.pending) return;
    localBusy = true; rememberInputs(); clearTimeout(timer); historyGeneration++; clearHistory(); disposeMounts(outputMounts);
    try { await client.selectSession(event.target.value); query.set("graph_session", client.sessionId); history.replaceState(null, "", `${location.pathname}?${query}${location.hash}`); error(""); await loadRegistrations(); await loadEvents(); }
    catch (failure) { error(failure.message); }
    finally { localBusy = false; render(); schedule(); }
  };
  window.addEventListener("pagehide", () => { clearTimeout(timer); frontendHost?.dispose(); if (client) { client.generation++; client.invalidateInformation(); } historyGeneration++; resetInformation(); });
  try {
    if (!uuid(workflowId) || requestedSession !== null && !uuid(requestedSession)) throw new Error("工作流入口身份无效");
    client = new GraphChatClient({ workflowId, sessionId: requestedSession, storage: localStorage, request, keyFactory: () => crypto.randomUUID() });
    if (typeof WorkflowFrontendHost !== "undefined") {
      frontendHost = new WorkflowFrontendHost.ConsumerFrontendHost(client);
      if (typeof WorkflowFrontendPackage !== "undefined") WorkflowFrontendPackage.install(frontendHost);
    }
    renderCapabilities(await client.discover()); definition = await client.readDefinition();
    $("graph-name").textContent = definition.name; await loadDirectory(); await client.refresh(); render(); schedule();
    await loadRegistrations();
    await loadEvents();
  } catch (failure) { error(failure.message); }
})();
