import { isExactPromptSelection, sessionUuid, type ExactPromptSelection } from "./workbenchSessions";
import { isExactModelSelection, type ExactModelSelection } from "../domain/modelConfiguration";
import { isExposureReference, type ExposureReference } from "../domain/exposureConfiguration";

export const DEFAULT_LEGACY_UI_URL = "http://127.0.0.1:8765/";

export function resolveLegacyUiUrl(configuredUrl?: string): string {
  const candidate = configuredUrl?.trim();
  if (!candidate) return DEFAULT_LEGACY_UI_URL;

  try {
    const url = new URL(candidate);
    if (url.protocol === "http:" || url.protocol === "https:") {
      return url.href;
    }
  } catch {
    // Invalid configuration keeps the original local user interface reachable.
  }
  return DEFAULT_LEGACY_UI_URL;
}

export const legacyUiUrl = resolveLegacyUiUrl(import.meta.env.VITE_LEGACY_UI_URL);

export function graphChatInterfaceUrl(base: string, definitionId: string, sessionId: string | null) {
  const url = new URL(resolveLegacyUiUrl(base));
  if (!["http:", "https:"].includes(url.protocol) || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
    || url.username || url.password || !sessionUuid(definitionId)
    || sessionId !== null && !sessionUuid(sessionId)) return url.href;
  for (const key of ["session", "prompt_a", "prompt_a_revision", "prompt_b", "prompt_b_revision",
    "model_config", "model_revision", "exposure_config", "exposure_revision", "graph_session"])
    url.searchParams.delete(key);
  url.searchParams.set("graph_workflow", definitionId);
  if (sessionId) url.searchParams.set("graph_session", sessionId);
  return url.href;
}

export function workbenchUserInterfaceUrl(
  base: string, sessionId: string | null, selection: ExactPromptSelection | null,
  modelSelection: ExactModelSelection | null = null,
  exposureReference: ExposureReference | null = null,
) {
  const url = new URL(resolveLegacyUiUrl(base));
  // Only the configured local backend shares the workbench's session identities.
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname)
    || url.port !== "8765" || !sessionUuid(sessionId)) return url.href;
  url.searchParams.set("session", sessionId);
  if (exposureReference && isExposureReference(exposureReference)) {
    url.searchParams.set("exposure_config", exposureReference.config_id);
    url.searchParams.set("exposure_revision", String(exposureReference.revision));
  }
  if (modelSelection && isExactModelSelection(modelSelection)) {
    url.searchParams.set("model_config", modelSelection.config_id);
    url.searchParams.set("model_revision", String(modelSelection.revision));
  }
  if (selection && isExactPromptSelection(selection))
    for (const stage of ["A", "B"] as const) {
      url.searchParams.set(`prompt_${stage.toLowerCase()}`, selection.nodes[stage].config_id);
      url.searchParams.set(`prompt_${stage.toLowerCase()}_revision`, String(selection.nodes[stage].revision));
    }
  return url.href;
}
