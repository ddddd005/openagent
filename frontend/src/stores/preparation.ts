import { ref } from "vue";
import { defineStore } from "pinia";
import {
  assemblePreparationPreview,
  clonePreparation,
  createPreparationNode,
  defaultRolePlacement,
  freezeContextNodeConfig,
  newPreparationId,
  preparationPorts,
  type ContextNodeConfig,
  type ContextReference,
  type PreparationDraft,
  type PreparationEdge,
  type PreparationNode,
  type PreparationNodeKind,
  type PreparationStage,
  type RolePlacement,
} from "../domain/preparation";
import { createPreparationDraft } from "../fixtures/preparation";
import { AGENT_BINDINGS } from "../adapters/workbenchApi";
import type { WorkflowEditGuard } from "../domain/workflowIdentity";

export interface PreparationViewToken {
  workflowId: string;
  stage: PreparationStage;
  version: number;
}
interface PreparationHistory {
  past: PreparationDraft[];
  future: PreparationDraft[];
}
const HISTORY_LIMIT = 60;
const scopeKey = (workflowId: string, stage: PreparationStage) =>
  JSON.stringify([workflowId, stage]);

export const usePreparationStore = defineStore("preparation", () => {
  const drafts = ref<Record<string, PreparationDraft>>({});
  const histories = ref<Record<string, PreparationHistory>>({});
  const versions = ref<Record<string, number>>({});
  const baselines = ref<Record<string, string>>({});
  const revisionHeads = new Map<string, number>();
  let editGuard: WorkflowEditGuard | null = null;
  function setEditGuard(guard: WorkflowEditGuard) { editGuard = guard; }
  const kernelId = (stage: PreparationStage) => `frontend:main-test:agent-${stage.toLowerCase()}`;
  function isUnified(workflowId: string) {
    return getDraft(workflowId, "A").nodes.some(n => n.kind === "assemble" && !!n.config.targetStage);
  }
  function enableUnified(workflowId: string) {
    if (workflowId === "frontend:empty-test" || isUnified(workflowId)) return;
    const a = clonePreparation(getDraft(workflowId, "A"));
    const b = clonePreparation(getDraft(workflowId, "B"));
    for (const [stage, graph] of [["A", a], ["B", b]] as const) {
      for (const node of graph.nodes) {
        node.position.x -= 1100;
        if (stage === "B") node.position.y += 1000;
        if (node.kind === "context" && node.config.sourceBindingId === undefined)
          node.config.sourceBindingId = AGENT_BINDINGS[stage];
        if (node.kind === "assemble") {
          node.config.targetStage = stage;
          graph.edges.push({ id: newPreparationId(), source: node.id, target: kernelId(stage),
            sourceHandle: "assembled", targetHandle: "prompt-in" });
        }
      }
    }
    a.nodes.push(...b.nodes);
    a.edges.push(...b.edges);
    drafts.value[scopeKey(workflowId, "A")] = a;
    baselines.value[scopeKey(workflowId, "A")] = configurationSignature(a);
  }
  function getExecutionDraft(workflowId: string, stage: PreparationStage): PreparationDraft {
    if (!isUnified(workflowId)) return getDraft(workflowId, stage);
    const all = getDraft(workflowId, "A");
    const links = all.edges.filter(e => e.target === kernelId(stage) && e.targetHandle === "prompt-in");
    const assembly = links.length === 1 ? all.nodes.find(n => n.id === links[0]!.source && n.kind === "assemble") : undefined;
    if (!assembly) return { ...clonePreparation(all), stage, nodes: [], edges: [] };
    const selected = new Set<string>();
    function visit(id: string) {
      if (selected.has(id)) return;
      selected.add(id);
      all.edges.filter(e => e.target === id).forEach(e => visit(e.source));
    }
    visit(assembly.id);
    all.nodes.filter(n => ["variable-register", "variable-assign", "session-data-write"].includes(n.kind))
      .forEach(n => visit(n.id));
    return { ...clonePreparation(all), stage,
      nodes: clonePreparation(all.nodes.filter(n => selected.has(n.id))),
      edges: clonePreparation(all.edges.filter(e => selected.has(e.source) && selected.has(e.target))),
    };
  }
  function cloneWorkflow(sourceId: string, targetId: string) {
    for (const stage of ["A", "B"] as const) {
      const graph = clonePreparation(getDraft(sourceId, stage));
      graph.workflowId = targetId;
      graph.configId = newPreparationId();
      graph.revision = 1;
      restoreConfiguration([graph]);
    }
  }

  function getDraft(workflowId: string, stage: PreparationStage): PreparationDraft {
    const key = scopeKey(workflowId, stage);
    if (!drafts.value[key]) {
      drafts.value[key] = createPreparationDraft(workflowId, stage);
      histories.value[key] = { past: [], future: [] };
      versions.value[key] = 0;
      baselines.value[key] = configurationSignature(drafts.value[key]);
      recordHeads(drafts.value[key]);
    }
    return drafts.value[key];
  }
  function nextRevision(id: string, current: number) {
    const revision = Math.max(revisionHeads.get(id) ?? 0, current) + 1;
    revisionHeads.set(id, revision);
    return revision;
  }
  function recordHeads(draft: PreparationDraft) {
    const record = (id: string, revision: number) =>
      revisionHeads.set(id, Math.max(revisionHeads.get(id) ?? 0, revision));
    record(draft.configId, draft.revision);
    for (const node of draft.nodes) {
      if (node.kind === "prompt-item") record(node.config.itemId, node.config.revision);
      if (node.kind === "prompt-group") {
        record(node.config.groupId, node.config.revision);
        for (const member of node.config.members) record(member.itemId, member.revision);
      }
      if (node.kind === "tool") {
        record(node.config.descriptionItemId, node.config.revision);
        record(node.config.schemaItemId, node.config.revision);
      }
    }
  }
  function configurationSignature(draft: PreparationDraft): string {
    return JSON.stringify({
      nodes: draft.nodes.map((node) => node.kind === "context"
        ? { ...node, config: { sourceBindingId: node.config.sourceBindingId, reference: null, source: "local-preview", floors: [] } } : node),
      edges: draft.edges,
    });
  }
  function isDirty(workflowId: string, stage: PreparationStage): boolean {
    const key = scopeKey(workflowId, stage);
    const draft = drafts.value[key];
    return !!draft && configurationSignature(draft) !== baselines.value[key];
  }
  function currentViewToken(workflowId: string, stage: PreparationStage): PreparationViewToken {
    getDraft(workflowId, stage);
    return { workflowId, stage, version: versions.value[scopeKey(workflowId, stage)] };
  }
  function isCurrentView(token: PreparationViewToken) {
    return versions.value[scopeKey(token.workflowId, token.stage)] === token.version;
  }
  function invalidateView(workflowId: string, stage: PreparationStage) {
    getDraft(workflowId, stage);
    versions.value[scopeKey(workflowId, stage)] += 1;
  }
  function mutate(
    workflowId: string, stage: PreparationStage,
    change: (draft: PreparationDraft) => boolean,
  ) {
    const current = getDraft(workflowId, stage);
    const draft = clonePreparation(current);
    if (!change(draft) || JSON.stringify(current) === JSON.stringify(draft)) return false;
    if (editGuard && !editGuard(workflowId, id => mutate(id, stage, change))) return false;
    const key = scopeKey(workflowId, stage);
    histories.value[key].past.push(clonePreparation(current));
    histories.value[key].past = histories.value[key].past.slice(-HISTORY_LIMIT);
    histories.value[key].future = [];
    draft.revision = nextRevision(draft.configId, current.revision);
    drafts.value[key] = draft;
    recordHeads(draft);
    versions.value[key] += 1;
    return true;
  }
  function normalizePresentation(value: RolePlacement): RolePlacement {
    return defaultRolePlacement(clonePreparation(value));
  }
  function bumpNode(node: PreparationNode) {
    if (node.kind === "prompt-item")
      node.config.revision = nextRevision(node.config.itemId, node.config.revision);
    else if (node.kind === "prompt-group")
      node.config.revision = nextRevision(node.config.groupId, node.config.revision);
    else if (node.kind === "tool") {
      node.config.revision = nextRevision(node.config.descriptionItemId, node.config.revision);
      revisionHeads.set(node.config.schemaItemId, node.config.revision);
    }
  }
  function updateNodeConfig(
    workflowId: string, stage: PreparationStage, nodeId: string,
    patch: Record<string, unknown>,
  ) {
    return mutate(workflowId, stage, (draft) => {
      const node = draft.nodes.find((candidate) => candidate.id === nodeId);
      if (!node) return false;
      const previous = clonePreparation(node.config);
      if (node.kind === "context") {
        if (typeof patch.sourceBindingId === "string" && Object.values(AGENT_BINDINGS).includes(patch.sourceBindingId as typeof AGENT_BINDINGS.A))
          node.config = { sourceBindingId: patch.sourceBindingId, reference: null, source: "local-preview", floors: [] };
      } else if (node.kind === "global-content") {
        if (patch.resourceId === null || typeof patch.resourceId === "string") node.config.resourceId = patch.resourceId;
      } else if (node.kind === "session-data-read" || node.kind === "session-data-write") {
        if (patch.definition && typeof patch.definition === "object")
          node.config.definition = clonePreparation(patch.definition) as typeof node.config.definition;
        if (node.kind === "session-data-write" && Object.hasOwn(patch, "value")) node.config.value = clonePreparation(patch.value);
      } else if (node.kind === "prompt-item") {
        if (typeof patch.text === "string") node.config.text = patch.text;
        if (patch.presentation && typeof patch.presentation === "object")
          node.config.presentation = normalizePresentation(patch.presentation as RolePlacement);
      } else if (node.kind === "prompt-group") {
        if (typeof patch.enabled === "boolean") node.config.enabled = patch.enabled;
        if (Array.isArray(patch.members)) {
          const old = new Map(node.config.members.map((member) => [member.id, member]));
          const members = clonePreparation(patch.members) as typeof node.config.members;
          for (const member of members) {
            member.presentation = normalizePresentation(member.presentation);
            const before = old.get(member.id);
            if (before && JSON.stringify({ ...member, revision: before.revision }) !== JSON.stringify(before))
              member.revision = nextRevision(member.itemId, before.revision);
          }
          node.config.members = members;
        }
      } else if (node.kind === "tool") {
        for (const part of ["description", "schema"] as const)
          if (patch[part] && typeof patch[part] === "object")
            node.config[part] = normalizePresentation(patch[part] as RolePlacement);
      } else if (node.kind === "root-input") {
        if (typeof patch.text === "string") node.config.text = patch.text;
      } else if (node.kind === "text") {
        if (typeof patch.text === "string") node.config.text = patch.text;
      } else if (node.kind === "text-to-prompt") {
        if (patch.presentation && typeof patch.presentation === "object")
          node.config.presentation = normalizePresentation(patch.presentation as RolePlacement);
      } else if (node.kind === "prompt-to-text") {
        if (typeof patch.separator === "string") node.config.separator = patch.separator;
      } else if (node.kind === "regex") {
        if (patch.mode === "text" || patch.mode === "prompt") node.config.mode = patch.mode;
        if (typeof patch.pattern === "string") node.config.pattern = patch.pattern;
        if (typeof patch.replacement === "string") node.config.replacement = patch.replacement;
        if (patch.replaceMode === "first" || patch.replaceMode === "all") node.config.replaceMode = patch.replaceMode;
        if (Array.isArray(patch.flags))
          node.config.flags = patch.flags.filter((flag): flag is "i" | "m" | "s" | "x" | "a" =>
            ["i", "m", "s", "x", "a"].includes(flag));
      } else if (node.kind === "variable-register") {
        if (typeof patch.name === "string") node.config.name = patch.name;
        if (["string", "integer", "number", "boolean"].includes(patch.valueType as string))
          node.config.valueType = patch.valueType as typeof node.config.valueType;
        if (typeof patch.hasInitialValue === "boolean") node.config.hasInitialValue = patch.hasInitialValue;
        if (patch.initialValue === null || ["string", "number", "boolean"].includes(typeof patch.initialValue))
          node.config.initialValue = patch.initialValue as typeof node.config.initialValue;
      } else if (node.kind === "variable-assign") {
        if (typeof patch.name === "string") node.config.name = patch.name;
        if (["string", "integer", "number", "boolean"].includes(patch.valueType as string))
          node.config.valueType = patch.valueType as typeof node.config.valueType;
        if (["set", "add", "subtract"].includes(patch.operation as string))
          node.config.operation = patch.operation as typeof node.config.operation;
        if (["string", "number", "boolean"].includes(typeof patch.value))
          node.config.value = patch.value as typeof node.config.value;
      } else if (node.kind === "variable-replace") {
        if (patch.mode === "text" || patch.mode === "prompt") node.config.mode = patch.mode;
      } else if ((node.kind === "prompt-collect" || node.kind === "tool-collect") && Array.isArray(patch.inputs)) {
        node.config.inputs = patch.inputs.filter((id): id is string => typeof id === "string");
      }
      if (JSON.stringify(previous) === JSON.stringify(node.config)) return false;
      bumpNode(node);
      return true;
    });
  }
  function updateNodeTitle(
    workflowId: string, stage: PreparationStage, nodeId: string, title: string,
  ) {
    return mutate(workflowId, stage, (draft) => {
      const node = draft.nodes.find((candidate) => candidate.id === nodeId);
      if (!node || node.title === title) return false;
      node.title = title;
      bumpNode(node);
      return true;
    });
  }
  function setNodePublicOutput(workflowId: string, stage: PreparationStage, nodeId: string, outputId: string, enabled: boolean) {
    return mutate(workflowId, stage, draft => {
      const node = draft.nodes.find(n => n.id === nodeId);
      if (!node || !preparationPorts(node).some(p => p.direction === "output" && p.id === outputId)
        || node.kind === "assemble" || (node.kind === "session-data-read" || node.kind === "session-data-write") && !node.config.definition.public)
        return false;
      const outputs = new Set(node.publicOutputs ?? []);
      if (enabled) outputs.add(outputId); else outputs.delete(outputId);
      node.publicOutputs = [...outputs];
      return true;
    });
  }
  function moveNodes(
    workflowId: string, stage: PreparationStage,
    moves: { id: string; position: { x: number; y: number } }[],
    token?: PreparationViewToken,
  ) {
    if (token && (!isCurrentView(token) || token.workflowId !== workflowId || token.stage !== stage))
      return false;
    return mutate(workflowId, stage, (draft) => {
      let changed = false;
      for (const move of moves) {
        const node = draft.nodes.find((candidate) => candidate.id === move.id);
        if (!node || !Number.isFinite(move.position.x) || !Number.isFinite(move.position.y)) continue;
        if (node.position.x !== move.position.x || node.position.y !== move.position.y) {
          node.position = { ...move.position };
          changed = true;
        }
      }
      return changed;
    });
  }
  function addNode(
    workflowId: string, stage: PreparationStage, kind: PreparationNodeKind,
    position = { x: 80, y: 80 },
  ): string | null {
    let id: string | null = null;
    const changed = mutate(workflowId, stage, (draft) => {
      if (draft.nodes.length >= 128 || !isUnified(workflowId) && (kind === "assemble" || kind === "root-input")
        && draft.nodes.some((node) => node.kind === kind)) return false;
      const node = createPreparationNode(kind, position);
      draft.nodes.push(node);
      id = node.id;
      return true;
    });
    return changed ? id : null;
  }
  function removeNode(workflowId: string, stage: PreparationStage, nodeId: string) {
    return mutate(workflowId, stage, (draft) => {
      const node = draft.nodes.find((candidate) => candidate.id === nodeId);
      if (!node || node.kind === "assemble" && !isUnified(workflowId)) return false;
      draft.nodes = draft.nodes.filter((candidate) => candidate.id !== nodeId);
      draft.edges = draft.edges.filter((edge) => edge.source !== nodeId && edge.target !== nodeId);
      for (const collector of draft.nodes)
        if (collector.kind === "prompt-collect" || collector.kind === "tool-collect")
          collector.config.inputs = collector.config.inputs.filter((id) => id !== nodeId);
      return true;
    });
  }
  function connect(
    workflowId: string, stage: PreparationStage,
    connection: Omit<PreparationEdge, "id">,
  ) {
    return mutate(workflowId, stage, (draft) => {
      const source = draft.nodes.find((node) => node.id === connection.source);
      const target = draft.nodes.find((node) => node.id === connection.target);
      if (source?.kind === "assemble" && !target && connection.sourceHandle === "assembled"
        && connection.targetHandle === "prompt-in" && ["A", "B"].some(s => kernelId(s as PreparationStage) === connection.target)) {
        draft.edges = draft.edges.filter(e => e.target !== connection.target || e.targetHandle !== "prompt-in");
        draft.edges.push({ ...connection, id: newPreparationId() });
        return true;
      }
      if (!source || !target || source.id === target.id) return false;
      if (!preparationPorts(source).some((port) => port.direction === "output" && port.id === connection.sourceHandle)
        || !preparationPorts(target).some((port) => port.direction === "input" && port.id === connection.targetHandle)) return false;
      if (draft.edges.some((edge) => edge.source === connection.source && edge.target === connection.target
        && edge.sourceHandle === connection.sourceHandle && edge.targetHandle === connection.targetHandle)) return false;
      // Invalid but existing port pairs are kept visible so diagnostics can locate them.
      draft.edges.push({ ...connection, id: newPreparationId() });
      if ((target.kind === "prompt-collect" || target.kind === "tool-collect")
        && !target.config.inputs.includes(source.id)) target.config.inputs.push(source.id);
      return true;
    });
  }
  function removeEdge(workflowId: string, stage: PreparationStage, edgeId: string) {
    return mutate(workflowId, stage, (draft) => {
      const edge = draft.edges.find((candidate) => candidate.id === edgeId);
      if (!edge) return false;
      draft.edges = draft.edges.filter((candidate) => candidate.id !== edgeId);
      const target = draft.nodes.find((node) => node.id === edge.target);
      if ((target?.kind === "prompt-collect" || target?.kind === "tool-collect")
        && !draft.edges.some((candidate) => candidate.source === edge.source && candidate.target === edge.target))
        target.config.inputs = target.config.inputs.filter((id) => id !== edge.source);
      return true;
    });
  }
  function restore(
    workflowId: string, stage: PreparationStage, snapshot: PreparationDraft,
  ) {
    const current = getDraft(workflowId, stage);
    const restored = clonePreparation(snapshot);
    // Observation caches are never rewound by configuration undo.
    for (const node of restored.nodes)
      if (node.kind === "context") {
        const present = current.nodes.find((candidate) => candidate.id === node.id);
        node.config = present?.kind === "context" && present.config.sourceBindingId === node.config.sourceBindingId
          ? freezeContextNodeConfig(present.config)
          : { sourceBindingId: node.config.sourceBindingId, reference: null, source: "local-preview", floors: [] };
      }
    restored.revision = nextRevision(current.configId, current.revision);
    drafts.value[scopeKey(workflowId, stage)] = restored;
    versions.value[scopeKey(workflowId, stage)] += 1;
    recordHeads(restored);
  }
  function canUndo(workflowId: string, stage: PreparationStage) {
    return (histories.value[scopeKey(workflowId, stage)]?.past.length ?? 0) > 0;
  }
  function canRedo(workflowId: string, stage: PreparationStage) {
    return (histories.value[scopeKey(workflowId, stage)]?.future.length ?? 0) > 0;
  }
  function undo(workflowId: string, stage: PreparationStage) {
    const key = scopeKey(workflowId, stage);
    const snapshot = histories.value[key]?.past.pop();
    if (!snapshot) return false;
    histories.value[key].future.push(clonePreparation(getDraft(workflowId, stage)));
    restore(workflowId, stage, snapshot);
    return true;
  }
  function redo(workflowId: string, stage: PreparationStage) {
    const key = scopeKey(workflowId, stage);
    const snapshot = histories.value[key]?.future.pop();
    if (!snapshot) return false;
    histories.value[key].past.push(clonePreparation(getDraft(workflowId, stage)));
    restore(workflowId, stage, snapshot);
    return true;
  }
  function getRootText(workflowId: string, stage: PreparationStage) {
    const root = getExecutionDraft(workflowId, stage).nodes.find((node) => node.kind === "root-input");
    return root?.kind === "root-input" ? root.config.text : "";
  }
  function setRootText(workflowId: string, stage: PreparationStage, text: string) {
    const root = getExecutionDraft(workflowId, stage).nodes.find((node) => node.kind === "root-input");
    return root ? updateNodeConfig(workflowId, isUnified(workflowId) ? "A" : stage, root.id, { text }) : false;
  }
  function getPreview(workflowId: string, stage: PreparationStage) {
    return assemblePreparationPreview(getExecutionDraft(workflowId, stage));
  }
  function setContextCache(
    workflowId: string, stage: PreparationStage, nodeId: string,
    config: ContextNodeConfig, token: PreparationViewToken,
  ) {
    if (!isCurrentView(token) || token.workflowId !== workflowId || token.stage !== stage
      || config.source !== "backend-read" || !config.reference
      || config.reference.workflowId !== workflowId) return false;
    const node = getDraft(workflowId, stage).nodes.find((candidate) => candidate.id === nodeId);
    if (node?.kind !== "context") return false;
    if (config.reference.nodeBindingId !== (node.config.sourceBindingId ?? AGENT_BINDINGS[stage])) return false;
    node.config = freezeContextNodeConfig({ ...config, sourceBindingId: node.config.sourceBindingId });
    return true;
  }
  function clearContextCache(workflowId: string, stage: PreparationStage) {
    const draft = drafts.value[scopeKey(workflowId, stage)];
    if (!draft) return;
    for (const node of draft.nodes)
      if (node.kind === "context") node.config = { sourceBindingId: node.config.sourceBindingId, reference: null, source: "local-preview", floors: [] };
    invalidateView(workflowId, stage);
  }
  function bindContextReference(
    workflowId: string, stage: PreparationStage, nodeId: string,
    reference: ContextReference | null,
  ) {
    const node = getDraft(workflowId, stage).nodes.find((candidate) => candidate.id === nodeId);
    if (node?.kind !== "context" || reference && (
      reference.workflowId !== workflowId || reference.stage !== stage)) return false;
    invalidateView(workflowId, stage);
    node.config = { reference: clonePreparation(reference), source: "local-preview", floors: [] };
    return true;
  }
  function exportConfiguration() {
    return clonePreparation(Object.values(drafts.value)).map((draft) => ({
      ...draft,
      nodes: draft.nodes.map((node) => node.kind === "context"
        ? { ...node, config: { ...(node.config.sourceBindingId ? { sourceBindingId: node.config.sourceBindingId } : {}),
          reference: null, source: "local-preview" as const, floors: [] } } : node),
    }));
  }
  function markSaved() {
    for (const [key, draft] of Object.entries(drafts.value)) baselines.value[key] = configurationSignature(draft);
  }
  function clearHistory(workflowId: string) {
    for (const stage of ["A", "B"] as const) histories.value[scopeKey(workflowId, stage)] = { past: [], future: [] };
  }
  function removeWorkflow(workflowId: string) {
    for (const stage of ["A", "B"] as const) {
      const key = scopeKey(workflowId, stage);
      delete drafts.value[key];
      delete histories.value[key];
      delete baselines.value[key];
      // Keep the generation tombstone so a late observation cannot match a recreated view.
      versions.value[key] = (versions.value[key] ?? 0) + 1;
    }
  }
  function restoreConfiguration(values: PreparationDraft[]) {
    for (const draft of clonePreparation(values)) {
      const key = scopeKey(draft.workflowId, draft.stage);
      for (const node of draft.nodes)
        if (node.kind === "context") node.config = { sourceBindingId: node.config.sourceBindingId, reference: null, source: "local-preview", floors: [] };
      drafts.value[key] = draft;
      histories.value[key] = { past: [], future: [] };
      versions.value[key] = (versions.value[key] ?? 0) + 1;
      baselines.value[key] = configurationSignature(draft);
      recordHeads(draft);
    }
  }
  return {
    drafts, histories, versions, getDraft, getPreview, getRootText, setRootText,
    isDirty, updateNodeConfig, updateNodeTitle, moveNodes, addNode, removeNode,
    connect, removeEdge, canUndo, canRedo, undo, redo, currentViewToken,
    isCurrentView, invalidateView, setContextCache, clearContextCache, bindContextReference,
    exportConfiguration, restoreConfiguration, markSaved,
    enableUnified, isUnified, getExecutionDraft, cloneWorkflow, setEditGuard,
    clearHistory, removeWorkflow,
    restoreDocument: restore,
    setNodePublicOutput,
  };
});
