import { graphObject, graphUuid, isGraphJsonValue, type GraphDiagnostic, type GraphOccupyingSession } from "./workflowGraph";

/** 保存预演是只读契约（Q06-2）；它不是提交，也不取得保存资格。 */
export interface WorkflowPreviewStructure {
  ok: boolean;
  diagnostics: GraphDiagnostic[];
}
export interface WorkflowPreviewDeclaredReferences {
  ok: boolean;
  diagnostics: GraphDiagnostic[];
}
export interface WorkflowPreviewCompilation {
  ok: boolean;
  skipped: boolean;
  target_node_ids: string[];
  ordered_node_count: number;
  diagnostics?: GraphDiagnostic[];
}
export interface WorkflowPreviewBaseline {
  configuration_exists: boolean;
  edit_sequence_matches: boolean;
  run_locked: boolean;
  occupying_sessions: GraphOccupyingSession[];
  has_effective_session: boolean;
  [key: string]: unknown;
}
/** Backend omits the outcome when structure/references make it undecidable. */
export interface WorkflowPreviewChanges {
  categories: string[] | null;
  would_outcome: string | null;
  would_require_confirmation: boolean;
  would_derive_after_confirmation: boolean;
  diagnostics?: GraphDiagnostic[];
}
export interface WorkflowPreviewRunConditions {
  package_lock_available: boolean;
  checked: string[];
  not_checked: string[];
}
export interface WorkflowPreview {
  contract_version: number;
  read_only: true;
  reserves_save_right: false;
  structure: WorkflowPreviewStructure;
  declared_references: WorkflowPreviewDeclaredReferences;
  compilation: WorkflowPreviewCompilation;
  baseline: WorkflowPreviewBaseline;
  changes: WorkflowPreviewChanges;
  run_conditions: WorkflowPreviewRunConditions;
  notes: string[];
}

const bounded = (value: unknown, maximum = 512): value is string =>
  typeof value === "string" && value.length <= maximum;
const bool = (value: unknown): value is boolean => typeof value === "boolean";
const strings = (value: unknown): value is string[] => Array.isArray(value)
  && value.length <= 256 && value.every(item => bounded(item, 256));
/** Optional booleans: absent means false, a wrong type invalidates the contract. */
function optionalBoolean(value: unknown): boolean | undefined {
  if (value === undefined) return false;
  return bool(value) ? value : undefined;
}
/** Bounded, possibly truncated JSON value summary; never a whole payload. */
function summaryValue(value: unknown): unknown {
  if (value === undefined || !isGraphJsonValue(value)) return undefined;
  try { return JSON.stringify(value).length <= 2000 ? value : undefined; } catch { return undefined; }
}
function occupants(value: unknown): GraphOccupyingSession[] | null {
  if (!Array.isArray(value) || value.length > 256) return null;
  const rows: GraphOccupyingSession[] = [];
  for (const item of value) {
    if (!graphObject(item) || !graphUuid(item.workflow_session_id)) return null;
    const chain = item.active_chain_run_id;
    if (!(chain === null || chain === undefined || graphUuid(chain))) return null;
    rows.push({ workflow_session_id: item.workflow_session_id, active_chain_run_id: chain ?? null });
  }
  return rows;
}
/**
 * The backend diagnostics embed `code` (not `reason_code`) and optional location
 * fields. Normalize one diagnostic; anything outside the bounded location set is
 * dropped instead of being echoed as a whole payload.
 */
