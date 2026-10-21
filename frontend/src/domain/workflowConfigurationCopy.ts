import { graphJsonSignature, graphObject, graphUuid } from "./workflowGraph";
import { isGraphConfigurationDocument, type GraphConfigurationDocument } from "./workflowConfiguration";

/** Narrow formal-configuration copy contract.
 *
 * Only the source identity and the two formal source versions are sent; the
 * request never carries an editor document, selected session or run history.
 */
export interface WorkflowConfigurationCopyRequest {
  source_workflow_definition_id: string;
  expected_source_definition_revision: number;
  expected_source_edit_sequence: number;
  target_workflow_definition_id: string;
  target_name: string;
  idempotency_key: string;
}

export interface WorkflowConfigurationCopyResult {
  schema_version: 1;
  kind: "workflow.configuration-copy-result";
  outcome: "copied";
  source_workflow_definition_id: string;
  source_definition_revision: number;
  source_edit_sequence: number;
  workflow_definition_id: string;
  definition_revision: number;
  edit_sequence: number;
  session_id: null;
  node_id_map: Record<string, string>;
  document: GraphConfigurationDocument;
}

/** The application receipt's accepted projection for a copy result. */
export interface WorkflowConfigurationCopyAccepted {
  outcome: "copied";
  source_workflow_definition_id: string;
  source_definition_revision: number;
  source_edit_sequence: number;
  workflow_definition_id: string;
  definition_revision: number;
  edit_sequence: number;
  session_id: null;
}

const copyRequestFields = ["source_workflow_definition_id", "expected_source_definition_revision",
  "expected_source_edit_sequence", "target_workflow_definition_id", "target_name",
  "idempotency_key"] as const;
const copyResultFields = ["schema_version", "kind", "outcome", "source_workflow_definition_id",
  "source_definition_revision", "source_edit_sequence", "workflow_definition_id",
  "definition_revision", "edit_sequence", "session_id", "node_id_map", "document"] as const;
export const workflowConfigurationCopyOperation = "definition.copy";
export const workflowConfigurationCopyOwnerKind = "workflow-configuration-copy";
/** The longest workflow name the formal workflow domain accepts. */
export const WORKFLOW_NAME_LIMIT = 128;

const canonicalUuid = (value: unknown): value is string => graphUuid(value) && value === value.toLowerCase();
const integer = (value: unknown, minimum: number): boolean =>
  Number.isSafeInteger(value) && Number(value) >= minimum;
const exactKeys = (value: Record<string, unknown>, fields: readonly string[]): boolean =>
  Object.keys(value).length === fields.length && fields.every(field => Object.hasOwn(value, field));
const boundedName = (value: unknown): boolean => typeof value === "string"
  && [...value].length <= WORKFLOW_NAME_LIMIT && value.trim().length > 0 && value === value.trim()
  && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);
const idempotencyKey = (value: unknown): boolean => typeof value === "string"
  && [...value].length >= 1 && [...value].length <= 128 && value.trim().length > 0
  && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);

export function isWorkflowConfigurationCopyRequest(value: unknown): value is WorkflowConfigurationCopyRequest {
  return graphObject(value) && exactKeys(value, copyRequestFields)
    && canonicalUuid(value.source_workflow_definition_id)
    && canonicalUuid(value.target_workflow_definition_id)
    && value.source_workflow_definition_id !== value.target_workflow_definition_id
    && integer(value.expected_source_definition_revision, 1)
    && integer(value.expected_source_edit_sequence, 1)
    && boundedName(value.target_name) && idempotencyKey(value.idempotency_key);
}

export function isWorkflowConfigurationCopyResult(value: unknown,
  request: WorkflowConfigurationCopyRequest): value is WorkflowConfigurationCopyResult {
  if (!isWorkflowConfigurationCopyRequest(request) || !graphObject(value)
    || !exactKeys(value, copyResultFields)
    || value.schema_version !== 1 || value.kind !== "workflow.configuration-copy-result"
    || value.outcome !== "copied"
    || value.source_workflow_definition_id !== request.source_workflow_definition_id
    || value.source_definition_revision !== request.expected_source_definition_revision
    || value.source_edit_sequence !== request.expected_source_edit_sequence
    || value.workflow_definition_id !== request.target_workflow_definition_id
    || value.definition_revision !== 1 || value.edit_sequence !== 1
    || value.session_id !== null || !isGraphConfigurationDocument(value.document)
    || value.document.workflow_definition_id !== request.target_workflow_definition_id
    || value.document.revision !== 1 || value.document.edit_sequence !== 1
    || value.document.name !== request.target_name
    || !graphObject(value.node_id_map)) return false;
  const identities = Object.fromEntries(value.document.nodes.map(node =>
    [node.node_binding_id, node.node_binding_id]));
  return graphJsonSignature(value.node_id_map) === graphJsonSignature(identities);
}

/** Exactly the receipt.accepted projection this result must produce. */
export function workflowConfigurationCopyAccepted(
  result: WorkflowConfigurationCopyResult): WorkflowConfigurationCopyAccepted {
  return {
    outcome: result.outcome,
    source_workflow_definition_id: result.source_workflow_definition_id,
    source_definition_revision: result.source_definition_revision,
    source_edit_sequence: result.source_edit_sequence,
    workflow_definition_id: result.workflow_definition_id,
    definition_revision: result.definition_revision,
    edit_sequence: result.edit_sequence,
    session_id: result.session_id,
  };
}

/** Deterministic copy name: source display name plus "（副本）", capped at 128 code points. */
export function workflowConfigurationCopyTargetName(sourceName: string): string {
  const suffix = "（副本）";
  const source = sourceName.trim() || "工作流";
  const room = WORKFLOW_NAME_LIMIT - [...suffix].length;
  return [...source].slice(0, room).join("") + suffix;
}
