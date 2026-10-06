import { spawnSync } from "node:child_process";
import { delimiter, join } from "node:path";
import { env } from "node:process";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { decodeWorkbenchContextPreview } from "../adapters/workbenchContext";
import { contextReadFixture, contextScope } from "../adapters/workbenchContext.fixtures";
import { createPreparationDraft } from "../fixtures/preparation";
import {
  compilePromptItems, createPreparationNode, newPreparationId,
  type PreparationDraft, type PreparationNode,
} from "./preparation";

const backendProject = fileURLToPath(new URL("../../../backend/", import.meta.url));
const pythonPreview = `
import json
import sys
from phase1_agent.workflow_context_view import (
    ArchivedContext, materialize_prompt_draft, preview_context,
)

# Reproduce the catalog's canonical JSON key ordering without opening a store.
request = json.loads(json.dumps(json.load(sys.stdin), sort_keys=True))
archive = ArchivedContext(**request["archive"])
config = materialize_prompt_draft(request["draft"])
result = preview_context(
    archive, request["scope"], config, text=request["text"], stage="A",
)
json.dump(result, sys.stdout, ensure_ascii=True, allow_nan=False)
`;

function of<K extends PreparationNode["kind"]>(draft: PreparationDraft, kind: K) {
  return draft.nodes.find((node) => node.kind === kind) as Extract<PreparationNode, { kind: K }>;
}
function connect(draft: PreparationDraft, source: string, target: string,
  sourceHandle: string, targetHandle = sourceHandle) {
  draft.edges.push({ id: newPreparationId(), source, target, sourceHandle, targetHandle });
}
function prepare(draft: PreparationDraft, read = contextReadFixture(), text = "next") {
  const compiled = compilePromptItems(draft);
  expect(compiled.diagnostics.filter((entry) => entry.severity === "error")).toEqual([]);
  expect(compiled.config.schema_version).toBe(2);
  const promptConfig = { items: compiled.items, groups: compiled.groups, config: compiled.config };
  const result = spawnSync(env.PHASE1_TEST_PYTHON ?? "python", ["-c", pythonPreview], {
    cwd: backendProject,
    input: JSON.stringify({
      draft: promptConfig, text, scope: read.scope,
      archive: {
        messages: read.messages, logical_floors: read.logical_floors,
        protected_blocks: read.protected_blocks, turns: read.turns,
      },
    }),
    encoding: "utf8",
    env: {
      ...env, PYTHONIOENCODING: "utf-8",
      PYTHONPATH: [join(backendProject, "src"), env.PYTHONPATH].filter(Boolean).join(delimiter),
    },
    timeout: 30_000,
    maxBuffer: 8_000_000,
  });
  if (result.error)
    throw new Error(`跨语言准备图测试无法调用 Python：${result.error.message}。可配置 PHASE1_TEST_PYTHON。`);
  if (result.status !== 0)
    throw new Error(`Python 准备图契约验证失败（退出码 ${result.status}）：\n${result.stderr}`);
  const raw: unknown = JSON.parse(result.stdout);
  const decoded = decodeWorkbenchContextPreview(raw, contextScope(), { text, promptConfig });
  if (decoded.status !== "ready") throw new Error("A 预览必须生成确定的装配结果");
  return { compiled, preparation: decoded.preparation };
}
function base() {
  const draft = createPreparationDraft("main", "A");
  of(draft, "prompt-group").config.enabled = false;
  return draft;
}

