<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from "vue";
import { MarkerType, VueFlow, type Connection, type OnConnectStartParams, type VueFlowStore } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { ArrowDown, ArrowUp, Copy, Crosshair, Database, Plus, Redo2, RefreshCw, Settings2, Trash2, Undo2, X } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { nodePorts, type GraphNode } from "../domain/workflowGraph";
import { buildNodeFamilies, nodeDisplayTitle, nodeMenuGroups, nodeProfileLabel, nodeTypeLabel } from "../domain/nodeCatalog";
import { captureNodeSelection, nodeSelectionAllowed, nodeSelectionIssue, type NodeSelectionRequest } from "../domain/nodeSelection";
import GraphCanvasNode from "./GraphCanvasNode.vue";
import GraphNodeConfiguration from "./GraphNodeConfiguration.vue";
import CanvasNodeMenu from "./CanvasNodeMenu.vue";
import NodeProfileDialog from "./NodeProfileDialog.vue";
import GraphSessionData from "./GraphSessionData.vue";
import GraphObjectBindings from "./GraphObjectBindings.vue";
import GraphInformation from "./GraphInformation.vue";
import GraphEvents from "./GraphEvents.vue";
import GraphResult from "./GraphResult.vue";
import { graphConnectionIssue } from "../domain/graphConnections";
import { resultStatusLabel } from "../domain/resultPresentation";
import { modelPresentationJson } from "../domain/modelPresentation";
import { useInstalledWorkbenchFrontendHost } from "../application/workbenchFrontendHost";
import { useCanvasNodeMenu } from "../composables/useCanvasNodeMenu";
import { useVolatileFormState } from "../composables/useVolatileFormState";
import { diagnosticRecovery } from "../domain/diagnosticPresentation";
const graph = useWorkflowGraphStore();
const workspace = useWorkspaceStore();
const navigationError = ref("");
let navigationGeneration = 0;
function leaveEditor(change: () => void) {
  const token = ++navigationGeneration, id = workspace.activeWorkflowId;
  if (token !== navigationGeneration || id !== workspace.activeWorkflowId) return;
  navigationError.value = "";
  change();
}
function selectNodes(ids: string[], edgeId: string | null = null, closePanels = false) {
  void leaveEditor(() => {
    if (closePanels) showEvents.value = showFrontend.value = showBindings.value = showInformation.value = false;
    graph.selectedNodeIds = ids; graph.selectedEdgeId = edgeId;
  });
}
function panel(name: "events" | "frontend" | "bindings" | "information") {
  void leaveEditor(() => {
    const previous = { events: showEvents.value, frontend: showFrontend.value,
      bindings: showBindings.value, information: showInformation.value };
    showEvents.value = name === "events" && !previous.events;
    showFrontend.value = name === "frontend" && !previous.frontend;
    showBindings.value = name === "bindings" && !previous.bindings;
    showInformation.value = name === "information" && !previous.information;
  });
}
const frontendHost = useInstalledWorkbenchFrontendHost();
const frontendPanels = computed(() => frontendHost.extensions.value.filter(row => row.declaration.binding.slot === "panel"));
const canvas = ref<HTMLElement | null>(null);
const menu = useCanvasNodeMenu(canvas);
const flow = shallowRef<VueFlowStore | null>(null);
const connecting = ref<OnConnectStartParams | null>(null), connectionMessage = ref("");
function connectionIssue(connection: Connection) {
  if (!graph.document || graph.editorLocked) return "当前不可连线";
  return graphConnectionIssue(graph.document, graph.catalog, { source_node_id: connection.source,
    source_port_id: connection.sourceHandle ?? "", target_node_id: connection.target,
    target_port_id: connection.targetHandle ?? "" });
}
function validConnection(connection: Connection & { id?: string }) {
  // VueFlow also validates supplied edges; retain them for display and diagnostics.
  const document = graph.document;
  if (document && connection.id && (document.edges.some(edge => edge.edge_id === connection.id
    && edge.source_node_id === connection.source && edge.target_node_id === connection.target
    && edge.source_port_id === connection.sourceHandle && edge.target_port_id === connection.targetHandle)
    || document.control_edges?.some(edge => edge.edge_id === connection.id
      && edge.source_node_id === connection.source && edge.target_node_id === connection.target
      && connection.sourceHandle === "__control_out" && connection.targetHandle === "__control_in"))) return true;
  const issue = connectionIssue(connection);
  if (connecting.value) connectionMessage.value = issue ?? "端口兼容，可连接";
  return !issue;
}
function portIssues(node: GraphNode, direction: "inputs" | "outputs") {
  const from = connecting.value;
  if (!from?.nodeId || !from.handleId || (direction === "inputs") === (from.handleType === "target")) return undefined;
  return Object.fromEntries(nodePorts(graph.typeFor(node), node, direction).map(port => [port.port_id,
    connectionIssue(direction === "inputs"
      ? { source: from.nodeId!, sourceHandle: from.handleId, target: node.node_binding_id, targetHandle: port.port_id }
      : { source: node.node_binding_id, sourceHandle: port.port_id, target: from.nodeId!, targetHandle: from.handleId })]));
}
let initialFit = true;
const inspectorTab = ref<"config" | "result" | "information">("config");
const showBindings = ref(false);
const showInformation = ref(false);
const showFrontend = ref(false);
const showEvents = ref(false);
const selectedNode = computed(() => graph.document?.nodes.find(n => graph.selectedNodeIds.includes(n.node_binding_id)));
const titleDraft = ref("");
const titleBasis = () => ({ workflowId: graph.document?.workflow_definition_id ?? null,
  nodeId: selectedNode.value?.node_binding_id ?? null, title: selectedNode.value?.title ?? "" });
