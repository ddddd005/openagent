import type { Diagnostic, DraftNode, LocalDraft, Port } from "./draft";

export const copyDraft = (draft: LocalDraft): LocalDraft =>
  JSON.parse(JSON.stringify(draft));
export const localId = () => `local:${crypto.randomUUID()}`;

export function diagnose(draft: LocalDraft): Diagnostic[] {
  return draft.edges.flatMap((edge) => {
    const source = draft.nodes
      .find((n) => n.id === edge.source)
      ?.outputs.find((p) => p.id === edge.sourcePort);
    const target = draft.nodes
      .find((n) => n.id === edge.target)
      ?.inputs.find((p) => p.id === edge.targetPort);
    if (
      source &&
      target &&
      source.type === target.type &&
      source.version === target.version
    )
      return [];
    return [
      {
        edgeId: edge.id,
        nodeId: edge.target,
        portId: edge.targetPort,
        expected: target
          ? `${target.type} @ ${target.version}`
          : "有效输入端口",
        actual: source ? `${source.type} @ ${source.version}` : "缺失输出端口",
      },
    ];
  });
}

export function groupNodes(
  draft: LocalDraft,
  ids: string[],
  id = localId(),
): LocalDraft {
  const next = copyDraft(draft);
  const selected = next.nodes.filter((n) => ids.includes(n.id));
  if (
    selected.length < 2 ||
    new Set(selected.map((n) => n.scope)).size !== 1 ||
    selected.some((n) => n.kind === "group" || n.scope !== "root")
  ) {
    throw new Error("请选择同一层的至少两个普通节点。首片不支持嵌套建组。");
  }
  const scope = selected[0].scope;
  const group: DraftNode = {
    id,
    kind: "group",
    title: "处理节点组",
    scope,
    config: {},
    inputs: [],
    outputs: [],
  };
  next.groups[id] = [];
  const expose = (
    direction: "input" | "output",
    nodeId: string,
    internalPortId: string,
  ) => {
    const existing = next.groups[id].find(
      (m) =>
        m.direction === direction &&
        m.nodeId === nodeId &&
        m.internalPortId === internalPortId,
    );
    if (existing) return existing.portId;
    const node = next.nodes.find((n) => n.id === nodeId)!;
    const original = (direction === "input" ? node.inputs : node.outputs).find(
      (p) => p.id === internalPortId,
    )!;
    const portId = `${id}:port-${next.groups[id].length}`;
    const port: Port = {
      ...original,
      id: portId,
      label: `${node.title} · ${original.label}`,
    };
    (direction === "input" ? group.inputs : group.outputs).push(port);
    next.groups[id].push({ direction, portId, nodeId, internalPortId });
    return portId;
  };
  next.edges.forEach((e) => {
    const from = ids.includes(e.source),
      to = ids.includes(e.target);
    if (from && to) e.scope = id;
    else if (from) {
      e.sourcePort = expose("output", e.source, e.sourcePort);
      e.source = id;
    } else if (to) {
      e.targetPort = expose("input", e.target, e.targetPort);
      e.target = id;
    }
  });
  const positions = selected.map((n) => next.layout[n.id]);
  const origin = {
    x: Math.min(...positions.map((p) => p.x)),
    y: Math.min(...positions.map((p) => p.y)),
  };
  selected.forEach((n) => {
    n.scope = id;
    next.layout[n.id] = {
      x: next.layout[n.id].x - origin.x + 80,
      y: next.layout[n.id].y - origin.y + 80,
    };
  });
  next.nodes.push(group);
  next.layout[id] = origin;
  return next;
}

