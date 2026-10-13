import { isCredentialRequest, isProviderCredentialRequest } from "./providerConfigurationApi";
import { isRequestEvidenceRecord, requestEvidencePorts, type RequestEvidencePorts, type RequestEvidenceRecord } from "./requestEvidenceApi";
import { graphClone, graphJsonSignature } from "../domain/workflowGraph";
import type { CredentialIntent } from "../application/providerCredentialEditing";
import { isLocalDraftRecord, localDraftPorts, type LocalDraftPorts } from "./localDraftsApi";

export function createProviderCredentialEvidenceChannel(owner: string,
  ports: RequestEvidencePorts = requestEvidencePorts, legacyRead: LocalDraftPorts["read"] = localDraftPorts.read) {
  const ownerKind = "provider-credential";
  let original: RequestEvidenceRecord | null = null;
  async function legacyPresent() {
    const address = { scope: "management" as const, kind: "intent" as const, key: `credential:${owner}`,
      workflow_definition_id: null, workflow_session_id: null };
    const record = await legacyRead(address);
    if (record !== null && (!isLocalDraftRecord(record)
      || graphJsonSignature(record.address) !== graphJsonSignature(address)))
      throw new Error("旧原 Key 请求证据格式或身份无法确认，禁止新保存");
    return record !== null && !record.deleted;
  }
  function validate(record: RequestEvidenceRecord) {
    if (!isRequestEvidenceRecord(record) || record.owner_kind !== ownerKind || record.owner_id !== owner
      || record.operation === "provider.credential.save" && (!isProviderCredentialRequest(record.request)
        || record.request.provider_seed === null
        || `${record.request.identity.scope}:${record.request.identity.resource_id}` !== owner)
      || record.operation === "credential.delete" && !isCredentialRequest(record.request)
      || !["provider.credential.save", "credential.delete"].includes(record.operation))
      throw new Error("原 Key 请求证据格式或归属不匹配，未覆盖");
    return graphClone(record);
  }
  function sameOriginal(record: RequestEvidenceRecord, prior: RequestEvidenceRecord) {
    return record.operation === prior.operation && record.idempotency_key === prior.idempotency_key
      && record.request_digest === prior.request_digest && record.owner_kind === prior.owner_kind
      && record.owner_id === prior.owner_id && graphJsonSignature(record.request) === graphJsonSignature(prior.request);
  }
  async function read(_refresh = false) {
    const records = await ports.read(ownerKind, owner);
    const legacy = await legacyPresent();
    const pending = records.filter(record => record.resolution === "pending");
    if (pending.length > 1) throw new Error("同一供应商存在多条未决原请求，未覆盖");
    if (pending[0]) {
      const found = validate(pending[0]);
      if (original && !sameOriginal(found, original)) throw new Error("原 Key 请求证据已变化，未替换");
      return original = found;
    }
    if (original) {
      const found = records.find(record => record.operation === original!.operation
        && record.idempotency_key === original!.idempotency_key);
      if (!found || !sameOriginal(found, original)) throw new Error("原 Key 请求证据已变化，未替换");
      return original = validate(found);
    }
    if (legacy) throw new Error("旧原 Key 请求证据尚未接管，禁止新保存；原记录未修改");
    return null;
  }
  async function write(intent: CredentialIntent | null, _formatVersion = 2) {
    if (intent !== null) {
      if (await legacyPresent())
        throw new Error("旧原 Key 请求证据尚未接管，禁止新保存；原记录未修改");
      const operation = intent.kind === "delete" ? "credential.delete" : "provider.credential.save";
      if (intent.kind !== "save" && intent.kind !== "delete")
        throw new Error("原 Key 请求操作不支持新提交，未保存");
      const request = graphClone(intent.request) as unknown as Record<string, unknown>;
      const saved = validate(await ports.write({ operation, idempotency_key: intent.request.idempotency_key,
        owner_kind: ownerKind, owner_id: owner, request }));
      if (saved.resolution !== "pending" || saved.operation !== operation
        || graphJsonSignature(saved.request) !== graphJsonSignature(request))
        throw new Error("已落盘 Key 请求证据与原请求不匹配，Key 未发送");
      original = saved;
      return graphClone(saved);
    }
    if (!original) throw new Error("没有准确原 Key 请求证据，未结案");
    const prior = graphClone(original), current = await read(true);
    if (!current || !sameOriginal(current, prior))
      throw new Error("原 Key 请求证据已变化，未结案");
    const resolved = current.resolution === "pending" ? validate(await ports.resolve({
      operation: prior.operation, idempotency_key: prior.idempotency_key,
      request_digest: prior.request_digest, resolution: "verified",
    })) : current;
    if (!sameOriginal(resolved, prior) || resolved.resolution !== "verified" && resolved.resolution !== "archived")
      throw new Error("原 Key 请求证据尚未结案，仍保留");
    original = null;
    return graphClone(resolved);
  }
  async function archive(intent: CredentialIntent) {
    const current = await read(true);
    if (!current || current.operation !== (intent.kind === "delete" ? "credential.delete" : "provider.credential.save")
      || current.idempotency_key !== intent.request.idempotency_key
      || graphJsonSignature(current.request) !== graphJsonSignature(intent.request))
      throw new Error("原 Key 请求证据无法确认，未归档");
    if (current.resolution === "verified") throw new Error("原 Key 请求已核实结案，不能改为归档");
    original = current.resolution === "archived" ? current : validate(await ports.resolve({
      operation: current.operation, idempotency_key: current.idempotency_key,
      request_digest: current.request_digest, resolution: "archived",
    }));
    if (!sameOriginal(original, current) || original.resolution !== "archived")
      throw new Error("原 Key 请求归档未核实，仍保留");
  }
  return { read, write, archive };
}
