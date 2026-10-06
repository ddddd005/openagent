import { WorkbenchApiError } from "../adapters/workbenchApi";
import { graphApplicationQuery } from "../adapters/workflowApplicationApi";
import { graphClone, graphObject } from "../domain/workflowGraph";
import {
  graphInformationCursor, graphInformationScopes, isGraphInformationBinding, isGraphInformationOwner,
  isGraphInformationReference, isGraphRegistration, sameGraphInformationOwner, sameGraphInformationReference,
  type GraphInformationBinding, type GraphInformationPage, type GraphRegistrationPage, type GraphRegistrationParameters,
} from "../domain/graphInformation";

export async function readGraphRegistrations(parameters: GraphRegistrationParameters = {}): Promise<GraphRegistrationPage> {
  const fixed = graphClone({ ...parameters, limit: parameters.limit ?? 50 });
  const value = await graphApplicationQuery("registration.list", fixed);
  if (!graphObject(value) || !Array.isArray(value.items) || value.items.length > fixed.limit
    || !value.items.every(isGraphRegistration) || !graphInformationCursor(value.next_cursor)
    || !value.items.every(item => (!fixed.kind || item.kind === fixed.kind)
      && (!fixed.package_id || item.package_id === fixed.package_id)
      && (!fixed.channel_id || item.declaration.channel_id === fixed.channel_id)
      && (item.kind !== "information_binding" || (!fixed.chain_id || item.owner?.chain_run_id === fixed.chain_id)
        && (!fixed.node_id || item.owner?.node_binding_id === fixed.node_id))))
    throw new WorkbenchApiError("unavailable", "登记目录格式、分页或确切调用范围不匹配");
  return value as unknown as GraphRegistrationPage;
}

export async function readGraphInformation(sessionId: string, binding: GraphInformationBinding,
  sourceScope: "live" | "history", cursor?: string): Promise<GraphInformationPage> {
  if (!isGraphInformationBinding(binding) || !graphInformationScopes(binding).includes(sourceScope))
    throw new WorkbenchApiError("unavailable", "信息源未登记所请求的读取范围");
  const fixed = graphClone(binding), limit = Math.min(50, fixed.declaration.max_page_size);
  const value = await graphApplicationQuery("information.read", {
    session_id: sessionId, reference: fixed.registration_ref, owner: fixed.owner,
    generation: fixed.generation, source_scope: sourceScope, limit, ...(cursor ? { cursor } : {}),
  });
  if (!graphObject(value) || value.schema_version !== 1
    || !isGraphInformationReference(value.source_ref) || !sameGraphInformationReference(value.source_ref, fixed.registration_ref)
    || !isGraphInformationOwner(value.owner) || !sameGraphInformationOwner(value.owner, fixed.owner)
    || value.generation !== fixed.generation || value.source_scope !== sourceScope
    || value.format_id !== fixed.declaration.format_id || value.format_version !== fixed.declaration.format_version
    || !Array.isArray(value.items) || value.items.length > limit || !graphInformationCursor(value.next_cursor)
    || !["ok", "gap", "reset"].includes(String(value.status)))
    throw new WorkbenchApiError("unavailable", "信息响应的原归属、代次、格式或分页上限不匹配");
  // Canonical byte bounds and item schemas are checked by the provider router, whose JSON profile is server-owned.
  return value as unknown as GraphInformationPage;
}

// Each view owns this fence; unrelated views do not share an implicit "latest node" reader.
export function createWorkflowInformationAccess(currentBasis: () => string) {
  let generation = 0;
  function invalidate() { generation++; }
  async function scoped<T>(read: () => Promise<T>): Promise<T | null> {
    const token = ++generation, basis = currentBasis();
    const current = () => token === generation && basis === currentBasis();
    try { const value = await read(); return current() ? value : null; }
    catch (failure) { if (current()) throw failure; return null; }
  }
  return {
    invalidate,
    registrations: (parameters: GraphRegistrationParameters) => scoped(() => readGraphRegistrations(parameters)),
    information: (sessionId: string, binding: GraphInformationBinding, scope: "live" | "history", cursor?: string) =>
      scoped(() => readGraphInformation(sessionId, binding, scope, cursor)),
  };
}
