"use strict";

((root) => {
  const MAX_TEXT = 1000000;
  const type = "TAVERN_CHAT_DISPLAY";
  const namespace = "workflow.tavern:";
  const clone = value => GraphChat.clone(value);
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  function storageForTavern(storage) {
    return {
      getItem: key => storage.getItem(namespace + key),
      setItem: (key, value) => storage.setItem(namespace + key, value),
    };
  }
  function textInput(view) {
    if (!view) return { input: null, diagnostic: null };
    if (view.inputs.length !== 1 || view.inputs[0].data_type !== "TEXT")
      return { input: null, diagnostic: "此聊天页需要唯一的 TEXT 输入；请在工作台调整工作流。" };
    return { input: clone(view.inputs[0]), diagnostic: null };
  }
  function inputsFor(view, text) {
    const adaptation = textInput(view);
    if (!adaptation.input) throw new Error(adaptation.diagnostic ?? "先选择会话");
    if (typeof text !== "string" || text.length > MAX_TEXT) throw new Error("消息超出文本输入范围");
    if (!text.trim()) throw new Error("消息不能为空");
    return { [adaptation.input.name]: text };
  }
  function displayPorts(view) {
    return (view?.outputs ?? []).filter(output => output.data_type === type
      && (output.data_schema_version ?? output.payload?.schema_version ?? 1) === 1);
  }
  function collectEntries(outputs) {
    const ordered = new Map();
    for (const output of outputs) {
      if (output.data_type !== type || output.availability !== "produced") continue;
      GraphChat.validateContent(output.payload, type, 1);
      for (const entry of output.payload.entries) {
        const previous = ordered.get(entry.entry_id);
        if (previous && !same(previous.entry, entry)) throw new Error("聊天展示条目身份冲突");
        ordered.set(entry.entry_id, { entry: clone(entry), output });
      }
    }
    return [...ordered.values()];
  }
  async function transcript(client, cache = new Map()) {
    const view = client.consumer;
    if (!view) return null;
    const ports = displayPorts(view);
    if (ports.length > 1) throw new Error("第一版聊天页需要唯一的公开酒馆展示端口");
    const outputs = [];
    for (const port of ports) {
      const history = await client.readHistory(port);
      if (!history || view !== client.consumer) return null;
      outputs.push(...history.outputs, port);
    }
    const rows = collectEntries(outputs), messages = [];
    for (const row of rows) {
      if (view !== client.consumer) return null;
      const key = JSON.stringify(row.entry.source_ref);
      let resolved = cache.get(key);
      if (resolved === undefined) {
        const artifact = await client.readDisplayEntry(row.output, row.entry);
        if (!artifact || view !== client.consumer) return null;
        if (artifact.data_type !== "TEXT" || artifact.data_schema_version !== 2)
          throw new Error("聊天条目正文需要 TEXT@2");
        resolved = { text: GraphChat.displayEntryText(artifact), producer: clone(artifact.producer) };
        cache.set(key, resolved);
      }
      messages.push({ entry_id: row.entry.entry_id, role: row.entry.role, text: resolved.text,
        source_ref: clone(row.entry.source_ref), producer: clone(resolved.producer) });
    }
    return messages;
  }
  function roundTargets(messages, packet) {
    const targets = new Map();
    if (!packet?.can_fork) return targets;
    const groups = new Map();
    for (const [index, message] of messages.entries()) {
      const producer = message.producer;
      if (!producer || !GraphChat.uuid(producer.workflow_session_id) || !GraphChat.uuid(producer.chain_run_id)) continue;
      const key = JSON.stringify([producer.workflow_session_id, producer.chain_run_id]);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push({ message, index });
    }
    let round = 0;
    for (const [key, members] of groups) {
      const candidate = packet.candidates.find(row =>
        JSON.stringify([row.source_session_id, row.chain_run_id]) === key);
      if (!candidate) continue;
      const target = { candidate: clone(candidate), round: ++round,
        records: members.map(row => row.index + 1) };
      for (const member of members) targets.set(member.message.entry_id, target);
    }
    return targets;
  }
  function forkConfirmation(target) {
    return `从第 ${target.round} 个完成回合检查点创建分叉？\n\n`
      + `相关展示记录：${target.records.map(number => "#" + number).join(", ")}\n`
      + `完成回合：${target.candidate.chain_run_id}\n`
      + `检查点：${target.candidate.candidate_id}\n`
      + `工作流定义 r${target.candidate.source_definition_revision}\n\n`
      + "新会话继承该完整回合的检查点；父会话不变。";
  }
  function createRequest(fetcher) {
    const allowed = new Map([
      ["/api/graph/consumer/application", "GET"],
      ["/api/graph/consumer/queries", "POST"],
      ["/api/graph/consumer/commands", "POST"],
      ["/api/graph/consumer/receipts/read", "POST"],
    ]);
    return async (path, options = {}) => {
      if (allowed.get(path) !== (options.method ?? "GET")) throw new Error("酒馆传输入口未授权");
      const mutation = path === "/api/graph/consumer/commands";
      let response;
      try {
        response = await fetcher(path, { ...options, credentials: "same-origin", cache: "no-store",
          headers: { "Content-Type": "application/json" } });
      } catch {
        throw new Error(mutation ? "请求结果未知，请核实原请求" : "公开读取失败，请刷新");
      }
      let value;
      try { value = await response.json(); } catch {
        throw new Error(mutation ? "响应内容未知，请核实原请求" : "公开读取响应无效，请刷新");
      }
      if (!response.ok) throw GraphChat.httpFailure(value, response.status);
      return value;
    };
  }
  function createClient({ storage, fetcher, ...options }) {
    return new GraphChat.GraphChatClient({ ...options, storage: storageForTavern(storage),
      request: createRequest(fetcher) });
  }
  root.TavernAdapter = { MAX_TEXT, type, namespace, storageForTavern, textInput, inputsFor,
    displayPorts, collectEntries, transcript, roundTargets, forkConfirmation, createRequest, createClient };
})(globalThis);
