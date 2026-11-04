import { describe, expect, it } from "vitest";
import { isWorkflowPreview, normalizeWorkflowPreview } from "./workflowPreview";
import { workflowPreviewFixture } from "../testUtils/workflowPreviewFixture";

/** Flat backend payloads embed `code` (not `reason_code`) and omit undecidable fields. */
function backendPreview(overrides: Record<string, unknown> = {}) {
  return {
    contract_version: 2, read_only: true, reserves_save_right: false,
    structure: { ok: true, diagnostics: [] },
    declared_references: { ok: true, diagnostics: [] },
    compilation: { ok: true, skipped: false, target_node_ids: ["00000000-0000-4000-8000-000000000010"],
      ordered_node_count: 1, diagnostics: [] },
    baseline: { configuration_exists: true, edit_sequence_matches: true, run_locked: false,
      occupying_sessions: [], has_effective_session: false },
    changes: { categories: ["fixed"], would_outcome: "saved", would_require_confirmation: false,
      would_derive_after_confirmation: false, diagnostics: [] },
    run_conditions: { package_lock_available: true,
      checked: ["structure", "declared_references", "compilation", "package_lock_membership", "baseline_counters"],
      not_checked: ["G21 run lock re-check at submit"] },
    notes: ["preview writes nothing"],
    ...overrides,
  };
}

describe("versioned save preview contract", () => {
  it("accepts the version-2 read-only preview without requiring contract version 1", () => {
    const preview = workflowPreviewFixture();
    expect(isWorkflowPreview(preview)).toBe(true);
    expect(isWorkflowPreview({ ...preview, read_only: false })).toBe(false);
    expect(isWorkflowPreview({ ...preview, reserves_save_right: true })).toBe(false);
    expect(isWorkflowPreview({ ...preview, contract_version: 1 })).toBe(false);
    expect(isWorkflowPreview({ ...preview, contract_version: 3 })).toBe(true);
  });
  it("validates structure/declaration diagnostics, baseline occupants and run condition lists", () => {
    const preview = workflowPreviewFixture();
    expect(isWorkflowPreview({ ...preview, structure: { ok: false,
      diagnostics: [{ reason_code: "graph_missing_edge_node", message: "missing" }] } })).toBe(true);
    expect(isWorkflowPreview({ ...preview, structure: { ok: true } })).toBe(false);
    expect(isWorkflowPreview({ ...preview, baseline: { ...preview.baseline,
      occupying_sessions: [{ workflow_session_id: "not-a-uuid", active_chain_run_id: null }] } })).toBe(false);
    expect(isWorkflowPreview({ ...preview, run_conditions: { ...preview.run_conditions,
      checked: ["ok", 3] } })).toBe(false);
    expect(isWorkflowPreview({ ...preview, changes: { ...preview.changes, would_derive_after_confirmation: "no" } }))
      .toBe(false);
  });
  it("normalizes a real structure failure: code-only diagnostics and undecidable changes", () => {
    const preview = normalizeWorkflowPreview(backendPreview({
      structure: { ok: false, diagnostics: [{ code: "graph_missing_edge_node",
        message: "数据边目标节点不存在", phase: "save", path: "/edges/0/target_node_id",
        node_id: "00000000-0000-4000-8000-000000000010", expected: "existing-node", actual: null }] },
      compilation: { ok: false, skipped: true, diagnostics: [] },
      changes: { categories: null, would_outcome: null, diagnostics: [] },
    }));
    expect(preview).not.toBeNull();
    expect(preview!.structure.ok).toBe(false);
    expect(preview!.structure.diagnostics[0]).toMatchObject({
      reason_code: "graph_missing_edge_node", code: "graph_missing_edge_node", phase: "save",
      path: "/edges/0/target_node_id", expected: "existing-node", actual: null });
    expect(preview!.compilation).toMatchObject({ skipped: true, target_node_ids: [], ordered_node_count: 0 });
    expect(preview!.changes).toMatchObject({ categories: null, would_outcome: null,
      would_require_confirmation: false, would_derive_after_confirmation: false });
  });
  it("normalizes a compile failure that keeps save possible and omits compile extras", () => {
    const preview = normalizeWorkflowPreview(backendPreview({
      compilation: { ok: false, skipped: false,
        diagnostics: [{ code: "graph_no_outputs", message: "no outputs", phase: "compile", path: "/nodes" }] },
      changes: { categories: ["display"], would_outcome: "saved",
        would_require_confirmation: false, would_derive_after_confirmation: false },
      baseline: { configuration_exists: true, edit_sequence_matches: true, run_locked: false,
        occupying_sessions: [], has_effective_session: true },
    }));
    expect(preview).not.toBeNull();
    expect(preview!.structure.ok).toBe(true);
    expect(preview!.compilation.ok).toBe(false);
    expect(preview!.compilation.diagnostics![0]).toMatchObject({ reason_code: "graph_no_outputs", phase: "compile" });
    expect(preview!.changes.would_outcome).toBe("saved");
  });
  it("keeps strict identity/occupancy validation and bounded diagnostic values", () => {
    expect(normalizeWorkflowPreview(backendPreview({ read_only: false }))).toBeNull();
    expect(normalizeWorkflowPreview(backendPreview({ structure: { ok: true } }))).toBeNull();
    expect(normalizeWorkflowPreview(backendPreview({ changes: { categories: "fixed", would_outcome: "saved" } })))
      .toBeNull();
    expect(normalizeWorkflowPreview(workflowPreviewFixture({
      structure: { ok: false, diagnostics: [{ reason_code: "x", message: "m", expected: "y".repeat(4000) }] } })))
      .toMatchObject({ structure: { diagnostics: [{ reason_code: "x", message: "m" }] } });
  });
});
