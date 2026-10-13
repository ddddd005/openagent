import { WorkbenchApiError } from "./workbenchApi";
import { readGraphReceipt, sendGraphCommand } from "./workflowApplicationApi";
import { graphClone } from "../domain/workflowGraph";
import { isGraphConfigurationResult, isGraphConfigurationUpdateRequest,
  type GraphConfigurationResult, type GraphConfigurationUpdateRequest } from "../domain/workflowConfiguration";

const path = "/api/graph/definitions/update";
async function submit(request: GraphConfigurationUpdateRequest, readOnly: boolean): Promise<GraphConfigurationResult> {
  if (!isGraphConfigurationUpdateRequest(request))
    throw new WorkbenchApiError("rejected", "配置提交依据或文档无效", undefined, "graph_invalid_document");
  const captured = graphClone(request);
  const result = await (readOnly ? readGraphReceipt : sendGraphCommand)(path, { ...graphClone(captured) });
  if (!isGraphConfigurationResult(result, captured))
    throw new WorkbenchApiError("unknown", "配置回执与原提交依据不匹配，保留原请求", undefined,
      "graph_configuration_receipt_invalid");
  return graphClone(result);
}
export const updateGraphConfiguration = (request: GraphConfigurationUpdateRequest) => submit(request, false);
export const readGraphConfigurationReceipt = (request: GraphConfigurationUpdateRequest) => submit(request, true);
