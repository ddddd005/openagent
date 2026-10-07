import { computed, createSSRApp, defineComponent, h } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { renderToString } from "@vue/server-renderer";
import { afterEach, describe, expect, it, vi } from "vitest";
import ProviderSidebar from "./ProviderSidebar.vue";
import ModelSourceFields from "./ModelSourceFields.vue";
import { createProviderResources, modelResourceControllerKey } from "../application/workflowResources";
import { listCurrentProviders, readCurrentProviderReceipt, saveCurrentProvider,
  type ProviderSaveRequest } from "../adapters/workflowResourcesApi";
import { graphReceiptReadResponse } from "../testUtils/graphApplicationServer";
import { chatProviderType, newProvider, providerIdentity, type CurrentProvider } from "../domain/workflowModelResources";
import { graphClone, newGraph, type GraphNode } from "../domain/workflowGraph";
import { modelFrontendExtensions } from "../plugins/modelFrontendManifest";
import { workflowFrontendSdkKey, type WorkflowFrontendSdk } from "../plugins/workflowFrontendSdk";
import { useWorkflowGraphStore } from "../stores/workflowGraph";

type SaveOutcome = "accept" | "accepted-without-response" | "stale_revision" | "invalid_origin" | "idempotency_conflict";
type ResourceController = ReturnType<typeof createProviderResources>;

function controlledResources(initial: CurrentProvider[] = []) {
  let persisted: string | null = null;
  const state = { records: graphClone(initial), outcome: "accept" as SaveOutcome };
  const requests: { path: string; body: string }[] = [];
  const accepted = new Map<string, ProviderSaveRequest>();
  vi.stubGlobal("fetch", vi.fn(async (path: string, options?: RequestInit) => {
    const body = String(options?.body);
    requests.push({ path, body });
    const request = JSON.parse(body);
    expect(options?.method).toBe("POST");
    if (path === "/api/graph/queries") {
      expect(request).toEqual({ operation: "resource.list", parameters: { type_id: chatProviderType } });
      return new Response(JSON.stringify(state.records));
    }
    expect(["/api/graph/commands", "/api/graph/receipts/read"]).toContain(path);
    expect(request.operation).toBe("resource.save");
    const parameters = request.parameters as ProviderSaveRequest;
    if (["stale_revision", "invalid_origin", "idempotency_conflict"].includes(state.outcome)) {
      return new Response(JSON.stringify({ error: { reason_code: state.outcome } }), {
        status: state.outcome === "invalid_origin" ? 403 : 409,
      });
    }
    const previous = accepted.get(parameters.idempotency_key);
    if (path === "/api/graph/receipts/read") {
      if (!previous) return new Response(JSON.stringify({ schema_version: 1,
        kind: "workflow.application-receipt-read", outcome: "unresolved", reason_code: "application_identity_missing" }));
      expect(parameters).toEqual(previous);
      return graphReceiptReadResponse("resource.save", parameters as unknown as Record<string, unknown>, {
        reference: providerIdentity(previous.record), update_sequence: previous.record.update_sequence, deleted: false,
      });
    }
    if (previous) expect(parameters).toEqual(previous);
    else {
      accepted.set(parameters.idempotency_key, graphClone(parameters));
      state.records = state.records.filter(record => record.resource_id !== parameters.record.resource_id);
      state.records.push(graphClone(parameters.record));
    }
    if (state.outcome === "accepted-without-response") throw new TypeError("Controlled response loss");
    const result = { reference: providerIdentity(parameters.record),
      update_sequence: parameters.record.update_sequence, deleted: false };
    return new Response(JSON.stringify({
      schema_version: 1, kind: "workflow.application-command", result,
      receipt: { schema_version: 1, kind: "workflow.application-receipt", operation: "resource.save",
        operation_scope: "management", idempotency_key: parameters.idempotency_key,
        request_sha256: "a".repeat(64), authority: "service_receipt", target: {}, accepted: result },
    }));
  }));
  return {
    state, requests,
    pending: () => persisted,
    commands: () => requests.filter(request => request.path === "/api/graph/commands"),
    receipts: () => requests.filter(request => request.path === "/api/graph/receipts/read"),
    create: () => createProviderResources({
      list: listCurrentProviders, save: saveCurrentProvider, readReceipt: readCurrentProviderReceipt,
      readPending: () => persisted, writePending: value => { persisted = value; },
    }),
  };
}

async function renderResources(resources: ResourceController, reference: CurrentProvider) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const graph = useWorkflowGraphStore();
  graph.createWorkflow();
  graph.frontendExtensions = graphClone(modelFrontendExtensions);
  graph.packageLock = [{ package_id: "workflow.models", version: "1.0.0" }];
  const node: GraphNode = {
    node_binding_id: crypto.randomUUID(), component_id: "models.source", component_version: "1",
    title: "Model source", position: { x: 0, y: 0 },
    config: { reference: providerIdentity(reference),
      parameters: { model: "offline-model", thinking: "disabled", stream: false } },
  };
  const document = { ...newGraph("Current provider fields"), nodes: [node] };
  const configureNode = vi.fn();
  const sdk: WorkflowFrontendSdk = {
    workflowId: computed(() => document.workflow_definition_id), document: computed(() => document),
    catalog: computed(() => []), packages: computed(() => graph.packageLock), session: computed(() => null),
    locked: computed(() => false), lifecycle: computed(() => "resource-test"),
    attachDisplay: () => false, readArtifact: vi.fn(), configureNode,
  };
  // SSR does not run mounted hooks or form events; the real controller is exercised separately.
  const root = defineComponent({ setup: () => () => h("main", [
    h(ProviderSidebar), h(ModelSourceFields, { node, document }),
  ]) });
  try {
    const html = await renderToString(createSSRApp(root).use(pinia)
      .provide(modelResourceControllerKey, resources).provide(workflowFrontendSdkKey, sdk));
    expect(configureNode).not.toHaveBeenCalled();
    return html;
  } finally {
    pinia._s.forEach(store => store.$dispose());
  }
}

