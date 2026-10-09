import { graphApplicationQuery } from "./workflowApplicationApi";
import { graphObject, graphUuid, type GraphDocument, type GraphDiagnostic } from "../domain/workflowGraph";
import { WorkbenchApiError } from "./workbenchApi";

export interface GraphInputDeclarations {
  schema_version: 1; kind: "workflow.input-declarations"; workflow_definition_id: string;
  inputs: { name: string; data_type: "TEXT" | "PROMPT"; required: boolean; node_ids: string[]; conflict: boolean }[];
  required_for_run: string[] | null; diagnostics: GraphDiagnostic[];
}
export async function readGraphInputDeclarations(document: GraphDocument): Promise<GraphInputDeclarations> {
  const value = await graphApplicationQuery("definition.inputs", { document });
  const inputs = graphObject(value) && Array.isArray(value.inputs) ? value.inputs : [];
  if (!graphObject(value) || value.schema_version !== 1 || value.kind !== "workflow.input-declarations"
    || value.workflow_definition_id !== document.workflow_definition_id || !Array.isArray(value.inputs)
    || !value.inputs.every(row => graphObject(row) && typeof row.name === "string" && row.name.length > 0
      && row.name.length <= 128 && row.name !== "_workflow_frozen_resources"
      && ["TEXT", "PROMPT"].includes(String(row.data_type)) && typeof row.required === "boolean"
      && typeof row.conflict === "boolean" && Array.isArray(row.node_ids) && row.node_ids.every(id =>
        graphUuid(id) && document.nodes.some(node => node.node_binding_id === id)))
    || new Set(value.inputs.map(row => row.name)).size !== value.inputs.length
    || !(value.required_for_run === null || Array.isArray(value.required_for_run)
      && value.required_for_run.every(name => inputs.some(row => row.name === name && row.required && !row.conflict)))
    || !Array.isArray(value.diagnostics) || !value.diagnostics.every(row => graphObject(row)
      && typeof row.reason_code === "string" && typeof row.message === "string"))
    throw new WorkbenchApiError("unavailable", "运行输入声明无效，保留原输入");
  return value as unknown as GraphInputDeclarations;
}
