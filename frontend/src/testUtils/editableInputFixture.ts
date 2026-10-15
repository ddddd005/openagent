import type { GraphNodeType } from "../domain/workflowGraph";

export const editableInputType: GraphNodeType = {
  component_id: "tools.editable-input", component_version: "1", display_name: "Editable input", category: "Tools",
  default_config: { text: "" }, config_schema: { type: "object", properties: {
    text: { type: "string", maxLength: 1_000_000 },
  }, required: ["text"], additionalProperties: false },
  inputs: [], outputs: [{ port_id: "output", data_type: "TEXT", data_schema_version: 2,
    required: true, multiple: false }],
  executable: true, is_output: false, execution_class: "working",
  adjustable_configuration: { schema_version: 1, policy: "input", fields: ["text"] },
  default_configuration_policy: { schema_version: 1, prompt_mode: "adjustable" },
};
