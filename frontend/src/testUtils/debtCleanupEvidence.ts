import { vi } from "vitest";
import { requestEvidencePorts } from "../adapters/requestEvidenceApi";
import { memoryRequestEvidence } from "./requestEvidence";

export function installDebtCleanupEvidence() {
  const evidence = memoryRequestEvidence();
  vi.spyOn(requestEvidencePorts, "read").mockImplementation(evidence.ports.read);
  vi.spyOn(requestEvidencePorts, "write").mockImplementation(evidence.ports.write);
  vi.spyOn(requestEvidencePorts, "resolve").mockImplementation(evidence.ports.resolve);
  return evidence;
}
