import {
  createPreparationNode,
  newPreparationId,
  type PreparationDraft,
  type PreparationStage,
} from "../domain/preparation";

export function createPreparationDraft(
  workflowId: string, stage: PreparationStage,
): PreparationDraft {
  const item = createPreparationNode("prompt-item", { x: 40, y: 30 });
  const group = createPreparationNode("prompt-group", { x: 40, y: 260 });
  const prompts = createPreparationNode("prompt-collect", { x: 370, y: 110 });
  const tool = createPreparationNode("tool", { x: 40, y: 490 });
  const tools = createPreparationNode("tool-collect", { x: 370, y: 400 });
  const context = createPreparationNode("context", { x: 370, y: 650 });
  const input = createPreparationNode("root-input", { x: 700, y: 650 });
  const assembly = createPreparationNode("assemble", { x: 720, y: 270 });
  if (item.kind === "prompt-item") {
    item.title = `${stage} / 主提示词`;
    item.config.text = stage === "A"
      ? "Create a draft from the user's request. Use inspect_text, then call final_answer with {text: string}."
      : "Revise the complete draft JSON supplied by the upstream node. Use inspect_text, then call final_answer with {text: string}.";
  }
  if (group.kind === "prompt-group") {
    group.title = `${stage} / 补充条目组`;
    group.config.members[0].name = "输出要求";
    group.config.members[0].text = "Preserve the user's intent.";
    group.config.members[0].presentation.order = 10;
  }
  if (input.kind === "root-input") input.title = stage === "A" ? "用户当前输入" : "A 的业务结果";
  if (prompts.kind === "prompt-collect") prompts.config.inputs = [item.id, group.id];
  if (tools.kind === "tool-collect") tools.config.inputs = [tool.id];
  const edge = (source: string, target: string, handle: string) => ({
    id: newPreparationId(), source, target, sourceHandle: handle, targetHandle: handle,
  });
  return {
    workflowId, stage, revision: 1, configId: newPreparationId(),
    nodes: [item, group, prompts, tool, tools, context, input, assembly],
    edges: [
      edge(item.id, prompts.id, "prompt"),
      edge(group.id, prompts.id, "prompt"),
      edge(prompts.id, assembly.id, "prompt"),
      edge(tool.id, tools.id, "tool-descriptions"),
      edge(tool.id, tools.id, "tool-schemas"),
      edge(tools.id, assembly.id, "tool-descriptions"),
      edge(tools.id, assembly.id, "tool-schemas"),
      edge(context.id, assembly.id, "context"),
      edge(input.id, assembly.id, "current-input"),
    ],
  };
}
