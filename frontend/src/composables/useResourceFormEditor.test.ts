import { describe, expect, it } from "vitest";
import { graphClone } from "../domain/workflowGraph";
import { newProvider } from "../domain/workflowModelResources";
import { useResourceFormEditor } from "./useResourceFormEditor";

describe("resource memory form", () => {
  it("discards unsubmitted edits when switching or reopening instead of restoring a stash", () => {
    const editor = useResourceFormEditor(newProvider), first = newProvider(), second = newProvider();
    editor.edit(first);
    editor.form.value!.value.name = "Unsubmitted edit";
    editor.edit(second);
    editor.edit(first);
    expect(editor.form.value!.value.name).toBe(first.value.name);
    editor.form.value!.value.name = "Another edit";
    editor.close();
    editor.edit(first);
    expect(editor.form.value).toEqual(first);
    expect(useResourceFormEditor(newProvider).form.value).toBeNull();
    expect(first.value.name).not.toBe("Unsubmitted edit");
  });

  it("uses sequence zero for creation and copies, and the saved sequence for editing", () => {
    const editor = useResourceFormEditor(newProvider), saved = { ...newProvider(), update_sequence: 7 };
    editor.edit(saved);
    expect(editor.expectedSequence.value).toBe(7);
    editor.edit(saved, true);
    expect(editor.expectedSequence.value).toBe(0);
    editor.edit(null);
    expect(editor.expectedSequence.value).toBe(0);
  });

  it("accepts only the submitted identity, sequence and contents despite key order normalization", () => {
    const editor = useResourceFormEditor(newProvider), original = newProvider();
    editor.edit(original);
    const saved = { ...graphClone(original), update_sequence: original.update_sequence + 1 };
    saved.value = { enabled: original.value.enabled, credential_ref: original.value.credential_ref,
      base_url: original.value.base_url, protocol: original.value.protocol, name: original.value.name };
    expect(editor.acceptSubmitted(saved, original.update_sequence + 1)).toBe(false);
    editor.form.value!.value.name = "Later input";
    expect(editor.acceptSubmitted(saved, original.update_sequence)).toBe(false);
    editor.edit(newProvider());
    expect(editor.acceptSubmitted(saved, original.update_sequence)).toBe(false);
    editor.edit(original);
    expect(editor.acceptSubmitted(saved, original.update_sequence)).toBe(true);
    expect(editor.form.value).toBeNull();
  });

  it("supports a public projection without discarding later projected edits", () => {
    const editor = useResourceFormEditor(newProvider), original = newProvider();
    editor.edit(original);
    const saved = { ...graphClone(original), update_sequence: 2, data_schema_version: 3 as const };
    const project = (record: typeof original) => record.value;
    expect(editor.acceptSubmitted(saved, original.update_sequence, project)).toBe(true);
    editor.edit(original);
    editor.form.value!.value.name = "Later input";
    expect(editor.acceptSubmitted(saved, original.update_sequence, project)).toBe(false);
  });

  it("updates the accepted Key basis without dropping unrelated in-memory fields", () => {
    const editor = useResourceFormEditor(newProvider), original = newProvider();
    editor.edit(original);
    editor.form.value!.value.name = "Current input";
    const saved = { ...original, update_sequence: 2 };
    expect(editor.acceptCredential(saved, 1, { data_schema_version: 3 })).toBe(true);
    expect(editor.form.value!.value.name).toBe("Current input");
    expect(editor.expectedSequence.value).toBe(2);
    expect(editor.acceptCredential({ ...saved, update_sequence: 3 }, 1, {})).toBe(false);
    expect(editor.expectedSequence.value).toBe(2);
  });

  it("reloads only an explicitly confirmed saved version of the current resource", async () => {
    const editor = useResourceFormEditor(newProvider), original = newProvider();
    editor.edit(original);
    editor.form.value!.value.name = "Current input";
    const saved = { ...graphClone(original), update_sequence: 2 };
    expect(await editor.reloadSaved(saved, false)).toBe(false);
    expect(await editor.reloadSaved(newProvider(), true)).toBe(false);
    expect(await editor.reloadSaved({ ...saved, update_sequence: -1 }, true)).toBe(false);
    expect(editor.form.value!.value.name).toBe("Current input");
    expect(await editor.reloadSaved(saved, true)).toBe(true);
    expect(editor.form.value).toEqual(saved);
    expect(editor.expectedSequence.value).toBe(2);
  });
});
