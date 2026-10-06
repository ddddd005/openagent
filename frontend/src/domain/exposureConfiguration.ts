import { AGENT_BINDINGS, type PublicSession } from "../adapters/workbenchApi";
import { exactSessionObject, sessionInteger, sessionUuid } from "./sessionWire";
import { MAIN_WORKFLOW_ID } from "../fixtures/workflows";
import { isWorkflowIdentity } from "./workflowIdentity";
import type { ExposureObservation, ExposureRegistration } from "./exposure";

export interface ExposureReference { config_id: string; revision: number }
export interface ExposureConfiguration {
  schema_version: 1;
  kind: "workflow_exposure_configuration";
  config_id: string;
  workflow_id: string;
  revision: number;
  registrations: ExposureRegistration[];
}
export interface ExposureMutation {
  record: ExposureConfiguration;
  expected_revision: number;
  idempotency_key: string;
}
export interface ExposurePublication extends ExposureReference {
  workflowId: string;
  signature: string;
}
export interface ExposureSnapshot {
  schemaVersion: 1;
  publications: ExposurePublication[];
  pending: ExposureMutation | null;
}
export interface ExposureRead {
  schema_version: 1;
  kind: "workflow_exposure_read";
  config_id: string;
  revision: number;
  workflow_session_id: string;
  session_revision: number;
  head_commit_id: string;
  availability: "available" | "unavailable";
  reason_code: string | null;
  registrations: ExposureRegistration[];
  observations: ExposureObservation[];
}
export const exposureSignature = (value: unknown): string => JSON.stringify(value, (_key, child: unknown) =>
  child && typeof child === "object" && !Array.isArray(child)
    ? Object.fromEntries(Object.entries(child).sort(([a], [b]) => a.localeCompare(b))) : child);
export function isExposureReference(value: unknown): value is ExposureReference {
  return exactSessionObject(value, ["config_id", "revision"])
    && sessionUuid(value.config_id) && sessionInteger(value.revision) && value.revision > 0;
}
export function isExposureRegistration(value: unknown): value is ExposureRegistration {
  if (!exactSessionObject(value, ["schemaVersion", "id", "workflowId", "stage", "nodeBindingId",
    "publicName", "kind", "type", "fields"]) || value.schemaVersion !== 1 || !sessionUuid(value.id)
    || !isWorkflowIdentity(value.workflowId) || !["A", "B"].includes(value.stage as string)
    || value.nodeBindingId !== AGENT_BINDINGS[value.stage as "A" | "B"]
    || typeof value.publicName !== "string" || !value.publicName || value.publicName.length > 128
    || value.publicName.trim() !== value.publicName || !Array.isArray(value.fields)
    || !value.fields.length || new Set(value.fields).size !== value.fields.length) return false;
  const allowed = value.kind === "state" && value.type === "public_agent_state"
    ? ["status", "run_id", "revision", "budget"]
    : value.kind === "result" && ["delivered_workflow_result", "public_node_result"].includes(value.type as string)
      ? value.stage === "A" ? ["result_text", "delivered_text"] : ["delivered_text"] : [];
  return value.fields.every((field) => allowed.includes(field));
}
export function isExposureConfiguration(value: unknown): value is ExposureConfiguration {
  return exactSessionObject(value, ["schema_version", "kind", "config_id", "workflow_id", "revision", "registrations"])
    && value.schema_version === 1 && value.kind === "workflow_exposure_configuration"
    && isExposureReference({ config_id: value.config_id, revision: value.revision })
    && isWorkflowIdentity(value.workflow_id) && Array.isArray(value.registrations)
    && value.registrations.length <= 128 && value.registrations.every(isExposureRegistration)
    && new Set(value.registrations.map((row) => row.id)).size === value.registrations.length
    && new Set(value.registrations.map((row) => row.publicName)).size === value.registrations.length
    && new TextEncoder().encode(JSON.stringify(value)).byteLength <= 60 * 1024;
}
export function isExposureMutation(value: unknown): value is ExposureMutation {
  return exactSessionObject(value, ["record", "expected_revision", "idempotency_key"])
    && isExposureConfiguration(value.record) && sessionInteger(value.expected_revision)
    && value.record.revision === value.expected_revision + 1 && sessionUuid(value.idempotency_key);
}
export function isExposureSnapshot(value: unknown): value is ExposureSnapshot {
  return exactSessionObject(value, ["schemaVersion", "publications", "pending"])
    && value.schemaVersion === 1 && (value.pending === null || isExposureMutation(value.pending))
    && Array.isArray(value.publications) && value.publications.length <= 128
    && value.publications.every((row) => exactSessionObject(row, ["workflowId", "config_id", "revision", "signature"])
      && isWorkflowIdentity(row.workflowId) && isExposureReference({ config_id: row.config_id, revision: row.revision })
      && typeof row.signature === "string" && row.signature.length <= 60 * 1024);
}
export function isExposureRead(value: unknown, ref: ExposureReference, view: PublicSession): value is ExposureRead {
  if (!exactSessionObject(value, ["schema_version", "kind", "config_id", "revision", "workflow_session_id",
    "session_revision", "head_commit_id", "availability", "reason_code", "registrations", "observations"])
    || value.schema_version !== 1 || value.kind !== "workflow_exposure_read"
    || value.config_id !== ref.config_id || value.revision !== ref.revision
    || value.workflow_session_id !== view.workflow_session_id || value.session_revision !== view.revision
    || value.head_commit_id !== view.head_commit_id || !Array.isArray(value.registrations)
    || !Array.isArray(value.observations)) return false;
  if (value.availability === "unavailable") return value.reason_code === "declaration_stale"
    && value.registrations.length === 0 && value.observations.length === 0;
  if (value.availability !== "available" || value.reason_code !== null
    || !isExposureConfiguration({ schema_version: 1, kind: "workflow_exposure_configuration",
      config_id: ref.config_id, revision: ref.revision, workflow_id: MAIN_WORKFLOW_ID,
      registrations: value.registrations }) || value.observations.length !== value.registrations.length) return false;
  const registrations = value.registrations as ExposureRegistration[];
  return new Set(value.observations.map((row) => row?.registrationId)).size === registrations.length
    && value.observations.every((row) => {
      const declaration = registrations.find((item) => item.id === row?.registrationId);
      if (!declaration || !exactSessionObject(row, ["registrationId", "workflowId", "workflowSessionId",
        "nodeBindingId", "runId", "schemaVersion", "availability", "value"], ["reason"])
        || row.workflowId !== declaration.workflowId || row.workflowSessionId !== view.workflow_session_id
        || row.nodeBindingId !== declaration.nodeBindingId || row.schemaVersion !== 1
        || !(row.runId === null || sessionUuid(row.runId))
        || !exactSessionObject(row.value, [], declaration.fields)) return false;
      if (row.availability === "unavailable") return typeof row.reason === "string"
        && /^[A-Za-z0-9_]{1,80}$/.test(row.reason) && Object.keys(row.value).length === 0;
      return row.availability === "available" && row.reason === undefined && sessionUuid(row.runId)
        && Object.entries(row.value).every(([field, child]) => field === "budget"
          ? exactSessionObject(child, ["max_model_requests", "max_model_attempts", "model_requests", "attempts"])
            && Object.values(child).every(sessionInteger)
          : field === "revision" ? sessionInteger(child)
            : field === "run_id" ? child === row.runId : typeof child === "string");
    });
}
