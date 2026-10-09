<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from "vue";
import { MarkerType, VueFlow, type VueFlowStore } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { ArrowDown, ArrowUp, Copy, Crosshair, Database, Redo2, RefreshCw, Settings2, Trash2, Undo2, X } from "lucide-vue-next";
import { useWorkflowGraphStore } from "../stores/workflowGraph";
import { useWorkspaceStore } from "../stores/workspace";
import { useWorkbenchPersistenceStore } from "../stores/workbenchPersistence";
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
import ThinkingSummary from "./ThinkingSummary.vue";
import { modelPresentationJson } from "../domain/modelPresentation";
import { useWorkbenchFrontendHost } from "../composables/useWorkbenchFrontendHost";
import { useCanvasNodeMenu } from "../composables/useCanvasNodeMenu";
import { useBoundFormDraft } from "../composables/useBoundFormDraft";
import FormDraftNotice from "./FormDraftNotice.vue";
const graph = useWorkflowGraphStore();
const workspace = useWorkspaceStore();
const persistence = useWorkbenchPersistenceStore();
const navigationError = ref("");
let navigationGeneration = 0;
async function leaveEditor(change: () => void) {
  const token = ++navigationGeneration, id = workspace.activeWorkflowId;
  if (!await persistence.flushAll()) {
    navigationError.value = "当前编辑尚未写入本机，保留此页面";
    return;
  }
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
const frontendHost = useWorkbenchFrontendHost();
const frontendPanels = computed(() => frontendHost.extensions.value.filter(row => row.declaration.binding.slot === "panel"));
const canvas = ref<HTMLElement | null>(null);
const menu = useCanvasNodeMenu(canvas);
const flow = shallowRef<VueFlowStore | null>(null);
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
const titleStorage = useBoundFormDraft({
  id: () => graph.document && selectedNode.value ? `node:${graph.document.workflow_definition_id}:${selectedNode.value.node_binding_id}:title` : "",
  basis: titleBasis, capture: () => titleDraft.value,
  reset: () => { titleDraft.value = selectedNode.value ? displayTitle(selectedNode.value) : ""; },
  restore: value => {
    if (typeof value !== "string") throw new Error("标题草稿格式不兼容，原文仍保留");
    titleDraft.value = value;
  },
});
async function changeTitle() {
  const node = selectedNode.value, workflowId = graph.document?.workflow_definition_id, title = titleDraft.value;
  if (!node || !workflowId || graph.locked || !await titleStorage.flush()) return;
  if (graph.document?.workflow_definition_id !== workflowId || selectedNode.value?.node_binding_id !== node.node_binding_id) return;
  const result = graph.patchNode(node.node_binding_id, { title });
  if (result === workflowId) titleStorage.adoptBasis({ workflowId, nodeId: node.node_binding_id, title });
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
const menuGroups = computed(() => nodeMenuGroups(families.value, graph.locked || graph.catalogLoading || !!graph.catalogError));
const selectionRequest = shallowRef<NodeSelectionRequest | null>(null);
const selectionContext = computed(() => ({ workflowId: workspace.activeWorkflowId, viewVersion: workspace.viewVersion,
  sessionId: graph.active?.session_id ?? null, document: graph.document, catalog: graph.catalog,
  packageLock: graph.executionPackageLock, locked: graph.locked, catalogLoading: graph.catalogLoading,
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
    inputs: graph.document!.edges.filter(e => e.target_node_id === node.node_binding_id).map(e => e.target_port_id),
    outputs: graph.document!.edges.filter(e => e.source_node_id === node.node_binding_id).map(e => e.source_port_id) },
})) ?? []);
const edges = computed(() => [...(graph.document?.edges.map(edge => ({ id: edge.edge_id, source: edge.source_node_id,
  target: edge.target_node_id, sourceHandle: edge.source_port_id, targetHandle: edge.target_port_id,
  type: "smoothstep", selected: edge.edge_id === graph.selectedEdgeId, markerEnd: MarkerType.ArrowClosed,
  style: { stroke: edge.edge_id === graph.selectedEdgeId ? "#d6e5df" : "#899e95", strokeWidth: 1.5 } })) ?? []),
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
      && ![connection.sourceHandle, connection.targetHandle].some(handle => handle.startsWith("__control_"))) graph.connect({ source_node_id: connection.source,
      source_port_id: connection.sourceHandle, target_node_id: connection.target, target_port_id: connection.targetHandle });
  });
  instance.onNodesInitialized(() => { if (current() && initialFit) { fit(); initialFit = false; } });
}
async function add(familyId: string) {
  if (!menu.menu.value) return;
  const position = flow.value?.screenToFlowCoordinate(menu.menu.value) ?? { x: 80, y: 80 };
  const family = families.value.find(row => row.id === familyId);
  const request = captureNodeSelection(selectionContext.value, familyId, { position });
  menu.close(true);
  if (!family || !request) return;
  if (!await persistence.flushAll()) { navigationError.value = "当前编辑尚未写入本机"; return; }
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
async function confirmSelection(key: string, resetConfirmed: boolean) {
  const request = selectionRequest.value;
  if (!await persistence.flushAll()) { navigationError.value = "当前编辑尚未写入本机"; return; }
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
watch(() => workspace.activeWorkflowId, () => { flow.value = null; initialFit = true; menu.close(); selectionRequest.value = null; inspectorTab.value = "config"; showBindings.value = false; showInformation.value = false; showFrontend.value = false; showEvents.value = false; });
</script>

<template>
  <section class="graph-workbench" tabindex="0" aria-label="工作流节点工作台" @keydown.capture="keyboard">
    <header class="graph-toolbar">
      <span>{{ graph.document?.nodes.length ?? 0 }} 个节点</span>
      <span v-if="graph.session" class="graph-run-status" :title="graph.session.workflow_definition_id">{{ graph.session.status }} · 定义 {{ graph.session.workflow_definition_id.slice(0,8) }} r{{ graph.session.definition_revision }}</span>
      <div class="graph-toolbar-actions">
        <button type="button" :aria-pressed="showEvents" :disabled="!graph.document" @click="panel('events')">局部事件</button>
        <button type="button" :aria-pressed="showFrontend" :disabled="!graph.document || !frontendPanels.length" @click="panel('frontend')">前端展示</button>
        <button type="button" :aria-pressed="showBindings" :disabled="!graph.document" @click="panel('bindings')">会话绑定</button>
        <button type="button" title="统一登记目录" aria-label="统一登记目录" :aria-pressed="showInformation" @click="panel('information')"><Database :size="15" /></button>
        <button type="button" title="撤销" aria-label="撤销" :disabled="!graph.canUndo" @click="leaveEditor(() => graph.undo())"><Undo2 :size="15" /></button>
        <button type="button" title="重做" aria-label="重做" :disabled="!graph.canRedo" @click="leaveEditor(() => graph.undo(true))"><Redo2 :size="15" /></button>
        <button type="button" title="复制选中节点" aria-label="复制选中节点" :disabled="!graph.selectedNodeIds.length || graph.locked" @click="leaveEditor(() => graph.duplicateSelection())"><Copy :size="15" /></button>
        <button type="button" title="定位全部节点" aria-label="定位全部节点" @click="fit"><Crosshair :size="15" /></button>
        <button type="button" title="刷新目录和运行" aria-label="刷新目录和运行" :disabled="graph.catalogLoading" @click="graph.loadCatalog(); graph.refresh()"><RefreshCw :size="15" /></button>
      </div>
    </header>
    <div v-if="graph.catalogError" class="graph-warning" role="status">{{ graph.catalogError }}</div>
    <div v-if="navigationError" class="graph-warning" role="status">{{ navigationError }}</div>
    <div v-for="issue in frontendHost.issues.value" :key="issue" class="graph-warning" role="status">{{ issue }}</div>
    <div class="graph-body">
      <div ref="canvas" class="graph-canvas" tabindex="0" aria-label="工作流节点画布" @contextmenu="menu.contextMenu" @pointermove="menu.pointerMove" @pointerleave="menu.pointerLeave">
        <VueFlow :key="workspace.activeWorkflowId" :id="`graph-${workspace.activeWorkflowId}`" :nodes="nodes" :edges="edges" :min-zoom="0.15" :max-zoom="2" :delete-key-code="null" :disable-keyboard-a11y="true" :nodes-draggable="!graph.locked" :nodes-connectable="!graph.locked" @init="attach">
          <Background :gap="24" :size="1" pattern-color="#4c4c53" />
          <template #node-graph="nodeProps"><GraphCanvasNode v-bind="nodeProps" /></template>
        </VueFlow>
        <CanvasNodeMenu :state="menu.menu.value" :groups="menuGroups" @select="add" @add="menu.open('add', menu.menu.value ?? undefined)" @back="menu.open('context', menu.menu.value ?? undefined)" @dismiss="menu.close()" />
      </div>
      <aside class="graph-inspector" aria-label="节点详情">
        <header><strong>{{ showEvents ? '局部事件' : showFrontend ? '前端展示接线' : showInformation ? '统一登记目录' : showBindings ? '会话对象绑定' : selectedNode ? displayTitle(selectedNode) : '运行结果' }}</strong><button v-if="!showEvents && !showFrontend && !showBindings && !showInformation && (selectedNode || graph.selectedEdgeId)" type="button" aria-label="删除选中" title="删除选中" :disabled="graph.locked" @click="leaveEditor(() => graph.removeSelection())"><Trash2 :size="15" /></button></header>
        <div v-if="showEvents" class="graph-inspector-content"><GraphEvents /></div>
        <div v-else-if="showFrontend" class="graph-inspector-content"><component v-for="panel in frontendPanels"
          :key="panel.declaration.extension_id" :is="panel.component" @connected="showFrontend = false; fit()" />
          <p v-if="!frontendPanels.length" role="status">前端扩展不可用，请刷新目录。</p></div>
        <div v-else-if="showInformation" class="graph-inspector-content"><GraphInformation :session-id="graph.session?.workflow_session_id" /></div>
        <div v-else-if="showBindings && graph.document" class="graph-inspector-content">
          <GraphObjectBindings :document="graph.document" :types="graph.dataTypes" :disabled="graph.locked" @change="graph.setObjectBindings($event)" />
          <GraphSessionData />
        </div>
        <template v-else-if="selectedNode">
          <div class="graph-tabs"><button type="button" :class="{ active: inspectorTab === 'config' }" @click="leaveEditor(() => inspectorTab = 'config')">配置</button><button type="button" :class="{ active: inspectorTab === 'result' }" @click="leaveEditor(() => inspectorTab = 'result')">状态与结果</button><button type="button" :class="{ active: inspectorTab === 'information' }" @click="leaveEditor(() => inspectorTab = 'information')">信息</button></div>
          <div v-if="inspectorTab === 'config'" class="graph-inspector-content">
            <FormDraftNotice v-if="titleStorage.message.value" :message="titleStorage.message.value"
              :conflict="titleStorage.conflict.value" @restore="titleStorage.restoreConflict" @discard="titleStorage.discardConflict" />
            <label>标题<input v-model="titleDraft" :disabled="graph.locked || titleStorage.loading.value || titleStorage.conflict.value" @change="changeTitle" /></label>
            <label>节点 UUID<input :value="selectedNode.node_binding_id" readonly @focus="($event.target as HTMLInputElement).select()" /></label>
            <label class="graph-checkbox"><input type="checkbox" :checked="graph.document?.execution_roots?.includes(selectedNode.node_binding_id)" :disabled="graph.locked" @change="graph.setExecutionRoot(selectedNode.node_binding_id, ($event.target as HTMLInputElement).checked)" />显式执行根</label>
            <fieldset><legend>前序节点（控制依赖）</legend>
              <label v-for="node in graph.document?.nodes.filter(node => node.node_binding_id !== selectedNode?.node_binding_id)" :key="node.node_binding_id" class="graph-checkbox">
                <input type="checkbox" :checked="predecessors.includes(node.node_binding_id)" :disabled="graph.locked" @change="predecessor(node.node_binding_id, $event)" />{{ displayTitle(node) }} · {{ node.node_binding_id.slice(0, 8) }}
              </label><small>前序完成后才执行当前节点；画布虚线表示顺序，循环由运行诊断检查。</small>
            </fieldset>
            <section class="graph-node-profile" aria-label="节点类型">
              <div><strong>{{ selectedType ? nodeTypeLabel(selectedType) : selectedNode.component_id }}</strong>
                <span>{{ !selectedType ? '声明缺失' : !selectedType.executable ? '不可执行' : nodeProfileLabel(selectedType) }}</span>
                <code>{{ selectedNode.component_id }}@{{ selectedNode.component_version }}</code></div>
              <button type="button" aria-label="切换节点配置" title="切换节点配置"
                :disabled="graph.locked || graph.catalogLoading || !!graph.catalogError || !families.length" @click="changeProfile"><Settings2 :size="15" /></button>
            </section>
            <GraphNodeConfiguration v-if="selectedType && graph.document" :node="selectedNode" :definition="selectedType" :document="graph.document" :catalog="graph.catalog" :disabled="graph.locked" @change="graph.patchNode(selectedNode.node_binding_id, { config: $event })"
              @source-change="(edgeId, source) => graph.patchFrontendSource(selectedNode!.node_binding_id, edgeId, source)" />
            <pre v-else>{{ json(selectedNode.config) }}</pre>
            <details v-if="selectedType?.object_accesses?.length"><summary>会话对象访问契约</summary><pre>{{ json(selectedType.object_accesses) }}</pre></details>
            <button v-if="graph.active?.diagnostics?.some(d => d.node_id === selectedNode?.node_binding_id && d.reason_code.includes('state'))" class="graph-reset-state" type="button" :disabled="graph.locked" @click="graph.resetPrivateState(workspace.activeWorkflowId, selectedNode.node_binding_id); graph.saveWorkflow()">重建该节点私有状态并保存</button>
            <fieldset v-if="selectedType"><legend>公开输出</legend><label v-for="port in nodePorts(selectedType, selectedNode, 'outputs')" :key="port.port_id" class="graph-checkbox"><input type="checkbox" :checked="selectedNode.public_outputs?.includes(port.port_id)" :disabled="graph.locked" @change="graph.patchNode(selectedNode.node_binding_id, { public_outputs: ($event.target as HTMLInputElement).checked ? [...(selectedNode.public_outputs ?? []), port.port_id] : (selectedNode.public_outputs ?? []).filter(p => p !== port.port_id) })" />{{ port.port_id }}</label></fieldset>
            <section v-if="incoming.length"><h3>输入顺序</h3><div v-for="edge in incoming" :key="edge.edge_id" class="graph-incoming"><span>{{ edge.target_port_id }} · {{ titleForId(edge.source_node_id) }} / {{ edge.source_port_id }}</span><button type="button" title="上移" aria-label="上移输入" :disabled="graph.locked" @click="graph.reorderEdge(edge.edge_id, -1)"><ArrowUp :size="13" /></button><button type="button" title="下移" aria-label="下移输入" :disabled="graph.locked" @click="graph.reorderEdge(edge.edge_id, 1)"><ArrowDown :size="13" /></button></div></section>
          </div>
          <div v-else-if="inspectorTab === 'information'" class="graph-inspector-content"><GraphInformation :session-id="graph.session?.workflow_session_id" :node-id="selectedNode.node_binding_id" initial-kind="information_binding" /></div>
          <div v-else class="graph-inspector-content"><code>{{ selectedState?.status ?? 'idle' }}</code><dl v-if="selectedState?.budget" class="graph-budget"><div><dt>模型请求</dt><dd>{{ selectedState.budget.model_requests ?? 0 }} / {{ selectedState.budget.max_model_requests }}</dd></div><div><dt>模型尝试</dt><dd>{{ selectedState.budget.attempts ?? 0 }} / {{ selectedState.budget.max_model_attempts }}</dd></div><div><dt>已接受消息</dt><dd>{{ selectedState.budget.accepted_messages ?? 0 }}</dd></div></dl><ThinkingSummary :value="selectedState?.outputs" /><pre>{{ json(selectedState?.outputs ?? {}) }}</pre><pre v-if="selectedState?.diagnostic" class="graph-warning">{{ json(selectedState.diagnostic) }}</pre></div>
        </template>
        <div v-else class="graph-inspector-content">
          <section v-for="output in graph.session?.outputs ?? []" :key="output.output_id"><h3>{{ titleForId(output.node_binding_id) }} / {{ output.port_id }}</h3><pre>{{ json(output.payload) }}</pre></section>
          <p v-if="!graph.session?.outputs.length" class="graph-muted">暂无结果</p>
          <button v-for="(diagnostic, index) in diagnostics" :key="index" class="graph-diagnostic" type="button" @click="locate(diagnostic.node_id)"><strong>{{ diagnostic.reason_code }}</strong><span>{{ diagnostic.message }}</span></button>
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
.graph-workbench { display:flex; flex:1; flex-direction:column; height:100%; min-width:0; min-height:0; outline:none; }
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
.graph-muted { color:#9c9ca7; font-size:12px; }
.graph-diagnostic { display:flex; flex-direction:column; align-items:flex-start; gap:5px; color:#e4a0a0; text-align:left; font-size:11px; }
.graph-diagnostic span { overflow-wrap:anywhere; }
.graph-close-run { display:flex; gap:5px; align-self:flex-start; font-size:11px; }
.graph-budget { display:grid; gap:7px; margin:0; font-size:11px; }
.graph-budget > div { display:flex; justify-content:space-between; gap:8px; }
.graph-budget dt { color:#a9b2b1; }
.graph-budget dd { margin:0; font-variant-numeric:tabular-nums; }
</style>