const titleForm = useVolatileFormState({
  id: () => graph.document && selectedNode.value ? `node:${graph.document.workflow_definition_id}:${selectedNode.value.node_binding_id}:title` : "",
  basis: titleBasis,
  reset: () => { titleDraft.value = selectedNode.value ? displayTitle(selectedNode.value) : ""; },
});
function changeTitle() {
  const node = selectedNode.value, workflowId = graph.document?.workflow_definition_id,
    title = titleDraft.value, context = titleForm.context.value;
  if (!node || !workflowId || graph.editorLocked) return;
  if (context !== titleForm.context.value || graph.document?.workflow_definition_id !== workflowId
    || selectedNode.value?.node_binding_id !== node.node_binding_id) return;
  graph.patchNode(node.node_binding_id, { title });
}
const selectedType = computed(() => selectedNode.value ? graph.typeFor(selectedNode.value) : undefined);
const selectedState = computed(() => graph.session?.nodes.find(n => n.node_binding_id === selectedNode.value?.node_binding_id));
const incoming = computed(() => graph.document?.edges.filter(e => e.target_node_id === selectedNode.value?.node_binding_id).sort((a,b) => a.order-b.order) ?? []);
const predecessors = computed(() => (graph.document?.control_edges ?? [])
  .filter(edge => edge.target_node_id === selectedNode.value?.node_binding_id).map(edge => edge.source_node_id));
function predecessor(id: string, event: Event) {
  if (!selectedNode.value) return;
  const checked = (event.target as HTMLInputElement).checked;
  graph.setControlDependencies(selectedNode.value.node_binding_id,
    [...predecessors.value.filter(source => source !== id), ...(checked ? [id] : [])]);
}
const families = computed(() => buildNodeFamilies(graph.catalog));
const menuGroups = computed(() => nodeMenuGroups(families.value, graph.editorLocked || graph.catalogLoading || !!graph.catalogError));
const selectionRequest = shallowRef<NodeSelectionRequest | null>(null);
const selectionContext = computed(() => ({ workflowId: workspace.activeWorkflowId, viewVersion: workspace.viewVersion,
  sessionId: graph.active?.session_id ?? null, document: graph.document, catalog: graph.catalog,
  packageLock: graph.executionPackageLock, locked: graph.editorLocked, catalogLoading: graph.catalogLoading,
  catalogError: graph.catalogError }));
