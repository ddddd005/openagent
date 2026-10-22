import type { GraphDataType, GraphObjectBinding } from "../domain/workflowGraph";
export const preloadVariableType: GraphDataType = { type_id: "workflow.variable", schema_version: 1,
  scope: "session", schema: { type: "object" }, checkpoint_admission: true, has_validator: true,
  has_references: false, has_reference_mapper: false, has_migrator: false };
export const preloadPlainType: GraphDataType = { ...preloadVariableType, type_id: "sample.structure",
  has_validator: false, has_default: false, schema: { type: "object", required: ["label"], properties: { label: { type: "string" } } } };
export const preloadBinding: GraphObjectBinding = { object_key: "shared/seed", type_id: "sample.structure",
  schema_version: 1, scope: "shared", owner_node_id: null, readers: [], writers: [] };
export const preloadVariableValue = { registered: true, name: "score", value_type: "integer", assigned: true, value: 7 };
