import { describe, expect, it } from "vitest";
import { createSSRApp, defineComponent, h, provide, ref } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createPinia } from "pinia";
import { createWorkbenchFrontendHost, type WorkbenchFrontendPackageModule } from "./workflowFrontendPackage";
import { workbenchFrontendHostKey } from "./workbenchFrontendHost";
import { workflowFrontendExtensions } from "../plugins/workflowFrontendManifest";
import { promptFrontendExtensions } from "../plugins/promptFrontendManifest";
import { graphClone } from "../domain/workflowGraph";
import GraphNodeConfiguration from "../components/GraphNodeConfiguration.vue";
import GraphSessionData from "../components/GraphSessionData.vue";
import { frontendCatalog, frontendSession } from "../testUtils/frontendFixture";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";

function fakePackage() {
  const declarations = workflowFrontendExtensions.slice(0, 3).map(row => {
    const declaration = graphClone(row);
    declaration.package_id = "plugin.custom-ui"; declaration.package_version = "2.1.0";
    declaration.extension_id = `plugin.custom-ui.${row.binding.slot}`;
    declaration.entrypoint = `plugin.custom-ui.local.${row.binding.slot}`;
    if (row.binding.slot === "session-object")
      declaration.binding.target = { scope: "session", type_id: "plugin.custom-state", schema_version: 7 };
    if (row.binding.slot === "node-fields") {
      declaration.component_id = "plugin.custom-node"; declaration.component_version = "4";
      declaration.binding.target = { component_id: declaration.component_id, component_version: declaration.component_version };
    }
    return declaration;
  });
  const module: WorkbenchFrontendPackageModule = {
    implementations: declarations.map(declaration => ({
      declaration, configurationFields: declaration.binding.slot === "node-fields" ? ["generic"] : undefined,
      implementation: defineComponent({
        props: ["node", "objectKey"],
        setup: props => () => h("span", `${declaration.binding.slot}:${props.objectKey ?? props.node?.component_id ?? "custom-panel"}`),
      }),
    })),
  };
  return { declarations, module, package: { package_id: "plugin.custom-ui", version: "2.1.0" } };
}

