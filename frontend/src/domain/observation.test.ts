import { afterEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { AGENT_BINDINGS, type PublicSession } from "../adapters/workbenchApi";
import { decodeWorkbenchSession } from "../adapters/workbenchSessions";
import { OUTPUT_BINDING, validObservation, type WorkflowObservation } from "./observation";
import { useWorkbenchRuntimeStore } from "../stores/workbenchRuntime";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import MainFlowNode from "../components/MainFlowNode.vue";

vi.mock("@vue-flow/core", () => ({
  Position: { Left: "left", Right: "right" }, Handle: { render: () => null },
}));
const SID = "00000000-0000-4000-8000-000000000001";
const RUN = "00000000-0000-4000-8000-000000000002";
const CHAIN = "00000000-0000-4000-8000-000000000003";
const HEAD = "00000000-0000-4000-8000-000000000004";
export function observationView(status = "running"): PublicSession {
  const nodes = [AGENT_BINDINGS.A, AGENT_BINDINGS.B, OUTPUT_BINDING].map((binding, index) => ({
    node_binding_id: binding, label: String(index), status: index ? "idle" : status,
    run_id: index ? null : RUN, revision: index ? null : 2,
  }));
  return {
    workflow_session_id: SID, revision: 3, mode: "offline", can_submit: false,
    head_commit_id: HEAD, available_actions: ["interrupt"], nodes, messages: [],
    chains: [{ chain_run_id: CHAIN, status }],
    observation: {
      schema_version: 1, kind: "workflow_observation", workflow_session_id: SID,
      session_revision: 3, head_commit_id: HEAD, chain_run_id: CHAIN, chain_status: status,
      diagnostic: null, nodes: nodes.map((node) => ({
        ...node, workflow_session_id: SID, chain_run_id: CHAIN,
        source_workflow_session_id: node.run_id ? SID : null, diagnostic: null,
        result: { availability: "unavailable", reason_code: "no_result" },
      })),
    },
  };
}
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
describe("scoped public node observations", () => {
  it("accepts matching identities and rejects unregistered private result fields", () => {
    const view = observationView();
    expect(validObservation(view.observation, view)).toBe(true);
    const changed = structuredClone(view.observation) as WorkflowObservation;
    changed.workflow_session_id = HEAD;
    expect(validObservation(changed, view)).toBe(false);
    const privateView = structuredClone(view);
    Object.assign(privateView.observation!.nodes[0].result, { messages: ["private"] });
    expect(() => decodeWorkbenchSession(privateView, SID)).toThrow();
  });
  it("clears stale running state when the read fails and does not persist observations", async () => {
    vi.useFakeTimers();
    setActivePinia(createPinia());
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(observationView())))
      .mockRejectedValueOnce(new Error("offline")));
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.restoreSession(SID);
    expect(runtime.nodeStatus("A")).toBe("running");
    expect(JSON.stringify(runtime.exportRuntimeSnapshot())).not.toContain("observation");
    await runtime.refresh();
    expect(runtime.nodeObservation("A")).toBeNull();
    expect(runtime.refreshRequired).toBe(true);
    runtime.$dispose();
  });
  it.each(["running", "paused", "succeeded"])("keeps selection distinct from the %s border", async status => {
    const pinia = createPinia();
    setActivePinia(pinia);
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(observationView(status)))));
    const runtime = useWorkbenchRuntimeStore();
    await runtime.activateWorkflow(MAIN_WORKFLOW_ID);
    await runtime.restoreSession(SID);
    const html = await renderToString(createSSRApp(MainFlowNode, {
      id: "a", selected: true, data: { stage: "A", title: "Agent A" },
    }).use(pinia));
    expect(html.includes("main-flow-node-running")).toBe(status === "running");
    expect(html).toContain("main-flow-node-selected");
    expect(html).toContain("运行详情");
    runtime.$dispose();
  });
});