export function ungroupNode(draft: LocalDraft, id: string): LocalDraft {
  const next = copyDraft(draft);
  const group = next.nodes.find((n) => n.id === id && n.kind === "group");
  if (!group) throw new Error("请选择节点组。");
  const maps = next.groups[id];
  next.edges.forEach((e) => {
    if (e.source === id) {
      const mapping = maps.find(
        (m) => m.direction === "output" && m.portId === e.sourcePort,
      )!;
      e.source = mapping.nodeId;
      e.sourcePort = mapping.internalPortId;
    }
    if (e.target === id) {
      const mapping = maps.find(
        (m) => m.direction === "input" && m.portId === e.targetPort,
      )!;
      e.target = mapping.nodeId;
      e.targetPort = mapping.internalPortId;
    }
    if (e.scope === id) e.scope = group.scope;
  });
  next.nodes
    .filter((n) => n.scope === id)
    .forEach((n) => {
      n.scope = group.scope;
      next.layout[n.id] = {
        x: next.layout[n.id].x + next.layout[id].x - 80,
        y: next.layout[n.id].y + next.layout[id].y - 80,
      };
    });
  next.nodes = next.nodes.filter((n) => n.id !== id);
  delete next.groups[id];
  delete next.layout[id];
  return next;
}

export function isLocalDraft(value: unknown): value is LocalDraft {
  if (!value || typeof value !== "object") return false;
  const d = value as LocalDraft;
  if (
    d.format !== "workflow-ui-local/v1" ||
    typeof d.id !== "string" ||
    typeof d.title !== "string" ||
    !Array.isArray(d.nodes) ||
    !Array.isArray(d.edges) ||
    !d.groups ||
    !d.layout
  )
    return false;
  const ids = new Set<string>();
  const kinds = [
    "items",
    "collect",
    "macro",
    "regex",
    "context",
    "assemble",
    "to-text",
    "group",
  ];
  const validPorts = (ports: unknown): ports is Port[] =>
    Array.isArray(ports) &&
    ports.every(
      (p) =>
        p &&
        typeof p.id === "string" &&
        typeof p.label === "string" &&
        [
          "Text",
          "PromptItem",
          "PromptCollection",
          "ContextView",
          "VariableSnapshot",
        ].includes(p.type) &&
        typeof p.version === "string",
    );
  for (const n of d.nodes) {
    if (
      !n ||
      !n.id?.startsWith("local:") ||
      ids.has(n.id) ||
      !kinds.includes(n.kind) ||
      typeof n.scope !== "string" ||
      typeof n.title !== "string" ||
      !n.config ||
      Object.values(n.config).some((v) => typeof v !== "string") ||
      !validPorts(n.inputs) ||
      !validPorts(n.outputs)
    )
      return false;
    if (
      !d.layout[n.id] ||
      !Number.isFinite(d.layout[n.id].x) ||
      !Number.isFinite(d.layout[n.id].y)
    )
      return false;
    ids.add(n.id);
  }
  const scopes = new Set([
    "root",
    ...d.nodes.filter((n) => n.kind === "group").map((n) => n.id),
  ]);
  if (d.nodes.some((n) => !scopes.has(n.scope) || n.scope === n.id))
    return false;
  if (
    d.edges.some(
      (e) =>
        !e ||
        typeof e.id !== "string" ||
        !scopes.has(e.scope) ||
        !ids.has(e.source) ||
        !ids.has(e.target) ||
        typeof e.sourcePort !== "string" ||
        typeof e.targetPort !== "string",
    )
  )
    return false;
  for (const n of d.nodes.filter((n) => n.kind === "group")) {
    if (n.scope !== "root" || !Array.isArray(d.groups[n.id])) return false;
    for (const m of d.groups[n.id]) {
      const internal = d.nodes.find(
        (child) => child.id === m.nodeId && child.scope === n.id,
      );
      if (!internal || !["input", "output"].includes(m.direction)) return false;
      if (
        !(m.direction === "input" ? n.inputs : n.outputs).some(
          (p) => p.id === m.portId,
        )
      )
        return false;
      if (
        !(m.direction === "input" ? internal.inputs : internal.outputs).some(
          (p) => p.id === m.internalPortId,
        )
      )
        return false;
    }
  }
  return true;
}
