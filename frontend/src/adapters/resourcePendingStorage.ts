import { requestEvidencePorts, type RequestEvidencePorts, type RequestEvidenceRecord } from "./requestEvidenceApi";
import { graphJsonSignature, graphObject } from "../domain/workflowGraph";

export function createResourcePendingStorage(key: string, legacyKey: string, operation = "resource.save",
  ports: RequestEvidencePorts = requestEvidencePorts) {
  const ownerKind = "resource-channel";
  let original: RequestEvidenceRecord | null = null;
  let archivedLocally = false;
  async function readPending() {
    const records = await ports.read(ownerKind, key);
    const record = records.find(value => value.resolution === "pending") ?? null;
    if (record && record.operation !== operation) throw new Error("本机原请求操作不匹配，原记录未覆盖");
    if (record) original = record;
    else if (original) {
      const resolved = records.find(value => value.operation === original!.operation
        && value.idempotency_key === original!.idempotency_key
        && value.request_digest === original!.request_digest);
      if (resolved?.resolution === "archived") { original = resolved; archivedLocally = true; }
    }
    if (!record && archivedLocally && original?.resolution === "archived")
      return JSON.stringify(original.request);
    return record === null ? null : JSON.stringify(record.request);
  }
  async function writePending(value: string | null) {
    if (value !== null) {
      const request: unknown = JSON.parse(value);
      if (!graphObject(request) || typeof request.idempotency_key !== "string")
        throw new Error("本机原请求身份无效，未提交");
      original = await ports.write({ operation, idempotency_key: request.idempotency_key,
        owner_kind: ownerKind, owner_id: key, request });
      archivedLocally = false;
      return;
    }
    if (!original) return;
    const records = await ports.read(ownerKind, key);
    const current = records.find(record => record.operation === original!.operation
      && record.idempotency_key === original!.idempotency_key);
    if (!current || current.request_digest !== original.request_digest)
      throw new Error("原请求证据已变化，未结案");
    if (current.resolution === "pending")
      original = await ports.resolve({ operation, idempotency_key: original.idempotency_key,
        request_digest: original.request_digest, resolution: "verified" });
    else original = current;
    archivedLocally = false;
    original = null;
  }
  function readLegacyPending() {
    return !legacyKey || typeof localStorage === "undefined" ? null : localStorage.getItem(legacyKey);
  }
  async function archivePending(raw: string) {
    await readPending();
    if (!original || !["pending", "archived"].includes(original.resolution)
      || graphJsonSignature(original.request) !== graphJsonSignature(JSON.parse(raw)))
      throw new Error("原请求证据不匹配，未归档");
    if (original.resolution === "pending")
      original = await ports.resolve({ operation, idempotency_key: original.idempotency_key,
        request_digest: original.request_digest, resolution: "archived" });
    archivedLocally = true;
  }
  return { readPending, writePending, readLegacyPending, archivePending };
}
