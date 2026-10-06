import type { LocalDraft } from "../domain/draft";
import { isLocalDraft } from "../domain/graph";

export const STORAGE_KEY = "workflow-workbench:local-draft:v1";
export interface DraftStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}
export function readLocalDraft(storage: DraftStorage): {
  draft: LocalDraft | null;
  error: string | null;
} {
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return { draft: null, error: null };
    const draft: unknown = JSON.parse(raw);
    if (!isLocalDraft(draft))
      return { draft: null, error: "本地草稿格式不兼容，原始数据未覆盖。" };
    return { draft, error: null };
  } catch {
    return { draft: null, error: "本地草稿读取失败，原始数据未覆盖。" };
  }
}
export function saveLocalDraft(storage: DraftStorage, draft: LocalDraft) {
  if (!isLocalDraft(draft)) throw new Error("草稿格式无效。");
  storage.setItem(STORAGE_KEY, JSON.stringify(draft));
}
