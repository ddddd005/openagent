export type PortType =
  | "Text"
  | "PromptItem"
  | "PromptCollection"
  | "ContextView"
  | "VariableSnapshot";
export type NodeKind =
  | "items"
  | "collect"
  | "macro"
  | "regex"
  | "context"
  | "assemble"
  | "to-text"
  | "group";
export interface Port {
  id: string;
  label: string;
  type: PortType;
  version: string;
}
export interface CatalogNode {
  kind: Exclude<NodeKind, "group">;
  title: string;
  category: string;
  description: string;
  color: string;
  inputs: Port[];
  outputs: Port[];
  defaults: Record<string, string>;
}
export interface DraftNode {
  id: string;
  kind: NodeKind;
  title: string;
  scope: string;
  config: Record<string, string>;
  inputs: Port[];
  outputs: Port[];
}
export interface DraftEdge {
  id: string;
  scope: string;
  source: string;
  sourcePort: string;
  target: string;
  targetPort: string;
}
export interface InterfaceMapping {
  direction: "input" | "output";
  portId: string;
  nodeId: string;
  internalPortId: string;
}
// This document is browser-local editor state, not a backend definition or wire contract.
export interface LocalDraft {
  format: "workflow-ui-local/v1";
  id: string;
  title: string;
  nodes: DraftNode[];
  edges: DraftEdge[];
  groups: Record<string, InterfaceMapping[]>;
  layout: Record<string, { x: number; y: number }>;
}
export interface Diagnostic {
  edgeId: string;
  nodeId: string;
  portId: string;
  expected: string;
  actual: string;
}
