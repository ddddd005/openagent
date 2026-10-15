import type { GraphNodeType } from "../domain/workflowGraph";
import { sessionVariableType } from "../domain/workflowVariableRegistration";

const base = { category: "Variables", config_schema: { type: "object", properties: {} },
  inputs: [], outputs: [], is_output: false, executable: true, input_storage: "references" as const };
const access = { config_field: "object_key", multiple: false, ...sessionVariableType };
export const variableCatalog: GraphNodeType[] = [
  { ...base, component_id: "tools.variable-register", component_version: "2",
    display_name: "Register variable", execution_class: "non_working",
    default_config: { object_key: "", name: "place", value_type: "string", has_initial: false, initial_value: null },
    provided_session_item_types: [sessionVariableType], object_accesses: [{ ...access, access: "read_write" }] },
  { ...base, component_id: "tools.variable-assign", component_version: "1",
    display_name: "Assign variable", execution_class: "working",
    default_config: { object_key: "variable", operation: "set", value: "next" },
    object_accesses: [{ ...access, access: "read_write" }] },
  { ...base, component_id: "tools.variable-to-content", component_version: "1",
    display_name: "Read variable", execution_class: "working", default_config: { object_key: "variable" },
    object_accesses: [{ ...access, access: "read" }] },
  { ...base, component_id: "tools.variable-replace-all", component_version: "1",
    display_name: "Replace variables", execution_class: "working", default_config: { object_keys: ["variable"] },
    object_accesses: [{ ...access, config_field: "object_keys", multiple: true, access: "read" }] },
];
export const unregisteredVariable = {
  registered: false, name: null, value_type: null, assigned: false, value: null,
};