describe("local package composition lifecycle", () => {
  it("loads prompt management and exact reference fields from the default trusted modules without compatibility", () => {
    const declarations = ref(graphClone(promptFrontendExtensions));
    const packages = ref([{ package_id: "workflow.prompts", version: "1.0.0" }]);
    const host = createWorkbenchFrontendHost(() => declarations.value, () => packages.value);
    expect(host.issues.value).toEqual([]);
    expect(host.extensions.value.map(row => row.declaration)).toEqual(promptFrontendExtensions);
    expect(host.extensions.value.find(row => row.declaration.binding.slot === "node-fields")!
      .configurationFields).toEqual(["reference"]);
    packages.value = [];
    expect(host.extensions.value).toEqual([]);
    packages.value = [{ package_id: "workflow.prompts", version: "2.0.0" }];
    expect(host.extensions.value).toEqual([]);
    packages.value = [{ package_id: "workflow.prompts", version: "1.0.0" }];
    declarations.value[1]!.binding.target.component_version = "99";
    expect(host.extensions.value.map(row => row.declaration.binding.slot)).toEqual(["panel"]);
    expect(host.issues.value).not.toEqual([]);
  });

  it("mounts only exact local workbench components and unloads them synchronously", () => {
    const declarations = ref(workflowFrontendExtensions);
    const packages = ref([{ package_id: "workflow.frontend", version: "1.0.0" }]);
    const host = createWorkbenchFrontendHost(() => declarations.value, () => packages.value);
    expect(host.extensions.value.map(row => row.declaration.binding.slot)).toEqual(["panel", "session-object", "node-fields"]);
    expect(host.extensions.value.every(row => !!row.component)).toBe(true);
    packages.value = [];
    expect(host.extensions.value).toEqual([]);
    packages.value = [{ package_id: "workflow.frontend", version: "2.0.0" }];
    expect(host.extensions.value).toEqual([]);
    packages.value = [{ package_id: "workflow.frontend", version: "1.0.0" }];
    expect(host.extensions.value).toHaveLength(3);
    declarations.value = [];
    expect(host.extensions.value).toEqual([]);
  });
  it("loads another trusted panel, renderer and editor by module registration alone", async () => {
    const fake = fakePackage();
    const declarations = ref([...workflowFrontendExtensions, ...fake.declarations]);
    const packages = ref([{ package_id: "workflow.frontend", version: "1.0.0" }, fake.package]);
    const host = createWorkbenchFrontendHost(() => declarations.value, () => packages.value);
    expect(host.extensions.value).toHaveLength(3);
    expect(host.issues.value).toHaveLength(3);
    host.register(fake.module);
    expect(host.issues.value).toEqual([]);
    expect(host.extensions.value).toHaveLength(6);
    expect(host.extensions.value.filter(row => row.declaration.binding.slot === "panel")).toHaveLength(2);
    const custom = host.extensions.value.filter(row => row.declaration.package_id === fake.package.package_id);
    const html = await renderToString(createSSRApp({
      render: () => h("main", custom.map(row => h(row.component, { objectKey: "custom-object",
        node: { component_id: "plugin.custom-node" } }))),
    }));
    expect(html).toContain("panel:custom-object"); expect(html).toContain("session-object:custom-object");
    expect(html).toContain("node-fields:custom-object");
    packages.value = packages.value.filter(row => row.package_id !== fake.package.package_id);
    expect(host.extensions.value).toHaveLength(3);
  });
  it("accepts arbitrary trusted package modules during construction without builtins", () => {
    const fake = fakePackage();
    const host = createWorkbenchFrontendHost(() => fake.declarations, () => [fake.package], [fake.module]);
    expect(host.extensions.value.map(row => row.declaration.binding.target)).toEqual(fake.declarations.map(row => row.binding.target));
    expect(host.extensions.value.find(row => row.declaration.binding.slot === "node-fields")!.configurationFields).toEqual(["generic"]);
    expect(host.issues.value).toEqual([]);
  });
  it("rejects every competing exact target even when both local packages are trusted", () => {
    const fake = fakePackage();
    fake.declarations[1].binding.target = graphClone(workflowFrontendExtensions[1].binding.target);
    const host = createWorkbenchFrontendHost(() => [...workflowFrontendExtensions, ...fake.declarations],
      () => [{ package_id: "workflow.frontend", version: "1.0.0" }, fake.package]);
    host.register(fake.module);
    expect(host.extensions.value.filter(row => row.declaration.binding.slot === "session-object")).toEqual([]);
    expect(host.extensions.value.filter(row => row.declaration.binding.slot === "panel")).toHaveLength(2);
    expect(host.issues.value.filter(issue => issue.includes("冲突"))).toHaveLength(2);
  });
  it("shares one registry across the actual panel, session renderer and node field slots", async () => {
    const fake = fakePackage(), pinia = createPinia(), graph = useWorkflowGraphStore(pinia);
    graph.setPersistenceGuard(() => true);
    graph.packageLock = [fake.package]; graph.frontendExtensions = fake.declarations;
    const definition = { ...graphClone(frontendCatalog[0]), component_id: "plugin.custom-node",
      component_version: "4", config_schema: { properties: { generic: { type: "string" } } } };
    graph.catalog = [definition]; const id = graph.createWorkflow();
    const nodeId = graph.addNode("plugin.custom-node@4", { x: 0, y: 0 })!;
    const node = graph.document!.nodes.find(row => row.node_binding_id === nodeId)!;
    const session = frontendSession(id), object = session.objects!.frontend;
    object.type_id = "plugin.custom-state"; object.schema_version = 7;
    object.binding.type_id = object.type_id; object.binding.schema_version = object.schema_version;
    graph.entries[id].session_id = session.workflow_session_id; graph.views[session.workflow_session_id] = session;
    const host = createWorkbenchFrontendHost(() => graph.frontendExtensions, () => graph.packageLock, []);
    const Root = defineComponent({
      setup() {
        provide(workbenchFrontendHostKey, host);
        return () => h("main", [
          ...host.extensions.value.filter(row => row.declaration.binding.slot === "panel")
            .map(row => h(row.component)),
          h(GraphNodeConfiguration, { node, definition, document: graph.document!, catalog: graph.catalog }),
          h(GraphSessionData),
        ]);
      },
    });
    try {
      let html = await renderToString(createSSRApp(Root).use(pinia));
      expect(html).toContain("generic"); expect(html).toContain("会话对象");
      expect(html).not.toContain("node-fields:"); expect(html).not.toContain("session-object:");
      host.register(fake.module);
      html = await renderToString(createSSRApp(Root).use(pinia));
      expect(html).toContain("panel:custom-panel");
      expect(html).toContain("node-fields:plugin.custom-node"); expect(html).toContain("session-object:frontend");
      expect(html).not.toContain("<textarea");
      graph.packageLock = [];
      html = await renderToString(createSSRApp(Root).use(pinia));
      expect(html).toContain("generic"); expect(html).toContain("会话对象");
      expect(html).not.toContain("panel:custom-panel"); expect(html).not.toContain("node-fields:");
      expect(html).not.toContain("session-object:");
    } finally {
      graph.$dispose(); useWorkspaceStore(pinia).$dispose(); pinia._s.forEach(store => store.$dispose());
    }
  });
  it("rejects field ownership on a panel and duplicate field names without enabling the package", () => {
    const fake = fakePackage();
    const invalidPanel = { ...fake.module.implementations[0], configurationFields: ["generic"] };
    expect(() => createWorkbenchFrontendHost(() => fake.declarations, () => [fake.package],
      [{ implementations: [invalidPanel] }])).toThrow("配置字段接管");
    const invalidFields = { ...fake.module.implementations[2], configurationFields: ["generic", "generic"] };
    expect(() => createWorkbenchFrontendHost(() => fake.declarations, () => [fake.package],
      [{ implementations: [invalidFields] }])).toThrow("配置字段接管");
  });
});
