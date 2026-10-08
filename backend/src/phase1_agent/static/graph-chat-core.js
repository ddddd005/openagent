"use strict";

((root) => {
  const uuid = (value) => typeof value === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
  const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const exact = (value, fields) => object(value) && Object.keys(value).length === fields.length
    && fields.every((field) => Object.hasOwn(value, field));
  const positive = (value) => Number.isSafeInteger(value) && value > 0;
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const freeze = value => {
    if (value && typeof value === "object") {
      Object.values(value).forEach(freeze); Object.freeze(value);
    }
    return value;
  };
  const actions = new Set(["pause", "resume", "close", "extend_budget", "retry_archive", "retry_acceptance", "retry_failed_node"]);
  const consumerQueries = new Set(["consumer.definition", "consumer.sessions", "consumer.read", "consumer.actions",
    "output.list", "output.read", "output.history", "output.artifact.read", "registration.list", "information.read",
    "frontend.extensions.read", "consumer.event.bindings", "consumer.event.read", "consumer.candidate.list", "receipt.read"]);
  const bounded = value => typeof value === "string" && value.length > 0 && value.length <= 128 && value.trim() === value;
  const cursor = value => value === null || typeof value === "string" && value.length > 0 && value.length <= 4096;
  const same = (left, right) => {
    if (left === right) return true;
    if (Array.isArray(left)) return Array.isArray(right) && left.length === right.length && left.every((item, index) => same(item, right[index]));
    return object(left) && object(right) && Object.keys(left).length === Object.keys(right).length
      && Object.keys(left).every(key => Object.hasOwn(right, key) && same(left[key], right[key]));
  };
  function strictJson(value, ancestors = new Set()) {
    if (value === null || typeof value === "string" || typeof value === "boolean") return true;
    if (typeof value === "number") return Number.isFinite(value);
    if (typeof value !== "object" || ancestors.has(value) || Object.getOwnPropertySymbols(value).length) return false;
    const prototype = Object.getPrototypeOf(value);
    if (!Array.isArray(value) && prototype !== null
      && (Object.getPrototypeOf(prototype) !== null || prototype.constructor?.name !== "Object")) return false;
    ancestors.add(value);
    const descriptors = Object.getOwnPropertyDescriptors(value);
    const valid = Array.isArray(value)
      ? prototype?.constructor?.name === "Array" && Object.getOwnPropertyNames(value).length === value.length + 1
        && Object.keys(value).length === value.length && Array.from({ length: value.length }, (_, index) => {
        const descriptor = descriptors[String(index)];
        return descriptor && descriptor.enumerable && Object.hasOwn(descriptor, "value") && strictJson(descriptor.value, ancestors);
      }).every(Boolean)
      : Object.values(descriptors).every(descriptor => descriptor.enumerable && Object.hasOwn(descriptor, "value")
        && strictJson(descriptor.value, ancestors));
    ancestors.delete(value); return valid;
  }
  function require(value, reason) { if (!value) throw new Error(reason); }
  function supportedContent(type, version) {
    return ["TEXT", "PROMPT", "JSON"].includes(type) && [1, 2].includes(version)
      || ["MODEL_RESOURCE", "FRONTEND_DISPLAY", "TAVERN_CHAT_DISPLAY"].includes(type) && version === 1;
  }
  function identity(value, workflow, session) {
    return value.workflow_definition_id === workflow && (!session || value.workflow_session_id === session)
      && uuid(value.workflow_session_id) && positive(value.definition_revision);
  }
  function validateSource(value) {
    return exact(value, ["workflow_definition_id", "definition_revision", "workflow_session_id",
      "node_binding_id", "port_id", "run_id", "chain_run_id"])
      && ["workflow_definition_id", "workflow_session_id", "node_binding_id", "run_id", "chain_run_id"].every(key => uuid(value[key]))
      && positive(value.definition_revision) && typeof value.port_id === "string";
  }
  function validateContent(value, type, version) {
    require(object(value) && [1, 2].includes(value.schema_version)
      && (version === undefined || value.schema_version === version), "公开内容封套无效");
    if (type === "TEXT") require(exact(value, ["schema_version", "kind", "text"])
      && value.kind === "workflow.text" && typeof value.text === "string", "TEXT 公开内容无效");
    else if (type === "PROMPT") {
      require(exact(value, ["schema_version", "kind", "stage", "items", "assembly"])
        && value.kind === "workflow.prompt" && ["materials", "assembled"].includes(value.stage)
        && Array.isArray(value.items) && (value.stage === "materials" ? value.assembly === null
          : value.schema_version === 1 ? exact(value.assembly, ["schema_version", "kind", "prompt", "current_root", "context_basis"])
            && value.assembly.schema_version === 1 && value.assembly.kind === "workflow.prompt-assembly"
            && object(value.assembly.prompt) && object(value.assembly.current_root) && Array.isArray(value.assembly.context_basis)
          : validateV2Assembly(value.assembly)), "PROMPT 公开内容无效");
      const ids = new Set();
      for (const item of value.items) {
        require(exact(item, ["item_instance_id", "text", "role", "placement", "depth", "order", "enabled",
          "purpose", "source", "protected", "metadata"]) && uuid(item.item_instance_id) && !ids.has(item.item_instance_id)
          && typeof item.text === "string" && ["system", "user", "assistant", "tool"].includes(item.role)
          && ["before", "middle", "after"].includes(item.placement) && Number.isSafeInteger(item.order)
          && typeof item.enabled === "boolean" && typeof item.protected === "boolean" && typeof item.purpose === "string"
          && !!item.purpose && item.purpose.length <= 128 && object(item.source) && Object.keys(item.source).length > 0
          && object(item.metadata) && (item.role !== "tool" || item.protected)
          && (item.placement === "middle" ? Number.isSafeInteger(item.depth) && item.depth >= 0 : item.depth === null), "PROMPT 材料结构无效");
        ids.add(item.item_instance_id);
      }
    } else if (type === "JSON") require(exact(value, ["schema_version", "kind", "value"])
      && value.kind === "workflow.json", "JSON 公开内容无效");
    else if (type === "MODEL_RESOURCE") require(value.schema_version === 1 && exact(value, ["schema_version", "kind", "binding"])
      && value.kind === "workflow.model-resource" && exact(value.binding, ["node_id", "provider", "credential_evidence", "parameters"])
      && uuid(value.binding.node_id) && object(value.binding.provider) && uuid(value.binding.provider.provider_id)
      && positive(value.binding.provider.revision) && typeof value.binding.provider.name === "string"
      && /^[0-9a-f]{64}$/.test(value.binding.credential_evidence) && object(value.binding.parameters), "模型资源引用无效");
    else if (["FRONTEND_DISPLAY", "TAVERN_CHAT_DISPLAY"].includes(type)) {
      require(exact(value, ["schema_version", "kind", "entries"]) && value.schema_version === 1
        && value.kind === (type === "FRONTEND_DISPLAY" ? "workflow.frontend-display" : "workflow.tavern-chat-display")
        && Array.isArray(value.entries) && value.entries.length <= 4095,
      "前端展示声明无效");
      const ids = new Set();
      for (const entry of value.entries) {
        require(exact(entry, ["entry_id", "role", "source_ref"]) && uuid(entry.entry_id) && !ids.has(entry.entry_id)
          && ["user", "assistant", "system"].includes(entry.role) && validateArtifactReference(entry.source_ref), "前端展示条目无效");
        ids.add(entry.entry_id);
      }
    } else throw new Error("公开端口数据类型不受支持");
    return value;
  }
  function validateV2Assembly(value) {
    const refs = (items) => Array.isArray(items) && items.length <= 4096 && items.every(item =>
      exact(item, ["edge_id", "output_id", "order"]) && uuid(item.edge_id) && uuid(item.output_id)
      && Number.isSafeInteger(item.order) && item.order >= 0)
      && new Set(items.map(item => item.edge_id)).size === items.length;
    return exact(value, ["schema_version", "kind", "messages", "manifest", "current_input"])
      && value.schema_version === 2 && value.kind === "workflow.prompt-assembly"
      && Array.isArray(value.messages) && value.messages.length > 0 && value.messages.every(message =>
        exact(message, ["role", "content"]) && ["system", "user", "assistant", "tool"].includes(message.role)
        && typeof message.content === "string")
      && exact(value.manifest, ["ordered_item_ids", "source_output_refs", "current_input_refs"])
      && Array.isArray(value.manifest.ordered_item_ids) && value.manifest.ordered_item_ids.every(uuid)
      && new Set(value.manifest.ordered_item_ids).size === value.manifest.ordered_item_ids.length
      && refs(value.manifest.source_output_refs) && refs(value.manifest.current_input_refs)
      && value.manifest.current_input_refs.length <= 1
      && (value.current_input === null ? value.manifest.current_input_refs.length === 0
        : exact(value.current_input, ["role", "content"]) && value.current_input.role === "user"
          && typeof value.current_input.content === "string");
  }
  function displayText(payload) {
    if (payload.kind === "workflow.text") return payload.text;
    if (payload.kind === "workflow.prompt" && payload.schema_version === 2) {
      validateContent(payload, "PROMPT");
      return payload.stage === "assembled" ? payload.assembly.messages.map(message => message.content).join("\n\n")
        : payload.items.filter(item => item.enabled).map(item => item.text).join("\n\n");
    }
    if (payload.kind === "workflow.model-resource") return JSON.stringify({ provider_id: payload.binding.provider.provider_id,
      revision: payload.binding.provider.revision, name: payload.binding.provider.name, model: payload.binding.parameters.model }, null, 2);
    return JSON.stringify(payload, null, 2);
  }
  function displayEntryText(value) {
    if (value.data_type === "TEXT" && value.data_schema_version === 2)
      return validateContent(value.value, "TEXT", 2).text;
    return `不支持的正文格式 · ${value.data_type}@${value.data_schema_version}`;
  }
  function httpFailure(value, status) {
    const detail = object(value?.error) ? value.error : object(value) ? value : {};
    const code = detail.code ?? detail.reason_code ?? detail.diagnostic?.reason_code ?? status;
    const message = detail.message ?? detail.diagnostic?.message ?? "请求未完成";
    const failure = new Error(`${code} · ${message}`);
    failure.definite = [400, 403, 404, 409, 422].includes(status);
    return failure;
  }
  function parseExternalInput(type, text) {
    if (type === "TEXT") return text;
    require(type === "PROMPT", "输入数据类型不受支持");
    let value;
    try { value = JSON.parse(text); } catch { throw new Error("PROMPT 输入须为有效 JSON"); }
    return validateContent(value, "PROMPT");
  }
  function validateExternalInputs(inputs) {
    require(object(inputs), "本机输入请求无效");
    for (const value of Object.values(inputs)) if (typeof value !== "string") validateContent(value, "PROMPT");
    return inputs;
  }
  function validateOutput(value) {
    require(exact(value, ["node_binding_id", "port_id", "data_type", "label", "is_output", "status",
      "availability", "reason_code", "run_id", "chain_run_id", "payload", "source",
      ...(Object.hasOwn(value, "data_schema_version") ? ["data_schema_version"] : [])])
      && uuid(value.node_binding_id) && typeof value.port_id === "string" && !!value.port_id
      && typeof value.data_type === "string" && typeof value.label === "string" && typeof value.is_output === "boolean"
      && (value.data_schema_version === undefined || positive(value.data_schema_version))
      && typeof value.status === "string" && ["produced", "unproduced", "unavailable"].includes(value.availability)
      && (value.run_id === null || uuid(value.run_id)) && (value.chain_run_id === null || uuid(value.chain_run_id)), "公开端口结构无效");
    if (value.source !== null) require(validateSource(value.source) && value.source.port_id === value.port_id
      && value.source.run_id === value.run_id && value.source.chain_run_id === value.chain_run_id, "公开结果来源无效");
    if (value.availability === "produced") require(value.reason_code === null && value.source !== null, "公开结果来源无效");
    else require(value.payload === null
      && (value.reason_code === null || typeof value.reason_code === "string"), "未产出端口不应携带结果");
    if (value.availability === "produced") {
      const version = value.data_schema_version ?? (object(value.payload) ? value.payload.schema_version : undefined);
      if (["TEXT", "PROMPT", "JSON", "MODEL_RESOURCE", "FRONTEND_DISPLAY", "TAVERN_CHAT_DISPLAY"].includes(value.data_type)
        && (version === undefined || supportedContent(value.data_type, version))) {
        validateContent(value.payload, value.data_type, value.data_schema_version);
      }
    }
    return value;
  }
  function validateConsumer(value, workflow, session) {
    require(exact(value, ["schema_version", "kind", "workflow_definition_id", "definition_revision",
      "workflow_session_id", "session_revision", "status", "can_submit", "available_actions",
      "inputs", "nodes", "outputs", "history", "diagnostics"])
      && value.schema_version === 1 && value.kind === "workflow.consumer" && identity(value, workflow, session)
      && positive(value.session_revision) && typeof value.status === "string" && typeof value.can_submit === "boolean"
      && Array.isArray(value.available_actions) && value.available_actions.every(action => actions.has(action))
      && Array.isArray(value.inputs) && Array.isArray(value.nodes) && Array.isArray(value.outputs)
      && Array.isArray(value.history) && Array.isArray(value.diagnostics), "工作流消费声明身份或结构无效");
    const names = new Set();
    for (const input of value.inputs) {
      require(exact(input, ["name", "data_type", "required", "node_ids"]) && typeof input.name === "string"
        && !!input.name && !names.has(input.name) && ["TEXT", "PROMPT"].includes(input.data_type) && typeof input.required === "boolean"
        && Array.isArray(input.node_ids) && input.node_ids.length > 0 && input.node_ids.every(uuid), "输入声明无效");
      names.add(input.name);
    }
    const nodes = new Set();
    for (const node of value.nodes) {
      require(exact(node, ["node_binding_id", "label", "status", "run_id", "revision", "diagnostic", "budget"])
        && uuid(node.node_binding_id) && !nodes.has(node.node_binding_id) && typeof node.label === "string"
        && typeof node.status === "string" && (node.run_id === null || uuid(node.run_id))
        && (node.revision === null || positive(node.revision))
        && (node.diagnostic === null || object(node.diagnostic)) && (node.budget === null || object(node.budget)), "节点观察无效");
      nodes.add(node.node_binding_id);
    }
    const ports = new Set();
    for (const output of value.outputs) {
      validateOutput(output);
      const key = output.node_binding_id + ":" + output.port_id;
      require(nodes.has(output.node_binding_id) && !ports.has(key), "公开端口重复或无节点");
      if (output.source) require(value.history.some(chain => chain.workflow_definition_id === output.source.workflow_definition_id
        && chain.workflow_session_id === output.source.workflow_session_id && chain.definition_revision === output.source.definition_revision
        && chain.chain_run_id === output.source.chain_run_id), "公开结果不在当前会话授权历史范围内");
      ports.add(key);
    }
    for (const history of value.history) require(exact(history, ["workflow_definition_id", "definition_revision",
      "workflow_session_id", "chain_run_id", "status", "revision", "inherited"])
      && uuid(history.workflow_definition_id) && uuid(history.workflow_session_id) && uuid(history.chain_run_id)
      && positive(history.definition_revision) && positive(history.revision) && typeof history.status === "string"
      && typeof history.inherited === "boolean", "公开运行历史无效");
    return value;
  }
  function validateOperationReceipt(receipt, command, workflow) {
    const fork = command.action === "fork";
    const operation = command.action === "create" ? "create" : command.action === "start" ? "start" : command.action === "event" ? "event" : fork ? "fork" : "control";
    require(exact(receipt, ["workflow_definition_id", "definition_revision", "workflow_session_id", "session_revision",
      "chain_run_id", "status", "idempotency_key", "operation", ...(fork ? ["fork_source"] : [])])
      && identity(receipt, workflow, fork ? null : command.workflow_session_id) && positive(receipt.session_revision)
      && receipt.idempotency_key === command.body.idempotency_key && receipt.operation === operation
      && typeof receipt.status === "string" && (receipt.chain_run_id === null || uuid(receipt.chain_run_id)), "提交回执与原请求不匹配");
    if (fork) {
      const candidate = command.candidate;
      require(exact(receipt.fork_source, ["workflow_session_id", "candidate_id", "chain_run_id",
        "workflow_definition_id", "definition_revision"])
        && receipt.fork_source.workflow_session_id === command.workflow_session_id
        && receipt.fork_source.candidate_id === candidate.candidate_id
        && receipt.fork_source.chain_run_id === candidate.chain_run_id
        && receipt.fork_source.workflow_definition_id === candidate.source_workflow_definition_id
        && receipt.fork_source.definition_revision === candidate.source_definition_revision
        && receipt.workflow_session_id !== command.workflow_session_id
        && receipt.workflow_definition_id === candidate.source_workflow_definition_id
        && receipt.definition_revision === candidate.source_definition_revision
        && receipt.chain_run_id === null && receipt.status === "succeeded" && receipt.session_revision === 1,
      "分叉回执与原检查点不匹配");
    }
    if (command.action === "create") require(receipt.definition_revision === command.body.definition_revision, "新会话定义版本不匹配");
    if (["start", "event"].includes(command.action)) require(uuid(receipt.chain_run_id), "启动回执缺少运行身份");
    if (command.action === "event") require(receipt.definition_revision === command.body.definition_revision, "事件回执定义版本不匹配");
    return receipt;
  }
  function validateReceipt(value, command, workflow) {
    require(exact(value, ["schema_version", "kind", "receipt", "consumer"])
      && value.schema_version === 1 && value.kind === "workflow.consumer.receipt", "提交回执无效，原请求待核实");
    const receipt = validateOperationReceipt(value.receipt, command, workflow);
    validateConsumer(value.consumer, workflow, receipt.workflow_session_id);
    require(value.consumer.session_revision >= receipt.session_revision, "回执观察早于提交依据");
    if (command.action === "fork") require(value.consumer.definition_revision === receipt.definition_revision
      && value.consumer.history.some(chain => chain.chain_run_id === command.candidate.chain_run_id
        && chain.workflow_session_id === command.candidate.source_session_id
        && chain.workflow_definition_id === command.candidate.source_workflow_definition_id
        && chain.definition_revision === command.candidate.source_definition_revision && chain.status === "succeeded"),
    "分叉会话没有原完成回合依据");
    return value;
  }
  function validateCommand(command, workflow) {
    require(exact(command, ["path", "body", "action", "workflow_session_id",
      ...(command?.action === "fork" ? ["candidate"] : [])]) && object(command.body)
      && uuid(command.body.idempotency_key), "本机待核实请求无效");
    if (command.action === "create") require(command.path === "/api/graph/consumer/sessions"
      && command.workflow_session_id === null && exact(command.body, ["workflow_definition_id", "definition_revision", "idempotency_key"])
      && command.body.workflow_definition_id === workflow && positive(command.body.definition_revision), "本机新会话请求无效");
    else if (command.action === "fork") {
      require(uuid(command.workflow_session_id)
        && command.path === `/api/graph/sessions/${command.workflow_session_id}/consumer/candidates/fork`
        && exact(command.body, ["candidate_id", "expected_revision", "expected_data_revision",
          "expected_head_revision", "idempotency_key"])
        && positive(command.body.expected_revision) && positive(command.body.expected_head_revision)
        && Number.isSafeInteger(command.body.expected_data_revision) && command.body.expected_data_revision >= 0
        && validateCandidate(command.candidate, workflow)
        && command.body.candidate_id === command.candidate.candidate_id, "本机分叉请求或检查点归属无效");
    } else {
      require(uuid(command.workflow_session_id) && command.path === `/api/graph/sessions/${command.workflow_session_id}/consumer/${command.action === "start" ? "runs" : command.action === "event" ? "events/submit" : "control"}`
        && positive(command.body.expected_revision), "本机执行请求归属无效");
      if (command.action === "start") {
        require(exact(command.body, ["expected_revision", "idempotency_key", "inputs"]), "本机输入请求无效");
        validateExternalInputs(command.body.inputs);
      }
      else if (command.action === "event") require(exact(command.body, ["workflow_definition_id", "definition_revision",
        "event_id", "event_schema_version", "payload", "expected_revision", "idempotency_key"])
        && command.body.workflow_definition_id === workflow && positive(command.body.definition_revision)
        && bounded(command.body.event_id) && positive(command.body.event_schema_version)
        && object(command.body.payload) && strictJson(command.body.payload)
        && !Object.hasOwn(command.body.payload, "_workflow_frozen_resources"), "本机事件请求无效");
      else require(actions.has(command.action) && command.body.action === command.action
        && exact(command.body, ["action", "expected_revision", "idempotency_key",
          ...(command.action === "extend_budget" ? ["add_model_requests", "add_model_attempts"] : [])])
        && (command.action !== "extend_budget" || ["add_model_requests", "add_model_attempts"].every(key => Number.isSafeInteger(command.body[key]) && command.body[key] >= 0)), "本机控制请求无效");
    }
    return command;
  }
  function commandEnvelope(command, workflow) {
    validateCommand(command, workflow);
    return { operation: command.action === "create" ? "consumer.session.create"
      : command.action === "start" ? "consumer.run.start" : command.action === "event" ? "consumer.event.submit"
        : command.action === "fork" ? "consumer.candidate.fork" : "consumer.run.control",
    parameters: { ...clone(command.body), ...(command.workflow_session_id ? { session_id: command.workflow_session_id } : {}) } };
  }
  function validateApplicationReceiptIdentity(receipt, command, workflow) {
    const envelope = commandEnvelope(command, workflow);
    require(exact(receipt, ["schema_version", "kind", "operation", "operation_scope", "idempotency_key",
      "request_sha256", "authority", "target", "accepted"]) && receipt.schema_version === 1
      && receipt.kind === "workflow.application-receipt" && receipt.operation === envelope.operation
      && receipt.operation_scope === "consumer" && receipt.idempotency_key === command.body.idempotency_key
      && typeof receipt.request_sha256 === "string" && /^[0-9a-f]{64}$/.test(receipt.request_sha256)
      && receipt.authority === "service_receipt"
      && same(receipt.target, command.workflow_session_id ? { session_id: command.workflow_session_id } : {}),
    "应用提交回执与原请求不匹配");
    return receipt;
  }
  function validateApplicationReceipt(value, command, workflow) {
    require(exact(value, ["schema_version", "kind", "receipt", "result"])
      && value.schema_version === 1 && value.kind === "workflow.application-command", "应用提交回执无效，原请求待核实");
    const receipt = validateApplicationReceiptIdentity(value.receipt, command, workflow);
    const result = validateReceipt(value.result, command, workflow);
    require(same(receipt.accepted, result.receipt), "应用接纳依据与操作回执不匹配");
    return result;
  }
  function validateApplicationReceiptRead(value, command, workflow) {
    require(object(value) && value.schema_version === 1 && value.kind === "workflow.application-receipt-read",
      "原请求核实回执无效，保留原请求");
    if (value.outcome === "unresolved") {
      require(exact(value, ["schema_version", "kind", "outcome", "reason_code"]) && bounded(value.reason_code),
        "原请求核实诊断无效，保留原请求");
      throw new Error(`原请求尚无法核实 [${value.reason_code}]，保留原请求`);
    }
    require(exact(value, ["schema_version", "kind", "outcome", "reason_code", "receipt", "result"])
      && value.outcome === "matched" && value.reason_code === "receipt_matched"
      && exact(value.result, ["receipt"]), "原请求核实回执无效，保留原请求");
    const receipt = validateApplicationReceiptIdentity(value.receipt, command, workflow);
    validateOperationReceipt(value.result.receipt, command, workflow);
    require(same(receipt.accepted, value.result.receipt), "应用接纳依据与原操作回执不匹配，保留原请求");
    return value.result;
  }
  function validateCandidate(value, workflow) {
    return exact(value, ["candidate_id", "chain_run_id", "source_session_id",
      "source_workflow_definition_id", "source_definition_revision"])
      && ["candidate_id", "chain_run_id", "source_session_id", "source_workflow_definition_id"].every(key => uuid(value[key]))
      && value.source_workflow_definition_id === workflow && positive(value.source_definition_revision);
  }
  function validateCandidates(value, consumer) {
    require(exact(value, ["schema_version", "kind", "workflow_definition_id", "definition_revision",
      "workflow_session_id", "session_revision", "data_revision", "head_revision", "status", "can_fork", "candidates"])
      && value.schema_version === 1 && value.kind === "workflow.consumer-candidates"
      && identity(value, consumer.workflow_definition_id, consumer.workflow_session_id)
      && value.definition_revision === consumer.definition_revision && value.session_revision === consumer.session_revision
      && Number.isSafeInteger(value.data_revision) && value.data_revision >= 0 && positive(value.head_revision)
      && value.status === consumer.status && typeof value.can_fork === "boolean" && Array.isArray(value.candidates),
    "分叉目录归属或依据无效");
    require(!value.can_fork || !["prepared", "running", "pausing", "paused"].includes(consumer.status),
      "活动会话不能分叉");
    const ids = new Set(), chains = new Set();
    for (const candidate of value.candidates) {
      require(validateCandidate(candidate, consumer.workflow_definition_id) && !ids.has(candidate.candidate_id)
        && !chains.has(candidate.chain_run_id) && consumer.history.some(chain =>
        chain.chain_run_id === candidate.chain_run_id && chain.workflow_session_id === candidate.source_session_id
        && chain.workflow_definition_id === candidate.source_workflow_definition_id
        && chain.definition_revision === candidate.source_definition_revision && chain.status === "succeeded"),
      "分叉检查点不在当前可用完成回合中");
      ids.add(candidate.candidate_id); chains.add(candidate.chain_run_id);
    }
    return value;
  }
  function validateEventBinding(binding) {
    require(exact(binding, ["event_id", "schema_version", "display_name", "audience", "target_node_ids", "payload_schema"])
      && bounded(binding.event_id) && positive(binding.schema_version) && typeof binding.display_name === "string"
      && !!binding.display_name.trim() && binding.display_name.length <= 128
      && ["management", "consumer"].includes(binding.audience) && Array.isArray(binding.target_node_ids)
      && binding.target_node_ids.length > 0 && binding.target_node_ids.length <= 512 && binding.target_node_ids.every(uuid)
      && new Set(binding.target_node_ids).size === binding.target_node_ids.length
      && object(binding.payload_schema) && binding.payload_schema.type === "object" && strictJson(binding.payload_schema), "事件绑定契约无效");
    function local(value) {
      if (!object(value)) return true;
      if (["$ref", "$dynamicRef", "$recursiveRef"].some(key => Object.hasOwn(value, key)
        && (typeof value[key] !== "string" || !value[key].startsWith("#")))) return false;
      const children = [];
      for (const key of ["$defs", "definitions", "properties", "patternProperties", "dependentSchemas"])
        if (object(value[key])) children.push(...Object.values(value[key]));
      for (const key of ["additionalProperties", "unevaluatedProperties", "propertyNames", "contains", "items",
        "additionalItems", "unevaluatedItems", "not", "if", "then", "else"]) if (Object.hasOwn(value, key)) children.push(value[key]);
      for (const key of ["allOf", "anyOf", "oneOf", "prefixItems", "items"]) if (Array.isArray(value[key])) children.push(...value[key]);
      if (object(value.dependencies)) children.push(...Object.values(value.dependencies).filter(object));
      return children.every(local);
    }
    require(local(binding.payload_schema), "事件 schema 只能使用本地引用");
    return binding;
  }
  function validateEventBindings(value, workflow, session, definition) {
    require(exact(value, ["schema_version", "kind", "workflow_definition_id", "definition_revision",
      "workflow_session_id", "session_revision", "can_submit", "bindings"])
      && value.schema_version === 1 && value.kind === "workflow.event-bindings" && identity(value, workflow, session)
      && value.definition_revision === definition && positive(value.session_revision)
      && typeof value.can_submit === "boolean" && Array.isArray(value.bindings) && value.bindings.length <= 512, "公开事件目录身份或契约无效");
    const seen = new Set();
    for (const binding of value.bindings) {
      validateEventBinding(binding);
      const key = JSON.stringify([binding.event_id, binding.schema_version]);
      require(binding.audience === "consumer" && !seen.has(key), "公开事件重复或未授权");
      seen.add(key);
    }
    return value;
  }
  function validateEventRun(value, workflow, session, chain, definition) {
    require(exact(value, ["schema_version", "kind", "workflow_definition_id", "definition_revision", "workflow_session_id",
      "session_revision", "chain_run_id", "execution_kind", "event", "status", "revision", "targets", "ordered_nodes",
      "completed_nodes", "next_node_index", "diagnostic", "source"]) && value.schema_version === 1 && value.kind === "workflow.event-run"
      && identity(value, workflow, session) && positive(value.session_revision) && value.chain_run_id === chain
      && (definition === undefined || value.definition_revision === definition)
      && exact(value.source, ["workflow_definition_id", "definition_revision", "workflow_session_id"])
      && uuid(value.source.workflow_definition_id) && uuid(value.source.workflow_session_id) && positive(value.source.definition_revision)
      && uuid(chain) && value.execution_kind === "event" && typeof value.status === "string" && positive(value.revision)
      && exact(value.event, ["event_id", "schema_version", "audience", "idempotency_key"])
      && bounded(value.event.event_id) && positive(value.event.schema_version) && value.event.audience === "consumer"
      && bounded(value.event.idempotency_key) && ["targets", "ordered_nodes", "completed_nodes"].every(key =>
        Array.isArray(value[key]) && value[key].length <= 512 && value[key].every(uuid) && new Set(value[key]).size === value[key].length)
      && value.targets.length > 0
      && Number.isSafeInteger(value.next_node_index) && value.next_node_index >= 0
      && value.next_node_index <= value.ordered_nodes.length
      && value.targets.every(id => value.ordered_nodes.includes(id)) && value.completed_nodes.every(id => value.ordered_nodes.includes(id))
      && (value.diagnostic === null || exact(value.diagnostic, ["reason_code", "message"])
        && bounded(value.diagnostic.reason_code) && value.diagnostic.message === "Event execution requires attention"),
    "公开事件运行概况无效");
    return value;
  }
  function validateApplication(value) {
    require(exact(value, ["schema_version", "kind", "scope", "boundary", "commands", "queries"])
      && value.schema_version === 1 && value.kind === "workflow.application" && value.scope === "consumer"
      && object(value.boundary) && value.boundary.caller === "trusted_local"
      && value.boundary.remote_plugin_authentication === false
      && Array.isArray(value.commands) && Array.isArray(value.queries), "消费能力目录无效");
    for (const [kind, entries] of [["command", value.commands], ["query", value.queries]]) {
      const names = new Set();
      for (const entry of entries) {
        require(exact(entry, ["name", "kind", "scope", "required_fields", "optional_fields", "idempotency",
          "idempotency_key_format", "http_status"]) && bounded(entry.name) && !names.has(entry.name)
          && entry.kind === kind && entry.scope === "consumer" && Array.isArray(entry.required_fields)
          && entry.required_fields.every(bounded) && Array.isArray(entry.optional_fields) && entry.optional_fields.every(bounded)
          && ["service_receipt", "none"].includes(entry.idempotency)
          && [null, "opaque", "uuid4"].includes(entry.idempotency_key_format)
          && Number.isSafeInteger(entry.http_status) && entry.http_status >= 200 && entry.http_status < 300, "消费操作目录无效");
        names.add(entry.name);
      }
    }
    return value;
  }
  function validateInformationReference(value) {
    return exact(value, ["source_id", "exact_version"]) && bounded(value.source_id) && bounded(value.exact_version);
  }
  function validateOwner(value) {
    return exact(value, ["workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"])
      && Object.values(value).every(uuid);
  }
  function validateArtifactReference(value) {
    return exact(value, ["scope", "output_id"]) && value.scope === "artifact" && uuid(value.output_id);
  }
  function validatePublicArtifact(value, consumer, output, entry) {
    require(exact(value, ["schema_version", "kind", "workflow_definition_id", "definition_revision",
      "workflow_session_id", "session_revision", "node_id", "port_id", "run_id", "reference", "data_type",
      "data_schema_version", "value", "producer"]) && value.schema_version === 1 && value.kind === "workflow.public-artifact"
      && identity(value, consumer.workflow_definition_id, consumer.workflow_session_id)
      && value.definition_revision === consumer.definition_revision && positive(value.session_revision)
      && value.session_revision >= consumer.session_revision && value.node_id === output.node_binding_id
      && value.port_id === output.port_id && value.run_id === output.run_id && same(value.reference, entry.source_ref)
      && bounded(value.data_type) && positive(value.data_schema_version) && validateOwner(value.producer)
      && consumer.history.some(chain => chain.workflow_session_id === value.producer.workflow_session_id
        && chain.chain_run_id === value.producer.chain_run_id), "公开条目正文归属或引用无效");
    if (supportedContent(value.data_type, value.data_schema_version)) validateContent(value.value, value.data_type, value.data_schema_version);
    return value;
  }
  function validateInformationDeclaration(value) {
    return exact(value, ["source_ref", "component_id", "component_version", "channel_id", "format_id", "format_version",
      "item_schema", "discover_public", "read_public", "source_scope", "max_page_size", "max_page_bytes"])
      && validateInformationReference(value.source_ref)
      && ["component_id", "component_version", "channel_id", "format_id"].every(key => bounded(value[key]))
      && positive(value.format_version) && object(value.item_schema) && typeof value.discover_public === "boolean"
      && typeof value.read_public === "boolean" && ["live", "history", "live_and_history"].includes(value.source_scope)
      && positive(value.max_page_size) && value.max_page_size <= 1000 && positive(value.max_page_bytes) && value.max_page_bytes <= 4000000;
  }
  function validateRegistrations(value, limit = 100) {
    require(exact(value, ["items", "next_cursor"]) && Array.isArray(value.items) && value.items.length <= limit
      && cursor(value.next_cursor), "公开登记目录分页无效");
    for (const item of value.items) {
      require(object(item) && bounded(item.kind) && object(item.registration_ref) && object(item.declaration)
        && item.discover_public === true && typeof item.read_public === "boolean" && bounded(item.availability)
        && (item.package_id === null || bounded(item.package_id))
        && (item.package_version === null || bounded(item.package_version)), "公开登记声明无效");
      if (["information_source", "information_binding"].includes(item.kind)) {
        require(validateInformationDeclaration(item.declaration) && same(item.registration_ref, item.declaration.source_ref)
          && item.declaration.discover_public === true && item.read_public === item.declaration.read_public, "公开信息源声明无效");
        if (item.kind === "information_binding") require(validateOwner(item.owner) && positive(item.generation), "信息调用身份无效");
      }
      if (item.information_sources !== undefined) require(Array.isArray(item.information_sources)
        && item.information_sources.every(validateInformationReference)
        && item.has_information_sources === (item.information_sources.length > 0), "公开节点信息源声明无效");
    }
    return value;
  }
  function validateInformationPage(value, binding, sourceScope, limit) {
    const declaration = binding.declaration;
    require(exact(value, ["schema_version", "source_ref", "owner", "generation", "source_scope", "format_id",
      "format_version", "items", "next_cursor", "status"]) && value.schema_version === 1
      && same(value.source_ref, binding.registration_ref) && same(value.owner, binding.owner)
      && value.generation === binding.generation && value.source_scope === sourceScope
      && value.format_id === declaration.format_id && value.format_version === declaration.format_version
      && Array.isArray(value.items) && value.items.length <= limit && cursor(value.next_cursor)
      && ["ok", "gap", "reset"].includes(value.status), "信息读取归属、格式或分页无效");
    return value;
  }
  function validateHistory(value, consumer, port) {
    require(exact(value, ["schema_version", "kind", "workflow_definition_id", "definition_revision",
      "workflow_session_id", "session_revision", "node_id", "port_id", "outputs"])
      && value.schema_version === 1 && value.kind === "workflow.public-history"
      && identity(value, consumer.workflow_definition_id, consumer.workflow_session_id)
      && value.definition_revision === consumer.definition_revision && positive(value.session_revision)
      && value.node_id === port.node_binding_id && value.port_id === port.port_id && Array.isArray(value.outputs), "公开历史身份无效");
    for (const output of value.outputs) {
      validateOutput(output);
      const version = output.data_schema_version ?? output.payload?.schema_version;
      const portVersion = port.data_schema_version ?? port.payload?.schema_version;
      require(output.node_binding_id === port.node_binding_id && output.port_id === port.port_id
        && output.data_type === port.data_type && (version === undefined || portVersion === undefined || version === portVersion),
      "公开历史端口不匹配");
      if (output.source) require(consumer.history.some(chain => chain.workflow_definition_id === output.source.workflow_definition_id
        && chain.workflow_session_id === output.source.workflow_session_id && chain.definition_revision === output.source.definition_revision
        && chain.chain_run_id === output.source.chain_run_id), "公开历史不在冻结会话范围内");
    }
    return value;
  }
  class GraphChatClient {
    constructor({ workflowId, sessionId = null, storage, request, keyFactory }) {
      require(uuid(workflowId) && (sessionId === null || uuid(sessionId)), "工作流入口身份无效");
      this.workflowId = workflowId; this.sessionId = sessionId; this.storage = storage;
      this.request = request; this.keyFactory = keyFactory; this.consumer = null; this.pending = null;
      this.generation = 0; this.busy = false; this.revisions = new Map();
      this.application = null; this.registrationGeneration = 0; this.informationGeneration = 0; this.sessionGeneration = 0;
      this.registrationContext = null; this.registrations = [];
      this.publicHistory = []; this.historyRequests = new Map(); this.artifactRequests = new Map();
      this.frontendRequests = new Map();
      this.eventBindings = null; this.eventGeneration = 0;
      this.forkCandidates = null; this.candidateContext = null; this.candidateGeneration = 0;
      this.storageKey = "workflow-chat:v1:" + workflowId;
      const raw = storage.getItem(this.storageKey);
      if (raw) {
        const value = JSON.parse(raw);
        require(exact(value, ["schema_version", "kind", "workflow_definition_id", "workflow_session_id", "pending"])
          && value.schema_version === 1 && value.kind === "workflow.chat-client" && value.workflow_definition_id === workflowId
          && (value.workflow_session_id === null || uuid(value.workflow_session_id)), "本机聊天记录无效");
        if (value.pending) {
          this.pending = validateCommand(value.pending, workflowId);
          require(this.pending.action === "create" || this.pending.workflow_session_id === value.workflow_session_id,
            "待核实请求与本机会话不匹配");
        }
        if (this.pending || !sessionId) this.sessionId = this.pending?.workflow_session_id ?? value.workflow_session_id;
      }
    }
    persist() {
      this.storage.setItem(this.storageKey, JSON.stringify({ schema_version: 1, kind: "workflow.chat-client",
        workflow_definition_id: this.workflowId, workflow_session_id: this.sessionId, pending: this.pending }));
    }
    accept(value) {
      validateConsumer(value, this.workflowId, this.sessionId);
      if (value.session_revision < (this.revisions.get(value.workflow_session_id) ?? 0)) return false;
      const current = this.consumer;
      if (current?.workflow_session_id === value.workflow_session_id && current.session_revision === value.session_revision
        && (current.nodes.some(node => {
          const incoming = value.nodes.find(row => row.node_binding_id === node.node_binding_id);
          return incoming?.run_id === node.run_id && node.revision !== null && incoming.revision !== null && incoming.revision < node.revision;
        }) || current.history.some(chain => value.history.some(row => row.chain_run_id === chain.chain_run_id && row.revision < chain.revision)))) return false;
      if (!current || current.workflow_session_id !== value.workflow_session_id
        || current.definition_revision !== value.definition_revision || current.session_revision !== value.session_revision
        || current.status !== value.status || !same(current.history, value.history)) {
        this.forkCandidates = null; this.candidateContext = null; this.candidateGeneration++;
      }
      if (!current || current.workflow_session_id !== value.workflow_session_id
        || current.definition_revision !== value.definition_revision || current.session_revision !== value.session_revision
        || !same(current.outputs, value.outputs)
        || !same(current.nodes.map(node => [node.node_binding_id, node.run_id]), value.nodes.map(node => [node.node_binding_id, node.run_id]))) {
        this.publicHistory = []; this.historyRequests.clear(); this.artifactRequests.clear(); this.frontendRequests.clear();
        this.eventBindings = null; this.eventGeneration++;
      }
      this.revisions.set(value.workflow_session_id, value.session_revision); this.consumer = freeze(clone(value));
      return true;
    }
    async refresh() {
      if (!this.sessionId) return null;
      const session = this.sessionId, generation = ++this.generation;
      let value;
      try { value = await this.query("consumer.read", { session_id: session }); }
      catch (error) {
        if (session !== this.sessionId || generation !== this.generation) return null;
        throw error;
      }
      if (session !== this.sessionId || generation !== this.generation) return null;
      this.accept(value); return this.consumer;
    }
    async selectSession(session) {
      require(!this.pending && !this.busy && uuid(session), "待核实原请求完成后才能切换会话");
      this.generation++; this.sessionId = session; this.consumer = null; this.persist();
      this.eventBindings = null; this.eventGeneration++;
      this.invalidateInformation();
      return this.refresh();
    }
    async discover() {
      const value = validateApplication(await this.request("/api/graph/consumer/application"));
      this.application = value;
      return value;
    }
    async query(operation, parameters = {}) {
      require(consumerQueries.has(operation) && object(parameters), "消费查询不受支持");
      require(!this.application || this.application.queries.some(item => item.name === operation), "消费查询未登记");
      return this.request("/api/graph/consumer/queries", { method: "POST",
        body: JSON.stringify({ operation, parameters: clone(parameters) }) });
    }
    async readEventBindings() {
      require(this.consumer && this.sessionId, "先选择并读取已有会话");
      const context = clone(this.informationContext()), generation = ++this.eventGeneration;
      this.eventBindings = null;
      let value;
      try { value = await this.query("consumer.event.bindings", { session_id: context.session,
        workflow_definition_id: this.workflowId, definition_revision: context.definition }); }
      catch (error) { if (generation !== this.eventGeneration || !same(context, this.informationContext())) return null; throw error; }
      if (generation !== this.eventGeneration || !same(context, this.informationContext())) return null;
      validateEventBindings(value, this.workflowId, context.session, context.definition);
      require(value.session_revision === this.consumer.session_revision, "事件目录依据已变化，请刷新会话");
      this.eventBindings = freeze(clone(value)); return this.eventBindings;
    }
    async readEvent(chainId) {
      require(uuid(chainId) && this.consumer && this.sessionId, "事件运行身份无效");
      const context = clone(this.informationContext()), generation = this.generation;
      let value;
      try { value = await this.query("consumer.event.read", { session_id: context.session, chain_id: chainId }); }
      catch (error) { if (generation !== this.generation || !same(context, this.informationContext())) return null; throw error; }
      if (generation !== this.generation || !same(context, this.informationContext())) return null;
      return freeze(clone(validateEventRun(value, this.workflowId, context.session, chainId, context.definition)));
    }
    async submitEvent(binding, payload) {
      if (this.pending) return this.command("event");
      validateEventBinding(binding);
      require(binding.audience === "consumer" && object(payload) && strictJson(payload)
        && !Object.hasOwn(payload, "_workflow_frozen_resources"), "事件载荷或公开权限无效");
      const fixed = clone(binding), fixedPayload = clone(payload);
      const available = this.eventBindings ?? await this.readEventBindings();
      require(available && available.can_submit && available.bindings.some(row => same(row, fixed)), "事件未登记或依据已变化");
      return this.command("event", { workflow_definition_id: this.workflowId, definition_revision: available.definition_revision,
        event_id: fixed.event_id, event_schema_version: fixed.schema_version, payload: fixedPayload });
    }
    candidatesCurrent() {
      return this.forkCandidates !== null && same(this.candidateContext, this.informationContext())
        && this.forkCandidates.status === this.consumer?.status;
    }
    async readCandidates() {
      require(this.consumer && this.sessionId, "先选择并读取已有会话");
      const view = this.consumer, context = clone(this.informationContext()), generation = ++this.candidateGeneration;
      this.forkCandidates = null; this.candidateContext = null;
      let value;
      try {
        value = await this.query("consumer.candidate.list", { session_id: context.session,
          workflow_definition_id: this.workflowId, definition_revision: context.definition });
      } catch (error) {
        if (generation !== this.candidateGeneration || view !== this.consumer
          || !same(context, this.informationContext())) return null;
        throw error;
      }
      if (generation !== this.candidateGeneration || view !== this.consumer
        || !same(context, this.informationContext())) return null;
      validateCandidates(value, view);
      this.forkCandidates = freeze(clone(value)); this.candidateContext = context;
      return this.forkCandidates;
    }
    async forkCandidate(candidate) {
      require(!this.busy && !this.pending, "待核实原请求完成后才能创建新分叉");
      require(validateCandidate(candidate, this.workflowId), "检查点工作流身份不受支持");
      return this.command("fork", { candidate: clone(candidate) });
    }
    async readDefinition() {
      const value = await this.query("consumer.definition", { identity: this.workflowId });
      require(exact(value, ["schema_version", "kind", "workflow_definition_id", "definition_revision", "name"])
        && value.schema_version === 1 && value.kind === "workflow.consumer-definition"
        && value.workflow_definition_id === this.workflowId && positive(value.definition_revision)
        && typeof value.name === "string", "工作流身份或版本无效");
      return value;
    }
    async readSessions() {
      const value = await this.query("consumer.sessions", { identity: this.workflowId });
      require(Array.isArray(value) && value.every(row => exact(row, ["workflow_definition_id", "definition_revision",
        "workflow_session_id", "session_revision", "status"]) && row.workflow_definition_id === this.workflowId
        && uuid(row.workflow_session_id) && positive(row.definition_revision) && positive(row.session_revision)
        && typeof row.status === "string"), "会话目录身份无效");
      return value;
    }
    async readHistory(port) {
      const view = this.consumer, generation = this.generation;
      require(view && this.sessionId === view.workflow_session_id && view.outputs.some(item => same(item, port)), "公开历史端口不可用");
      const fixedPort = clone(port), key = JSON.stringify([fixedPort.node_binding_id, fixedPort.port_id]), token = {};
      this.historyRequests.set(key, token);
      const current = () => generation === this.generation && this.consumer === view && this.historyRequests.get(key) === token;
      let value;
      try {
        value = await this.query("output.history", { session_id: view.workflow_session_id,
          workflow_definition_id: this.workflowId, definition_revision: view.definition_revision,
          node_id: fixedPort.node_binding_id, port_id: fixedPort.port_id });
      } catch (failure) { if (!current()) return null; throw failure; }
      if (!current()) return null;
      const history = validateHistory(value, view, fixedPort);
      this.publicHistory = freeze(this.publicHistory.filter(item => item.node_binding_id !== fixedPort.node_binding_id || item.port_id !== fixedPort.port_id)
        .concat(clone(history.outputs)));
      return history;
    }
    async readDisplayEntry(output, entry) {
      const view = this.consumer, context = this.informationContext();
      require(view && this.sessionId === view.workflow_session_id && [...view.outputs, ...this.publicHistory].some(item => same(item, output)),
        "公开展示来源已变化，请刷新");
      const fixedOutput = clone(output), fixedEntry = clone(entry);
      require(fixedOutput.availability === "produced" && ["FRONTEND_DISPLAY", "TAVERN_CHAT_DISPLAY"].includes(fixedOutput.data_type)
        && (fixedOutput.data_schema_version ?? fixedOutput.payload?.schema_version) === 1, "公开展示类型不受支持");
      validateContent(fixedOutput.payload, fixedOutput.data_type, 1);
      require(fixedOutput.payload.entries.some(item => same(item, fixedEntry)), "条目不在公开展示声明中");
      return this.readOutputArtifact(fixedOutput, fixedEntry.source_ref);
    }
    async readOutputArtifact(output, reference) {
      const view = this.consumer, context = this.informationContext();
      require(view && this.sessionId === view.workflow_session_id
        && [...view.outputs, ...this.publicHistory].some(item => same(item, output)), "公开展示来源已变化，请刷新");
      const fixedOutput = clone(output), fixedEntry = { source_ref: clone(reference) };
      require(fixedOutput.availability === "produced" && validateArtifactReference(fixedEntry.source_ref), "公开产物引用无效");
      const historical = !view.outputs.some(item => same(item, fixedOutput));
      const historyKey = JSON.stringify([fixedOutput.node_binding_id, fixedOutput.port_id]);
      const historyToken = this.historyRequests.get(historyKey);
      const key = JSON.stringify([fixedOutput.node_binding_id, fixedOutput.port_id, fixedOutput.run_id, fixedEntry.source_ref.output_id]);
      const token = {}; this.artifactRequests.set(key, token);
      const current = () => this.artifactRequests.get(key) === token && same(context, this.informationContext())
        && (!historical || this.historyRequests.get(historyKey) === historyToken)
        && this.consumer && [...this.consumer.outputs, ...this.publicHistory].some(item => same(item, fixedOutput));
      let value;
      try {
        value = await this.query("output.artifact.read", { session_id: view.workflow_session_id,
          workflow_definition_id: this.workflowId, definition_revision: view.definition_revision,
          node_id: fixedOutput.node_binding_id, port_id: fixedOutput.port_id, run_id: fixedOutput.run_id,
          reference: fixedEntry.source_ref });
      } catch (error) { if (!current()) return null; throw error; }
      if (!current()) return null;
      return validatePublicArtifact(value, view, fixedOutput, fixedEntry);
    }
    async readFrontendExtensions(output) {
      const view = this.consumer, context = this.informationContext();
      require(view && this.sessionId === view.workflow_session_id
        && [...view.outputs, ...this.publicHistory].some(item => same(item, output)), "公开界面绑定来源已变化，请刷新");
      const fixed = clone(output), key = JSON.stringify([fixed.node_binding_id, fixed.port_id, fixed.run_id]), token = {};
      this.frontendRequests.set(key, token);
      const current = () => this.frontendRequests.get(key) === token && same(context, this.informationContext())
        && this.consumer && [...this.consumer.outputs, ...this.publicHistory].some(item => same(item, fixed));
      let response;
      try {
        response = await this.query("frontend.extensions.read", { session_id: view.workflow_session_id,
          workflow_definition_id: this.workflowId, definition_revision: view.definition_revision,
          node_id: fixed.node_binding_id, port_id: fixed.port_id });
      } catch (failure) { if (!current()) return null; throw failure; }
      if (!current()) return null;
      require(exact(response, ["schema_version", "kind", "workflow_definition_id", "definition_revision",
        "workflow_session_id", "session_revision", "node_id", "port_id", "package_lock", "frontend_extensions"])
        && response.schema_version === 1 && response.kind === "workflow.consumer-frontend-extensions"
        && identity(response, this.workflowId, view.workflow_session_id)
        && response.definition_revision === view.definition_revision && positive(response.session_revision)
        && response.session_revision >= view.session_revision
        && response.node_id === fixed.node_binding_id && response.port_id === fixed.port_id
        && Array.isArray(response.package_lock) && Array.isArray(response.frontend_extensions), "公开界面绑定归属无效");
      return freeze(clone(response));
    }
    informationContext() {
      return { session: this.sessionId, definition: this.consumer?.definition_revision ?? null,
        revision: this.consumer?.session_revision ?? null, generation: this.sessionGeneration };
    }
    registrationsCurrent() {
      return same(this.registrationContext, this.informationContext());
    }
    invalidateInformation() {
      this.registrationGeneration++; this.informationGeneration++;
      this.sessionGeneration++;
      this.eventBindings = null; this.eventGeneration++;
      this.forkCandidates = null; this.candidateContext = null; this.candidateGeneration++;
      this.registrationContext = null; this.registrations = [];
      this.publicHistory = []; this.historyRequests.clear(); this.artifactRequests.clear(); this.frontendRequests.clear();
    }
    async listRegistrations({ limit = 100, cursor: nextCursor = null } = {}) {
      require(positive(limit) && limit <= 1000 && cursor(nextCursor), "登记目录读取参数无效");
      const context = this.informationContext(), generation = ++this.registrationGeneration;
      let response;
      try {
        response = await this.query("registration.list", {
          ...(context.session ? { session_id: context.session } : {}), limit,
          ...(nextCursor === null ? {} : { cursor: nextCursor }),
        });
      } catch (error) {
        if (generation !== this.registrationGeneration || !same(context, this.informationContext())) return null;
        throw error;
      }
      if (generation !== this.registrationGeneration || !same(context, this.informationContext())) return null;
      const value = validateRegistrations(response, limit);
      this.informationGeneration++; this.registrationContext = context; this.registrations = clone(value.items);
      return value;
    }
    async readInformation(binding, { source_scope: sourceScope = "live", limit, cursor: nextCursor = null } = {}) {
      const context = this.informationContext();
      require(this.consumer && same(context, this.registrationContext)
        && this.registrations.some(item => same(item, binding)), "信息登记已变化，请重新列举");
      const fixed = clone(binding);
      require(fixed.kind === "information_binding" && fixed.discover_public === true && fixed.read_public === true
        && fixed.declaration.read_public === true && validateOwner(fixed.owner) && positive(fixed.generation),
      "信息源未授予公开读取");
      require(this.consumer.history.some(chain => chain.chain_run_id === fixed.owner.chain_run_id
        && chain.workflow_session_id === fixed.owner.workflow_session_id), "信息调用不在当前会话历史范围内");
      require(["live", "history"].includes(sourceScope)
        && [sourceScope, "live_and_history"].includes(fixed.declaration.source_scope), "信息来源作用域不受支持");
      const pageLimit = limit ?? Math.min(100, fixed.declaration.max_page_size);
      require(positive(pageLimit) && pageLimit <= fixed.declaration.max_page_size && cursor(nextCursor), "信息读取参数超出登记边界");
      const generation = ++this.informationGeneration, registrationGeneration = this.registrationGeneration;
      let value;
      try {
        value = await this.query("information.read", { session_id: context.session,
          reference: fixed.registration_ref, owner: fixed.owner, generation: fixed.generation,
          source_scope: sourceScope, limit: pageLimit, ...(nextCursor === null ? {} : { cursor: nextCursor }) });
      } catch (error) {
        if (generation !== this.informationGeneration || registrationGeneration !== this.registrationGeneration
          || !same(context, this.informationContext())) return null;
        throw error;
      }
      if (generation !== this.informationGeneration || registrationGeneration !== this.registrationGeneration
        || !same(context, this.informationContext())) return null;
      return validateInformationPage(value, fixed, sourceScope, pageLimit);
    }
    async command(action, fields = {}) {
      require(!this.busy, "请求正在提交");
      const reconciling = this.pending !== null;
      if (!this.pending) {
        const create = action === "create";
        const fork = action === "fork";
        require(create || this.consumer && (fork ? this.candidatesCurrent() && this.forkCandidates.can_fork
          : action === "event" ? !!this.eventBindings?.can_submit
          : action === "start" ? this.consumer.can_submit : this.consumer.available_actions.includes(action)), "动作当前不可用");
        if (fork) require(validateCandidate(fields.candidate, this.workflowId)
          && this.forkCandidates.candidates.some(row => same(row, fields.candidate))
          && !["prepared", "running", "pausing", "paused"].includes(this.consumer.status), "分叉需要当前可用的完成检查点");
        if (action === "event") require(!["prepared", "running", "pausing", "paused"].includes(this.consumer.status)
          && fields.workflow_definition_id === this.workflowId && fields.definition_revision === this.consumer.definition_revision
          && this.eventBindings?.session_revision === this.consumer.session_revision && this.eventBindings.can_submit
          && this.eventBindings.bindings.some(row => row.event_id === fields.event_id && row.schema_version === fields.event_schema_version),
        "事件需要当前已授权的稳定会话与精确定义");
        if (action === "event") require(object(fields.payload) && strictJson(fields.payload)
          && !Object.hasOwn(fields.payload, "_workflow_frozen_resources"), "事件载荷须为严格 JSON 对象");
        if (action === "start") {
          validateExternalInputs(fields.inputs ?? {});
          for (const input of this.consumer.inputs) if (Object.hasOwn(fields.inputs ?? {}, input.name)) {
            const value = fields.inputs[input.name];
            if (input.data_type === "TEXT") require(typeof value === "string", "TEXT 输入须为文本");
            else validateContent(value, "PROMPT");
          }
        }
        const body = create ? { workflow_definition_id: this.workflowId, definition_revision: fields.definition_revision,
          idempotency_key: this.keyFactory() } : fork ? { candidate_id: fields.candidate.candidate_id,
          expected_revision: this.forkCandidates.session_revision, expected_data_revision: this.forkCandidates.data_revision,
          expected_head_revision: this.forkCandidates.head_revision, idempotency_key: this.keyFactory() }
          : action === "start" ? { expected_revision: this.consumer.session_revision,
          idempotency_key: this.keyFactory(), inputs: clone(fields.inputs ?? {}) } : action === "event"
          ? { workflow_definition_id: fields.workflow_definition_id, definition_revision: fields.definition_revision,
            event_id: fields.event_id, event_schema_version: fields.event_schema_version, payload: clone(fields.payload),
            expected_revision: this.consumer.session_revision, idempotency_key: this.keyFactory() } : { action,
          expected_revision: this.consumer.session_revision, idempotency_key: this.keyFactory(), ...fields };
        this.pending = validateCommand({ action, workflow_session_id: create ? null : this.sessionId,
          path: create ? "/api/graph/consumer/sessions" : `/api/graph/sessions/${this.sessionId}/consumer/${fork ? "candidates/fork" : action === "start" ? "runs" : action === "event" ? "events/submit" : "control"}`, body,
          ...(fork ? { candidate: clone(fields.candidate) } : {}) }, this.workflowId);
        try { this.persist(); } catch (error) { this.pending = null; throw error; }
      }
      const command = clone(this.pending); this.busy = true;
      const generation = this.generation, session = this.sessionId;
      const current = () => generation === this.generation && session === this.sessionId && same(this.pending, command);
      try {
        const envelope = commandEnvelope(command, this.workflowId);
        if (reconciling) {
          const value = validateApplicationReceiptRead(await this.request("/api/graph/consumer/receipts/read", {
            method: "POST", body: JSON.stringify(envelope) }), command, this.workflowId);
          require(current(), "核实依据已变化，迟到回执未覆盖当前会话；原请求待核实");
          this.sessionId = value.receipt.workflow_session_id;
          this.pending = null;
          try { this.persist(); } catch (error) { this.sessionId = session; this.pending = command; throw error; }
          this.generation++;
          if (session !== this.sessionId) {
            this.consumer = null;
            this.invalidateInformation();
          }
          return value;
        }
        require(!this.application || this.application.commands.some(item => item.name === envelope.operation), "消费操作未登记，原请求保留");
        const value = validateApplicationReceipt(await this.request("/api/graph/consumer/commands", {
          method: "POST", body: JSON.stringify(envelope) }), command, this.workflowId);
        require(current(), "提交依据已变化，迟到回执未覆盖当前会话；原请求待核实");
        const parentView = this.consumer;
        this.generation++; this.sessionId = value.receipt.workflow_session_id;
        this.invalidateInformation();
        this.accept(value.consumer); this.pending = null;
        try { this.persist(); } catch (error) {
          this.pending = command;
          if (command.action === "fork") {
            this.sessionId = session; this.consumer = parentView; this.invalidateInformation();
          }
          throw error;
        }
        return value;
      } catch (error) {
        if (!reconciling && error.definite && current()) {
          this.pending = null;
          try { this.persist(); } catch (storageError) { this.pending = command; throw storageError; }
        }
        throw error;
      } finally { this.busy = false; }
    }
  }
  root.GraphChat = { uuid, exact, clone, supportedContent, validateContent, displayText, displayEntryText, httpFailure, parseExternalInput,
    validateConsumer, validateReceipt, validateCommand, commandEnvelope, validateApplicationReceipt, validateApplicationReceiptRead, validateApplication,
    validateHistory, validateRegistrations, validateInformationPage, validatePublicArtifact,
    validateEventBinding, validateEventBindings, validateEventRun, validateCandidate, validateCandidates, strictJson, GraphChatClient };
})(globalThis);
