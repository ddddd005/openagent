import { afterEach, describe, expect, it } from "vitest";
import { createPinia, disposePinia, setActivePinia } from "pinia";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import ErrorToast from "./ErrorToast.vue";
import { useWorkbenchNoticesStore } from "../stores/workbenchNotices";

describe("project top-level notice presentation", () => {
  const pinia = createPinia();
  afterEach(() => disposePinia(pinia));
  it("renders all four severities with status text, icons, close controls and domain actions in body", async () => {
    setActivePinia(pinia);
    const store = useWorkbenchNoticesStore();
    for (const severity of ["info", "success", "warning", "major"] as const)
      store.publish({ id: severity, severity, title: `title-${severity}`, reason: `reason-${severity}`,
        requestId: severity, action: { label: "查看原请求", run() {} } });
    const context: { teleports?: Record<string, string> } = {};
    const html = await renderToString(createSSRApp(ErrorToast).use(pinia), context);
    expect(html).not.toContain("reason-major");
    const body = context.teleports?.body ?? "";
    for (const severity of ["info", "success", "warning", "major"])
      expect(body).toContain(`notice-${severity}`);
    for (const label of ["通知", "成功", "警告", "重大问题", "关闭通知", "查看原请求"])
      expect(body).toContain(label);
    for (const icon of ["lucide-info", "lucide-circle-check", "lucide-triangle-alert", "lucide-circle-alert"])
      expect(body).toContain(icon);
    expect(body).toContain('role="alert"');
    expect(body).toContain('role="status"');
  });
});
