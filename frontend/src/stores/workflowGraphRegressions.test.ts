import { stubGraphApplicationFetch } from "../testUtils/graphApplicationServer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { usePreparationStore } from "./preparation";
import { useExposuresStore } from "./exposures";
import { useWorkspaceStore } from "./workspace";
import { useModelConfigurationStore } from "./modelConfiguration";
import { useWorkbenchPersistenceStore } from "./workbenchPersistence";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import { legacyWorkbenchStorage } from "../testUtils/legacyWorkbenchStorage";
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => { useWorkbenchPersistenceStore().$dispose(); vi.unstubAllGlobals(); });
describe("architecture findings B01 B02 B03", () => {
  it("B01 restores context source config while dropping observation from a different binding", () => {
    const preparation = usePreparationStore(); const graph = preparation.getDraft(MAIN_WORKFLOW_ID,"A");
    const context = graph.nodes.find(n => n.kind === "context")!;
    preparation.updateNodeConfig(MAIN_WORKFLOW_ID,"A",context.id,{ sourceBindingId:AGENT_BINDINGS.B });
    preparation.undo(MAIN_WORKFLOW_ID,"A");
    expect(preparation.getDraft(MAIN_WORKFLOW_ID,"A").nodes.find(n => n.id === context.id)?.config).toMatchObject({ sourceBindingId:undefined, reference:null });
    preparation.redo(MAIN_WORKFLOW_ID,"A");
    expect(preparation.getDraft(MAIN_WORKFLOW_ID,"A").nodes.find(n => n.id === context.id)?.config).toMatchObject({ sourceBindingId:AGENT_BINDINGS.B, reference:null });
  });
  it("B02 retains other workflows' exact publication references", async () => {
    const exposures = useExposuresStore(); exposures.setPersistenceGuard(() => true);
    const second = crypto.randomUUID();
    exposures.register(MAIN_WORKFLOW_ID,"A","state","state",["status"]);
    exposures.register(second,"A","state","state",["status"]);
    stubGraphApplicationFetch(vi.fn(async (_path,init) => new Response(JSON.stringify(JSON.parse(init.body).record))));
    await exposures.publish(MAIN_WORKFLOW_ID); const first = exposures.referenceFor(MAIN_WORKFLOW_ID);
    await exposures.publish(second);
    expect(exposures.referenceFor(MAIN_WORKFLOW_ID)).toEqual(first); expect(exposures.referenceFor(second)).not.toBeNull();
  });
  it("B03 undoes a mixed preparation/main/model movement as one edit", () => {
    const persistence = useWorkbenchPersistenceStore();
    persistence.initialize(legacyWorkbenchStorage());
    const workspace = useWorkspaceStore(); const preparation = usePreparationStore(); const models = useModelConfigurationStore();
    const prompt = preparation.getDraft(MAIN_WORKFLOW_ID,"A").nodes.find(n => n.kind === "prompt-item")!;
    preparation.updateNodeConfig(MAIN_WORKFLOW_ID,"A",prompt.id,{ text:"draft" });
    const id = workspace.activeWorkflowId; const model = models.addNode(id,{ x:600,y:700 })!;
    const main = workspace.nodes[0]; const originalPrompt = { ...preparation.getDraft(id,"A").nodes.find(n => n.id === prompt.id)!.position };
    const originalMain = { ...main.position };
    persistence.mutateLegacy(id,target => {
      preparation.moveNodes(target,"A",[{ id:prompt.id,position:{ x:10,y:20 } }]);
      workspace.drafts[target].nodes[0].position = { x:30,y:40 };
      models.updateNode(target,model,{ position:{ x:50,y:60 } });
    });
    persistence.undoLegacy(id);
    expect(preparation.getDraft(id,"A").nodes.find(n => n.id === prompt.id)?.position).toEqual(originalPrompt);
    expect(workspace.nodes[0].position).toEqual(originalMain);
    expect(models.getDraft(id)?.nodes.find(n => n.id === model)?.position).toEqual({ x:600,y:700 });
    persistence.undoLegacy(id,true);
    expect(workspace.nodes[0].position).toEqual({ x:30,y:40 });
    expect(models.getDraft(id)?.nodes.find(n => n.id === model)?.position).toEqual({ x:50,y:60 });
  });
});