const selectionIssue = computed(() => selectionRequest.value ? nodeSelectionIssue(selectionRequest.value, selectionContext.value) : null);
function displayTitle(node: GraphNode) { return nodeDisplayTitle(node, graph.typeFor(node), graph.catalog); }
function titleForId(id: string) {
  const node = graph.document?.nodes.find(row => row.node_binding_id === id);
  return node ? displayTitle(node) : id;
}
const nodes = computed(() => graph.document?.nodes.map(node => ({ id: node.node_binding_id,
  type: "graph", position: { ...node.position }, selected: graph.selectedNodeIds.includes(node.node_binding_id),
  data: { node, definition: graph.typeFor(node), displayTitle: displayTitle(node), status: graph.session?.nodes.find(n => n.node_binding_id === node.node_binding_id)?.status ?? "idle",
    executionRoot: graph.document?.execution_roots?.includes(node.node_binding_id) ?? false,
    error: !!graph.session?.nodes.find(n => n.node_binding_id === node.node_binding_id)?.diagnostic,
    inputIssues: portIssues(node, "inputs"), outputIssues: portIssues(node, "outputs"),
    inputs: graph.document!.edges.filter(e => e.target_node_id === node.node_binding_id).map(e => e.target_port_id),
    outputs: graph.document!.edges.filter(e => e.source_node_id === node.node_binding_id).map(e => e.source_port_id) },
})) ?? []);
const invalidEdges = computed(() => {
  const document = graph.document;
  if (!document || graph.catalogLoading) return [];
  return document.edges.flatMap(edge => {
    const issue = graphConnectionIssue({ ...document, edges: document.edges.filter(row => row.edge_id !== edge.edge_id) },
      graph.catalog, { source_node_id: edge.source_node_id, source_port_id: edge.source_port_id,
        target_node_id: edge.target_node_id, target_port_id: edge.target_port_id });
    return issue ? [{ edgeId: edge.edge_id, issue }] : [];
  });
});
const edges = computed(() => [...(graph.document?.edges.map(edge => ({ id: edge.edge_id, source: edge.source_node_id,
  target: edge.target_node_id, sourceHandle: edge.source_port_id, targetHandle: edge.target_port_id,
  type: "smoothstep", selected: edge.edge_id === graph.selectedEdgeId, markerEnd: MarkerType.ArrowClosed,
  style: { stroke: invalidEdges.value.some(row => row.edgeId === edge.edge_id) ? "#d17e7e"
    : edge.edge_id === graph.selectedEdgeId ? "#d6e5df" : "#899e95", strokeWidth: 1.5 } })) ?? []),
  ...(graph.document?.control_edges ?? []).map(edge => ({ id: edge.edge_id, source: edge.source_node_id,
    target: edge.target_node_id, sourceHandle: "__control_out", targetHandle: "__control_in",
    type: "smoothstep", label: "先序", selected: edge.edge_id === graph.selectedEdgeId,
    markerEnd: MarkerType.ArrowClosed, style: { stroke: edge.edge_id === graph.selectedEdgeId ? "#d6e5df" : "#afa483",
      strokeWidth: 1.5, strokeDasharray: "5 4" } })),
]);
const diagnostics = computed(() => [
  ...(graph.active?.diagnostics ?? []),
  ...(graph.session?.chains.flatMap(chain => chain.diagnostic ? [chain.diagnostic] : []) ?? []),
  ...(graph.session?.nodes.flatMap(node => node.diagnostic ? [{ ...node.diagnostic, node_id: node.node_binding_id }] : []) ?? []),
]);
const json = modelPresentationJson;
function fit() { void flow.value?.fitView({ padding: 0.2, maxZoom: 0.9 }); }
function attach(instance: VueFlowStore) {
  flow.value = instance;
  const id = workspace.activeWorkflowId;
  const current = () => workspace.activeWorkflowId === id && flow.value === instance;
  instance.onNodeClick(({ node }) => { if (current()) selectNodes([node.id], null, true); });
  instance.onNodesChange(changes => {
    if (!current() || !changes.some(change => change.type === "select")) return;
    const selected = new Set(graph.selectedNodeIds);
    for (const change of changes) if (change.type === "select") {
      if (change.selected) selected.add(change.id); else selected.delete(change.id);
    }
    selectNodes([...selected]);
  });
  instance.onEdgeClick(({ edge }) => { if (current()) selectNodes([], edge.id); });
  instance.onPaneClick(() => { if (current()) { selectNodes([]); menu.close(); } });
  instance.onNodeDragStop(({ nodes }) => { if (current()) graph.moveNodes(nodes.map(node => ({ id: node.id, position: { ...node.position } }))); });
  instance.onConnect(connection => {
    if (current() && connection.sourceHandle && connection.targetHandle
      && ![connection.sourceHandle, connection.targetHandle].some(handle => handle.startsWith("__control_"))) {
      const accepted = graph.connect({ source_node_id: connection.source,
        source_port_id: connection.sourceHandle, target_node_id: connection.target, target_port_id: connection.targetHandle });
      if (accepted) connectionMessage.value = "";
    }
  });
  instance.onConnectStart(({ ...start }) => { if (current()) { connecting.value = start; connectionMessage.value = ""; } });
  instance.onConnectEnd(() => { if (current()) connecting.value = null; });
  instance.onNodesInitialized(() => { if (current() && initialFit) { fit(); initialFit = false; } });
}
function add(familyId: string) {
  if (!menu.menu.value) return;
  const position = flow.value?.screenToFlowCoordinate(menu.menu.value) ?? { x: 80, y: 80 };
  const family = families.value.find(row => row.id === familyId);
  const request = captureNodeSelection(selectionContext.value, familyId, { position });
  menu.close(true);
  if (!family || !request) return;
  const only = family.profiles.length === 1 ? family.profiles[0] : undefined;
  if (only && family.preferredKey === only.key
    && nodeSelectionAllowed(request, selectionContext.value, only.key)) graph.addNode(only.key, position);
  else selectionRequest.value = request;
}
function changeProfile() {
  if (!selectedNode.value) return;
  selectionRequest.value = captureNodeSelection(selectionContext.value, selectedNode.value.component_id,
    { nodeId: selectedNode.value.node_binding_id });
}
function confirmSelection(key: string, resetConfirmed: boolean) {
  const request = selectionRequest.value;
  if (!request || !nodeSelectionAllowed(request, selectionContext.value, key, resetConfirmed)) return;
  if (request.nodeId) graph.replaceNode(request.nodeId, key);
  else if (request.position) graph.addNode(key, request.position);
  selectionRequest.value = null;
}
function keyboard(event: KeyboardEvent) {
  if (selectionRequest.value || menu.menu.value) return;
  if ((event.target as HTMLElement).closest("input,textarea,select,[contenteditable]")) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") { event.preventDefault(); void leaveEditor(() => graph.undo(event.shiftKey)); }
  else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "d") { event.preventDefault(); void leaveEditor(() => graph.duplicateSelection()); }
  else if (event.key === "Delete" || event.key === "Backspace") { event.preventDefault(); void leaveEditor(() => graph.removeSelection()); }
  else if (event.key === "Escape") { menu.close(); selectNodes([]); }
}
function locate(id?: string) {
  if (!id) return;
  void leaveEditor(() => {
    graph.selectedNodeIds = [id];
    void nextTick(() => { void flow.value?.fitView({ nodes: [id], padding: 0.6, maxZoom: 1 }); });
  });
}
watch(() => workspace.activeWorkflowId, () => { flow.value = null; connecting.value = null; connectionMessage.value = ""; initialFit = true; menu.close(); selectionRequest.value = null; inspectorTab.value = "config"; showBindings.value = false; showInformation.value = false; showFrontend.value = false; showEvents.value = false; });
</script>

