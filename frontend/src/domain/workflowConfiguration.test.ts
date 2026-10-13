import { describe, expect, it } from "vitest";
import { graphClone } from "./workflowGraph";
import { isGraphConfigurationResult, isGraphConfigurationUpdateRequest,
  type GraphConfigurationResult, type GraphConfigurationUpdateRequest } from "./workflowConfiguration";

function request(sequence = 8): GraphConfigurationUpdateRequest {
  return { workflow_definition_id: crypto.randomUUID(), expected_edit_sequence: sequence,
    idempotency_key: crypto.randomUUID(),
    document: { schema_version: 3, workflow_definition_id: "", revision: sequence ? 3 : 1,
      name: "Saved configuration", object_bindings: [],
      nodes: [{ node_binding_id: crypto.randomUUID(), component_id: "missing.node", component_version: "1",
        title: "", position: { x: 0, y: 0 }, config: { nested: { first: 1, second: null } },
        configuration_policy: { schema_version: 1, prompt_mode: "fixed" } }], edges: [],
      package_lock: [{ package_id: "missing.package", version: "1.0.0" }] } };
}
function fixture(outcome: GraphConfigurationResult["outcome"] = "saved", sequence = 8) {
  const input = request(sequence);
  input.document.workflow_definition_id = input.workflow_definition_id;
  if (outcome === "derived") input.confirm_derivation = true;
  const target = outcome === "derived" ? crypto.randomUUID() : input.workflow_definition_id;
  const revision = outcome === "derived" ? 1 : input.document.revision;
  const nextSequence = outcome === "derived" ? 1
    : outcome === "unchanged" || outcome === "confirmation_required" ? sequence : sequence + 1;
  const result: GraphConfigurationResult = {
    schema_version: 1, kind: "workflow.configuration-result", outcome,
    source_workflow_definition_id: input.workflow_definition_id, workflow_definition_id: target,
    definition_revision: revision, edit_sequence: nextSequence,
    source_has_effective_session: outcome === "derived" || outcome === "confirmation_required",
    changed_categories: outcome === "unchanged" ? []
      : outcome === "derived" || outcome === "confirmation_required" || sequence === 0 ? ["fixed"] : ["display"],
    node_id_map: Object.fromEntries(input.document.nodes.map(node => [node.node_binding_id, node.node_binding_id])),
    session_id: null,
    document: { ...graphClone(input.document), workflow_definition_id: target, revision, edit_sequence: nextSequence },
  };
  return { input, result };
}

