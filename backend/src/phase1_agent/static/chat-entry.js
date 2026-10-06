"use strict";

(async () => {
  const query = new URLSearchParams(location.search);
  const paths = query.has("graph_workflow") || query.has("graph_session")
    ? ["/static/graph-chat-core.js", "/static/frontend-package-host.js", "/static/frontend-package.js",
      "/static/graph-chat.js"] : ["/static/app.js"];
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
    const box = document.getElementById("error");
    box.textContent = error.message;
    box.hidden = false;
  }
})();
