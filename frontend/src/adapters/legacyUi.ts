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

function localPage(base: string): URL | null {
  try {
    const url = new URL(base);
    return ["http:", "https:"].includes(url.protocol) && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      && !url.username && !url.password && ["/", "/static/index.html"].includes(url.pathname) ? url : null;
  } catch { return null; }
}
const strictUuid = (value: unknown): value is string => sessionUuid(value) && value.length === 36;

export function graphChatInterfaceUrl(base: string, definitionId: string, sessionId: string | null): string | null {
  const url = localPage(base);
  if (!url || !strictUuid(definitionId) || sessionId !== null && !strictUuid(sessionId)) return null;
  url.search = "";
  url.searchParams.set("graph_workflow", definitionId);
  if (sessionId) url.searchParams.set("graph_session", sessionId);
  return url.href;
}

export function workbenchUserInterfaceUrl(
  base: string, sessionId: string | null, selection: ExactPromptSelection | null,
  modelSelection: ExactModelSelection | null = null,
  exposureReference: ExposureReference | null = null,
): string | null {
  const url = localPage(base);
  // Only the configured local backend shares the workbench's session identities.
  if (!url || url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname)
    || url.port !== "8765" || !strictUuid(sessionId)) return null;
  url.search = "";
  url.searchParams.set("session", sessionId);
  if (exposureReference && isExposureReference(exposureReference) && strictUuid(exposureReference.config_id)) {
    url.searchParams.set("exposure_config", exposureReference.config_id);
    url.searchParams.set("exposure_revision", String(exposureReference.revision));
  }
  if (modelSelection && isExactModelSelection(modelSelection) && strictUuid(modelSelection.config_id)) {
    url.searchParams.set("model_config", modelSelection.config_id);
    url.searchParams.set("model_revision", String(modelSelection.revision));
  }
  if (selection && isExactPromptSelection(selection) && Object.values(selection.nodes).every(ref => strictUuid(ref.config_id)))
    for (const stage of ["A", "B"] as const) {
      url.searchParams.set(`prompt_${stage.toLowerCase()}`, selection.nodes[stage].config_id);
      url.searchParams.set(`prompt_${stage.toLowerCase()}_revision`, String(selection.nodes[stage].revision));
    }
  return url.href;
}
