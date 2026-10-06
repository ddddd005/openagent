import { describe, expect, it } from "vitest";
import { readLocalDraft, saveLocalDraft, STORAGE_KEY } from "./localDraft";
import { createFixtureDraft } from "../fixtures/catalog";

describe("browser-local storage", () => {
  it("round trips a local draft", () => {
    const memory = new Map<string, string>();
    const storage = {
      getItem: (k: string) => memory.get(k) ?? null,
      setItem: (k: string, v: string) => {
        memory.set(k, v);
      },
    };
    const d = createFixtureDraft();
    saveLocalDraft(storage, d);
    expect(readLocalDraft(storage)).toEqual({ draft: d, error: null });
    expect(memory.has(STORAGE_KEY)).toBe(true);
  });
  it("preserves corrupt raw data and reports the problem", () => {
    let raw = "{bad-json";
    const storage = {
      getItem: () => raw,
      setItem: (_: string, v: string) => {
        raw = v;
      },
    };
    expect(readLocalDraft(storage).error).toBeTruthy();
    expect(raw).toBe("{bad-json");
  });
  it("handles inaccessible browser storage", () => {
    const storage = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
    };
    expect(readLocalDraft(storage).error).toBeTruthy();
    expect(() => saveLocalDraft(storage, createFixtureDraft())).toThrow(
      "denied",
    );
  });
});
