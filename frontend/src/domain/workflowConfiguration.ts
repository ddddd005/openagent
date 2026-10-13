import { graphJsonSignature, graphObject, graphUuid, isGraphDocument, isGraphJsonValue,
  type GraphConfigurationPolicy, type GraphDocument, type GraphNode, type GraphObjectBinding } from "./workflowGraph";

export interface GraphConfigurationNode extends GraphNode {
  configuration_policy: GraphConfigurationPolicy;
}
export interface GraphConfigurationUpdateDocument extends GraphDocument {
  schema_version: 3;
  nodes: GraphConfigurationNode[];
  object_bindings: GraphObjectBinding[];
}
export interface GraphConfigurationDocument extends GraphConfigurationUpdateDocument {
  edit_sequence: number;
}
export interface GraphConfigurationUpdateRequest {
  workflow_definition_id: string;
  expected_edit_sequence: number;
  document: GraphConfigurationUpdateDocument;
  idempotency_key: string;
  /** Set only when the user agreed to the backend's derivation question. */
  confirm_derivation?: true;
}
export type GraphConfigurationChangedCategory = "display" | "adjustable" | "fixed";
export type GraphConfigurationOutcome = "unchanged" | "saved" | "derived" | "confirmation_required";
export interface GraphConfigurationResult {
  schema_version: 1;
  kind: "workflow.configuration-result";
  outcome: GraphConfigurationOutcome;
  source_workflow_definition_id: string;
  workflow_definition_id: string;
  definition_revision: number;
  edit_sequence: number;
  source_has_effective_session: boolean;
  changed_categories: GraphConfigurationChangedCategory[];
  node_id_map: Record<string, string>;
  session_id: null;
  document: GraphConfigurationDocument;
}

const canonicalUuid = (value: unknown): value is string => graphUuid(value) && value === value.toLowerCase();
const integer = (value: unknown, minimum: number) => Number.isSafeInteger(value) && Number(value) >= minimum;
const exactKeys = (value: Record<string, unknown>, fields: readonly string[]) =>
  Object.keys(value).length === fields.length && fields.every(field => Object.hasOwn(value, field));
const requestFields = ["workflow_definition_id", "expected_edit_sequence", "document", "idempotency_key"] as const;

export function isGraphConfigurationUpdateDocument(value: unknown): value is GraphConfigurationUpdateDocument {
  return isGraphDocument(value) && value.schema_version === 3;
}

export function isGraphConfigurationDocument(value: unknown): value is GraphConfigurationDocument {
  return isGraphConfigurationUpdateDocument(value) && integer(value.edit_sequence, 1);
}

export function isGraphConfigurationUpdateRequest(value: unknown): value is GraphConfigurationUpdateRequest {
  return graphObject(value) && isGraphJsonValue(value)
    && requestFields.every(field => Object.hasOwn(value, field))
    && Object.keys(value).every(key => key === "confirm_derivation"
      || (requestFields as readonly string[]).includes(key))
    && (value.confirm_derivation === undefined || value.confirm_derivation === true)
    && canonicalUuid(value.workflow_definition_id) && integer(value.expected_edit_sequence, 0)
    && typeof value.idempotency_key === "string" && value.idempotency_key.trim().length > 0
    && [...value.idempotency_key].length <= 128
    && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value.idempotency_key)
    && isGraphConfigurationUpdateDocument(value.document)
    && value.document.workflow_definition_id === value.workflow_definition_id
    && (value.document.edit_sequence === undefined || value.document.edit_sequence === value.expected_edit_sequence)
    && (value.expected_edit_sequence !== 0 || value.document.revision === 1);
}

export function isGraphConfigurationResult(value: unknown,
  request: GraphConfigurationUpdateRequest): value is GraphConfigurationResult {
  if (!isGraphConfigurationUpdateRequest(request) || !graphObject(value) || !isGraphJsonValue(value)
    || !exactKeys(value, ["schema_version", "kind", "outcome", "source_workflow_definition_id",
      "workflow_definition_id", "definition_revision", "edit_sequence", "source_has_effective_session",
      "changed_categories", "node_id_map", "session_id", "document"])
    || value.schema_version !== 1 || value.kind !== "workflow.configuration-result"
    || typeof value.outcome !== "string"
    || !["unchanged", "saved", "derived", "confirmation_required"].includes(value.outcome)
    || value.source_workflow_definition_id !== request.workflow_definition_id
    || !canonicalUuid(value.workflow_definition_id) || !integer(value.definition_revision, 1)
    || !integer(value.edit_sequence, 1) || typeof value.source_has_effective_session !== "boolean"
    || value.session_id !== null || !isGraphConfigurationDocument(value.document)
    || value.document.workflow_definition_id !== value.workflow_definition_id
    || value.document.revision !== value.definition_revision || value.document.edit_sequence !== value.edit_sequence
    || !Array.isArray(value.changed_categories) || !graphObject(value.node_id_map)) return false;
  const categories = value.changed_categories;
  if (graphJsonSignature(categories) !== graphJsonSignature(
    ["display", "adjustable", "fixed"].filter(category => categories.includes(category)))) return false;
  const expectedDocument = { ...request.document, workflow_definition_id: value.workflow_definition_id,
    revision: value.definition_revision, edit_sequence: value.edit_sequence };
  if (graphJsonSignature(value.document) !== graphJsonSignature(expectedDocument)) return false;
  const identities = Object.fromEntries(value.document.nodes.map(node => [node.node_binding_id, node.node_binding_id]));
  if (graphJsonSignature(value.node_id_map) !== graphJsonSignature(identities)) return false;

  const expected = request.expected_edit_sequence, fixed = categories.includes("fixed");
  const confirmed = request.confirm_derivation === true;
  if (value.outcome === "unchanged") {
    return categories.length === 0 && value.workflow_definition_id === request.workflow_definition_id
      && expected >= 1 && value.edit_sequence === expected && value.definition_revision === request.document.revision;
  }
  if (value.outcome === "confirmation_required") {
    // The backend answered the question without writing: same target, same
    // counters, and the exact proposed document is returned only as the ask.
    return value.workflow_definition_id === request.workflow_definition_id && value.source_has_effective_session
      && fixed && !confirmed && expected >= 1 && value.edit_sequence === expected
      && value.definition_revision === request.document.revision;
  }
  if (value.outcome === "derived") {
    return value.workflow_definition_id !== request.workflow_definition_id && value.source_has_effective_session
      && fixed && confirmed && expected >= 1 && value.edit_sequence === 1 && value.definition_revision === 1;
  }
  if (value.workflow_definition_id !== request.workflow_definition_id || categories.length === 0
    || value.edit_sequence !== expected + 1) return false;
  if (expected === 0) {
    return categories.length === 1 && fixed && !value.source_has_effective_session && value.definition_revision === 1;
  }
  return !(fixed && value.source_has_effective_session)
    && value.definition_revision === request.document.revision + Number(fixed);
}