describe("compiled preparation wire with the real Python processor", () => {
  it("round trips Python regex syntax, exact prompt instances and immutable presentation metadata", () => {
    const draft = base();
    const item = of(draft, "prompt-item");
    const collect = of(draft, "prompt-collect");
    const regex = createPreparationNode("regex");
    if (regex.kind !== "regex") throw new Error("wrong node");
    item.config.text = "city {{literal}}";
    item.config.presentation.role = "user";
    item.config.presentation.order = 17;
    regex.config = {
      mode: "prompt", pattern: "(?P<place>city)", replacement: "\\g<place>-town",
      flags: ["i"], replaceMode: "all",
    };
    draft.nodes.push(regex);
    draft.edges = draft.edges.filter((edge) => !(edge.source === item.id && edge.target === collect.id));
    collect.config.inputs = collect.config.inputs.map((id) => id === item.id ? regex.id : id);
    connect(draft, item.id, regex.id, "prompt");
    connect(draft, regex.id, collect.id, "prompt");
    const { preparation } = prepare(draft);
    expect(preparation.schema_version).toBe(2);
    expect(preparation.collection.items.find((entry) => entry.item_instance_id === item.id)).toMatchObject({
      item_id: item.config.itemId, revision: item.config.revision,
      item_instance_id: item.id, role: "user", placement: "before",
      depth: null, order: 17, interpolation: "literal", text: "city-town {{literal}}",
    });
    expect(preparation.program).toMatchObject({ kind: "preparation_program_result" });
  }, 30_000);

  it("captures a Chinese variable into text b, updates the original and keeps unprocessed fixed macros literal", () => {
    const draft = base();
    const fixed = of(draft, "prompt-item");
    fixed.config.text = "{{倒计时}} remains literal";
    const countdown = createPreparationNode("variable-register");
    const source = createPreparationNode("text");
    const capture = createPreparationNode("variable-replace");
    const b = createPreparationNode("variable-register");
    const update = createPreparationNode("variable-assign");
    const later = createPreparationNode("text");
    const replace = createPreparationNode("variable-replace");
    const converter = createPreparationNode("text-to-prompt");
    if (countdown.kind !== "variable-register" || source.kind !== "text"
      || capture.kind !== "variable-replace" || b.kind !== "variable-register"
      || update.kind !== "variable-assign" || later.kind !== "text"
      || replace.kind !== "variable-replace") throw new Error("wrong node");
    countdown.config = { name: "倒计时", valueType: "integer", hasInitialValue: true, initialValue: 10 };
    source.config.text = "{{倒计时}}";
    capture.config.mode = "text";
    b.config.name = "b";
    update.config = { name: "倒计时", valueType: "integer", operation: "subtract", value: 1 };
    later.config.text = "{{b}}/{{倒计时}}";
    replace.config.mode = "text";
    draft.nodes.push(countdown, source, capture, b, update, later, replace, converter);
    connect(draft, source.id, capture.id, "text");
    connect(draft, capture.id, b.id, "text");
    connect(draft, later.id, replace.id, "text");
    connect(draft, replace.id, converter.id, "text");
    const collect = of(draft, "prompt-collect");
    collect.config.inputs.push(converter.id);
    connect(draft, converter.id, collect.id, "prompt");
    const { compiled, preparation } = prepare(draft);
    expect(preparation.collection.items.find((entry) => entry.item_instance_id === fixed.id)?.text)
      .toBe("{{倒计时}} remains literal");
    expect(preparation.collection.items.find((entry) => entry.item_instance_id === converter.id)?.text).toBe("10/9");
    expect(preparation.program).toMatchObject({
      state: { values: { 倒计时: { type: "integer", value: 9 }, b: { type: "string", value: "10" } } },
    });
    if (compiled.config.schema_version !== 2) throw new Error("wrong version");
    const stages = preparation.program!.stages as { node_id: string }[];
    expect(stages.map((stage) => stage.node_id)).toEqual(compiled.config.preparation.nodes.map((node) => node.node_id));
    expect(new Set(stages.map((stage) => stage.node_id)).size).toBe(stages.length);
  }, 30_000);

  it("processes the send context while keeping canonical history, display and protected final unchanged", () => {
    const draft = base();
    const read = contextReadFixture();
    read.messages = read.messages.map((message, index) => ({
      ...message, blocks: [{ kind: "text", text: index === 1
        ? "city {{倒计时}}" : '{"text":"city {{倒计时}}"}' }],
    }));
    const context = of(draft, "context");
    const assembly = of(draft, "assemble");
    const declaration = createPreparationNode("variable-register");
    const regex = createPreparationNode("regex");
    const replacement = createPreparationNode("variable-replace");
    if (declaration.kind !== "variable-register" || regex.kind !== "regex") throw new Error("wrong node");
    declaration.config = { name: "倒计时", valueType: "integer", hasInitialValue: true, initialValue: 9 };
    regex.config = { mode: "prompt", pattern: "city", replacement: "town", flags: [], replaceMode: "all" };
    draft.nodes.push(declaration, regex, replacement);
    draft.edges = draft.edges.filter((edge) => edge.source !== context.id);
    connect(draft, context.id, regex.id, "context", "prompt");
    connect(draft, regex.id, replacement.id, "prompt");
    connect(draft, replacement.id, assembly.id, "prompt", "context");
    const { preparation } = prepare(draft, read);
    expect(preparation.canonical_messages.slice(0, read.messages.length)).toEqual(read.messages);
    expect(preparation.display_view.messages.slice(0, read.messages.length)).toEqual(read.messages);
    expect(preparation.display_view.overrides).toEqual([]);
    expect(preparation.send_view.overrides).toContainEqual({
      message_id: read.messages[0].message_id, block_index: 0, text: '{"text":"town 9"}',
    });
    expect(preparation.send_view.overrides).toContainEqual({
      message_id: read.messages[1].message_id, block_index: 0, text: "town 9",
    });
    expect(preparation.send_view.overrides.some((entry) => entry.message_id === read.messages[2].message_id)).toBe(false);
    expect(preparation.protected_blocks).toEqual(read.protected_blocks);
    expect(preparation.s0.find((message) => message.message_id === read.messages[2].message_id)).toEqual(read.messages[2]);
  }, 30_000);

  it("keeps twelve productive inputs in declared order after canonical JSON sorts input-10 before input-2", () => {
    const draft = base();
    const item = of(draft, "prompt-item");
    const collect = of(draft, "prompt-collect");
    item.config.text = "entry-0";
    const expectedInstances = [item.id];
    const expectedTexts = ["entry-0"];
    for (let index = 1; index < 12; index += 1) {
      const source = createPreparationNode("prompt-item");
      if (source.kind !== "prompt-item") throw new Error("wrong node");
      source.config.text = `entry-${index}`;
      source.config.presentation.order = 0;
      draft.nodes.push(source);
      collect.config.inputs.push(source.id);
      connect(draft, source.id, collect.id, "prompt");
      expectedInstances.push(source.id);
      expectedTexts.push(source.config.text);
    }
    const declaration = createPreparationNode("variable-register");
    if (declaration.kind !== "variable-register") throw new Error("wrong node");
    declaration.config.name = "wire_order";
    draft.nodes.push(declaration);
    const { compiled, preparation } = prepare(draft);
    if (compiled.config.schema_version !== 2) throw new Error("wrong version");
    const programCollector = compiled.config.preparation.nodes.find((node) => node.node_id === collect.id)!;
    const order = collect.config.inputs.map((_, index) => `input-${index}`);
    expect(order.length).toBeGreaterThanOrEqual(12);
    expect(programCollector.config.input_order).toEqual(order);
    expect([...Object.keys(programCollector.inputs)].sort()).not.toEqual(order);
    const productive = preparation.collection.items.filter((entry) =>
      expectedInstances.includes(entry.item_instance_id as string));
    expect(productive.map((entry) => entry.item_instance_id)).toEqual(expectedInstances);
    expect(productive.map((entry) => entry.text)).toEqual(expectedTexts);
    expect(productive.map((entry) => entry.declaration_index)).toEqual(Array.from({ length: 12 }, (_, index) => index));
    expect(preparation.s0.filter((message) => message.source.kind === "prompt")
      .slice(0, 12).map((message) => message.blocks[0].text)).toEqual(expectedTexts);
    const stages = preparation.program!.stages as { node_id: string; output: { items?: Record<string, unknown>[] } }[];
    expect(stages.find((stage) => stage.node_id === collect.id)?.output.items?.map((entry) => entry.text))
      .toEqual(expectedTexts);
  }, 30_000);
});