function diagnostic(value: unknown): GraphDiagnostic | null {
  if (!graphObject(value) || !bounded(value.message, 2048)) return null;
  const code = value.reason_code ?? value.code;
  if (!bounded(code, 128)) return null;
  const row: GraphDiagnostic = {
    reason_code: code, message: value.message,
    ...(bounded(value.code, 128) ? { code: value.code } : {}),
    ...(bounded(value.phase, 64) ? { phase: value.phase } : {}),
    ...(bounded(value.path, 512) ? { path: value.path } : {}),
    ...(bounded(value.failure_stage, 128) ? { failure_stage: value.failure_stage } : {}),
    ...(bounded(value.node_id, 128) ? { node_id: value.node_id } : {}),
    ...(bounded(value.port_id, 128) ? { port_id: value.port_id } : {}),
    ...(bounded(value.edge_id, 128) ? { edge_id: value.edge_id } : {}),
    ...(bounded(value.object_key, 128) ? { object_key: value.object_key } : {}),
    ...(Array.isArray(value.dependent_outputs)
      ? { dependent_outputs: value.dependent_outputs.filter((item): item is string => typeof item === "string") } : {}),
    ...(bounded(value.cause_code, 128) ? { cause_code: value.cause_code } : {}),
    ...(bounded(value.step, 128) ? { step: value.step } : {}),
    ...(bounded(value.recovery, 128) ? { recovery: value.recovery } : {}),
    ...(graphUuid(value.correlation_id) ? { correlation_id: value.correlation_id } : {}),
  };
  const expected = summaryValue(value.expected), actual = summaryValue(value.actual);
  if (expected !== undefined) row.expected = expected;
  if (actual !== undefined) row.actual = actual;
  const occupying = value.occupying_sessions === undefined ? [] : occupants(value.occupying_sessions);
  if (occupying === null) return null;
  if (occupying.length) row.occupying_sessions = occupying;
  return row;
}
function diagnostics(value: unknown): GraphDiagnostic[] | null {
  if (!Array.isArray(value) || value.length > 256) return null;
  const rows: GraphDiagnostic[] = [];
  for (const item of value) {
    const row = diagnostic(item);
    if (!row) return null;
    rows.push(row);
  }
  return rows;
}
function checkSection(value: unknown): { ok: boolean; diagnostics: GraphDiagnostic[] } | null {
  if (!graphObject(value) || !bool(value.ok)) return null;
  const rows = diagnostics(value.diagnostics);
  return rows ? { ok: value.ok, diagnostics: rows } : null;
}
function compilation(value: unknown): WorkflowPreviewCompilation | null {
  if (!graphObject(value) || !bool(value.ok) || !bool(value.skipped)) return null;
  const targets = value.target_node_ids === undefined ? []
    : strings(value.target_node_ids) ? value.target_node_ids : null;
  const count = value.ordered_node_count === undefined ? 0 : value.ordered_node_count;
  if (!targets || !Number.isSafeInteger(count) || Number(count) < 0) return null;
  const rows = diagnostics(value.diagnostics ?? []);
  if (!rows) return null;
  return { ok: value.ok, skipped: value.skipped, target_node_ids: targets,
    ordered_node_count: Number(count), diagnostics: rows };
}
function baseline(value: unknown): WorkflowPreviewBaseline | null {
  if (!graphObject(value) || !bool(value.configuration_exists) || !bool(value.edit_sequence_matches)
    || !bool(value.run_locked) || !bool(value.has_effective_session)) return null;
  const occupying = occupants(value.occupying_sessions);
  if (!occupying) return null;
  return { configuration_exists: value.configuration_exists,
    edit_sequence_matches: value.edit_sequence_matches, run_locked: value.run_locked,
    occupying_sessions: occupying, has_effective_session: value.has_effective_session };
}
function changes(value: unknown): WorkflowPreviewChanges | null {
  if (!graphObject(value)) return null;
  const categories = value.categories === undefined || value.categories === null ? null
    : strings(value.categories) ? value.categories : undefined;
  const outcome = value.would_outcome === undefined || value.would_outcome === null ? null
    : bounded(value.would_outcome, 64) ? value.would_outcome : undefined;
  const confirmation = optionalBoolean(value.would_require_confirmation);
  const derive = optionalBoolean(value.would_derive_after_confirmation);
  const rows = diagnostics(value.diagnostics ?? []);
  if (categories === undefined || outcome === undefined || confirmation === undefined
    || derive === undefined || !rows) return null;
  return { categories, would_outcome: outcome, would_require_confirmation: confirmation,
    would_derive_after_confirmation: derive, diagnostics: rows };
}
function conditions(value: unknown): WorkflowPreviewRunConditions | null {
  if (!graphObject(value) || !bool(value.package_lock_available)
    || !strings(value.checked) || !strings(value.not_checked)) return null;
  return { package_lock_available: value.package_lock_available,
    checked: value.checked, not_checked: value.not_checked };
}
/**
 * Validate and normalize one `definition.preview` response (contract version ≥ 2).
 * Returns ``null`` when the payload is not a usable preview; never invents fields.
 */
export function normalizeWorkflowPreview(value: unknown): WorkflowPreview | null {
  if (!graphObject(value)
    || !Number.isSafeInteger(value.contract_version) || Number(value.contract_version) < 2
    || value.read_only !== true || value.reserves_save_right !== false) return null;
  const structure = checkSection(value.structure), declared = checkSection(value.declared_references);
  const compiled = compilation(value.compilation), base = baseline(value.baseline);
  const changed = changes(value.changes), run = conditions(value.run_conditions);
  if (!structure || !declared || !compiled || !base || !changed || !run) return null;
  const notes = value.notes === undefined ? []
    : strings(value.notes) ? value.notes : null;
  if (!notes) return null;
  return { contract_version: Number(value.contract_version), read_only: true, reserves_save_right: false,
    structure, declared_references: declared,
    compilation: compiled, baseline: base, changes: changed, run_conditions: run, notes };
}
/** Compatibility predicate for already-normalized previews (fixtures/tests). */
export function isWorkflowPreview(value: unknown): value is WorkflowPreview {
  return normalizeWorkflowPreview(value) !== null;
}