<template>
  <section class="graph-workbench" tabindex="0" aria-label="工作流节点工作台" @keydown.capture="keyboard">
    <header class="graph-toolbar">
      <span>{{ graph.document?.nodes.length ?? 0 }} 个节点</span>
      <span v-if="graph.session" class="graph-run-status">{{ resultStatusLabel(graph.session.status) }} · r{{ graph.session.definition_revision }}</span>
      <div class="graph-toolbar-actions">
        <button type="button" title="添加节点" aria-label="添加节点" :disabled="graph.editorLocked || graph.catalogLoading || !!graph.catalogError" @click="menu.open('add')"><Plus :size="15" /></button>
        <button type="button" :aria-pressed="showEvents" :disabled="!graph.document" @click="panel('events')">局部事件</button>
        <button type="button" :aria-pressed="showFrontend" :disabled="!graph.document || !frontendPanels.length" @click="panel('frontend')">前端展示</button>
        <button type="button" :aria-pressed="showBindings" :disabled="!graph.document" @click="panel('bindings')">会话绑定</button>
        <button type="button" title="统一登记目录" aria-label="统一登记目录" :aria-pressed="showInformation" @click="panel('information')"><Database :size="15" /></button>
        <button type="button" title="撤销" aria-label="撤销" :disabled="!graph.canUndo" @click="leaveEditor(() => graph.undo())"><Undo2 :size="15" /></button>
        <button type="button" title="重做" aria-label="重做" :disabled="!graph.canRedo" @click="leaveEditor(() => graph.undo(true))"><Redo2 :size="15" /></button>
        <button type="button" title="复制选中节点" aria-label="复制选中节点" :disabled="!graph.selectedNodeIds.length || graph.editorLocked" @click="leaveEditor(() => graph.duplicateSelection())"><Copy :size="15" /></button>
        <button type="button" title="定位全部节点" aria-label="定位全部节点" @click="fit"><Crosshair :size="15" /></button>
        <button type="button" title="刷新目录和运行" aria-label="刷新目录和运行" :disabled="graph.catalogLoading" @click="graph.loadCatalog(); graph.refresh()"><RefreshCw :size="15" /></button>
      </div>
    </header>
    <div v-if="graph.catalogError" class="graph-warning" role="status">{{ graph.catalogError }}</div>
    <div v-if="navigationError" class="graph-warning" role="status">{{ navigationError }}</div>
    <div v-if="invalidEdges.length" class="graph-warning" role="status">有 {{ invalidEdges.length }} 条连线需要核对：{{ invalidEdges[0]?.issue }}</div>
    <div v-if="connectionMessage" class="graph-connection-message" role="status">{{ connectionMessage }}</div>
    <div v-for="issue in frontendHost.issues.value" :key="issue" class="graph-warning" role="status">{{ issue }}</div>
    <div class="graph-body">
      <div ref="canvas" class="graph-canvas" tabindex="0" aria-label="工作流节点画布" @contextmenu="menu.contextMenu" @pointermove="menu.pointerMove" @pointerleave="menu.pointerLeave">
        <VueFlow :key="workspace.activeWorkflowId" :id="`graph-${workspace.activeWorkflowId}`" :nodes="nodes" :edges="edges" :min-zoom="0.15" :max-zoom="2" :connection-radius="28" :is-valid-connection="validConnection" :delete-key-code="null" :disable-keyboard-a11y="true" :nodes-draggable="!graph.editorLocked" :nodes-connectable="!graph.editorLocked" @init="attach">
          <Background :gap="24" :size="1" pattern-color="#4c4c53" />
          <template #node-graph="nodeProps"><GraphCanvasNode v-bind="nodeProps" /></template>
        </VueFlow>
        <CanvasNodeMenu :state="menu.menu.value" :groups="menuGroups" @select="add" @add="menu.open('add', menu.menu.value ?? undefined)" @back="menu.open('context', menu.menu.value ?? undefined)" @dismiss="menu.close()" />
      </div>
      <aside class="graph-inspector" aria-label="节点详情">
        <header><strong>{{ showEvents ? '局部事件' : showFrontend ? '前端展示接线' : showInformation ? '统一登记目录' : showBindings ? '会话对象绑定' : selectedNode ? displayTitle(selectedNode) : '运行结果' }}</strong><button v-if="!showEvents && !showFrontend && !showBindings && !showInformation && (selectedNode || graph.selectedEdgeId)" type="button" aria-label="删除选中" title="删除选中" :disabled="graph.editorLocked" @click="leaveEditor(() => graph.removeSelection())"><Trash2 :size="15" /></button></header>
        <div v-if="showEvents" class="graph-inspector-content"><GraphEvents /></div>
        <div v-else-if="showFrontend" class="graph-inspector-content"><component v-for="panel in frontendPanels"
          :key="panel.declaration.extension_id" :is="panel.component" @connected="showFrontend = false; fit()" />
          <p v-if="!frontendPanels.length" role="status">前端扩展不可用，请刷新目录。</p></div>
        <div v-else-if="showInformation" class="graph-inspector-content"><GraphInformation :session-id="graph.session?.workflow_session_id" /></div>
        <div v-else-if="showBindings && graph.document" class="graph-inspector-content">
          <GraphObjectBindings :document="graph.document" :types="graph.dataTypes" :disabled="graph.editorLocked" @change="graph.setObjectBindings($event)" />
          <GraphSessionData />
        </div>
        <template v-else-if="selectedNode">
          <div class="graph-tabs"><button type="button" :class="{ active: inspectorTab === 'config' }" @click="leaveEditor(() => inspectorTab = 'config')">配置</button><button type="button" :class="{ active: inspectorTab === 'result' }" @click="leaveEditor(() => inspectorTab = 'result')">状态与结果</button><button type="button" :class="{ active: inspectorTab === 'information' }" @click="leaveEditor(() => inspectorTab = 'information')">信息</button></div>
          <div v-if="inspectorTab === 'config'" class="graph-inspector-content">
            <label>标题<input v-model="titleDraft" :disabled="graph.editorLocked" @change="changeTitle" /></label>
            <section class="graph-node-profile" aria-label="节点类型">
              <div><strong>{{ selectedType ? nodeTypeLabel(selectedType) : selectedNode.component_id }}</strong>
                <span>{{ !selectedType ? '声明缺失' : !selectedType.executable ? '不可执行' : nodeProfileLabel(selectedType) }}</span>
                </div>
              <button type="button" aria-label="替换节点或切换模型协议" title="替换节点或切换模型协议"
                :disabled="graph.editorLocked || graph.catalogLoading || !!graph.catalogError || !families.length" @click="changeProfile"><Settings2 :size="15" /></button>
            </section>
            <GraphNodeConfiguration v-if="selectedType && graph.document" :node="selectedNode" :definition="selectedType" :document="graph.document" :catalog="graph.catalog" :disabled="graph.editorLocked" @change="graph.patchNode(selectedNode.node_binding_id, { config: $event })"
              @policy-change="graph.patchNodeConfigurationPolicy(selectedNode.node_binding_id, $event)"
              @source-change="(edgeId, source) => graph.patchFrontendSource(selectedNode!.node_binding_id, edgeId, source)" />
            <pre v-else>{{ json(selectedNode.config) }}</pre>
            <small class="graph-muted">{{ graph.document?.execution_roots?.includes(selectedNode.node_binding_id) ? '运行目标' : '按依赖执行' }} · {{ predecessors.length ? `等待 ${predecessors.length} 个前序节点` : '无前序等待' }}</small>
            <details class="graph-runtime-settings"><summary>运行设置</summary>
              <label class="graph-checkbox"><input type="checkbox" :checked="graph.document?.execution_roots?.includes(selectedNode.node_binding_id)" :disabled="graph.editorLocked" @change="graph.setExecutionRoot(selectedNode.node_binding_id, ($event.target as HTMLInputElement).checked)" />作为运行目标</label>
              <fieldset><legend>前序节点</legend><label v-for="node in graph.document?.nodes.filter(node => node.node_binding_id !== selectedNode?.node_binding_id)" :key="node.node_binding_id" class="graph-checkbox"><input type="checkbox" :checked="predecessors.includes(node.node_binding_id)" :disabled="graph.editorLocked" @change="predecessor(node.node_binding_id, $event)" />{{ displayTitle(node) }}</label></fieldset>
              <fieldset v-if="selectedType"><legend>允许聊天端读取的结果</legend><label v-for="port in nodePorts(selectedType, selectedNode, 'outputs')" :key="port.port_id" class="graph-checkbox"><input type="checkbox" :checked="selectedNode.public_outputs?.includes(port.port_id)" :disabled="graph.editorLocked" @change="graph.patchNode(selectedNode.node_binding_id, { public_outputs: ($event.target as HTMLInputElement).checked ? [...(selectedNode.public_outputs ?? []), port.port_id] : (selectedNode.public_outputs ?? []).filter(p => p !== port.port_id) })" />{{ port.port_id }}</label></fieldset>
            </details>
            <details><summary>技术详情</summary><label>节点 UUID<input :value="selectedNode.node_binding_id" readonly @focus="($event.target as HTMLInputElement).select()" /></label><code>{{ selectedNode.component_id }}@{{ selectedNode.component_version }}</code>
              <pre>{{ json(selectedNode.config) }}</pre><details v-if="selectedType?.object_accesses?.length"><summary>会话对象访问契约</summary><pre>{{ json(selectedType.object_accesses) }}</pre></details></details>
            <button v-if="graph.active?.diagnostics?.some(d => d.node_id === selectedNode?.node_binding_id && d.reason_code.includes('state'))" class="graph-reset-state" type="button" :disabled="graph.editorLocked" title="仅修改当前内存工作流；正式保存暂不可用" @click="graph.resetPrivateState(workspace.activeWorkflowId, selectedNode.node_binding_id)">重建该节点私有状态（仅内存）</button>
            <section v-if="incoming.length"><h3>输入顺序</h3><div v-for="edge in incoming" :key="edge.edge_id" class="graph-incoming"><span>{{ edge.target_port_id }} · {{ titleForId(edge.source_node_id) }} / {{ edge.source_port_id }}</span><button type="button" title="上移" aria-label="上移输入" :disabled="graph.editorLocked" @click="graph.reorderEdge(edge.edge_id, -1)"><ArrowUp :size="13" /></button><button type="button" title="下移" aria-label="下移输入" :disabled="graph.editorLocked" @click="graph.reorderEdge(edge.edge_id, 1)"><ArrowDown :size="13" /></button></div></section>
          </div>
          <div v-else-if="inspectorTab === 'information'" class="graph-inspector-content"><GraphInformation :session-id="graph.session?.workflow_session_id" :node-id="selectedNode.node_binding_id" initial-kind="information_binding" /></div>
          <div v-else class="graph-inspector-content"><span>{{ resultStatusLabel(selectedState?.status ?? 'idle') }}</span><GraphResult v-for="(value, port) in selectedState?.outputs ?? {}" :key="port" :value="value" :label="String(port)" /><p v-if="!Object.keys(selectedState?.outputs ?? {}).length" class="graph-muted">暂无结果</p><pre v-if="selectedState?.diagnostic" class="graph-warning">{{ json(selectedState.diagnostic) }}</pre></div>
        </template>
        <div v-else class="graph-inspector-content">
          <section v-for="output in graph.session?.outputs ?? []" :key="output.output_id"><h3>{{ graph.session?.nodes.find(node => node.node_binding_id === output.node_binding_id)?.label ?? '历史节点' }} / {{ output.port_id }}</h3><GraphResult :value="output.payload" /></section>
          <p v-if="!graph.session?.outputs.length" class="graph-muted">暂无结果</p>
          <button v-for="(diagnostic, index) in diagnostics" :key="index" class="graph-diagnostic" type="button" @click="locate(diagnostic.node_id)"><strong>{{ diagnostic.reason_code ?? diagnostic.code }}</strong><span>{{ diagnostic.message }}</span><span v-if="diagnosticRecovery(diagnostic)">{{ diagnosticRecovery(diagnostic) }}</span><small v-if="diagnostic.step">失败步骤：{{ diagnostic.step }}</small><small v-if="diagnostic.correlation_id">关联编号：{{ diagnostic.correlation_id }}</small></button>
          <button v-if="graph.session?.available_actions?.includes('close')" type="button" class="graph-close-run" @click="graph.closeRun()"><X :size="14" />关闭本次运行</button>
          <GraphSessionData />
        </div>
      </aside>
    </div>
    <NodeProfileDialog v-if="selectionRequest" :families="families" :family-id="selectionRequest.familyId"
      :current-key="selectionRequest.currentKey" :issue="selectionIssue" @confirm="confirmSelection" @cancel="selectionRequest = null" />
  </section>
