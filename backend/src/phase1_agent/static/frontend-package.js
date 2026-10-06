"use strict";

((root) => {
  const declaration = {
    schema_version: 2, host_protocol_version: 1, extension_id: "workflow.frontend.public-output",
    kind: "consumer", entrypoint: "workflow.frontend.consumer.public-output",
    component_id: null, component_version: null, package_id: "workflow.frontend", package_version: "1.0.0",
    binding: { surface: "consumer", slot: "public-output",
      target: { scope: "content", type_id: "FRONTEND_DISPLAY", schema_version: 1 } },
  };
  function mountDisplay({ output, container, sdk }) {
    root.GraphChat.validateContent(output.payload, "FRONTEND_DISPLAY", 1);
    const doc = container.ownerDocument;
    const count = doc.createElement("p");
    count.textContent = `${output.payload.entries.length} 条展示引用 · 按条读取正文`;
    container.replaceChildren(count);
    const buttons = [];
    let active = true;
    const current = () => active && sdk.current();
    for (const entry of output.payload.entries) {
      const row = doc.createElement("article"), role = doc.createElement("h4"), source = doc.createElement("small");
      const button = doc.createElement("button"), body = doc.createElement("pre");
      role.textContent = { user: "用户", assistant: "助手", system: "系统" }[entry.role];
      source.textContent = `条目 ${entry.entry_id} · 产物 ${entry.source_ref.output_id}`;
      button.type = "button"; button.textContent = "读取正文"; body.textContent = "正文尚未读取";
      button.onclick = async () => {
        if (!current()) return;
        button.disabled = true; body.textContent = "正在读取正文";
        try {
          const value = await sdk.readArtifact(entry.source_ref);
          if (!current()) return;
          if (!value) { body.textContent = "展示来源已变化，请刷新"; return; }
          body.textContent = root.GraphChat.displayEntryText(value);
          source.textContent = `条目 ${entry.entry_id} · 产物 ${entry.source_ref.output_id} · 原会话 ${value.producer.workflow_session_id} · 原运行 ${value.producer.chain_run_id} · 节点调用 ${value.producer.node_run_id}`;
        } catch (failure) { if (current()) body.textContent = `正文读取失败 · ${failure.message}`; }
        finally { if (current()) button.disabled = false; }
      };
      buttons.push(button); row.append(role, source, button, body); container.append(row);
    }
    return () => {
      active = false;
      for (const button of buttons) { button.onclick = null; button.disabled = true; }
      container.replaceChildren();
    };
  }
  root.WorkflowFrontendPackage = {
    declaration,
    install(host) { host.register(declaration, mountDisplay); },
  };
})(globalThis);