describe("formal configuration result", () => {
  it.each(["saved", "unchanged", "derived"] as const)("accepts %s with the captured source request", outcome => {
    const { input, result } = fixture(outcome);
    const before = graphClone({ input, result });
    expect(isGraphConfigurationUpdateRequest(input)).toBe(true);
    expect(isGraphConfigurationResult(result, input)).toBe(true);
    expect({ input, result }).toEqual(before);
    const other = graphClone(input);
    other.workflow_definition_id = other.document.workflow_definition_id = crypto.randomUUID();
    expect(isGraphConfigurationResult(result, other)).toBe(false);
  });
  it("accepts a confirmation ask only against its unconfirmed request", () => {
    const { input, result } = fixture("confirmation_required");
    expect(isGraphConfigurationUpdateRequest(input)).toBe(true);
    expect(isGraphConfigurationResult(result, input)).toBe(true);
    // The ask is same-target, same-counters and must not claim a derivation.
    expect(result.workflow_definition_id).toBe(input.workflow_definition_id);
    expect(result.edit_sequence).toBe(input.expected_edit_sequence);
    // A confirmed request can never receive "confirmation_required".
    input.confirm_derivation = true;
    expect(isGraphConfigurationResult(result, input)).toBe(false);
  });
  it.each([
    (value: GraphConfigurationResult) => { value.workflow_definition_id = crypto.randomUUID();
      value.document.workflow_definition_id = value.workflow_definition_id; },
    (value: GraphConfigurationResult) => { value.source_has_effective_session = false; },
    (value: GraphConfigurationResult) => { value.changed_categories = ["adjustable"]; },
    (value: GraphConfigurationResult) => { value.changed_categories = []; },
    (value: GraphConfigurationResult) => { value.edit_sequence = value.document.edit_sequence = 9; },
    (value: GraphConfigurationResult) => { value.definition_revision = value.document.revision = 4; },
  ])("rejects invalid confirmation semantics %#", change => {
    const { input, result } = fixture("confirmation_required");
    change(result);
    expect(isGraphConfigurationResult(result, input)).toBe(false);
  });
  it.each([
    (value: GraphConfigurationUpdateRequest) => { value.confirm_derivation = false as never; },
    (value: GraphConfigurationUpdateRequest) => { value.confirm_derivation = "true" as never; },
    (value: GraphConfigurationUpdateRequest) => { Object.assign(value, { confirmation: true }); },
  ])("rejects a nonformal confirmation flag %#", change => {
    const { input } = fixture();
    change(input);
    expect(isGraphConfigurationUpdateRequest(input)).toBe(false);
  });
  it("requires a confirmed request for a derived write", () => {
    const { input, result } = fixture("derived");
    expect(isGraphConfigurationResult(result, input)).toBe(true);
    Reflect.deleteProperty(input, "confirm_derivation");
    expect(isGraphConfigurationResult(result, input)).toBe(false);
  });
  it("accepts initial fixed save and adjustable edits without demanding package availability", () => {
    const initial = fixture("saved", 0);
    expect(isGraphConfigurationResult(initial.result, initial.input)).toBe(true);
    const { input, result } = fixture();
    result.source_has_effective_session = true;
    result.changed_categories = ["adjustable"];
    expect(isGraphConfigurationResult(result, input)).toBe(true);
    input.document.edit_sequence = input.expected_edit_sequence;
    expect(isGraphConfigurationResult(result, input)).toBe(true);
  });
  it("requires a fixed revision increment for fixed edits without an effective session", () => {
    const { input, result } = fixture();
    result.changed_categories = ["fixed"];
    expect(isGraphConfigurationResult(result, input)).toBe(false);
    result.definition_revision++;
    result.document.revision++;
    expect(isGraphConfigurationResult(result, input)).toBe(true);
    result.source_has_effective_session = true;
    expect(isGraphConfigurationResult(result, input)).toBe(false);
  });
  it("checks the actual submitted target node set for mixed derivation, including additions/deletions", () => {
    const { input, result } = fixture("derived");
    const added = { ...graphClone(input.document.nodes[0]), node_binding_id: crypto.randomUUID() };
    input.document.nodes = [added];
    result.document.nodes = graphClone(input.document.nodes);
    result.node_id_map = { [added.node_binding_id]: added.node_binding_id };
    result.changed_categories = ["display", "adjustable", "fixed"];
    expect(isGraphConfigurationResult(result, input)).toBe(true);
    result.node_id_map[crypto.randomUUID()] = added.node_binding_id;
    expect(isGraphConfigurationResult(result, input)).toBe(false);
  });
  it.each([
    (value: GraphConfigurationResult) => { Object.assign(value, { has_effective_session: true }); },
    (value: GraphConfigurationResult) => { value.source_workflow_definition_id = crypto.randomUUID(); },
    (value: GraphConfigurationResult) => { value.session_id = crypto.randomUUID() as never; },
    (value: GraphConfigurationResult) => { value.source_has_effective_session = 1 as never; },
    (value: GraphConfigurationResult) => { value.changed_categories = ["fixed", "display"]; },
    (value: GraphConfigurationResult) => { value.changed_categories = ["display", "display"]; },
    (value: GraphConfigurationResult) => { value.changed_categories = ["unknown"] as never; },
    (value: GraphConfigurationResult) => { value.node_id_map = {}; },
    (value: GraphConfigurationResult) => { value.node_id_map[value.document.nodes[0].node_binding_id] = crypto.randomUUID(); },
    (value: GraphConfigurationResult) => { value.document.name = "An unrelated response"; },
    (value: GraphConfigurationResult) => { value.document.nodes[0].config = { replaced: true }; },
    (value: GraphConfigurationResult) => { Reflect.deleteProperty(value.document.nodes[0], "configuration_policy"); },
    (value: GraphConfigurationResult) => { value.document.workflow_definition_id = crypto.randomUUID(); },
    (value: GraphConfigurationResult) => { value.document.edit_sequence++; },
    (value: GraphConfigurationResult) => { value.document.revision++; },
    (value: GraphConfigurationResult) => { value.edit_sequence = Number.MAX_SAFE_INTEGER + 1; },
  ])("rejects untruthful envelope, target, body or mapping %#", change => {
    const { input, result } = fixture();
    change(result);
    expect(isGraphConfigurationResult(result, input)).toBe(false);
  });
  it.each([
    (value: GraphConfigurationResult) => { value.workflow_definition_id = value.source_workflow_definition_id;
      value.document.workflow_definition_id = value.source_workflow_definition_id; },
    (value: GraphConfigurationResult) => { value.source_has_effective_session = false; },
    (value: GraphConfigurationResult) => { value.changed_categories = ["adjustable"]; },
    (value: GraphConfigurationResult) => { value.edit_sequence = value.document.edit_sequence = 9; },
    (value: GraphConfigurationResult) => { value.definition_revision = value.document.revision = 3; },
  ])("rejects invalid derived semantics %#", change => {
    const { input, result } = fixture("derived");
    change(result);
    expect(isGraphConfigurationResult(result, input)).toBe(false);
  });
  it("requires unchanged to preserve both counters and have no changed categories", () => {
    const { input, result } = fixture("unchanged");
    result.edit_sequence = result.document.edit_sequence = 9;
    expect(isGraphConfigurationResult(result, input)).toBe(false);
    result.edit_sequence = result.document.edit_sequence = 8;
    result.changed_categories = ["display"];
    expect(isGraphConfigurationResult(result, input)).toBe(false);
    const initial = fixture("unchanged", 0);
    expect(isGraphConfigurationResult(initial.result, initial.input)).toBe(false);
  });
  it("ignores JSON object key order while preserving array and node order", () => {
    const { input, result } = fixture();
    result.document.nodes[0].config = { nested: { second: null, first: 1 } };
    expect(isGraphConfigurationResult(result, input)).toBe(true);
    input.document.nodes.push({ ...graphClone(input.document.nodes[0]), node_binding_id: crypto.randomUUID() });
    result.document.nodes = graphClone(input.document.nodes);
    result.node_id_map = Object.fromEntries(result.document.nodes.map(node => [node.node_binding_id, node.node_binding_id]));
    expect(isGraphConfigurationResult(result, input)).toBe(true);
    result.document.nodes.reverse();
    expect(isGraphConfigurationResult(result, input)).toBe(false);
  });
  it.each([
    (value: GraphConfigurationUpdateRequest) => { value.document.workflow_definition_id = crypto.randomUUID(); },
    (value: GraphConfigurationUpdateRequest) => { value.document.schema_version = 2 as never; },
    (value: GraphConfigurationUpdateRequest) => { value.document.edit_sequence = 99; },
    (value: GraphConfigurationUpdateRequest) => { value.expected_edit_sequence = -1; },
    (value: GraphConfigurationUpdateRequest) => { value.idempotency_key = ""; },
    (value: GraphConfigurationUpdateRequest) => { value.idempotency_key = "\ud800"; },
    (value: GraphConfigurationUpdateRequest) => { Object.assign(value, { selected_session_id: crypto.randomUUID() }); },
  ])("rejects inconsistent or nonformal request basis %#", change => {
    const { input, result } = fixture();
    change(input);
    expect(isGraphConfigurationUpdateRequest(input)).toBe(false);
    expect(isGraphConfigurationResult(result, input)).toBe(false);
  });
});
