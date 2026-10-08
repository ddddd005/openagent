/*
 * Modified 2026-10-08 from SillyTavern public/script.js:
 * addOneMessage, updateMessageElement, scrollChatToBottom and Markdown conversion.
 * Upstream 06bde939fb1e9c4c8d8641d810f0a916b5bce127, AGPL-3.0.
 * See vendor/sillytavern/PROVENANCE.md and COPY-MANIFEST.json.
 */
"use strict";

((root) => {
  const converter = new showdown.Converter({ tables: true, strikethrough: true,
    simpleLineBreaks: true, ghCodeBlocks: true, literalMidWordUnderscores: true,
    openLinksInNewWindow: false, simplifiedAutoLink: false });
  const roles = { user: "你", assistant: "Agent", system: "系统" };
  const allowedTags = ["p", "br", "strong", "b", "em", "i", "s", "del", "blockquote", "q",
    "code", "pre", "ul", "ol", "li", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td", "a"];
  function messageFormatting(text) {
    if (typeof text !== "string") throw new Error("聊天正文无效");
    const html = converter.makeHtml(text).trim();
    return DOMPurify.sanitize(html, { ALLOWED_TAGS: allowedTags,
      ALLOWED_ATTR: ["href", "title"], ALLOW_DATA_ATTR: false, ALLOW_ARIA_ATTR: false,
      ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|\/(?!\/)|#)/i });
  }
  function icons(element) {
    lucide.createIcons({ root: element, nameAttr: "data-lucide",
      attrs: { "aria-hidden": "true", focusable: "false", width: "18", height: "18" } });
  }
  class ChatRenderer {
    constructor({ chatElement, messageTemplate, onCopy, onFork = null, requestFrame = callback => requestAnimationFrame(callback),
      cancelFrame = handle => cancelAnimationFrame(handle) }) {
      this.chatElement = chatElement; this.messageTemplate = messageTemplate;
      this.onCopy = onCopy; this.onFork = onFork; this.requestFrame = requestFrame; this.cancelFrame = cancelFrame;
      this.rows = new Map(); this.requestId = null;
    }
    updateMessageElement(mes, { messageId, messageElement = this.messageTemplate.content.firstElementChild.cloneNode(true) }) {
      messageElement.dataset.entryId = mes.entry_id;
      messageElement.setAttribute("mesid", String(messageId));
      messageElement.setAttribute("is_user", String(mes.role === "user"));
      messageElement.setAttribute("is_system", String(mes.role === "system"));
      messageElement.querySelector(".avatar img").setAttribute("src", mes.role === "user"
        ? "/tavern/assets/user-default.png" : "/tavern/assets/assistant.png");
      messageElement.querySelector(".ch_name .name_text").textContent = roles[mes.role];
      const timestamp = messageElement.querySelector(".timestamp");
      timestamp.textContent = "";
      timestamp.title = `来源运行 ${mes.producer.node_run_id}`;
      messageElement.querySelector(".mesIDDisplay").textContent = `#${messageId + 1}`;
      messageElement.querySelector(".mes_text").innerHTML = messageFormatting(mes.text);
      for (const anchor of messageElement.querySelectorAll(".mes_text a")) {
        anchor.setAttribute("target", "_blank"); anchor.setAttribute("rel", "noopener noreferrer");
      }
      const copy = messageElement.querySelector(".mes_copy");
      copy.onclick = () => void this.onCopy(mes.text, copy);
      icons(messageElement);
      return messageElement;
    }
    addOneMessage(mes, { messageId, scroll = true }) {
      const messageElement = this.updateMessageElement(mes, { messageId });
      this.chatElement.append(messageElement);
      this.chatElement.querySelectorAll(".mes").forEach(element => element.classList.remove("last_mes"));
      messageElement.classList.add("last_mes");
      if (scroll) this.scrollChatToBottom({ waitForFrame: true });
      return messageElement;
    }
    scrollChatToBottom({ waitForFrame = false } = {}) {
      const doScroll = () => { this.chatElement.scrollTop = this.chatElement.scrollHeight; this.requestId = null; };
      if (this.requestId !== null) this.cancelFrame(this.requestId);
      if (!waitForFrame) { doScroll(); return; }
      this.requestId = this.requestFrame(doScroll);
    }
    render(messages) {
      const follow = this.rows.size === 0
        || this.chatElement.scrollHeight - this.chatElement.scrollTop - this.chatElement.clientHeight < 100;
      const ids = new Set(messages.map(message => message.entry_id));
      for (const [id, row] of this.rows) if (!ids.has(id)) { row.element.remove(); this.rows.delete(id); }
      let added = false;
      for (const [index, message] of messages.entries()) {
        let row = this.rows.get(message.entry_id);
        if (!row) {
          row = { element: this.addOneMessage(message, { messageId: index, scroll: false }), text: message.text };
          this.rows.set(message.entry_id, row); added = true;
        } else if (row.text !== message.text) {
          this.updateMessageElement(message, { messageId: index, messageElement: row.element }); row.text = message.text;
        }
        if (this.chatElement.children[index] !== row.element)
          this.chatElement.insertBefore(row.element, this.chatElement.children[index] ?? null);
      }
      this.chatElement.querySelectorAll(".mes").forEach(element => element.classList.remove("last_mes"));
      messages.length && this.rows.get(messages[messages.length - 1].entry_id).element.classList.add("last_mes");
      if (added && follow) this.scrollChatToBottom({ waitForFrame: true });
    }
    renderForks(targets, locked) {
      for (const [id, row] of this.rows) {
        const target = targets.get(id), button = row.element.querySelector(".mes_create_branch");
        button.hidden = !target || !this.onFork;
        button.disabled = locked || !target;
        button.onclick = target && this.onFork ? () => void this.onFork(id) : null;
        if (target) {
          button.title = `从第 ${target.round} 个完成回合检查点分叉`;
          button.setAttribute("aria-label", button.title);
        }
      }
    }
    clear() {
      this.rows.clear(); this.chatElement.replaceChildren();
      if (this.requestId !== null) this.cancelFrame(this.requestId);
      this.requestId = null;
    }
  }
  root.TavernChat = { messageFormatting, icons, ChatRenderer };
})(globalThis);
