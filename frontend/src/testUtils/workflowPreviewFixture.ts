import type { WorkflowPreview } from "../domain/workflowPreview";

export function workflowPreviewFixture(overrides: Partial<WorkflowPreview> = {}): WorkflowPreview {
  const base: WorkflowPreview = {
    contract_version: 2, read_only: true, reserves_save_right: false,
    structure: { ok: true, diagnostics: [] },
    declared_references: { ok: true, diagnostics: [] },
    compilation: { ok: true, skipped: false, target_node_ids: [], ordered_node_count: 2, diagnostics: [] },
    baseline: { configuration_exists: true, edit_sequence_matches: true, run_locked: false,
      occupying_sessions: [], has_effective_session: false },
    changes: { categories: ["fixed"], would_outcome: "saved", would_require_confirmation: false,
      would_derive_after_confirmation: false, diagnostics: [] },
    run_conditions: { package_lock_available: true,
      checked: ["package_lock", "definition_revision"], not_checked: ["resources"] },
    notes: [],
  };
  return { ...base, ...overrides };
}
