"use strict";

(async () => {
  function unavailable(message, status = "入口不可用") {
    const client = document.getElementById("chat-client");
    const mode = document.getElementById("mode");
    const box = document.getElementById("error");
    if (client) client.hidden = true;
    if (mode) mode.textContent = status;
    if (box) {
      box.textContent = message;
      box.hidden = false;
    }
  }
  function entryKind() {
    if (!location.search) return null;
    const raw = location.search.slice(1);
    const allowed = new Set(["graph_workflow", "graph_session"]);
    const fields = raw.split("&"), values = new Map();
    if (!raw || raw.length > 1024 || fields.length > allowed.size) throw new Error("聊天入口身份无效");
    // URLSearchParams alone accepts empty fields and replaces malformed UTF-8.
    for (const field of fields) {
      const separator = field.indexOf("=");
      if (separator < 1) throw new Error("聊天入口身份无效");
      const key = decodeURIComponent(field.slice(0, separator).replace(/\+/g, " "));
      const value = decodeURIComponent(field.slice(separator + 1).replace(/\+/g, " "));
      if (!allowed.has(key) || values.has(key)) throw new Error("聊天入口身份无效");
      values.set(key, value);
    }
    const uuid = value => typeof value === "string" && value.length === 36
      && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
    if (!uuid(values.get("graph_workflow"))
      || values.has("graph_session") && !uuid(values.get("graph_session"))) throw new Error("聊天入口身份无效");
    return "graph";
  }
  let kind;
  try { kind = entryKind(); }
  catch { unavailable("聊天入口身份无效"); return; }
  if (!kind) { unavailable("未指定工作流或会话", "入口未绑定"); return; }
  const mode = document.getElementById("mode");
  if (mode) mode.textContent = "正在连接";
  const paths = ["/static/graph-chat-core.js", "/static/frontend-package-host.js", "/static/frontend-package.js",
    "/static/graph-chat.js"];
  try {
    for (const path of paths) {
      try {
        await new Promise((resolve, reject) => {
          const script = document.createElement("script");
          script.src = path;
          script.onload = resolve;
          script.onerror = () => reject(new Error("聊天界面加载失败"));
          document.head.append(script);
        });
      } catch (error) {
        if (!["/static/frontend-package-host.js", "/static/frontend-package.js"].includes(path)) throw error;
      }
    }
  } catch (error) {
    unavailable(error.message, "加载失败");
  }
})();