function expectSharedProvider(html: string, record: CurrentProvider) {
  expect(html).toContain("current-provider-panel");
  expect(html).not.toContain('class="provider-panel"');
  const fields = html.slice(html.indexOf('class="model-source-fields"'));
  expect(fields).toContain(record.value.name);
  expect(fields).toContain(record.resource_id);
  expect(fields).toContain(`s${record.update_sequence}`);
  expect(fields).not.toContain("\u7f3a\u5931\u5f15\u7528");
}

afterEach(() => vi.unstubAllGlobals());

describe("provider sidebar SSR and current-resource controller integration", () => {
  it("makes a newly saved sidebar resource selectable by the model-source fields through the same current API", async () => {
    const backend = controlledResources(), resources = backend.create(), provider = newProvider();
    provider.value.name = "Shared current provider";
    expect(await resources.save(provider, 0)).toBe(true);
    expect(backend.pending()).toBeNull();
    expectSharedProvider(await renderResources(resources, provider), provider);
    expect(JSON.parse(backend.commands()[0]!.body)).toMatchObject({
      operation: "resource.save", parameters: { record: provider, expected_sequence: 0 },
    });
    expect(backend.requests.map(request => request.path)).toEqual(["/api/graph/commands", "/api/graph/queries"]);
    expect(resources.records.value).toEqual([provider]);
  });

  it("keeps an accepted but unknown save frozen across controller reconstruction and reconciles its original body/key", async () => {
    const backend = controlledResources(), original = backend.create(), provider = newProvider();
    provider.value.name = "Original pending provider";
    backend.state.outcome = "accepted-without-response";
    expect(await original.save(provider, 0)).toBe(false);
    const persisted = backend.pending(), submitted = backend.commands()[0]!.body;
    provider.value.name = "Later unsent edit";
    const reopened = backend.create();
    expect(reopened.pending.value?.record.value.name).toBe("Original pending provider");
    expect(reopened.locked.value).toBe(true);
    await reopened.refresh();
    const pendingHtml = await renderResources(reopened, provider);
    expect(pendingHtml).toContain("\u8d44\u6e90\u63d0\u4ea4\u7ed3\u679c\u5f85\u6838\u5b9e");
    expect(pendingHtml).toMatch(/aria-label="\u65b0\u589e\u4f9b\u5e94\u5546\u8d44\u6e90"[^>]*disabled/);
    expect(backend.pending()).toBe(persisted);
    expect(await reopened.save(provider, 0)).toBe(false);
    expect(backend.commands()).toHaveLength(1);
    backend.state.outcome = "accept";
    expect(await reopened.reconcile()).toBe(true);
    expect(backend.commands()).toHaveLength(1); expect(backend.receipts()[0]!.body).toBe(submitted);
    expect(backend.pending()).toBeNull();
    expect(reopened.locked.value).toBe(false);
    expectSharedProvider(await renderResources(reopened, reopened.records.value[0]!), reopened.records.value[0]!);
    expect(reopened.records.value[0]!.value.name).toBe("Original pending provider");
  });

  it("uses the observed CAS sequence and unlocks a newly rejected stale save without replacing the current resource", async () => {
    const provider = newProvider();
    provider.value.name = "Current sequence eight"; provider.update_sequence = 8;
    const backend = controlledResources([provider]), resources = backend.create();
    await resources.refresh();
    const stale = graphClone(provider); stale.update_sequence = 7; stale.value.name = "Stale unsaved edit";
    backend.state.outcome = "stale_revision";
    expect(await resources.save(stale, 7)).toBe(false);
    expect(JSON.parse(backend.commands()[0]!.body).parameters).toMatchObject({
      expected_sequence: 7, record: { update_sequence: 8, value: { name: "Stale unsaved edit" } },
    });
    expect(backend.pending()).toBeNull();
    expect(resources.pending.value).toBeNull();
    expect(resources.locked.value).toBe(false);
    expect(resources.error.value).toContain("stale_revision");
    expectSharedProvider(await renderResources(resources, provider), provider);
    expect(resources.records.value).toEqual([provider]);
  });

  it.each(["stale_revision", "invalid_origin", "idempotency_conflict"] as const)(
    "does not settle an older unknown request when its reconciliation is rejected with %s", async code => {
      const backend = controlledResources(), original = backend.create(), provider = newProvider();
      backend.state.outcome = "accepted-without-response";
      expect(await original.save(provider, 0)).toBe(false);
      const persisted = backend.pending(), submitted = backend.commands()[0]!.body;
      const reopened = backend.create();
      backend.state.outcome = code;
      expect(await reopened.reconcile()).toBe(false);
      expect(backend.commands()).toHaveLength(1); expect(backend.receipts()[0]!.body).toBe(submitted);
      expect(backend.pending()).toBe(persisted);
      expect(reopened.pending.value).toEqual(JSON.parse(persisted!));
      expect(reopened.locked.value).toBe(true);
      expect(reopened.error.value).toContain(code);
      expect(await reopened.save(newProvider(), 0)).toBe(false);
      expect(backend.commands()).toHaveLength(1);
      const html = await renderResources(reopened, provider);
      expect(html).toContain("\u8d44\u6e90\u63d0\u4ea4\u7ed3\u679c\u5f85\u6838\u5b9e");
      expect(html).toMatch(/aria-label="\u65b0\u589e\u4f9b\u5e94\u5546\u8d44\u6e90"[^>]*disabled/);
    },
  );
});
