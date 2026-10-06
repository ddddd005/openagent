import { computed, ref } from "vue";
import { defineStore } from "pinia";
import type { LocalDraft, NodeKind } from "../domain/draft";
import {
  copyDraft,
  diagnose,
  groupNodes,
  localId,
  ungroupNode,
} from "../domain/graph";
import { createFixtureDraft, fixtureCatalog } from "../fixtures/catalog";
import { readLocalDraft, saveLocalDraft } from "../adapters/localDraft";

export const useEditorStore = defineStore("editor", () => {
  const draft = ref<LocalDraft>(createFixtureDraft());
  const scope = ref("root");
  const selected = ref<string[]>(["local:macro"]);
  const selectedEdge = ref<string | null>(null);
  const saved = ref(false);
  const storageBlocked = ref(false);
  const past = ref<LocalDraft[]>([]);
  const future = ref<LocalDraft[]>([]);
  const notice = ref("");
  const diagnostics = computed(() => diagnose(draft.value));
  const currentNode = computed(() =>
    draft.value.nodes.find((n) => n.id === selected.value[0]),
  );
  const visibleNodes = computed(() =>
    draft.value.nodes.filter((n) => n.scope === scope.value),
  );
  const visibleEdges = computed(() =>
    draft.value.edges.filter((e) => e.scope === scope.value),
  );
  const groupable = computed(
    () =>
      scope.value === "root" && selected.value.length >= 2 &&
      draft.value.nodes
        .filter((n) => selected.value.includes(n.id))
        .every((n) => n.kind !== "group"),
  );

  function change(action: (next: LocalDraft) => void) {
    past.value.push(copyDraft(draft.value));
    past.value = past.value.slice(-60);
    const next = copyDraft(draft.value);
    action(next);
    draft.value = next;
    future.value = [];
    saved.value = false;
  }
  function addNode(kind: NodeKind) {
    const descriptor = fixtureCatalog.find((n) => n.kind === kind);
    if (!descriptor) return;
    const id = localId();
    change((d) => {
      d.nodes.push({
        id,
        kind,
        title: descriptor.title,
        scope: scope.value,
        config: { ...descriptor.defaults },
        inputs: copyPorts(descriptor.inputs),
        outputs: copyPorts(descriptor.outputs),
      });
      const count = d.nodes.filter((n) => n.scope === scope.value).length;
      d.layout[id] = {
        x: 110 + (count % 3) * 270,
        y: 440 + Math.floor(count / 3) * 80,
      };
    });
    selected.value = [id];
  }
  function copyPorts<T>(value: T): T {
    return JSON.parse(JSON.stringify(value));
  }
  function connect(
    source: string,
    sourcePort: string,
    target: string,
    targetPort: string,
  ) {
    if (source === target) {
      notice.value = "首片不支持节点自连。";
      return;
    }
    if (
      draft.value.edges.some(
        (e) => e.target === target && e.targetPort === targetPort,
      )
    ) {
      notice.value = "该输入已有连接，请先移除原连线。";
      return;
    }
    change((d) => {
      d.edges.push({
        id: localId(),
        scope: scope.value,
        source,
        sourcePort,
        target,
        targetPort,
      });
    });
    notice.value = "连线已保留；类型诊断仅为本地提示。";
  }
  function updateConfig(id: string, key: string, value: string) {
    const node = draft.value.nodes.find((n) => n.id === id);
    if (!node || (key === "title" ? node.title : node.config[key]) === value)
      return;
    change((d) => {
      const n = d.nodes.find((n) => n.id === id)!;
      if (key === "title") n.title = value;
      else n.config[key] = value;
    });
  }
  function move(
    positions: { id: string; position: { x: number; y: number } }[],
  ) {
    if (
      !positions.some(
        (n) =>
          draft.value.layout[n.id].x !== n.position.x ||
          draft.value.layout[n.id].y !== n.position.y,
      )
    )
      return;
    change((d) =>
      positions.forEach((n) => {
        d.layout[n.id] = { ...n.position };
      }),
    );
  }
  function group() {
    try {
      const result = groupNodes(draft.value, selected.value);
      const id = result.nodes[result.nodes.length - 1].id;
      change((d) => Object.assign(d, result));
      selected.value = [id];
      notice.value = "本地节点组已建立，内部身份和接口映射已保留。";
    } catch (e) {
      notice.value = (e as Error).message;
    }
  }
  function ungroup() {
    const id = currentNode.value?.id;
    if (!id) return;
    const result = ungroupNode(draft.value, id);
    change((d) => Object.assign(d, result));
    selected.value = [];
    scope.value = "root";
    notice.value = "已解组，连接与内部身份已恢复。";
  }
  function enterGroup(id: string) {
    scope.value = id;
    selected.value = [];
    selectedEdge.value = null;
  }
  function leaveGroup() {
    scope.value = "root";
    selected.value = [];
    selectedEdge.value = null;
  }
  function removeSelection() {
    if (scope.value !== "root") {
      notice.value = "首片组内接口结构受保护；可解组后删除节点。";
      return;
    }
    if (!selected.value.length && !selectedEdge.value) return;
    if (
      draft.value.nodes.some(
        (n) => selected.value.includes(n.id) && n.kind === "group",
      )
    ) {
      notice.value = "请先解组，再删除内部节点。";
      return;
    }
    const ids = [...selected.value],
      edge = selectedEdge.value;
    change((d) => {
      d.nodes = d.nodes.filter((n) => !ids.includes(n.id));
      d.edges = d.edges.filter(
        (e) =>
          e.id !== edge && !ids.includes(e.source) && !ids.includes(e.target),
      );
      ids.forEach((id) => delete d.layout[id]);
    });
    selected.value = [];
    selectedEdge.value = null;
  }
  function repairSelection() {
    if (!draft.value.nodes.some((n) => n.id === scope.value))
      scope.value = "root";
    selected.value = selected.value.filter((id) =>
      draft.value.nodes.some((n) => n.id === id),
    );
    selectedEdge.value = null;
    saved.value = false;
  }
  function undo() {
    if (!past.value.length) return;
    future.value.push(copyDraft(draft.value));
    draft.value = past.value.pop()!;
    repairSelection();
  }
  function redo() {
    if (!future.value.length) return;
    past.value.push(copyDraft(draft.value));
    draft.value = future.value.pop()!;
    repairSelection();
  }
  function load() {
    try {
      const result = readLocalDraft(localStorage);
      storageBlocked.value = !!result.error;
      if (result.draft) {
        draft.value = result.draft;
        selected.value = [];
        saved.value = true;
      }
      if (result.error) notice.value = result.error;
    } catch {
      storageBlocked.value = true;
      notice.value = "浏览器存储不可用，草稿仅保留在当前页面。";
    }
  }
  function save() {
    if (storageBlocked.value) {
      notice.value = "已有无法读取的草稿，未覆盖。请先备份浏览器存储。";
      return;
    }
    try {
      saveLocalDraft(localStorage, draft.value);
      saved.value = true;
      notice.value = "已保存到此浏览器；未提交或发布至后端。";
    } catch {
      notice.value = "保存失败：浏览器存储不可用。草稿仍在当前页面。";
    }
  }
  return {
    draft,
    scope,
    selected,
    selectedEdge,
    saved,
    past,
    future,
    notice,
    diagnostics,
    currentNode,
    visibleNodes,
    visibleEdges,
    groupable,
    addNode,
    connect,
    updateConfig,
    move,
    group,
    ungroup,
    enterGroup,
    leaveGroup,
    removeSelection,
    undo,
    redo,
    load,
    save,
  };
});
