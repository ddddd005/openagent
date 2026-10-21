import { graphObject, graphUuid } from "./workflowGraph";

/** Read-only directory projections returned by the workflow's summary queries.
 *
 * These are deliberately not GraphDocument / GraphSession. A summary row never
 * carries the configuration body, node/chain detail, outputs or object data;
 * consumers that need those must read the detailed entry/read APIs.
 */

export const workflowSessionStatuses = [
  "idle", "prepared", "running", "pausing", "paused", "budget_exhausted",
  "archive_failed", "failed", "succeeded", "closed", "recovery_unavailable",
] as const;

export interface WorkflowDefinitionSummary {
  workflow_definition_id: string;
  display_name: string;
  fixed_definition_revision: number;
  /** null exactly when the formal record has no current configuration. */
  edit_sequence: number | null;
  archived: boolean;
  node_count: number;
  session_count: number;
  has_current_configuration: boolean;
}

export interface WorkflowSessionSummary {
  workflow_session_id: string;
  workflow_definition_id: string;
  definition_revision: number;
  revision: number;
  name: string;
  metadata_sequence: number;
  archived: boolean;
  active_chain_run_id: string | null;
  status: string;
}

const canonicalUuid = (value: unknown): value is string => graphUuid(value) && value === value.toLowerCase();
const integer = (value: unknown, minimum: number): boolean =>
  Number.isSafeInteger(value) && Number(value) >= minimum;
const exactKeys = (value: Record<string, unknown>, fields: readonly string[]): boolean =>
  Object.keys(value).length === fields.length && fields.every(field => Object.hasOwn(value, field));
const boundedText = (value: unknown, maximum: number): boolean => typeof value === "string"
  && [...value].length <= maximum
  && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);
// The workflow name rule: bounded and non-blank after trimming.
const displayName = (value: unknown): boolean => boundedText(value, 128)
  && (value as string).trim().length > 0;
// The session metadata rule: bounded text without control characters; empty is the
// stored default when no formal name was written.
const sessionName = (value: unknown): boolean => boundedText(value, 128)
  && !/[\u0000-\u001f\u007f]/.test(value as string);

const definitionSummaryFields = ["workflow_definition_id", "display_name", "fixed_definition_revision",
  "edit_sequence", "archived", "node_count", "session_count", "has_current_configuration"] as const;
const sessionSummaryFields = ["workflow_session_id", "workflow_definition_id", "definition_revision",
  "revision", "name", "metadata_sequence", "archived", "active_chain_run_id", "status"] as const;

export function isWorkflowDefinitionSummary(value: unknown): value is WorkflowDefinitionSummary {
  if (!graphObject(value) || !exactKeys(value, definitionSummaryFields)
    || !canonicalUuid(value.workflow_definition_id)
    || !displayName(value.display_name)
    || !integer(value.fixed_definition_revision, 1)
    || !integer(value.node_count, 0) || !integer(value.session_count, 0)
    || typeof value.archived !== "boolean" || typeof value.has_current_configuration !== "boolean") return false;
  if (value.has_current_configuration !== (value.edit_sequence !== null)) return false;
  return value.edit_sequence === null || integer(value.edit_sequence, 1);
}

export function isWorkflowSessionSummary(value: unknown): value is WorkflowSessionSummary {
  return graphObject(value) && exactKeys(value, sessionSummaryFields)
    && canonicalUuid(value.workflow_session_id) && canonicalUuid(value.workflow_definition_id)
    && integer(value.definition_revision, 1) && integer(value.revision, 1)
    && sessionName(value.name) && integer(value.metadata_sequence, 0)
    && typeof value.archived === "boolean"
    && (value.active_chain_run_id === null || canonicalUuid(value.active_chain_run_id))
    && (workflowSessionStatuses as readonly string[]).includes(String(value.status));
}

/** Project a freshly accepted full session view into its directory row.
 *
 * Only used after the workflow itself returned the accepted formal view (for
 * example a create/archive receipt). It never substitutes for a summary read:
 * the next directory refresh replaces these rows with the formal summaries.
 */
export function sessionSummaryFromView<T extends {
  workflow_session_id: string; workflow_definition_id: string; definition_revision: number;
  revision: number; name?: string; metadata_sequence?: number; archived?: boolean;
  active_chain_run_id?: string | null; status: string;
}>(view: T): WorkflowSessionSummary {
  return {
    workflow_session_id: view.workflow_session_id,
    workflow_definition_id: view.workflow_definition_id,
    definition_revision: view.definition_revision,
    revision: view.revision,
    name: view.name ?? "",
    metadata_sequence: view.metadata_sequence ?? 0,
    archived: view.archived === true,
    active_chain_run_id: view.active_chain_run_id ?? null,
    status: view.status,
  };
}
