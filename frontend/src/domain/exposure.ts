import { AGENT_BINDINGS, type PublicSession } from "../adapters/workbenchApi";

export type ExposureField = "status" | "run_id" | "revision" | "budget" | "delivered_text" | "result_text";
export interface ExposureRegistration {
  schemaVersion: 1;
  id: string;
  workflowId: string;
  stage: "A" | "B";
  nodeBindingId: string;
  publicName: string;
  kind: "state" | "result";
  type: "public_agent_state" | "delivered_workflow_result" | "public_node_result";
  fields: ExposureField[];
}

export interface ExposureObservation {
  registrationId: string;
  workflowId: string;
  workflowSessionId: string;
  nodeBindingId: string;
  runId: string | null;
  schemaVersion: 1;
  availability: "available" | "unavailable";
  reason?: string;
  value: Record<string, unknown>;
}

export function readPublicExposure(
  registration: ExposureRegistration,
  session: PublicSession,
): ExposureObservation {
  const node = (session.observation?.nodes ?? session.nodes).find((row) => row.node_binding_id === registration.nodeBindingId);
  const observation: ExposureObservation = {
    registrationId: registration.id,
    workflowId: registration.workflowId,
    workflowSessionId: session.workflow_session_id,
    nodeBindingId: registration.nodeBindingId,
    runId: node?.run_id ?? null,
    schemaVersion: 1,
    availability: "unavailable",
    reason: "暂无对应的公开运行状态",
    value: {},
  };
  if (registration.nodeBindingId !== AGENT_BINDINGS[registration.stage] || !node) return observation;
  if (registration.kind === "state") {
    const safe = new Set<ExposureField>(["status", "run_id", "revision", "budget"]);
    for (const field of registration.fields) {
      if (!safe.has(field)) continue;
      if (field === "budget" && node.budget) observation.value.budget = {
        max_model_requests: node.budget.max_model_requests,
        max_model_attempts: node.budget.max_model_attempts,
        model_requests: node.budget.model_requests,
        attempts: node.budget.attempts,
      };
      else if (field === "status" || field === "run_id" || field === "revision")
        observation.value[field] = node[field];
    }
    observation.availability = "available";
    delete observation.reason;
    return observation;
  }
  const result = session.observation?.nodes.find((row) => row.node_binding_id === registration.nodeBindingId)?.result;
  if (result) {
    if (result.availability === "available") {
      for (const field of registration.fields)
        if (field === "delivered_text" || field === "result_text") observation.value[field] = result.text;
      observation.availability = "available";
      delete observation.reason;
    } else observation.reason = result.reason_code;
    return observation;
  }
  // Older servers expose only the fixed B -> Output delivered result.
  const chain = session.chains.at(-1);
  const reply = session.messages.find((message) => message.role === "assistant"
    && message.chain_run_id === chain?.chain_run_id);
  const payload = reply?.payload as { text?: unknown } | undefined;
  if (registration.stage !== "B" || node.status !== "succeeded"
    || chain?.status !== "succeeded" || typeof payload?.text !== "string") {
    observation.reason = registration.stage === "A"
      ? "现有公开接口未提供 Agent A 的业务结果"
      : "当前运行尚无已归档并交付的公开结果";
    return observation;
  }
  if (registration.fields.includes("delivered_text")) observation.value.delivered_text = payload.text;
  observation.availability = "available";
  delete observation.reason;
  return observation;
}
