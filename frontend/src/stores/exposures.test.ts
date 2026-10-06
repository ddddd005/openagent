import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useExposuresStore } from "./exposures";
import { readPublicExposure } from "../domain/exposure";
import { AGENT_BINDINGS, type PublicSession } from "../adapters/workbenchApi";

const SID = "00000000-0000-4000-8000-000000000001";
const RUN = "00000000-0000-4000-8000-000000000002";
const CHAIN = "00000000-0000-4000-8000-000000000003";
function view(): PublicSession {
  return {
    workflow_session_id: SID, revision: 1, mode: "offline", can_submit: true,
    available_actions: [],
    nodes: [
      { node_binding_id: AGENT_BINDINGS.A, label: "A", status: "succeeded", run_id: RUN, revision: 1 },
      { node_binding_id: AGENT_BINDINGS.B, label: "B", status: "succeeded", run_id: RUN, revision: 1 },
    ],
    chains: [{ chain_run_id: CHAIN, status: "succeeded" }],
    messages: [{
      visible_message_id: SID, role: "assistant", payload: { text: "Delivered", private: "secret" },
      sequence: 2, chain_run_id: CHAIN,
    }],
  };
}
beforeEach(() => setActivePinia(createPinia()));
describe("safe reusable information registrations", () => {
  it("keeps workflow registrations while clearing observations on session switch", () => {
    const store = useExposuresStore();
    const descriptor = store.register("workflow", "A", "A.state", "state", ["status", "revision"]);
    store.observe("workflow", view());
    expect(store.observations[descriptor.id].value).toEqual({ status: "succeeded", revision: 1 });
    store.clearObservations();
    expect(store.registrations).toHaveLength(1);
    expect(store.observations).toEqual({});
  });
  it("does not pretend A result is the public B reply and excludes private fields", () => {
    const store = useExposuresStore();
    const a = store.register("workflow", "A", "A.result", "result", ["delivered_text"]);
    const b = store.register("workflow", "B", "B.result", "result", ["delivered_text"]);
    expect(readPublicExposure(a, view()).availability).toBe("unavailable");
    expect(readPublicExposure(b, view()).value).toEqual({ delivered_text: "Delivered" });
  });
  it("does not associate a previously delivered reply with a newer chain/run", () => {
    const store = useExposuresStore();
    const b = store.register("workflow", "B", "B.result", "result", ["delivered_text"]);
    const current = view();
    current.chains.push({ chain_run_id: SID, status: "succeeded" });
    expect(readPublicExposure(b, current).availability).toBe("unavailable");
    current.messages[0].chain_run_id = SID;
    current.chains[1].status = "running";
    expect(readPublicExposure(b, current).availability).toBe("unavailable");
  });
  it("requires unique public names and a controlled field whitelist", () => {
    const store = useExposuresStore();
    store.register("workflow", "A", "state", "state", ["status", "delivered_text"]);
    expect(store.registrations[0].fields).toEqual(["status"]);
    expect(() => store.register("workflow", "B", "state", "state", ["status"])).toThrow();
    expect(() => store.register("workflow", "A", "", "state", ["status"])).toThrow();
    expect(() => store.register("workflow", "A", "invalid", "result", ["status"])).toThrow();
  });
});