</template>

<style scoped>
.graph-workbench { position:relative; display:flex; flex:1; flex-direction:column; height:100%; min-width:0; min-height:0; outline:none; }
.graph-toolbar { flex:0 0 36px; display:flex; align-items:center; gap:12px; padding:0 11px; border-bottom:1px solid #424248; font-size:11px; color:#b8bec2; }
.graph-toolbar-actions { display:flex; margin-left:auto; gap:3px; }
button { display:inline-flex; align-items:center; justify-content:center; min-width:25px; min-height:25px; padding:4px; border-radius:3px; }
button:not(:disabled):hover { background:#44444c; }
button:disabled { opacity:.4; }
.graph-body { display:flex; flex:1; min-height:0; }
.graph-canvas { flex:1; min-width:0; position:relative; background:#242428; outline:none; }
.graph-inspector { flex:0 0 280px; width:280px; min-width:0; background:#29292f; border-left:1px solid #48484e; display:flex; flex-direction:column; overflow:hidden; }
.graph-inspector > header { display:flex; align-items:center; justify-content:space-between; padding:8px 11px; border-bottom:1px solid #44444c; min-height:34px; font-size:12px; }
.graph-inspector > header strong { min-width:0; overflow-wrap:anywhere; }
.graph-tabs { display:flex; border-bottom:1px solid #44444c; padding:5px; gap:5px; font-size:11px; }
.graph-tabs button { padding:3px 8px; }
.graph-tabs .active { color:#c9dfd5; background:#394840; }
.graph-inspector-content { min-height:0; overflow:auto; display:flex; flex-direction:column; padding:12px; gap:12px; }
label { display:grid; gap:5px; font-size:12px; min-width:0; }
input,select { width:100%; min-width:0; box-sizing:border-box; padding:6px; color:#dedee4; background:#222226; border:1px solid #505059; border-radius:3px; }
pre { font-size:11px; line-height:1.6; white-space:pre-wrap; overflow-wrap:anywhere; margin:0; color:#c6cfca; }
h3 { margin:0 0 6px; font-size:11px; font-weight:500; overflow-wrap:anywhere; }
fieldset { border:1px solid #46464d; margin:0; padding:8px; min-width:0; }
legend { font-size:11px; padding:0 3px; }
.graph-checkbox { display:flex; flex-direction:row; gap:6px; align-items:center; font-size:11px; margin:4px 0; }
.graph-checkbox input { width:14px; height:14px; accent-color:#81bca2; }
.graph-incoming { display:flex; align-items:center; gap:2px; padding:5px 0; border-bottom:1px solid #404047; font-size:10px; }
.graph-incoming span { flex:1; min-width:0; overflow-wrap:anywhere; }
.graph-node-profile { display:flex; align-items:flex-start; gap:8px; padding:8px 0; border-top:1px solid #46464d; border-bottom:1px solid #46464d; }
.graph-node-profile > div { flex:1; min-width:0; display:grid; gap:5px; }
.graph-node-profile strong { font-size:12px; font-weight:500; }
.graph-node-profile span,.graph-node-profile code { font-size:11px; color:#aab9b3; overflow-wrap:anywhere; }
.graph-warning { color:#e4a0a0; padding:7px 10px; font-size:11px; overflow-wrap:anywhere; }
.graph-connection-message { position:absolute; bottom:8px; left:8px; z-index:10; max-width:calc(100% - 310px); pointer-events:none; padding:6px 10px; font-size:11px; color:#dbbf91; background:#303034; overflow-wrap:anywhere; }
.graph-inspector-content > details { font-size:11px; min-width:0; }
.graph-inspector-content summary { cursor:pointer; color:#b4bdb8; padding:5px 0; }
.graph-runtime-settings[open] { display:grid; gap:8px; }
.graph-muted { color:#9c9ca7; font-size:12px; }
.graph-diagnostic { display:flex; flex-direction:column; align-items:flex-start; gap:5px; color:#e4a0a0; text-align:left; font-size:11px; }
.graph-diagnostic span { overflow-wrap:anywhere; }
.graph-close-run { display:flex; gap:5px; align-self:flex-start; font-size:11px; }
</style>
