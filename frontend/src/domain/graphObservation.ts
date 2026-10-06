import type { GraphSession } from "./workflowGraph";

export function newerGraphObservation(current: GraphSession | undefined, incoming: GraphSession) {
  if (!current) return incoming;
  if (current.workflow_session_id !== incoming.workflow_session_id) return current;
  if (current.revision > incoming.revision || current.data_revision > incoming.data_revision
    || current.head_revision > incoming.head_revision) return current;
  if (current.revision === incoming.revision && (current.chains.some(chain => {
    const next = incoming.chains.find(row => row.chain_run_id === chain.chain_run_id);
    return next && next.revision < chain.revision;
  }) || current.nodes.some(node => {
    const next = incoming.nodes.find(row => row.node_binding_id === node.node_binding_id);
    return next && next.run_id === node.run_id && node.revision !== null
      && next.revision !== null && next.revision < node.revision;
  }))) return current;
  return incoming;
}
